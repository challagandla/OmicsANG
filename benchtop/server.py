# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""FastAPI app: REST + websocket terminals + static UI."""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import (
    __version__,
    config_form,
    fleet,
    fs_policy,
    git_ops,
    help_content,
    pipelines,
    provenance,
    qc,
    results,
    runplan,
    security,
    settings,
    store,
    study,
)
from .sessions import EOF, Session, SessionManager

app = FastAPI(
    title="OmicsANG",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(security.SecurityMiddleware, auth=security.AUTH_STATE)
mgr = SessionManager()
INSTANCE_ID = f"benchtop-{os.getpid()}-{uuid.uuid4().hex[:10]}"
LOCAL_SCHEDULER_LOCK = threading.Lock()
# Compatibility/debug projection; the Lock above is the actual concurrency guard.
LOCAL_SCHEDULER_ACTIVE = False
STARTUP_RECOVERY: dict = {"status": "not-run"}
CONTROLLER_TASKS: set[asyncio.Task] = set()
CONTROLLER_LEASE_TTL = 15.0
FINALIZATION_LEASE_TTL = 600.0
FINALIZATION_BATCH_LIMIT = 20
MAX_INTERACTIVE_SESSIONS = 32
MAX_WEBSOCKET_MESSAGE = 65_536

WEB_DIR = Path(str(resources.files("benchtop").joinpath("web")))

CODE_FILE_LIMIT = 1_000_000
CODE_LIST_LIMIT = 1200
CODE_SKIP_DIRS = {
    ".git",
    ".snakemake",
    ".benchtop",
    ".benchtop-worktrees",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".ipynb_checkpoints",
    "build",
    "dist",
    "logs",
    "benchmarks",
    "tmp",
    "results",
    "result",
    "output",
    "outputs",
    "analysis",
}
CODE_EXTS = {
    ".py",
    ".r",
    ".R",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".sql",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".scss",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".md",
    ".rst",
    ".txt",
    ".csv",
    ".tsv",
    ".smk",
    ".snakefile",
    ".rule",
    ".dockerfile",
    ".gitignore",
}
CODE_NAMES = {
    "Snakefile",
    "Dockerfile",
    "Makefile",
    "README",
    "LICENSE",
    "CITATION.cff",
    ".gitignore",
    ".dockerignore",
    "environment.yml",
    "environment.yaml",
    "requirements.txt",
    "pyproject.toml",
}
BROWSE_DEFAULT_LIMIT = 80
BROWSE_MAX_LIMIT = 200
BROWSE_MAX_DEPTH = 12
BROWSE_MAX_QUERY = 160
BROWSE_SCAN_LIMIT = 20_000
BROWSE_SCOPES = {
    "all",
    "files",
    "code",
    "config",
    "results",
    "directories",
}
BROWSE_OPEN_FILE_LIMIT = 32 * 1024 * 1024
BROWSE_CONFIG_DIRS = {"config", "configs", "envs", "profiles"}
BROWSE_CODE_DIRS = {
    "bin",
    "lib",
    "modules",
    "notebooks",
    "rules",
    "scripts",
    "src",
    "workflow",
    "workflows",
}
BROWSE_RESULT_DIRS = {
    "analysis",
    "figure",
    "figures",
    "output",
    "outputs",
    "qc",
    "report",
    "reports",
    "result",
    "results",
}
BROWSE_CONFIG_NAMES = {
    "environment.yaml",
    "environment.yml",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
}


# ---- helpers -------------------------------------------------------------
async def _await_blocking(function, /, *args, **kwargs):
    """Run bounded blocking work without relying on executor wakeup support."""
    result: dict = {}

    def worker() -> None:
        try:
            result["value"] = function(*args, **kwargs)
        except BaseException as exc:  # propagate the original HTTP/state error
            result["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0.05)
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _controller_identity_payload() -> dict:
    return {
        "pid": os.getpid(),
        "process_start": Session._process_start_token(os.getpid()),
        "boot_id": Session._boot_id(),
    }


def _controller_lease_resource(owner: str = INSTANCE_ID) -> str:
    return f"controller:{owner}"


def _renew_controller_heartbeat() -> bool:
    return bool(
        store.acquire_lease(
            _controller_lease_resource(),
            INSTANCE_ID,
            ttl=CONTROLLER_LEASE_TTL,
            payload=_controller_identity_payload(),
        )
    )


def _controller_owner_alive(owner: str) -> bool:
    if not owner:
        return False
    heartbeat = store.lease(_controller_lease_resource(owner)) or {}
    payload = heartbeat.get("payload") or {}
    try:
        return bool(
            payload.get("pid")
            and payload.get("process_start")
            and payload.get("boot_id") == Session._boot_id()
            and Session._process_start_token(int(payload["pid"]))
            == str(payload.get("process_start"))
        )
    except (TypeError, ValueError):
        return False


def _job_owned_by_live_controller(job: dict) -> bool:
    owner = str(job.get("lease_owner") or "")
    state = str(job.get("state") or "")
    lease_current = float(job.get("lease_expires") or 0) > time.time()
    active_pty_state = state in {
        "created",
        "preparing",
        "running",
        "cancel_requested",
        "cancelling",
    }
    return bool(
        owner and _controller_owner_alive(owner) and (lease_current or active_pty_state)
    )


def _renew_owned_session_leases() -> int:
    renewed = 0
    for session in mgr.list():
        job = store.get_job(session.id)
        if not job or str(job.get("lease_owner") or "") != INSTANCE_ID:
            continue
        if not _session_controls_job(session, job):
            continue
        if store.renew_job_lease(
            job["id"],
            INSTANCE_ID,
            ttl=CONTROLLER_LEASE_TTL,
            expected_states=(
                "preparing",
                "running",
                "cancel_requested",
                "cancelling",
            ),
        ):
            renewed += 1
    return renewed


def _require_pipeline(name: str) -> pipelines.Pipeline:
    p = pipelines.get(name)
    if not p:
        raise HTTPException(404, f"pipeline {name!r} not found")
    return p


def _safe_path(base: Path, rel: str) -> Path:
    """Resolve one browser-supplied path through the centralized policy."""
    try:
        _, target = fs_policy.resolve_relative(base, rel)
        return target
    except fs_policy.PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc


ACTIVE_CONTENT_SUFFIXES = {".html", ".htm", ".svg", ".xhtml", ".xml"}


def _authorized_file_response(target: Path) -> FileResponse:
    """Force active browser content to download outside the application origin."""
    suffix = target.suffix.casefold()
    if suffix in ACTIVE_CONTENT_SUFFIXES:
        return FileResponse(
            str(target),
            media_type="application/octet-stream",
            filename=target.name,
            content_disposition_type="attachment",
        )
    return FileResponse(str(target))


RUN_FORM_FLAGS = {
    "--config",
    "--configfile",
    "--configfiles",
    "--snakefile",
    "-s",
    "--profile",
    "--workflow-profile",
    "--directory",
    "-d",
    "--cores",
    "--jobs",
    "-c",
    "-j",
}


def _validate_run_extra(extra: str) -> None:
    try:
        tokens = shlex.split(extra or "")
    except ValueError as exc:
        raise HTTPException(400, f"invalid extra arguments: {exc}") from exc
    for token in tokens:
        flag = token.split("=", 1)[0]
        short_override = (
            token.startswith(("-s", "-d", "-c", "-j"))
            and not token.startswith("--")
            and len(token) > 2
        )
        if flag in RUN_FORM_FLAGS or short_override:
            raise HTTPException(
                400,
                f"{flag} must be set through the corresponding Run control, not Extra args",
            )


def _code_language(path: Path) -> str:
    name = path.name
    suffix = path.suffix.lower()
    if name == "Snakefile" or suffix in {".smk", ".snakefile"}:
        return "snakemake"
    if name in {"Dockerfile", ".dockerignore"}:
        return "docker"
    return {
        ".py": "python",
        ".r": "r",
        ".sh": "shell",
        ".bash": "shell",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".html": "html",
        ".css": "css",
        ".json": "json",
        ".jsonl": "jsonl",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".md": "markdown",
        ".rst": "rst",
        ".sql": "sql",
        ".csv": "csv",
        ".tsv": "tsv",
    }.get(suffix, "text")


def _is_code_candidate(path: Path) -> bool:
    return path.name in CODE_NAMES or path.suffix in CODE_EXTS


def _clean_code_relpath(raw: str) -> str:
    try:
        rel = fs_policy.clean_relative_path(raw)
    except fs_policy.PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if any(part in CODE_SKIP_DIRS for part in Path(rel).parts):
        raise HTTPException(400, "path is in a skipped workspace directory")
    return rel


def _safe_code_file(
    root: Path, raw: str, *, must_exist: bool = True
) -> tuple[str, Path]:
    rel = _clean_code_relpath(raw)
    try:
        _, target = fs_policy.resolve_relative(
            root,
            rel,
            must_exist=True if must_exist else None,
            expected="file" if must_exist else "any",
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "file not found") from exc
    except fs_policy.PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if target.exists() and not target.is_file():
        raise HTTPException(400, "target is not a file")
    if not _is_code_candidate(target):
        raise HTTPException(400, "path is not an editable code/config/doc file")
    return rel, target


def _backup_file(root: Path, target: Path, namespace: str) -> str | None:
    try:
        return fs_policy.private_backup(root, target, namespace=namespace)
    except fs_policy.PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc


def _trash_code_path(pipeline_name: str, rel: str) -> Path:
    stamp = f"{time.time_ns()}-{uuid.uuid4().hex}"
    safe_rel = rel.replace("/", "__")
    settings.ensure_dirs()
    trash_dir = settings.STATE_DIR
    for part in ("trash", pipeline_name):
        trash_dir = trash_dir / part
        try:
            info = trash_dir.lstat()
        except FileNotFoundError:
            trash_dir.mkdir(mode=0o700)
            info = trash_dir.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise HTTPException(400, "private trash path is not a regular directory")
        trash_dir.chmod(0o700)
    return trash_dir / f"{stamp}__{safe_rel}"


def _code_entry(root: Path, path: Path) -> dict:
    stat = path.stat()
    return {
        "path": path.relative_to(root).as_posix(),
        "name": path.name,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "language": _code_language(path),
    }


def _code_revision(content: str | bytes) -> str:
    """Return a content revision suitable for optimistic editor saves."""
    payload = content.encode("utf-8") if isinstance(content, str) else content
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _code_file_revision(path: Path) -> str | None:
    """Read one bounded ordinary file revision, or ``None`` when it is absent."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HTTPException(400, "file metadata is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise HTTPException(400, "write target is not an ordinary file")
    if info.st_size > CODE_FILE_LIMIT:
        raise HTTPException(413, f"file is larger than {CODE_FILE_LIMIT} bytes")
    try:
        return _code_revision(path.read_bytes())
    except OSError as exc:
        raise HTTPException(400, "file cannot be read") from exc


def _read_code_payload(pipeline: pipelines.Pipeline, raw_path: str) -> dict:
    _, target = _safe_code_file(pipeline.path, raw_path)
    try:
        file_stat = target.stat()
    except OSError as exc:
        raise HTTPException(400, str(exc)) from exc
    if file_stat.st_size > CODE_FILE_LIMIT:
        raise HTTPException(413, f"file is larger than {CODE_FILE_LIMIT} bytes")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(415, "file is not UTF-8 text") from exc
    except OSError as exc:
        raise HTTPException(400, "file cannot be read") from exc
    return {
        **_code_entry(pipeline.path.resolve(), target),
        "content": content,
        "revision": _code_revision(content),
    }


def _safe_browse_directory(root: Path, raw: str) -> tuple[str, Path, Path]:
    """Resolve the browse root and an optional repository-relative directory."""
    try:
        resolved_root = fs_policy.resolve_root(root)
        if not raw:
            return "", resolved_root, resolved_root
        rel, target = fs_policy.resolve_relative(
            resolved_root,
            raw,
            must_exist=True,
            expected="directory",
        )
        return rel, resolved_root, target
    except FileNotFoundError as exc:
        raise HTTPException(404, "directory not found") from exc
    except fs_policy.PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc


def _safe_browse_file(root: Path, raw: str) -> tuple[str, Path]:
    try:
        return fs_policy.resolve_relative(
            root,
            raw,
            must_exist=True,
            expected="file",
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "file not found") from exc
    except fs_policy.PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc


def _clean_browse_query(raw: str) -> str:
    query = str(raw or "").strip()
    if len(query.encode("utf-8", errors="ignore")) > BROWSE_MAX_QUERY:
        raise HTTPException(400, "search query is too long")
    if (
        fs_policy.CONTROL_RE.search(query)
        or "\\" in query
        or query.startswith(("/", "~"))
        or any(part == ".." for part in query.split("/"))
    ):
        raise HTTPException(400, "search query contains forbidden path syntax")
    return query


def _browse_breadcrumbs(pipeline_name: str, rel: str) -> list[dict[str, str]]:
    crumbs = [{"name": pipeline_name, "path": ""}]
    current: list[str] = []
    for part in Path(rel).parts if rel else ():
        current.append(part)
        crumbs.append({"name": part, "path": "/".join(current)})
    return crumbs


def _browse_directory_entries(directory: Path) -> list[tuple[str, os.stat_result]]:
    """Read one directory through a no-follow descriptor and return safe metadata."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return []
    found: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    continue
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    continue
                found.append((entry.name, info))
    except OSError:
        return []
    finally:
        os.close(descriptor)
    found.sort(
        key=lambda item: (
            0 if stat.S_ISDIR(item[1].st_mode) else 1,
            item[0].casefold(),
        )
    )
    return found


def _browse_category(path: Path, *, is_directory: bool) -> str:
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    if parts & BROWSE_RESULT_DIRS or any(
        hint in name
        for hint in ("multiqc_report", "flagstat", "idxstats", "summary_report")
    ):
        return "results"
    if parts & BROWSE_CONFIG_DIRS:
        return "config"
    if is_directory:
        return "code" if name in BROWSE_CODE_DIRS else "directory"
    if name in BROWSE_CONFIG_NAMES or path.suffix.casefold() in {
        ".cfg",
        ".conf",
        ".ini",
        ".toml",
        ".yaml",
        ".yml",
    }:
        return "config"
    if _is_code_candidate(path):
        return "code"
    return "file"


def _browse_scope_matches(scope: str, *, is_directory: bool, category: str) -> bool:
    if scope == "all":
        return True
    if scope == "files":
        return not is_directory
    if scope == "directories":
        return is_directory
    return category == scope


def _browse_open_action(
    path: Path,
    info: os.stat_result,
    *,
    category: str,
) -> str | None:
    if stat.S_ISDIR(info.st_mode):
        return "browse"
    if info.st_size > BROWSE_OPEN_FILE_LIMIT:
        return None
    if category == "results":
        return "results"
    if category in {"code", "config"} and info.st_size <= CODE_FILE_LIMIT:
        return "code"
    return "download"


def _search_pipeline_entries(
    pipeline_name: str,
    root: Path,
    base: Path,
    *,
    query: str,
    scope: str,
    limit: int,
) -> dict:
    needle_tokens = query.casefold().split()
    queue: deque[tuple[Path, int]] = deque([(base, 0)])
    matches: list[dict] = []
    scanned = 0
    scan_limited = False
    depth_limited = False
    while queue and len(matches) <= limit:
        current, current_depth = queue.popleft()
        try:
            if current == root:
                current = fs_policy.resolve_root(root)
            else:
                current_rel = current.relative_to(root).as_posix()
                _, current = fs_policy.resolve_relative(
                    root,
                    current_rel,
                    must_exist=True,
                    expected="directory",
                )
        except (ValueError, FileNotFoundError, fs_policy.PathPolicyError):
            continue
        for name, info in _browse_directory_entries(current):
            scanned += 1
            if scanned > BROWSE_SCAN_LIMIT:
                scan_limited = True
                break
            path = current / name
            try:
                rel = path.relative_to(root).as_posix()
                fs_policy.clean_relative_path(rel)
            except (ValueError, fs_policy.PathPolicyError):
                continue
            is_directory = stat.S_ISDIR(info.st_mode)
            category = _browse_category(Path(rel), is_directory=is_directory)
            child_depth = current_depth + 1
            if query and is_directory:
                if child_depth < BROWSE_MAX_DEPTH:
                    queue.append((path, child_depth))
                else:
                    depth_limited = True
            local_path = path.relative_to(base).as_posix()
            matches_query = not needle_tokens or all(
                token in local_path.casefold() for token in needle_tokens
            )
            matches_scope = _browse_scope_matches(
                scope,
                is_directory=is_directory,
                category=category,
            )
            if not matches_query or not matches_scope:
                continue
            parent = Path(rel).parent.as_posix()
            matches.append(
                {
                    "kind": "directory" if is_directory else category,
                    "category": category,
                    "path": rel,
                    "name": name,
                    "parent": "" if parent == "." else parent,
                    "size": None if is_directory else info.st_size,
                    "open_action": _browse_open_action(
                        Path(rel),
                        info,
                        category=category,
                    ),
                }
            )
            if len(matches) > limit:
                break
        if scan_limited or len(matches) > limit:
            break

    matches.sort(
        key=lambda item: (
            0 if item["kind"] == "directory" else 1,
            item["path"].count("/"),
            item["path"].casefold(),
        )
    )
    result_limited = len(matches) > limit
    reasons = []
    if result_limited:
        reasons.append("result limit")
    if scan_limited:
        reasons.append("scan cap")
    if depth_limited:
        reasons.append("depth cap")
    truncated = bool(reasons)
    matches = matches[:limit]
    return {
        "results": matches,
        "count": len(matches),
        "limit": limit,
        "scanned": min(scanned, BROWSE_SCAN_LIMIT),
        "max_depth": BROWSE_MAX_DEPTH if query else 1,
        "truncated": truncated,
        "truncated_reason": "; ".join(reasons),
    }


def _diagnostic(
    level: str,
    path: str,
    line: int,
    title: str,
    message: str,
    code: str = "",
    fixes: list[dict] | None = None,
    extra: dict | None = None,
) -> dict:
    item = {
        "level": level,
        "path": path,
        "line": line,
        "title": title,
        "message": message,
        "code": code,
    }
    if fixes:
        item["fixes"] = fixes
    if extra:
        item.update(extra)
    return item


PATH_KEY_RE = re.compile(
    r"(path|file|dir|ref|genome|gtf|gff|fasta|fa|bed|bam|fastq|fq|csv|tsv|"
    r"yaml|yml|json|manifest|index|sample|samples|result|output|log|report)",
    re.I,
)
PATH_VALUE_RE = re.compile(
    r"(^\.{0,2}/|^~/|/|\.(?:fa|fasta|fq|fastq|bam|sam|bed|gtf|gff|csv|tsv|"
    r"txt|yaml|yml|json|idx|bt2|mmi|html|pdf)$)",
    re.I,
)
REMOTE_PATH_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
DIRECTIVE_FIXES = {
    "thread": "threads",
    "resource": "resources",
    "logs": "log",
    "benchmarks": "benchmark",
}
DIRECTIVE_HINTS = {
    bad: f"Snakemake uses '{good}:', not '{bad}:'."
    for bad, good in DIRECTIVE_FIXES.items()
}
SHELL_RISK_PATTERNS = [
    (
        re.compile(r"\brm\s+-[A-Za-z]*r[A-Za-z]*f\b"),
        "rm -rf command",
        "Review broad recursive deletion before running this workflow.",
    ),
    (
        re.compile(r"\b(?:curl|wget)\b.+\|\s*(?:bash|sh)\b"),
        "download piped to shell",
        "Install scripts piped directly to a shell are hard to audit and reproduce.",
    ),
    (
        re.compile(r"\bsudo\b"),
        "sudo in pipeline command",
        "Pipeline steps should usually run without elevated privileges.",
    ),
    (
        re.compile(r"\bchmod\s+777\b"),
        "world-writable chmod",
        "chmod 777 can hide permission problems and weaken local isolation.",
    ),
    (
        re.compile(r"\b(?:pkill|killall)\b"),
        "process-kill command",
        "Process-kill commands can terminate unrelated jobs on a shared workstation.",
    ),
]

SRA_RUN_RE = re.compile(r"\b[SED]RR\d+\b", re.I)
SRA_ACC_RE = re.compile(r"\b(?:[SED]RR|[SED]RX|[SED]RS|[SED]RP)\d+\b", re.I)
GEO_ACC_RE = re.compile(r"\b(?:GSE|GSM|GPL|GDS)\d+\b", re.I)
NCBI_DB = {"sra": "sra", "geo": "gds"}
NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DOWNLOAD_SKIP_PARTS = {
    ".git",
    ".snakemake",
    ".benchtop",
    ".benchtop-worktrees",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    "__pycache__",
}


def _iter_code_files(root: Path, limit: int = 500):
    seen = 0
    for path in root.rglob("*"):
        if seen >= limit:
            break
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in CODE_SKIP_DIRS for part in rel_parts[:-1]):
            continue
        try:
            if not path.is_file() or not _is_code_candidate(path):
                continue
            if path.stat().st_size > CODE_FILE_LIMIT:
                continue
        except OSError:
            continue
        seen += 1
        yield path


def _read_utf8(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _looks_checkable_path(key: str, value: str) -> bool:
    value = value.strip()
    if not value or REMOTE_PATH_RE.search(value):
        return False
    if any(token in value for token in ("{", "}", "$", "*", "?", "\n")):
        return False
    return bool(PATH_KEY_RE.search(key) or PATH_VALUE_RE.search(value))


def _path_exists_for_config(root: Path, config_path: Path, raw_value: str) -> bool:
    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        return candidate.exists()
    return (root / candidate).exists() or (config_path.parent / candidate).exists()


def _line_for_text(text: str, needle: str) -> int:
    if not needle:
        return 1
    idx = text.find(needle)
    if idx < 0:
        return 1
    return text.count("\n", 0, idx) + 1


def _safe_missing_rel(root: Path, base: Path, raw_value: str) -> str | None:
    value = raw_value.strip()
    if not value or REMOTE_PATH_RE.search(value):
        return None
    if any(token in value for token in ("{", "}", "$", "*", "?", "\n")):
        return None
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return None
    target = (base / candidate).resolve()
    root = root.resolve()
    if target != root and root not in target.parents:
        return None
    return target.relative_to(root).as_posix()


def _looks_likely_directory(key: str, value: str) -> bool:
    if value.endswith("/"):
        return True
    if Path(value).suffix:
        return False
    return bool(
        re.search(
            r"(^|[._-])(dir|folder|outdir|output_dir|outputs_dir|tmpdir|workdir|logdir|results?|results_dir)$",
            key,
            re.I,
        )
    )


def _missing_path_fixes(root: Path, base: Path, key: str, value: str) -> list[dict]:
    rel = _safe_missing_rel(root, base, value)
    if not rel:
        return []
    target = root / rel
    if _looks_likely_directory(key, value):
        return [
            {
                "label": f"Create folder {rel}",
                "action": "create-path",
                "target": rel,
                "kind": "dir",
            }
        ]
    if _is_code_candidate(target):
        return [
            {
                "label": f"Create file {rel}",
                "action": "create-path",
                "target": rel,
                "kind": "file",
            }
        ]
    return []


def _sra_geo_tools() -> dict:
    return {
        "prefetch": shutil.which("prefetch"),
        "fasterq_dump": shutil.which("fasterq-dump"),
        "pigz": shutil.which("pigz"),
        "gzip": shutil.which("gzip"),
    }


def _extract_accessions(text: str, *, runs_only: bool = False) -> list[str]:
    regex = SRA_RUN_RE if runs_only else SRA_ACC_RE
    seen: set[str] = set()
    out: list[str] = []
    for match in regex.finditer(text or ""):
        acc = match.group(0).upper()
        if acc not in seen:
            seen.add(acc)
            out.append(acc)
    return out


def _extract_geo_accessions(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in GEO_ACC_RE.finditer(text or ""):
        acc = match.group(0).upper()
        if acc not in seen:
            seen.add(acc)
            out.append(acc)
    return out


def _ncbi_json(endpoint: str, params: dict, timeout: int = 20) -> dict:
    if endpoint not in {"esearch.fcgi", "esummary.fcgi"}:
        raise HTTPException(500, "unsupported NCBI endpoint")
    query = urllib.parse.urlencode(
        {
            **params,
            "retmode": "json",
            "tool": "omicsang",
        }
    )
    url = f"{NCBI_BASE}/{endpoint}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "OmicsANG/0.1"})
    try:
        # The scheme and host come from the fixed HTTPS NCBI_BASE above.
        with urllib.request.urlopen(req, timeout=timeout) as res:  # nosec B310
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HTTPException(exc.code, f"NCBI request failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(502, f"NCBI request failed: {exc.reason}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(502, "NCBI returned an unreadable response") from exc


def _summarize_ncbi_item(db: str, uid: str, item: dict) -> dict:
    blob = json.dumps(item, sort_keys=True)
    runs = _extract_accessions(blob, runs_only=True)
    geo_accs = _extract_geo_accessions(blob)
    accession = (
        item.get("accession")
        or item.get("acc")
        or item.get("gds")
        or item.get("caption")
        or (runs[0] if runs else "")
        or (geo_accs[0] if geo_accs else "")
        or uid
    )
    return {
        "uid": uid,
        "db": db,
        "accession": str(accession),
        "title": item.get("title") or item.get("expname") or item.get("summary") or "",
        "organism": item.get("organism") or item.get("taxon") or "",
        "study": item.get("study") or item.get("bioproject") or "",
        "date": item.get("createdate")
        or item.get("updatedate")
        or item.get("pdat")
        or "",
        "runs": runs,
        "geo_accessions": geo_accs,
        "url": (
            f"https://www.ncbi.nlm.nih.gov/sra/{accession}"
            if db == "sra"
            else f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}"
        ),
    }


def _ncbi_search(db: str, term: str, limit: int) -> dict:
    db = NCBI_DB.get(db)
    if not db:
        raise HTTPException(400, "db must be 'sra' or 'geo'")
    term = (term or "").strip()
    if not term:
        raise HTTPException(400, "search term is required")
    retmax = max(1, min(int(limit or 20), 100))
    search = _ncbi_json(
        "esearch.fcgi",
        {
            "db": db,
            "term": term,
            "retmax": retmax,
            "sort": "relevance",
        },
    )
    sr = search.get("esearchresult") or {}
    ids = [str(i) for i in sr.get("idlist") or []]
    items: list[dict] = []
    if ids:
        summary = _ncbi_json(
            "esummary.fcgi",
            {
                "db": db,
                "id": ",".join(ids),
            },
        )
        result = summary.get("result") or {}
        for uid in result.get("uids") or ids:
            item = result.get(str(uid))
            if isinstance(item, dict):
                items.append(_summarize_ncbi_item(db, str(uid), item))
    return {
        "db": db,
        "term": term,
        "count": int(sr.get("count") or 0),
        "retmax": retmax,
        "items": items,
    }


def _download_path_options(p: pipelines.Pipeline) -> list[str]:
    preferred = [
        "raw_files/sra",
        "raw_files",
        "data/sra",
        "data",
        "resources/sra",
        "resources",
        "downloads/sra",
        "downloads",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for rel in preferred:
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    try:
        for path in p.path.rglob("*"):
            if len(out) >= 80:
                break
            try:
                rel = path.relative_to(p.path).as_posix()
            except ValueError:
                continue
            if not path.is_dir() or any(
                part in DOWNLOAD_SKIP_PARTS for part in Path(rel).parts
            ):
                continue
            first = Path(rel).parts[0] if Path(rel).parts else ""
            if (
                first
                in {"raw_files", "data", "resources", "downloads", "input", "inputs"}
                and rel not in seen
            ):
                seen.add(rel)
                out.append(rel)
    except OSError:
        pass
    return out


def _clean_download_dir(p: pipelines.Pipeline, raw: str) -> Path:
    value = (raw or "").strip() or "raw_files/sra"
    try:
        rel, target = fs_policy.resolve_relative(
            p.path,
            value,
        )
    except fs_policy.PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if target == p.path.resolve():
        raise HTTPException(400, "choose a specific download subdirectory")
    if any(part in DOWNLOAD_SKIP_PARTS for part in Path(rel).parts):
        raise HTTPException(
            400,
            "destination path points inside a generated or hidden workspace directory",
        )
    if target.exists() and not target.is_dir():
        raise HTTPException(400, "destination path exists and is not a directory")
    return target


def _sra_download_plan(p: pipelines.Pipeline, body: "SraDownloadRequest") -> dict:
    text = "\n".join([body.accessions or "", body.accession_text or ""])
    accessions = body.accessions_list or _extract_accessions(text)
    clean: list[str] = []
    for acc in accessions:
        acc = str(acc).strip().upper()
        if not SRA_ACC_RE.fullmatch(acc):
            raise HTTPException(400, f"invalid SRA accession: {acc!r}")
        if acc not in clean:
            clean.append(acc)
    if not clean:
        raise HTTPException(400, "provide at least one SRA accession")
    if len(clean) > 500:
        raise HTTPException(400, "download batches are limited to 500 accessions")
    dest = _clean_download_dir(p, body.destination)
    threads = max(1, min(int(body.threads or 6), 64))
    tools = _sra_geo_tools()
    warnings: list[str] = []
    if body.use_prefetch and not tools["prefetch"]:
        warnings.append(
            "prefetch is not installed; download cannot start until SRA Toolkit is available"
        )
    if body.convert_fastq and not tools["fasterq_dump"]:
        warnings.append(
            "fasterq-dump is not installed; FASTQ conversion cannot start until SRA Toolkit is available"
        )
    non_run = [a for a in clean if not SRA_RUN_RE.fullmatch(a)]
    if non_run:
        warnings.append(
            "non-run accessions may not convert directly with fasterq-dump: "
            + ", ".join(non_run[:8])
        )
    return {
        "accessions": clean,
        "destination": str(dest),
        "prefetch_dir": str(dest / "prefetch"),
        "fastq_dir": str(dest / "fastq"),
        "tmp_dir": str(dest / ".tmp"),
        "threads": threads,
        "use_prefetch": bool(body.use_prefetch),
        "convert_fastq": bool(body.convert_fastq),
        "gzip_fastq": bool(body.gzip_fastq),
        "split_files": bool(body.split_files),
        "skip_technical": bool(body.skip_technical),
        "tools": {k: bool(v) for k, v in tools.items()},
        "tool_paths": tools,
        "warnings": warnings,
    }


def _write_sra_download_job(plan: dict) -> tuple[Path, Path]:
    job_dir = settings.STATE_DIR / "sra_geo" / uuid.uuid4().hex[:10]
    job_dir.mkdir(parents=True, exist_ok=True)
    config = job_dir / "download.json"
    script = job_dir / "download.py"
    config.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    script.write_text(
        r"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def quote(argv):
    return " ".join(subprocess.list2cmdline([str(x)]) for x in argv)


def run(argv):
    print("$ " + quote(argv), flush=True)
    proc = subprocess.run([str(x) for x in argv])
    if proc.returncode:
        raise SystemExit(proc.returncode)


def compress_with_python(path: Path) -> None:
    gz_path = path.with_suffix(path.suffix + ".gz")
    print(f"python gzip {path.name} -> {gz_path.name}", flush=True)
    with path.open("rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


cfg = json.loads(Path(sys.argv[1]).read_text())
dest = Path(cfg["destination"])
prefetch_dir = Path(cfg["prefetch_dir"])
fastq_dir = Path(cfg["fastq_dir"])
tmp_dir = Path(cfg["tmp_dir"])
for path in (dest, prefetch_dir, fastq_dir, tmp_dir):
    path.mkdir(parents=True, exist_ok=True)

prefetch = cfg["tool_paths"].get("prefetch")
fasterq = cfg["tool_paths"].get("fasterq_dump")
compressor = cfg["tool_paths"].get("pigz") or cfg["tool_paths"].get("gzip")

print(f"OmicsANG SRA download: {len(cfg['accessions'])} accession(s)", flush=True)
print(f"Destination: {dest}", flush=True)

for acc in cfg["accessions"]:
    print(f"\n== {acc} ==", flush=True)
    source = acc
    if cfg["use_prefetch"]:
        if not prefetch:
            raise SystemExit("prefetch is required but was not found on PATH")
        run([prefetch, "--output-directory", prefetch_dir, acc])
        local = prefetch_dir / acc
        if local.exists():
            source = str(local)
    if cfg["convert_fastq"]:
        if not fasterq:
            raise SystemExit("fasterq-dump is required but was not found on PATH")
        cmd = [
            fasterq, source,
            "--outdir", fastq_dir,
            "--temp", tmp_dir,
            "--threads", str(cfg["threads"]),
        ]
        if cfg["split_files"]:
            cmd.append("--split-files")
        if cfg["skip_technical"]:
            cmd.append("--skip-technical")
        run(cmd)
        if cfg["gzip_fastq"]:
            fastqs = sorted(fastq_dir.glob(f"{acc}*.fastq"))
            if not fastqs:
                print(f"warning: no FASTQ files matched {acc}*.fastq for compression", flush=True)
            for fq in fastqs:
                if compressor:
                    run([compressor, "-f", str(fq)])
                else:
                    compress_with_python(fq)

print("\nOmicsANG SRA download complete.", flush=True)
""".lstrip(),
        encoding="utf-8",
    )
    return script, config


def _scan_yaml_paths(root: Path, path: Path, rel: str, items: list[dict]) -> None:
    text = _read_utf8(path)
    if text is None:
        return
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        items.append(
            _diagnostic(
                "error",
                rel,
                1,
                "YAML parse error",
                f"Could not parse this config: {exc}",
                "yaml-parse",
            )
        )
        return

    def walk(obj, key_path: str) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                next_key = f"{key_path}.{key}" if key_path else str(key)
                walk(value, next_key)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, key_path)
        elif isinstance(obj, str) and _looks_checkable_path(key_path, obj):
            if not _path_exists_for_config(root, path, obj):
                line = _line_for_text(text, obj)
                items.append(
                    _diagnostic(
                        "warning",
                        rel,
                        line,
                        "Missing referenced path",
                        f"{key_path or 'value'} points to '{obj}', which was not found "
                        "relative to the pipeline root or this config file.",
                        "missing-config-path",
                        _missing_path_fixes(root, root, key_path, obj),
                    )
                )

    walk(data, "")


def _scan_text_diagnostics(root: Path, path: Path, rel: str, items: list[dict]) -> None:
    text = _read_utf8(path)
    if text is None:
        return
    lang = _code_language(path)
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if lang in {"snakemake", "python"}:
            m = re.match(r"^(thread|resource|logs|benchmarks)\s*:", stripped)
            if m:
                bad = m.group(1)
                good = DIRECTIVE_FIXES[bad]
                items.append(
                    _diagnostic(
                        "error",
                        rel,
                        i,
                        "Suspicious Snakemake directive",
                        DIRECTIVE_HINTS[bad],
                        "snakemake-directive",
                        [
                            {
                                "label": f"Replace with {good}:",
                                "action": "replace-directive",
                                "path": rel,
                                "line": i,
                                "from_value": bad,
                                "to_value": good,
                            }
                        ],
                    )
                )
            conda = re.search(r"\bconda\s*:\s*[\"']([^\"']+\.ya?ml)[\"']", line)
            if conda:
                ref = conda.group(1)
                if not (root / ref).exists() and not (path.parent / ref).exists():
                    items.append(
                        _diagnostic(
                            "error",
                            rel,
                            i,
                            "Missing conda environment file",
                            f"conda: '{ref}' does not exist relative to the pipeline root "
                            "or this workflow file.",
                            "missing-conda-env",
                            _missing_path_fixes(root, path.parent, "conda", ref),
                        )
                    )
            include = re.search(r"\binclude\s*:\s*[\"']([^\"']+)[\"']", line)
            if include:
                ref = include.group(1)
                if not (root / ref).exists() and not (path.parent / ref).exists():
                    items.append(
                        _diagnostic(
                            "error",
                            rel,
                            i,
                            "Missing included workflow",
                            f"include: '{ref}' does not exist relative to the pipeline root "
                            "or this workflow file.",
                            "missing-include",
                            _missing_path_fixes(root, path.parent, "include", ref),
                        )
                    )
            if re.match(r"^(output|log|benchmark)\s*:", stripped) and re.search(
                r"[\"']/(?:home|media|mnt|tmp|var)/", line
            ):
                items.append(
                    _diagnostic(
                        "warning",
                        rel,
                        i,
                        "Absolute workflow output path",
                        "Outputs anchored to host-specific absolute paths reduce portability.",
                        "absolute-output",
                    )
                )
        if lang in {"snakemake", "shell", "python", "r"}:
            for pattern, title, message in SHELL_RISK_PATTERNS:
                if pattern.search(line):
                    items.append(
                        _diagnostic("warning", rel, i, title, message, "shell-risk")
                    )


def _scan_conda_env_yaml(path: Path, rel: str, items: list[dict]) -> None:
    if path.name not in {"environment.yml", "environment.yaml"} and not rel.startswith(
        "envs/"
    ):
        return
    text = _read_utf8(path)
    if text is None:
        return
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        items.append(
            _diagnostic(
                "error",
                rel,
                1,
                "Conda env YAML parse error",
                f"Could not parse this environment file: {exc}",
                "conda-yaml-parse",
            )
        )
        return
    if not isinstance(data, dict):
        items.append(
            _diagnostic(
                "warning",
                rel,
                1,
                "Unexpected conda env structure",
                "Conda environment files are usually mappings with name/channels/dependencies.",
                "conda-env-structure",
            )
        )
        return
    deps = data.get("dependencies")
    if not isinstance(deps, list) or not deps:
        items.append(
            _diagnostic(
                "warning",
                rel,
                1,
                "Conda env has no dependencies",
                "This environment file has no non-empty dependencies list.",
                "conda-env-dependencies",
            )
        )
    if path.name in {"environment.yml", "environment.yaml"} and not data.get("name"):
        items.append(
            _diagnostic(
                "warning",
                rel,
                1,
                "Conda env has no name",
                "A root environment file without a name is harder for OmicsANG to resolve.",
                "conda-env-name",
            )
        )


FASTQ_COLUMN_RE = re.compile(r"(^|[_-])(fastq|fq|read1|read2|r1|r2)([_-]|$)", re.I)


def _scan_sample_sheet_fastqs(
    root: Path, path: Path, rel: str, items: list[dict]
) -> None:
    if path.suffix.lower() not in {".csv", ".tsv"}:
        return
    text = _read_utf8(path)
    if text is None or not text.strip():
        return
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    except csv.Error:
        return
    if not reader.fieldnames:
        return
    fastq_cols = [c for c in reader.fieldnames if c and FASTQ_COLUMN_RE.search(c)]
    if not fastq_cols:
        return
    for row_idx, row in enumerate(reader, 2):
        for col in fastq_cols:
            value = (row.get(col) or "").strip()
            if not value:
                continue
            for part in re.split(r"[;,]", value):
                candidate = part.strip()
                if not _looks_checkable_path(col, candidate) and not re.search(
                    r"\.(?:f(?:ast)?q)(?:\.gz)?$", candidate, re.I
                ):
                    continue
                if _path_exists_for_config(root, path, candidate):
                    continue
                items.append(
                    _diagnostic(
                        "warning",
                        rel,
                        row_idx,
                        "Missing FASTQ in sample sheet",
                        f"{col} points to '{candidate}', which was not found relative "
                        "to the pipeline root or this sample sheet.",
                        "missing-fastq",
                    )
                )


RULE_START_RE = re.compile(r"^(\s*)(rule|checkpoint)\s+([A-Za-z_]\w*)\s*:")
RULE_DIRECTIVE_RE = re.compile(
    r"^\s*(input|output|params|threads|resources|log|benchmark|conda|container|script|shell|run|wrapper|notebook)\s*:"
)


def _scan_snakemake_structure(
    root: Path,
    path: Path,
    rel: str,
    items: list[dict],
    rule_seen: dict[str, tuple[str, int]],
    max_cores: int,
) -> None:
    text = _read_utf8(path)
    if text is None:
        return
    lines = text.splitlines()
    current: dict | None = None
    rules: list[dict] = []
    for i, line in enumerate(lines, 1):
        m = RULE_START_RE.match(line)
        if m:
            current = {
                "name": m.group(3),
                "line": i,
                "directives": set(),
                "threads": None,
            }
            rules.append(current)
            if current["name"] in rule_seen:
                prev_path, prev_line = rule_seen[current["name"]]
                items.append(
                    _diagnostic(
                        "warning",
                        rel,
                        i,
                        "Duplicate Snakemake rule name",
                        f"Rule '{current['name']}' was already seen in {prev_path}:{prev_line}.",
                        "duplicate-rule",
                        extra={"dry_run_target": current["name"]},
                    )
                )
            else:
                rule_seen[current["name"]] = (rel, i)
            continue
        if not current:
            continue
        d = RULE_DIRECTIVE_RE.match(line)
        if not d:
            continue
        directive = d.group(1)
        current["directives"].add(directive)
        if directive == "threads":
            raw = line.split(":", 1)[1].strip()
            m_threads = re.match(r"(\d+)\b", raw)
            if m_threads:
                threads = int(m_threads.group(1))
                current["threads"] = threads
                if threads > max_cores:
                    items.append(
                        _diagnostic(
                            "warning",
                            rel,
                            i,
                            "Rule exceeds local core budget",
                            f"Rule '{current['name']}' requests {threads} threads; "
                            f"the current local budget is {max_cores}.",
                            "rule-threads-over-budget",
                            extra={"dry_run_target": current["name"]},
                        )
                    )

    executable = {"shell", "run", "script", "wrapper", "notebook"}
    for rule in rules:
        if rule["name"] == "all":
            continue
        if rule["directives"] and not (rule["directives"] & executable):
            items.append(
                _diagnostic(
                    "info",
                    rel,
                    rule["line"],
                    "Rule has no executable directive",
                    f"Rule '{rule['name']}' has directives but no shell/run/script/wrapper/notebook block.",
                    "rule-no-executable",
                    extra={"dry_run_target": rule["name"]},
                )
            )


def _record_on_exit(pipeline_name: str, meta: dict):
    def cb(s: Session):
        terminal_job = _persist_session_terminal(s, reason="process exited")
        if not terminal_job or terminal_job.get("state") not in store.TERMINAL_STATES:
            return
        try:
            store.record(
                {
                    "id": s.id,
                    "kind": s.kind,
                    "pipeline": pipeline_name,
                    "title": s.title,
                    "status": s.status,
                    "exit_code": s.exit_code,
                    "command": runplan.redact_command(s.argv),
                    "started": s.started,
                    "ended": s.ended,
                    "logfile": s.logfile,
                    **meta,
                }
            )
        except Exception as exc:
            try:
                store.append_event(
                    s.id,
                    "history_persist_failed",
                    reason=str(exc),
                    actor=INSTANCE_ID,
                )
            except Exception:
                pass

    return cb


def _session_job_payload(s: Session) -> dict:
    snapshot = s.to_dict()
    snapshot.setdefault("meta", {})["durable_job_id"] = s.id
    return {
        "title": s.title,
        "cores": _session_cores(s),
        "dryrun": bool(s.meta.get("dryrun")),
        "executor_state": s.status,
        "command": runplan.redact_command(s.argv),
        "logfile": s.logfile or "",
        "capsule_id": str(s.meta.get("capsule_id") or ""),
        "process_boot_id": s.boot_id,
        "process_pgid": s.pgid,
        "session": snapshot,
    }


def _session_private_payload(s: Session) -> dict:
    execution = {
        "kind": s.kind,
        "title": s.title,
        "cwd": s.cwd,
        "argv": list(s.argv),
        "env": dict(s.env),
        "cols": s.cols,
        "rows": s.rows,
        "meta": dict(s.meta),
        "logfile": s.logfile or "",
        "created": s.created,
    }
    plan = getattr(s, "_run_plan", None)
    payload = getattr(plan, "payload", None)
    plan_pipeline = payload.get("pipeline") if isinstance(payload, dict) else None
    if isinstance(plan_pipeline, dict):
        execution["run_plan_pipeline"] = {
            key: str(plan_pipeline.get(key) or "")
            for key in ("name", "local_name", "root")
        }
    return {"execution": execution}


PUBLIC_JOB_INTERNAL_KEYS = {
    "_private",
    "cwd",
    "idempotency_key",
    "lease_owner",
    "lease_expires",
    "logfile",
    "log_path",
    "pid",
    "process_boot_id",
    "process_pgid",
    "process_start",
    "request_digest",
    "cancel_dispatch_owner",
    "controller_owner",
    "plan_record",
    "script",
    "state_dir",
    "trash_path",
    "trashed_to",
}


def _without_internal_paths(value):
    """Recursively omit known private control-plane path/identity fields."""
    if isinstance(value, dict):
        return {
            str(key): _without_internal_paths(child)
            for key, child in value.items()
            if str(key) not in PUBLIC_JOB_INTERNAL_KEYS
        }
    if isinstance(value, list):
        return [_without_internal_paths(child) for child in value]
    return value


def _public_text(value: object) -> str:
    """Redact secrets and configured private-state prefixes from API text."""
    text = provenance.redact_text(str(value or ""))
    private_roots = sorted(
        {str(settings.STATE_DIR), str(settings.RUN_LOG_DIR)}, key=len, reverse=True
    )
    for root in private_roots:
        if root:
            text = text.replace(root, "<private-state>")
    return text


def _public_session_snapshot(snapshot: dict | None) -> dict:
    """Sanitize a current or legacy durable session projection."""
    public = _without_internal_paths(json.loads(json.dumps(snapshot or {})))
    meta = public.get("meta")
    if isinstance(meta, dict):
        for key in PUBLIC_JOB_INTERNAL_KEYS:
            meta.pop(key, None)
        public["meta"] = runplan.redact_mapping(meta)
    argv = public.get("argv")
    if isinstance(argv, list):
        public["argv"] = runplan.redact_argv([str(value) for value in argv])
        public["command"] = runplan.redact_command([str(value) for value in argv])
    elif public.get("command"):
        public["command"] = runplan.redact_command_text(str(public["command"]))
    return public


def _public_slurm_job(job: dict | None) -> dict | None:
    """Expose scheduler identity/status without private control-plane paths."""
    if job is None:
        return None
    public = _without_internal_paths(json.loads(json.dumps(job)))
    output = str(public.pop("output", "") or "")
    for key in (
        "_private",
        "cwd",
        "error",
        "idempotency_key",
        "log_path",
        "logfile",
        "plan_record",
        "script",
        "state_dir",
        "submission_tag",
        "trash_path",
        "trashed_to",
    ):
        public.pop(key, None)
    if output:
        # The UI shows only the scheduler output filename.  Preserve that
        # contract without disclosing the private state directory.
        public["output"] = Path(output).name
    if public.get("command"):
        public["command"] = runplan.redact_command_text(str(public["command"]))
    for key in ("capsule_error", "resolution_reason", "submission_error"):
        if public.get(key):
            public[key] = _public_text(public[key])
    return public


def _public_history_entry(entry: dict) -> dict:
    """Return the timeline fields while omitting private log/state locations."""
    public = _without_internal_paths(json.loads(json.dumps(entry)))
    if isinstance(public.get("session"), dict):
        public["session"] = _public_session_snapshot(public["session"])
    if isinstance(public.get("slurm_job"), dict):
        public["slurm_job"] = _public_slurm_job(public["slurm_job"])
    if public.get("command"):
        public["command"] = runplan.redact_command_text(str(public["command"]))
    if public.get("capsule_error"):
        public["capsule_error"] = _public_text(public["capsule_error"])
    return public


def _public_database_status(status: dict) -> dict:
    """Expose database health without its private filesystem location."""
    migrations = status.get("migrations")
    return {
        "journal_mode": str(status.get("journal_mode") or ""),
        "schema_version": int(status.get("schema_version") or 0),
        "migration_count": len(migrations) if isinstance(migrations, list) else 0,
        "legacy_imported": bool(status.get("legacy_import")),
    }


def _public_recovery_status(status: dict) -> dict:
    """Expose bounded startup counters, never exception text or paths."""
    allowed = {
        "status",
        "reconciled",
        "reason",
        "queued",
        "lost",
        "slurm",
        "submission_unknown",
        "finalized",
        "owned_elsewhere",
    }
    public = {key: status[key] for key in allowed if key in status}
    if status.get("error"):
        public["error"] = "startup recovery failed; inspect the private server log"
    return public


def _public_job(job: dict | None) -> dict | None:
    """Return the least-data durable-job projection needed by the browser."""
    if job is None:
        return None
    public = _without_internal_paths(json.loads(json.dumps(job)))
    if isinstance(public.get("session"), dict):
        public["session"] = _public_session_snapshot(public["session"])
    if isinstance(public.get("slurm_job"), dict):
        public["slurm_job"] = _public_slurm_job(public["slurm_job"])
    if public.get("command"):
        public["command"] = runplan.redact_command_text(str(public["command"]))
    if public.get("last_error"):
        public["last_error"] = _public_text(public["last_error"])
    return public


def _public_job_events(events: list[dict]) -> list[dict]:
    public = []
    for event in events:
        row = json.loads(json.dumps(event))
        row["actor"] = "benchtop"
        if row.get("reason"):
            row["reason"] = _public_text(row["reason"])
        payload = row.get("payload")
        if isinstance(payload, dict):
            row["payload"] = _public_job(payload)
        public.append(row)
    return public


def _public_job_response(result: dict) -> dict:
    public = dict(result)
    if isinstance(public.get("job"), dict):
        public["job"] = _public_job(public["job"])
    if isinstance(public.get("durable_job"), dict):
        public["durable_job"] = _public_job(public["durable_job"])
    return public


def _workspace_identifier(root: Path) -> str:
    """Return a stable opaque browser-storage namespace for a canonical root."""
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()


def _session_terminal_state(s: Session) -> str:
    forced = str(s.meta.get("terminal_job_state") or "")
    if forced in store.TERMINAL_STATES:
        return forced
    if s.status == "killed":
        return "cancelled"
    return "succeeded" if s.exit_code == 0 else "failed"


def _persist_session_terminal(s: Session, *, reason: str = "") -> dict | None:
    job = store.get_job(s.id)
    if not job:
        return None
    target = _session_terminal_state(s)
    if job["state"] in store.TERMINAL_STATES:
        return job
    terminal_payload = _session_job_payload(s)
    if s.kind == "run":
        terminal_payload.update(
            {
                "capsule_finalize_pending": bool(s.meta.get("capsule_id")),
                "history_finalize_pending": True,
            }
        )
    if job["state"] in {"cancel_requested", "cancelling"}:
        if job.get("cancel_dispatching"):
            terminal_payload.update(
                {
                    "cancel_pending_terminal_state": target,
                    "cancel_pending_terminal_reason": reason
                    or f"session became {s.status}",
                    "capsule_finalize_pending": bool(s.meta.get("capsule_id")),
                    "history_finalize_pending": True,
                }
            )
            return store.update_job(
                s.id,
                payload_update=terminal_payload,
                private_update=_session_private_payload(s),
                event_type="exit_deferred_for_cancel_dispatch",
                reason="waiting for durable signal outcome before terminal classification",
                actor=INSTANCE_ID,
                ended=s.ended or time.time(),
            )
        if job.get("cancel_signalled"):
            target = "cancelled"
    try:
        updated = store.transition_job(
            s.id,
            target,
            reason=reason or f"session became {s.status}",
            actor=INSTANCE_ID,
            payload_update=terminal_payload,
            private_update=_session_private_payload(s),
            ended=s.ended or time.time(),
            pid=None,
            process_start="",
            last_error=(
                "" if target == "succeeded" else str(s.meta.get("capsule_error") or "")
            ),
        )
    except store.InvalidTransition:
        updated = store.get_job(s.id)
    try:
        store.release_job_lease(s.id, INSTANCE_ID)
    except Exception:
        pass
    return updated


def _persist_session_running(s: Session) -> dict:
    return store.transition_job(
        s.id,
        "running",
        expected_states=("preparing",),
        reason="PTY child started",
        actor=INSTANCE_ID,
        payload_update=_session_job_payload(s),
        private_update=_session_private_payload(s),
        pid=s.pid,
        process_start=s.process_start,
        started=s.started,
        lease_owner=INSTANCE_ID,
        lease_expires=time.time() + CONTROLLER_LEASE_TTL,
    )


async def _spawn(**kwargs) -> Session:
    """Register and start a PTY; persist run output but never agent output."""
    initial_input = str(kwargs.pop("initial_input", ""))
    if len(mgr.active()) >= MAX_INTERACTIVE_SESSIONS:
        raise HTTPException(429, "interactive session limit reached")
    settings.ensure_dirs()
    s = mgr.create(**kwargs)
    s.logfile = None if s.kind == "agent" else str(settings.RUN_LOG_DIR / f"{s.id}.log")
    original_on_exit = s._on_exit

    def durable_exit(session: Session) -> None:
        terminal_job = _persist_session_terminal(session, reason="process exited")
        if (
            original_on_exit
            and terminal_job
            and terminal_job.get("state") in store.TERMINAL_STATES
        ):
            original_on_exit(session)

    s._on_exit = durable_exit
    try:
        store.create_job(
            job_id=s.id,
            kind=s.kind,
            executor="local",
            pipeline=str(s.meta.get("pipeline") or ""),
            state="created",
            session_id=s.id,
            payload=_session_job_payload(s),
            private=_session_private_payload(s),
            actor=INSTANCE_ID,
            lease_owner=INSTANCE_ID,
            lease_ttl=CONTROLLER_LEASE_TTL,
        )
        if not store.acquire_job_lease(
            s.id,
            INSTANCE_ID,
            ttl=CONTROLLER_LEASE_TTL,
            expected_states=("created",),
        ):
            raise RuntimeError(
                "could not acquire durable interactive-session ownership"
            )
        store.transition_job(
            s.id,
            "preparing",
            expected_states=("created",),
            reason="interactive session accepted",
            actor=INSTANCE_ID,
            payload_update=_session_job_payload(s),
            private_update=_session_private_payload(s),
        )
        s.start()
        if initial_input:
            s.write_initial_input(initial_input.encode("utf-8") + b"\n")
        _persist_session_running(s)
    except Exception as exc:
        s.meta["terminal_job_state"] = "failed"
        s.meta["capsule_error"] = str(exc)
        if s.pid:
            try:
                await s.cancel(interrupt_grace=0.25, terminate_grace=0.25)
                deadline = time.monotonic() + 1.0
                while s.ended is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                if s.is_alive():
                    s.send_signal(signal.SIGKILL)
                    deadline = time.monotonic() + 1.0
                    while s.ended is None and time.monotonic() < deadline:
                        await asyncio.sleep(0.05)
                if s.fd is not None:
                    s._on_eof()
                # PTY EOF can arrive just before the child becomes waitable.
                # Drive one final reap cycle instead of leaving a dead PID on
                # the failed Session when the event-loop reader already closed.
                if s.ended is None:
                    s._poll_child_exit()
                    deadline = time.monotonic() + 1.0
                    while s.ended is None and time.monotonic() < deadline:
                        await asyncio.sleep(0.05)
            except Exception:
                pass
        try:
            job = store.get_job(s.id)
            if job and job.get("state") not in store.TERMINAL_STATES:
                store.transition_job(
                    s.id,
                    "failed",
                    reason=f"session start failed: {exc}",
                    actor=INSTANCE_ID,
                    last_error=str(exc),
                    pid=None,
                    process_start="",
                    lease_owner="",
                    lease_expires=None,
                )
        except Exception:
            pass
        if not s.is_alive():
            mgr.sessions.pop(s.id, None)
        raise
    return s


def _run_queue_config() -> dict:
    return store.run_queue_config(settings.MAX_LOCAL_CORES)


def _run_core_budget() -> int:
    return _run_queue_config()["max_cores"]


def _session_cores(s: Session) -> int:
    try:
        return max(1, int(s.meta.get("cores", 1)))
    except (TypeError, ValueError):
        return 1


def _running_run_sessions() -> list[Session]:
    return [s for s in mgr.list() if s.kind == "run" and s.status == "running"]


def _active_run_cores() -> int:
    active = store.list_jobs(
        kind="run",
        executor="local",
        states=("preparing", "running", "cancel_requested", "cancelling"),
        limit=1000,
    )
    lost = store.list_jobs(
        kind="run",
        executor="local",
        states=("lost",),
        limit=1000,
    )
    for job in lost:
        if job.get("process_alive_at_recovery") and _durable_process_alive(job):
            active.append(job)
        elif job.get("process_alive_at_recovery"):
            try:
                store.update_job(
                    job["id"],
                    payload_update={"process_alive_at_recovery": False},
                    event_type="lost_process_no_longer_alive",
                    reason="released conservative local core reservation",
                    actor=INSTANCE_ID,
                )
            except Exception:
                # On a persistence failure, keep reserving capacity conservatively.
                active.append(job)
    total = 0
    for job in active:
        try:
            total += max(1, int(job.get("cores") or 1))
        except (TypeError, ValueError):
            total += 1
    return total


def _job_session_projection(job: dict) -> dict:
    snapshot = _public_session_snapshot(job.get("session") or {})
    state_to_status = {
        "created": "created",
        "queued": "queued",
        "preparing": "queued",
        "running": "running",
        "cancel_requested": "killed",
        "cancelling": "killed",
        "succeeded": "exited",
        "failed": "failed",
        "cancelled": "killed",
        "blocked": "failed",
        "lost": "failed",
    }
    snapshot.update(
        {
            "id": job.get("session_id") or job["id"],
            "kind": job.get("kind") or snapshot.get("kind") or "run",
            "title": job.get("title") or snapshot.get("title") or job["id"],
            "status": state_to_status.get(
                job.get("state"), snapshot.get("status", "failed")
            ),
            "created": job.get("created"),
            "started": job.get("started"),
            "ended": job.get("ended"),
            "durable_state": job.get("state"),
            "state_version": job.get("state_version"),
        }
    )
    meta = dict(snapshot.get("meta") or {})
    meta.update(
        {
            "durable_job_id": job["id"],
            "durable_state": job.get("state"),
            "cores": job.get("cores", meta.get("cores", 1)),
        }
    )
    snapshot["meta"] = meta
    return snapshot


def _local_replay_response(job: dict) -> dict:
    session = mgr.get(str(job.get("session_id") or job["id"]))
    return {
        "session": session.to_dict() if session else _job_session_projection(job),
        "job": _public_job(job),
        "queue": _run_queue_state(),
        "plan_digest": job.get("plan_digest", ""),
        "plan_schema_version": runplan.SCHEMA_VERSION,
        "idempotency_key": job.get("idempotency_key", ""),
        "idempotent_replay": True,
        "replayed": True,
    }


def _queued_execution_binding_error(job: dict, execution: object) -> str:
    """Return a safe reason when restored execution is not root-authorized."""
    if not isinstance(execution, dict):
        return "queued execution record is malformed"
    meta = execution.get("meta")
    if not isinstance(meta, dict):
        return "queued execution metadata is malformed"
    pipeline_name = str(job.get("pipeline") or "")
    if not pipeline_name or str(meta.get("pipeline") or "") != pipeline_name:
        return "queued execution pipeline identity is inconsistent"
    pipeline = pipelines.get(pipeline_name)
    if not pipeline:
        return "queued execution pipeline is not currently discovered"
    try:
        registered_root = settings.canonical_pipeline_root()
        pipeline_root = pipeline.path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return "queued execution pipeline cannot be resolved safely"
    if (
        pipeline.name != pipeline_name
        or pipeline.path != pipeline_root
        or pipeline_root.parent != registered_root
    ):
        return "queued execution pipeline is outside the registered root"

    raw_cwd = execution.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd or not Path(raw_cwd).is_absolute():
        return "queued execution working directory is invalid"
    cwd = Path(raw_cwd)
    try:
        lexical_cwd = Path(os.path.abspath(os.fspath(cwd)))
        resolved_cwd = cwd.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return "queued execution working directory cannot be resolved safely"
    if cwd != lexical_cwd or cwd != pipeline_root or resolved_cwd != pipeline_root:
        return "queued execution working directory does not match its pipeline"

    # New records carry the identity copied from the immutable RunPlan.  Older
    # queued records remain compatible, but if this binding is present it must
    # independently agree with discovery, the durable job, and the exact cwd.
    plan_pipeline = execution.get("run_plan_pipeline")
    if plan_pipeline is not None:
        if not isinstance(plan_pipeline, dict):
            return "queued RunPlan pipeline identity is malformed"
        if (
            str(plan_pipeline.get("name") or "") != pipeline_name
            or str(plan_pipeline.get("local_name") or "") != pipeline_root.name
            or str(plan_pipeline.get("root") or "") != pipeline_root.as_posix()
        ):
            return "queued RunPlan pipeline does not match the registered pipeline"
    return ""


def _terminalize_unauthorized_queued_job(job: dict, reason: str) -> None:
    """Make a rejected pre-spawn record terminal without executing it."""
    state = str(job.get("state") or "")
    if state not in {"created", "queued"}:
        return
    try:
        store.transition_job(
            job["id"],
            "blocked",
            expected_states=(state,),
            event_type="queued_execution_root_rejected",
            reason=reason,
            actor=INSTANCE_ID,
            last_error=reason,
            payload_update={"restore_authorized": False},
        )
    except (store.InvalidTransition, store.StateError):
        pass


def _restore_queued_session(job: dict) -> Session | None:
    private = store.get_job(job["id"], include_private=True) or {}
    execution = (private.get("_private") or {}).get("execution") or {}
    binding_error = _queued_execution_binding_error(job, execution)
    if binding_error:
        mgr.sessions.pop(str(job.get("session_id") or job["id"]), None)
        _terminalize_unauthorized_queued_job(job, binding_error)
        return None
    current = mgr.get(str(job.get("session_id") or job["id"]))
    if current:
        return current
    argv = list(execution.get("argv") or [])
    cwd = str(execution.get("cwd") or "")
    if not argv or not cwd:
        try:
            store.transition_job(
                job["id"],
                "lost",
                expected_states=("queued",),
                reason="queued execution record is incomplete",
                actor=INSTANCE_ID,
                last_error="queued execution record is incomplete",
            )
        except Exception:
            pass
        return None
    meta = dict(execution.get("meta") or {})
    pipeline_name = str(job.get("pipeline") or meta.get("pipeline") or "")
    callback = (
        _record_run_on_exit(pipeline_name, {"dryrun": bool(meta.get("dryrun"))})
        if execution.get("kind") == "run"
        else _record_on_exit(pipeline_name, {})
    )
    session = Session(
        kind=str(execution.get("kind") or "run"),
        title=str(execution.get("title") or job.get("title") or job["id"]),
        cwd=cwd,
        argv=argv,
        env=dict(execution.get("env") or {}),
        cols=int(execution.get("cols") or 120),
        rows=int(execution.get("rows") or 32),
        meta=meta,
        logfile=str(execution.get("logfile") or job.get("logfile") or "") or None,
        on_exit=callback,
        session_id=str(job.get("session_id") or job["id"]),
        created=float(execution.get("created") or job.get("created") or time.time()),
    )
    session.status = "queued"
    return mgr.register(session)


def _queued_run_sessions(*, restore: bool = False) -> list[Session]:
    out: list[Session] = []
    jobs = store.list_jobs(
        kind="run",
        executor="local",
        states=("queued",),
        limit=1000,
        oldest_first=True,
    )
    for job in jobs:
        s = mgr.get(str(job.get("session_id") or job["id"]))
        if not s and restore:
            s = _restore_queued_session(job)
        if s and s.status == "queued":
            out.append(s)
    return out


def _run_queue_state() -> dict:
    queued_jobs = store.list_jobs(
        kind="run",
        executor="local",
        states=("queued",),
        limit=1000,
        oldest_first=True,
    )
    queued = []
    for i, job in enumerate(queued_jobs, 1):
        row = _job_session_projection(job)
        row.setdefault("meta", {})["queue_position"] = i
        queued.append(row)
    running_jobs = store.list_jobs(
        kind="run",
        executor="local",
        states=("preparing", "running", "cancel_requested", "cancelling"),
        limit=1000,
        oldest_first=True,
    )
    running = []
    for job in running_jobs:
        session = mgr.get(str(job.get("session_id") or job["id"]))
        running.append(
            session.to_dict()
            if session and _session_controls_job(session, job)
            else _job_session_projection(job)
        )
    lost = store.list_jobs(
        kind="run",
        executor="local",
        states=("lost",),
        limit=25,
    )
    lost_rows = []
    for job in lost:
        row = _job_session_projection(job)
        alive = bool(
            job.get("process_alive_at_recovery") and _durable_process_alive(job)
        )
        row.setdefault("meta", {}).update(
            {
                "process_identity_status": "alive-and-matched"
                if alive
                else "not-running",
                "cores_reserved": alive,
                "pid": job.get("pid") if alive else None,
            }
        )
        lost_rows.append(row)
    return {
        "max_cores": _run_core_budget(),
        "active_cores": _active_run_cores(),
        "running": running,
        "queued": queued,
        "lost": lost_rows,
        "durability": store.database_status(),
    }


def _prepare_run_capsule(s: Session) -> bool:
    """Capture execution-time state and return whether the run may start."""
    if s.kind != "run" or s.meta.get("capsule_id"):
        return not bool(s.meta.get("study_blocked_at_execution"))
    pipeline_name = str(s.meta.get("pipeline") or "")
    p = pipelines.get(pipeline_name)
    if not p:
        s.meta["capsule_error"] = (
            f"pipeline {pipeline_name!r} was not found at execution time"
        )
        s.meta["study_blocked_at_execution"] = True
        return False
    try:
        pipeline_root = p.path.resolve(strict=True)
        session_cwd = Path(s.cwd)
        exact_cwd = (
            session_cwd.is_absolute()
            and session_cwd == Path(os.path.abspath(os.fspath(session_cwd)))
            and session_cwd == pipeline_root
            and session_cwd.resolve(strict=True) == pipeline_root
        )
    except (OSError, RuntimeError, ValueError):
        exact_cwd = False
    if not exact_cwd:
        s.meta["capsule_error"] = (
            "execution working directory does not match the registered pipeline"
        )
        s.meta["study_blocked_at_execution"] = True
        s.meta["run_plan_blocked_at_execution"] = True
        return False
    blocked = False
    expected_plan_digest = str(s.meta.get("plan_digest") or "")
    try:
        body = RunRequest(
            target=str(s.meta.get("target") or ""),
            configfile=str(s.meta.get("configfile") or ""),
            profile=str(s.meta.get("profile") or ""),
            cores=_session_cores(s),
            dryrun=bool(s.meta.get("dryrun")),
            use_conda=bool(s.meta.get("use_conda", True)),
            extra=str(s.meta.get("extra") or ""),
            env=str(s.meta.get("env") or ""),
            study_sheet="",
            study_roles=dict(s.meta.get("study_roles") or {}),
            study_override=bool(s.meta.get("study_override")),
            study_fingerprint=str(s.meta.get("study_fingerprint") or ""),
        )
        current_plan, _, report, override_valid, _ = _resolve_run_plan(
            p,
            body,
            enforce_gate=False,
            check_digest=False,
            require_explicit_study=False,
            enforce_resolution=True,
        )
        accepted_fingerprint = str(s.meta.get("study_fingerprint") or "")
        current_fingerprint = str(report.get("fingerprint") or "")
        accepted_sheet = str(s.meta.get("study_sheet") or "")
        current_sheet = str((report.get("selected") or {}).get("path") or "")
        sheet_changed = bool(accepted_sheet and accepted_sheet != current_sheet)
        study_changed = bool(
            sheet_changed
            or (
                accepted_fingerprint
                and current_fingerprint
                and accepted_fingerprint != current_fingerprint
            )
        )
        plan_changed = bool(
            expected_plan_digest and expected_plan_digest != current_plan.digest
        )
        s.meta["current_study_gate"] = report.get("gate", "unknown")
        s.meta["current_study_sheet"] = current_sheet
        s.meta["current_study_fingerprint"] = current_fingerprint
        if not accepted_sheet:
            s.meta["study_sheet"] = current_sheet
        if not accepted_fingerprint:
            s.meta["study_fingerprint"] = current_fingerprint
        if not study_changed:
            s.meta["study_gate"] = report.get("gate", "unknown")
        s.meta["plan_schema_version"] = runplan.SCHEMA_VERSION
        if not expected_plan_digest:
            s.meta["plan_digest"] = current_plan.digest
        blocked = bool(
            plan_changed
            or (
                not s.meta.get("dryrun")
                and (
                    study_changed
                    or (
                        report.get("summary", {}).get("errors", 0)
                        and not override_valid
                    )
                )
            )
        )
        s.meta["study_blocked_at_execution"] = blocked
        s.meta["run_plan_blocked_at_execution"] = plan_changed
        if study_changed:
            s.meta["study_changed_while_queued"] = True
            reason = "selection" if sheet_changed else "fingerprint"
            s.note(f"OmicsANG: Study Guard {reason} changed while this run was queued")
        if plan_changed:
            s.meta["run_plan_changed_while_queued"] = True
            s.meta["current_plan_digest"] = current_plan.digest
            s.note(
                "OmicsANG: RunPlan changed while this run was queued; preview and launch again"
            )
        if not plan_changed:
            s._run_plan = current_plan
            capsule = provenance.capture(p, s.id, s.meta, run_plan=current_plan)
            s.meta["capsule_id"] = capsule["id"]
            s.meta["capsule_fingerprint"] = capsule["fingerprint"]
            store.record_capsule_ref(
                provenance.capsule_reference(
                    p,
                    capsule,
                    job_id=s.id,
                    status="running",
                )
            )
            s.note(f"OmicsANG: run capsule {capsule['id']} captured at execution time")
    except Exception as exc:
        s.meta["capsule_error"] = str(exc)
        blocked = bool(expected_plan_digest or not s.meta.get("dryrun"))
        s.meta["study_blocked_at_execution"] = blocked
        s.meta["run_plan_blocked_at_execution"] = bool(expected_plan_digest)
        s.note(f"OmicsANG: run contract or capsule preparation failed: {exc}")
    if blocked:
        contract = (
            "RunPlan" if s.meta.get("run_plan_changed_while_queued") else "Study Guard"
        )
        s.note(
            f"OmicsANG: {contract} blocked this run at execution time; preview and launch again"
        )
    try:
        if store.get_job(s.id):
            store.update_job(
                s.id,
                payload_update=_session_job_payload(s),
                private_update=_session_private_payload(s),
                event_type="execution_contract_checked",
                reason="blocked" if blocked else "ready",
                actor=INSTANCE_ID,
            )
    except Exception as exc:
        s.note(f"OmicsANG: durable contract update failed: {exc}")
        blocked = True
        s.meta["study_blocked_at_execution"] = True
    return not blocked


def _maybe_start_queued_runs_sync() -> None:
    global LOCAL_SCHEDULER_ACTIVE
    if not LOCAL_SCHEDULER_LOCK.acquire(blocking=False):
        return
    try:
        LOCAL_SCHEDULER_ACTIVE = True
        lease_payload = {
            "pid": os.getpid(),
            "process_start": Session._process_start_token(os.getpid()),
            "boot_id": Session._boot_id(),
        }
        token = store.acquire_lease(
            "scheduler:local",
            INSTANCE_ID,
            ttl=15.0,
            payload=lease_payload,
        )
        if not token:
            # A controller can die while holding the cross-process lease. Reclaim
            # only when its exact PID/start/boot identity no longer exists.
            held = store.lease("scheduler:local") or {}
            owner = str(held.get("owner") or "")
            payload = held.get("payload") or {}
            owner_alive = bool(
                payload.get("pid")
                and payload.get("process_start")
                and payload.get("boot_id") == Session._boot_id()
                and Session._process_start_token(int(payload["pid"]))
                == str(payload.get("process_start"))
            )
            if owner and not owner_alive:
                store.release_lease(
                    "scheduler:local",
                    owner,
                    str(held.get("token") or ""),
                )
                token = store.acquire_lease(
                    "scheduler:local",
                    INSTANCE_ID,
                    ttl=15.0,
                    payload=lease_payload,
                )
        if not token:
            return
        try:
            _schedule_queued_runs_under_lock()
        finally:
            store.release_lease("scheduler:local", INSTANCE_ID, token)
    finally:
        LOCAL_SCHEDULER_ACTIVE = False
        LOCAL_SCHEDULER_LOCK.release()


def _maybe_start_queued_runs() -> None:
    """Wake the scheduler without hashing RunPlan/capsule material on the API loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _maybe_start_queued_runs_sync()
        return
    task = loop.create_task(
        _maybe_start_queued_runs_async(),
        name="benchtop-local-scheduler",
    )
    CONTROLLER_TASKS.add(task)
    task.add_done_callback(CONTROLLER_TASKS.discard)


def _schedule_queued_runs_under_lock() -> None:
    while True:
        started = False
        budget = _run_core_budget()
        active = _active_run_cores()
        for s in _queued_run_sessions(restore=True):
            cores = _session_cores(s)
            if active + cores > budget:
                continue
            lease = store.acquire_job_lease(
                s.id,
                INSTANCE_ID,
                ttl=60.0,
                expected_states=("queued",),
            )
            if not lease:
                continue
            try:
                store.transition_job(
                    s.id,
                    "preparing",
                    expected_states=("queued",),
                    reason="local core lease acquired",
                    actor=INSTANCE_ID,
                    payload_update=_session_job_payload(s),
                    private_update=_session_private_payload(s),
                )
            except store.InvalidTransition:
                store.release_job_lease(s.id, INSTANCE_ID)
                continue
            s.meta.pop("queue_position", None)
            s.meta["queue_started"] = time.time()
            s.note(f"OmicsANG: starting queued run ({cores}/{budget} local cores)")
            if _prepare_run_capsule(s):
                try:
                    s.start()
                    _persist_session_running(s)
                except Exception as exc:
                    s.meta["terminal_job_state"] = "failed"
                    s.meta["capsule_error"] = str(exc)
                    s.note(f"OmicsANG: could not start queued run: {exc}")
                    if s.pid:
                        s.kill(signal.SIGTERM)
                    else:
                        s.status = "failed"
                        s.exit_code = 127
                        s.ended = time.time()
                    try:
                        store.transition_job(
                            s.id,
                            "failed",
                            expected_states=("preparing", "running"),
                            reason=f"PTY start failed: {exc}",
                            actor=INSTANCE_ID,
                            last_error=str(exc),
                            payload_update=_session_job_payload(s),
                        )
                    finally:
                        store.release_job_lease(s.id, INSTANCE_ID)
            else:
                s.meta["terminal_job_state"] = "blocked"
                s.kill()
            started = True
            break
        if not started:
            break


async def _maybe_start_queued_runs_async() -> None:
    global LOCAL_SCHEDULER_ACTIVE
    if not LOCAL_SCHEDULER_LOCK.acquire(blocking=False):
        return
    try:
        LOCAL_SCHEDULER_ACTIVE = True
        lease_payload = _controller_identity_payload()
        token = store.acquire_lease(
            "scheduler:local",
            INSTANCE_ID,
            ttl=600.0,
            payload=lease_payload,
        )
        if not token:
            held = store.lease("scheduler:local") or {}
            owner = str(held.get("owner") or "")
            payload = held.get("payload") or {}
            owner_alive = bool(
                payload.get("pid")
                and payload.get("process_start")
                and payload.get("boot_id") == Session._boot_id()
                and Session._process_start_token(int(payload["pid"]))
                == str(payload.get("process_start"))
            )
            if owner and not owner_alive:
                store.release_lease(
                    "scheduler:local",
                    owner,
                    str(held.get("token") or ""),
                )
                token = store.acquire_lease(
                    "scheduler:local",
                    INSTANCE_ID,
                    ttl=600.0,
                    payload=lease_payload,
                )
        if not token:
            return
        try:
            await _schedule_queued_runs_under_lock_async()
        finally:
            store.release_lease("scheduler:local", INSTANCE_ID, token)
    finally:
        LOCAL_SCHEDULER_ACTIVE = False
        LOCAL_SCHEDULER_LOCK.release()


async def _schedule_queued_runs_under_lock_async() -> None:
    while True:
        started = False
        budget = _run_core_budget()
        active = _active_run_cores()
        for s in _queued_run_sessions(restore=True):
            cores = _session_cores(s)
            if active + cores > budget:
                continue
            lease = store.acquire_job_lease(
                s.id,
                INSTANCE_ID,
                ttl=600.0,
                expected_states=("queued",),
            )
            if not lease:
                continue
            try:
                store.transition_job(
                    s.id,
                    "preparing",
                    expected_states=("queued",),
                    reason="local core lease acquired",
                    actor=INSTANCE_ID,
                    payload_update=_session_job_payload(s),
                    private_update=_session_private_payload(s),
                )
            except store.InvalidTransition:
                store.release_job_lease(s.id, INSTANCE_ID)
                continue
            s.meta.pop("queue_position", None)
            s.meta["queue_started"] = time.time()
            s.note(f"OmicsANG: starting queued run ({cores}/{budget} local cores)")
            allowed = await _await_blocking(_prepare_run_capsule, s)
            if allowed:
                try:
                    s.start()
                    await _await_blocking(_persist_session_running, s)
                except Exception as exc:
                    s.meta["terminal_job_state"] = "failed"
                    s.meta["capsule_error"] = str(exc)
                    s.note(f"OmicsANG: could not start queued run: {exc}")
                    if s.pid:
                        try:
                            await s.cancel(interrupt_grace=0.25, terminate_grace=0.25)
                        except Exception:
                            pass
                    else:
                        s.status = "failed"
                        s.exit_code = 127
                        s.ended = time.time()
                    try:
                        await _await_blocking(
                            store.transition_job,
                            s.id,
                            "failed",
                            expected_states=("preparing", "running"),
                            reason=f"PTY start failed: {exc}",
                            actor=INSTANCE_ID,
                            last_error=str(exc),
                            payload_update=_session_job_payload(s),
                        )
                    finally:
                        store.release_job_lease(s.id, INSTANCE_ID)
            else:
                s.meta["terminal_job_state"] = "blocked"
                s.kill()
            started = True
            break
        if not started:
            break


def _record_run_on_exit(pipeline_name: str, meta: dict):
    def cb(s: Session):
        # Capacity and the authoritative terminal state are released first.  A
        # capsule/history write failure must never strand the durable queue.
        terminal_job = _persist_session_terminal(s, reason="run process exited")
        if not terminal_job or terminal_job.get("state") not in store.TERMINAL_STATES:
            return
        if s._loop and not s._loop.is_closed():
            s._loop.call_soon_threadsafe(_maybe_start_queued_runs)
        capsule_error = ""
        capsule_complete = not bool(s.meta.get("capsule_id"))
        if s.meta.get("capsule_id"):
            try:
                p = pipelines.get(pipeline_name)
                if not p:
                    raise RuntimeError(
                        f"pipeline {pipeline_name!r} not found while finalizing capsule"
                    )
                capsule = provenance.finalize(p, s)
                s.meta["capsule_complete"] = True
                s.meta["capsule_fingerprint"] = capsule.get("fingerprint", "")
                store.record_capsule_ref(
                    provenance.capsule_reference(
                        p,
                        capsule,
                        job_id=s.id,
                        status=s.status,
                    )
                )
                capsule_complete = True
            except Exception as exc:
                capsule_error = str(exc)
                s.meta["capsule_error"] = capsule_error
        persisted_meta = {
            key: s.meta.get(key)
            for key in (
                "dryrun",
                "human",
                "cores",
                "target",
                "configfile",
                "profile",
                "use_conda",
                "extra",
                "env",
                "study_sheet",
                "study_gate",
                "study_fingerprint",
                "study_override",
                "study_override_fingerprint",
                "current_study_sheet",
                "current_study_gate",
                "current_study_fingerprint",
                "study_changed_while_queued",
                "study_blocked_at_execution",
                "plan_schema_version",
                "plan_digest",
                "current_plan_digest",
                "run_plan_changed_while_queued",
                "run_plan_blocked_at_execution",
                "capsule_id",
                "capsule_fingerprint",
                "capsule_complete",
                "capsule_error",
            )
            if key in s.meta
        }
        for key in ("human", "extra"):
            if key in persisted_meta:
                persisted_meta[key] = provenance.redact_text(
                    str(persisted_meta[key] or "")
                )
        history_complete = False
        try:
            store.record(
                {
                    "id": s.id,
                    "kind": s.kind,
                    "pipeline": pipeline_name,
                    "title": s.title,
                    "status": s.status,
                    "exit_code": s.exit_code,
                    "command": runplan.redact_command(s.argv),
                    "started": s.started,
                    "ended": s.ended,
                    "logfile": s.logfile,
                    **persisted_meta,
                    **meta,
                }
            )
            history_complete = True
        except Exception as exc:
            try:
                store.append_event(
                    s.id,
                    "history_persist_failed",
                    reason=str(exc),
                    actor=INSTANCE_ID,
                )
            except Exception:
                pass
        if capsule_error:
            s.note(f"OmicsANG: run capsule finalization failed: {capsule_error}")
        try:
            store.update_job(
                s.id,
                payload_update={
                    "capsule_finalize_pending": not capsule_complete,
                    "history_finalize_pending": not history_complete,
                    "capsule_id": str(s.meta.get("capsule_id") or ""),
                    "capsule_fingerprint": str(s.meta.get("capsule_fingerprint") or ""),
                },
                event_type="run_finalization_checkpoint",
                reason=(
                    "complete"
                    if capsule_complete and history_complete
                    else "retry required"
                ),
                actor=INSTANCE_ID,
            )
        except Exception:
            pass

    return cb


def _queue_or_start_run(s: Session) -> None:
    budget = _run_core_budget()
    cores = _session_cores(s)
    if cores > budget:
        mgr.sessions.pop(s.id, None)
        raise HTTPException(
            400,
            f"requested {cores} cores exceeds the local run budget of {budget}; "
            "raise the budget or lower the run cores",
        )
    settings.ensure_dirs()
    s.logfile = str(settings.RUN_LOG_DIR / f"{s.id}.log")
    s.status = "queued"
    s.meta["queued_at"] = time.time()
    store.transition_job(
        s.id,
        "queued",
        expected_states=("created",),
        reason="accepted by local durable scheduler",
        actor=INSTANCE_ID,
        payload_update=_session_job_payload(s),
        private_update=_session_private_payload(s),
        queued_at=s.meta["queued_at"],
    )
    s.note(
        f"OmicsANG: queued run requesting {cores} cores; "
        f"{_active_run_cores()}/{budget} cores active"
    )
    _maybe_start_queued_runs()


# ---- request models ------------------------------------------------------
class RunRequest(BaseModel):
    target: str = ""
    configfile: str = ""
    profile: str = ""
    cores: int = 8
    dryrun: bool = True
    use_conda: bool = True
    extra: str = ""
    env: str = ""
    study_sheet: str = ""
    study_roles: dict[str, str] = {}
    study_override: bool = False
    study_fingerprint: str = ""
    plan_digest: str = ""
    idempotency_key: str = ""


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline: str
    tool: str = "claude"  # claude | codex | shell
    prompt: str = Field(default="", max_length=50_000)
    worktree_branch: str = Field(default="", max_length=240)
    acknowledge_external_agent: bool = False


class ConfigSave(BaseModel):
    path: str
    content: str


class CodeSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    create_dirs: bool = True
    expected_revision: str = Field(
        default="",
        pattern=r"^(?:|sha256:[0-9a-f]{64})$",
    )
    overwrite: bool = False


class CodeMove(BaseModel):
    src: str
    dst: str
    create_dirs: bool = True


class CodeCopy(BaseModel):
    src: str
    dst: str
    create_dirs: bool = True


class CodeDelete(BaseModel):
    path: str


class CodeMkdir(BaseModel):
    path: str


class CodeHelpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=fs_policy.MAX_RELATIVE_PATH)


class BrowseSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="", max_length=fs_policy.MAX_RELATIVE_PATH)
    query: str = Field(default="", max_length=BROWSE_MAX_QUERY)
    scope: str = Field(
        default="all",
        pattern="^(all|files|code|config|results|directories)$",
    )
    limit: int = Field(default=BROWSE_DEFAULT_LIMIT, ge=1, le=BROWSE_MAX_LIMIT)


class BrowseOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=fs_policy.MAX_RELATIVE_PATH)


class ConfigForm(BaseModel):
    path: str
    fields: list[dict] = []


class WorktreeRequest(BaseModel):
    pipeline: str
    branch: str
    base: str = "HEAD"


class FleetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(max_length=50_000)
    tool: str = "claude"
    pipelines: list[str] = Field(default_factory=list, max_length=32)
    use_worktree: bool = True
    branch: str = ""
    acknowledge_external_agent: bool = False


class FleetPR(BaseModel):
    index: int
    message: str = "OmicsANG: apply fleet task"
    title: str = ""
    body: str = ""


class GitHubPushRequest(BaseModel):
    branch: str = ""


class GitHubPRRequest(BaseModel):
    title: str
    body: str = ""
    draft: bool = False


class GitHubRepoCreateRequest(BaseModel):
    full_name: str
    private: bool = True
    description: str = ""
    push_initial: bool = False


class GitHubRepoConnectRequest(BaseModel):
    full_name: str


class DebugRequest(BaseModel):
    session_id: str
    tool: str = "claude"
    preview_only: bool = True
    acknowledge_external_agent: bool = False


class BootstrapRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)


class RunQueueConfig(BaseModel):
    max_cores: int


class JobCancelRequest(BaseModel):
    reason: str = "operator requested cancellation"
    expected_version: int | None = None
    interrupt_grace: float = 2.0
    terminate_grace: float = 2.0


class JobResolveRequest(BaseModel):
    action: str
    reason: str = ""
    expected_version: int | None = None


class RunTemplateSave(BaseModel):
    id: str = ""
    name: str
    form: dict = {}


class StudyAuditRequest(BaseModel):
    configfile: str = ""
    sheet: str = ""
    roles: dict[str, str] = {}


class ResultDirectoryRequest(BaseModel):
    path: str
    limit: int = 40


class ResultDirectorySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_id: str = Field(default="", max_length=80)
    query: str = Field(default="", max_length=BROWSE_MAX_QUERY)
    limit: int = Field(default=30, ge=1, le=100)


class SraSearchRequest(BaseModel):
    db: str = "sra"
    term: str = ""
    limit: int = 20


class SraDownloadRequest(BaseModel):
    accessions: str = ""
    accession_text: str = ""
    accessions_list: list[str] = []
    destination: str = "raw_files/sra"
    threads: int = 6
    use_prefetch: bool = True
    convert_fastq: bool = True
    gzip_fastq: bool = True
    split_files: bool = True
    skip_technical: bool = True


class DiagnosticFixRequest(BaseModel):
    action: str
    path: str = ""
    line: int = 0
    from_value: str = ""
    to_value: str = ""
    target: str = ""
    kind: str = "file"


class SlurmSubmitRequest(RunRequest):
    job_name: str = ""
    partition: str = ""
    account: str = ""
    qos: str = ""
    time_limit: str = "04:00:00"
    mem: str = ""
    gres: str = ""


@app.post("/api/auth/bootstrap")
def bootstrap_browser(body: BootstrapRequest, response: Response):
    """Exchange the fragment-only launch capability for a process-local cookie."""
    browser_session = security.AUTH_STATE.exchange(body.token)
    if not browser_session:
        raise HTTPException(401, "bootstrap capability is invalid or already used")
    response.headers.append("Set-Cookie", security.auth_cookie_header(browser_session))
    return {
        "ok": True,
        "csrf": browser_session.csrf,
        "expires": browser_session.expires,
    }


@app.get("/api/auth/session")
def recover_browser_session(request: Request):
    """Recover only the CSRF half of an authenticated browser session."""
    browser_session = getattr(request.state, "benchtop_session", None)
    if not isinstance(browser_session, security.BrowserSession):
        raise HTTPException(401, "authentication required")
    return {
        "ok": True,
        "csrf": browser_session.csrf,
        "expires": browser_session.expires,
    }


IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


def _submission_key(body: RunRequest) -> str:
    key = str(body.idempotency_key or "").strip()
    if not key:
        return uuid.uuid4().hex
    if not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise HTTPException(
            400,
            "idempotency_key must be 8-128 safe characters (letters, digits, . _ : -)",
        )
    return key


def _submission_request_digest(body: RunRequest, pipeline: str, executor: str) -> str:
    values = body.model_dump()
    values.pop("idempotency_key", None)
    canonical = json.dumps(
        {"pipeline": pipeline, "executor": executor, "request": values},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _assert_idempotent_contract(
    job: dict,
    *,
    pipeline: str,
    executor: str,
    plan_digest: str,
    request_digest: str = "",
) -> None:
    if (
        str(job.get("pipeline") or "") != pipeline
        or str(job.get("executor") or "") != executor
        or str(job.get("plan_digest") or "") != plan_digest
        or (
            request_digest
            and job.get("request_digest")
            and str(job.get("request_digest")) != request_digest
        )
    ):
        raise HTTPException(
            409,
            detail={
                "message": "idempotency key is already bound to a different execution contract",
                "job_id": job.get("id"),
                "existing_pipeline": job.get("pipeline"),
                "existing_executor": job.get("executor"),
                "existing_plan_digest": job.get("plan_digest"),
                "existing_request_digest": job.get("request_digest", ""),
            },
        )


def _slurm_plan_resources(
    pipeline: pipelines.Pipeline,
    body: SlurmSubmitRequest,
) -> dict:
    return {
        "job_name": _clean_slurm_field(
            body.job_name or f"bt-{pipeline.name}"[:32],
            "job name",
        ),
        "partition": _clean_slurm_field(body.partition, "partition"),
        "account": _clean_slurm_field(body.account, "account"),
        "qos": _clean_slurm_field(body.qos, "qos"),
        "time_limit": _clean_slurm_field(body.time_limit, "time limit"),
        "mem": _clean_slurm_field(body.mem, "memory"),
        "gres": _clean_slurm_field(body.gres, "gres"),
    }


def _audit_launch_study(
    pipeline: pipelines.Pipeline,
    body: RunRequest,
    *,
    enforce_gate: bool = True,
    require_explicit_study: bool = True,
) -> tuple[dict, bool]:
    if body.cores < 1:
        raise HTTPException(400, "cores must be at least 1")
    _validate_run_extra(body.extra)
    try:
        report = study.audit(
            pipeline,
            configfile=body.configfile,
            sheet="",
            roles=body.study_roles,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    selected = report.get("selected") or {}
    selected_sheet = str(selected.get("path") or "")
    has_study_context = bool(
        body.study_roles or body.study_fingerprint or body.study_override
    )
    if require_explicit_study and has_study_context and not body.study_sheet:
        raise HTTPException(
            409,
            detail={
                "message": (
                    "Study roles or fingerprints require an explicit audited study sheet; "
                    "review the configured study before launching"
                ),
                "study": report,
            },
        )
    if body.study_sheet and body.study_sheet != selected_sheet:
        raise HTTPException(
            409,
            detail={
                "message": "Study Guard selection is stale or is not referenced by this run config",
                "study": report,
            },
        )
    if (
        enforce_gate
        and not body.dryrun
        and selected_sheet
        and not selected.get("authoritative")
    ):
        raise HTTPException(
            409,
            detail={
                "message": (
                    "Study Guard found a sample table, but the selected run config does not "
                    "reference it; update the config and re-audit before a real run"
                ),
                "study": report,
            },
        )
    current_fingerprint = str(report.get("fingerprint") or "")
    override_valid = bool(
        body.study_override
        and body.study_fingerprint
        and body.study_fingerprint == current_fingerprint
    )
    if (
        enforce_gate
        and not body.dryrun
        and report.get("summary", {}).get("errors", 0)
        and not override_valid
    ):
        message = (
            "Study Guard override is stale; review the changed study before launching"
            if body.study_override
            else "Study Guard found blocking cohort or input errors"
        )
        raise HTTPException(409, detail={"message": message, "study": report})
    return report, override_valid


def _resolve_run_plan(
    pipeline: pipelines.Pipeline,
    body: RunRequest,
    *,
    executor: str = "local",
    enforce_gate: bool = True,
    check_digest: bool = True,
    require_explicit_study: bool = True,
    enforce_resolution: bool = False,
) -> tuple[runplan.RunPlan, str, dict, bool, str]:
    study_report, override_valid = _audit_launch_study(
        pipeline,
        body,
        enforce_gate=enforce_gate,
        require_explicit_study=require_explicit_study,
    )
    resolved_env = body.env or pipelines.resolve_env(pipeline) or ""
    argv, human = pipelines.build_snakemake_argv(
        pipeline,
        target=body.target,
        configfile=body.configfile,
        profile=body.profile,
        cores=body.cores,
        dryrun=body.dryrun,
        use_conda=body.use_conda,
        extra=body.extra,
        env=resolved_env,
        resolve_default_env=False,
    )
    launch = {
        "target": body.target,
        "configfile": body.configfile,
        "profile": body.profile,
        "cores": body.cores,
        "dryrun": body.dryrun,
        "use_conda": body.use_conda,
        "env": resolved_env,
        "study_override": override_valid,
    }
    scheduler = (
        _slurm_plan_resources(pipeline, body)
        if isinstance(body, SlurmSubmitRequest)
        else None
    )
    plan = runplan.build(
        pipeline,
        launch=launch,
        argv=argv,
        study_report=study_report,
        resolved_env=resolved_env,
        executor=executor,
        scheduler=scheduler,
    )
    resolution = dict(plan.payload.get("resolution") or {})
    if (
        enforce_resolution
        and not body.dryrun
        and not bool(resolution.get("launch_safe"))
    ):
        raise HTTPException(
            409,
            detail={
                "message": (
                    "RunPlan resolution is incomplete; real execution is blocked until "
                    "every workflow, config, reference, wrapper, and container identity "
                    "is resolved"
                ),
                "plan_resolution": resolution,
                "plan_digest": plan.digest,
            },
        )
    if check_digest and body.plan_digest and body.plan_digest != plan.digest:
        raise HTTPException(
            409,
            detail={
                "message": "RunPlan is stale; preview the run again before launching",
                "expected_plan_digest": body.plan_digest,
                "current_plan_digest": plan.digest,
            },
        )
    return plan, human, study_report, override_valid, resolved_env


def _latest_mtime_limited(root: Path, max_files: int = 600) -> float | None:
    if not root.exists():
        return None
    latest = root.stat().st_mtime
    seen = 0
    try:
        for path in root.rglob("*"):
            if seen >= max_files:
                break
            try:
                if path.is_file():
                    latest = max(latest, path.stat().st_mtime)
                    seen += 1
            except OSError:
                continue
    except OSError:
        return latest
    return latest


def _results_freshness(p: pipelines.Pipeline) -> dict:
    roots = []
    for name in ("results", "result", "outputs", "output", "reports", "qc"):
        root = p.path / name
        if root.exists():
            roots.append({"path": name, "mtime": _latest_mtime_limited(root)})
    roots.sort(key=lambda r: r.get("mtime") or 0, reverse=True)
    return {"roots": roots[:6], "latest": roots[0]["mtime"] if roots else None}


def _run_state_for_pipeline(name: str) -> dict:
    running = [
        _job_session_projection(job)
        for job in store.list_jobs(
            pipeline=name,
            kind="run",
            executor="local",
            states=("preparing", "running", "cancel_requested", "cancelling"),
            limit=100,
        )
    ]
    queued = [
        _job_session_projection(job)
        for job in store.list_jobs(
            pipeline=name,
            kind="run",
            executor="local",
            states=("queued",),
            limit=100,
        )
    ]
    return {"running": running, "queued": queued}


def _health_for_pipeline(
    p: pipelines.Pipeline, include_diagnostics: bool = True
) -> dict:
    hist = store.history(p.name, limit=1)
    diag = {"error": None, "warning": None, "info": None}
    if include_diagnostics:
        try:
            diag = code_diagnostics(p.name, limit=160)["summary"]
        except Exception:
            diag = {"error": None, "warning": None, "info": None}
    git = git_ops.status(p.path)
    run_state = _run_state_for_pipeline(p.name)
    freshness = _results_freshness(p)
    status = "ok"
    if diag.get("error"):
        status = "error"
    elif (hist and hist[0].get("status") == "failed") or diag.get("warning"):
        status = "warn"
    return {
        "name": p.name,
        "path": str(p.path),
        "kind": p.kind,
        "status": status,
        "git": {
            "branch": git.get("branch"),
            "dirty": git.get("dirty"),
            "is_repo": git.get("is_repo"),
        },
        "last_run": hist[0] if hist else None,
        "run_state": run_state,
        "diagnostics": diag,
        "configs": len(p.configs) + len(p.project_local) + len(p.test_configs),
        "env_resolved": pipelines.resolve_env(p),
        "result_freshness": freshness,
    }


SLURM_FIELD_RE = re.compile(r"^[A-Za-z0-9_./:=,+@%-]+$")
SLURM_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+:-]*$")
SLURM_CLUSTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SLURM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@+$-]*$")


def _clean_slurm_field(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "\n" in value or "\r" in value or not SLURM_FIELD_RE.match(value):
        raise HTTPException(400, f"invalid Slurm {label}")
    return value


def _write_private_text(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    if path.stat().st_mode & 0o077:
        path.unlink(missing_ok=True)
        raise OSError("private state file has group or world permissions")


def _slurm_safe_environment() -> dict[str, str]:
    """Minimal non-secret environment explicitly reconstructed on compute nodes."""
    defaults = {
        "HOME": str(Path.home()),
        "USER": os.environ.get("USER", ""),
        "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "")),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        "TMPDIR": os.environ.get("TMPDIR") or tempfile.gettempdir(),
    }
    return {
        key: value
        for key, value in defaults.items()
        if value and "\n" not in value and "\r" not in value and "\x00" not in value
    }


def _slurm_tools() -> dict:
    return {
        name: shutil.which(name)
        for name in ("sbatch", "squeue", "sacct", "scancel", "sinfo")
    }


def _effective_slurm_username() -> str:
    """Resolve a safe user filter for scheduler discovery or fail closed."""
    candidates = [os.environ.get("USER", ""), os.environ.get("LOGNAME", "")]
    if not any(str(value or "").strip() for value in candidates):
        try:
            candidates.append(pwd.getpwuid(os.geteuid()).pw_name)
        except Exception:
            return ""
    for value in candidates:
        username = str(value or "").strip()
        if SLURM_USERNAME_RE.fullmatch(username):
            return username
    return ""


def _run_slurm_cmd(
    argv: list[str], timeout: int = 20, *, umask: int | None = None
) -> dict:
    try:
        kwargs = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
        }
        if umask is not None and os.name == "posix":
            kwargs["umask"] = umask
        proc = subprocess.run(argv, **kwargs)
    except FileNotFoundError:
        return {"ok": False, "ambiguous": False, "error": f"{argv[0]} not found"}
    except OSError as exc:
        # Popen failed before the command could execute (permission, descriptor,
        # or other OS-level launch failure), so sbatch definitely did not accept
        # this invocation.  Return a normal executor failure so a persisted
        # `submitting` intent can transition to `failed` instead of stranding.
        return {
            "ok": False,
            "ambiguous": False,
            "error": f"could not execute {argv[0]}: {exc}",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "ambiguous": True, "error": f"{argv[0]} timed out"}
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    text = "\n".join(part for part in (stdout, stderr) if part)
    return {
        "ok": proc.returncode == 0,
        "ambiguous": False,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "output": text,
    }


def _slurm_live_jobs(cluster: str = "") -> list[dict]:
    tools = _slurm_tools()
    if not tools.get("squeue"):
        return []
    fmt = "%i|%j|%T|%M|%l|%D|%C|%m|%P|%R|%k"
    argv = [tools["squeue"], "-h"]
    if cluster:
        if not SLURM_CLUSTER_RE.fullmatch(cluster):
            return []
        argv.extend(["-M", cluster])
    current_user = _effective_slurm_username()
    if not current_user:
        return []
    argv.extend(["-u", current_user])
    argv.extend(["-o", fmt])
    res = _run_slurm_cmd(argv, timeout=12)
    if not res["ok"]:
        return []
    jobs = []
    for line in res.get("stdout", res.get("output", "")).splitlines():
        parts = line.split("|", 10)
        if len(parts) not in {10, 11}:
            continue
        if len(parts) == 10:
            parts.append("")
        (
            job_id,
            name,
            state,
            elapsed,
            limit,
            nodes,
            cpus,
            mem,
            partition,
            reason,
            comment,
        ) = parts
        jobs.append(
            {
                "job_id": job_id.strip(),
                "name": name.strip(),
                "state": state.strip(),
                "elapsed": elapsed.strip(),
                "time_limit": limit.strip(),
                "nodes": nodes.strip(),
                "cpus": cpus.strip(),
                "mem": mem.strip(),
                "partition": partition.strip(),
                "reason": reason.strip(),
                "comment": comment.strip(),
                "cluster": cluster,
                "benchtop_owned": comment.strip().startswith("benchtop:"),
            }
        )
    return jobs


def _slurm_accounting(
    job_ids: list[str],
    *,
    cluster: str = "",
) -> dict[str, dict]:
    tools = _slurm_tools()
    safe_ids = [job_id for job_id in job_ids if SLURM_JOB_ID_RE.fullmatch(job_id)]
    if not safe_ids or not tools.get("sacct"):
        return {}
    argv = [tools["sacct"], "-n", "-P"]
    if cluster:
        if not SLURM_CLUSTER_RE.fullmatch(cluster):
            return {}
        argv.extend(["-M", cluster])
    argv.extend(
        [
            "-j",
            ",".join(sorted(set(safe_ids))),
            "--format=JobIDRaw,State,ExitCode,Comment",
        ]
    )
    res = _run_slurm_cmd(argv, timeout=15)
    if not res.get("ok"):
        return {}
    rows: dict[str, dict] = {}
    for line in res.get("stdout", res.get("output", "")).splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        job_id, raw_state, exit_code, comment = (part.strip() for part in parts)
        root_id = job_id.split(".", 1)[0]
        if SLURM_JOB_ID_RE.fullmatch(root_id) and root_id not in rows:
            rows[root_id] = {
                "job_id": root_id,
                "state": raw_state.split()[0].rstrip("+"),
                "exit_code": exit_code,
                "comment": comment,
                "cluster": cluster,
            }
    return rows


def _canonical_slurm_state(raw_state: str) -> str | None:
    parts = str(raw_state or "").upper().split()
    if not parts:
        return None
    state = parts[0].rstrip("+")
    if state in {
        "PENDING",
        "CONFIGURING",
        "REQUEUED",
        "REQUEUE_FED",
        "REQUEUE_HOLD",
        "RESIZING",
        "SPECIAL_EXIT",
    }:
        return "pending"
    if state in {
        "RUNNING",
        "COMPLETING",
        "SIGNALING",
        "STAGE_OUT",
        "STOPPED",
        "SUSPENDED",
    }:
        return "running"
    if state == "COMPLETED":
        return "succeeded"
    if state.startswith("CANCELLED"):
        return "cancelled"
    if state in {
        "FAILED",
        "TIMEOUT",
        "OUT_OF_MEMORY",
        "NODE_FAIL",
        "BOOT_FAIL",
        "DEADLINE",
        "PREEMPTED",
        "REVOKED",
    }:
        return "failed"
    return None


def _scheduler_event_once(
    job: dict,
    event_type: str,
    reason: str,
    *,
    fingerprint: str = "",
    interval: float = 3600.0,
) -> dict:
    """Persist scheduler anomalies on change or a bounded reminder interval."""
    key = re.sub(r"[^a-z0-9_]+", "_", event_type.lower())
    fingerprint_key = f"{key}_fingerprint"
    timestamp_key = f"{key}_at"
    now = time.time()
    if (
        str(job.get(fingerprint_key) or "") == fingerprint
        and now - float(job.get(timestamp_key) or 0) < interval
    ):
        return job
    store.append_event(
        job["id"],
        event_type,
        reason=reason,
        actor=INSTANCE_ID,
    )
    return store.update_job(
        job["id"],
        payload_update={fingerprint_key: fingerprint, timestamp_key: now},
        event_type="",
        actor=INSTANCE_ID,
    )


def _dispatch_slurm_cancel(
    job: dict,
    *,
    ownership_observed: bool = False,
) -> tuple[dict, dict]:
    scheduler_id = str(job.get("scheduler_job_id") or "")
    if not scheduler_id:
        raise RuntimeError("scheduler job id is not yet reconciled")
    if not SLURM_JOB_ID_RE.fullmatch(scheduler_id):
        store.append_event(
            job["id"],
            "invalid_scheduler_job_id",
            reason="refused to pass an unsafe executor reference to scancel",
            actor=INSTANCE_ID,
        )
        raise HTTPException(409, "stored Slurm job id is invalid")
    expected_tag = f"benchtop:{job['id']}"
    cluster = str(job.get("scheduler_cluster") or "")
    if cluster and not SLURM_CLUSTER_RE.fullmatch(cluster):
        raise HTTPException(409, "stored Slurm cluster is invalid")
    if str(job.get("submission_tag") or "") != expected_tag:
        store.append_event(
            job["id"],
            "cancel_ownership_unproven",
            reason="stored submission tag does not prove OmicsANG ownership",
            actor=INSTANCE_ID,
        )
        raise HTTPException(409, "Slurm job ownership cannot be proven")
    if not ownership_observed:
        live_match = next(
            (
                item
                for item in _slurm_live_jobs(cluster)
                if str(item.get("job_id") or "") == scheduler_id
                and str(item.get("comment") or "") == expected_tag
            ),
            None,
        )
        if not live_match:
            store.append_event(
                job["id"],
                "cancel_ownership_unobserved",
                reason="squeue did not confirm the scheduler id and submission tag pair",
                actor=INSTANCE_ID,
            )
            raise HTTPException(409, "Slurm job ownership is not currently observable")
    dispatch_owner = f"{INSTANCE_ID}:cancel:{uuid.uuid4().hex[:8]}"
    dispatch_resource = f"cancel:slurm:{job['id']}"
    token = store.acquire_lease(dispatch_resource, dispatch_owner, ttl=30.0)
    if not token:
        raise HTTPException(409, "Slurm cancellation is already being dispatched")
    try:
        tools = _slurm_tools()
        if not tools.get("scancel"):
            store.append_event(
                job["id"],
                "cancel_executor_unavailable",
                reason="scancel is not available",
                actor=INSTANCE_ID,
            )
            raise HTTPException(400, "scancel is not available on this host")
        argv = [tools["scancel"]]
        if cluster:
            argv.extend(["-M", cluster])
        argv.extend(["--", scheduler_id])
        res = _run_slurm_cmd(argv, timeout=15)
        if not res.get("ok"):
            message = provenance.redact_text(
                str(res.get("output") or res.get("error") or "scancel failed")
            )[:4000]
            store.append_event(
                job["id"],
                "cancel_executor_failed",
                reason=message,
                actor=INSTANCE_ID,
            )
            raise HTTPException(500, message)
        job = store.transition_job(
            job["id"],
            "cancelling",
            expected_states=("cancel_requested",),
            event_type="cancel_sent",
            reason=f"scancel {scheduler_id}",
            actor=INSTANCE_ID,
            payload_update={"executor_state": "cancelling"},
        )
        tracked = store.slurm_job(intent_id=job["id"])
        if tracked:
            store.record_slurm_job(
                {
                    **tracked,
                    "status": "cancelling",
                    "cancel_requested": time.time(),
                }
            )
        return job, res
    finally:
        store.release_lease(dispatch_resource, dispatch_owner, token)


def _finalize_reconciled_slurm_job(job: dict) -> dict:
    if (
        not job.get("capsule_finalize_pending")
        or float(job.get("finalization_next_attempt") or 0) > time.time()
    ):
        return job
    resource = f"finalizer:slurm:{job['id']}"
    owner = f"{INSTANCE_ID}:slurm-finalizer:{uuid.uuid4().hex}"
    token = store.acquire_lease(
        resource,
        owner,
        ttl=FINALIZATION_LEASE_TTL,
        payload={
            **_controller_identity_payload(),
            "job_id": job["id"],
            "state_version": job.get("state_version"),
        },
    )
    if not token:
        return store.get_job(job["id"]) or job
    fence_stop = threading.Event()
    fence_lost = threading.Event()
    fence_payload = {
        **_controller_identity_payload(),
        "job_id": job["id"],
        "state_version": job.get("state_version"),
    }

    def renew_fence() -> None:
        if fence_lost.is_set():
            raise store.InvalidTransition("Slurm finalization lease was lost")
        renewed = store.acquire_lease(
            resource,
            owner,
            ttl=FINALIZATION_LEASE_TTL,
            payload=fence_payload,
        )
        if renewed != token:
            if renewed:
                store.release_lease(resource, owner, renewed)
            fence_lost.set()
            raise store.InvalidTransition("Slurm finalization lease was lost")

    def heartbeat_fence() -> None:
        interval = max(0.25, min(30.0, FINALIZATION_LEASE_TTL / 3.0))
        while not fence_stop.wait(interval):
            try:
                renew_fence()
            except Exception:
                fence_lost.set()
                return

    fence_thread = threading.Thread(
        target=heartbeat_fence,
        name=f"slurm-finalizer-{job['id']}",
        daemon=True,
    )
    fence_thread.start()
    try:
        # The caller's row may predate a concurrent scheduler transition or a
        # completed finalizer.  Re-read only after obtaining the per-job fence.
        current = store.get_job(job["id"])
        if not current:
            return job
        job = current
        state = str(job.get("state") or "")
        if (
            job.get("executor") != "slurm"
            or state not in store.TERMINAL_STATES
            or not job.get("capsule_finalize_pending")
            or float(job.get("finalization_next_attempt") or 0) > time.time()
        ):
            return job
        pipeline = pipelines.get(str(job.get("pipeline") or ""))
        capsule_id = str(job.get("capsule_id") or "")
        scheduler_id = str(job.get("scheduler_job_id") or "")
        if not pipeline:
            raise RuntimeError("pipeline is not currently discoverable")
        if not capsule_id:
            raise RuntimeError("Slurm job has no capsule id to finalize")
        if not SLURM_JOB_ID_RE.fullmatch(scheduler_id):
            raise RuntimeError("Slurm job has no safe scheduler id to finalize")
        tracked = store.slurm_job(intent_id=job["id"]) or {}
        logfile = str(tracked.get("output") or job.get("logfile") or "").replace(
            "%j", scheduler_id
        )
        raw_exit = str((job.get("scheduler_observation") or {}).get("exit_code") or "")
        try:
            exit_code = int(raw_exit.split(":", 1)[0]) if raw_exit else None
        except ValueError:
            exit_code = None
        session_status = {
            "succeeded": "exited",
            "failed": "failed",
            "cancelled": "killed",
        }.get(state, "failed")
        renew_fence()
        capsule = provenance.finalize_slurm(
            pipeline,
            capsule_id,
            status=session_status,
            job_id=scheduler_id,
            exit_code=exit_code,
            logfile=logfile,
        )
        renew_fence()
        store.record_capsule_ref(
            provenance.capsule_reference(
                pipeline,
                capsule,
                job_id=job["id"],
                status=session_status,
            )
        )
        # History and capsule-reference writes are idempotent upserts.  Perform
        # every side effect before the fenced checkpoint; a lost/stale worker
        # must never clear pending finalization after only a partial write.
        renew_fence()
        store.record(
            {
                "id": f"slurm-{scheduler_id}",
                "kind": "slurm",
                "pipeline": job.get("pipeline", ""),
                "title": job.get("title", tracked.get("title", "Slurm run")),
                "status": session_status,
                "exit_code": exit_code,
                "command": job.get("command", tracked.get("command", "")),
                "started": job.get("started"),
                "ended": job.get("ended") or time.time(),
                "logfile": logfile,
                "slurm_job_id": scheduler_id,
                "plan_digest": job.get("plan_digest", ""),
                "capsule_id": capsule_id,
                "capsule_fingerprint": capsule.get("fingerprint", ""),
            }
        )
        job = store.update_job(
            job["id"],
            payload_update={
                "capsule_finalize_pending": False,
                "capsule_fingerprint": capsule.get("fingerprint", ""),
                "finalization_attempt": 0,
                "finalization_next_attempt": 0,
            },
            expected_states=(state,),
            expected_version=int(job.get("state_version") or 0),
            expected_payload={"capsule_finalize_pending": True},
            fence_resource=resource,
            fence_owner=owner,
            fence_token=token,
            event_type="slurm_capsule_finalized",
            reason=f"scheduler terminal state {state}",
            actor=INSTANCE_ID,
            last_error="",
        )
    except Exception as exc:
        error = provenance.redact_text(str(exc))
        current = store.get_job(job["id"]) or job
        state = str(current.get("state") or "")
        if (
            current.get("executor") != "slurm"
            or state not in store.TERMINAL_STATES
            or not current.get("capsule_finalize_pending")
        ):
            return current
        attempt = int(current.get("finalization_attempt") or 0) + 1
        try:
            job = store.update_job(
                current["id"],
                payload_update={
                    "capsule_finalize_pending": True,
                    "finalization_attempt": attempt,
                    "finalization_next_attempt": time.time()
                    + min(
                        3600.0,
                        30.0 * (2 ** min(7, attempt - 1)),
                    ),
                },
                expected_states=(state,),
                expected_version=int(current.get("state_version") or 0),
                expected_payload={"capsule_finalize_pending": True},
                fence_resource=resource,
                fence_owner=owner,
                fence_token=token,
                event_type="slurm_capsule_finalize_failed",
                reason=error,
                actor=INSTANCE_ID,
                last_error=error,
            )
        except Exception:
            job = store.get_job(current["id"]) or current
        return job
    finally:
        fence_stop.set()
        fence_thread.join(timeout=1.0)
        store.release_lease(resource, owner, token)
    return job


def _restore_slurm_binding_from_tracking(job: dict) -> dict:
    """Recover an accepted scheduler id across the post-sbatch crash window."""
    if job.get("scheduler_job_id"):
        return job
    tracked = store.slurm_job(intent_id=str(job.get("id") or "")) or {}
    scheduler_id = str(tracked.get("job_id") or "")
    cluster = str(tracked.get("scheduler_cluster") or "")
    expected_tag = f"benchtop:{job.get('id', '')}"
    if (
        not SLURM_JOB_ID_RE.fullmatch(scheduler_id)
        or (cluster and not SLURM_CLUSTER_RE.fullmatch(cluster))
        or str(job.get("submission_tag") or "") != expected_tag
        or str(tracked.get("submission_tag") or "") != expected_tag
    ):
        return job
    try:
        state = str(job.get("state") or "")
        if state in {"submitting", "submission_unknown"}:
            return store.transition_job(
                job["id"],
                "submitted",
                expected_states=(state,),
                event_type="scheduler_binding_recovered",
                reason=f"recovered accepted sbatch id {scheduler_id} from durable tracking",
                actor=INSTANCE_ID,
                scheduler_job_id=scheduler_id,
                payload_update={
                    "executor_state": "submitted",
                    "scheduler_cluster": cluster,
                    "slurm_job": tracked,
                },
            )
        return store.update_job(
            job["id"],
            scheduler_job_id=scheduler_id,
            payload_update={"scheduler_cluster": cluster, "slurm_job": tracked},
            event_type="scheduler_binding_recovered",
            reason=f"recovered accepted sbatch id {scheduler_id} from durable tracking",
            actor=INSTANCE_ID,
        )
    except (store.InvalidTransition, KeyError):
        return store.get_job(str(job.get("id") or "")) or job


def _reconcile_slurm_jobs(live: list[dict] | None = None) -> list[dict]:
    """Persist scheduler observations; never submit or infer success from absence."""
    durable = store.list_jobs(
        executor="slurm",
        states=(
            "submitting",
            "submission_unknown",
            "submitted",
            "pending",
            "running",
            "cancel_requested",
            "cancelling",
        ),
        limit=1000,
    )
    durable = [_restore_slurm_binding_from_tracking(job) for job in durable]
    if live is None:
        clusters = {str(job.get("scheduler_cluster") or "") for job in durable}
        live = []
        for cluster in sorted(clusters):
            live.extend(_slurm_live_jobs(cluster))
    live_by_key = {
        (str(item.get("cluster") or ""), str(item.get("job_id") or "")): item
        for item in live
        if SLURM_JOB_ID_RE.fullmatch(str(item.get("job_id") or ""))
    }
    live_by_tag = {
        (str(item.get("cluster") or ""), str(item.get("comment") or "")): item
        for item in live
        if str(item.get("comment") or "").startswith("benchtop:")
    }
    missing_by_cluster: dict[str, list[str]] = {}
    for job in durable:
        scheduler_id = str(job.get("scheduler_job_id") or "")
        cluster = str(job.get("scheduler_cluster") or "")
        expected_tag = str(job.get("submission_tag") or "")
        observed = live_by_key.get((cluster, scheduler_id))
        if (
            scheduler_id
            and SLURM_JOB_ID_RE.fullmatch(scheduler_id)
            and expected_tag == f"benchtop:{job['id']}"
            and (not observed or str(observed.get("comment") or "") != expected_tag)
        ):
            missing_by_cluster.setdefault(cluster, []).append(scheduler_id)
    accounted: dict[tuple[str, str], dict] = {}
    for cluster, scheduler_ids in missing_by_cluster.items():
        for scheduler_id, row in _slurm_accounting(
            scheduler_ids,
            cluster=cluster,
        ).items():
            accounted[(cluster, scheduler_id)] = row
    reconciled = []
    for job in durable:
        scheduler_id = str(job.get("scheduler_job_id") or "")
        cluster = str(job.get("scheduler_cluster") or "")
        expected_tag = str(job.get("submission_tag") or "")
        ownership_tag_valid = expected_tag == f"benchtop:{job['id']}"
        observation = live_by_key.get((cluster, scheduler_id)) if scheduler_id else None
        if observation and (
            not ownership_tag_valid
            or str(observation.get("comment") or "") != expected_tag
        ):
            try:
                job = _scheduler_event_once(
                    job,
                    "scheduler_identity_mismatch",
                    reason="scheduler id exists but its ownership tag does not match",
                    fingerprint=f"{scheduler_id}:{observation.get('comment', '')}",
                )
            except Exception:
                pass
            observation = None
        if not observation and ownership_tag_valid and not scheduler_id:
            observation = live_by_tag.get((cluster, expected_tag))
            if observation and not scheduler_id:
                scheduler_id = str(observation.get("job_id") or "")
                cluster = str(observation.get("cluster") or cluster)
                if not SLURM_JOB_ID_RE.fullmatch(scheduler_id):
                    try:
                        job = _scheduler_event_once(
                            job,
                            "invalid_scheduler_job_id",
                            reason="scheduler tag resolved to an unsafe executor reference",
                            fingerprint=scheduler_id,
                        )
                    except Exception:
                        pass
                    scheduler_id = ""
                    observation = None
            if observation and not job.get("scheduler_job_id"):
                try:
                    if job.get("state") == "cancel_requested":
                        job = store.update_job(
                            job["id"],
                            scheduler_job_id=scheduler_id,
                            payload_update={"scheduler_cluster": cluster},
                            event_type="scheduler_tag_bound",
                            reason=f"reconciled cancelled intent to job {scheduler_id}",
                            actor=INSTANCE_ID,
                        )
                        try:
                            job, _ = _dispatch_slurm_cancel(
                                job,
                                ownership_observed=True,
                            )
                        except (HTTPException, RuntimeError):
                            job = store.get_job(job["id"]) or job
                    else:
                        job = store.transition_job(
                            job["id"],
                            "submitted",
                            expected_states=("submitting", "submission_unknown"),
                            reason=f"reconciled scheduler tag to job {scheduler_id}",
                            actor=INSTANCE_ID,
                            scheduler_job_id=scheduler_id,
                            payload_update={"scheduler_cluster": cluster},
                        )
                except (store.InvalidTransition, HTTPException):
                    job = store.get_job(job["id"]) or job
        if job.get("state") == "cancel_requested" and scheduler_id and observation:
            try:
                job, _ = _dispatch_slurm_cancel(job, ownership_observed=True)
            except (HTTPException, RuntimeError):
                job = store.get_job(job["id"]) or job
        accounting = accounted.get((cluster, scheduler_id))
        if accounting and (
            not ownership_tag_valid
            or str(accounting.get("comment") or "") != expected_tag
        ):
            try:
                job = _scheduler_event_once(
                    job,
                    "scheduler_accounting_identity_mismatch",
                    reason="sacct record ownership tag does not match",
                    fingerprint=f"{scheduler_id}:{accounting.get('comment', '')}",
                )
            except Exception:
                pass
            accounting = None
        raw = observation or accounting
        target = _canonical_slurm_state(str((raw or {}).get("state") or ""))
        if target and target != job.get("state"):
            try:
                requeued = target == "pending" and job.get("state") == "running"
                scheduler_attempt = int(job.get("scheduler_attempt") or 1)
                if requeued:
                    scheduler_attempt += 1
                job = store.transition_job(
                    job["id"],
                    target,
                    event_type="scheduler_requeued" if requeued else "state_changed",
                    reason=f"scheduler reported {(raw or {}).get('state', '')}",
                    actor=INSTANCE_ID,
                    scheduler_job_id=scheduler_id,
                    payload_update={
                        "executor_state": str((raw or {}).get("state") or ""),
                        "scheduler_observation": raw or {},
                        "scheduler_attempt": scheduler_attempt,
                        "scheduler_cluster": cluster,
                        "capsule_finalize_pending": target in store.TERMINAL_STATES,
                    },
                )
                tracked = store.slurm_job(intent_id=job["id"])
                if tracked:
                    store.record_slurm_job(
                        {
                            **tracked,
                            "job_id": scheduler_id,
                            "status": target,
                            "scheduler_state": str((raw or {}).get("state") or ""),
                            "reconciled": time.time(),
                        }
                    )
            except store.InvalidTransition:
                job = store.get_job(job["id"]) or job
        elif scheduler_id and not raw:
            try:
                job = _scheduler_event_once(
                    job,
                    "scheduler_observation_missing",
                    reason="job absent from squeue and no terminal sacct record",
                    fingerprint=scheduler_id,
                )
            except Exception:
                pass
        reconciled.append(job)
    pending = store.list_jobs(
        executor="slurm",
        states=tuple(store.TERMINAL_STATES),
        limit=1000,
        oldest_first=True,
    )
    processed = 0
    now = time.time()
    for job in pending:
        if (
            not job.get("capsule_finalize_pending")
            or float(job.get("finalization_next_attempt") or 0) > now
        ):
            continue
        if processed >= FINALIZATION_BATCH_LIMIT:
            break
        processed += 1
        _finalize_reconciled_slurm_job(job)
    return reconciled


def _slurm_partitions() -> list[dict]:
    tools = _slurm_tools()
    if not tools.get("sinfo"):
        return []
    fmt = "%P|%a|%D|%C|%m|%G"
    res = _run_slurm_cmd([tools["sinfo"], "-h", "-o", fmt], timeout=12)
    if not res["ok"]:
        return []
    rows = []
    # Scheduler warnings belong on stderr and must never be interpreted as
    # partition records merely because they contain pipe delimiters.
    for line in str(res.get("stdout") or "").splitlines():
        parts = line.split("|", 5)
        if len(parts) != 6:
            continue
        rows.append(
            {
                "partition": parts[0].strip().rstrip("*"),
                "available": parts[1].strip(),
                "nodes": parts[2].strip(),
                "cpus": parts[3].strip(),
                "memory": parts[4].strip(),
                "gres": parts[5].strip(),
            }
        )
    return rows


def _slurm_replay_response(job: dict, plan: runplan.RunPlan | None = None) -> dict:
    plan_digest = plan.digest if plan is not None else str(job.get("plan_digest") or "")
    tracked = store.slurm_job(intent_id=str(job["id"])) or dict(
        job.get("slurm_job") or {}
    )
    if not tracked:
        tracked = {
            "id": job["id"],
            "job_id": job.get("scheduler_job_id") or "",
            "pipeline": job.get("pipeline") or "",
            "status": job.get("state") or "created",
            "plan_digest": job.get("plan_digest") or plan_digest,
        }
    tracked_public = _public_slurm_job(tracked)
    return {
        "ok": job.get("state") not in {"failed", "blocked", "cancelled"},
        "job": tracked_public,
        "durable_job": _public_job(job),
        "sbatch_output": provenance.redact_text(str(tracked.get("sbatch_output", ""))),
        "plan_digest": plan_digest,
        "plan_schema_version": runplan.SCHEMA_VERSION,
        "idempotency_key": job.get("idempotency_key", ""),
        "idempotent_replay": True,
        "replayed": True,
        "persistence_degraded": job.get("state") == "submission_unknown",
        "warnings": (
            ["submission outcome is unknown; OmicsANG will not resubmit this intent"]
            if job.get("state") == "submission_unknown"
            else []
        ),
    }


def _mark_slurm_capsule_failed(
    p: pipelines.Pipeline, capsule_id: str, error: str
) -> None:
    capsule = provenance.mark_slurm_failed(p, capsule_id, error)
    store.record_capsule_ref(
        provenance.capsule_reference(
            p,
            capsule,
            job_id=capsule_id,
            status="submission-failed",
        )
    )


def _submit_slurm_run(
    p: pipelines.Pipeline,
    body: SlurmSubmitRequest,
    study_report: dict,
    override_valid: bool,
    *,
    plan: runplan.RunPlan | None = None,
    resolved_env: str = "",
) -> dict:
    tools = _slurm_tools()
    sbatch = tools.get("sbatch")
    if not sbatch:
        raise HTTPException(400, "sbatch is not available on this host")
    if plan is None:
        resolved_env = body.env or pipelines.resolve_env(p) or ""
        argv, _ = pipelines.build_snakemake_argv(
            p,
            target=body.target,
            configfile=body.configfile,
            profile=body.profile,
            cores=body.cores,
            dryrun=body.dryrun,
            use_conda=body.use_conda,
            extra=body.extra,
            env=resolved_env,
            resolve_default_env=False,
        )
        plan = runplan.build(
            p,
            launch={
                "target": body.target,
                "configfile": body.configfile,
                "profile": body.profile,
                "cores": body.cores,
                "dryrun": body.dryrun,
                "use_conda": body.use_conda,
                "env": resolved_env,
                "study_override": override_valid,
            },
            argv=argv,
            study_report=study_report,
            resolved_env=resolved_env,
            executor="slurm",
            scheduler=_slurm_plan_resources(p, body),
        )
    else:
        resolved_env = str(
            plan.payload.get("environment", {}).get("driver", {}).get("name", "")
        )
    plan_payload = plan.payload
    argv = plan.argv
    human = shlex.join(argv)
    public_human = runplan.redact_command(argv)
    plan_resources = plan_payload.get("resources", {})
    slurm_resources = plan_resources.get("slurm") or {}
    plan_cores = max(1, int(plan_resources.get("cores", body.cores)))
    request_digest = _submission_request_digest(body, p.name, "slurm")
    idempotency_key = _submission_key(body)
    existing = store.job_by_idempotency(idempotency_key)
    if existing:
        _assert_idempotent_contract(
            existing,
            pipeline=p.name,
            executor="slurm",
            plan_digest=plan.digest,
            request_digest=request_digest,
        )
        return _slurm_replay_response(existing, plan)
    settings.ensure_dirs()
    slurm_dir = settings.STATE_DIR / "slurm"
    slurm_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    slurm_dir.chmod(0o700)
    stamp = uuid.uuid4().hex
    job_name = str(slurm_resources.get("job_name") or f"bt-{p.name}"[:32])
    script = slurm_dir / f"{stamp}.sbatch"
    plan_file = slurm_dir / f"{stamp}.runplan.json"
    output = slurm_dir / f"{stamp}-%j.out"
    error = slurm_dir / f"{stamp}-%j.err"
    durable_job, created = store.create_job(
        job_id=stamp,
        kind="run",
        executor="slurm",
        pipeline=p.name,
        state="created",
        idempotency_key=idempotency_key,
        plan_digest=plan.digest,
        payload={
            "title": job_name,
            "cores": plan_cores,
            "dryrun": body.dryrun,
            "executor_state": "created",
            "command": public_human,
            "submission_tag": f"benchtop:{stamp}",
            "request_digest": request_digest,
        },
        private={"argv": argv, "cwd": plan.cwd},
        actor=INSTANCE_ID,
    )
    if not created:
        _assert_idempotent_contract(
            durable_job,
            pipeline=p.name,
            executor="slurm",
            plan_digest=plan.digest,
            request_digest=request_digest,
        )
        return _slurm_replay_response(durable_job, plan)
    try:
        _write_private_text(
            plan_file,
            json.dumps(plan.record(), indent=2, sort_keys=True, ensure_ascii=True),
        )
    except OSError as exc:
        try:
            store.transition_job(
                stamp,
                "failed",
                expected_states=("created",),
                reason=f"could not persist private RunPlan: {exc}",
                actor=INSTANCE_ID,
                last_error=str(exc),
            )
        except Exception:
            pass
        raise HTTPException(
            500, f"could not persist private RunPlan record: {exc}"
        ) from exc
    launch_meta = {
        "pipeline": p.name,
        "dryrun": body.dryrun,
        "human": public_human,
        "cores": plan_cores,
        "target": body.target,
        "configfile": body.configfile,
        "profile": body.profile,
        "use_conda": body.use_conda,
        "extra": runplan.redact_command_text(body.extra),
        "env": resolved_env,
        "study_sheet": (study_report.get("selected") or {}).get("path", ""),
        "study_roles": study_report.get("roles", body.study_roles),
        "study_gate": study_report.get("gate", "unknown"),
        "study_fingerprint": study_report.get("fingerprint", ""),
        "study_override": override_valid,
        "study_override_fingerprint": (
            study_report.get("fingerprint", "") if override_valid else ""
        ),
        "plan_schema_version": runplan.SCHEMA_VERSION,
        "plan_digest": plan.digest,
        "plan_record": str(plan_file),
    }
    try:
        capsule = provenance.capture(p, stamp, launch_meta, run_plan=plan)
        store.record_capsule_ref(
            provenance.capsule_reference(
                p,
                capsule,
                job_id=stamp,
                status="preparing",
            )
        )
    except Exception as exc:
        try:
            store.transition_job(
                stamp,
                "failed",
                expected_states=("created",),
                reason=f"capsule preparation failed: {exc}",
                actor=INSTANCE_ID,
                last_error=str(exc),
            )
        except Exception:
            pass
        raise HTTPException(
            500,
            f"Run capsule preparation failed; Slurm job was not submitted: {exc}",
        ) from exc
    capsule_id = capsule["id"]
    capsule_fingerprint = capsule.get("fingerprint", "")
    capsule_error = ""
    lines = [
        "#!/usr/bin/env bash",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --cpus-per-task={plan_cores}",
        f"#SBATCH --output={output}",
        f"#SBATCH --error={error}",
        f"#SBATCH --comment=benchtop:{stamp}",
        "#SBATCH --export=NIL",
    ]
    for flag, key in (
        ("--partition", "partition"),
        ("--account", "account"),
        ("--qos", "qos"),
        ("--time", "time_limit"),
        ("--mem", "mem"),
        ("--gres", "gres"),
    ):
        value = str(slurm_resources.get(key) or "")
        if value:
            lines.append(f"#SBATCH {flag}={value}")
    lines.extend(
        [
            "umask 077",
            "set -euo pipefail",
            *[
                f"export {key}={shlex.quote(value)}"
                for key, value in _slurm_safe_environment().items()
            ],
            f"cd {shlex.quote(plan.cwd)}",
            (
                f"PYTHONPATH={shlex.quote(str(Path(__file__).resolve().parents[1]))} "
                + shlex.join(
                    [
                        sys.executable,
                        "-m",
                        "benchtop.runplan",
                        "verify",
                        str(plan_file),
                    ]
                )
            ),
            f"exec {human}",
            "",
        ]
    )
    try:
        _write_private_text(script, "\n".join(lines))
    except OSError as exc:
        try:
            _mark_slurm_capsule_failed(
                p, stamp, f"could not write sbatch script: {exc}"
            )
        except Exception:
            pass
        try:
            store.transition_job(
                stamp,
                "failed",
                expected_states=("created",),
                reason=f"could not write sbatch script: {exc}",
                actor=INSTANCE_ID,
                last_error=str(exc),
            )
        except Exception:
            pass
        raise HTTPException(500, f"could not write sbatch script: {exc}") from exc
    job_base = {
        "id": stamp,
        "job_id": "",
        "pipeline": p.name,
        "title": job_name,
        "status": "submitting",
        "created": time.time(),
        "command": public_human,
        "script": str(script),
        "plan_record": str(plan_file),
        "output": str(output),
        "error": str(error),
        "cores": plan_cores,
        "dryrun": body.dryrun,
        "study_gate": study_report.get("gate", "unknown"),
        "study_fingerprint": study_report.get("fingerprint", ""),
        "study_override": override_valid,
        "plan_schema_version": runplan.SCHEMA_VERSION,
        "plan_digest": plan.digest,
        "capsule_id": capsule_id,
        "capsule_fingerprint": capsule_fingerprint,
        "capsule_error": capsule_error,
        "idempotency_key": idempotency_key,
        "submission_tag": f"benchtop:{stamp}",
    }
    try:
        store.transition_job(
            stamp,
            "submitting",
            expected_states=("created",),
            reason="submission intent persisted before sbatch",
            actor=INSTANCE_ID,
            payload_update={
                "executor_state": "submitting",
                "slurm_job": job_base,
                "capsule_id": capsule_id,
                "logfile": str(output),
            },
            private_update={
                "plan_record": str(plan_file),
                "script": str(script),
            },
        )
        store.record_slurm_job(job_base)
    except Exception as exc:
        try:
            _mark_slurm_capsule_failed(
                p, stamp, f"could not persist submission intent: {exc}"
            )
        except Exception:
            pass
        try:
            store.transition_job(
                stamp,
                "failed",
                reason=f"submission intent persistence failed: {exc}",
                actor=INSTANCE_ID,
                last_error=str(exc),
            )
        except Exception:
            pass
        raise HTTPException(
            500,
            f"could not persist Slurm submission intent; job was not submitted: {exc}",
        ) from exc
    res = _run_slurm_cmd(
        [sbatch, "--parsable", str(script)],
        timeout=20,
        umask=0o077,
    )
    if not res["ok"]:
        failure = provenance.redact_text(
            str(res.get("output") or res.get("error") or "sbatch failed")
        )
        if res.get("ambiguous"):
            unknown_job = {
                **job_base,
                "status": "submission-unknown",
                "submission_error": failure,
            }
            try:
                store.record_slurm_job(unknown_job)
                store.transition_job(
                    stamp,
                    "submission_unknown",
                    expected_states=("submitting",),
                    reason=failure,
                    actor=INSTANCE_ID,
                    last_error=failure,
                    payload_update={
                        "executor_state": "submission-unknown",
                        "slurm_job": unknown_job,
                    },
                )
            except Exception:
                pass
            raise HTTPException(
                504,
                detail={
                    "message": (
                        "sbatch timed out and may have accepted the job; this intent will not "
                        "be resubmitted until scheduler reconciliation proves the outcome"
                    ),
                    "job_id": stamp,
                    "idempotency_key": idempotency_key,
                    "state": "submission_unknown",
                },
            )
        try:
            _mark_slurm_capsule_failed(
                p,
                stamp,
                failure,
            )
        except Exception:
            pass
        try:
            store.record_slurm_job(
                {
                    **job_base,
                    "status": "submission-failed",
                    "submission_error": failure,
                }
            )
        except Exception:
            pass
        try:
            store.transition_job(
                stamp,
                "failed",
                expected_states=("submitting",),
                reason=failure,
                actor=INSTANCE_ID,
                last_error=failure,
                payload_update={"executor_state": "submission-failed"},
            )
        except Exception:
            pass
        raise HTTPException(500, failure)
    stdout_lines = [
        line.strip()
        for line in str(res.get("stdout") or "").splitlines()
        if line.strip()
    ]
    parsable = stdout_lines[0].split(";", 1) if len(stdout_lines) == 1 else []
    job_id = parsable[0].strip() if parsable else ""
    scheduler_cluster = parsable[1].strip() if len(parsable) == 2 else ""
    valid_result = bool(
        len(stdout_lines) == 1
        and SLURM_JOB_ID_RE.fullmatch(job_id)
        and (not scheduler_cluster or SLURM_CLUSTER_RE.fullmatch(scheduler_cluster))
    )
    if not valid_result:
        failure = (
            "sbatch returned success without one safe --parsable job identifier; "
            "scheduler acceptance is ambiguous"
        )
        unknown_job = {
            **job_base,
            "status": "submission-unknown",
            "submission_error": failure,
            "sbatch_output": provenance.redact_text(str(res.get("output") or "")),
        }
        try:
            store.record_slurm_job(unknown_job)
            store.transition_job(
                stamp,
                "submission_unknown",
                expected_states=("submitting",),
                reason=failure,
                actor=INSTANCE_ID,
                last_error=failure,
                payload_update={
                    "executor_state": "submission-unknown",
                    "slurm_job": unknown_job,
                },
            )
        except Exception:
            pass
        raise HTTPException(
            502,
            detail={
                "message": failure,
                "job_id": stamp,
                "idempotency_key": idempotency_key,
                "state": "submission_unknown",
            },
        )
    accepted_output = provenance.redact_text(
        job_id + (f";{scheduler_cluster}" if scheduler_cluster else "")
    )
    persistence_errors: list[str] = []
    submitted_job = {
        **job_base,
        "job_id": job_id,
        "status": "submitted",
        "capsule_fingerprint": capsule_fingerprint,
        "capsule_error": capsule_error,
        "sbatch_output": accepted_output,
        "scheduler_cluster": scheduler_cluster,
    }
    # The tracking row is the recovery journal for the narrow interval before
    # the authoritative job transition commits.  Both bindings are attempted
    # before capsule mutation, so a process crash at that boundary can recover
    # the scheduler id and cluster through sacct after the job leaves squeue.
    tracking_persisted = False
    try:
        job = store.record_slurm_job(submitted_job)
        tracking_persisted = True
    except Exception as exc:
        job = submitted_job
        persistence_errors.append(
            provenance.redact_text(f"Slurm job tracking update failed: {exc}")
        )
    try:
        durable_job = store.transition_job(
            stamp,
            "submitted",
            expected_states=("submitting",),
            reason=f"sbatch accepted job {job_id}",
            actor=INSTANCE_ID,
            scheduler_job_id=job_id,
            started=time.time(),
            payload_update={
                "executor_state": "submitted",
                "slurm_job": submitted_job,
                "capsule_id": capsule_id,
                "sbatch_output": accepted_output,
                "scheduler_cluster": scheduler_cluster,
            },
        )
    except Exception as exc:
        try:
            durable_job = store.get_job(stamp) or durable_job
        except Exception:
            pass
        binding_error = provenance.redact_text(str(exc))
        persistence_errors.append(
            provenance.redact_text(
                "durable submission binding failed; intent remains submission-unknown: "
                + binding_error
            )
        )
        try:
            durable_job = store.transition_job(
                stamp,
                "submission_unknown",
                expected_states=("submitting",),
                reason=f"sbatch returned {job_id} but durable binding failed: {binding_error}",
                actor=INSTANCE_ID,
                last_error=binding_error,
                scheduler_job_id=job_id,
                payload_update={
                    "executor_state": "submission-unknown",
                    "slurm_job": submitted_job,
                    "capsule_id": capsule_id,
                    "sbatch_output": accepted_output,
                    "scheduler_cluster": scheduler_cluster,
                },
            )
        except Exception as fallback_exc:
            persistence_errors.append(
                provenance.redact_text(
                    f"submission-unknown binding fallback failed: {fallback_exc}"
                )
            )
    durable_binding_persisted = bool(
        str(durable_job.get("scheduler_job_id") or "") == job_id
        and str(durable_job.get("scheduler_cluster") or "") == scheduler_cluster
    )
    if not tracking_persisted and not durable_binding_persisted:
        raise HTTPException(
            500,
            detail={
                "message": (
                    "sbatch accepted the job, but OmicsANG could not durably bind it; "
                    "do not resubmit this intent"
                ),
                "job_id": stamp,
                "scheduler_job_id": job_id,
                "scheduler_cluster": scheduler_cluster,
                "state": str(durable_job.get("state") or "submitting"),
            },
        )
    try:
        capsule = provenance.mark_slurm_submitted(p, stamp, job_id)
        capsule_fingerprint = capsule.get("fingerprint", "")
        store.record_capsule_ref(
            provenance.capsule_reference(
                p,
                capsule,
                job_id=stamp,
                status="submitted",
            )
        )
    except Exception as exc:
        capsule_error = provenance.redact_text(str(exc))
        persistence_errors.append(
            provenance.redact_text(f"capsule submission update failed: {exc}")
        )
    submitted_job = {
        **submitted_job,
        "capsule_fingerprint": capsule_fingerprint,
        "capsule_error": capsule_error,
    }
    try:
        job = store.record_slurm_job(submitted_job)
    except Exception as exc:
        persistence_errors.append(
            provenance.redact_text(f"Slurm capsule tracking update failed: {exc}")
        )
    try:
        durable_job = store.update_job(
            stamp,
            payload_update={
                "slurm_job": submitted_job,
                "capsule_fingerprint": capsule_fingerprint,
                "capsule_error": capsule_error,
            },
            event_type="",
            actor=INSTANCE_ID,
        )
    except Exception as exc:
        persistence_errors.append(
            provenance.redact_text(f"durable capsule tracking update failed: {exc}")
        )
    history_entry = {
        "id": f"slurm-{job_id}",
        "kind": "slurm",
        "pipeline": p.name,
        "title": job_name,
        "status": "submitted",
        "exit_code": None,
        "command": public_human,
        "started": time.time(),
        "ended": None,
        "logfile": str(output),
        "slurm_job_id": job_id,
        "dryrun": body.dryrun,
        "study_gate": study_report.get("gate", "unknown"),
        "study_fingerprint": study_report.get("fingerprint", ""),
        "study_override": override_valid,
        "plan_schema_version": runplan.SCHEMA_VERSION,
        "plan_digest": plan.digest,
        "capsule_id": capsule_id,
        "capsule_fingerprint": capsule_fingerprint,
        "capsule_error": capsule_error,
    }
    try:
        store.record(history_entry)
    except Exception as exc:
        persistence_errors.append(
            provenance.redact_text(f"run history update failed: {exc}")
        )
    return {
        "ok": True,
        "job": _public_slurm_job(job),
        "durable_job": _public_job(durable_job),
        "sbatch_output": accepted_output,
        "plan_digest": plan.digest,
        "plan_schema_version": runplan.SCHEMA_VERSION,
        "persistence_degraded": bool(persistence_errors),
        "warnings": persistence_errors,
        "idempotency_key": idempotency_key,
        "idempotent_replay": False,
        "replayed": False,
    }


# ---- pipeline endpoints --------------------------------------------------
@app.get("/api/pipelines")
def list_pipelines():
    return [p.to_dict() for p in pipelines.discover()]


@app.get("/api/health")
def fleet_health(diagnostics: bool = True):
    return {
        "generated": time.time(),
        "run_queue": _run_queue_state(),
        "slurm": slurm_status(),
        "pipelines": [
            _health_for_pipeline(p, diagnostics) for p in pipelines.discover()
        ],
    }


@app.get("/api/pipelines/{name}")
def pipeline_detail(name: str):
    p = _require_pipeline(name)
    d = p.to_dict()
    d["git"] = git_ops.status(p.path)
    d["worktrees"] = git_ops.list_worktrees(p.path)
    d["history"] = [
        _public_history_entry(entry) for entry in store.history(name, limit=25)
    ]
    d["available_envs"] = pipelines.real_envs()
    d["snakemake_envs"] = pipelines.snakemake_envs()
    d["env_resolved"] = pipelines.resolve_env(p)
    d["run_templates"] = store.run_templates(name)
    d["slurm_jobs"] = [
        _public_slurm_job(job) for job in store.slurm_jobs(name, limit=20)
    ]
    return d


@app.get("/api/pipelines/{name}/health")
def pipeline_health(name: str, diagnostics: bool = True):
    return _health_for_pipeline(_require_pipeline(name), diagnostics)


@app.get("/api/pipelines/{name}/results")
def pipeline_results(name: str):
    return results.gather(_require_pipeline(name))


@app.post("/api/pipelines/{name}/results/directories/search")
def pipeline_result_directories_search(
    name: str,
    body: ResultDirectorySearchRequest,
):
    try:
        return results.search_directories(
            _require_pipeline(name),
            query=body.query,
            root_id=body.root_id,
            limit=body.limit,
        )
    except results.ResultPathError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/pipelines/{name}/results/preview")
def pipeline_result_preview(name: str, body: ResultDirectoryRequest):
    try:
        return results.preview_directory(
            _require_pipeline(name),
            body.path,
            limit=body.limit,
        )
    except results.ResultPathError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/pipelines/{name}/results/attach")
def pipeline_result_attach(name: str, body: ResultDirectoryRequest):
    pipeline = _require_pipeline(name)
    try:
        attachment, created = results.attach(pipeline, body.path)
    except results.ResultPathError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "created": created,
        "attachment": attachment,
        "attachments": results.attachments(pipeline),
    }


@app.post("/api/pipelines/{name}/results/detach")
def pipeline_result_detach(name: str, body: ResultDirectoryRequest):
    pipeline = _require_pipeline(name)
    try:
        attachments, removed = results.detach(pipeline, body.path)
    except results.ResultPathError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"removed": removed, "attachments": attachments}


@app.get("/api/pipelines/{name}/results/file")
def pipeline_result_file(name: str, source: str, path: str):
    try:
        target = results.resolve_source_file(
            _require_pipeline(name),
            source,
            path,
        )
    except results.ResultPathError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    # FileResponse streams every authorized artifact, including large CSV/TSV
    # outputs, instead of first materializing text files in server memory.
    return _authorized_file_response(target)


@app.get("/api/pipelines/{name}/qc")
def pipeline_qc(name: str):
    return qc.gather(_require_pipeline(name))


@app.get("/api/pipelines/{name}/study")
def pipeline_study(name: str, configfile: str = "", sheet: str = ""):
    p = _require_pipeline(name)
    try:
        report = study.audit(p, configfile=configfile, sheet=sheet)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    report["generated"] = time.time()
    return report


@app.post("/api/pipelines/{name}/study")
def pipeline_study_analyze(name: str, body: StudyAuditRequest):
    p = _require_pipeline(name)
    try:
        report = study.audit(
            p,
            configfile=body.configfile,
            sheet=body.sheet,
            roles=body.roles,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    report["generated"] = time.time()
    return report


@app.get("/api/pipelines/{name}/capsules")
def pipeline_capsules(name: str, limit: int = 50):
    p = _require_pipeline(name)
    return {"capsules": provenance.list_capsules(p, limit=limit)}


@app.get("/api/pipelines/{name}/capsules/compare")
def pipeline_capsules_compare(name: str, left: str, right: str = "current"):
    p = _require_pipeline(name)
    try:
        return provenance.compare(p, left, right)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/pipelines/{name}/capsules/current")
def pipeline_capsule_current(name: str, configfile: str = ""):
    p = _require_pipeline(name)
    return {
        "capsule": provenance.current(
            p,
            {
                "configfile": configfile,
                "env": pipelines.resolve_env(p) or "",
            },
        )
    }


@app.get("/api/pipelines/{name}/capsules/{capsule_id}")
def pipeline_capsule(name: str, capsule_id: str):
    p = _require_pipeline(name)
    try:
        return {"capsule": provenance.load(p, capsule_id)}
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/pipelines/{name}/git")
def pipeline_git(name: str):
    p = _require_pipeline(name)
    return {"status": git_ops.status(p.path), "diff": git_ops.diff(p.path)}


@app.get("/api/pipelines/{name}/github")
def pipeline_github(name: str):
    p = _require_pipeline(name)
    git = git_ops.status(p.path)
    return {
        "git": git,
        "github_slug": git_ops.github_slug(git.get("remote")),
        "gh_available": bool(shutil.which("gh")),
        "gh_authenticated": False,
        "gh_error": git_ops.OUTBOUND_DISABLED,
        "gh_user": "",
        "repo": {},
        "repo_error": "",
        "branches": {"local": [], "remote": []},
        "prs": [],
        "outbound_disabled": True,
    }


@app.post("/api/pipelines/{name}/github/fetch")
def pipeline_github_fetch(name: str):
    _require_pipeline(name)
    raise HTTPException(403, git_ops.OUTBOUND_DISABLED)


@app.post("/api/pipelines/{name}/github/pull")
def pipeline_github_pull(name: str):
    _require_pipeline(name)
    raise HTTPException(403, git_ops.OUTBOUND_DISABLED)


@app.post("/api/pipelines/{name}/github/push")
def pipeline_github_push(name: str, body: GitHubPushRequest):
    _require_pipeline(name)
    raise HTTPException(403, git_ops.OUTBOUND_DISABLED)


@app.post("/api/pipelines/{name}/github/pr")
def pipeline_github_pr(name: str, body: GitHubPRRequest):
    if not body.title.strip():
        raise HTTPException(400, "PR title is required")
    _require_pipeline(name)
    raise HTTPException(403, git_ops.OUTBOUND_DISABLED)


@app.post("/api/pipelines/{name}/github/repo")
def pipeline_github_create_repo(name: str, body: GitHubRepoCreateRequest):
    _require_pipeline(name)
    raise HTTPException(403, git_ops.OUTBOUND_DISABLED)


@app.post("/api/pipelines/{name}/github/connect")
def pipeline_github_connect_repo(name: str, body: GitHubRepoConnectRequest):
    _require_pipeline(name)
    raise HTTPException(403, git_ops.OUTBOUND_DISABLED)


@app.get("/api/pipelines/{name}/dag")
def pipeline_dag(
    name: str, mode: str = "rulegraph", configfile: str = "", env: str = ""
):
    """Generate a Snakemake graph and rasterize it before browser delivery."""
    p = _require_pipeline(name)
    if p.kind != "snakemake":
        raise HTTPException(400, "DAG view supports snakemake pipelines only")
    if mode not in pipelines.GRAPH_MODES:
        raise HTTPException(400, f"mode must be one of {pipelines.GRAPH_MODES}")
    argv, _ = pipelines.build_snakemake_graph_argv(
        p, mode=mode, configfile=configfile, env=env
    )
    public_command = runplan.redact_command(argv)
    try:
        proc = subprocess.run(
            argv, cwd=str(p.path), capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "mode": mode,
            "command": public_command,
            "error": "Snakemake timed out (120s) generating the graph.",
        }
    # the DOT graph is on stdout; snakemake chatter goes to stderr
    dot = proc.stdout
    start = dot.find("digraph")
    if start == -1:
        msg = (proc.stderr or proc.stdout or "no output").strip()
        return {
            "ok": False,
            "mode": mode,
            "command": public_command,
            "error": provenance.redact_text(msg[-4000:]),
        }
    dot = dot[start:]

    png, render_error = None, None
    dot_exe = shutil.which("dot")
    if not dot_exe:
        render_error = "graphviz 'dot' not found in the OmicsANG env"
    else:
        try:
            r = subprocess.run(
                [dot_exe, "-Tpng"],
                input=dot.encode("utf-8"),
                capture_output=True,
                timeout=60,
            )
            if r.returncode == 0:
                if len(r.stdout) > 8_000_000:
                    render_error = "rendered DAG exceeds the 8 MB browser preview limit"
                else:
                    png = base64.b64encode(r.stdout).decode("ascii")
            else:
                render_error = r.stderr.decode("utf-8", errors="replace").strip()
        except Exception as exc:  # pragma: no cover
            render_error = str(exc)
    node_count = sum(
        1
        for line in dot.splitlines()
        if "->" not in line and re.search(r"\[[^]]*(?:label|shape)=", line)
    )
    return {
        "ok": png is not None,
        "mode": mode,
        "command": public_command,
        "png": png,
        "node_count": node_count,
        "render_error": provenance.redact_text(render_error or ""),
        "stderr": provenance.redact_text((proc.stderr or "")[-1500:]),
    }


@app.get("/api/pipelines/{name}/preview")
def run_preview(
    name: str,
    target: str = "",
    configfile: str = "",
    profile: str = "",
    cores: int = 8,
    dryrun: bool = True,
    use_conda: bool = True,
    extra: str = "",
    env: str = "",
):
    p = _require_pipeline(name)
    if p.kind != "snakemake":
        raise HTTPException(
            400, "command preview only supported for snakemake pipelines"
        )
    body = RunRequest(
        target=target,
        configfile=configfile,
        profile=profile,
        cores=cores,
        dryrun=dryrun,
        use_conda=use_conda,
        extra=extra,
        env=env,
    )
    plan, _, _, _, _ = _resolve_run_plan(
        p,
        body,
        enforce_gate=False,
        check_digest=False,
    )
    plan_record = plan.record()
    return {
        "command": plan.public_command(),
        "plan": plan_record["plan"],
        "plan_digest": plan.digest,
        "plan_digest_scope": plan_record["digest_scope"],
        "plan_projection_digest": plan_record["projection_digest"],
        "plan_redacted": True,
        "plan_schema_version": runplan.SCHEMA_VERSION,
    }


@app.post("/api/pipelines/{name}/preview")
def run_preview_post(name: str, body: RunRequest):
    p = _require_pipeline(name)
    if p.kind != "snakemake":
        raise HTTPException(
            400, "command preview only supported for snakemake pipelines"
        )
    plan, _, study_report, _, _ = _resolve_run_plan(
        p,
        body,
        enforce_gate=False,
        check_digest=False,
    )
    plan_record = plan.record()
    return {
        "command": plan.public_command(),
        "plan": plan_record["plan"],
        "plan_digest": plan.digest,
        "plan_digest_scope": plan_record["digest_scope"],
        "plan_projection_digest": plan_record["projection_digest"],
        "plan_redacted": True,
        "plan_schema_version": runplan.SCHEMA_VERSION,
        "study_fingerprint": study_report.get("fingerprint", ""),
        "study": study_report,
    }


@app.post("/api/pipelines/{name}/slurm_preview")
def slurm_run_preview(name: str, body: SlurmSubmitRequest):
    p = _require_pipeline(name)
    if p.kind != "snakemake":
        raise HTTPException(400, "Slurm preview currently supports Snakemake pipelines")
    plan, _, study_report, _, _ = _resolve_run_plan(
        p,
        body,
        executor="slurm",
        enforce_gate=False,
        check_digest=False,
    )
    plan_record = plan.record()
    return {
        "command": plan.public_command(),
        "plan": plan_record["plan"],
        "plan_digest": plan.digest,
        "plan_digest_scope": plan_record["digest_scope"],
        "plan_projection_digest": plan_record["projection_digest"],
        "plan_redacted": True,
        "plan_schema_version": runplan.SCHEMA_VERSION,
        "study_fingerprint": study_report.get("fingerprint", ""),
        "study": study_report,
    }


@app.get("/api/pipelines/{name}/run_templates")
def run_templates_get(name: str):
    _require_pipeline(name)
    return {"templates": store.run_templates(name)}


@app.post("/api/pipelines/{name}/run_templates")
def run_templates_save(name: str, body: RunTemplateSave):
    _require_pipeline(name)
    if not body.name.strip():
        raise HTTPException(400, "template name is required")
    return {
        "template": store.save_run_template(
            name, {"id": body.id, "name": body.name, "form": body.form}
        )
    }


@app.post("/api/pipelines/{name}/run_templates/{template_id}/delete")
def run_templates_delete(name: str, template_id: str):
    _require_pipeline(name)
    return {"ok": store.delete_run_template(name, template_id)}


# ---- SRA/GEO search + SRA download --------------------------------------
@app.get("/api/pipelines/{name}/sra_geo/info")
def sra_geo_info(name: str):
    p = _require_pipeline(name)
    tools = _sra_geo_tools()
    return {
        "tools": {k: bool(v) for k, v in tools.items()},
        "default_destination": "raw_files/sra",
        "path_options": _download_path_options(p),
    }


@app.post("/api/pipelines/{name}/sra_geo/search")
def sra_geo_search(name: str, body: SraSearchRequest):
    _require_pipeline(name)
    return _ncbi_search(body.db, body.term, body.limit)


@app.post("/api/pipelines/{name}/sra_geo/preview")
def sra_geo_preview(name: str, body: SraDownloadRequest):
    p = _require_pipeline(name)
    plan = _sra_download_plan(p, body)
    return {
        "ok": True,
        "plan": {k: v for k, v in plan.items() if k != "tool_paths"},
    }


@app.post("/api/pipelines/{name}/sra_geo/download")
async def sra_geo_download(name: str, body: SraDownloadRequest):
    p = _require_pipeline(name)
    plan = _sra_download_plan(p, body)
    missing = []
    if plan["use_prefetch"] and not plan["tool_paths"].get("prefetch"):
        missing.append("prefetch")
    if plan["convert_fastq"] and not plan["tool_paths"].get("fasterq_dump"):
        missing.append("fasterq-dump")
    if missing:
        raise HTTPException(
            400, "missing required SRA Toolkit command(s): " + ", ".join(missing)
        )
    Path(plan["destination"]).mkdir(parents=True, exist_ok=True)
    script, config = _write_sra_download_job(plan)
    s = await _spawn(
        kind="download",
        title=f"SRA download · {name}",
        cwd=str(p.path),
        argv=[sys.executable, "-u", str(script), str(config)],
        meta={
            "pipeline": name,
            "accessions": plan["accessions"],
            "destination": plan["destination"],
            "kind": "sra_geo",
        },
        on_exit=_record_on_exit(
            name,
            {
                "tool": "sra_geo",
                "destination": plan["destination"],
                "accessions": plan["accessions"],
            },
        ),
    )
    return {
        "session": s.to_dict(),
        "plan": {k: v for k, v in plan.items() if k != "tool_paths"},
    }


# ---- config editor -------------------------------------------------------
@app.get("/api/file")
def read_file(pipeline: str, path: str):
    p = _require_pipeline(pipeline)
    _, target = _safe_code_file(p.path, path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(400, "file is inaccessible") from exc
    if size > CODE_FILE_LIMIT:
        raise HTTPException(413, f"file is larger than {CODE_FILE_LIMIT} bytes")
    if target.suffix.casefold() in ACTIVE_CONTENT_SUFFIXES | {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".pdf",
    }:
        return _authorized_file_response(target)
    try:
        return PlainTextResponse(target.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return _authorized_file_response(target)


@app.get("/api/pipelines/{name}/config_form")
def config_form_get(name: str, path: str):
    p = _require_pipeline(name)
    _safe_path(p.path, path)
    try:
        return config_form.build(p, path)
    except Exception as exc:
        raise HTTPException(422, f"could not parse as a form: {exc}")


@app.post("/api/pipelines/{name}/config_validate")
def config_form_validate(name: str, body: ConfigForm):
    p = _require_pipeline(name)
    return {"errors": config_form.validate(p, body.fields)}


@app.post("/api/pipelines/{name}/config_save")
def config_form_save(name: str, body: ConfigForm):
    p = _require_pipeline(name)
    _safe_path(p.path, body.path)
    errors = config_form.validate(p, body.fields)
    if any(e["level"] == "error" for e in errors):
        return {"ok": False, "errors": errors}
    result = config_form.save(p, body.path, body.fields)
    result["errors"] = errors  # may contain warnings
    return result


@app.post("/api/file")
def save_file(body: ConfigSave, pipeline: str):
    p = _require_pipeline(pipeline)
    rel, target = _safe_code_file(p.path, body.path, must_exist=False)
    if len(body.content.encode("utf-8")) > CODE_FILE_LIMIT:
        raise HTTPException(413, f"content is larger than {CODE_FILE_LIMIT} bytes")
    backup = _backup_file(p.path, target, pipeline)
    fs_policy.atomic_write_text(target, body.content)
    return {"ok": True, "path": rel, "backup": backup}


# ---- repository browser + code editor -----------------------------------
@app.post("/api/pipelines/{name}/browse/search")
def pipeline_browse_search(name: str, body: BrowseSearchRequest):
    p = _require_pipeline(name)
    rel, root, base = _safe_browse_directory(p.path, body.path)
    query = _clean_browse_query(body.query)
    result = _search_pipeline_entries(
        name,
        root,
        base,
        query=query,
        scope=body.scope,
        limit=body.limit,
    )
    return {
        "path": rel,
        "breadcrumbs": _browse_breadcrumbs(name, rel),
        "query": query,
        "scope": body.scope,
        **result,
    }


@app.post("/api/pipelines/{name}/browse/open")
def pipeline_browse_open(name: str, body: BrowseOpenRequest):
    p = _require_pipeline(name)
    _, target = _safe_browse_file(p.path, body.path)
    try:
        file_info = target.lstat()
    except OSError as exc:
        raise HTTPException(400, "file metadata is unavailable") from exc
    if (
        not stat.S_ISREG(file_info.st_mode)
        or stat.S_ISLNK(file_info.st_mode)
        or file_info.st_nlink != 1
    ):
        raise HTTPException(400, "only ordinary non-hard-linked files can be opened")
    if file_info.st_size > BROWSE_OPEN_FILE_LIMIT:
        raise HTTPException(
            413,
            f"file is larger than the {BROWSE_OPEN_FILE_LIMIT}-byte browser limit",
        )
    return _authorized_file_response(target)


@app.get("/api/pipelines/{name}/files")
def pipeline_files(name: str, q: str = "", limit: int = 800):
    """Return editable source/config/doc files for the code workspace."""
    p = _require_pipeline(name)
    root = p.path.resolve()
    needle = q.strip().lower()
    limit = max(1, min(limit, CODE_LIST_LIMIT))
    files: list[dict] = []
    try:
        candidates = root.rglob("*")
        for path in candidates:
            if len(files) >= limit:
                break
            try:
                rel_parts = path.relative_to(root).parts
            except ValueError:
                continue
            if any(part in CODE_SKIP_DIRS for part in rel_parts[:-1]):
                continue
            try:
                if not path.is_file() or not _is_code_candidate(path):
                    continue
                resolved = path.resolve()
                if root not in resolved.parents and resolved != root:
                    continue
                if path.stat().st_size > CODE_FILE_LIMIT:
                    continue
                rel = path.relative_to(root).as_posix()
            except OSError:
                continue
            if needle and needle not in rel.lower():
                continue
            files.append(_code_entry(root, path))
    except OSError:
        pass
    files.sort(key=lambda f: (f["path"].count("/"), f["path"].lower()))
    return {"files": files, "limit": limit, "max_size": CODE_FILE_LIMIT}


@app.get("/api/pipelines/{name}/code")
def code_read(name: str, path: str):
    return _read_code_payload(_require_pipeline(name), path)


@app.post("/api/pipelines/{name}/code/read")
def code_read_private(name: str, body: CodeHelpRequest):
    return _read_code_payload(_require_pipeline(name), body.path)


@app.post("/api/pipelines/{name}/code/help")
def code_help(name: str, body: CodeHelpRequest):
    p = _require_pipeline(name)
    rel, target = _safe_code_file(p.path, body.path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(400, "file metadata is unavailable") from exc
    if size > CODE_FILE_LIMIT:
        raise HTTPException(413, f"file is larger than {CODE_FILE_LIMIT} bytes")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(415, "file is not UTF-8 text") from exc
    except OSError as exc:
        raise HTTPException(400, "file cannot be read") from exc
    language = _code_language(target)
    packages = help_content.detect_packages(target, text, language)
    notes = [
        "Package detection is static and local: OmicsANG does not import, execute, or upload project code.",
        "Documentation destinations are curated and shown with their domain; opening one leaves the local application.",
    ]
    if not packages:
        notes.append("No supported package reference was detected in this file.")
    return {
        "path": rel,
        "language": language,
        "packages": packages,
        "notes": notes,
    }


@app.get("/api/code/command-help/{command}")
def code_command_help(command: str):
    """Serve a curated static guide; never resolve or execute the command."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.+-]{0,63}", command):
        raise HTTPException(404, "command help is not available")
    guide = help_content.command_help(command)
    if guide is None:
        raise HTTPException(404, "command help is not available")
    return guide


@app.post("/api/pipelines/{name}/code")
def code_save(name: str, body: CodeSave):
    p = _require_pipeline(name)
    relpath = _clean_code_relpath(body.path)
    target = _safe_path(p.path, relpath)
    if target.exists() and not target.is_file():
        raise HTTPException(400, "target is not a file")
    if not _is_code_candidate(target):
        raise HTTPException(400, "path is not an editable code/config/doc file")
    if len(body.content.encode("utf-8")) > CODE_FILE_LIMIT:
        raise HTTPException(413, f"content is larger than {CODE_FILE_LIMIT} bytes")
    if not target.parent.exists():
        if not body.create_dirs:
            raise HTTPException(400, "parent directory does not exist")
        target.parent.mkdir(parents=True, exist_ok=True)

    checked_revision = _code_file_revision(target)
    if not body.overwrite:
        if checked_revision is None and body.expected_revision:
            raise HTTPException(
                409,
                detail={
                    "code": "external-file-change",
                    "message": "the file was removed after it was opened",
                    "path": relpath,
                    "expected_revision": body.expected_revision,
                    "current_revision": "",
                },
            )
        if checked_revision is not None and not body.expected_revision:
            raise HTTPException(
                428,
                detail={
                    "code": "save-precondition-required",
                    "message": "saving an existing file requires its read revision",
                    "path": relpath,
                    "expected_revision": "",
                    "current_revision": checked_revision,
                },
            )
        if checked_revision != (body.expected_revision or None):
            raise HTTPException(
                409,
                detail={
                    "code": "external-file-change",
                    "message": "the file changed on disk after it was opened",
                    "path": relpath,
                    "expected_revision": body.expected_revision,
                    "current_revision": checked_revision or "",
                },
            )

    backup = _backup_file(p.path, target, name)
    # Recheck after the backup read.  This narrows the unavoidable race with
    # external editors that do not participate in OmicsANG's save protocol.
    if not body.overwrite:
        latest_revision = _code_file_revision(target)
        if latest_revision != checked_revision:
            raise HTTPException(
                409,
                detail={
                    "code": "external-file-change",
                    "message": "the file changed while OmicsANG was preparing the save",
                    "path": relpath,
                    "expected_revision": body.expected_revision,
                    "current_revision": latest_revision or "",
                },
            )
    fs_policy.atomic_write_text(target, body.content)
    return {
        "ok": True,
        "backup": backup,
        "revision": _code_revision(body.content),
        **_code_entry(p.path.resolve(), target),
    }


@app.post("/api/pipelines/{name}/code/move")
def code_move(name: str, body: CodeMove):
    p = _require_pipeline(name)
    _, src = _safe_code_file(p.path, body.src)
    _, dst = _safe_code_file(p.path, body.dst, must_exist=False)
    if dst.exists():
        raise HTTPException(409, "destination already exists")
    if not dst.parent.exists():
        if not body.create_dirs:
            raise HTTPException(400, "parent directory does not exist")
        dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return {"ok": True, "src": body.src, **_code_entry(p.path.resolve(), dst)}


@app.post("/api/pipelines/{name}/code/copy")
def code_copy(name: str, body: CodeCopy):
    p = _require_pipeline(name)
    _, src = _safe_code_file(p.path, body.src)
    _, dst = _safe_code_file(p.path, body.dst, must_exist=False)
    if dst.exists():
        raise HTTPException(409, "destination already exists")
    if not dst.parent.exists():
        if not body.create_dirs:
            raise HTTPException(400, "parent directory does not exist")
        dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"ok": True, "src": body.src, **_code_entry(p.path.resolve(), dst)}


@app.post("/api/pipelines/{name}/code/delete")
def code_delete(name: str, body: CodeDelete):
    p = _require_pipeline(name)
    rel, target = _safe_code_file(p.path, body.path)
    trash = _trash_code_path(name, rel)
    shutil.move(str(target), str(trash))
    trash.chmod(0o600)
    return {"ok": True, "path": rel, "trashed_to": trash.name}


@app.post("/api/pipelines/{name}/code/mkdir")
def code_mkdir(name: str, body: CodeMkdir):
    p = _require_pipeline(name)
    rel = _clean_code_relpath(body.path)
    target = _safe_path(p.path, rel)
    if target.exists() and not target.is_dir():
        raise HTTPException(409, "a file already exists at that path")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {"ok": True, "path": target.relative_to(p.path.resolve()).as_posix()}


@app.get("/api/pipelines/{name}/diagnostics")
def code_diagnostics(name: str, limit: int = 120):
    p = _require_pipeline(name)
    root = p.path.resolve()
    limit = max(20, min(limit, 500))
    items: list[dict] = []
    rule_seen: dict[str, tuple[str, int]] = {}

    if p.kind == "snakemake":
        if not p.snakefile:
            items.append(
                _diagnostic(
                    "error",
                    "",
                    0,
                    "Missing Snakefile",
                    "This pipeline is marked as Snakemake but no Snakefile was detected.",
                    "missing-snakefile",
                )
            )
        if not p.configs and not p.project_local:
            items.append(
                _diagnostic(
                    "warning",
                    "",
                    0,
                    "No detected config file",
                    "No config/config.yaml, config.yml, or project_local YAML was found.",
                    "missing-config",
                )
            )
        if p.env_file and not (root / p.env_file).exists():
            items.append(
                _diagnostic(
                    "warning",
                    p.env_file,
                    0,
                    "Declared env file missing",
                    "The discovered conda environment file is no longer present.",
                    "missing-env-file",
                )
            )

    scanned = 0
    for path in _iter_code_files(root):
        if len(items) >= limit:
            break
        rel = path.relative_to(root).as_posix()
        scanned += 1
        if path.suffix.lower() in {".yaml", ".yml"} and (
            rel in (p.configs + p.project_local + p.test_configs)
            or rel.startswith(("config/", "project_local/", ".test/"))
        ):
            _scan_yaml_paths(root, path, rel, items)
        if path.suffix.lower() in {".yaml", ".yml"}:
            _scan_conda_env_yaml(path, rel, items)
        _scan_sample_sheet_fastqs(root, path, rel, items)
        _scan_text_diagnostics(root, path, rel, items)
        if _code_language(path) == "snakemake":
            _scan_snakemake_structure(
                root, path, rel, items, rule_seen, _run_core_budget()
            )

    items = items[:limit]
    summary = {
        "error": sum(1 for i in items if i["level"] == "error"),
        "warning": sum(1 for i in items if i["level"] == "warning"),
        "info": sum(1 for i in items if i["level"] == "info"),
    }
    return {
        "items": items,
        "summary": summary,
        "scanned": scanned,
        "rules": len(rule_seen),
        "limit": limit,
    }


def _new_diagnostic_file_content(path: Path) -> str:
    suffix = path.suffix.lower()
    lower_parts = [p.lower() for p in path.parts]
    if suffix in {".yaml", ".yml"} and (
        path.name in {"environment.yml", "environment.yaml"} or "envs" in lower_parts
    ):
        env_name = (
            re.sub(r"[^A-Za-z0-9_.-]+", "-", path.stem).strip("-") or "omicsang-env"
        )
        return f"name: {env_name}\nchannels:\n  - conda-forge\ndependencies: []\n"
    if path.name == "Snakefile" or suffix in {".smk", ".snakefile"}:
        return "# Created by OmicsANG diagnostic quick fix.\n"
    return ""


@app.post("/api/pipelines/{name}/diagnostics/fix")
def diagnostics_fix(name: str, body: DiagnosticFixRequest):
    p = _require_pipeline(name)
    root = p.path.resolve()
    if body.action == "replace-directive":
        if not body.path or body.line < 1 or not body.from_value or not body.to_value:
            raise HTTPException(
                400, "path, line, from_value, and to_value are required"
            )
        _, target = _safe_code_file(root, body.path)
        text = _read_utf8(target)
        if text is None:
            raise HTTPException(415, "file is not UTF-8 text")
        lines = text.splitlines(keepends=True)
        if body.line > len(lines):
            raise HTTPException(409, "diagnostic line is no longer present")
        line = lines[body.line - 1]
        pattern = rf"^(\s*){re.escape(body.from_value)}(\s*:.*?)(\r?\n)?$"
        match = re.match(pattern, line)
        if not match:
            raise HTTPException(409, "line no longer matches this directive fix")
        lines[body.line - 1] = (
            f"{match.group(1)}{body.to_value}{match.group(2)}{match.group(3) or ''}"
        )
        backup = _backup_file(root, target, name)
        fs_policy.atomic_write_text(target, "".join(lines))
        return {"ok": True, "action": body.action, "path": body.path, "backup": backup}

    if body.action == "create-path":
        rel = body.target.strip().lstrip("/")
        if not rel:
            raise HTTPException(400, "target is required")
        if REMOTE_PATH_RE.search(rel) or any(
            token in rel for token in ("{", "}", "$", "*", "?", "\n")
        ):
            raise HTTPException(400, "target must be a static relative path")
        target = _safe_path(root, rel)
        if body.kind == "dir":
            if target.exists() and not target.is_dir():
                raise HTTPException(409, "a file already exists at that path")
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            return {"ok": True, "action": body.action, "kind": "dir", "path": rel}
        if target.exists() and not target.is_file():
            raise HTTPException(409, "a directory already exists at that path")
        if not _is_code_candidate(target):
            raise HTTPException(
                400,
                "quick-fix file creation is limited to editable code/config/doc files",
            )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.exists():
            fs_policy.atomic_write_text(target, _new_diagnostic_file_content(target))
        return {"ok": True, "action": body.action, "kind": "file", "path": rel}

    raise HTTPException(400, f"unknown diagnostic fix action {body.action!r}")


# ---- durable jobs --------------------------------------------------------
def _durable_process_alive(job: dict) -> bool:
    pid = job.get("pid")
    process_start = str(job.get("process_start") or "")
    boot_id = str(job.get("process_boot_id") or "")
    expected_pgid = job.get("process_pgid")
    if not pid or not process_start or not boot_id or expected_pgid in (None, ""):
        return False
    if boot_id != Session._boot_id():
        return False
    if Session._process_start_token(int(pid)) != process_start:
        return False
    try:
        process_state = (
            Path(f"/proc/{int(pid)}/stat")
            .read_text(
                encoding="utf-8",
            )
            .rsplit(")", 1)[1]
            .split()[0]
        )
        if process_state == "Z":
            return False
    except (OSError, IndexError):
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _signal_durable_process(job: dict, sig: int) -> bool:
    if not _durable_process_alive(job):
        return False
    pid = int(job["pid"])
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return False
    expected_pgid = job.get("process_pgid")
    if expected_pgid not in (None, "") and int(expected_pgid) != pgid:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except OSError:
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False


def _session_controls_job(session: Session, job: dict) -> bool:
    state = str(job.get("state") or "")
    if state in {"created", "queued", "preparing"} or (
        state == "cancel_requested" and session.pid is None
    ):
        return session.status in {"created", "queued"} and session.pid is None
    if state in {"running", "cancel_requested", "cancelling"}:
        return bool(
            session.status in {"running", "killed"}
            and session.pid
            and int(session.pid) == int(job.get("pid") or -1)
            and session.process_start
            and session.process_start == str(job.get("process_start") or "")
            and session.boot_id == str(job.get("process_boot_id") or "")
        )
    return False


async def _cancel_recovered_local_process(
    current: dict,
    body: JobCancelRequest,
) -> dict:
    """Fence and cancel a local process whose PTY is owned by another controller."""
    dispatch_owner = f"{INSTANCE_ID}:cancel:{uuid.uuid4().hex[:8]}"
    resource = f"cancel:local:{current['id']}"
    token = store.acquire_lease(
        resource,
        dispatch_owner,
        ttl=45.0,
        payload=_controller_identity_payload(),
    )
    if not token:
        raise HTTPException(409, "local cancellation is already being dispatched")
    try:
        current = store.get_job(current["id"]) or current
        if current.get("state") in store.TERMINAL_STATES:
            return {"ok": True, "job": current, "already_terminal": True}
        if current.get("state") != "cancel_requested":
            raise HTTPException(409, "local job is no longer awaiting cancellation")
        current = store.update_job(
            current["id"],
            payload_update={
                "cancel_dispatching": True,
                "cancel_dispatch_owner": INSTANCE_ID,
                "cancel_signalled": False,
            },
            event_type="local_cancel_dispatch_started",
            reason="durable checkpoint before identity-checked signaling",
            actor=INSTANCE_ID,
        )
        signalled = _signal_durable_process(current, signal.SIGINT)
        if signalled:
            deadline = time.monotonic() + max(
                0.0,
                min(float(body.interrupt_grace), 30.0),
            )
            while _durable_process_alive(current) and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
        escalated = False
        if _durable_process_alive(current):
            term_sent = _signal_durable_process(current, signal.SIGTERM)
            escalated = term_sent
            signalled = term_sent or signalled
            deadline = time.monotonic() + max(
                0.0,
                min(float(body.terminate_grace), 30.0),
            )
            while _durable_process_alive(current) and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
        if _durable_process_alive(current):
            kill_sent = _signal_durable_process(current, signal.SIGKILL)
            escalated = kill_sent or escalated
            signalled = kill_sent or signalled
            if kill_sent:
                deadline = time.monotonic() + 0.5
                while _durable_process_alive(current) and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
        alive = _durable_process_alive(current)
        current = store.update_job(
            current["id"],
            payload_update={
                "cancel_dispatching": False,
                "cancel_dispatch_owner": "",
                "cancel_escalated": escalated,
                "cancel_signalled": signalled,
            },
            event_type="local_cancel_dispatch_finished",
            reason="signal sent" if signalled else "no signal sent",
            actor=INSTANCE_ID,
        )
        pending_target = str(current.get("cancel_pending_terminal_state") or "")
        if alive and not signalled:
            store.append_event(
                current["id"],
                "cancel_waiting_for_exit_observation",
                reason="no signal was sent; preserving the natural process outcome",
                actor=INSTANCE_ID,
            )
            return {
                "ok": True,
                "job": current,
                "signalled": False,
                "escalated": escalated,
                "awaiting_exit": True,
            }
        if signalled:
            target = "cancelling" if alive else "cancelled"
        elif pending_target in {"succeeded", "failed", "blocked"}:
            target = pending_target
        else:
            target = "lost"
        current = store.transition_job(
            current["id"],
            target,
            expected_states=("cancel_requested",),
            event_type="recovered_process_cancel",
            reason=(
                "identity-checked cancellation"
                if signalled
                else "no matching live process"
            ),
            actor=INSTANCE_ID,
            payload_update={
                "cancel_escalated": escalated,
                "cancel_signalled": signalled,
                "cancel_dispatching": False,
            },
        )
        if current.get("state") in store.TERMINAL_STATES:
            lease_owner = str(current.get("lease_owner") or "")
            if lease_owner:
                try:
                    store.release_job_lease(current["id"], lease_owner)
                except Exception:
                    pass
            try:
                await _await_blocking(_retry_pending_local_finalizations)
            except Exception:
                pass
        return {
            "ok": True,
            "job": current,
            "signalled": signalled,
            "escalated": escalated,
        }
    finally:
        store.release_lease(resource, dispatch_owner, token)


async def _cancel_durable_job(job: dict, body: JobCancelRequest) -> dict:
    current = store.get_job(job["id"]) or job
    if (
        body.expected_version is not None
        and current.get("state_version") != body.expected_version
    ):
        raise HTTPException(
            409,
            detail={
                "message": "job changed before cancellation could be applied",
                "current_version": current.get("state_version"),
                "job": _public_job(current),
            },
        )
    if current.get("state") in store.TERMINAL_STATES:
        return {"ok": True, "job": current, "already_terminal": True}
    if current.get("state") == "cancelling":
        return {"ok": True, "job": current, "already_requested": True}
    reason = provenance.redact_text(
        str(body.reason or "operator requested cancellation")
    )[:500]
    candidate_session = mgr.get(str(current.get("session_id") or current["id"]))
    remote_local_dispatch = bool(
        current.get("executor") == "local"
        and current.get("pid")
        and not (
            candidate_session and _session_controls_job(candidate_session, current)
        )
    )
    if current.get("state") == "cancel_requested":
        if current.get("executor") != "slurm":
            dispatch_owner = str(current.get("cancel_dispatch_owner") or "")
            if current.get("cancel_dispatching") and _controller_owner_alive(
                dispatch_owner,
            ):
                return {"ok": True, "job": current, "already_requested": True}
    else:
        try:
            current = store.transition_job(
                current["id"],
                "cancel_requested",
                expected_states=(str(current.get("state")),),
                expected_version=body.expected_version,
                event_type="cancel_requested",
                reason=reason,
                actor=INSTANCE_ID,
                payload_update=(
                    {
                        "cancel_dispatching": True,
                        "cancel_dispatch_owner": INSTANCE_ID,
                        "cancel_signalled": False,
                    }
                    if remote_local_dispatch
                    else None
                ),
            )
        except store.InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc

    if current.get("executor") == "slurm":
        scheduler_id = str(current.get("scheduler_job_id") or "")
        if not scheduler_id:
            # An ambiguous submission has no safe executor reference to cancel.
            store.append_event(
                current["id"],
                "cancel_deferred",
                reason="scheduler job id is not yet reconciled",
                actor=INSTANCE_ID,
            )
            return {"ok": True, "job": store.get_job(current["id"]), "deferred": True}
        current, res = await _await_blocking(_dispatch_slurm_cancel, current)
        return {
            "ok": True,
            "job": current,
            "output": provenance.redact_text(str(res.get("output") or ""))[:4000],
        }

    sid = str(current.get("session_id") or current["id"])
    session = mgr.get(sid)
    if session and not _session_controls_job(session, current):
        store.append_event(
            current["id"],
            "stale_session_overlay_ignored",
            reason="in-memory session identity does not own the durable process",
            actor=INSTANCE_ID,
        )
        session = None
    if session:
        result = await session.cancel(
            interrupt_grace=max(0.0, min(float(body.interrupt_grace), 30.0)),
            terminate_grace=max(0.0, min(float(body.terminate_grace), 30.0)),
        )
        await asyncio.sleep(0)
        current = store.get_job(current["id"]) or current
        if current.get("state") not in store.TERMINAL_STATES:
            if result.get("cancelled_before_start"):
                current = store.transition_job(
                    current["id"],
                    "cancelled",
                    expected_states=("cancel_requested",),
                    event_type="cancelled_before_start",
                    reason=reason,
                    actor=INSTANCE_ID,
                    payload_update={"cancel_result": result},
                )
                return {"ok": True, "job": current, "result": result}
            if not result.get("signalled"):
                store.append_event(
                    current["id"],
                    "cancel_waiting_for_exit_observation",
                    reason="no signal was sent; preserving the natural process outcome",
                    actor=INSTANCE_ID,
                    payload=result,
                )
                return {
                    "ok": True,
                    "job": store.get_job(current["id"]),
                    "result": result,
                    "awaiting_exit": True,
                }
            target = "cancelling" if result.get("alive") else "cancelled"
            current = store.transition_job(
                current["id"],
                target,
                expected_states=("cancel_requested",),
                event_type="cancel_escalated"
                if result.get("escalated")
                else "cancel_observed",
                reason="process remains alive"
                if result.get("alive")
                else "process stopped",
                actor=INSTANCE_ID,
                payload_update={"cancel_result": result},
            )
        return {"ok": True, "job": current, "result": result}

    # A queued job restored without an in-memory object has never crossed the
    # spawn barrier and can be cancelled without signaling anything.
    if job.get("state") in {"created", "queued"}:
        current = store.transition_job(
            current["id"],
            "cancelled",
            expected_states=("cancel_requested",),
            event_type="cancelled_before_start",
            reason=reason,
            actor=INSTANCE_ID,
        )
        return {"ok": True, "job": current, "signalled": False}

    # Recovered/cross-worker work has no local PTY object. Its exact durable
    # process identity and cancellation lease are the only signaling authority.
    return await _cancel_recovered_local_process(current, body)


@app.get("/api/jobs")
def jobs_list(
    pipeline: str = "",
    executor: str = "",
    state: str = "",
    limit: int = 100,
):
    states = tuple(item.strip() for item in state.split(",") if item.strip())
    return [
        _public_job(job)
        for job in store.list_jobs(
            pipeline=pipeline or None,
            executor=executor or None,
            states=states or None,
            limit=max(1, min(limit, 1000)),
        )
    ]


@app.get("/api/jobs/idempotency/{key}")
def job_by_idempotency(key: str):
    job = store.job_by_idempotency(key)
    if not job:
        raise HTTPException(404, "job not found")
    return _public_job(job)


@app.get("/api/jobs/{job_id}")
def job_get(job_id: str):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return _public_job(job)


@app.get("/api/jobs/{job_id}/events")
def job_event_list(job_id: str, after: int = 0, limit: int = 500):
    if not store.get_job(job_id):
        raise HTTPException(404, "job not found")
    return _public_job_events(store.job_events(job_id, after=after, limit=limit))


@app.post("/api/jobs/{job_id}/cancel")
async def job_cancel(job_id: str, body: JobCancelRequest | None = None):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    result = await _cancel_durable_job(job, body or JobCancelRequest())
    return _public_job_response(result)


@app.post("/api/jobs/{job_id}/resolve")
async def job_resolve(job_id: str, body: JobResolveRequest):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if (
        body.expected_version is not None
        and job.get("state_version") != body.expected_version
    ):
        raise HTTPException(409, "job changed before resolution could be applied")
    action = str(body.action or "").strip()
    reason = provenance.redact_text(str(body.reason or "").strip())[:500]
    if action == "reconcile_now" and job.get("executor") == "slurm":
        await _await_blocking(_reconcile_slurm_jobs)
        return _public_job_response(
            {"ok": True, "job": store.get_job(job_id), "action": action}
        )
    if action == "acknowledge_failed" and job.get("executor") == "local":
        if job.get("state") != "lost":
            raise HTTPException(409, "only lost local jobs can be acknowledged")
        if _durable_process_alive(job):
            raise HTTPException(
                409, "the exact lost process is still alive; cancel it first"
            )
        if not reason:
            raise HTTPException(400, "an operator reason is required")
        resolved = store.transition_job(
            job_id,
            "failed",
            expected_states=("lost",),
            expected_version=body.expected_version,
            event_type="operator_acknowledged_lost",
            reason=reason,
            actor=INSTANCE_ID,
            payload_update={"process_alive_at_recovery": False},
            lease_owner="",
            lease_expires=None,
            last_error=reason,
        )
        return _public_job_response({"ok": True, "job": resolved, "action": action})
    if action == "mark_not_submitted" and job.get("executor") == "slurm":
        if job.get("state") not in {"submission_unknown", "cancel_requested"}:
            raise HTTPException(409, "Slurm submission is no longer unresolved")
        if job.get("scheduler_job_id"):
            raise HTTPException(
                409, "a scheduler job id is already bound; reconcile or cancel it"
            )
        if not reason:
            raise HTTPException(400, "an operator evidence note is required")
        resolved = store.transition_job(
            job_id,
            "failed",
            expected_states=(str(job.get("state")),),
            expected_version=body.expected_version,
            event_type="operator_marked_not_submitted",
            reason=reason,
            actor=INSTANCE_ID,
            last_error=reason,
            payload_update={"executor_state": "operator-confirmed-not-submitted"},
        )
        tracked = store.slurm_job(intent_id=job_id)
        if tracked:
            store.record_slurm_job(
                {
                    **tracked,
                    "status": "not-submitted",
                    "resolution_reason": reason,
                    "resolved": time.time(),
                }
            )
        pipeline = pipelines.get(str(job.get("pipeline") or ""))
        capsule_id = str(job.get("capsule_id") or "")
        if pipeline and capsule_id:
            try:
                _mark_slurm_capsule_failed(pipeline, capsule_id, reason)
            except Exception:
                pass
        return _public_job_response({"ok": True, "job": resolved, "action": action})
    raise HTTPException(400, "resolution action is not valid for this job")


# ---- run queue -----------------------------------------------------------
@app.get("/api/run_queue")
def run_queue_get():
    return _run_queue_state()


@app.post("/api/run_queue")
async def run_queue_set(body: RunQueueConfig):
    if body.max_cores < 1:
        raise HTTPException(400, "max_cores must be at least 1")
    if body.max_cores > 1024:
        raise HTTPException(400, "max_cores is unreasonably high")
    store.set_run_queue_config(body.max_cores)
    _maybe_start_queued_runs()
    return _run_queue_state()


# ---- Slurm resources -----------------------------------------------------
@app.get("/api/slurm/status")
def slurm_status():
    tools = _slurm_tools()
    capabilities = {
        "submit": bool(tools.get("sbatch")),
        "monitor": bool(tools.get("squeue")),
        "accounting": bool(tools.get("sacct")),
        # Cancellation is ownership-gated by a live squeue observation before
        # scancel is dispatched, so both commands are required for this action.
        "cancel": bool(tools.get("scancel") and tools.get("squeue")),
        "partitions": bool(tools.get("sinfo")),
    }
    stored = store.slurm_jobs(limit=100)
    clusters = {str(job.get("scheduler_cluster") or "") for job in stored}
    live = []
    for cluster in sorted(clusters):
        live.extend(_slurm_live_jobs(cluster))
    try:
        _reconcile_slurm_jobs(live)
    except Exception:
        pass
    live_by_key = {
        (str(job.get("cluster") or ""), str(job.get("job_id") or "")): job
        for job in live
    }
    owned_pairs = {
        (
            str(job.get("scheduler_cluster") or ""),
            str(job.get("job_id") or ""),
            str(job.get("submission_tag") or ""),
        )
        for job in stored
        if job.get("job_id") and job.get("submission_tag")
    }
    jobs = []
    for job in stored:
        merged = dict(job)
        expected_tag = f"benchtop:{job.get('id', '')}"
        cluster = str(job.get("scheduler_cluster") or "")
        live_job = live_by_key.get((cluster, str(job.get("job_id") or "")))
        ownership_observed = bool(
            live_job
            and str(job.get("submission_tag") or "") == expected_tag
            and str(live_job.get("comment") or "") == expected_tag
        )
        if ownership_observed:
            merged.update(live_job)
        elif live_job:
            merged["scheduler_identity_mismatch"] = True
        merged["benchtop_owned"] = ownership_observed
        merged["durable_job_id"] = job.get("id", "")
        jobs.append(_public_slurm_job(merged))
    seen = {
        (str(job.get("scheduler_cluster") or ""), str(job.get("job_id") or ""))
        for job in jobs
    }
    for job in live:
        key = (str(job.get("cluster") or ""), str(job.get("job_id") or ""))
        if key not in seen:
            job["benchtop_owned"] = (
                str(job.get("cluster") or ""),
                str(job.get("job_id") or ""),
                str(job.get("comment") or ""),
            ) in owned_pairs
            jobs.append(_public_slurm_job(job))
    return {
        # Preserve the original aggregate readiness contract for older clients;
        # newer clients can gate individual actions with `capabilities`.
        "available": capabilities["submit"] and capabilities["monitor"],
        "capabilities": capabilities,
        "tools": {k: bool(v) for k, v in tools.items()},
        "partitions": _slurm_partitions(),
        "jobs": jobs[:100],
    }


@app.post("/api/pipelines/{name}/slurm_submit")
def slurm_submit(name: str, body: SlurmSubmitRequest):
    p = _require_pipeline(name)
    if p.kind != "snakemake":
        raise HTTPException(
            400, "Slurm submission currently supports Snakemake pipelines"
        )
    provided_key = str(body.idempotency_key or "").strip()
    if provided_key:
        idempotency_key = _submission_key(body)
        existing = store.job_by_idempotency(idempotency_key)
        if existing:
            request_digest = _submission_request_digest(body, name, "slurm")
            _assert_idempotent_contract(
                existing,
                pipeline=name,
                executor="slurm",
                plan_digest=str(body.plan_digest or existing.get("plan_digest") or ""),
                request_digest=request_digest,
            )
            return _slurm_replay_response(existing)
    plan, human, study_report, override_valid, resolved_env = _resolve_run_plan(
        p,
        body,
        executor="slurm",
        enforce_resolution=True,
    )
    if not body.plan_digest:
        raise HTTPException(428, "Slurm submission requires a previewed RunPlan digest")
    return _submit_slurm_run(
        p,
        body,
        study_report,
        override_valid,
        plan=plan,
        resolved_env=resolved_env,
    )


@app.post("/api/slurm/jobs/{job_id}/cancel")
async def slurm_cancel(job_id: str):
    if not re.match(r"^[A-Za-z0-9_.+-]+$", job_id):
        raise HTTPException(400, "invalid Slurm job id")
    matches = [
        job
        for job in store.jobs_by_scheduler_id(job_id)
        if job.get("executor") == "slurm"
    ]
    if len(matches) > 1:
        raise HTTPException(
            409,
            "Slurm job id is ambiguous across clusters; cancel by durable job id",
        )
    durable = matches[0] if matches else None
    if not durable:
        raise HTTPException(404, "Slurm job is not owned by an OmicsANG submission")
    if durable.get("scheduler_cluster"):
        raise HTTPException(
            409,
            "cluster-qualified Slurm jobs must be cancelled by durable job id",
        )
    result = await _cancel_durable_job(durable, JobCancelRequest())
    return {**_public_job_response(result), "job_id": job_id}


# ---- runs ----------------------------------------------------------------
@app.post("/api/pipelines/{name}/run")
async def start_run(name: str, body: RunRequest):
    p = _require_pipeline(name)
    if p.kind != "snakemake":
        raise HTTPException(400, "run endpoint currently supports snakemake pipelines")
    request_digest = _submission_request_digest(body, name, "local")
    provided_key = str(body.idempotency_key or "").strip()
    if provided_key:
        idempotency_key = _submission_key(body)
        existing = store.job_by_idempotency(idempotency_key)
        if existing:
            _assert_idempotent_contract(
                existing,
                pipeline=name,
                executor="local",
                plan_digest=str(body.plan_digest or existing.get("plan_digest") or ""),
                request_digest=request_digest,
            )
            return _local_replay_response(existing)
    budget = _run_core_budget()
    if body.cores > budget:
        raise HTTPException(
            400,
            f"requested {body.cores} cores exceeds the local run budget of {budget}; "
            "raise the budget or lower the run cores",
        )
    if body.cores < 1:
        raise HTTPException(400, "cores must be at least 1")
    plan, human, study_report, override_valid, resolved_env = await _await_blocking(
        _resolve_run_plan,
        p,
        body,
        enforce_resolution=True,
    )
    if not body.dryrun and not body.plan_digest:
        raise HTTPException(428, "real runs require a previewed RunPlan digest")
    idempotency_key = provided_key or _submission_key(body)
    existing = store.job_by_idempotency(idempotency_key)
    if existing:
        _assert_idempotent_contract(
            existing,
            pipeline=name,
            executor="local",
            plan_digest=plan.digest,
            request_digest=request_digest,
        )
        return _local_replay_response(existing)
    title = f"{name} {'dry-run' if body.dryrun else 'run'}"
    meta = {
        "pipeline": name,
        "dryrun": body.dryrun,
        "human": runplan.redact_command(plan.argv),
        "cores": body.cores,
        "target": body.target,
        "configfile": body.configfile,
        "profile": body.profile,
        "use_conda": body.use_conda,
        "extra": runplan.redact_command_text(body.extra),
        "env": resolved_env,
        "study_sheet": (study_report.get("selected") or {}).get(
            "path", body.study_sheet
        ),
        "study_roles": study_report.get("roles", body.study_roles),
        "study_gate": study_report.get("gate", "unknown"),
        "study_fingerprint": study_report.get("fingerprint", ""),
        "study_override": override_valid,
        "study_override_fingerprint": (
            study_report.get("fingerprint", "") if override_valid else ""
        ),
        "plan_schema_version": runplan.SCHEMA_VERSION,
        "plan_digest": plan.digest,
    }
    s = mgr.create(
        kind="run",
        title=title,
        cwd=plan.cwd,
        argv=plan.argv,
        meta=meta,
        on_exit=_record_run_on_exit(name, {"dryrun": body.dryrun}),
    )
    s._run_plan = plan
    job, created = store.create_job(
        job_id=s.id,
        kind="run",
        executor="local",
        pipeline=name,
        state="created",
        idempotency_key=idempotency_key,
        plan_digest=plan.digest,
        session_id=s.id,
        payload={**_session_job_payload(s), "request_digest": request_digest},
        private=_session_private_payload(s),
        actor=INSTANCE_ID,
        lease_owner=INSTANCE_ID,
        lease_ttl=CONTROLLER_LEASE_TTL,
    )
    if not created:
        mgr.sessions.pop(s.id, None)
        _assert_idempotent_contract(
            job,
            pipeline=name,
            executor="local",
            plan_digest=plan.digest,
            request_digest=request_digest,
        )
        return _local_replay_response(job)
    try:
        _queue_or_start_run(s)
    except Exception as exc:
        mgr.sessions.pop(s.id, None)
        try:
            store.transition_job(
                s.id,
                "failed",
                reason=f"scheduler acceptance failed: {exc}",
                actor=INSTANCE_ID,
                last_error=str(exc),
            )
        except Exception:
            pass
        raise
    job = store.get_job(s.id) or job
    return {
        "session": s.to_dict(),
        "job": _public_job(job),
        "queue": _run_queue_state(),
        "plan_digest": plan.digest,
        "plan_schema_version": runplan.SCHEMA_VERSION,
        "idempotency_key": idempotency_key,
        "idempotent_replay": False,
        "replayed": False,
    }


# ---- agents --------------------------------------------------------------
def _require_agent_acknowledgement(tool: str, acknowledged: bool) -> None:
    if not acknowledged:
        if tool == "shell":
            detail = (
                "the shell runs as your OS account and OmicsANG does not add an "
                "OS sandbox; commands or shell startup configuration may use the network"
            )
        else:
            detail = (
                "the agent runs as your OS account and OmicsANG does not add an OS "
                "sandbox; repository context it reads may be sent to its provider"
            )
        raise HTTPException(
            428,
            f"explicit launch confirmation is required: {detail}",
        )


def _agent_cwd(p: pipelines.Pipeline, worktree_branch: str) -> Path:
    branch = str(worktree_branch or "").strip()
    if not branch:
        return p.path.resolve()
    worktree = git_ops.worktree_for_branch(p.path, branch)
    if not worktree:
        raise HTTPException(400, "worktree branch is not registered for this pipeline")
    return worktree


@app.post("/api/agents")
async def start_agent(body: AgentRequest):
    p = _require_pipeline(body.pipeline)
    tool = settings.AGENT_TOOLS.get(body.tool)
    if not tool:
        raise HTTPException(400, f"unknown tool {body.tool!r}")
    _require_agent_acknowledgement(body.tool, body.acknowledge_external_agent)
    exe = shutil.which(tool["exe"]) or tool["exe"]
    argv = [exe]
    prompt = body.prompt if body.tool in ("claude", "codex") else ""
    cwd = _agent_cwd(p, body.worktree_branch)
    s = await _spawn(
        kind="agent",
        title=f"{tool['label']} · {body.pipeline}",
        cwd=str(cwd),
        argv=argv,
        initial_input=prompt,
        meta={"pipeline": body.pipeline, "tool": body.tool},
        on_exit=_record_on_exit(body.pipeline, {"tool": body.tool}),
    )
    return {"session": s.to_dict()}


@app.post("/api/agents/debug")
async def debug_run(body: DebugRequest):
    """Preview, then explicitly launch, a provider-visible run-debug prompt."""
    src = mgr.get(body.session_id)
    if not src:
        raise HTTPException(404, "session not found")
    pipeline_name = src.meta.get("pipeline", "")
    p = _require_pipeline(pipeline_name)
    log = src.logfile or ""
    prompt = provenance.redact_text(
        f"A Snakemake run in this repo ({pipeline_name}) just finished with "
        f"status '{src.status}' (exit code {src.exit_code}). Its full terminal "
        f"log is at {log}. Read the log, find the root cause of the failure (or "
        f"summarise what succeeded), and propose a concrete fix. The exact command "
        f"was: {runplan.redact_command(src.argv)}"
    )
    tool = settings.AGENT_TOOLS.get(body.tool)
    if not tool or body.tool not in {"claude", "codex"}:
        raise HTTPException(400, "debug tool must be claude or codex")
    preview = {
        "tool": body.tool,
        "provider_warning": (
            "This prompt and any repository/log content the agent reads may be "
            "transmitted under the provider's configuration and terms. OmicsANG "
            "contains the CLI to this repository and its own provider "
            "configuration, but does not restrict what it sends."
        ),
        "pipeline": pipeline_name,
        "repository": str(p.path.resolve()),
        "log_path": log,
        "prompt": prompt,
    }
    if body.preview_only:
        return {"preview": preview}
    _require_agent_acknowledgement(body.tool, body.acknowledge_external_agent)
    exe = shutil.which(tool["exe"]) or tool["exe"]
    s = await _spawn(
        kind="agent",
        title=f"debug · {pipeline_name}",
        cwd=str(p.path),
        argv=[exe],
        initial_input=prompt,
        meta={"pipeline": pipeline_name, "tool": body.tool, "debug_of": src.id},
    )
    return {"session": s.to_dict()}


# ---- worktrees -----------------------------------------------------------
@app.post("/api/worktrees")
def create_worktree(body: WorktreeRequest):
    p = _require_pipeline(body.pipeline)
    return git_ops.add_worktree(p.path, body.branch, body.base)


# ---- fleet: one task across many pipelines -------------------------------
@app.post("/api/fleet")
async def fleet_launch(body: FleetRequest):
    if not body.prompt.strip():
        raise HTTPException(400, "prompt is required")
    if not body.pipelines:
        raise HTTPException(400, "pick at least one pipeline")
    tool = settings.AGENT_TOOLS.get(body.tool)
    if not tool:
        raise HTTPException(400, f"unknown tool {body.tool!r}")
    _require_agent_acknowledgement(body.tool, body.acknowledge_external_agent)
    exe = shutil.which(tool["exe"]) or tool["exe"]
    branch = body.branch.strip() or f"omicsang/{body.tool}-{uuid.uuid4().hex[:6]}"

    names = list(dict.fromkeys(body.pipelines))
    job = fleet.new_job(body.prompt, body.tool, body.use_worktree)
    prepared: list[dict] = []
    for name in names:
        p = pipelines.get(name)
        if not p:
            fleet.add_target(job, pipeline=name, error="pipeline not found")
            continue
        cwd, wt_branch = str(p.path), None
        if body.use_worktree:
            res = git_ops.add_worktree(p.path, branch)
            if res["ok"]:
                cwd, wt_branch = res["path"], branch
            else:
                fleet.add_target(
                    job,
                    pipeline=name,
                    error="worktree preparation failed; no agent was launched",
                )
                continue
        prepared.append(
            {
                "pipeline": name,
                "cwd": cwd,
                "branch": wt_branch,
                "worktree": body.use_worktree,
            }
        )

    if len(prepared) != len(names):
        for target in prepared:
            fleet.add_target(
                job,
                **target,
                error="fleet launch aborted before starting any agent",
            )
        fleet.persist(job)
        return _fleet_detail(job)

    for target in prepared:
        name = target["pipeline"]
        argv = [exe]
        s = await _spawn(
            kind="agent",
            title=f"{tool['label']} · {name}",
            cwd=target["cwd"],
            argv=argv,
            initial_input=(body.prompt if body.tool in ("claude", "codex") else ""),
            meta={"pipeline": name, "tool": body.tool, "fleet": job["id"]},
        )
        fleet.add_target(
            job,
            pipeline=name,
            cwd=target["cwd"],
            branch=target["branch"],
            session_id=s.id,
            worktree=target["worktree"],
            error=None,
        )
    fleet.persist(job)
    return _fleet_detail(job)


def _fleet_detail(job: dict) -> dict:
    targets = []
    for t in job["targets"]:
        row = {
            "pipeline": t.get("pipeline", ""),
            "branch": t.get("branch"),
            "session_id": t.get("session_id"),
            "worktree": bool(t.get("worktree")),
            "error": t.get("error"),
            "can_diff": bool(t.get("cwd")),
        }
        s = mgr.get(t.get("session_id", "")) if t.get("session_id") else None
        row["status"] = s.status if s else ("error" if t.get("error") else "gone")
        if row["can_diff"]:
            row["diffstat"] = git_ops.diffstat(Path(t["cwd"]))
        targets.append(row)
    return {
        "id": job["id"],
        "created": job["created"],
        "prompt": "[prompt omitted]",
        "tool": job["tool"],
        "use_worktree": job["use_worktree"],
        "targets": targets,
    }


@app.get("/api/fleet")
def fleet_list():
    return [
        {
            "id": j["id"],
            "created": j["created"],
            "tool": j["tool"],
            "prompt": "[prompt omitted]",
            "n": len(j["targets"]),
        }
        for j in fleet.all_jobs()
    ]


@app.get("/api/fleet/{job_id}")
def fleet_get(job_id: str):
    job = fleet.get(job_id)
    if not job:
        raise HTTPException(404, "fleet job not found")
    return _fleet_detail(job)


@app.get("/api/fleet/{job_id}/diff/{index}")
def fleet_diff(job_id: str, index: int):
    job = fleet.get(job_id)
    if not job or index >= len(job["targets"]):
        raise HTTPException(404, "not found")
    t = job["targets"][index]
    if not t.get("cwd"):
        return {"pipeline": t["pipeline"], "diff": ""}
    cwd = Path(t["cwd"])
    text = git_ops.diff(cwd)
    untracked = [
        line[3:] for line in git_ops.diffstat(cwd)["changed"] if line.startswith("??")
    ]
    if untracked:
        text = (
            "# new (untracked) files:\n"
            + "".join(f"#   {u}\n" for u in untracked)
            + "\n"
            + text
        )
    return {"pipeline": t["pipeline"], "diff": text}


@app.post("/api/fleet/{job_id}/pr")
def fleet_pr(job_id: str, body: FleetPR):
    """Fail closed until a reviewed two-step staging protocol is implemented."""
    del job_id, body
    raise HTTPException(403, git_ops.OUTBOUND_DISABLED)


# ---- sessions ------------------------------------------------------------
@app.get("/api/sessions")
def list_sessions():
    live = {s.id: s.to_dict() for s in mgr.list()}
    durable = store.list_jobs(executor="local", limit=200)
    for job in durable:
        sid = str(job.get("session_id") or job["id"])
        live.setdefault(sid, _job_session_projection(job))
    return sorted(
        live.values(), key=lambda item: item.get("created") or 0, reverse=True
    )


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    s = mgr.get(sid)
    if s:
        return s.to_dict()
    job = store.get_job(sid)
    if job and job.get("executor") == "local":
        return _job_session_projection(job)
    raise HTTPException(404, "not found")


@app.post("/api/sessions/{sid}/kill")
async def kill_session(sid: str):
    s = mgr.get(sid)
    job = store.get_job(sid)
    if job:
        result = await _cancel_durable_job(job, JobCancelRequest())
        return _public_job_response(result)
    if not s:
        raise HTTPException(404, "not found")
    result = await s.cancel()
    return {"ok": True, "result": result}


# ---- websocket terminal --------------------------------------------------
@app.websocket("/ws/term/{sid}")
async def ws_term(ws: WebSocket, sid: str):
    await ws.accept()
    s = mgr.get(sid)
    if not s:
        job = store.get_job(sid)
        if not job or job.get("executor") != "local":
            await ws.close(code=4404)
            return
        logfile = Path(str(job.get("logfile") or "")).expanduser()
        try:
            resolved = logfile.resolve()
            state_root = settings.STATE_DIR.resolve()
            if resolved.is_file() and (
                resolved == state_root or state_root in resolved.parents
            ):
                with resolved.open("rb") as handle:
                    size = resolved.stat().st_size
                    if size > 2_000_000:
                        handle.seek(size - 2_000_000)
                    replay = handle.read()
                if replay:
                    await ws.send_bytes(replay)
        except OSError:
            pass
        await ws.send_text(
            json.dumps(
                {
                    "type": "exit",
                    "code": (job.get("session") or {}).get("exit_code"),
                    "status": _job_session_projection(job).get("status"),
                    "durable_state": job.get("state"),
                }
            )
        )
        await ws.close()
        return
    try:
        queue, snapshot = s.subscribe()
    except RuntimeError:
        await ws.close(code=4429, reason="terminal subscriber limit reached")
        return
    if snapshot:
        await ws.send_bytes(snapshot)

    async def pump_out():
        while True:
            item = await queue.get()
            if item is EOF:
                await ws.send_text(
                    json.dumps(
                        {"type": "exit", "code": s.exit_code, "status": s.status}
                    )
                )
                return
            await ws.send_bytes(item)

    async def pump_in():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("text") is not None:
                if len(msg["text"].encode("utf-8")) > MAX_WEBSOCKET_MESSAGE:
                    await ws.close(code=4409, reason="message is too large")
                    return
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    continue
                # a malformed input/resize message must not tear down the terminal
                if data.get("type") == "input":
                    raw = str(data.get("data", "")).encode("utf-8")
                    if len(raw) <= MAX_WEBSOCKET_MESSAGE:
                        s.write(raw)
                elif data.get("type") == "resize":
                    try:
                        rows, cols = int(data["rows"]), int(data["cols"])
                        if 1 <= rows <= 500 and 1 <= cols <= 500:
                            s.resize(rows, cols)
                    except (KeyError, TypeError, ValueError):
                        pass
            elif msg.get("bytes") is not None:
                if len(msg["bytes"]) > MAX_WEBSOCKET_MESSAGE:
                    await ws.close(code=4409, reason="message is too large")
                    return
                s.write(msg["bytes"])

    out_t = asyncio.create_task(pump_out())
    in_t = asyncio.create_task(pump_in())
    try:
        await asyncio.wait({out_t, in_t}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        out_t.cancel()
        in_t.cancel()
        # await the cancelled tasks so their exceptions are retrieved, not orphaned
        await asyncio.gather(out_t, in_t, return_exceptions=True)
        s.unsubscribe(queue)


# ---- restart reconciliation ---------------------------------------------
def _retry_pending_local_finalizations_under_lease() -> int:
    completed = 0
    processed = 0
    jobs = store.list_jobs(
        kind="run",
        executor="local",
        states=tuple(store.TERMINAL_STATES),
        limit=1000,
    )
    for job in jobs:
        capsule_pending = bool(job.get("capsule_finalize_pending"))
        history_pending = bool(job.get("history_finalize_pending"))
        if not capsule_pending and not history_pending:
            continue
        if float(job.get("finalization_next_attempt") or 0) > time.time():
            continue
        if processed >= 20:
            break
        processed += 1
        pipeline = pipelines.get(str(job.get("pipeline") or ""))
        if not pipeline:
            store.update_job(
                job["id"],
                payload_update={
                    "finalization_attempt": int(job.get("finalization_attempt") or 0)
                    + 1,
                    "finalization_next_attempt": time.time() + 300.0,
                },
                event_type="local_finalization_pipeline_unavailable",
                reason="pipeline is not currently discoverable",
                actor=INSTANCE_ID,
            )
            continue
        private = store.get_job(job["id"], include_private=True) or {}
        execution = (private.get("_private") or {}).get("execution") or {}
        meta = dict(execution.get("meta") or {})
        session_record = dict(job.get("session") or {})
        status = {
            "succeeded": "exited",
            "failed": "failed",
            "cancelled": "killed",
            "blocked": "failed",
        }.get(str(job.get("state") or ""), "failed")
        exit_code = session_record.get("exit_code")
        proxy = SimpleNamespace(
            id=str(job.get("session_id") or job["id"]),
            status=status,
            started=job.get("started") or session_record.get("started"),
            ended=job.get("ended") or session_record.get("ended") or time.time(),
            exit_code=exit_code,
            logfile=str(job.get("logfile") or execution.get("logfile") or ""),
        )
        capsule_complete = not capsule_pending
        history_complete = not history_pending
        capsule_fingerprint = str(job.get("capsule_fingerprint") or "")
        if capsule_pending and job.get("capsule_id"):
            try:
                capsule = provenance.finalize(pipeline, proxy)
                capsule_fingerprint = str(capsule.get("fingerprint") or "")
                store.record_capsule_ref(
                    provenance.capsule_reference(
                        pipeline,
                        capsule,
                        job_id=job["id"],
                        status=status,
                    )
                )
                capsule_complete = True
            except Exception as exc:
                store.append_event(
                    job["id"],
                    "local_capsule_finalize_retry_failed",
                    reason=provenance.redact_text(str(exc)),
                    actor=INSTANCE_ID,
                )
        if history_pending:
            try:
                store.record(
                    {
                        "id": job["id"],
                        "kind": "run",
                        "pipeline": job.get("pipeline", ""),
                        "title": job.get("title", execution.get("title", "Run")),
                        "status": status,
                        "exit_code": exit_code,
                        "command": job.get("command", ""),
                        "started": proxy.started,
                        "ended": proxy.ended,
                        "logfile": proxy.logfile,
                        "dryrun": bool(job.get("dryrun", meta.get("dryrun"))),
                        "plan_digest": job.get("plan_digest", ""),
                        "capsule_id": job.get("capsule_id", ""),
                        "capsule_fingerprint": capsule_fingerprint,
                        "recovered_finalization": True,
                    }
                )
                history_complete = True
            except Exception as exc:
                store.append_event(
                    job["id"],
                    "local_history_finalize_retry_failed",
                    reason=provenance.redact_text(str(exc)),
                    actor=INSTANCE_ID,
                )
        store.update_job(
            job["id"],
            payload_update={
                "capsule_finalize_pending": not capsule_complete,
                "history_finalize_pending": not history_complete,
                "capsule_fingerprint": capsule_fingerprint,
                "finalization_attempt": (
                    0
                    if capsule_complete and history_complete
                    else int(job.get("finalization_attempt") or 0) + 1
                ),
                "finalization_next_attempt": (
                    0
                    if capsule_complete and history_complete
                    else time.time()
                    + min(
                        3600.0,
                        30.0 * (2 ** min(7, int(job.get("finalization_attempt") or 0))),
                    )
                ),
            },
            event_type="local_finalization_retried",
            reason=(
                "complete" if capsule_complete and history_complete else "still pending"
            ),
            actor=INSTANCE_ID,
        )
        if capsule_complete and history_complete:
            completed += 1
    return completed


def _retry_pending_local_finalizations() -> int:
    owner = f"{INSTANCE_ID}:local-finalizer"
    token = store.acquire_lease(
        "finalizer:local",
        owner,
        ttl=600.0,
        payload=_controller_identity_payload(),
    )
    if not token:
        return 0
    try:
        return _retry_pending_local_finalizations_under_lease()
    finally:
        store.release_lease("finalizer:local", owner, token)


def _reconcile_recovered_local_cancellations() -> int:
    """Close identity-checked cancellations after a detached process exits."""
    reconciled = 0
    jobs = store.list_jobs(
        executor="local",
        states=("cancel_requested", "cancelling"),
        limit=1000,
    )
    for job in jobs:
        session = mgr.get(str(job.get("session_id") or job["id"]))
        if session and _session_controls_job(session, job):
            # The PTY reader owns the exact exit status for attached children.
            continue
        dispatching = bool(job.get("cancel_dispatching"))
        dispatch_owner = str(job.get("cancel_dispatch_owner") or "")
        if dispatching and _controller_owner_alive(dispatch_owner):
            continue
        if dispatching:
            job = store.update_job(
                job["id"],
                payload_update={
                    "cancel_dispatching": False,
                    "cancel_dispatch_owner": "",
                },
                event_type="stale_cancel_dispatch_reclaimed",
                reason="dispatch controller heartbeat expired",
                actor=INSTANCE_ID,
            )
        if _durable_process_alive(job):
            continue
        signalled = bool(job.get("cancel_signalled"))
        pending_target = str(job.get("cancel_pending_terminal_state") or "")
        target = (
            "cancelled"
            if signalled
            else pending_target
            if pending_target in {"succeeded", "failed", "blocked"}
            else "lost"
        )
        try:
            store.transition_job(
                job["id"],
                target,
                expected_states=(str(job.get("state")),),
                event_type="recovered_cancel_exit_observed",
                reason=(
                    "identity-checked process exited after cancellation"
                    if signalled
                    else "process outcome cannot be recovered"
                ),
                actor=INSTANCE_ID,
            )
            reconciled += 1
        except store.InvalidTransition:
            pass
    return reconciled


def _reconcile_orphaned_local_owners() -> int:
    """Fence dead controllers and make their queued/active jobs explicit."""
    owner = f"{INSTANCE_ID}:orphan-reconcile"
    token = store.acquire_lease(
        "controller:orphan-reconcile",
        owner,
        ttl=10.0,
        payload=_controller_identity_payload(),
    )
    if not token:
        return 0
    reconciled = 0
    try:
        jobs = store.list_jobs(
            executor="local",
            states=(
                "created",
                "queued",
                "preparing",
                "running",
                "cancel_requested",
                "cancelling",
            ),
            limit=1000,
        )
        for job in jobs:
            session = mgr.get(str(job.get("session_id") or job["id"]))
            if session and _session_controls_job(session, job):
                continue
            if _job_owned_by_live_controller(job):
                continue
            state = str(job.get("state") or "")
            old_owner = str(job.get("lease_owner") or "")
            if state == "queued":
                if old_owner:
                    store.release_job_lease(job["id"], old_owner)
                    reconciled += 1
                continue
            if state == "created" and job.get("kind") == "run":
                try:
                    store.transition_job(
                        job["id"],
                        "queued",
                        expected_states=("created",),
                        event_type="orphaned_acceptance_recovered",
                        reason="dead controller had not crossed the spawn barrier",
                        actor=INSTANCE_ID,
                        queued_at=time.time(),
                        lease_owner="",
                        lease_expires=None,
                    )
                    reconciled += 1
                except store.InvalidTransition:
                    pass
                continue
            alive = _durable_process_alive(job)
            try:
                store.transition_job(
                    job["id"],
                    "lost",
                    expected_states=(state,),
                    event_type="controller_heartbeat_expired",
                    reason=(
                        "owner heartbeat expired while exact process identity remains alive"
                        if alive
                        else "owner heartbeat expired and process identity is absent"
                    ),
                    actor=INSTANCE_ID,
                    payload_update={"process_alive_at_recovery": alive},
                    lease_owner="",
                    lease_expires=None,
                    last_error="local PTY controller ownership expired",
                )
                reconciled += 1
            except store.InvalidTransition:
                pass
        return reconciled
    finally:
        store.release_lease("controller:orphan-reconcile", owner, token)


async def _controller_maintenance_loop() -> None:
    """Periodic liveness and scheduler reconciliation for quiet installations."""
    next_slurm = 0.0
    while True:
        await asyncio.sleep(5.0)
        try:
            _renew_controller_heartbeat()
            _renew_owned_session_leases()
            _reconcile_orphaned_local_owners()
            _reconcile_recovered_local_cancellations()
            await _await_blocking(_retry_pending_local_finalizations)
            _maybe_start_queued_runs()
        except Exception:
            # Individual jobs retain durable errors/events; the controller loop
            # must remain alive so later state changes can make progress.
            pass
        if time.monotonic() >= next_slurm:
            try:
                await _await_blocking(_reconcile_slurm_jobs)
            except Exception:
                pass
            next_slurm = time.monotonic() + 15.0


async def _recover_durable_state() -> dict:
    store.initialize()
    settings.harden_existing_state_permissions()
    _renew_controller_heartbeat()
    token = store.acquire_lease(
        "controller:startup-reconcile",
        INSTANCE_ID,
        ttl=60.0,
        payload={"pid": os.getpid()},
    )
    if not token:
        return {"reconciled": False, "reason": "another controller owns recovery"}
    recovered = {
        "queued": 0,
        "lost": 0,
        "slurm": 0,
        "submission_unknown": 0,
        "finalized": 0,
        "owned_elsewhere": 0,
    }
    try:
        local = store.list_jobs(
            executor="local",
            states=(
                "created",
                "queued",
                "preparing",
                "running",
                "cancel_requested",
                "cancelling",
            ),
            limit=1000,
            include_private=True,
        )
        for job in local:
            state = str(job.get("state") or "")
            if _job_owned_by_live_controller(job):
                recovered["owned_elsewhere"] += 1
                continue
            if state == "created":
                if job.get("kind") != "run":
                    try:
                        store.transition_job(
                            job["id"],
                            "lost",
                            expected_states=("created",),
                            reason="interactive session acceptance was interrupted",
                            actor=INSTANCE_ID,
                            last_error="controller restarted before interactive process start",
                        )
                        recovered["lost"] += 1
                    except store.InvalidTransition:
                        pass
                    continue
                try:
                    job = store.transition_job(
                        job["id"],
                        "queued",
                        expected_states=("created",),
                        reason="recovered pre-spawn accepted job",
                        actor=INSTANCE_ID,
                        queued_at=time.time(),
                    )
                    state = "queued"
                except store.InvalidTransition:
                    continue
            if state == "queued":
                if _restore_queued_session(job):
                    recovered["queued"] += 1
                continue
            alive = _durable_process_alive(job)
            try:
                store.transition_job(
                    job["id"],
                    "lost",
                    expected_states=(state,),
                    event_type="controller_restart_reconciliation",
                    reason=(
                        "prior process is alive but its PTY controller is not reattachable"
                        if alive
                        else "prior process identity is absent or no longer matches"
                    ),
                    actor=INSTANCE_ID,
                    payload_update={"process_alive_at_recovery": alive},
                    last_error="controller restart interrupted local PTY ownership",
                )
                recovered["lost"] += 1
            except store.InvalidTransition:
                pass

        try:
            recovered["slurm"] = len(_reconcile_slurm_jobs())
        except Exception:
            recovered["slurm"] = 0
        # Anything still between invoking sbatch and recording an executor id is
        # ambiguous.  It is never automatically resubmitted.
        for job in store.list_jobs(
            executor="slurm", states=("submitting",), limit=1000
        ):
            try:
                store.transition_job(
                    job["id"],
                    "submission_unknown",
                    expected_states=("submitting",),
                    reason="restart crossed the sbatch acceptance window",
                    actor=INSTANCE_ID,
                    last_error="scheduler acceptance could not be proven",
                )
                recovered["submission_unknown"] += 1
            except store.InvalidTransition:
                pass
        recovered["finalized"] = await _await_blocking(
            _retry_pending_local_finalizations,
        )
        _maybe_start_queued_runs()
        return {"reconciled": True, **recovered}
    finally:
        store.release_lease("controller:startup-reconcile", INSTANCE_ID, token)


@app.on_event("startup")
async def _startup_reconcile() -> None:
    global STARTUP_RECOVERY
    recovery_complete = False
    try:
        STARTUP_RECOVERY = {"status": "complete", **await _recover_durable_state()}
        recovery_complete = True
    except Exception as exc:
        # The API still starts so the durability status and logs remain
        # inspectable; run creation will fail closed if the database is unusable.
        STARTUP_RECOVERY = {
            "status": "failed",
            "error": provenance.redact_text(str(exc)),
        }
    if not recovery_complete:
        # In particular, never start the periodic scheduler after a root-binding
        # failure: every execution path remains closed until the operator uses
        # the matching root or a distinct state directory.
        return
    task = asyncio.create_task(
        _controller_maintenance_loop(),
        name="benchtop-controller-maintenance",
    )
    CONTROLLER_TASKS.add(task)
    task.add_done_callback(CONTROLLER_TASKS.discard)


@app.on_event("shutdown")
async def _shutdown_record() -> None:
    tasks = list(CONTROLLER_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    CONTROLLER_TASKS.clear()
    try:
        jobs = store.list_jobs(
            executor="local",
            states=("preparing", "running", "cancel_requested", "cancelling"),
            limit=1000,
        )
    except Exception:
        # Shutdown bookkeeping is best-effort. In particular, a rejected
        # cross-root state directory must not turn a separate bind failure into
        # an application-shutdown traceback.
        jobs = ()
    for job in jobs:
        try:
            store.append_event(
                job["id"],
                "controller_shutdown",
                reason="web controller stopped; startup will reconcile process identity",
                actor=INSTANCE_ID,
            )
        except Exception:
            pass
    try:
        store.release_lease(_controller_lease_resource(), INSTANCE_ID)
    except Exception:
        pass


# ---- public UI metadata --------------------------------------------------
@app.get("/api/help")
def help_catalog():
    return help_content.public_help_catalog()


@app.get("/api/meta")
def meta():
    tools = {k: bool(shutil.which(v["exe"])) for k, v in settings.AGENT_TOOLS.items()}
    tools["github"] = bool(shutil.which("gh"))
    sra_tools = _sra_geo_tools()
    tools["prefetch"] = bool(sra_tools["prefetch"])
    tools["fasterq-dump"] = bool(sra_tools["fasterq_dump"])
    database = _public_database_status(store.database_status())
    return {
        "root": str(settings.ROOT),
        "workspace_id": _workspace_identifier(settings.ROOT),
        "version": app.version,
        "api": {
            "results_directories": 2,
            "repository_browse": 1,
            "contextual_help": 1,
            "command_parameter_help": 1,
            "private_code_read": 1,
        },
        "tools": tools,
        "conda": pipelines._conda_exe(),
        "envs": pipelines.real_envs(),
        "run_queue": _run_queue_state(),
        "durability": {
            "database": database,
            "recovery": _public_recovery_status(STARTUP_RECOVERY),
        },
        "slurm": {k: bool(v) for k, v in _slurm_tools().items()},
    }


# static UI (mounted last so /api/* wins)
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
