# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
from __future__ import annotations

import asyncio
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from benchtop import runplan, server, settings, store
from benchtop.pipelines import Pipeline
from benchtop.server import (
    JobCancelRequest,
    JobResolveRequest,
    RunRequest,
    SlurmSubmitRequest,
)
from benchtop.sessions import Session


class SchedulerDurabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline_tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.pipeline_collection = Path(self.pipeline_tmp.name)
        self.root = self.pipeline_collection / "durability-pipe"
        self.root.mkdir()
        (self.root / "Snakefile").write_text(
            "rule all:\n    input: []\n",
            encoding="utf-8",
        )
        self.pipeline = Pipeline(
            name="durability-pipe",
            path=self.root,
            kind="snakemake",
            snakefile="Snakefile",
        )

        self.old_state_dir = settings.STATE_DIR
        self.old_state_file = settings.STATE_FILE
        self.old_run_log_dir = settings.RUN_LOG_DIR
        self.old_root = settings.ROOT
        settings.ROOT = self.pipeline_collection
        settings.STATE_DIR = Path(self.state_tmp.name)
        settings.STATE_FILE = settings.STATE_DIR / "state.json"
        settings.RUN_LOG_DIR = settings.STATE_DIR / "sessions"

        self.old_sessions = server.mgr.sessions
        server.mgr.sessions = {}
        self.old_scheduler_active = server.LOCAL_SCHEDULER_ACTIVE
        server.LOCAL_SCHEDULER_ACTIVE = False

    def tearDown(self) -> None:
        server.LOCAL_SCHEDULER_ACTIVE = self.old_scheduler_active
        server.mgr.sessions = self.old_sessions
        settings.STATE_DIR = self.old_state_dir
        settings.STATE_FILE = self.old_state_file
        settings.RUN_LOG_DIR = self.old_run_log_dir
        settings.ROOT = self.old_root
        self.state_tmp.cleanup()
        self.pipeline_tmp.cleanup()

    def _plan(self, digest: str = "sha256:durable-plan", *, cores: int = 1):
        record = {
            "schema_version": runplan.SCHEMA_VERSION,
            "digest": digest,
            "payload": {"test": "scheduler durability"},
        }
        return SimpleNamespace(
            digest=digest,
            cwd=str(self.root),
            argv=["/usr/bin/true"],
            payload={
                "environment": {"driver": {"name": ""}},
                "resources": {"cores": cores, "slurm": {}},
            },
            record=lambda: record,
        )

    @staticmethod
    def _resolved(plan):
        return (
            plan,
            "/usr/bin/true",
            {
                "selected": None,
                "roles": {},
                "gate": "ready",
                "fingerprint": "study-fingerprint",
            },
            False,
            "",
        )

    def _local_body(self, key: str, **changes) -> RunRequest:
        values = {
            "cores": 1,
            "dryrun": True,
            "use_conda": False,
            "idempotency_key": key,
        }
        values.update(changes)
        return RunRequest(**values)

    def _execution_record(self, job_id: str) -> dict:
        return {
            "execution": {
                "kind": "run",
                "title": "restored queued run",
                "cwd": str(self.root),
                "argv": ["/usr/bin/true"],
                "env": {},
                "cols": 120,
                "rows": 32,
                "meta": {
                    "pipeline": self.pipeline.name,
                    "cores": 1,
                    "dryrun": True,
                },
                "logfile": str(settings.RUN_LOG_DIR / f"{job_id}.log"),
                "created": time.time(),
            },
        }

    def _create_local_job(self, job_id: str, state: str) -> dict:
        job, created = store.create_job(
            job_id=job_id,
            kind="run",
            executor="local",
            pipeline=self.pipeline.name,
            state=state,
            idempotency_key=f"key-{job_id}",
            plan_digest="sha256:durable-plan",
            session_id=job_id,
            payload={
                "title": "restored queued run",
                "cores": 1,
                "dryrun": True,
                "logfile": str(settings.RUN_LOG_DIR / f"{job_id}.log"),
            },
            private=self._execution_record(job_id),
            actor="test",
        )
        self.assertTrue(created)
        return job

    def _slurm_body(self, key: str) -> SlurmSubmitRequest:
        return SlurmSubmitRequest(
            cores=1,
            dryrun=True,
            use_conda=False,
            idempotency_key=key,
            job_name="durability-test",
        )

    def _create_slurm_job(
        self,
        job_id: str,
        state: str,
        *,
        scheduler_id: str = "",
        payload: dict | None = None,
    ) -> dict:
        saved_payload = {
            "title": "durable Slurm run",
            "command": "/usr/bin/true",
            "capsule_id": f"capsule-{job_id}",
            "submission_tag": f"benchtop:{job_id}",
            **(payload or {}),
        }
        job, created = store.create_job(
            job_id=job_id,
            kind="run",
            executor="slurm",
            pipeline=self.pipeline.name,
            state=state,
            idempotency_key=f"key-{job_id}",
            plan_digest="sha256:slurm-plan",
            scheduler_job_id=scheduler_id,
            payload=saved_payload,
            actor="test",
        )
        self.assertTrue(created)
        store.record_slurm_job(
            {
                "id": job_id,
                "job_id": scheduler_id,
                "pipeline": self.pipeline.name,
                "title": saved_payload["title"],
                "command": saved_payload["command"],
                "status": state,
                "output": str(settings.STATE_DIR / f"slurm-{job_id}-%j.out"),
            }
        )
        return job

    def test_local_same_key_replay_creates_one_session_and_one_side_effect(
        self,
    ) -> None:
        body = self._local_body("local-replay-key")
        plan = self._plan()
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=self.pipeline),
            mock.patch("benchtop.server._run_core_budget", return_value=8),
            mock.patch(
                "benchtop.server._resolve_run_plan", return_value=self._resolved(plan)
            ),
            mock.patch("benchtop.server._queue_or_start_run") as accept,
            mock.patch.object(server.mgr, "create", wraps=server.mgr.create) as create,
        ):
            first = asyncio.run(server.start_run(self.pipeline.name, body))
            replay = asyncio.run(server.start_run(self.pipeline.name, body))

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["session"]["id"], replay["session"]["id"])
        self.assertEqual(len(server.mgr.sessions), 1)
        self.assertEqual(len(store.list_jobs(executor="local", limit=10)), 1)
        create.assert_called_once()
        accept.assert_called_once()

    def test_local_same_key_changed_contract_returns_409(self) -> None:
        first_body = self._local_body("local-conflict-key")
        changed_body = self._local_body("local-conflict-key", cores=2)
        resolved = [
            self._resolved(self._plan("sha256:first-contract", cores=1)),
            self._resolved(self._plan("sha256:changed-contract", cores=2)),
        ]
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=self.pipeline),
            mock.patch("benchtop.server._run_core_budget", return_value=8),
            mock.patch("benchtop.server._resolve_run_plan", side_effect=resolved),
            mock.patch("benchtop.server._queue_or_start_run") as accept,
        ):
            asyncio.run(server.start_run(self.pipeline.name, first_body))
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(server.start_run(self.pipeline.name, changed_body))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(len(server.mgr.sessions), 1)
        self.assertEqual(len(store.list_jobs(executor="local", limit=10)), 1)
        accept.assert_called_once()

    def test_restart_restores_queued_session_without_starting_it(self) -> None:
        job = self._create_local_job("queued-restart", "queued")
        self.assertIsNone(server.mgr.get(job["id"]))

        with (
            mock.patch("benchtop.server._reconcile_slurm_jobs", return_value=[]),
            mock.patch("benchtop.server._maybe_start_queued_runs"),
            mock.patch.object(Session, "start") as start,
        ):
            recovered = asyncio.run(server._recover_durable_state())

        restored = server.mgr.get(job["id"])
        self.assertTrue(recovered["reconciled"])
        self.assertEqual(recovered["queued"], 1)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.id, job["id"])
        self.assertEqual(restored.status, "queued")
        self.assertEqual(store.get_job(job["id"])["state"], "queued")
        start.assert_not_called()

    def test_restart_marks_prior_running_job_lost_and_never_resumes_it(self) -> None:
        job = self._create_local_job("prior-running", "running")

        with (
            mock.patch("benchtop.server._durable_process_alive", return_value=True),
            mock.patch("benchtop.server._reconcile_slurm_jobs", return_value=[]),
            mock.patch("benchtop.server._maybe_start_queued_runs"),
            mock.patch.object(Session, "start") as start,
        ):
            recovered = asyncio.run(server._recover_durable_state())

        self.assertTrue(recovered["reconciled"])
        self.assertEqual(recovered["lost"], 1)
        self.assertEqual(store.get_job(job["id"])["state"], "lost")
        self.assertIsNone(server.mgr.get(job["id"]))
        start.assert_not_called()

    def test_queued_cancellation_never_sends_an_os_signal(self) -> None:
        job = self._create_local_job("queued-cancel", "queued")
        body = JobCancelRequest(interrupt_grace=0, terminate_grace=0)

        with (
            mock.patch("benchtop.server._signal_durable_process") as signal_process,
            mock.patch("benchtop.server.os.kill") as kill,
            mock.patch("benchtop.server.os.killpg") as killpg,
        ):
            result = asyncio.run(server._cancel_durable_job(job, body))

        self.assertTrue(result["ok"])
        self.assertFalse(result["signalled"])
        self.assertEqual(store.get_job(job["id"])["state"], "cancelled")
        signal_process.assert_not_called()
        kill.assert_not_called()
        killpg.assert_not_called()

    def test_unstarted_session_exit_callback_does_not_block_event_loop(self) -> None:
        callback_done = threading.Event()

        def slow_callback(_session: Session) -> None:
            time.sleep(0.4)
            callback_done.set()

        async def cancel_and_measure() -> float:
            session = Session(
                kind="run",
                title="queued block",
                cwd=str(self.root),
                argv=["/usr/bin/true"],
                on_exit=slow_callback,
            )
            session.status = "queued"
            started = time.monotonic()
            session.kill()
            await asyncio.sleep(0.02)
            return time.monotonic() - started

        gap = asyncio.run(cancel_and_measure())
        self.assertLess(gap, 0.15)
        self.assertTrue(callback_done.wait(timeout=2))

    def test_completed_session_late_cancel_never_signals(self) -> None:
        job = self._create_local_job("already-complete", "succeeded")
        session = Session(
            kind="run",
            title="already complete",
            cwd=str(self.root),
            argv=["/usr/bin/true"],
            session_id=job["id"],
        )
        session.status = "exited"
        session.exit_code = 0
        session.ended = time.time()
        server.mgr.register(session)

        with (
            mock.patch.object(session, "cancel", new_callable=mock.AsyncMock) as cancel,
            mock.patch.object(session, "send_signal") as send_signal,
            mock.patch("benchtop.server._signal_durable_process") as durable_signal,
            mock.patch("benchtop.server.os.kill") as kill,
            mock.patch("benchtop.server.os.killpg") as killpg,
        ):
            result = asyncio.run(server.kill_session(job["id"]))

        self.assertTrue(result["already_terminal"])
        self.assertEqual(store.get_job(job["id"])["state"], "succeeded")
        cancel.assert_not_called()
        send_signal.assert_not_called()
        durable_signal.assert_not_called()
        kill.assert_not_called()
        killpg.assert_not_called()

    def test_local_scheduler_lock_serializes_overlapping_wakeups(self) -> None:
        first_acquire_entered = threading.Event()
        second_acquire_entered = threading.Event()
        release_acquire = threading.Event()
        schedule_entered = threading.Event()
        duplicate_schedule_entered = threading.Event()
        release_schedule = threading.Event()
        counter_lock = threading.Lock()
        acquire_count = 0
        active_schedulers = 0
        max_active_schedulers = 0

        def acquire_lease(*_args, **_kwargs):
            nonlocal acquire_count
            with counter_lock:
                acquire_count += 1
                position = acquire_count
            if position == 1:
                first_acquire_entered.set()
            else:
                second_acquire_entered.set()
            self.assertTrue(release_acquire.wait(timeout=2))
            # The real store currently returns the same lease token for the same
            # owner, so both same-process callers may appear to own this lease.
            return "same-owner-token"

        def schedule_under_lock():
            nonlocal active_schedulers, max_active_schedulers
            with counter_lock:
                active_schedulers += 1
                max_active_schedulers = max(max_active_schedulers, active_schedulers)
                if active_schedulers > 1:
                    duplicate_schedule_entered.set()
            schedule_entered.set()
            self.assertTrue(release_schedule.wait(timeout=2))
            with counter_lock:
                active_schedulers -= 1

        with (
            mock.patch(
                "benchtop.server.store.acquire_lease",
                side_effect=acquire_lease,
            ),
            mock.patch(
                "benchtop.server.store.release_lease",
                return_value=True,
            ),
            mock.patch(
                "benchtop.server._schedule_queued_runs_under_lock",
                side_effect=schedule_under_lock,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(server._maybe_start_queued_runs)
                self.assertTrue(first_acquire_entered.wait(timeout=2))
                second = pool.submit(server._maybe_start_queued_runs)
                # Give a vulnerable implementation the deterministic opportunity
                # to pass the in-process guard before either lease call returns.
                second_acquire_entered.wait(timeout=0.5)
                release_acquire.set()
                self.assertTrue(schedule_entered.wait(timeout=2))
                duplicate_schedule_entered.wait(timeout=0.5)
                release_schedule.set()
                first.result(timeout=2)
                second.result(timeout=2)

        self.assertEqual(max_active_schedulers, 1)

    def test_slow_execution_contract_preparation_runs_off_event_loop(self) -> None:
        job = self._create_local_job("slow-preparation", "queued")
        restored = server._restore_queued_session(store.get_job(job["id"]))
        self.assertIsNotNone(restored)
        gaps = []

        def slow_prepare(_session: Session) -> bool:
            time.sleep(0.4)
            return False

        async def exercise() -> None:
            previous = time.monotonic()
            server._maybe_start_queued_runs()
            deadline = previous + 0.3
            while time.monotonic() < deadline:
                await asyncio.sleep(0.02)
                current = time.monotonic()
                gaps.append(current - previous)
                previous = current
            wait_deadline = time.monotonic() + 2
            while server.LOCAL_SCHEDULER_ACTIVE and time.monotonic() < wait_deadline:
                await asyncio.sleep(0.02)

        with (
            mock.patch(
                "benchtop.server._prepare_run_capsule",
                side_effect=slow_prepare,
            ),
            mock.patch("benchtop.server._run_core_budget", return_value=4),
        ):
            asyncio.run(exercise())

        self.assertLess(max(gaps), 0.15)
        self.assertFalse(server.LOCAL_SCHEDULER_ACTIVE)

    def test_squeue_pending_and_running_observations_are_persisted(self) -> None:
        job = self._create_slurm_job(
            "slurm-live",
            "submitted",
            scheduler_id="41001",
        )
        pending = {
            "job_id": "41001",
            "state": "PENDING",
            "comment": f"benchtop:{job['id']}",
            "reason": "Resources",
        }
        running = {
            "job_id": "41001",
            "state": "RUNNING",
            "comment": f"benchtop:{job['id']}",
            "elapsed": "00:00:07",
        }

        server._reconcile_slurm_jobs([pending])
        pending_job = store.get_job(job["id"])
        pending_tracking = store.slurm_job(intent_id=job["id"])
        server._reconcile_slurm_jobs([running])
        running_job = store.get_job(job["id"])
        running_tracking = store.slurm_job(intent_id=job["id"])

        self.assertEqual(pending_job["state"], "pending")
        self.assertEqual(pending_job["scheduler_observation"]["state"], "PENDING")
        self.assertEqual(pending_tracking["status"], "pending")
        self.assertEqual(running_job["state"], "running")
        self.assertEqual(running_job["scheduler_observation"]["state"], "RUNNING")
        self.assertEqual(running_tracking["status"], "running")

    def test_sacct_terminal_state_finalizes_capsule_and_history_exactly_once(
        self,
    ) -> None:
        job = self._create_slurm_job(
            "slurm-terminal",
            "running",
            scheduler_id="42002",
        )
        accounting = {
            "42002": {
                "job_id": "42002",
                "state": "COMPLETED",
                "exit_code": "0:0",
                "comment": f"benchtop:{job['id']}",
            },
        }

        with (
            mock.patch(
                "benchtop.server._slurm_accounting",
                return_value=accounting,
            ),
            mock.patch(
                "benchtop.server.pipelines.get",
                return_value=self.pipeline,
            ),
            mock.patch(
                "benchtop.server.provenance.finalize_slurm",
                return_value={
                    "id": "cccccccccccc",
                    "fingerprint": "final-capsule-fingerprint",
                },
            ) as finalize,
            mock.patch.object(
                store,
                "record",
                wraps=store.record,
            ) as record_history,
        ):
            server._reconcile_slurm_jobs([])
            server._reconcile_slurm_jobs([])

        durable = store.get_job(job["id"])
        history = store.history(self.pipeline.name, limit=10)
        self.assertEqual(durable["state"], "succeeded")
        self.assertFalse(durable["capsule_finalize_pending"])
        self.assertEqual(durable["scheduler_observation"]["exit_code"], "0:0")
        finalize.assert_called_once_with(
            self.pipeline,
            f"capsule-{job['id']}",
            status="exited",
            job_id="42002",
            exit_code=0,
            logfile=str(settings.STATE_DIR / f"slurm-{job['id']}-42002.out"),
        )
        record_history.assert_called_once()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], "slurm-42002")
        self.assertEqual(history[0]["status"], "exited")

    def test_known_slurm_job_cancel_calls_scancel_once_and_becomes_cancelling(
        self,
    ) -> None:
        job = self._create_slurm_job(
            "slurm-cancel",
            "running",
            scheduler_id="43003",
        )

        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"scancel": "/usr/bin/scancel"},
            ),
            mock.patch(
                "benchtop.server._slurm_live_jobs",
                return_value=[
                    {
                        "job_id": "43003",
                        "state": "RUNNING",
                        "comment": f"benchtop:{job['id']}",
                    }
                ],
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={"ok": True, "output": "", "error": ""},
            ) as scancel,
        ):
            result = asyncio.run(server._cancel_durable_job(job, JobCancelRequest()))

        self.assertEqual(result["job"]["state"], "cancelling")
        self.assertEqual(store.get_job(job["id"])["state"], "cancelling")
        self.assertEqual(store.slurm_job(intent_id=job["id"])["status"], "cancelling")
        scancel.assert_called_once_with(["/usr/bin/scancel", "--", "43003"], timeout=15)

    def test_repeated_slurm_cancel_is_idempotent_and_does_not_resignal(self) -> None:
        job = self._create_slurm_job(
            "slurm-repeat-cancel",
            "pending",
            scheduler_id="44004",
        )

        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"scancel": "/usr/bin/scancel"},
            ),
            mock.patch(
                "benchtop.server._slurm_live_jobs",
                return_value=[
                    {
                        "job_id": "44004",
                        "state": "PENDING",
                        "comment": f"benchtop:{job['id']}",
                    }
                ],
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={"ok": True, "output": "", "error": ""},
            ) as scancel,
        ):
            first = asyncio.run(server._cancel_durable_job(job, JobCancelRequest()))
            replay = asyncio.run(server._cancel_durable_job(job, JobCancelRequest()))

        self.assertEqual(first["job"]["state"], "cancelling")
        self.assertTrue(replay["already_requested"])
        self.assertEqual(replay["job"]["state"], "cancelling")
        scancel.assert_called_once_with(["/usr/bin/scancel", "--", "44004"], timeout=15)

    def test_legacy_unknown_scheduler_id_route_never_calls_scancel(self) -> None:
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"scancel": "/usr/bin/scancel"},
            ),
            mock.patch("benchtop.server._run_slurm_cmd") as scancel,
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(server.slurm_cancel("99999"))

        self.assertEqual(raised.exception.status_code, 404)
        scancel.assert_not_called()

    def test_slurm_same_key_retry_invokes_sbatch_once(self) -> None:
        body = self._slurm_body("slurm-replay-key")
        plan = self._plan("sha256:slurm-plan")
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"sbatch": "/usr/bin/sbatch"},
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={
                    "ok": True,
                    "stdout": "12345\n",
                    "stderr": "",
                    "output": "12345",
                },
            ) as sbatch,
            mock.patch(
                "benchtop.server.provenance.capture",
                return_value={"id": "aaaaaaaaaaaa", "fingerprint": "before-submit"},
            ),
            mock.patch(
                "benchtop.server.provenance.mark_slurm_submitted",
                return_value={"id": "aaaaaaaaaaaa", "fingerprint": "after-submit"},
            ),
        ):
            first = server._submit_slurm_run(
                self.pipeline,
                body,
                {},
                False,
                plan=plan,
                resolved_env="",
            )
            replay = server._submit_slurm_run(
                self.pipeline,
                body,
                {},
                False,
                plan=plan,
                resolved_env="",
            )

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["durable_job"]["id"], replay["durable_job"]["id"])
        sbatch.assert_called_once()

    def test_federated_slurm_cluster_is_bound_and_used_for_cancel(self) -> None:
        body = self._slurm_body("slurm-cluster-key")
        plan = self._plan("sha256:slurm-cluster-plan")
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"sbatch": "/usr/bin/sbatch"},
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={
                    "ok": True,
                    "stdout": "12345;alpha\n",
                    "stderr": "",
                    "output": "12345;alpha",
                },
            ),
            mock.patch(
                "benchtop.server.provenance.capture",
                return_value={"id": "eeeeeeeeeeee", "fingerprint": "before-submit"},
            ),
            mock.patch(
                "benchtop.server.provenance.mark_slurm_submitted",
                return_value={"id": "eeeeeeeeeeee", "fingerprint": "after-submit"},
            ),
        ):
            submitted = server._submit_slurm_run(
                self.pipeline,
                body,
                {},
                False,
                plan=plan,
                resolved_env="",
            )
        durable = submitted["durable_job"]
        self.assertEqual(durable["scheduler_cluster"], "alpha")

        live = [
            {
                "job_id": "12345",
                "state": "RUNNING",
                "cluster": "alpha",
                "comment": f"benchtop:{durable['id']}",
            }
        ]
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"scancel": "/usr/bin/scancel"},
            ),
            mock.patch(
                "benchtop.server._slurm_live_jobs",
                return_value=live,
            ) as live_jobs,
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={"ok": True, "stdout": "", "stderr": "", "output": ""},
            ) as command,
        ):
            cancelled = asyncio.run(
                server._cancel_durable_job(durable, JobCancelRequest()),
            )
        self.assertEqual(cancelled["job"]["state"], "cancelling")
        live_jobs.assert_called_once_with("alpha")
        command.assert_called_once_with(
            ["/usr/bin/scancel", "-M", "alpha", "--", "12345"],
            timeout=15,
        )

    def test_known_federated_id_survives_binding_failure_and_sacct_reconciles(
        self,
    ) -> None:
        body = self._slurm_body("slurm-binding-failure-key")
        plan = self._plan("sha256:slurm-binding-failure-plan")
        transition_job = store.transition_job

        def fail_first_binding(job_id, to_state, **kwargs):
            if to_state == "submitted":
                raise OSError("injected durable binding failure")
            return transition_job(job_id, to_state, **kwargs)

        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"sbatch": "/usr/bin/sbatch"},
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={
                    "ok": True,
                    "stdout": "12345;alpha\n",
                    "stderr": "",
                    "output": "12345;alpha",
                },
            ),
            mock.patch(
                "benchtop.server.provenance.capture",
                return_value={"id": "abababababab", "fingerprint": "before-submit"},
            ),
            mock.patch(
                "benchtop.server.provenance.mark_slurm_submitted",
                return_value={"id": "abababababab", "fingerprint": "after-submit"},
            ),
            mock.patch.object(
                store,
                "transition_job",
                side_effect=fail_first_binding,
            ),
        ):
            submitted = server._submit_slurm_run(
                self.pipeline,
                body,
                {},
                False,
                plan=plan,
                resolved_env="",
            )

        unknown = store.get_job(submitted["durable_job"]["id"])
        self.assertTrue(submitted["persistence_degraded"])
        self.assertEqual(unknown["state"], "submission_unknown")
        self.assertEqual(unknown["scheduler_job_id"], "12345")
        self.assertEqual(unknown["scheduler_cluster"], "alpha")

        accounting = {
            "12345": {
                "job_id": "12345",
                "state": "COMPLETED",
                "exit_code": "0:0",
                "comment": f"benchtop:{unknown['id']}",
                "cluster": "alpha",
            },
        }
        with (
            mock.patch(
                "benchtop.server._slurm_accounting",
                return_value=accounting,
            ) as sacct,
            mock.patch(
                "benchtop.server.pipelines.get",
                return_value=None,
            ),
        ):
            server._reconcile_slurm_jobs([])

        resolved = store.get_job(unknown["id"])
        self.assertEqual(resolved["state"], "succeeded")
        self.assertEqual(resolved["scheduler_observation"]["exit_code"], "0:0")
        sacct.assert_called_once_with(["12345"], cluster="alpha")

    def test_post_sbatch_crash_recovers_binding_from_tracking_record(self) -> None:
        job = self._create_slurm_job("post-sbatch-crash", "submitting")
        tracked = store.slurm_job(intent_id=job["id"])
        store.record_slurm_job(
            {
                **tracked,
                "job_id": "22334",
                "status": "submitted",
                "submission_tag": f"benchtop:{job['id']}",
                "scheduler_cluster": "beta",
                "sbatch_output": "22334;beta",
            }
        )
        accounting = {
            "22334": {
                "job_id": "22334",
                "state": "COMPLETED",
                "exit_code": "0:0",
                "comment": f"benchtop:{job['id']}",
                "cluster": "beta",
            },
        }

        with (
            mock.patch(
                "benchtop.server._slurm_accounting",
                return_value=accounting,
            ) as sacct,
            mock.patch(
                "benchtop.server.pipelines.get",
                return_value=None,
            ),
        ):
            server._reconcile_slurm_jobs([])

        resolved = store.get_job(job["id"])
        self.assertEqual(resolved["state"], "succeeded")
        self.assertEqual(resolved["scheduler_job_id"], "22334")
        self.assertEqual(resolved["scheduler_cluster"], "beta")
        self.assertIn(
            "scheduler_binding_recovered",
            [item["type"] for item in store.job_events(job["id"])],
        )
        sacct.assert_called_once_with(["22334"], cluster="beta")

    def test_slurm_status_exposes_independent_command_capabilities(self) -> None:
        tools = {
            "sbatch": "/usr/bin/sbatch",
            "squeue": None,
            "sacct": "/usr/bin/sacct",
            "scancel": "/usr/bin/scancel",
            "sinfo": "/usr/bin/sinfo",
        }
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value=tools,
            ),
            mock.patch(
                "benchtop.server._reconcile_slurm_jobs",
                return_value=[],
            ),
            mock.patch(
                "benchtop.server._slurm_partitions",
                return_value=[],
            ),
        ):
            status = server.slurm_status()

        self.assertFalse(status["available"])
        self.assertEqual(
            status["capabilities"],
            {
                "submit": True,
                "monitor": False,
                "accounting": True,
                "cancel": False,
                "partitions": True,
            },
        )

    def test_sinfo_warnings_are_not_parsed_as_partition_rows(self) -> None:
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"sinfo": "/usr/bin/sinfo"},
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={
                    "ok": True,
                    "stdout": "gpu*|up|2|64/0/0/64|512000|gpu:a100:4\n",
                    "stderr": "warning|with|enough|pipes|to|look-valid",
                    "output": (
                        "gpu*|up|2|64/0/0/64|512000|gpu:a100:4\n"
                        "warning|with|enough|pipes|to|look-valid"
                    ),
                },
            ),
        ):
            rows = server._slurm_partitions()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["partition"], "gpu")
        self.assertEqual(rows[0]["gres"], "gpu:a100:4")

    def test_repeated_missing_scheduler_observation_is_rate_limited(self) -> None:
        job = self._create_slurm_job(
            "missing-observation",
            "running",
            scheduler_id="59009",
        )
        with mock.patch("benchtop.server._slurm_accounting", return_value={}):
            server._reconcile_slurm_jobs([])
            server._reconcile_slurm_jobs([])
            server._reconcile_slurm_jobs([])
        events = [
            event
            for event in store.job_events(job["id"])
            if event["type"] == "scheduler_observation_missing"
        ]
        self.assertEqual(len(events), 1)

    def test_sbatch_timeout_becomes_submission_unknown_and_retry_does_not_resubmit(
        self,
    ) -> None:
        key = "slurm-timeout-key"
        body = self._slurm_body(key)
        plan = self._plan("sha256:slurm-timeout-plan")
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"sbatch": "/usr/bin/sbatch"},
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={
                    "ok": False,
                    "ambiguous": True,
                    "error": "sbatch timed out",
                },
            ) as sbatch,
            mock.patch(
                "benchtop.server.provenance.capture",
                return_value={"id": "bbbbbbbbbbbb", "fingerprint": "before-submit"},
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                server._submit_slurm_run(
                    self.pipeline,
                    body,
                    {},
                    False,
                    plan=plan,
                    resolved_env="",
                )
            replay = server._submit_slurm_run(
                self.pipeline,
                body,
                {},
                False,
                plan=plan,
                resolved_env="",
            )

        durable = store.job_by_idempotency(key)
        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.detail["state"], "submission_unknown")
        self.assertEqual(durable["state"], "submission_unknown")
        self.assertTrue(replay["idempotent_replay"])
        self.assertTrue(replay["persistence_degraded"])
        sbatch.assert_called_once()

    def test_malformed_successful_sbatch_output_is_ambiguous_not_submitted(
        self,
    ) -> None:
        key = "slurm-malformed-key"
        body = self._slurm_body(key)
        plan = self._plan("sha256:slurm-malformed-plan")
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"sbatch": "/usr/bin/sbatch"},
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={
                    "ok": True,
                    "stdout": "",
                    "stderr": "scheduler warning",
                    "output": "scheduler warning",
                },
            ),
            mock.patch(
                "benchtop.server.provenance.capture",
                return_value={"id": "dddddddddddd", "fingerprint": "before-submit"},
            ),
            mock.patch(
                "benchtop.server.provenance.mark_slurm_submitted",
            ) as mark_submitted,
        ):
            with self.assertRaises(HTTPException) as raised:
                server._submit_slurm_run(
                    self.pipeline,
                    body,
                    {},
                    False,
                    plan=plan,
                    resolved_env="",
                )

        durable = store.job_by_idempotency(key)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(durable["state"], "submission_unknown")
        self.assertFalse(durable.get("scheduler_job_id"))
        mark_submitted.assert_not_called()

    def test_sbatch_os_launch_failure_cannot_strand_submitting_intent(self) -> None:
        key = "slurm-permission-key"
        body = self._slurm_body(key)
        plan = self._plan("sha256:slurm-permission-plan")
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"sbatch": "/usr/bin/sbatch"},
            ),
            mock.patch(
                "benchtop.server.subprocess.run",
                side_effect=PermissionError("sbatch is not executable"),
            ),
            mock.patch(
                "benchtop.server.provenance.capture",
                return_value={"id": "ffffffffffff", "fingerprint": "before-submit"},
            ),
            mock.patch(
                "benchtop.server._mark_slurm_capsule_failed",
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                server._submit_slurm_run(
                    self.pipeline,
                    body,
                    {},
                    False,
                    plan=plan,
                    resolved_env="",
                )

        durable = store.job_by_idempotency(key)
        self.assertEqual(raised.exception.status_code, 500)
        self.assertIn("could not execute", str(raised.exception.detail))
        self.assertEqual(durable["state"], "failed")
        self.assertEqual(
            store.slurm_job(intent_id=durable["id"])["status"],
            "submission-failed",
        )

    def test_submission_unknown_slurm_cancel_never_calls_scancel(self) -> None:
        job, created = store.create_job(
            job_id="unknown-slurm-intent",
            kind="run",
            executor="slurm",
            pipeline=self.pipeline.name,
            state="submission_unknown",
            idempotency_key="unknown-slurm-key",
            plan_digest="sha256:slurm-plan",
            actor="test",
        )
        self.assertTrue(created)

        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"scancel": "/usr/bin/scancel"},
            ),
            mock.patch("benchtop.server._run_slurm_cmd") as scancel,
        ):
            result = asyncio.run(
                server._cancel_durable_job(job, JobCancelRequest()),
            )

        self.assertTrue(result["deferred"])
        self.assertEqual(store.get_job(job["id"])["state"], "cancel_requested")
        scancel.assert_not_called()

    def test_deferred_slurm_cancel_dispatches_after_tag_reconciliation(self) -> None:
        job = self._create_slurm_job("deferred-cancel", "submission_unknown")
        deferred = asyncio.run(server._cancel_durable_job(job, JobCancelRequest()))
        live = [
            {
                "job_id": "55005",
                "state": "PENDING",
                "comment": f"benchtop:{job['id']}",
            }
        ]

        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"scancel": "/usr/bin/scancel"},
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={"ok": True, "output": "", "error": ""},
            ) as scancel,
        ):
            server._reconcile_slurm_jobs(live)
            server._reconcile_slurm_jobs(live)

        reconciled = store.get_job(job["id"])
        self.assertTrue(deferred["deferred"])
        self.assertEqual(reconciled["scheduler_job_id"], "55005")
        self.assertEqual(reconciled["state"], "cancelling")
        scancel.assert_called_once_with(["/usr/bin/scancel", "--", "55005"], timeout=15)

    def test_failed_scancel_can_be_retried_with_same_cancel_intent(self) -> None:
        job = self._create_slurm_job("retry-cancel", "running", scheduler_id="56006")
        secret = "cancel-secret"
        responses = [
            {"ok": False, "output": f"temporary failure token={secret}", "error": ""},
            {"ok": True, "output": f"cancelled token={secret}", "error": ""},
        ]
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"scancel": "/usr/bin/scancel"},
            ),
            mock.patch(
                "benchtop.server._slurm_live_jobs",
                return_value=[
                    {
                        "job_id": "56006",
                        "state": "RUNNING",
                        "comment": f"benchtop:{job['id']}",
                    }
                ],
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                side_effect=responses,
            ) as scancel,
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(server._cancel_durable_job(job, JobCancelRequest()))
            retried = asyncio.run(
                server._cancel_durable_job(
                    store.get_job(job["id"]), JobCancelRequest()
                ),
            )

        self.assertEqual(retried["job"]["state"], "cancelling")
        self.assertNotIn(secret, str(raised.exception.detail))
        self.assertNotIn(secret, retried["output"])
        failed = [
            event
            for event in store.job_events(job["id"])
            if event["type"] == "cancel_executor_failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertNotIn(secret, failed[0]["reason"])
        self.assertEqual(scancel.call_count, 2)

    def test_reused_scheduler_id_with_wrong_tag_is_never_reconciled_or_cancelled(
        self,
    ) -> None:
        job = self._create_slurm_job("tag-mismatch", "running", scheduler_id="57007")
        mismatched = [
            {
                "job_id": "57007",
                "state": "RUNNING",
                "comment": "external-owner",
            }
        ]

        server._reconcile_slurm_jobs(mismatched)
        with (
            mock.patch(
                "benchtop.server._slurm_live_jobs",
                return_value=mismatched,
            ),
            mock.patch("benchtop.server._run_slurm_cmd") as command,
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(server._cancel_durable_job(job, JobCancelRequest()))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(store.get_job(job["id"])["state"], "cancel_requested")
        event_types = [item["type"] for item in store.job_events(job["id"])]
        self.assertIn("scheduler_identity_mismatch", event_types)
        self.assertIn("cancel_ownership_unobserved", event_types)
        command.assert_not_called()

    def test_slurm_requeue_states_return_running_job_to_pending(self) -> None:
        for index, raw_state in enumerate(
            ("REQUEUED", "REQUEUE_FED", "REQUEUE_HOLD", "SPECIAL_EXIT"),
            start=1,
        ):
            with self.subTest(raw_state=raw_state):
                scheduler_id = f"5800{index}"
                job = self._create_slurm_job(
                    f"slurm-requeue-{index}",
                    "running",
                    scheduler_id=scheduler_id,
                )
                server._reconcile_slurm_jobs(
                    [
                        {
                            "job_id": scheduler_id,
                            "state": raw_state,
                            "comment": f"benchtop:{job['id']}",
                        }
                    ]
                )

                durable = store.get_job(job["id"])
                self.assertEqual(durable["state"], "pending")
                self.assertEqual(durable["scheduler_attempt"], 2)
                self.assertIn(
                    "scheduler_requeued",
                    [item["type"] for item in store.job_events(job["id"])],
                )

    def test_allocated_suspended_slurm_states_remain_active(self) -> None:
        for raw_state in ("SIGNALING", "STOPPED", "SUSPENDED"):
            with self.subTest(raw_state=raw_state):
                self.assertEqual(server._canonical_slurm_state(raw_state), "running")

    def test_session_cancel_counts_successful_escalation_as_a_signal(self) -> None:
        session = Session(
            kind="run",
            title="escalation",
            cwd=str(self.root),
            argv=["/usr/bin/true"],
        )
        session.status = "running"
        session.pid = 99124
        session.pgid = 99124
        with (
            mock.patch.object(session, "is_alive", return_value=True),
            mock.patch.object(
                session,
                "send_signal",
                side_effect=[False, True, False],
            ) as send_signal,
        ):
            result = asyncio.run(
                session.cancel(interrupt_grace=0, terminate_grace=0),
            )

        self.assertTrue(result["signalled"])
        self.assertTrue(result["escalated"])
        self.assertTrue(result["requested"])
        self.assertEqual(session.status, "killed")
        self.assertEqual(send_signal.call_count, 3)

    def test_session_cancel_keeps_running_state_when_every_signal_fails(self) -> None:
        session = Session(
            kind="run",
            title="failed signals",
            cwd=str(self.root),
            argv=["/usr/bin/true"],
        )
        session.status = "running"
        session.pid = 99125
        session.pgid = 99125
        with (
            mock.patch.object(session, "is_alive", return_value=True),
            mock.patch.object(
                session,
                "send_signal",
                return_value=False,
            ),
        ):
            result = asyncio.run(
                session.cancel(interrupt_grace=0, terminate_grace=0),
            )

        self.assertFalse(result["signalled"])
        self.assertEqual(session.status, "running")

    def test_recovered_cancel_is_closed_after_process_exit(self) -> None:
        job = self._create_local_job("recovered-cancel", "cancelling")
        store.update_job(job["id"], payload_update={"cancel_signalled": True})
        with mock.patch("benchtop.server._durable_process_alive", return_value=False):
            count = server._reconcile_recovered_local_cancellations()
        self.assertEqual(count, 1)
        self.assertEqual(store.get_job(job["id"])["state"], "cancelled")

    def test_stale_controller_lease_is_reclaimed_by_exact_process_identity(
        self,
    ) -> None:
        store.acquire_lease(
            "scheduler:local",
            "dead-controller",
            ttl=600,
            payload={"pid": 99999999, "process_start": "old", "boot_id": "old-boot"},
        )
        with mock.patch("benchtop.server._schedule_queued_runs_under_lock") as schedule:
            server._maybe_start_queued_runs()
        schedule.assert_called_once_with()
        self.assertIsNone(store.lease("scheduler:local"))

    def test_live_controller_heartbeat_prevents_replacement_worker_orphaning(
        self,
    ) -> None:
        job = self._create_local_job("worker-a-run", "running")
        store.update_job(
            job["id"],
            lease_owner="worker-a",
            lease_expires=time.time() + 60,
        )
        store.acquire_lease(
            "controller:worker-a",
            "worker-a",
            ttl=60,
            payload={
                "pid": os.getpid(),
                "process_start": Session._process_start_token(os.getpid()),
                "boot_id": Session._boot_id(),
            },
        )
        with (
            mock.patch("benchtop.server._reconcile_slurm_jobs", return_value=[]),
            mock.patch(
                "benchtop.server._maybe_start_queued_runs",
            ),
            mock.patch(
                "benchtop.server._retry_pending_local_finalizations", return_value=0
            ),
        ):
            recovered = asyncio.run(server._recover_durable_state())

        self.assertEqual(recovered["owned_elsewhere"], 1)
        self.assertEqual(store.get_job(job["id"])["state"], "running")

        store.release_lease("controller:worker-a", "worker-a")
        with mock.patch("benchtop.server._durable_process_alive", return_value=True):
            reconciled = server._reconcile_orphaned_local_owners()
        orphaned = store.get_job(job["id"])
        self.assertEqual(reconciled, 1)
        self.assertEqual(orphaned["state"], "lost")
        self.assertTrue(orphaned["process_alive_at_recovery"])

    def test_cross_worker_eof_waits_for_durable_signal_outcome(self) -> None:
        job = self._create_local_job("cross-worker-cancel", "running")
        owner_session = Session(
            kind="run",
            title="owner",
            cwd=str(self.root),
            argv=["/usr/bin/true"],
            session_id=job["id"],
        )
        owner_session.status = "exited"
        owner_session.exit_code = 0
        owner_session.ended = time.time()
        job = store.update_job(
            job["id"],
            pid=99126,
            process_start="start-token",
            lease_owner="worker-a",
            lease_expires=time.time() + 60,
            payload_update={"process_boot_id": "boot-token", "process_pgid": 99126},
        )
        observed = []

        def signal_then_owner_eof(current: dict, _sig: int) -> bool:
            checkpoint = store.get_job(current["id"])
            observed.append(bool(checkpoint.get("cancel_dispatching")))
            deferred = server._persist_session_terminal(
                owner_session,
                reason="owner observed EOF during remote cancel",
            )
            self.assertEqual(deferred["state"], "cancel_requested")
            return True

        with (
            mock.patch(
                "benchtop.server._signal_durable_process",
                side_effect=signal_then_owner_eof,
            ),
            mock.patch("benchtop.server._durable_process_alive", return_value=False),
            mock.patch(
                "benchtop.server._retry_pending_local_finalizations",
                return_value=0,
            ),
        ):
            result = asyncio.run(
                server._cancel_durable_job(
                    job,
                    JobCancelRequest(interrupt_grace=0, terminate_grace=0),
                ),
            )

        self.assertEqual(observed, [True])
        self.assertTrue(result["signalled"])
        self.assertEqual(store.get_job(job["id"])["state"], "cancelled")
        self.assertIn(
            "exit_deferred_for_cancel_dispatch",
            [event["type"] for event in store.job_events(job["id"])],
        )

    def test_pty_eof_before_child_exit_does_not_block_event_loop(self) -> None:
        gaps = []

        async def run_session() -> Session:
            session = Session(
                kind="run",
                title="early eof",
                cwd=str(self.root),
                argv=[
                    sys.executable,
                    "-c",
                    "import os,signal,time; signal.signal(signal.SIGHUP, signal.SIG_IGN); "
                    "[os.close(fd) for fd in (0,1,2)]; time.sleep(0.4)",
                ],
            )
            session.start()
            previous = time.monotonic()
            deadline = previous + 2
            while session.ended is None and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
                current = time.monotonic()
                gaps.append(current - previous)
                previous = current
            return session

        session = asyncio.run(run_session())
        self.assertIsNotNone(session.ended)
        self.assertEqual(session.exit_code, 0)
        self.assertLess(max(gaps), 0.15)

    def test_spawn_persistence_failure_kills_and_reaps_post_fork_child(self) -> None:
        captured = []

        def fail_after_fork(session: Session):
            captured.append(session)
            raise RuntimeError("forced persistence failure")

        async def launch() -> None:
            with mock.patch(
                "benchtop.server._persist_session_running",
                side_effect=fail_after_fork,
            ):
                with self.assertRaises(RuntimeError):
                    await server._spawn(
                        kind="agent",
                        title="cleanup",
                        cwd=str(self.root),
                        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                        meta={"pipeline": self.pipeline.name},
                    )

        asyncio.run(launch())
        self.assertEqual(len(captured), 1)
        session = captured[0]
        self.assertFalse(session.is_alive())
        self.assertIsNone(session.fd)
        self.assertIsNone(session.pid)
        self.assertEqual(store.get_job(session.id)["state"], "failed")

    def test_new_session_log_is_private(self) -> None:
        logfile = settings.RUN_LOG_DIR / "private-session.log"

        async def run_session() -> None:
            session = Session(
                kind="run",
                title="private log",
                cwd=str(self.root),
                argv=["/usr/bin/true"],
                logfile=str(logfile),
            )
            session.start()
            deadline = time.monotonic() + 2
            while session.status == "running" and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            self.assertNotEqual(session.status, "running")

        settings.ensure_dirs()
        asyncio.run(run_session())
        self.assertEqual(stat.S_IMODE(logfile.stat().st_mode), 0o600)

    def test_no_signal_cancel_race_preserves_natural_exit_outcome(self) -> None:
        job = self._create_local_job("natural-exit-race", "running")
        session = Session(
            kind="run",
            title="natural exit",
            cwd=str(self.root),
            argv=["/usr/bin/true"],
            session_id=job["id"],
        )
        session.status = "running"
        session.pid = 99123
        session.pgid = 99123
        session.process_start = "start-token"
        session.boot_id = "boot-token"
        server.mgr.register(session)
        job = store.update_job(
            job["id"],
            pid=session.pid,
            process_start=session.process_start,
            payload_update={
                "process_boot_id": session.boot_id,
                "process_pgid": session.pgid,
            },
        )

        with (
            mock.patch.object(session, "is_alive", return_value=False),
            mock.patch.object(session, "send_signal") as send_signal,
        ):
            result = asyncio.run(
                server._cancel_durable_job(
                    job,
                    JobCancelRequest(interrupt_grace=0, terminate_grace=0),
                ),
            )
        self.assertTrue(result["awaiting_exit"])
        self.assertEqual(store.get_job(job["id"])["state"], "cancel_requested")
        send_signal.assert_not_called()

        session.status = "exited"
        session.exit_code = 0
        session.ended = time.time()
        terminal = server._persist_session_terminal(
            session, reason="natural EOF won race"
        )
        self.assertEqual(terminal["state"], "succeeded")

    def test_alive_lost_process_keeps_conservative_core_reservation(self) -> None:
        job = self._create_local_job("alive-lost", "lost")
        store.update_job(
            job["id"],
            payload_update={"cores": 4, "process_alive_at_recovery": True},
        )
        with mock.patch("benchtop.server._durable_process_alive", return_value=True):
            self.assertEqual(server._active_run_cores(), 4)

    def test_dead_lost_job_requires_audited_failure_resolution(self) -> None:
        job = self._create_local_job("dead-lost", "lost")
        with mock.patch("benchtop.server._durable_process_alive", return_value=False):
            result = asyncio.run(
                server.job_resolve(
                    job["id"],
                    JobResolveRequest(
                        action="acknowledge_failed",
                        reason="verified PID absent and output cannot be recovered",
                        expected_version=job["state_version"],
                    ),
                )
            )
        self.assertEqual(result["job"]["state"], "failed")
        self.assertIn(
            "operator_acknowledged_lost",
            [event["type"] for event in store.job_events(job["id"])],
        )

    def test_operator_can_close_proven_not_submitted_slurm_intent(self) -> None:
        job = self._create_slurm_job("never-submitted", "submission_unknown")
        result = asyncio.run(
            server.job_resolve(
                job["id"],
                JobResolveRequest(
                    action="mark_not_submitted",
                    reason="squeue and sacct retained no matching scheduler ownership tag",
                    expected_version=job["state_version"],
                ),
            )
        )
        self.assertEqual(result["job"]["state"], "failed")
        self.assertEqual(
            store.slurm_job(intent_id=job["id"])["status"],
            "not-submitted",
        )


if __name__ == "__main__":
    unittest.main()
