# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from benchtop import local_resources, pipelines, runplan, server, settings, store
from benchtop.sessions import Session


def capacity():
    return {
        "max_cores": 4,
        "max_memory_mb": 4000,
        "max_parallel": 2,
        "detected": {"memory_detected": True, "available_for_launch_mb": 4000},
    }


class ResourceDetectionTest(unittest.TestCase):
    def test_affinity_and_ancestor_cgroup_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            (proc / "self").mkdir(parents=True)
            (proc / "meminfo").write_text(
                "MemTotal: 32000000 kB\nMemAvailable: 24000000 kB\n"
            )
            (proc / "self" / "cgroup").write_text("0::/group/child\n")
            cg = root / "cgroup"
            (cg / "group" / "child").mkdir(parents=True)
            (cg / "group" / "child" / "cpu.max").write_text("400000 100000")
            (cg / "group" / "cpu.max").write_text("200000 100000")
            (cg / "group" / "memory.max").write_text("8000000000")
            (cg / "group" / "memory.current").write_text("3000000000")
            with (
                mock.patch.object(local_resources.os, "cpu_count", return_value=16),
                mock.patch.object(
                    local_resources.os, "sched_getaffinity", return_value=set(range(8))
                ),
            ):
                detected = local_resources.detect(proc, cg)
            self.assertEqual(detected["effective_cpus"], 2)
            self.assertEqual(detected["usable_cpus"], 1)
            self.assertEqual(detected["total_memory_mb"], 8000)
            self.assertEqual(detected["available_memory_mb"], 5000)
            self.assertEqual(detected["usable_memory_mb"], 6000)
            self.assertEqual(detected["available_for_launch_mb"], 3000)

            (cg / "group" / "memory.max").write_text("0")
            detected = local_resources.detect(proc, cg)
            self.assertEqual(detected["usable_memory_mb"], 0)
            self.assertEqual(detected["available_for_launch_mb"], 0)

    def test_missing_memory_does_not_invent_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            detected = local_resources.detect(Path(tmp), Path(tmp))
        self.assertFalse(detected["memory_detected"])
        self.assertEqual(detected["usable_memory_mb"], 0)

    def test_policy_respects_user_caps_and_available_memory(self):
        detected = {
            "usable_cpus": 12,
            "usable_memory_mb": 16000,
            "available_for_launch_mb": 6000,
        }
        result = local_resources.policy(
            {"max_cores": 8, "max_memory_mb": 12000, "max_parallel": 2}, detected
        )
        self.assertEqual(result["recommended"], {"cores": 4, "memory_mb": 3000})
        self.assertEqual(result["max_memory_mb"], 12000)

    def test_admission_checks_memory_cpu_slots_and_same_checkout(self):
        active = [{"pipeline": "a", "cores": 2, "memory_mb": 1000}]
        candidate = {
            "pipeline": "b",
            "cores": 2,
            "memory_mb": 1000,
            "allow_parallel": True,
        }
        self.assertEqual(
            local_resources.admission_reason(candidate, active, capacity()), ""
        )
        for change, message in [
            ({"cores": 3}, "CPU"),
            ({"memory_mb": 3500}, "memory"),
            ({"pipeline": "a"}, "current run"),
            ({"allow_parallel": False}, "not approved"),
            ({"memory_mb": 0}, "no memory reservation"),
        ]:
            with self.subTest(change=change):
                self.assertIn(
                    message,
                    local_resources.admission_reason(
                        {**candidate, **change}, active, capacity()
                    ),
                )
        limited = capacity()
        limited["detected"]["available_for_launch_mb"] = 500
        self.assertIn(
            "available system memory",
            local_resources.admission_reason(candidate, active, limited),
        )
        self.assertIn(
            "parallel run slot",
            local_resources.admission_reason(candidate, active * 2, capacity()),
        )

    def test_legacy_running_job_reserves_entire_memory_budget(self):
        candidate = {
            "pipeline": "b",
            "cores": 1,
            "memory_mb": 1000,
            "allow_parallel": True,
        }
        self.assertIn(
            "memory",
            local_resources.admission_reason(
                candidate, [{"pipeline": "a", "cores": 1}], capacity()
            ),
        )

    def test_snakemake_resources_are_merged_and_memory_cannot_be_overridden(self):
        p = pipelines.Pipeline(
            name="pipe", path=Path("/tmp"), kind="snakemake", snakefile="Snakefile"
        )
        with mock.patch.object(pipelines, "_conda_exe", return_value=None):
            argv, _ = pipelines.build_snakemake_argv(
                p,
                memory_mb=2000,
                target="all",
                extra="--resources gpu=1 --keep-going",
                resolve_default_env=False,
            )
            self.assertEqual(argv.count("--resources"), 1)
            self.assertIn("mem_mb=2000", argv)
            self.assertIn("gpu=1", argv)
            self.assertEqual(argv[-2:], ["--", "all"])
            with self.assertRaises(ValueError):
                pipelines.build_snakemake_argv(
                    p,
                    memory_mb=2000,
                    extra="--resources mem_mb=9999",
                    resolve_default_env=False,
                )

    def test_allocation_changes_runplan_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Snakefile").write_text("rule all:\n    input: []\n")
            pipeline = pipelines.Pipeline(
                name="pipe", path=root, kind="snakemake", snakefile="Snakefile"
            )
            plans = [
                runplan.build(
                    pipeline,
                    launch={
                        "cores": 1,
                        "memory_mb": memory,
                        "dryrun": True,
                        "use_conda": False,
                    },
                    argv=[
                        "snakemake",
                        "--cores",
                        "1",
                        "--resources",
                        f"mem_mb={memory}",
                    ],
                    study_report={},
                    resolved_env="",
                )
                for memory in (1000, 2000)
            ]
            self.assertNotEqual(plans[0].digest, plans[1].digest)
            self.assertEqual(plans[0].payload["resources"]["memory_mb"], 1000)


