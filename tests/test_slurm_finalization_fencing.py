# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from benchtop import provenance, server, settings, store
from benchtop.pipelines import Pipeline


class SlurmFinalizationFencingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline_tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.pipeline_tmp.name)
        (self.root / "Snakefile").write_text(
            "rule all:\n    input: []\n",
            encoding="utf-8",
        )
        self.pipeline = Pipeline(
            name="slurm-fence-pipe",
            path=self.root,
            kind="snakemake",
            snakefile="Snakefile",
        )
        self.old_state_dir = settings.STATE_DIR
        self.old_state_file = settings.STATE_FILE
        self.old_run_log_dir = settings.RUN_LOG_DIR
        settings.STATE_DIR = Path(self.state_tmp.name)
        settings.STATE_FILE = settings.STATE_DIR / "state.json"
        settings.RUN_LOG_DIR = settings.STATE_DIR / "sessions"
        store.initialize()

    def tearDown(self) -> None:
        settings.STATE_DIR = self.old_state_dir
        settings.STATE_FILE = self.old_state_file
        settings.RUN_LOG_DIR = self.old_run_log_dir
        self.state_tmp.cleanup()
        self.pipeline_tmp.cleanup()

    def _terminal_job(
        self,
        job_id: str,
        *,
        scheduler_id: str = "74001",
        payload: dict | None = None,
    ) -> dict:
        capsule_id = f"{abs(hash(job_id)):012x}"[-12:]
        job, created = store.create_job(
            job_id=job_id,
            kind="run",
            executor="slurm",
            pipeline=self.pipeline.name,
            state="succeeded",
            idempotency_key=f"key-{job_id}",
            plan_digest="sha256:slurm-fence-plan",
            scheduler_job_id=scheduler_id,
            payload={
                "title": "fenced Slurm run",
                "command": "/usr/bin/true",
                "capsule_id": capsule_id,
                "capsule_finalize_pending": True,
                "submission_tag": f"benchtop:{job_id}",
                "scheduler_observation": {"exit_code": "0:0"},
                **(payload or {}),
            },
            actor="test",
        )
        self.assertTrue(created)
        store.record_slurm_job(
            {
                "id": job_id,
                "job_id": scheduler_id,
                "pipeline": self.pipeline.name,
                "title": "fenced Slurm run",
                "command": "/usr/bin/true",
                "status": "succeeded",
                "output": str(settings.STATE_DIR / f"slurm-{job_id}-%j.out"),
            }
        )
        return job

    @staticmethod
    def _final_capsule(job: dict) -> dict:
        return {
            "id": job["capsule_id"],
            "fingerprint": f"fingerprint-{job['id']}",
            "created": time.time(),
        }

    def test_overlapping_same_process_finalizers_execute_once(self) -> None:
        job = self._terminal_job("same-process-finalizer")
        entered = threading.Event()
        release = threading.Event()
        duplicate = threading.Event()
        call_lock = threading.Lock()
        calls = 0

        def finalize(*_args, **_kwargs):
            nonlocal calls
            with call_lock:
                calls += 1
                position = calls
            if position == 1:
                entered.set()
                self.assertTrue(release.wait(timeout=3))
            else:
                duplicate.set()
            return self._final_capsule(job)

        with (
            mock.patch(
                "benchtop.server.pipelines.get",
                return_value=self.pipeline,
            ),
            mock.patch(
                "benchtop.server.provenance.finalize_slurm",
                side_effect=finalize,
            ),
            mock.patch.object(
                server,
                "FINALIZATION_LEASE_TTL",
                1.0,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(server._finalize_reconciled_slurm_job, job)
                self.assertTrue(entered.wait(timeout=2))
                # Cross the initial lease horizon while the expensive capsule
                # scan is blocked.  Its heartbeat must retain ownership.
                time.sleep(1.2)
                second = pool.submit(server._finalize_reconciled_slurm_job, job)
                second.result(timeout=2)
                self.assertFalse(duplicate.is_set())
                release.set()
                first.result(timeout=3)

        durable = store.get_job(job["id"])
        events = store.job_events(job["id"])
        self.assertEqual(calls, 1)
        self.assertFalse(durable["capsule_finalize_pending"])
        self.assertEqual(
            sum(event["type"] == "slurm_capsule_finalized" for event in events),
            1,
        )
        self.assertEqual(len(store.history(self.pipeline.name)), 1)

    def test_stale_lease_token_cannot_checkpoint_completion(self) -> None:
        job = self._terminal_job("stale-token-finalizer", scheduler_id="74002")
        resource = f"finalizer:slurm:{job['id']}"
        takeover: dict[str, str] = {}

        def finalize_and_steal(*_args, **_kwargs):
            held = store.lease(resource)
            self.assertIsNotNone(held)
            self.assertTrue(
                store.release_lease(
                    resource,
                    held["owner"],
                    held["token"],
                )
            )
            takeover["owner"] = "other-controller:slurm-finalizer"
            takeover["token"] = str(
                store.acquire_lease(
                    resource,
                    takeover["owner"],
                    ttl=60.0,
                )
            )
            return self._final_capsule(job)

        with (
            mock.patch(
                "benchtop.server.pipelines.get",
                return_value=self.pipeline,
            ),
            mock.patch(
                "benchtop.server.provenance.finalize_slurm",
                side_effect=finalize_and_steal,
            ),
        ):
            server._finalize_reconciled_slurm_job(job)

        durable = store.get_job(job["id"])
        self.assertTrue(durable["capsule_finalize_pending"])
        self.assertFalse(
            any(
                event["type"] == "slurm_capsule_finalized"
                for event in store.job_events(job["id"])
            )
        )
        self.assertTrue(
            store.release_lease(
                resource,
                takeover["owner"],
                takeover["token"],
            )
        )

        with (
            mock.patch(
                "benchtop.server.pipelines.get",
                return_value=self.pipeline,
            ),
            mock.patch(
                "benchtop.server.provenance.finalize_slurm",
                return_value=self._final_capsule(job),
            ),
        ):
            server._finalize_reconciled_slurm_job(durable)

        self.assertFalse(store.get_job(job["id"])["capsule_finalize_pending"])

    def test_failure_is_exponentially_backed_off_then_can_recover(self) -> None:
        job = self._terminal_job("backoff-finalizer", scheduler_id="74003")
        with (
            mock.patch(
                "benchtop.server.pipelines.get",
                return_value=self.pipeline,
            ),
            mock.patch(
                "benchtop.server.provenance.finalize_slurm",
                side_effect=RuntimeError("temporary result scan failure"),
            ) as finalize,
        ):
            server._finalize_reconciled_slurm_job(job)
            server._finalize_reconciled_slurm_job(store.get_job(job["id"]))

        durable = store.get_job(job["id"])
        failures = [
            event
            for event in store.job_events(job["id"])
            if event["type"] == "slurm_capsule_finalize_failed"
        ]
        self.assertEqual(finalize.call_count, 1)
        self.assertEqual(durable["finalization_attempt"], 1)
        self.assertGreater(durable["finalization_next_attempt"], time.time())
        self.assertEqual(len(failures), 1)

        durable = store.update_job(
            job["id"],
            payload_update={"finalization_next_attempt": 0},
            event_type="",
        )
        with (
            mock.patch(
                "benchtop.server.pipelines.get",
                return_value=self.pipeline,
            ),
            mock.patch(
                "benchtop.server.provenance.finalize_slurm",
                return_value=self._final_capsule(job),
            ),
        ):
            server._finalize_reconciled_slurm_job(durable)

        recovered = store.get_job(job["id"])
        self.assertFalse(recovered["capsule_finalize_pending"])
        self.assertEqual(recovered["finalization_attempt"], 0)
        self.assertEqual(recovered["finalization_next_attempt"], 0)

    def test_reconciliation_caps_each_finalization_batch(self) -> None:
        pending = [
            {
                "id": f"pending-{index}",
                "capsule_finalize_pending": True,
                "finalization_next_attempt": 0,
            }
            for index in range(server.FINALIZATION_BATCH_LIMIT + 5)
        ]
        with (
            mock.patch.object(
                store,
                "list_jobs",
                side_effect=[[], pending],
            ),
            mock.patch(
                "benchtop.server._finalize_reconciled_slurm_job",
            ) as finalize,
        ):
            server._reconcile_slurm_jobs([])

        self.assertEqual(finalize.call_count, server.FINALIZATION_BATCH_LIMIT)

    def test_concurrent_capsule_writes_use_distinct_atomic_tempfiles(self) -> None:
        capsule_id = "abcdef123456"
        capsules = [
            {
                "id": capsule_id,
                "pipeline": {"root": str(self.root.resolve())},
                "writer": writer,
            }
            for writer in ("first", "second")
        ]
        original_replace = os.replace
        replace_barrier = threading.Barrier(2)
        sources: list[str] = []
        source_lock = threading.Lock()

        def synchronized_replace(source, target):
            with source_lock:
                sources.append(str(source))
            replace_barrier.wait(timeout=3)
            return original_replace(source, target)

        with mock.patch(
            "benchtop.provenance.os.replace",
            side_effect=synchronized_replace,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(provenance._write, self.pipeline, capsule)
                    for capsule in capsules
                ]
                for future in futures:
                    future.result(timeout=4)

        path = provenance._capsule_path(self.pipeline, capsule_id)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(set(sources)), 2)
        self.assertIn(saved["writer"], {"first", "second"})
        self.assertEqual(list(path.parent.glob(f".{path.name}.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
