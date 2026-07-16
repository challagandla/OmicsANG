# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Durable SQLite state for OmicsANG jobs, events, and user configuration.

The database is the control-plane source of truth.  Live PTY file descriptors
remain process-local, but their identity and lifecycle are persisted here so a
service restart can recover queued work and explicitly reconcile work that was
running when the process disappeared.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import settings

SCHEMA_VERSION = 1
DATABASE_NAME = "state.sqlite3"
ROOT_BINDING_KEY = "pipeline_root_binding_v1"
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "blocked"})

# Lost is deliberately recoverable: a restarted controller may still be able to
# signal an identity-checked process or later reconcile a scheduler record.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset(
        {
            "queued",
            "preparing",
            "submitting",
            "cancel_requested",
            "cancelled",
            "failed",
            "blocked",
            "lost",
        }
    ),
    "queued": frozenset(
        {
            "preparing",
            "cancel_requested",
            "cancelled",
            "failed",
            "blocked",
            "lost",
        }
    ),
    "preparing": frozenset(
        {
            "running",
            "submitting",
            "cancel_requested",
            "cancelled",
            "failed",
            "blocked",
            "lost",
        }
    ),
    "running": frozenset(
        {
            "pending",
            "succeeded",
            "failed",
            "cancel_requested",
            "cancelling",
            "cancelled",
            "lost",
        }
    ),
    "submitting": frozenset(
        {
            "submitted",
            "submission_unknown",
            "cancel_requested",
            "cancelling",
            "cancelled",
            "failed",
            "lost",
        }
    ),
    "submitted": frozenset(
        {
            "pending",
            "running",
            "succeeded",
            "failed",
            "cancel_requested",
            "cancelling",
            "cancelled",
            "submission_unknown",
            "lost",
        }
    ),
    "pending": frozenset(
        {
            "running",
            "succeeded",
            "failed",
            "cancel_requested",
            "cancelling",
            "cancelled",
            "lost",
        }
    ),
    "cancel_requested": frozenset(
        {
            "cancelling",
            "cancelled",
            "failed",
            "succeeded",
            "lost",
        }
    ),
    "cancelling": frozenset({"cancelled", "failed", "succeeded", "lost"}),
    "submission_unknown": frozenset(
        {
            "submitted",
            "pending",
            "running",
            "succeeded",
            "cancel_requested",
            "cancelling",
            "cancelled",
            "failed",
            "lost",
        }
    ),
    "lost": frozenset({"cancel_requested", "cancelling", "cancelled", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "blocked": frozenset(),
}


class StateError(RuntimeError):
    """Base class for durable-state errors."""


class InvalidTransition(StateError):
    """Raised when a caller attempts a state-machine transition that is illegal."""


class StateRootMismatch(StateError):
    """Raised when a state database belongs to another pipeline root."""


_INIT_LOCK = threading.RLock()
_INITIALIZED: dict[Path, tuple[int, int]] = {}


def database_path() -> Path:
    """Resolve dynamically so tests and alternate BENCHTOP_STATE roots stay isolated."""
    return settings.STATE_DIR / DATABASE_NAME


def _json_dump(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _json_load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _secure_database_files(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            try:
                candidate.chmod(0o600)
            except OSError:
                pass


@contextmanager
def _migration_lock(path: Path, timeout: float = 30.0):
    """Serialize schema and legacy-import work across server processes."""
    lock_path = path.with_name(path.name + ".migrate.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StateError("timed out waiting for the state migration lock")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def connect() -> sqlite3.Connection:
    settings.ensure_dirs()
    path = database_path()
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    try:
        with _INIT_LOCK:
            stat_result = path.stat()
            identity = (stat_result.st_dev, stat_result.st_ino)
            if _INITIALIZED.get(path) != identity:
                with _migration_lock(path):
                    conn.execute("PRAGMA journal_mode = WAL")
                    _migrate(conn)
                    _bind_or_verify_pipeline_root(conn, allow_create=True)
                    import_ready = _import_legacy_json(conn)
                    _secure_database_files(path)
                    if import_ready:
                        _INITIALIZED[path] = identity
            else:
                # The same state directory can be presented with a different
                # BENCHTOP_ROOT on a later launch (or in a long-lived test
                # process).  The inode cache must never bypass root binding.
                _bind_or_verify_pipeline_root(conn, allow_create=False)
    except Exception:
        conn.close()
        raise
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version > SCHEMA_VERSION:
        raise StateError(
            f"database schema {current_version} is newer than supported schema {SCHEMA_VERSION}"
        )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS configuration (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS legacy_sections (
            section TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            imported_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS history (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT NOT NULL UNIQUE,
            pipeline TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            sort_time REAL NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS history_pipeline_time
            ON history(pipeline, sort_time DESC, seq DESC);
        CREATE TABLE IF NOT EXISTS run_templates (
            pipeline TEXT NOT NULL,
            template_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created REAL NOT NULL,
            updated REAL NOT NULL,
            form_json TEXT NOT NULL,
            PRIMARY KEY (pipeline, template_id)
        );
        CREATE INDEX IF NOT EXISTS run_templates_pipeline_updated
            ON run_templates(pipeline, updated DESC);
        CREATE TABLE IF NOT EXISTS slurm_jobs (
            intent_id TEXT PRIMARY KEY,
            scheduler_job_id TEXT NOT NULL DEFAULT '',
            pipeline TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL,
            updated REAL NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS slurm_jobs_pipeline_updated
            ON slurm_jobs(pipeline, updated DESC);
        CREATE INDEX IF NOT EXISTS slurm_jobs_scheduler_id
            ON slurm_jobs(scheduler_job_id);
        CREATE TABLE IF NOT EXISTS capsule_refs (
            capsule_id TEXT PRIMARY KEY,
            pipeline TEXT NOT NULL DEFAULT '',
            job_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL,
            updated REAL NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS capsule_refs_pipeline_updated
            ON capsule_refs(pipeline, updated DESC);
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            executor TEXT NOT NULL,
            pipeline TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            state_version INTEGER NOT NULL DEFAULT 0,
            idempotency_key TEXT UNIQUE,
            plan_digest TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            scheduler_job_id TEXT NOT NULL DEFAULT '',
            pid INTEGER,
            process_start TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL,
            updated REAL NOT NULL,
            queued_at REAL,
            started REAL,
            ended REAL,
            cancel_requested_at REAL,
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_expires REAL,
            last_error TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            private_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs(state, created);
        CREATE INDEX IF NOT EXISTS jobs_pipeline_created ON jobs(pipeline, created DESC);
        CREATE INDEX IF NOT EXISTS jobs_scheduler_id ON jobs(scheduler_job_id);
        CREATE TABLE IF NOT EXISTS job_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            job_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            actor TEXT NOT NULL DEFAULT 'benchtop',
            reason TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS job_events_job_seq ON job_events(job_id, seq);
        CREATE TABLE IF NOT EXISTS leases (
            resource TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            token TEXT NOT NULL,
            acquired REAL NOT NULL,
            expires REAL NOT NULL,
            updated REAL NOT NULL,
            payload_json TEXT NOT NULL
        );
        """
    )
    now = time.time()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
            (1, "initial-wal-job-state", now),
        )
        if current_version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _metadata_value(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute(
        "SELECT value_json FROM metadata WHERE key = ?", (key,)
    ).fetchone()
    return _json_load(row[0], None) if row else None


def _set_metadata(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        """INSERT INTO metadata(key, value_json, updated_at) VALUES(?,?,?)
           ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
             updated_at=excluded.updated_at""",
        (key, _json_dump(value), time.time()),
    )


def _root_binding() -> dict[str, Any]:
    try:
        canonical_root = settings.canonical_pipeline_root()
    except ValueError as exc:
        raise StateError("pipeline root is unavailable for durable state") from exc
    return {"version": 1, "canonical_root": canonical_root.as_posix()}


def _bind_or_verify_pipeline_root(
    conn: sqlite3.Connection,
    *,
    allow_create: bool,
) -> dict[str, Any]:
    """Bind a database once and reject every later cross-root reuse."""
    expected = _root_binding()
    saved = _metadata_value(conn, ROOT_BINDING_KEY)
    if saved is None:
        if not allow_create:
            raise StateError("durable state root binding is missing")
        with conn:
            # The migration lock serializes the first binding across processes.
            _set_metadata(conn, ROOT_BINDING_KEY, expected)
        return expected
    if not isinstance(saved, dict) or saved.get("version") != 1:
        raise StateError("durable state root binding is malformed")
    if saved.get("canonical_root") != expected["canonical_root"]:
        raise StateRootMismatch(
            "OmicsANG state belongs to a different pipeline root; "
            "use that root or choose a separate state directory"
        )
    return saved


def _legacy_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _legacy_id(prefix: str, value: Any, index: int) -> str:
    material = _json_dump({"index": index, "value": value}).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:24]}"


def _import_legacy_json(conn: sqlite3.Connection) -> bool:
    """Import state.json exactly once without modifying or deleting the source."""
    marker = "legacy_json_import_v1"
    if _metadata_value(conn, marker) is not None:
        return True
    path = settings.STATE_FILE
    if not path.exists():
        conn.execute("BEGIN IMMEDIATE")
        try:
            _set_metadata(conn, marker, {"status": "absent", "path": str(path)})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return True
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("legacy state root is not an object")
    except (OSError, ValueError, TypeError):
        # Do not set the marker: a repaired file can still be imported later.
        return False

    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _metadata_value(conn, marker) is not None:
            conn.commit()
            return True
        history_value = data.get("history") or []
        history = list(history_value) if isinstance(history_value, list) else []
        for index, item in enumerate(reversed(history)):
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            entry_id = str(
                entry.get("id") or _legacy_id("legacy-history", entry, index)
            )
            sort_time = now - (len(history) - index) / 1000.0
            conn.execute(
                """INSERT INTO history(entry_id,pipeline,kind,status,sort_time,payload_json,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(entry_id) DO NOTHING""",
                (
                    entry_id,
                    str(entry.get("pipeline") or ""),
                    str(entry.get("kind") or ""),
                    str(entry.get("status") or ""),
                    sort_time,
                    _json_dump(entry),
                    now,
                ),
            )
        queue_config = data.get("run_queue")
        if isinstance(queue_config, dict):
            conn.execute(
                "INSERT OR REPLACE INTO configuration(key,value_json,updated_at) VALUES(?,?,?)",
                ("run_queue", _json_dump(queue_config), now),
            )
        templates = data.get("run_templates") or {}
        if isinstance(templates, dict):
            for pipeline, items in templates.items():
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict):
                        continue
                    tid = str(
                        item.get("id")
                        or _legacy_id(
                            "legacy-template",
                            {"pipeline": pipeline, "item": item},
                            0,
                        )
                    )
                    created = _legacy_float(item.get("created"), now)
                    updated = _legacy_float(item.get("updated"), created)
                    conn.execute(
                        """INSERT OR IGNORE INTO run_templates
                           (pipeline,template_id,name,created,updated,form_json)
                           VALUES(?,?,?,?,?,?)""",
                        (
                            str(pipeline),
                            tid,
                            str(item.get("name") or "Run profile"),
                            created,
                            updated,
                            _json_dump(item.get("form") or {}),
                        ),
                    )
        slurm_value = data.get("slurm_jobs") or []
        for slurm_index, item in enumerate(
            slurm_value if isinstance(slurm_value, list) else []
        ):
            if not isinstance(item, dict):
                continue
            scheduler_id = str(item.get("job_id") or "")
            intent_id = str(
                item.get("id")
                or (
                    f"slurm-{scheduler_id}"
                    if scheduler_id
                    else _legacy_id("legacy-slurm", item, slurm_index)
                )
            )
            created = _legacy_float(item.get("created"), now)
            conn.execute(
                """INSERT OR IGNORE INTO slurm_jobs
                   (intent_id,scheduler_job_id,pipeline,status,created,updated,payload_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    intent_id,
                    scheduler_id,
                    str(item.get("pipeline") or ""),
                    str(item.get("status") or ""),
                    created,
                    now,
                    _json_dump(item),
                ),
            )
            legacy_status = str(item.get("status") or "submitted").lower()
            canonical_state = {
                "submitting": "submitting",
                "submission-unknown": "submission_unknown",
                "submission_unknown": "submission_unknown",
                "submission-failed": "failed",
                "submitted": "submitted",
                "pending": "pending",
                "running": "running",
                "completed": "succeeded",
                "succeeded": "succeeded",
                "failed": "failed",
                "cancelled": "cancelled",
                "canceled": "cancelled",
            }.get(legacy_status, "submitted")
            cur = conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (id,kind,executor,pipeline,state,state_version,idempotency_key,plan_digest,
                    session_id,scheduler_job_id,created,updated,payload_json,private_json)
                   VALUES(?,?,?,?,?,0,NULL,?,?,?, ?,?,?,?)""",
                (
                    intent_id,
                    "run",
                    "slurm",
                    str(item.get("pipeline") or ""),
                    canonical_state,
                    str(item.get("plan_digest") or ""),
                    "",
                    scheduler_id,
                    created,
                    now,
                    _json_dump(
                        {
                            "title": str(item.get("title") or f"Slurm {scheduler_id}"),
                            "cores": item.get("cores") or 1,
                            "dryrun": bool(item.get("dryrun")),
                            "executor_state": legacy_status,
                            "command": str(item.get("command") or ""),
                            "logfile": str(item.get("output") or ""),
                            "capsule_id": str(item.get("capsule_id") or ""),
                            "slurm_job": item,
                            "legacy_imported": True,
                        }
                    ),
                    _json_dump({}),
                ),
            )
            if cur.rowcount:
                _insert_event(
                    conn,
                    intent_id,
                    "legacy_job_imported",
                    None,
                    canonical_state,
                    actor="migration",
                    reason="imported from state.json",
                )
        known_sections = {"history", "run_queue", "run_templates", "slurm_jobs"}
        for section, value in data.items():
            if section in known_sections:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO legacy_sections(section,payload_json,imported_at) VALUES(?,?,?)",
                (str(section), _json_dump(value), now),
            )
        _set_metadata(
            conn,
            marker,
            {
                "status": "imported",
                "path": str(path),
                "bytes": len(raw.encode("utf-8")),
                "history": len(history),
                "imported_at": now,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def status() -> dict[str, Any]:
    with closing(connect()) as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        migrations = [
            dict(row)
            for row in conn.execute(
                "SELECT version,name,applied_at FROM schema_migrations ORDER BY version"
            )
        ]
        legacy = _metadata_value(conn, "legacy_json_import_v1")
        root_binding = _metadata_value(conn, ROOT_BINDING_KEY)
    return {
        "path": str(database_path()),
        "journal_mode": str(journal).lower(),
        "schema_version": int(version),
        "migrations": migrations,
        "legacy_import": legacy,
        "root_binding": root_binding,
    }


# ---- compatibility persistence ------------------------------------------
def configuration(key: str, default: Any = None) -> Any:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT value_json FROM configuration WHERE key=?", (key,)
        ).fetchone()
    return _json_load(row[0], default) if row else default


def set_configuration(key: str, value: Any) -> Any:
    with closing(connect()) as conn, conn:
        conn.execute(
            """INSERT INTO configuration(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                 updated_at=excluded.updated_at""",
            (key, _json_dump(value), time.time()),
        )
    return value


def mutate_configuration(
    key: str,
    update: Callable[[Any], Any],
    default: Any = None,
) -> Any:
    """Atomically read, transform, and persist one configuration value."""
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value_json FROM configuration WHERE key=?",
                (key,),
            ).fetchone()
            current = _json_load(row[0], default) if row else default
            value = update(current)
            conn.execute(
                """INSERT INTO configuration(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                     updated_at=excluded.updated_at""",
                (key, _json_dump(value), time.time()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return value


def record_history(entry: dict[str, Any]) -> None:
    saved = dict(entry)
    entry_id = str(saved.get("id") or uuid.uuid4().hex)
    saved["id"] = entry_id
    now = time.time()
    with closing(connect()) as conn, conn:
        conn.execute(
            """INSERT INTO history(entry_id,pipeline,kind,status,sort_time,payload_json,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(entry_id) DO UPDATE SET pipeline=excluded.pipeline,
                 kind=excluded.kind,status=excluded.status,sort_time=excluded.sort_time,
                 payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
            (
                entry_id,
                str(saved.get("pipeline") or ""),
                str(saved.get("kind") or ""),
                str(saved.get("status") or ""),
                now,
                _json_dump(saved),
                now,
            ),
        )
        conn.execute(
            "DELETE FROM history WHERE seq NOT IN (SELECT seq FROM history ORDER BY sort_time DESC,seq DESC LIMIT 500)"
        )


def history(pipeline: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(0, min(int(limit), 500))
    sql = "SELECT payload_json FROM history"
    args: list[Any] = []
    if pipeline:
        sql += " WHERE pipeline=?"
        args.append(pipeline)
    sql += " ORDER BY sort_time DESC,seq DESC LIMIT ?"
    args.append(limit)
    with closing(connect()) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_json_load(row[0], {}) for row in rows]


def save_template(pipeline: str, template: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    tid = str(template.get("id") or uuid.uuid4().hex[:10])
    created = float(template.get("created") or now)
    saved = {
        "id": tid,
        "name": str(template.get("name") or "Run profile").strip() or "Run profile",
        "created": created,
        "updated": now,
        "form": dict(template.get("form") or {}),
    }
    with closing(connect()) as conn, conn:
        conn.execute(
            """INSERT INTO run_templates(pipeline,template_id,name,created,updated,form_json)
               VALUES(?,?,?,?,?,?) ON CONFLICT(pipeline,template_id) DO UPDATE SET
                 name=excluded.name,updated=excluded.updated,form_json=excluded.form_json""",
            (pipeline, tid, saved["name"], created, now, _json_dump(saved["form"])),
        )
        conn.execute(
            """DELETE FROM run_templates WHERE pipeline=? AND template_id NOT IN
               (SELECT template_id FROM run_templates WHERE pipeline=? ORDER BY updated DESC LIMIT 100)""",
            (pipeline, pipeline),
        )
    return saved


def templates(pipeline: str) -> list[dict[str, Any]]:
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT template_id,name,created,updated,form_json FROM run_templates
               WHERE pipeline=? ORDER BY updated DESC LIMIT 100""",
            (pipeline,),
        ).fetchall()
    return [
        {
            "id": row["template_id"],
            "name": row["name"],
            "created": row["created"],
            "updated": row["updated"],
            "form": _json_load(row["form_json"], {}),
        }
        for row in rows
    ]


def delete_template(pipeline: str, template_id: str) -> bool:
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "DELETE FROM run_templates WHERE pipeline=? AND template_id=?",
            (pipeline, template_id),
        )
    return cur.rowcount > 0


def record_slurm_job(job: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    scheduler_id = str(job.get("job_id") or "")
    intent_id = str(
        job.get("id") or (f"slurm-{scheduler_id}" if scheduler_id else uuid.uuid4().hex)
    )
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT payload_json,created FROM slurm_jobs WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        previous = _json_load(row["payload_json"], {}) if row else {}
        saved = {**previous, **dict(job), "id": intent_id}
        created = float(saved.get("created") or (row["created"] if row else now))
        saved["created"] = created
        conn.execute(
            """INSERT INTO slurm_jobs
               (intent_id,scheduler_job_id,pipeline,status,created,updated,payload_json)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(intent_id) DO UPDATE SET
                 scheduler_job_id=excluded.scheduler_job_id,pipeline=excluded.pipeline,
                 status=excluded.status,updated=excluded.updated,payload_json=excluded.payload_json""",
            (
                intent_id,
                str(saved.get("job_id") or ""),
                str(saved.get("pipeline") or ""),
                str(saved.get("status") or ""),
                created,
                now,
                _json_dump(saved),
            ),
        )
        conn.execute(
            "DELETE FROM slurm_jobs WHERE intent_id NOT IN (SELECT intent_id FROM slurm_jobs ORDER BY updated DESC LIMIT 500)"
        )
    return saved


def slurm_jobs(pipeline: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(0, min(int(limit), 500))
    sql = "SELECT payload_json FROM slurm_jobs"
    args: list[Any] = []
    if pipeline:
        sql += " WHERE pipeline=?"
        args.append(pipeline)
    sql += " ORDER BY updated DESC LIMIT ?"
    args.append(limit)
    with closing(connect()) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_json_load(row[0], {}) for row in rows]


def slurm_job(
    *, intent_id: str = "", scheduler_job_id: str = ""
) -> dict[str, Any] | None:
    if not intent_id and not scheduler_job_id:
        return None
    value = intent_id or scheduler_job_id
    query = (
        "SELECT payload_json FROM slurm_jobs WHERE intent_id=? "
        "ORDER BY updated DESC LIMIT 1"
        if intent_id
        else "SELECT payload_json FROM slurm_jobs WHERE scheduler_job_id=? "
        "ORDER BY updated DESC LIMIT 1"
    )
    with closing(connect()) as conn:
        row = conn.execute(query, (value,)).fetchone()
    return _json_load(row[0], {}) if row else None


def record_capsule_ref(capsule: dict[str, Any]) -> dict[str, Any]:
    capsule_id = str(capsule.get("id") or capsule.get("capsule_id") or "")
    if not capsule_id:
        raise StateError("capsule id is required")
    now = time.time()
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT payload_json,created FROM capsule_refs WHERE capsule_id=?",
            (capsule_id,),
        ).fetchone()
        previous = _json_load(row["payload_json"], {}) if row else {}
        saved = {**previous, **dict(capsule), "id": capsule_id}
        created = float(saved.get("created") or (row["created"] if row else now))
        saved["created"] = created
        conn.execute(
            """INSERT INTO capsule_refs
               (capsule_id,pipeline,job_id,status,path,fingerprint,created,updated,payload_json)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(capsule_id) DO UPDATE SET
                 pipeline=excluded.pipeline,job_id=excluded.job_id,status=excluded.status,
                 path=excluded.path,fingerprint=excluded.fingerprint,
                 updated=excluded.updated,payload_json=excluded.payload_json""",
            (
                capsule_id,
                str(saved.get("pipeline") or ""),
                str(saved.get("job_id") or ""),
                str(saved.get("status") or ""),
                str(saved.get("path") or ""),
                str(saved.get("fingerprint") or ""),
                created,
                now,
                _json_dump(saved),
            ),
        )
    return saved


def capsule_refs(pipeline: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT payload_json FROM capsule_refs"
    args: list[Any] = []
    if pipeline:
        sql += " WHERE pipeline=?"
        args.append(pipeline)
    sql += " ORDER BY updated DESC LIMIT ?"
    args.append(max(0, min(int(limit), 1000)))
    with closing(connect()) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_json_load(row[0], {}) for row in rows]


# ---- durable jobs and append-only events ---------------------------------
def _job_from_row(row: sqlite3.Row, include_private: bool = False) -> dict[str, Any]:
    data = _json_load(row["payload_json"], {})
    data.update(
        {
            "id": row["id"],
            "kind": row["kind"],
            "executor": row["executor"],
            "pipeline": row["pipeline"],
            "state": row["state"],
            "state_version": row["state_version"],
            "idempotency_key": row["idempotency_key"] or "",
            "plan_digest": row["plan_digest"],
            "session_id": row["session_id"],
            "scheduler_job_id": row["scheduler_job_id"],
            "pid": row["pid"],
            "process_start": row["process_start"],
            "created": row["created"],
            "updated": row["updated"],
            "queued_at": row["queued_at"],
            "started": row["started"],
            "ended": row["ended"],
            "cancel_requested_at": row["cancel_requested_at"],
            "lease_owner": row["lease_owner"],
            "lease_expires": row["lease_expires"],
            "last_error": row["last_error"],
        }
    )
    if include_private:
        data["_private"] = _json_load(row["private_json"], {})
    return data


def _insert_event(
    conn: sqlite3.Connection,
    job_id: str,
    event_type: str,
    from_state: str | None,
    to_state: str | None,
    *,
    actor: str = "benchtop",
    reason: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """INSERT INTO job_events
           (event_id,job_id,event_type,from_state,to_state,actor,reason,created,payload_json)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            uuid.uuid4().hex,
            job_id,
            event_type,
            from_state,
            to_state,
            actor,
            reason,
            time.time(),
            _json_dump(payload or {}),
        ),
    )


def create_job(
    *,
    job_id: str,
    kind: str,
    executor: str,
    pipeline: str = "",
    state: str = "created",
    idempotency_key: str = "",
    plan_digest: str = "",
    session_id: str = "",
    scheduler_job_id: str = "",
    payload: dict[str, Any] | None = None,
    private: dict[str, Any] | None = None,
    actor: str = "benchtop",
    lease_owner: str = "",
    lease_ttl: float = 0.0,
) -> tuple[dict[str, Any], bool]:
    if state not in ALLOWED_TRANSITIONS:
        raise StateError(f"unknown job state {state!r}")
    now = time.time()
    key = idempotency_key.strip() or None
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if key:
            row = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row:
                conn.commit()
                return _job_from_row(row), False
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row:
            conn.commit()
            return _job_from_row(row), False
        conn.execute(
            """INSERT INTO jobs
               (id,kind,executor,pipeline,state,state_version,idempotency_key,plan_digest,
                session_id,scheduler_job_id,created,updated,payload_json,private_json,
                lease_owner,lease_expires)
               VALUES(?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                kind,
                executor,
                pipeline,
                state,
                key,
                plan_digest,
                session_id,
                scheduler_job_id,
                now,
                now,
                _json_dump(payload or {}),
                _json_dump(private or {}),
                lease_owner,
                now + max(1.0, float(lease_ttl)) if lease_owner else None,
            ),
        )
        _insert_event(conn, job_id, "job_created", None, state, actor=actor)
        conn.commit()
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_from_row(row), True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_job(job_id: str, *, include_private: bool = False) -> dict[str, Any] | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job_from_row(row, include_private) if row else None


def job_by_idempotency(
    key: str, *, include_private: bool = False
) -> dict[str, Any] | None:
    if not key:
        return None
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE idempotency_key=?", (key,)
        ).fetchone()
    return _job_from_row(row, include_private) if row else None


def job_by_scheduler_id(
    job_id: str, *, include_private: bool = False
) -> dict[str, Any] | None:
    matches = jobs_by_scheduler_id(job_id, include_private=include_private)
    return matches[0] if len(matches) == 1 else None


def jobs_by_scheduler_id(
    job_id: str,
    *,
    include_private: bool = False,
) -> list[dict[str, Any]]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE scheduler_job_id=? ORDER BY created DESC LIMIT 10",
            (job_id,),
        ).fetchall()
    return [_job_from_row(row, include_private) for row in rows]


def list_jobs(
    *,
    pipeline: str | None = None,
    kind: str | None = None,
    executor: str | None = None,
    states: Iterable[str] | None = None,
    limit: int = 100,
    oldest_first: bool = False,
    include_private: bool = False,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if pipeline:
        clauses.append("pipeline=?")
        args.append(pipeline)
    if kind:
        clauses.append("kind=?")
        args.append(kind)
    if executor:
        clauses.append("executor=?")
        args.append(executor)
    state_list = list(states or [])
    if state_list:
        clauses.append("state IN (" + ",".join("?" for _ in state_list) + ")")
        args.extend(state_list)
    sql = "SELECT * FROM jobs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created " + ("ASC" if oldest_first else "DESC") + " LIMIT ?"
    args.append(max(0, min(int(limit), 1000)))
    with closing(connect()) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_job_from_row(row, include_private) for row in rows]


_UPDATABLE_JOB_COLUMNS = {
    "plan_digest",
    "session_id",
    "scheduler_job_id",
    "pid",
    "process_start",
    "queued_at",
    "started",
    "ended",
    "cancel_requested_at",
    "lease_owner",
    "lease_expires",
    "last_error",
}


def _merged_json(
    conn: sqlite3.Connection, job_id: str, column: str, update: dict | None
) -> str:
    if column == "payload_json":
        query = "SELECT payload_json FROM jobs WHERE id=?"
    elif column == "private_json":
        query = "SELECT private_json FROM jobs WHERE id=?"
    else:
        raise StateError("unsupported JSON job column")
    row = conn.execute(query, (job_id,)).fetchone()
    current = _json_load(row[0], {}) if row else {}
    if update:
        current.update(update)
    return _json_dump(current)


def update_job(
    job_id: str,
    *,
    payload_update: dict[str, Any] | None = None,
    private_update: dict[str, Any] | None = None,
    expected_states: Sequence[str] | None = None,
    expected_version: int | None = None,
    expected_payload: dict[str, Any] | None = None,
    fence_resource: str = "",
    fence_owner: str = "",
    fence_token: str = "",
    event_type: str = "job_updated",
    reason: str = "",
    actor: str = "benchtop",
    **fields: Any,
) -> dict[str, Any]:
    unknown = set(fields) - _UPDATABLE_JOB_COLUMNS
    if unknown:
        raise StateError(f"unsupported job fields: {sorted(unknown)}")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        if expected_states is not None and row["state"] not in set(expected_states):
            raise InvalidTransition(
                f"job {job_id} is {row['state']!r}, expected one of {list(expected_states)!r}"
            )
        if expected_version is not None and int(row["state_version"]) != int(
            expected_version
        ):
            raise InvalidTransition(
                f"job {job_id} state version changed from {expected_version} "
                f"to {row['state_version']}"
            )
        current_payload = _json_load(row["payload_json"], {})
        for key, value in (expected_payload or {}).items():
            if current_payload.get(key) != value:
                raise InvalidTransition(
                    f"job {job_id} payload field {key!r} changed before update"
                )
        if fence_resource:
            lease_row = conn.execute(
                "SELECT owner,token,expires FROM leases WHERE resource=?",
                (fence_resource,),
            ).fetchone()
            if (
                not fence_owner
                or not fence_token
                or not lease_row
                or lease_row["owner"] != fence_owner
                or lease_row["token"] != fence_token
                or float(lease_row["expires"] or 0) <= time.time()
            ):
                raise InvalidTransition(
                    f"lease fence for {fence_resource!r} is no longer owned"
                )
        assignments = ["updated=?", "payload_json=?", "private_json=?"]
        values: list[Any] = [
            time.time(),
            _merged_json(conn, job_id, "payload_json", payload_update),
            _merged_json(conn, job_id, "private_json", private_update),
        ]
        for key, value in fields.items():
            assignments.append(f"{key}=?")
            values.append(value)
        values.append(job_id)
        # Every interpolated identifier is selected from _UPDATABLE_JOB_COLUMNS.
        conn.execute(
            f"UPDATE jobs SET {','.join(assignments)} WHERE id=?",  # nosec B608
            values,
        )
        if event_type:
            _insert_event(
                conn,
                job_id,
                event_type,
                row["state"],
                row["state"],
                actor=actor,
                reason=reason,
            )
        conn.commit()
        updated = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_from_row(updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def transition_job(
    job_id: str,
    to_state: str,
    *,
    expected_states: Sequence[str] | None = None,
    expected_version: int | None = None,
    event_type: str = "state_changed",
    reason: str = "",
    actor: str = "benchtop",
    payload_update: dict[str, Any] | None = None,
    private_update: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if to_state not in ALLOWED_TRANSITIONS:
        raise StateError(f"unknown job state {to_state!r}")
    unknown = set(fields) - _UPDATABLE_JOB_COLUMNS
    if unknown:
        raise StateError(f"unsupported job fields: {sorted(unknown)}")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        from_state = str(row["state"])
        if expected_states is not None and from_state not in set(expected_states):
            raise InvalidTransition(
                f"job {job_id} is {from_state!r}, expected one of {list(expected_states)!r}"
            )
        if expected_version is not None and int(row["state_version"]) != int(
            expected_version
        ):
            raise InvalidTransition(
                f"job {job_id} state version changed from {expected_version} to {row['state_version']}"
            )
        if to_state != from_state and to_state not in ALLOWED_TRANSITIONS.get(
            from_state, frozenset()
        ):
            raise InvalidTransition(
                f"illegal job transition {from_state!r} -> {to_state!r}"
            )
        now = time.time()
        assignments = [
            "state=?",
            "state_version=state_version+?",
            "updated=?",
            "payload_json=?",
            "private_json=?",
        ]
        values: list[Any] = [
            to_state,
            1 if to_state != from_state else 0,
            now,
            _merged_json(conn, job_id, "payload_json", payload_update),
            _merged_json(conn, job_id, "private_json", private_update),
        ]
        if to_state in TERMINAL_STATES and "ended" not in fields:
            fields["ended"] = now
        if to_state == "cancel_requested" and "cancel_requested_at" not in fields:
            fields["cancel_requested_at"] = now
        for key, value in fields.items():
            assignments.append(f"{key}=?")
            values.append(value)
        values.append(job_id)
        # Every interpolated identifier is selected from _UPDATABLE_JOB_COLUMNS.
        conn.execute(
            f"UPDATE jobs SET {','.join(assignments)} WHERE id=?",  # nosec B608
            values,
        )
        _insert_event(
            conn,
            job_id,
            event_type,
            from_state,
            to_state,
            actor=actor,
            reason=reason,
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_from_row(updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def append_event(
    job_id: str,
    event_type: str,
    *,
    reason: str = "",
    actor: str = "benchtop",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        _insert_event(
            conn,
            job_id,
            event_type,
            row["state"],
            row["state"],
            actor=actor,
            reason=reason,
            payload=payload,
        )
        event = conn.execute(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY seq DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    return _event_from_row(event)


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "id": row["event_id"],
        "job_id": row["job_id"],
        "type": row["event_type"],
        "from_state": row["from_state"],
        "to_state": row["to_state"],
        "actor": row["actor"],
        "reason": row["reason"],
        "created": row["created"],
        "payload": _json_load(row["payload_json"], {}),
    }


def job_events(
    job_id: str, *, after: int = 0, limit: int = 500
) -> list[dict[str, Any]]:
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT * FROM job_events WHERE job_id=? AND seq>?
               ORDER BY seq ASC LIMIT ?""",
            (job_id, max(0, int(after)), max(0, min(int(limit), 2000))),
        ).fetchall()
    return [_event_from_row(row) for row in rows]


def acquire_job_lease(
    job_id: str,
    owner: str,
    *,
    ttl: float = 30.0,
    expected_states: Sequence[str] = ("queued",),
) -> dict[str, Any] | None:
    now = time.time()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["state"] not in set(expected_states):
            conn.rollback()
            return None
        if (
            row["lease_owner"]
            and row["lease_owner"] != owner
            and (row["lease_expires"] or 0) > now
        ):
            conn.rollback()
            return None
        expires = now + max(1.0, float(ttl))
        conn.execute(
            "UPDATE jobs SET lease_owner=?,lease_expires=?,updated=? WHERE id=?",
            (owner, expires, now, job_id),
        )
        _insert_event(
            conn,
            job_id,
            "lease_acquired",
            row["state"],
            row["state"],
            actor=owner,
            payload={"expires": expires},
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_from_row(updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_job_lease(job_id: str, owner: str) -> bool:
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT state,lease_owner FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row or row["lease_owner"] != owner:
            return False
        conn.execute(
            "UPDATE jobs SET lease_owner='',lease_expires=NULL,updated=? WHERE id=?",
            (time.time(), job_id),
        )
        _insert_event(
            conn,
            job_id,
            "lease_released",
            row["state"],
            row["state"],
            actor=owner,
        )
    return True


def renew_job_lease(
    job_id: str,
    owner: str,
    *,
    ttl: float = 15.0,
    expected_states: Sequence[str] = (
        "preparing",
        "running",
        "cancel_requested",
        "cancelling",
    ),
) -> bool:
    """Renew an existing ownership lease without producing heartbeat events."""
    now = time.time()
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT state,lease_owner FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if (
            not row
            or row["lease_owner"] != owner
            or row["state"] not in set(expected_states)
        ):
            return False
        conn.execute(
            "UPDATE jobs SET lease_expires=?,updated=? WHERE id=? AND lease_owner=?",
            (now + max(1.0, float(ttl)), now, job_id, owner),
        )
    return True


# Generic leases are used for one-at-a-time reconciliation and other resources
# that are not represented by a job row.
def acquire_lease(
    resource: str,
    owner: str,
    *,
    ttl: float = 30.0,
    payload: dict[str, Any] | None = None,
) -> str | None:
    now = time.time()
    token = uuid.uuid4().hex
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM leases WHERE resource=?", (resource,)
        ).fetchone()
        if row and row["owner"] != owner and row["expires"] > now:
            conn.rollback()
            return None
        if row and row["owner"] == owner:
            token = row["token"]
        conn.execute(
            """INSERT INTO leases(resource,owner,token,acquired,expires,updated,payload_json)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(resource) DO UPDATE SET
                 owner=excluded.owner,token=excluded.token,expires=excluded.expires,
                 updated=excluded.updated,payload_json=excluded.payload_json""",
            (
                resource,
                owner,
                token,
                row["acquired"] if row and row["owner"] == owner else now,
                now + max(1.0, float(ttl)),
                now,
                _json_dump(payload or {}),
            ),
        )
        conn.commit()
        return token
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_lease(resource: str, owner: str, token: str = "") -> bool:
    with closing(connect()) as conn, conn:
        sql = "DELETE FROM leases WHERE resource=? AND owner=?"
        args: list[Any] = [resource, owner]
        if token:
            sql += " AND token=?"
            args.append(token)
        cur = conn.execute(sql, args)
    return cur.rowcount > 0


def lease(resource: str) -> dict[str, Any] | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM leases WHERE resource=?", (resource,)
        ).fetchone()
    if not row:
        return None
    return {
        "resource": row["resource"],
        "owner": row["owner"],
        "token": row["token"],
        "acquired": row["acquired"],
        "expires": row["expires"],
        "updated": row["updated"],
        "payload": _json_load(row["payload_json"], {}),
    }
