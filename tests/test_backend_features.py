# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from benchtop import (
    config_form,
    git_ops,
    pipelines,
    provenance,
    qc,
    server,
    settings,
    store,
    study,
)
from benchtop.pipelines import Pipeline
from benchtop.server import (
    RunRequest,
    SlurmSubmitRequest,
    SraDownloadRequest,
    _clean_download_dir,
    _clean_slurm_field,
    _extract_accessions,
    _missing_path_fixes,
    _scan_snakemake_structure,
    _scan_text_diagnostics,
    _sra_download_plan,
    start_run,
)
from benchtop.sessions import Session


class StoreStateTest(unittest.TestCase):
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

    def test_run_template_round_trip(self) -> None:
        saved = store.save_run_template(
            "pipe",
            {
                "name": "tiny dry-run",
                "form": {"cores": 2, "dryrun": True, "target": "all"},
            },
        )
        self.assertEqual(saved["name"], "tiny dry-run")
        self.assertEqual(store.run_templates("pipe")[0]["form"]["cores"], 2)
        self.assertTrue(store.delete_run_template("pipe", saved["id"]))
        self.assertEqual(store.run_templates("pipe"), [])

    def test_slurm_job_recording(self) -> None:
        store.record_slurm_job(
            {"job_id": "123", "pipeline": "pipe", "status": "submitted"}
        )
        self.assertEqual(store.slurm_jobs("pipe")[0]["job_id"], "123")

    def test_concurrent_history_writes_are_atomic_in_process(self) -> None:
        def write(index: int) -> None:
            store.record({"id": f"run-{index}", "pipeline": "pipe", "status": "exited"})

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(40)))

        ids = {item["id"] for item in store.history("pipe", limit=100)}
        self.assertEqual(ids, {f"run-{index}" for index in range(40)})
        self.assertEqual(store.slurm_jobs("other"), [])


class DiagnosticsTest(unittest.TestCase):
    def test_directive_fix_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snake = root / "Snakefile"
            snake.write_text(
                'rule a:\n    thread: 4\n    shell: "echo ok"\n', encoding="utf-8"
            )
            items: list[dict] = []
            _scan_text_diagnostics(root, snake, "Snakefile", items)
            self.assertEqual(items[0]["code"], "snakemake-directive")
            self.assertEqual(items[0]["fixes"][0]["to_value"], "threads")

    def test_structural_rule_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snake = root / "Snakefile"
            snake.write_text(
                'rule all:\n    input: "x"\n'
                'rule heavy:\n    threads: 64\n    output: "x"\n'
                'rule heavy:\n    shell: "echo duplicate"\n',
                encoding="utf-8",
            )
            items: list[dict] = []
            seen: dict[str, tuple[str, int]] = {}
            _scan_snakemake_structure(
                root, snake, "Snakefile", items, seen, max_cores=8
            )
            codes = {i["code"] for i in items}
            self.assertIn("rule-threads-over-budget", codes)
            self.assertIn("duplicate-rule", codes)
            self.assertTrue(any(i.get("dry_run_target") == "heavy" for i in items))

    def test_missing_path_quick_fix_is_relative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixes = _missing_path_fixes(root, root, "results_dir", "results/")
            self.assertEqual(fixes[0]["kind"], "dir")
            self.assertEqual(fixes[0]["target"], "results")


class ConfigFormInferenceTest(unittest.TestCase):
    def test_species_dropdown_and_path_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "resources" / "genome").mkdir(parents=True)
            (root / "config" / "samples.tsv").write_text(
                "sample\tfastq\n", encoding="utf-8"
            )
            (root / "resources" / "genome" / "hg38.fa").write_text(
                ">chr1\nAC\n", encoding="utf-8"
            )
            (root / "config" / "config.yaml").write_text(
                "species: human\n"
                "samples_tsv: config/samples.tsv\n"
                "genome:\n"
                '  fasta: ""\n',
                encoding="utf-8",
            )
            pipeline = Pipeline(
                name="pipe",
                path=root,
                kind="snakemake",
                configs=["config/config.yaml"],
            )

            form = config_form.build(pipeline, "config/config.yaml")
            fields = {
                f["id"]: f for section in form["sections"] for f in section["fields"]
            }

            self.assertEqual(fields["species"]["type"], "enum")
            self.assertIn("mouse", fields["species"]["enum"])
            self.assertTrue(fields["samples_tsv"]["pathlike"])
            self.assertIn(
                "config/samples.tsv",
                [o["value"] for o in fields["samples_tsv"]["path_options"]],
            )
            self.assertTrue(fields["genome.fasta"]["pathlike"])
            self.assertIn(
                "resources/genome/hg38.fa",
                [o["value"] for o in fields["genome.fasta"]["path_options"]],
            )