class MonitorTest(unittest.TestCase):
    @staticmethod
    def process(root, pid, parent, group, start, ticks, rss):
        directory = root / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ["0"] * 24
        fields[0:3] = ["S", str(parent), str(group)]
        fields[11], fields[19], fields[21] = str(ticks), str(start), str(rss)
        (directory / "stat").write_text(f"{pid} (fixture (child)) " + " ".join(fields))

    def test_process_tree_cpu_memory_and_pid_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stat").write_text("cpu 100 0 0 900 0 0 0 0\n")
            self.process(root, 100, 1, 100, 77, 10, 100)
            self.process(root, 101, 100, 101, 88, 20, 50)
            jobs = [{"id": "job", "pid": 100, "process_start": "77"}]
            monitor = local_resources.Monitor()
            with (
                mock.patch.object(
                    local_resources.time, "monotonic", side_effect=[10, 12, 14]
                ),
                mock.patch.object(
                    local_resources.os,
                    "sysconf",
                    side_effect=lambda key: 100 if key == "SC_CLK_TCK" else 4096,
                ),
            ):
                first = monitor.sample(jobs, root)
                self.assertIsNone(first["jobs"]["job"]["cpu_cores"])
                self.process(root, 100, 1, 100, 77, 210, 100)
                self.process(root, 101, 100, 101, 88, 120, 50)
                (root / "stat").write_text("cpu 200 0 0 1000 0 0 0 0\n")
                second = monitor.sample(jobs, root)
                self.assertEqual(second["system_cpu_percent"], 50.0)
                self.assertEqual(second["jobs"]["job"]["cpu_cores"], 1.5)
                self.assertEqual(second["jobs"]["job"]["memory_mb"], 0.6)
                self.assertEqual(second["jobs"]["job"]["processes"], 2)
                self.process(root, 100, 1, 100, 999, 210, 100)
                self.assertFalse(monitor.sample(jobs, root)["jobs"]["job"]["available"])


class AutomaticQueueTest(unittest.TestCase):
    def test_both_scheduler_paths_start_two_and_release_capacity_for_third(self):
        for asynchronous in (False, True):
            with (
                self.subTest(asynchronous=asynchronous),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                with (
                    mock.patch.multiple(
                        settings,
                        ROOT=workspace,
                        STATE_DIR=root / "state",
                        STATE_FILE=root / "state" / "state.json",
                        RUN_LOG_DIR=root / "state" / "sessions",
                    ),
                    mock.patch.object(server.mgr, "sessions", {}),
                    mock.patch.object(server, "_local_capacity", side_effect=capacity),
                ):
                    sessions = []
                    for name in ("a", "b", "c"):
                        checkout = workspace / name
                        checkout.mkdir()
                        s = server.mgr.create(
                            kind="run",
                            title=name,
                            cwd=str(checkout),
                            argv=["/usr/bin/true"],
                            meta={
                                "pipeline": name,
                                "cores": 2,
                                "memory_mb": 1000,
                                "allow_parallel": True,
                            },
                        )
                        s.status = "queued"
                        store.create_job(
                            job_id=s.id,
                            kind="run",
                            executor="local",
                            pipeline=name,
                            state="queued",
                            session_id=s.id,
                            payload=server._session_job_payload(s),
                        )
                        sessions.append(s)

                    def fake_start(session):
                        session.status = "running"
                        session.started = time.time()

                    def schedule():
                        if asynchronous:
                            asyncio.run(server._schedule_queued_runs_under_lock_async())
                        else:
                            server._schedule_queued_runs_under_lock()

                    with (
                        mock.patch.object(
                            server, "_prepare_run_capsule", return_value=True
                        ),
                        mock.patch.object(
                            Session, "start", autospec=True, side_effect=fake_start
                        ),
                    ):
                        schedule()
                        self.assertEqual(
                            [s.status for s in sessions],
                            ["running", "running", "queued"],
                        )
                        store.transition_job(sessions[0].id, "succeeded")
                        sessions[0].status = "exited"
                        schedule()
                        self.assertEqual(sessions[2].status, "running")
                        self.assertEqual(server._active_run_cores(), 4)

    def test_pipeline_exit_invalidates_result_inventory(self):
        with (
            mock.patch.object(server.results, "invalidate_inventory") as invalidate,
            mock.patch.object(server, "_persist_session_terminal", return_value=None),
        ):
            server._record_run_on_exit("pipe", {})(mock.Mock())
        invalidate.assert_called_once()
