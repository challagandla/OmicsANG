# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from benchtop import __main__ as cli
from benchtop import server, settings, store
from benchtop.sessions import Session


class DurableStateRootBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root_a = self.base / "root-a"
        self.root_b = self.base / "root-b"
        self.state = self.base / "state"
        for root in (self.root_a, self.root_b):
            pipeline = root / "pipe"
            pipeline.mkdir(parents=True)
            (pipeline / "Snakefile").write_text(
                "rule all:\n    input: []\n",
                encoding="utf-8",
            )

        self.old_settings = {
            "ROOT": settings.ROOT,
            "STATE_DIR": settings.STATE_DIR,
            "STATE_FILE": settings.STATE_FILE,
            "RUN_LOG_DIR": settings.RUN_LOG_DIR,
        }
        self.old_sessions = server.mgr.sessions
        self.old_startup_recovery = server.STARTUP_RECOVERY
        server.mgr.sessions = {}
        self._configure(self.root_a)

    def tearDown(self) -> None:
        server.mgr.sessions = self.old_sessions
        server.STARTUP_RECOVERY = self.old_startup_recovery
        for name, value in self.old_settings.items():
            setattr(settings, name, value)
        self.tmp.cleanup()

    def _configure(self, root: Path) -> None:
        settings.ROOT = root
        settings.STATE_DIR = self.state
        settings.STATE_FILE = self.state / "state.json"
        settings.RUN_LOG_DIR = self.state / "sessions"

    def _create_queued_job(
        self,
        job_id: str,
        *,
        cwd: Path,
        run_plan_pipeline: dict[str, str] | None = None,
    ) -> dict:
        execution = {
            "kind": "run",
            "title": "root-bound queued run",
            "cwd": str(cwd),
            "argv": ["/usr/bin/true"],
            "env": {},
            "cols": 120,
            "rows": 32,
            "meta": {"pipeline": "pipe", "cores": 1, "dryrun": True},
            "logfile": str(self.state / "sessions" / f"{job_id}.log"),
            "created": time.time(),
        }
        if run_plan_pipeline is not None:
            execution["run_plan_pipeline"] = run_plan_pipeline
        job, created = store.create_job(
            job_id=job_id,
            kind="run",
            executor="local",
            pipeline="pipe",
            state="queued",
            plan_digest="sha256:test-plan",
            session_id=job_id,
            payload={"title": "root-bound queued run", "cores": 1, "dryrun": True},
            private={"execution": execution},
            actor="test",
        )
        self.assertTrue(created)
        return job

    def test_restart_with_same_state_and_different_root_fails_before_spawn(
        self,
    ) -> None:
        job = self._create_queued_job(
            "cross-root-restart",
            cwd=self.root_a / "pipe",
        )
        bound = store.database_status()["root_binding"]
        self.assertEqual(bound["canonical_root"], self.root_a.resolve().as_posix())

        self._configure(self.root_b)
        with (
            mock.patch.object(Session, "start") as start,
            mock.patch("benchtop.server.asyncio.create_task") as create_task,
        ):
            asyncio.run(server._startup_reconcile())

        self.assertEqual(server.STARTUP_RECOVERY["status"], "failed")
        self.assertIn("different pipeline root", server.STARTUP_RECOVERY["error"])
        start.assert_not_called()
        create_task.assert_not_called()
        self.assertFalse(server.mgr.sessions)

        # The rejected launch did not mutate or consume the original root's job.
        self._configure(self.root_a)
        self.assertEqual(store.get_job(job["id"])["state"], "queued")

    def test_shutdown_after_root_mismatch_is_best_effort(self) -> None:
        self._create_queued_job(
            "cross-root-shutdown",
            cwd=self.root_a / "pipe",
        )
        self._configure(self.root_b)

        with mock.patch.object(store, "release_lease") as release_lease:
            asyncio.run(server._shutdown_record())

        release_lease.assert_called_once()

    def test_cli_preflight_rejects_cross_root_state_cleanly(self) -> None:
        original = store.initialize()["root_binding"]
        self._configure(self.root_b)

        with self.assertRaises(SystemExit) as caught:
            cli._preflight_state()

        self.assertIn("different pipeline root", str(caught.exception))
        self._configure(self.root_a)
        self.assertEqual(store.initialize()["root_binding"], original)

    def test_shutdown_lease_failure_does_not_escape(self) -> None:
        with (
            mock.patch.object(store, "list_jobs", return_value=[]),
            mock.patch.object(
                store,
                "release_lease",
                side_effect=RuntimeError("state unavailable"),
            ),
        ):
            asyncio.run(server._shutdown_record())

    def test_canonical_alias_of_same_root_reuses_binding(self) -> None:
        first = store.database_status()["root_binding"]
        alias = self.base / "root-a-alias"
        alias.symlink_to(self.root_a, target_is_directory=True)

        self._configure(alias)
        second = store.database_status()["root_binding"]

        self.assertEqual(first, second)
        self.assertEqual(second["canonical_root"], self.root_a.resolve().as_posix())

    def test_restart_terminalizes_queued_job_with_foreign_cwd_without_spawn(
        self,
    ) -> None:
        job = self._create_queued_job(
            "foreign-cwd",
            cwd=self.root_b / "pipe",
        )
        with (
            mock.patch("benchtop.server._reconcile_slurm_jobs", return_value=[]),
            mock.patch(
                "benchtop.server._maybe_start_queued_runs",
                side_effect=server._maybe_start_queued_runs_sync,
            ),
            mock.patch.object(Session, "start") as start,
        ):
            recovered = asyncio.run(server._recover_durable_state())

        self.assertTrue(recovered["reconciled"])
        self.assertEqual(recovered["queued"], 0)
        self.assertEqual(store.get_job(job["id"])["state"], "blocked")
        self.assertFalse(server.mgr.sessions)
        start.assert_not_called()

    def test_restart_terminalizes_mismatched_runplan_pipeline_without_spawn(
        self,
    ) -> None:
        job = self._create_queued_job(
            "foreign-runplan",
            cwd=self.root_a / "pipe",
            run_plan_pipeline={
                "name": "pipe",
                "local_name": "pipe",
                "root": (self.root_b / "pipe").resolve().as_posix(),
            },
        )
        with (
            mock.patch("benchtop.server._reconcile_slurm_jobs", return_value=[]),
            mock.patch(
                "benchtop.server._maybe_start_queued_runs",
                side_effect=server._maybe_start_queued_runs_sync,
            ),
            mock.patch.object(Session, "start") as start,
        ):
            recovered = asyncio.run(server._recover_durable_state())

        self.assertTrue(recovered["reconciled"])
        self.assertEqual(store.get_job(job["id"])["state"], "blocked")
        self.assertFalse(server.mgr.sessions)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