class SraGeoTest(unittest.TestCase):
    def test_accession_parser_deduplicates_sra_ids(self) -> None:
        text = "SRR123 ERR456 srr123 GSM9 DRR789 SRX111"
        self.assertEqual(
            _extract_accessions(text), ["SRR123", "ERR456", "DRR789", "SRX111"]
        )
        self.assertEqual(
            _extract_accessions(text, runs_only=True), ["SRR123", "ERR456", "DRR789"]
        )

    def test_download_destination_rejects_pipeline_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pipeline = Pipeline(name="pipe", path=root, kind="snakemake")
            with self.assertRaises(HTTPException):
                _clean_download_dir(pipeline, str(root))

    def test_sra_download_plan_normalizes_destination_and_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pipeline = Pipeline(name="pipe", path=root, kind="snakemake")
            body = SraDownloadRequest(
                accessions="SRR1\nERR2\nSRR1",
                destination="raw_files/sra",
                threads=128,
                use_prefetch=False,
                convert_fastq=False,
            )
            plan = _sra_download_plan(pipeline, body)
            self.assertEqual(plan["accessions"], ["SRR1", "ERR2"])
            self.assertEqual(plan["threads"], 64)
            self.assertEqual(Path(plan["destination"]), root / "raw_files" / "sra")


class ResourcesTest(unittest.TestCase):
    def test_conda_detection_falls_back_to_standard_user_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            mamba = home / "miniforge3" / "bin" / "mamba"
            mamba.parent.mkdir(parents=True)
            mamba.write_text("#!/bin/sh\n", encoding="utf-8")
            mamba.chmod(0o755)
            with (
                mock.patch("benchtop.pipelines.shutil.which", return_value=None),
                mock.patch("benchtop.pipelines.Path.home", return_value=home),
            ):
                detected = pipelines._conda_exe()
        self.assertEqual(detected, str(mamba))

    def test_slurm_field_validation(self) -> None:
        self.assertEqual(_clean_slurm_field("gpu:1", "gres"), "gpu:1")
        with self.assertRaises(HTTPException):
            _clean_slurm_field("bad\nvalue", "partition")

    def test_queued_session_kill_before_start(self) -> None:
        session = Session(kind="run", title="queued", cwd="/tmp", argv=["bash"])
        session.status = "queued"
        session.kill()
        self.assertEqual(session.status, "killed")
        self.assertIsNotNone(session.ended)

    def test_queued_session_subscription_waits_for_execution(self) -> None:
        session = Session(kind="run", title="queued", cwd="/tmp", argv=["bash"])
        session.status = "queued"
        queue, snapshot = session.subscribe()
        self.assertEqual(snapshot, b"")
        self.assertTrue(queue.empty())


