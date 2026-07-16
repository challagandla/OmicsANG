# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
from __future__ import annotations

import json
import multiprocessing
import sqlite3
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty
from unittest import mock

from benchtop import settings, state_db, store


def _multiprocess_state_writer(
    state_dir: str,
    worker: int,
    entries_per_worker: int,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    """Exercise independent interpreter connections against one state database."""
    root = Path(state_dir)
    settings.STATE_DIR = root
    settings.STATE_FILE = root / "state.json"
    settings.RUN_LOG_DIR = root / "sessions"
    try:
        if not start.wait(timeout=10):
            raise RuntimeError("multiprocess writer start timed out")
        for index in range(entries_per_worker):
            store.record(
                {
                    "id": f"process-{worker}-history-{index}",
                    "kind": "run",
                    "pipeline": "pipe",
                    "status": "exited",
                }
            )
        job, created = store.create_job(
            job_id=f"process-candidate-{worker}",
            kind="run",
            executor="local",
            pipeline="pipe",
            state="created",
            idempotency_key="multiprocess-request",
            payload={"claimant": worker},
        )
        results.put({"ok": True, "job_id": job["id"], "created": created})
    except BaseException as exc:
        results.put({"ok": False, "error": repr(exc)})
        raise


def _multiprocess_initializer(
    state_dir: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    root = Path(state_dir)
    settings.STATE_DIR = root
    settings.STATE_FILE = root / "state.json"
    settings.RUN_LOG_DIR = root / "sessions"
    try:
        if not start.wait(timeout=10):
            raise RuntimeError("initializer start timed out")
        status = store.initialize()
        results.put(
            {
                "ok": True,
                "schema": status["schema_version"],
                "legacy": status["legacy_import"]["status"],
            }
        )
    except BaseException as exc:
        results.put({"ok": False, "error": repr(exc)})
        raise


class StateDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state_dir = settings.STATE_DIR
        self.old_state_file = settings.STATE_FILE
        self.old_run_log_dir = settings.RUN_LOG_DIR
        settings.STATE_DIR = Path(self.tmp.name)
        settings.STATE_FILE = settings.STATE_DIR / "state.json"
        settings.RUN_LOG_DIR = settings.STATE_DIR / "sessions"

    def tearDown(self) -> None:
        settings.STATE_DIR = self.old_state_dir
        settings.STATE_FILE = self.old_state_file
        settings.RUN_LOG_DIR = self.old_run_log_dir
        self.tmp.cleanup()

    def test_wal_schema_status_and_private_database_files(self) -> None:
        status = store.database_status()

        self.assertEqual(status["journal_mode"], "wal")
        self.assertEqual(status["schema_version"], state_db.SCHEMA_VERSION)
        self.assertEqual([row["version"] for row in status["migrations"]], [1])
        self.assertEqual(status["legacy_import"]["status"], "absent")

        db_path = Path(status["path"])
        self.assertEqual(db_path, settings.STATE_DIR / state_db.DATABASE_NAME)
        self.assertTrue(db_path.is_file())
        self.assertEqual(stat.S_IMODE(db_path.stat().st_mode), 0o600)

        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertTrue(
            {
                "schema_migrations",
                "metadata",
                "configuration",
                "history",
                "run_templates",
                "slurm_jobs",
                "jobs",
                "job_events",
                "leases",
            }.issubset(tables)
        )

        conn = state_db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO configuration(key,value_json,updated_at) "
                "VALUES('permission-probe','{}',1.0)"
            )
            conn.commit()
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(db_path) + suffix)
                if sidecar.exists():
                    self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)
        finally:
            conn.close()

    def test_legacy_json_import_is_ordered_non_destructive_and_once_only(self) -> None:
        legacy = {
            "history": [
                {"id": "newest", "kind": "run", "pipeline": "pipe", "status": "exited"},
                {
                    "id": "middle",
                    "kind": "agent",
                    "pipeline": "pipe",
                    "status": "failed",
                },
                {
                    "id": "oldest",
                    "kind": "fleet",
                    "pipeline": "(fleet)",
                    "status": "launched",
                },
            ],
            "run_queue": {"max_cores": 7},
            "run_templates": {
                "pipe": [
                    {
                        "id": "new",
                        "name": "New",
                        "created": 3.0,
                        "updated": 4.0,
                        "form": {"cores": 4},
                    },
                    {
                        "id": "old",
                        "name": "Old",
                        "created": 1.0,
                        "updated": 2.0,
                        "form": {"cores": 1},
                    },
                ],
            },
            "slurm_jobs": [
                {
                    "id": "intent-new",
                    "job_id": "22",
                    "pipeline": "pipe",
                    "status": "submitted",
                    "created": 4.0,
                },
                {
                    "id": "intent-old",
                    "job_id": "11",
                    "pipeline": "pipe",
                    "status": "submission-failed",
                    "created": 2.0,
                },
            ],
        }
        original = json.dumps(legacy, indent=2)
        settings.STATE_FILE.write_text(original, encoding="utf-8")

        first = store.initialize()

        self.assertEqual(settings.STATE_FILE.read_text(encoding="utf-8"), original)
        self.assertEqual(first["legacy_import"]["status"], "imported")
        self.assertEqual(
            [entry["id"] for entry in store.history(limit=10)],
            ["newest", "middle", "oldest"],
        )
        self.assertEqual(store.run_queue_config(1), {"max_cores": 7})
        self.assertEqual(
            [item["id"] for item in store.run_templates("pipe")], ["new", "old"]
        )
        self.assertEqual(
            [item["id"] for item in store.slurm_jobs("pipe", limit=10)],
            ["intent-new", "intent-old"],
        )

        changed = dict(legacy)
        changed["history"] = [
            {
                "id": "late-json-write",
                "kind": "run",
                "pipeline": "pipe",
                "status": "exited",
            },
            *legacy["history"],
        ]
        settings.STATE_FILE.write_text(json.dumps(changed), encoding="utf-8")
        second = store.initialize()

        self.assertEqual(second["legacy_import"], first["legacy_import"])
        self.assertEqual(
            [entry["id"] for entry in store.history(limit=10)],
            ["newest", "middle", "oldest"],
        )

    def test_malformed_legacy_json_is_ignored_until_repaired(self) -> None:
        malformed = '{"history":[{"id":"truncated"}'
        settings.STATE_FILE.write_text(malformed, encoding="utf-8")

        first = store.initialize()

        self.assertIsNone(first["legacy_import"])
        self.assertEqual(settings.STATE_FILE.read_text(encoding="utf-8"), malformed)
        store.record(
            {
                "id": "database-remains-usable",
                "kind": "run",
                "pipeline": "pipe",
                "status": "exited",
            }
        )
        self.assertEqual(
            [entry["id"] for entry in store.history("pipe")],
            ["database-remains-usable"],
        )

        repaired = {
            "history": [
                {
                    "id": "repaired-import",
                    "kind": "run",
                    "pipeline": "pipe",
                    "status": "exited",
                },
            ],
        }
        repaired_text = json.dumps(repaired, indent=2)
        settings.STATE_FILE.write_text(repaired_text, encoding="utf-8")

        second = store.initialize()

        self.assertIsNotNone(
            second["legacy_import"],
            "repaired legacy JSON was not retried after the malformed read",
        )
        self.assertEqual(second["legacy_import"]["status"], "imported")
        self.assertEqual(settings.STATE_FILE.read_text(encoding="utf-8"), repaired_text)
        self.assertEqual(
            {entry["id"] for entry in store.history("pipe")},
            {"database-remains-usable", "repaired-import"},
        )

    def test_malformed_legacy_timestamps_are_safely_normalized(self) -> None:
        settings.STATE_FILE.write_text(
            json.dumps(
                {
                    "run_templates": {
                        "pipe": [
                            {
                                "id": "bad-time",
                                "name": "Imported",
                                "created": "not-a-number",
                                "updated": {"invalid": True},
                                "form": {"cores": 2},
                            }
                        ],
                    },
                    "slurm_jobs": [
                        {
                            "id": "legacy-slurm",
                            "job_id": "123",
                            "pipeline": "pipe",
                            "status": "submitted",
                            "created": "also-invalid",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        status = store.initialize()

        self.assertEqual(status["legacy_import"]["status"], "imported")
        self.assertEqual(store.run_templates("pipe")[0]["id"], "bad-time")
        self.assertEqual(store.slurm_jobs("pipe")[0]["id"], "legacy-slurm")
        durable = store.get_job("legacy-slurm")
        self.assertEqual(durable["executor"], "slurm")
        self.assertEqual(durable["scheduler_job_id"], "123")

    def test_future_database_schema_is_rejected_without_downgrade(self) -> None:
        db_path = settings.STATE_DIR / state_db.DATABASE_NAME
        settings.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA user_version = 2")

        with self.assertRaises(store.StateError):
            store.initialize()

        with sqlite3.connect(db_path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_invalid_transition_rolls_back_without_consuming_event_sequence(
        self,
    ) -> None:
        created, was_created = store.create_job(
            job_id="job-transition",
            kind="run",
            executor="local",
            pipeline="pipe",
            state="created",
        )
        self.assertTrue(was_created)
        self.assertEqual(created["state_version"], 0)

        queued = store.transition_job("job-transition", "queued", expected_version=0)
        before = store.job_events("job-transition")
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["state_version"], 1)

        with self.assertRaises(store.InvalidTransition):
            store.transition_job(
                "job-transition",
                "succeeded",
                expected_states=("queued",),
                expected_version=1,
            )

        unchanged = store.get_job("job-transition")
        after_failure = store.job_events("job-transition")
        self.assertEqual(unchanged["state"], "queued")
        self.assertEqual(unchanged["state_version"], 1)
        self.assertEqual(after_failure, before)

        preparing = store.transition_job(
            "job-transition",
            "preparing",
            expected_states=("queued",),
            expected_version=1,
        )
        events = store.job_events("job-transition")
        self.assertEqual(preparing["state_version"], 2)
        self.assertEqual(
            [event["seq"] for event in events], list(range(1, len(events) + 1))
        )
        self.assertEqual(events[-1]["from_state"], "queued")
        self.assertEqual(events[-1]["to_state"], "preparing")

    def test_concurrent_same_idempotency_key_is_claimed_exactly_once(self) -> None:
        workers = 12
        barrier = threading.Barrier(workers)

        def claim(index: int) -> tuple[str, bool]:
            barrier.wait()
            job, created = store.create_job(
                job_id=f"candidate-{index}",
                kind="run",
                executor="local",
                pipeline="pipe",
                state="created",
                idempotency_key="same-request",
                payload={"claimant": index},
            )
            return job["id"], created

        with ThreadPoolExecutor(max_workers=workers) as pool:
            claims = list(pool.map(claim, range(workers)))

        self.assertEqual(sum(created for _, created in claims), 1)
        self.assertEqual(len({job_id for job_id, _ in claims}), 1)
        jobs = store.list_jobs(pipeline="pipe", limit=50)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(store.job_events(jobs[0]["id"])), 1)
        self.assertEqual(store.job_by_idempotency("same-request")["id"], jobs[0]["id"])

    def test_multiprocess_history_writes_and_idempotency_claim_are_atomic(self) -> None:
        store.initialize()
        context = multiprocessing.get_context("spawn")
        workers = 6
        entries_per_worker = 6
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_multiprocess_state_writer,
                args=(self.tmp.name, worker, entries_per_worker, start, results),
            )
            for worker in range(workers)
        ]
        try:
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=30)
            self.assertTrue(
                all(not process.is_alive() for process in processes),
                "multiprocess writers did not finish within 30 seconds",
            )
            self.assertEqual([process.exitcode for process in processes], [0] * workers)

            claims = []
            for _ in range(workers):
                try:
                    claims.append(results.get(timeout=5))
                except Empty:
                    self.fail(
                        "a multiprocess writer exited without reporting its result"
                    )
            self.assertTrue(all(item["ok"] for item in claims), claims)
            self.assertEqual(sum(item["created"] for item in claims), 1)
            self.assertEqual(len({item["job_id"] for item in claims}), 1)

            history_ids = {entry["id"] for entry in store.history("pipe", limit=500)}
            expected_history_ids = {
                f"process-{worker}-history-{index}"
                for worker in range(workers)
                for index in range(entries_per_worker)
            }
            self.assertEqual(history_ids, expected_history_ids)
            jobs = store.list_jobs(pipeline="pipe", limit=100)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(len(store.job_events(jobs[0]["id"])), 1)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            results.close()
            results.join_thread()

    def test_multiprocess_fresh_migration_and_legacy_import_are_fenced(self) -> None:
        settings.STATE_FILE.write_text(
            json.dumps(
                {
                    "history": [
                        {"kind": "run", "pipeline": "pipe", "status": "exited"}
                    ],
                    "run_templates": {
                        "pipe": [{"name": "Imported", "form": {"cores": 2}}]
                    },
                    "slurm_jobs": [{"pipeline": "pipe", "status": "submitted"}],
                }
            ),
            encoding="utf-8",
        )
        context = multiprocessing.get_context("spawn")
        workers = 8
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_multiprocess_initializer,
                args=(self.tmp.name, start, results),
            )
            for _ in range(workers)
        ]
        try:
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=30)
            self.assertEqual([process.exitcode for process in processes], [0] * workers)
            outcomes = [results.get(timeout=5) for _ in range(workers)]
            self.assertTrue(all(item["ok"] for item in outcomes), outcomes)
            self.assertTrue(all(item["schema"] == 1 for item in outcomes))
            self.assertTrue(all(item["legacy"] == "imported" for item in outcomes))
            self.assertEqual(len(store.history("pipe", limit=10)), 1)
            self.assertEqual(len(store.run_templates("pipe")), 1)
            self.assertEqual(len(store.slurm_jobs("pipe", limit=10)), 1)
            self.assertEqual(len(store.list_jobs(executor="slurm", limit=10)), 1)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            results.close()
            results.join_thread()

    def test_job_and_generic_lease_ownership_and_expiry(self) -> None:
        store.create_job(
            job_id="lease-job",
            kind="run",
            executor="local",
            state="queued",
        )

        with mock.patch("benchtop.state_db.time.time", return_value=100.0):
            first = store.acquire_job_lease("lease-job", "worker-a", ttl=1.0)
        self.assertEqual(first["lease_owner"], "worker-a")
        self.assertEqual(first["lease_expires"], 101.0)

        with mock.patch("benchtop.state_db.time.time", return_value=100.5):
            self.assertIsNone(store.acquire_job_lease("lease-job", "worker-b", ttl=1.0))
        with mock.patch("benchtop.state_db.time.time", return_value=102.0):
            replacement = store.acquire_job_lease("lease-job", "worker-b", ttl=2.0)
        self.assertEqual(replacement["lease_owner"], "worker-b")
        self.assertEqual(replacement["lease_expires"], 104.0)
        self.assertFalse(store.release_job_lease("lease-job", "worker-a"))
        self.assertTrue(store.release_job_lease("lease-job", "worker-b"))
        self.assertEqual(store.get_job("lease-job")["lease_owner"], "")

        with mock.patch("benchtop.state_db.time.time", return_value=200.0):
            token_a = store.acquire_lease("reconciler", "worker-a", ttl=1.0)
        with mock.patch("benchtop.state_db.time.time", return_value=200.5):
            self.assertIsNone(store.acquire_lease("reconciler", "worker-b", ttl=1.0))
        with mock.patch("benchtop.state_db.time.time", return_value=202.0):
            token_b = store.acquire_lease("reconciler", "worker-b", ttl=2.0)
        self.assertIsNotNone(token_a)
        self.assertIsNotNone(token_b)
        self.assertNotEqual(token_a, token_b)
        self.assertEqual(store.lease("reconciler")["owner"], "worker-b")
        self.assertFalse(store.release_lease("reconciler", "worker-b", token_a))
        self.assertTrue(store.release_lease("reconciler", "worker-b", token_b))
        self.assertIsNone(store.lease("reconciler"))

    def test_threaded_history_writers_preserve_every_entry(self) -> None:
        workers = 8
        entries = 80
        barrier = threading.Barrier(workers)

        def write_batch(worker: int) -> None:
            barrier.wait()
            for index in range(worker, entries, workers):
                store.record(
                    {
                        "id": f"history-{index}",
                        "kind": "run",
                        "pipeline": "pipe",
                        "status": "exited",
                    }
                )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(write_batch, range(workers)))

        ids = {entry["id"] for entry in store.history("pipe", limit=entries)}
        self.assertEqual(ids, {f"history-{index}" for index in range(entries)})

    def test_compatibility_retention_caps_and_newest_first_ordering(self) -> None:
        store.initialize()
        conn = state_db.connect()
        try:
            with conn:
                conn.executemany(
                    """INSERT INTO history
                       (entry_id,pipeline,kind,status,sort_time,payload_json,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    [
                        (
                            f"history-{index:03d}",
                            "pipe",
                            "run",
                            "exited",
                            float(index),
                            json.dumps(
                                {
                                    "id": f"history-{index:03d}",
                                    "kind": "run",
                                    "pipeline": "pipe",
                                    "status": "exited",
                                }
                            ),
                            float(index),
                        )
                        for index in range(504)
                    ],
                )
                conn.executemany(
                    """INSERT INTO run_templates
                       (pipeline,template_id,name,created,updated,form_json)
                       VALUES(?,?,?,?,?,?)""",
                    [
                        (
                            "pipe",
                            f"template-{index:03d}",
                            f"Template {index}",
                            float(index),
                            float(index),
                            json.dumps({"cores": index + 1}),
                        )
                        for index in range(104)
                    ],
                )
                conn.executemany(
                    """INSERT INTO slurm_jobs
                       (intent_id,scheduler_job_id,pipeline,status,created,updated,payload_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    [
                        (
                            f"intent-{index:03d}",
                            str(50_000 + index),
                            "pipe",
                            "submitted",
                            float(index),
                            float(index),
                            json.dumps(
                                {
                                    "id": f"intent-{index:03d}",
                                    "job_id": str(50_000 + index),
                                    "pipeline": "pipe",
                                    "status": "submitted",
                                }
                            ),
                        )
                        for index in range(504)
                    ],
                )
        finally:
            conn.close()

        with mock.patch("benchtop.state_db.time.time", return_value=10_000.0):
            store.record(
                {
                    "id": "history-504",
                    "kind": "run",
                    "pipeline": "pipe",
                    "status": "exited",
                }
            )
        history = store.history("pipe", limit=10_000)
        self.assertEqual(len(history), 500)
        self.assertEqual(history[0]["id"], "history-504")
        self.assertEqual(history[-1]["id"], "history-005")
        self.assertEqual(store.history("pipe", limit=-1), [])

        with mock.patch("benchtop.state_db.time.time", return_value=10_000.0):
            store.save_run_template(
                "pipe",
                {
                    "id": "template-104",
                    "name": "Template 104",
                    "form": {"cores": 105},
                },
            )
        templates = store.run_templates("pipe")
        self.assertEqual(len(templates), 100)
        self.assertEqual(templates[0]["id"], "template-104")
        self.assertEqual(templates[-1]["id"], "template-005")

        with mock.patch("benchtop.state_db.time.time", return_value=10_000.0):
            store.record_slurm_job(
                {
                    "id": "intent-504",
                    "job_id": str(50_504),
                    "pipeline": "pipe",
                    "status": "submitted",
                }
            )
        slurm = store.slurm_jobs("pipe", limit=10_000)
        self.assertEqual(len(slurm), 500)
        self.assertEqual(slurm[0]["id"], "intent-504")
        self.assertEqual(slurm[-1]["id"], "intent-005")
        self.assertEqual(store.slurm_jobs("pipe", limit=-1), [])


if __name__ == "__main__":
    unittest.main()
