# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from benchtop import runplan, server, settings, store
from benchtop.pipelines import Pipeline
from benchtop.server import SlurmSubmitRequest


class SlurmAcceptanceWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline_tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.pipeline_tmp.name)
        (self.root / "Snakefile").write_text(
            "rule all:\n    input: []\n",
            encoding="utf-8",
        )
        self.pipeline = Pipeline(
            name="slurm-acceptance-pipe",
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

    def tearDown(self) -> None:
        settings.STATE_DIR = self.old_state_dir
        settings.STATE_FILE = self.old_state_file
        settings.RUN_LOG_DIR = self.old_run_log_dir
        self.state_tmp.cleanup()
        self.pipeline_tmp.cleanup()

    def _plan(self, digest: str) -> SimpleNamespace:
        record = {
            "schema_version": runplan.SCHEMA_VERSION,
            "digest": digest,
            "payload": {"test": "Slurm acceptance window"},
        }
        return SimpleNamespace(
            digest=digest,
            cwd=str(self.root),
            argv=["/usr/bin/true"],
            payload={
                "environment": {"driver": {"name": ""}},
                "resources": {"cores": 1, "slurm": {}},
            },
            record=lambda: record,
        )

    @staticmethod
    def _body(key: str) -> SlurmSubmitRequest:
        return SlurmSubmitRequest(
            cores=1,
            dryrun=True,
            use_conda=False,
            idempotency_key=key,
            job_name="acceptance-window",
        )

    @staticmethod
    def _prepared_capsule() -> dict:
        return {"id": "aaaaaaaaaaaa", "fingerprint": "before-submit"}

    def test_capsule_boundary_crash_keeps_binding_and_recovers_from_sacct(self) -> None:
        body = self._body("capsule-boundary-crash")
        plan = self._plan("sha256:capsule-boundary-crash")
        scheduler_result = {
            "ok": True,
            "stdout": "71234;research\n",
            "stderr": "",
            "output": "71234;research",
        }

        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"sbatch": "/usr/bin/sbatch"},
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value=scheduler_result,
            ) as sbatch,
            mock.patch(
                "benchtop.server.provenance.capture",
                return_value=self._prepared_capsule(),
            ),
            mock.patch(
                "benchtop.server.provenance.mark_slurm_submitted",
                side_effect=SystemExit("injected crash after sbatch acceptance"),
            ),
        ):
            with self.assertRaises(SystemExit):
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

        durable = store.job_by_idempotency(body.idempotency_key)
        tracked = store.slurm_job(intent_id=durable["id"])
        self.assertEqual(durable["state"], "submitted")
        self.assertEqual(durable["scheduler_job_id"], "71234")
        self.assertEqual(durable["scheduler_cluster"], "research")
        self.assertEqual(tracked["job_id"], "71234")
        self.assertEqual(tracked["scheduler_cluster"], "research")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["job"]["job_id"], "71234")
        sbatch.assert_called_once()

        accounting = {
            "71234": {
                "job_id": "71234",
                "state": "COMPLETED",
                "exit_code": "0:0",
                "comment": f"benchtop:{durable['id']}",
                "cluster": "research",
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
            # An empty live set models the accepted job having left squeue.
            server._reconcile_slurm_jobs([])

        recovered = store.get_job(durable["id"])
        self.assertEqual(recovered["state"], "succeeded")
        self.assertEqual(recovered["scheduler_observation"]["exit_code"], "0:0")
        sacct.assert_called_once_with(["71234"], cluster="research")

    def test_success_projection_uses_only_validated_redacted_parsable_output(
        self,
    ) -> None:
        secret = "do-not-publish"
        body = self._body("redacted-success-output")
        plan = self._plan("sha256:redacted-success-output")
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"sbatch": "/usr/bin/sbatch"},
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={
                    "ok": True,
                    "stdout": "81234;research\n",
                    "stderr": f"scheduler warning token={secret}",
                    "output": f"81234;research\nscheduler warning token={secret}",
                },
            ),
            mock.patch(
                "benchtop.server.provenance.capture",
                return_value=self._prepared_capsule(),
            ),
            mock.patch(
                "benchtop.server.provenance.mark_slurm_submitted",
                return_value={"id": "aaaaaaaaaaaa", "fingerprint": "after-submit"},
            ),
        ):
            result = server._submit_slurm_run(
                self.pipeline,
                body,
                {},
                False,
                plan=plan,
                resolved_env="",
            )

        durable = store.job_by_idempotency(body.idempotency_key)
        tracked = store.slurm_job(intent_id=durable["id"])
        self.assertEqual(result["sbatch_output"], "81234;research")
        self.assertEqual(result["job"]["sbatch_output"], "81234;research")
        self.assertEqual(durable["sbatch_output"], "81234;research")
        self.assertEqual(tracked["sbatch_output"], "81234;research")
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(secret, repr(durable))
        self.assertNotIn(secret, repr(tracked))

    def test_failed_sbatch_error_is_redacted_before_public_or_durable_use(self) -> None:
        secret = "do-not-publish"
        body = self._body("redacted-failure-output")
        plan = self._plan("sha256:redacted-failure-output")
        with (
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"sbatch": "/usr/bin/sbatch"},
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={
                    "ok": False,
                    "ambiguous": False,
                    "stdout": "",
                    "stderr": f"scheduler rejected token={secret}",
                    "output": f"scheduler rejected token={secret}",
                },
            ),
            mock.patch(
                "benchtop.server.provenance.capture",
                return_value=self._prepared_capsule(),
            ),
            mock.patch(
                "benchtop.server._mark_slurm_capsule_failed",
            ) as mark_failed,
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

        durable = store.job_by_idempotency(body.idempotency_key)
        tracked = store.slurm_job(intent_id=durable["id"])
        self.assertNotIn(secret, str(raised.exception.detail))
        self.assertNotIn(secret, durable["last_error"])
        self.assertNotIn(secret, tracked["submission_error"])
        self.assertIn("<redacted>", str(raised.exception.detail))
        self.assertNotIn(secret, str(mark_failed.call_args))

    def test_missing_environment_user_resolves_euid_and_always_filters_squeue(
        self,
    ) -> None:
        line = "99|job|RUNNING|00:01|01:00|1|2|4G|cpu|None|benchtop:intent\n"
        with (
            mock.patch.dict(os.environ, {"USER": "", "LOGNAME": ""}),
            mock.patch(
                "benchtop.server.os.geteuid",
                return_value=1001,
            ),
            mock.patch(
                "benchtop.server.pwd.getpwuid",
                return_value=SimpleNamespace(pw_name="cluster-user"),
            ) as getpwuid,
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"squeue": "/usr/bin/squeue"},
            ),
            mock.patch(
                "benchtop.server._run_slurm_cmd",
                return_value={"ok": True, "stdout": line, "stderr": "", "output": line},
            ) as command,
        ):
            jobs = server._slurm_live_jobs()

        getpwuid.assert_called_once_with(1001)
        argv = command.call_args.args[0]
        self.assertEqual(argv[argv.index("-u") + 1], "cluster-user")
        self.assertEqual(jobs[0]["job_id"], "99")

    def test_unresolvable_effective_user_fails_closed_without_squeue(self) -> None:
        with (
            mock.patch.dict(os.environ, {"USER": "", "LOGNAME": ""}),
            mock.patch(
                "benchtop.server.pwd.getpwuid",
                side_effect=KeyError(1001),
            ),
            mock.patch(
                "benchtop.server._slurm_tools",
                return_value={"squeue": "/usr/bin/squeue"},
            ),
            mock.patch("benchtop.server._run_slurm_cmd") as command,
        ):
            jobs = server._slurm_live_jobs()

        self.assertEqual(jobs, [])
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