class GitOpsTest(unittest.TestCase):
    def test_status_non_repo_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = git_ops.status(Path(tmp))
            self.assertFalse(status["is_repo"])
            self.assertEqual(status["dirty"], 0)
            self.assertEqual(status["dirty_files"], [])

    def test_github_slug_parses_common_remote_urls(self) -> None:
        cases = {
            "git@github.com:owner/repo.git": "owner/repo",
            "https://github.com/owner/repo.git": "owner/repo",
            "ssh://git@github.com/owner/repo.git": "owner/repo",
            "https://github.com/owner/repo/tree/main": "owner/repo",
            "https://gitlab.com/owner/repo.git": "",
        }
        for remote, expected in cases.items():
            with self.subTest(remote=remote):
                self.assertEqual(git_ops.github_slug(remote), expected)

    def test_run_json_parses_stdout_when_stderr_has_warnings(self) -> None:
        with mock.patch(
            "benchtop.git_ops._run_capture",
            return_value=(0, '{"name": "repo"}\n', "warning: ignored\n"),
        ):
            rc, _, data = git_ops._run_json(["gh", "repo", "view"], Path("/tmp"))
        self.assertEqual(rc, 0)
        self.assertEqual(data, {"name": "repo"})

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_connect_github_repo_is_disabled_without_mutating_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )

            result = git_ops.connect_github_repo(root, "owner/repo")
            self.assertFalse(result["ok"])
            self.assertIn("disabled", result["output"])
            remotes = subprocess.run(
                ["git", "remote"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.splitlines()
            self.assertEqual(remotes, [])


class StudyGuardTest(unittest.TestCase):
    def _pipeline(self, root: Path, *, confounded: bool = False) -> Pipeline:
        (root / "config").mkdir(parents=True)
        (root / "data").mkdir()
        rows = []
        for index in range(6):
            condition = "control" if index < 3 else "treated"
            if confounded:
                batch = "A" if condition == "control" else "B"
            else:
                batch = "A" if index in {0, 1, 3, 4} else "B"
            sample = f"S{index + 1}"
            r1 = root / "data" / f"{sample}_R1.fastq.gz"
            r2 = root / "data" / f"{sample}_R2.fastq.gz"
            r1.write_bytes(b"reads-r1")
            r2.write_bytes(b"reads-r2")
            rows.append(
                f"{sample}\t{condition}\t{batch}\tD{index + 1}\tdata/{r1.name}\tdata/{r2.name}"
            )
        (root / "config" / "samples.tsv").write_text(
            "sample_id\tcondition\tbatch\tsubject\tfastq_r1\tfastq_r2\n"
            + "\n".join(rows)
            + "\n",
            encoding="utf-8",
        )
        (root / "config" / "config.yaml").write_text(
            "samples_tsv: config/samples.tsv\n"
            "de:\n"
            "  design: '~ condition + batch + 1'\n",
            encoding="utf-8",
        )
        (root / "Snakefile").write_text("rule all:\n    input: []\n", encoding="utf-8")
        return Pipeline(
            name="pipe",
            path=root,
            kind="snakemake",
            snakefile="Snakefile",
            configs=["config/config.yaml"],
        )

    def test_balanced_study_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = study.audit(self._pipeline(Path(tmp)))
        self.assertEqual(report["gate"], "ready")
        self.assertTrue(report["selected"]["authoritative"])
        self.assertEqual(report["summary"]["biological_units"], 6)
        self.assertEqual(report["summary"]["groups"], 2)
        self.assertTrue(report["summary"]["paired"])
        self.assertTrue(report["design"]["full_rank"])
        self.assertIn("Intercept", report["design"]["columns"])

    def test_exploratory_sheet_is_not_authoritative_for_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._pipeline(Path(tmp))
            alternate = pipeline.path / "config" / "metadata_alt.tsv"
            alternate.write_text(
                (pipeline.path / "config" / "samples.tsv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            report = study.audit(pipeline, sheet="config/metadata_alt.tsv")
            configured_report = study.audit(pipeline)

        candidates = {item["path"]: item for item in report["candidates"]}
        self.assertEqual(report["selected"]["path"], "config/metadata_alt.tsv")
        self.assertFalse(report["selected"]["authoritative"])
        self.assertTrue(candidates["config/samples.tsv"]["authoritative"])
        self.assertFalse(candidates["config/metadata_alt.tsv"]["authoritative"])
        self.assertEqual(configured_report["selected"]["path"], "config/samples.tsv")
        self.assertNotEqual(report["fingerprint"], configured_report["fingerprint"])

    def test_discovered_unconfigured_sheet_is_not_authoritative_for_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._pipeline(Path(tmp))
            (pipeline.path / "config" / "config.yaml").write_text(
                "de:\n  design: '~ condition + batch + 1'\n",
                encoding="utf-8",
            )
            report = study.audit(pipeline)

        self.assertEqual(report["selected"]["path"], "config/samples.tsv")
        self.assertFalse(report["selected"]["authoritative"])

    def test_real_run_rejects_discovered_unconfigured_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._pipeline(Path(tmp))
            (pipeline.path / "config" / "config.yaml").write_text(
                "de:\n  design: '~ condition + batch + 1'\n",
                encoding="utf-8",
            )
            body = RunRequest(configfile="config/config.yaml", cores=1, dryrun=False)
            with (
                mock.patch("benchtop.server._require_pipeline", return_value=pipeline),
                mock.patch("benchtop.server._run_core_budget", return_value=8),
            ):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(start_run("pipe", body))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("does not reference it", raised.exception.detail["message"])

    def test_confounded_design_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = study.audit(self._pipeline(Path(tmp), confounded=True))
        codes = {item["code"] for item in report["issues"]}
        self.assertIn("condition-batch-confounded", codes)
        self.assertIn("rank-deficient-design", codes)
        self.assertEqual(report["gate"], "blocked")

    def test_duplicate_sample_and_reused_input_are_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._pipeline(Path(tmp))
            sheet = pipeline.path / "config" / "samples.tsv"
            text = sheet.read_text(encoding="utf-8")
            text = text.replace("S2\tcontrol", "S1\tcontrol").replace(
                "data/S2_R1.fastq.gz", "data/S1_R1.fastq.gz"
            )
            sheet.write_text(text, encoding="utf-8")
            report = study.audit(pipeline)
        codes = {item["code"] for item in report["issues"]}
        self.assertIn("duplicate-sample-id", codes)
        self.assertIn("reused-input", codes)
        self.assertGreater(report["summary"]["errors"], 0)

    def test_mismatched_multilane_pairs_are_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._pipeline(Path(tmp))
            extra = pipeline.path / "data" / "S1_L2_R1.fastq.gz"
            extra.write_bytes(b"lane-two")
            sheet = pipeline.path / "config" / "samples.tsv"
            text = sheet.read_text(encoding="utf-8").replace(
                "data/S1_R1.fastq.gz",
                "data/S1_R1.fastq.gz;data/S1_L2_R1.fastq.gz",
            )
            sheet.write_text(text, encoding="utf-8")
            report = study.audit(pipeline)
        self.assertIn(
            "lane-count-mismatch", {item["code"] for item in report["issues"]}
        )

    def test_path_aliases_are_duplicate_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._pipeline(Path(tmp))
            (pipeline.path / "data" / "S1_L2_R2.fastq.gz").write_bytes(b"lane-two")
            sheet = pipeline.path / "config" / "samples.tsv"
            text = sheet.read_text(encoding="utf-8")
            text = text.replace(
                "data/S1_R1.fastq.gz",
                "data/S1_R1.fastq.gz;./data/S1_R1.fastq.gz",
                1,
            ).replace(
                "data/S1_R2.fastq.gz",
                "data/S1_R2.fastq.gz;data/S1_L2_R2.fastq.gz",
                1,
            )
            sheet.write_text(text, encoding="utf-8")
            report = study.audit(pipeline)
        self.assertIn(
            "duplicate-lane-path", {item["code"] for item in report["issues"]}
        )

    def test_generic_reference_manifest_is_not_selected_as_a_study(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "reference_manifest.tsv").write_text(
                "filename\turl\tmd5\nref.fa\thttps://example.test/ref.fa\tabc\n",
                encoding="utf-8",
            )
            (root / "config" / "config.yaml").write_text(
                "reference_manifest: config/reference_manifest.tsv\n",
                encoding="utf-8",
            )
            pipeline = Pipeline(
                name="pipe",
                path=root,
                kind="snakemake",
                configs=["config/config.yaml"],
            )
            report = study.audit(pipeline)
        self.assertIsNone(report["selected"])
        self.assertEqual(report["gate"], "unknown")

    def test_missing_configured_sample_sheet_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "config.yaml").write_text(
                "samples_tsv: config/missing.tsv\n",
                encoding="utf-8",
            )
            pipeline = Pipeline(
                name="pipe",
                path=root,
                kind="snakemake",
                configs=["config/config.yaml"],
            )
            report = study.audit(pipeline)
        self.assertEqual(report["gate"], "blocked")
        self.assertIn(
            "missing-study-sheet", {item["code"] for item in report["issues"]}
        )

    def test_no_intercept_and_relevel_formulas_are_estimable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._pipeline(Path(tmp))
            config = pipeline.path / "config" / "config.yaml"
            config.write_text(
                "samples_tsv: config/samples.tsv\nde:\n  design: '~0 + condition + batch'\n",
                encoding="utf-8",
            )
            no_intercept = study.audit(pipeline)
            config.write_text(
                "samples_tsv: config/samples.tsv\nde:\n"
                "  design: 'relevel(condition, ref=\"control\") + batch'\n",
                encoding="utf-8",
            )
            relevel = study.audit(pipeline)
        self.assertTrue(no_intercept["design"]["full_rank"])
        self.assertNotIn("Intercept", no_intercept["design"]["columns"])
        self.assertNotIn(
            "unknown-design-factor", {item["code"] for item in relevel["issues"]}
        )

    def test_real_run_requires_override_for_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._pipeline(Path(tmp))
            missing = pipeline.path / "data" / "S1_R1.fastq.gz"
            missing.unlink()
            body = RunRequest(configfile="config/config.yaml", cores=1, dryrun=False)
            with (
                mock.patch("benchtop.server._require_pipeline", return_value=pipeline),
                mock.patch("benchtop.server._run_core_budget", return_value=8),
            ):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(start_run("pipe", body))
        self.assertEqual(raised.exception.status_code, 409)
        codes = {item["code"] for item in raised.exception.detail["study"]["issues"]}
        self.assertIn("missing-input", codes)

    def test_stale_override_and_reserved_extra_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._pipeline(Path(tmp))
            (pipeline.path / "data" / "S1_R1.fastq.gz").unlink()
            stale = RunRequest(
                configfile="config/config.yaml",
                cores=1,
                dryrun=False,
                study_override=True,
                study_fingerprint="stale",
            )
            reserved = RunRequest(
                configfile="config/config.yaml",
                cores=1,
                dryrun=True,
                extra="--configfile config/other.yaml",
            )
            alternate_sheet = pipeline.path / "config" / "metadata_alt.tsv"
            alternate_sheet.write_text(
                (pipeline.path / "config" / "samples.tsv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            alternate = RunRequest(
                configfile="config/config.yaml",
                cores=1,
                dryrun=True,
                study_sheet="config/metadata_alt.tsv",
            )
            slurm_real = SlurmSubmitRequest(
                configfile="config/config.yaml",
                cores=1,
                dryrun=False,
            )
            with (
                mock.patch("benchtop.server._require_pipeline", return_value=pipeline),
                mock.patch("benchtop.server._run_core_budget", return_value=8),
            ):
                with self.assertRaises(HTTPException) as stale_error:
                    asyncio.run(start_run("pipe", stale))
                with self.assertRaises(HTTPException) as extra_error:
                    asyncio.run(start_run("pipe", reserved))
                with self.assertRaises(HTTPException) as alternate_error:
                    asyncio.run(start_run("pipe", alternate))
                with self.assertRaises(HTTPException) as slurm_error:
                    server.slurm_submit("pipe", slurm_real)
        self.assertEqual(stale_error.exception.status_code, 409)
        self.assertEqual(extra_error.exception.status_code, 400)
        self.assertEqual(alternate_error.exception.status_code, 409)
        self.assertEqual(slurm_error.exception.status_code, 409)

    def test_study_context_requires_explicit_sheet_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._pipeline(Path(tmp))
            alternate = pipeline.path / "config" / "metadata_alt.tsv"
            alternate.write_text(
                (pipeline.path / "config" / "samples.tsv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            exploratory = study.audit(pipeline, sheet="config/metadata_alt.tsv")
            body = RunRequest(
                configfile="config/config.yaml",
                cores=1,
                dryrun=False,
                study_sheet="",
                study_roles=exploratory["roles"],
                study_fingerprint=exploratory["fingerprint"],
            )
            with (
                mock.patch("benchtop.server._require_pipeline", return_value=pipeline),
                mock.patch("benchtop.server._run_core_budget", return_value=8),
            ):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(start_run("pipe", body))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn(
            "explicit audited study sheet", raised.exception.detail["message"]
        )

    def test_queued_run_reaudits_changed_input_content(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as state_tmp,
        ):
            pipeline = self._pipeline(Path(tmp))
            report = study.audit(pipeline)
            session = Session(
                kind="run",
                title="queued",
                cwd=str(pipeline.path),
                argv=["true"],
                meta={
                    "pipeline": "pipe",
                    "configfile": "config/config.yaml",
                    "study_roles": report["roles"],
                    "study_fingerprint": report["fingerprint"],
                    "study_override": False,
                    "dryrun": False,
                },
            )
            session.status = "queued"
            old_state_dir, old_state_file = settings.STATE_DIR, settings.STATE_FILE
            try:
                settings.STATE_DIR = Path(state_tmp)
                settings.STATE_FILE = settings.STATE_DIR / "state.json"
                (pipeline.path / "data" / "S1_R1.fastq.gz").write_bytes(
                    b"mutated-reads"
                )
                with mock.patch("benchtop.server.pipelines.get", return_value=pipeline):
                    allowed = server._prepare_run_capsule(session)
            finally:
                settings.STATE_DIR, settings.STATE_FILE = old_state_dir, old_state_file
        self.assertFalse(allowed)
        self.assertTrue(session.meta["study_changed_while_queued"])

    def test_queued_run_rejects_identical_sheet_selected_by_changed_config(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as state_tmp,
        ):
            pipeline = self._pipeline(Path(tmp))
            report = study.audit(pipeline)
            alternate = pipeline.path / "config" / "metadata_alt.tsv"
            alternate.write_bytes(
                (pipeline.path / "config" / "samples.tsv").read_bytes()
            )
            session = Session(
                kind="run",
                title="queued",
                cwd=str(pipeline.path),
                argv=["true"],
                meta={
                    "pipeline": "pipe",
                    "configfile": "config/config.yaml",
                    "study_sheet": report["selected"]["path"],
                    "study_roles": report["roles"],
                    "study_fingerprint": report["fingerprint"],
                    "study_override": False,
                    "dryrun": False,
                },
            )
            session.status = "queued"
            old_state_dir, old_state_file = settings.STATE_DIR, settings.STATE_FILE
            try:
                settings.STATE_DIR = Path(state_tmp)
                settings.STATE_FILE = settings.STATE_DIR / "state.json"
                config = pipeline.path / "config" / "config.yaml"
                config.write_text(
                    config.read_text(encoding="utf-8").replace(
                        "config/samples.tsv",
                        "config/metadata_alt.tsv",
                    ),
                    encoding="utf-8",
                )
                with mock.patch("benchtop.server.pipelines.get", return_value=pipeline):
                    allowed = server._prepare_run_capsule(session)
            finally:
                settings.STATE_DIR, settings.STATE_FILE = old_state_dir, old_state_file

        self.assertFalse(allowed)
        self.assertTrue(session.meta["study_changed_while_queued"])
        self.assertEqual(session.meta["study_sheet"], "config/samples.tsv")
        self.assertEqual(session.meta["current_study_sheet"], "config/metadata_alt.tsv")


class QcExternalResultsTest(unittest.TestCase):
    def test_gather_labels_internal_and_external_result_roots(self) -> None:
        with (
            tempfile.TemporaryDirectory() as pipeline_tmp,
            tempfile.TemporaryDirectory() as external_tmp,
        ):
            root = Path(pipeline_tmp)
            external = Path(external_tmp)
            (root / "config").mkdir()
            (root / "results").mkdir()
            (root / "config" / "config.yaml").write_text(
                f"results_dir: {json.dumps(str(external))}\n",
                encoding="utf-8",
            )
            (external / "S1.flagstat.txt").write_text(
                "100 + 0 in total\n90 + 0 mapped (90.0% : N/A)\n",
                encoding="utf-8",
            )
            pipeline = Pipeline(
                name="pipe",
                path=root,
                kind="snakemake",
                configs=["config/config.yaml"],
            )

            with (
                mock.patch.object(settings, "RESULTS_ROOTS", (root, external)),
                mock.patch(
                    "benchtop.results.store.results_attachments",
                    return_value=[],
                ),
            ):
                report = qc.gather(pipeline)

        self.assertIn(str(external.resolve()), report["scanned"])
        self.assertIn("results", report["scanned"])
        flagstat = next(
            panel for panel in report["panels"] if panel["id"] == "flagstat"
        )
        self.assertEqual(flagstat["rows"][0][0], "S1")


class ProvenanceCapsuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_state_dir = settings.STATE_DIR
        self.old_state_file = settings.STATE_FILE
        self.old_run_log_dir = settings.RUN_LOG_DIR
        settings.STATE_DIR = Path(self.state_tmp.name)
        settings.STATE_FILE = settings.STATE_DIR / "state.json"
        settings.RUN_LOG_DIR = settings.STATE_DIR / "sessions"
        settings.ensure_dirs()
        (self.root / "config").mkdir()
        (self.root / "results").mkdir()
        (self.root / "Snakefile").write_text(
            "rule all:\n    input: []\n", encoding="utf-8"
        )
        (self.root / "environment.yml").write_text(
            "name: pipe\ndependencies: [snakemake]\n", encoding="utf-8"
        )
        (self.root / "config" / "config.yaml").write_text(
            "threshold: 1\napi_key: do-not-store\n",
            encoding="utf-8",
        )
        self.pipeline = Pipeline(
            name="pipe",
            path=self.root,
            kind="snakemake",
            snakefile="Snakefile",
            configs=["config/config.yaml"],
            env_file="environment.yml",
            conda_env="pipe",
        )

    def tearDown(self) -> None:
        settings.STATE_DIR = self.old_state_dir
        settings.STATE_FILE = self.old_state_file
        settings.RUN_LOG_DIR = self.old_run_log_dir
        self.state_tmp.cleanup()
        self.tmp.cleanup()

    def test_capsule_finalizes_output_delta_and_redacts_secrets(self) -> None:
        capsule = provenance.capture(
            self.pipeline,
            "abcdef123456",
            {
                "configfile": "config/config.yaml",
                "cores": 2,
                "dryrun": True,
                "env": "pipe",
                "human": "snakemake --token top-secret",
                "extra": "--api-key=another-secret --until all",
            },
        )
        self.assertEqual(capsule["config"]["values"]["api_key"], "<redacted>")
        self.assertNotIn("top-secret", json.dumps(capsule))
        self.assertNotIn("another-secret", json.dumps(capsule))
        (self.root / "results" / "summary.tsv").write_text(
            "sample\tvalue\nS1\t2\n", encoding="utf-8"
        )
        logfile = settings.RUN_LOG_DIR / "abcdef123456.log"
        logfile.write_text("complete\n", encoding="utf-8")
        session = SimpleNamespace(
            id="abcdef123456",
            status="exited",
            started=10.0,
            ended=14.5,
            exit_code=0,
            logfile=str(logfile),
        )
        final = provenance.finalize(self.pipeline, session)
        self.assertIn("results/summary.tsv", final["results"]["delta"]["added"])
        self.assertEqual(final["session"]["duration_s"], 4.5)
        self.assertTrue(provenance.list_capsules(self.pipeline)[0]["complete"])

    def test_current_comparison_reports_config_drift(self) -> None:
        provenance.capture(
            self.pipeline,
            "123456abcdef",
            {"configfile": "config/config.yaml", "cores": 2, "dryrun": True},
        )
        (self.root / "config" / "config.yaml").write_text(
            "threshold: 2\napi_key: changed-secret\n",
            encoding="utf-8",
        )
        diff = provenance.compare(self.pipeline, "123456abcdef", "current")
        config = next(
            section for section in diff["sections"] if section["id"] == "config"
        )
        changed_fields = {
            entry["field"] for entry in config["entries"] if entry["changed"]
        }
        self.assertIn("Config fingerprint", changed_fields)
        self.assertIn("threshold", changed_fields)
        self.assertNotIn("changed-secret", json.dumps(diff))

    def test_large_artifacts_use_sampled_hashing(self) -> None:
        large = self.root / "results" / "large.tsv"
        large.write_bytes(b"x" * (provenance.FULL_HASH_LIMIT + 1))
        fingerprint = provenance._hash_file(large)
        self.assertEqual(fingerprint["hash_mode"], "sampled")
        self.assertEqual(fingerprint["size"], provenance.FULL_HASH_LIMIT + 1)
        inventory = provenance._result_inventory(self.pipeline)
        item = next(
            row for row in inventory["items"] if row["path"] == "results/large.tsv"
        )
        self.assertEqual(item["hash_mode"], "sampled-3")

    def test_module_and_installed_environment_drift_are_visible(self) -> None:
        (self.root / "modules").mkdir()
        module = self.root / "modules" / "normalize.smk"
        module.write_text("rule normalize:\n    shell: 'true'\n", encoding="utf-8")
        env_root = self.root / "fake-env"
        conda_meta = env_root / "conda-meta"
        conda_meta.mkdir(parents=True)
        (conda_meta / "samtools-1.20-h50ea8bc_0.json").write_text(
            "{}", encoding="utf-8"
        )
        (conda_meta / "history").write_text("created\n", encoding="utf-8")
        with mock.patch(
            "benchtop.provenance.pipeline_tools.env_index",
            return_value={"pipe": {"path": str(env_root), "has_snakemake": True}},
        ):
            provenance.capture(
                self.pipeline,
                "fedcba654321",
                {"configfile": "config/config.yaml", "dryrun": True, "env": "pipe"},
            )
            module.write_text(
                "rule normalize:\n    shell: 'echo changed'\n", encoding="utf-8"
            )
            (conda_meta / "r-base-4.4.1-h.json").write_text("{}", encoding="utf-8")
            diff = provenance.compare(self.pipeline, "fedcba654321", "current")
        changed = {
            section["id"]
            for section in diff["sections"]
            if any(entry["changed"] for entry in section["entries"])
        }
        self.assertIn("code", changed)
        self.assertIn("environment", changed)

    def test_external_output_and_middle_content_change_are_captured(self) -> None:
        with tempfile.TemporaryDirectory() as external_tmp:
            external = Path(external_tmp)
            (self.root / "config" / "external.yaml").write_text(
                f"results_dir: {json.dumps(str(external))}\n",
                encoding="utf-8",
            )
            self.pipeline.configs.append("config/external.yaml")
            large = external / "large.tsv"
            large.write_bytes(b"a" * (provenance.FULL_HASH_LIMIT + 4096))
            original_times = (large.stat().st_atime_ns, large.stat().st_mtime_ns)
            with mock.patch.object(
                settings,
                "RESULTS_ROOTS",
                (self.root, external),
            ):
                provenance.capture(
                    self.pipeline,
                    "a1b2c3d4e5f6",
                    {"configfile": "config/external.yaml", "dryrun": True},
                )
                with large.open("r+b") as handle:
                    handle.seek(large.stat().st_size // 2)
                    handle.write(b"changed-middle")
                os.utime(large, ns=original_times)
                session = SimpleNamespace(
                    id="a1b2c3d4e5f6",
                    status="exited",
                    started=1.0,
                    ended=2.0,
                    exit_code=0,
                    logfile="",
                )
                final = provenance.finalize(self.pipeline, session)
        changed = final["results"]["delta"]["changed"]
        self.assertEqual(len(changed), 1)
        self.assertTrue(changed[0].startswith("external/"))
        self.assertEqual(final["results"]["after"]["external_roots"], 1)

    def test_slurm_submission_gets_a_pending_capsule(self) -> None:
        report = study.audit(self.pipeline)
        body = SlurmSubmitRequest(
            configfile="config/config.yaml",
            cores=2,
            dryrun=True,
            job_name="capsule-test",
        )
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
            ),
            mock.patch(
                "benchtop.server.pipelines.build_snakemake_argv",
                return_value=(["snakemake", "-n"], "snakemake -n"),
            ),
            mock.patch("benchtop.server.pipelines.resolve_env", return_value=""),
        ):
            submitted = server._submit_slurm_run(self.pipeline, body, report, False)
        capsule_id = submitted["job"]["capsule_id"]
        capsule = provenance.load(self.pipeline, capsule_id)
        self.assertEqual(capsule["session"]["status"], "submitted")
        self.assertEqual(capsule["session"]["slurm_job_id"], "12345")
        self.assertFalse(provenance.list_capsules(self.pipeline)[0]["complete"])


if __name__ == "__main__":
    unittest.main()
