# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from benchtop import provenance, runplan, server, settings, store, study
from benchtop.pipelines import Pipeline
from benchtop.server import RunRequest, SlurmSubmitRequest


class RunPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline_tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.pipeline_tmp.name)
        self.old_state_dir = settings.STATE_DIR
        self.old_state_file = settings.STATE_FILE
        self.old_run_log_dir = settings.RUN_LOG_DIR
        settings.STATE_DIR = Path(self.state_tmp.name)
        settings.STATE_FILE = settings.STATE_DIR / "state.json"
        settings.RUN_LOG_DIR = settings.STATE_DIR / "sessions"
        self.pipeline = self._pipeline(self.root)

    def tearDown(self) -> None:
        server.mgr.sessions = {
            sid: session
            for sid, session in server.mgr.sessions.items()
            if session.meta.get("pipeline") != self.pipeline.name
        }
        settings.STATE_DIR = self.old_state_dir
        settings.STATE_FILE = self.old_state_file
        settings.RUN_LOG_DIR = self.old_run_log_dir
        self.state_tmp.cleanup()
        self.pipeline_tmp.cleanup()

    @staticmethod
    def _pipeline(root: Path) -> Pipeline:
        (root / "config").mkdir()
        (root / "data").mkdir()
        (root / "rules").mkdir()
        (root / "scripts").mkdir()
        (root / "envs").mkdir()
        for sample in ("S1", "S2", "S3", "S4"):
            (root / "data" / f"{sample}.fastq.gz").write_bytes(b"reads")
        (root / "config" / "samples.tsv").write_text(
            "sample_id\tcondition\tsubject\tfastq_r1\n"
            "S1\tcontrol\tD1\tdata/S1.fastq.gz\n"
            "S2\tcontrol\tD2\tdata/S2.fastq.gz\n"
            "S3\ttreated\tD3\tdata/S3.fastq.gz\n"
            "S4\ttreated\tD4\tdata/S4.fastq.gz\n",
            encoding="utf-8",
        )
        (root / "config" / "config.yaml").write_text(
            "samples_tsv: config/samples.tsv\n"
            "results_dir: results\n"
            "design: '~ condition'\n",
            encoding="utf-8",
        )
        (root / "Snakefile").write_text(
            "include: 'rules/qc.smk'\nrule all:\n    input: []\n",
            encoding="utf-8",
        )
        (root / "rules" / "qc.smk").write_text(
            "rule qc:\n    output: 'results/qc.txt'\n    shell: 'python scripts/qc.py'\n",
            encoding="utf-8",
        )
        (root / "scripts" / "qc.py").write_text("print('qc')\n", encoding="utf-8")
        (root / "envs" / "qc.yaml").write_text(
            "name: qc\ndependencies:\n  - python=3.11\n",
            encoding="utf-8",
        )
        return Pipeline(
            name="runplan-pipe",
            path=root,
            kind="snakemake",
            snakefile="Snakefile",
            configs=["config/config.yaml"],
        )

    def _body(self, **changes) -> RunRequest:
        report = study.audit(self.pipeline)
        values = {
            "configfile": "config/config.yaml",
            "cores": 2,
            "dryrun": True,
            "use_conda": False,
            "study_sheet": report["selected"]["path"],
            "study_roles": report["roles"],
            "study_fingerprint": report["fingerprint"],
        }
        values.update(changes)
        return RunRequest(**values)

    def _build(
        self, *, cores: int = 2, argv: list[str] | None = None
    ) -> runplan.RunPlan:
        report = study.audit(self.pipeline)
        return runplan.build(
            self.pipeline,
            launch={
                "configfile": "config/config.yaml",
                "profile": "",
                "cores": cores,
                "dryrun": True,
                "use_conda": False,
                "target": "",
                "study_override": False,
            },
            argv=argv or ["snakemake", "-s", "Snakefile", "--cores", str(cores), "-n"],
            study_report=report,
            resolved_env="",
        )

    def test_canonical_digest_is_mapping_order_independent(self) -> None:
        left = runplan.RunPlan.create({"b": 2, "a": {"y": 1, "x": 0}})
        right = runplan.RunPlan.create({"a": {"x": 0, "y": 1}, "b": 2})
        self.assertEqual(left.digest, right.digest)
        self.assertEqual(left.canonical_json, right.canonical_json)

    def test_additive_resolver_fields_preserve_legacy_v1_verification(self) -> None:
        record = self._build(
            argv=["snakemake", "--configfile", "config/config.yaml", "-n"]
        ).record()
        plan = record["plan"]
        for key in (
            "directives",
            "directives_truncated",
            "include_targets",
            "unresolved_dynamic_directives",
        ):
            plan["workflow"].pop(key, None)
        for key in (
            "ordered_layers",
            "precedence_order",
            "literal_overrides",
            "authority",
            "fingerprint",
        ):
            plan["configuration"].pop(key, None)
        for key in (
            "reference_artifacts",
            "reference_artifacts_truncated",
            "reference_artifacts_fingerprint",
            "reference_resolution_issues",
        ):
            plan["inputs"].pop(key, None)
        for key in ("wrappers", "containers"):
            plan["environment"].pop(key, None)
        for key in (
            "targets",
            "targets_resolution_status",
            "target_present_in_command",
        ):
            plan["execution"].pop(key, None)
        plan.pop("resolution", None)
        for key in ("wrappers", "containers", "references"):
            plan["fingerprints"].pop(key, None)
        record["projection_digest"] = (
            "sha256:"
            + hashlib.sha256(runplan._canonical(plan).encode("utf-8")).hexdigest()
        )

        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(runplan.verify_record(record), [])

    def test_material_changes_change_digest(self) -> None:
        baseline = self._build()
        config = self.root / "config" / "config.yaml"
        config.write_text(
            config.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8"
        )
        changed_config = self._build()
        (self.root / "Snakefile").write_text(
            "rule all:\n    input: []\n# changed\n",
            encoding="utf-8",
        )
        changed_workflow = self._build()
        changed_resources = self._build(cores=3)
        changed_command = self._build(argv=["snakemake", "-n", "different-target"])

        self.assertNotEqual(baseline.digest, changed_config.digest)
        self.assertNotEqual(changed_config.digest, changed_workflow.digest)
        self.assertNotEqual(changed_workflow.digest, changed_resources.digest)
        self.assertNotEqual(changed_workflow.digest, changed_command.digest)

    def test_public_plan_redacts_secret_arguments(self) -> None:
        plan = self._build(
            argv=[
                "snakemake",
                "--api-key",
                "top-secret",
                "--token=abc123",
                "TOKEN=assignment-secret",
                "AWS_SECRET_ACCESS_KEY=aws-secret",
                "https://user:password@example.test/data?token=query-secret",
            ]
        )
        record = plan.record()
        public = json.dumps(record, sort_keys=True)
        self.assertNotIn("top-secret", public)
        self.assertNotIn("abc123", public)
        self.assertNotIn("assignment-secret", public)
        self.assertNotIn("aws-secret", public)
        self.assertNotIn("query-secret", public)
        self.assertNotIn("user:password", public)
        self.assertIn("<redacted>", public)
        self.assertEqual(record["digest_scope"], "private-canonical-plan")
        self.assertTrue(record["redacted"])
        self.assertNotEqual(record["digest"], record["projection_digest"])

    def test_argv_redaction_preserves_multiword_secret_boundaries(self) -> None:
        argv = [
            "snakemake",
            "--token",
            "two word token value",
            "--api-key=inline quoted value",
            "PASSWORD=assignment with spaces",
            "ordinary target",
        ]

        rendered = runplan.redact_command(argv)
        legacy = runplan.redact_command_text(
            "snakemake --token 'two word token value' "
            "--api-key='inline quoted value' PASSWORD='assignment with spaces' "
            "'ordinary target'"
        )

        for projection in (rendered, legacy):
            self.assertNotIn("two word token value", projection)
            self.assertNotIn("inline quoted value", projection)
            self.assertNotIn("assignment with spaces", projection)
            self.assertIn("ordinary target", projection)
            self.assertGreaterEqual(projection.count("<redacted>"), 3)

    def test_included_rule_script_and_per_rule_environment_change_digest(self) -> None:
        baseline = self._build()
        self.assertEqual(runplan.verify_record(baseline.record()), [])
        rule = self.root / "rules" / "qc.smk"
        rule.write_text(
            rule.read_text(encoding="utf-8") + "# rule drift\n", encoding="utf-8"
        )
        self.assertIn(
            "workflow source manifest changed after RunPlan creation",
            runplan.verify_record(baseline.record()),
        )
        changed_rule = self._build()
        script = self.root / "scripts" / "qc.py"
        script.write_text("print('changed')\n", encoding="utf-8")
        changed_script = self._build()
        environment = self.root / "envs" / "qc.yaml"
        environment.write_text(
            environment.read_text(encoding="utf-8") + "  - samtools=1.20\n",
            encoding="utf-8",
        )
        changed_environment = self._build()

        self.assertNotEqual(baseline.digest, changed_rule.digest)
        self.assertNotEqual(changed_rule.digest, changed_script.digest)
        self.assertNotEqual(changed_script.digest, changed_environment.digest)

    def test_resolved_conda_package_record_changes_digest(self) -> None:
        env_root = self.root / "driver-env"
        (env_root / "bin").mkdir(parents=True)
        (env_root / "conda-meta").mkdir()
        (env_root / "bin" / "snakemake").write_text("#!/bin/sh\n", encoding="utf-8")
        package = env_root / "conda-meta" / "snakemake-1.json"
        package.write_text('{"name":"snakemake","version":"1"}\n', encoding="utf-8")
        report = study.audit(self.pipeline)

        def build() -> runplan.RunPlan:
            return runplan.build(
                self.pipeline,
                launch={
                    "configfile": "config/config.yaml",
                    "profile": "",
                    "cores": 2,
                    "dryrun": True,
                    "use_conda": True,
                    "target": "",
                    "study_override": False,
                },
                argv=["snakemake", "-n"],
                study_report=report,
                resolved_env="driver",
            )

        with mock.patch(
            "benchtop.runplan.pipeline_tools.env_index",
            return_value={"driver": {"path": str(env_root), "has_snakemake": True}},
        ):
            before = build()
            package.write_text('{"name":"snakemake","version":"2"}\n', encoding="utf-8")
            after = build()

        self.assertNotEqual(before.digest, after.digest)

    def test_bounded_resolver_records_precedence_directives_and_artifacts(self) -> None:
        (self.root / "profiles" / "cluster").mkdir(parents=True)
        (self.root / "refs").mkdir()
        (self.root / "containers").mkdir()
        (self.root / "refs" / "genome.fa").write_text(">chr1\nACGT\n", encoding="utf-8")
        (self.root / "refs" / "current.fa").symlink_to("genome.fa")
        (self.root / "refs" / "genes.gtf").write_text("chr1\ttest\n", encoding="utf-8")
        (self.root / "refs" / "blacklist.bed").write_text(
            "chr1\t1\t2\n", encoding="utf-8"
        )
        (self.root / "refs" / "genome.1.bt2").write_bytes(b"index-one")
        (self.root / "refs" / "genome.2.bt2").write_bytes(b"index-two")
        (self.root / "containers" / "tool.sif").write_bytes(b"local-image")
        (self.root / "config" / "workflow.yaml").write_text(
            "reference_fasta: refs/current.fa\nbowtie2_index: refs/genome\n",
            encoding="utf-8",
        )
        (self.root / "config" / "profile.yaml").write_text(
            "annotation_gtf: refs/genes.gtf\n",
            encoding="utf-8",
        )
        (self.root / "profiles" / "cluster" / "config.yaml").write_text(
            "configfiles:\n  - config/profile.yaml\n",
            encoding="utf-8",
        )
        config = self.root / "config" / "config.yaml"
        config.write_text(
            config.read_text(encoding="utf-8") + "blacklist: refs/blacklist.bed\n",
            encoding="utf-8",
        )
        digest_pin = "a" * 64
        (self.root / "Snakefile").write_text(
            "configfile: 'config/workflow.yaml'\n"
            "include: 'rules/qc.smk'\n"
            "include: choose_rules()\n"
            "rule all:\n    input: []\n",
            encoding="utf-8",
        )
        (self.root / "rules" / "qc.smk").write_text(
            "rule wrapped:\n"
            "    wrapper: '0.90.0/bio/samtools/sort'\n"
            "rule pinned_container:\n"
            f"    container: 'docker://example/tool@sha256:{digest_pin}'\n"
            "rule local_container:\n"
            "    singularity: 'containers/tool.sif'\n"
            "rule dynamic_surfaces:\n"
            "    wrapper: wrapper_reference()\n"
            "    container: f'docker://example/{image}'\n",
            encoding="utf-8",
        )
        report = study.audit(self.pipeline, configfile="config/config.yaml")
        plan = runplan.build(
            self.pipeline,
            launch={
                "configfile": "config/config.yaml",
                "profile": "cluster",
                "cores": 2,
                "dryrun": True,
                "use_conda": False,
                "target": "wrapped",
                "study_override": False,
            },
            argv=[
                "snakemake",
                "-s",
                "Snakefile",
                "--profile",
                "profiles/cluster",
                "--configfile",
                "config/config.yaml",
                "--config=blacklist=refs/blacklist.bed",
                "wrapped",
            ],
            study_report=report,
            resolved_env="",
        )
        payload = plan.payload

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["configuration"]["precedence_order"], "low-to-high")
        self.assertEqual(
            [layer["origin"] for layer in payload["configuration"]["ordered_layers"]],
            [
                "workflow-configfile",
                "profile-settings",
                "profile-configfile",
                "cli-configfile",
            ],
        )
        self.assertTrue(payload["configuration"]["authority"]["aligned"])
        self.assertEqual(payload["execution"]["targets"], ["wrapped"])

        includes = payload["workflow"]["include_targets"]
        self.assertTrue(includes[0]["material"]["exists"])
        unresolved = payload["workflow"]["unresolved_dynamic_directives"]
        self.assertEqual(
            {item["directive"] for item in unresolved},
            {"include", "wrapper", "container"},
        )
        self.assertFalse(payload["resolution"]["runtime_evaluation_attempted"])
        self.assertFalse(payload["resolution"]["complete"])

        wrappers = payload["environment"]["wrappers"]
        self.assertEqual(wrappers[0]["version"], "0.90.0")
        self.assertTrue(wrappers[0]["immutable"])
        containers = payload["environment"]["containers"]
        self.assertEqual(
            {item["identity_status"] for item in containers},
            {
                "pinned-digest",
                "local-content",
            },
        )
        local_container = next(item for item in containers if item["scheme"] == "local")
        self.assertEqual(local_container["material"]["hash_mode"], "full")

        references = payload["inputs"]["reference_artifacts"]
        by_key = {item["key_path"]: item for item in references}
        self.assertTrue(by_key["reference_fasta"]["material"]["symlink"])
        self.assertEqual(
            by_key["reference_fasta"]["material"]["symlink_target"],
            "genome.fa",
        )
        self.assertEqual(
            by_key["bowtie2_index"]["material"]["artifact_type"], "index-prefix"
        )
        self.assertEqual(by_key["bowtie2_index"]["material"]["file_count"], 2)
        self.assertIn("annotation_gtf", by_key)
        self.assertIn("blacklist", by_key)
        self.assertTrue(payload["fingerprints"]["references"])

    def test_config_authority_conflicts_are_explicit(self) -> None:
        for name in ("launch.yaml", "command.yaml"):
            (self.root / "config" / name).write_text(
                "results_dir: results\n", encoding="utf-8"
            )
        report = study.audit(self.pipeline, configfile="config/config.yaml")
        plan = runplan.build(
            self.pipeline,
            launch={
                "configfile": "config/launch.yaml",
                "profile": "",
                "cores": 1,
                "dryrun": True,
                "use_conda": False,
                "target": "",
                "study_override": False,
            },
            argv=["snakemake", "--configfile", "config/command.yaml", "-n"],
            study_report=report,
            resolved_env="",
        )

        authority = plan.payload["configuration"]["authority"]
        self.assertFalse(authority["aligned"])
        self.assertEqual(authority["resolution_status"], "config-authority-conflict")
        self.assertIn("study-config-differs-from-launch-config", authority["issues"])
        self.assertIn("launch-config-differs-from-command-config", authority["issues"])
        self.assertFalse(plan.payload["resolution"]["launch_safe"])

    def test_dynamic_directive_is_recorded_without_execution(self) -> None:
        marker = self.root / "dynamic-was-executed"
        (self.root / "Snakefile").write_text(
            "include: __import__('pathlib').Path('dynamic-was-executed').write_text('bad')\n"
            "rule all:\n    input: []\n",
            encoding="utf-8",
        )

        plan = self._build(
            argv=[
                "snakemake",
                "--configfile",
                "config/config.yaml",
                "-n",
            ]
        )

        self.assertFalse(marker.exists())
        unresolved = plan.payload["workflow"]["unresolved_dynamic_directives"]
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["directive"], "include")
        self.assertEqual(
            unresolved[0]["resolution_status"],
            "unresolved-dynamic-python-expression",
        )

    def test_bounded_scan_truncation_is_explicit(self) -> None:
        (self.root / "refs").mkdir()
        (self.root / "refs" / "genome.fa").write_text(">chr1\nAC\n", encoding="utf-8")
        (self.root / "refs" / "genes.gtf").write_text("chr1\ttest\n", encoding="utf-8")
        config = self.root / "config" / "config.yaml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + "reference_fasta: refs/genome.fa\n"
            + "annotation_gtf: refs/genes.gtf\n",
            encoding="utf-8",
        )
        (self.root / "Snakefile").write_text(
            "include: 'rules/qc.smk'\n"
            "include: dynamic_rules()\n"
            "rule all:\n    input: []\n",
            encoding="utf-8",
        )
        report = study.audit(self.pipeline, configfile="config/config.yaml")

        with (
            mock.patch.object(runplan, "MAX_DIRECTIVE_RECORDS", 1),
            mock.patch.object(runplan, "MAX_REFERENCE_ARTIFACTS", 1),
            mock.patch.object(runplan, "MAX_ARTIFACT_HASH_BYTES", 4),
            mock.patch.object(runplan, "ARTIFACT_SAMPLE_BYTES", 2),
        ):
            plan = runplan.build(
                self.pipeline,
                launch={
                    "configfile": "config/config.yaml",
                    "profile": "",
                    "cores": 1,
                    "dryrun": True,
                    "use_conda": False,
                    "target": "",
                    "study_override": False,
                },
                argv=["snakemake", "--configfile", "config/config.yaml", "-n"],
                study_report=report,
                resolved_env="",
            )

        resolution = plan.payload["resolution"]
        self.assertTrue(resolution["directives_truncated"])
        self.assertTrue(resolution["reference_artifacts_truncated"])
        self.assertIn("snakemake-directive-scan-truncated", resolution["issues"])
        self.assertIn("reference-artifact-scan-truncated", resolution["issues"])
        self.assertTrue(
            any(
                issue.startswith("bounded-reference-material:")
                for issue in resolution["issues"]
            )
        )
        self.assertEqual(
            plan.payload["inputs"]["reference_artifacts"][0]["material"]["hash_mode"],
            "sampled-head-tail",
        )
        self.assertFalse(resolution["complete"])

    def test_mutable_wrapper_and_container_are_not_launch_safe(self) -> None:
        (self.root / "Snakefile").write_text(
            "rule unsafe:\n"
            "    wrapper: 'bio/samtools/sort'\n"
            "    container: 'docker://example/tool:latest'\n",
            encoding="utf-8",
        )

        plan = self._build(
            argv=[
                "snakemake",
                "--configfile",
                "config/config.yaml",
                "-n",
            ]
        )
        payload = plan.payload

        self.assertFalse(payload["environment"]["wrappers"][0]["immutable"])
        self.assertFalse(payload["environment"]["containers"][0]["immutable"])
        self.assertTrue(
            any(
                issue.startswith("mutable-wrapper:")
                for issue in payload["resolution"]["issues"]
            )
        )
        self.assertTrue(
            any(
                issue.startswith("mutable-container:")
                for issue in payload["resolution"]["issues"]
            )
        )
        self.assertFalse(payload["resolution"]["launch_safe"])

    def test_same_path_reference_index_symlink_and_container_drift(self) -> None:
        (self.root / "refs").mkdir()
        (self.root / "containers").mkdir()
        (self.root / "refs" / "genome-a.fa").write_text(
            ">chr1\nAAAA\n", encoding="utf-8"
        )
        (self.root / "refs" / "genome-b.fa").write_text(
            ">chr1\nAAAA\n", encoding="utf-8"
        )
        current = self.root / "refs" / "current.fa"
        current.symlink_to("genome-a.fa")
        shard = self.root / "refs" / "genome.1.bt2"
        shard.write_bytes(b"index-v1")
        image = self.root / "containers" / "tool.sif"
        image.write_bytes(b"image-v1")
        config = self.root / "config" / "config.yaml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + "reference_fasta: refs/current.fa\n"
            + "bowtie2_index: refs/genome\n",
            encoding="utf-8",
        )
        (self.root / "Snakefile").write_text(
            "rule all:\n    input: []\n    container: 'containers/tool.sif'\n",
            encoding="utf-8",
        )

        def build() -> runplan.RunPlan:
            report = study.audit(self.pipeline, configfile="config/config.yaml")
            return runplan.build(
                self.pipeline,
                launch={
                    "configfile": "config/config.yaml",
                    "profile": "",
                    "cores": 1,
                    "dryrun": True,
                    "use_conda": False,
                    "target": "",
                    "study_override": False,
                },
                argv=["snakemake", "--configfile", "config/config.yaml", "-n"],
                study_report=report,
                resolved_env="",
            )

        baseline = build()
        self.assertEqual(runplan.verify_record(baseline.record()), [])

        current.unlink()
        current.symlink_to("genome-b.fa")
        shard.write_bytes(b"index-v2")
        image.write_bytes(b"image-v2")

        errors = runplan.verify_record(baseline.record())
        changed = build()
        self.assertIn(
            "reference artifact material changed after RunPlan creation", errors
        )
        self.assertIn("local container material changed after RunPlan creation", errors)
        self.assertNotEqual(baseline.digest, changed.digest)

    def test_post_preview_and_local_session_share_digest(self) -> None:
        body = self._body()
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=self.pipeline),
            mock.patch("benchtop.pipelines.resolve_env", return_value=""),
            mock.patch("benchtop.pipelines._conda_exe", return_value=""),
        ):
            preview = server.run_preview_post(self.pipeline.name, body)
            body.plan_digest = preview["plan_digest"]
            with (
                mock.patch("benchtop.server._run_core_budget", return_value=8),
                mock.patch("benchtop.server._queue_or_start_run"),
            ):
                launched = asyncio.run(server.start_run(self.pipeline.name, body))

        session = server.mgr.get(launched["session"]["id"])
        self.assertIsNotNone(session)
        self.assertEqual(preview["plan_digest"], launched["plan_digest"])
        self.assertEqual(preview["plan_digest"], session.meta["plan_digest"])
        capsule = provenance.capture(
            self.pipeline,
            session.id,
            session.meta,
            run_plan=session._run_plan,
        )
        self.assertEqual(preview["plan_digest"], capsule["run_plan"]["digest"])

    def test_capsule_and_session_public_views_redact_assignment_and_url_secrets(
        self,
    ) -> None:
        config = self.root / "config" / "config.yaml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + "endpoint: https://user:password@example.test/data?token=config-secret\n",
            encoding="utf-8",
        )
        report = study.audit(self.pipeline)
        plan = runplan.build(
            self.pipeline,
            launch={
                "configfile": "config/config.yaml",
                "profile": "",
                "cores": 1,
                "dryrun": True,
                "use_conda": False,
                "target": "",
                "study_override": False,
            },
            argv=["snakemake", "TOKEN=session-secret", "--api-key", "flag-secret"],
            study_report=report,
            resolved_env="",
        )
        session = server.mgr.create(
            kind="run",
            title="secret test",
            cwd=str(self.root),
            argv=plan.argv,
            meta={
                "pipeline": self.pipeline.name,
                "human": "snakemake TOKEN=session-secret --api-key flag-secret",
                "extra": "AWS_SECRET_ACCESS_KEY=aws-secret",
            },
        )
        capsule = provenance.capture(
            self.pipeline,
            session.id,
            session.meta,
            run_plan=plan,
        )
        serialized = json.dumps({"session": session.to_dict(), "capsule": capsule})

        for secret in (
            "session-secret",
            "flag-secret",
            "aws-secret",
            "user:password",
            "config-secret",
        ):
            self.assertNotIn(secret, serialized)

    def test_legacy_get_preview_keeps_command_and_resolves_environment_once(
        self,
    ) -> None:
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=self.pipeline),
            mock.patch(
                "benchtop.pipelines.resolve_env", return_value="driver-env"
            ) as resolve,
            mock.patch("benchtop.pipelines._conda_exe", return_value=""),
        ):
            preview = server.run_preview(
                self.pipeline.name,
                configfile="config/config.yaml",
                cores=2,
                dryrun=True,
                use_conda=False,
            )

        self.assertIn("snakemake", preview["command"])
        self.assertTrue(preview["plan_digest"].startswith("sha256:"))
        self.assertEqual(preview["plan"]["schema_version"], 1)
        resolve.assert_called_once_with(self.pipeline)

    def test_stale_digest_is_rejected_before_session_creation(self) -> None:
        body = self._body(plan_digest="sha256:stale")
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=self.pipeline),
            mock.patch("benchtop.server._run_core_budget", return_value=8),
            mock.patch("benchtop.pipelines.resolve_env", return_value=""),
            mock.patch("benchtop.pipelines._conda_exe", return_value=""),
            mock.patch.object(server.mgr, "create", wraps=server.mgr.create) as create,
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(server.start_run(self.pipeline.name, body))
        self.assertEqual(raised.exception.status_code, 409)
        create.assert_not_called()

    def test_real_and_slurm_runs_require_plan_but_legacy_local_dry_run_remains(
        self,
    ) -> None:
        real = self._body(dryrun=False)
        dry = self._body(dryrun=True)
        slurm = SlurmSubmitRequest(**dry.model_dump(), job_name="requires-plan")
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=self.pipeline),
            mock.patch("benchtop.server._run_core_budget", return_value=8),
            mock.patch("benchtop.pipelines.resolve_env", return_value=""),
            mock.patch("benchtop.pipelines._conda_exe", return_value=""),
            mock.patch("benchtop.server._queue_or_start_run"),
        ):
            with self.assertRaises(HTTPException) as real_error:
                asyncio.run(server.start_run(self.pipeline.name, real))
            legacy_dry = asyncio.run(server.start_run(self.pipeline.name, dry))
            with self.assertRaises(HTTPException) as slurm_error:
                server.slurm_submit(self.pipeline.name, slurm)

        self.assertEqual(real_error.exception.status_code, 428)
        self.assertIn("session", legacy_dry)
        self.assertEqual(slurm_error.exception.status_code, 428)

    def test_incomplete_dynamic_runplan_blocks_real_local_and_slurm_execution(
        self,
    ) -> None:
        (self.root / "Snakefile").write_text(
            "include: choose_rules(config)\nrule all:\n    input: []\n",
            encoding="utf-8",
        )
        local = self._body(dryrun=False)
        slurm = SlurmSubmitRequest(**local.model_dump(), job_name="unsafe-plan")
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=self.pipeline),
            mock.patch("benchtop.server._run_core_budget", return_value=8),
            mock.patch("benchtop.pipelines.resolve_env", return_value=""),
            mock.patch("benchtop.pipelines._conda_exe", return_value=""),
        ):
            local_preview = server.run_preview_post(self.pipeline.name, local)
            slurm_preview = server.slurm_run_preview(self.pipeline.name, slurm)
            local.plan_digest = local_preview["plan_digest"]
            slurm.plan_digest = slurm_preview["plan_digest"]
            with (
                mock.patch.object(server.mgr, "create") as create,
                mock.patch(
                    "benchtop.server._run_slurm_cmd",
                ) as scheduler_command,
            ):
                with self.assertRaises(HTTPException) as local_error:
                    asyncio.run(server.start_run(self.pipeline.name, local))
                with self.assertRaises(HTTPException) as slurm_error:
                    server.slurm_submit(self.pipeline.name, slurm)

        self.assertFalse(local_preview["plan"]["resolution"]["launch_safe"])
        self.assertEqual(local_error.exception.status_code, 409)
        self.assertEqual(slurm_error.exception.status_code, 409)
        self.assertIn(
            "unresolved-dynamic-directives",
            " ".join(local_error.exception.detail["plan_resolution"]["issues"]),
        )
        create.assert_not_called()
        scheduler_command.assert_not_called()

    def test_discovered_but_undeclared_config_blocks_real_execution(self) -> None:
        body = self._body(configfile="", dryrun=False)
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=self.pipeline),
            mock.patch("benchtop.server._run_core_budget", return_value=8),
            mock.patch("benchtop.pipelines.resolve_env", return_value=""),
            mock.patch("benchtop.pipelines._conda_exe", return_value=""),
        ):
            preview = server.run_preview_post(self.pipeline.name, body)
            body.plan_digest = preview["plan_digest"]
            with mock.patch.object(server.mgr, "create") as create:
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(server.start_run(self.pipeline.name, body))

        self.assertEqual(raised.exception.status_code, 409)
        issues = raised.exception.detail["plan_resolution"]["issues"]
        self.assertTrue(any(issue.startswith("config-authority:") for issue in issues))
        create.assert_not_called()

    def test_queued_workflow_change_blocks_exact_plan(self) -> None:
        body = self._body()
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=self.pipeline),
            mock.patch("benchtop.pipelines.resolve_env", return_value=""),
            mock.patch("benchtop.pipelines._conda_exe", return_value=""),
        ):
            preview = server.run_preview_post(self.pipeline.name, body)
            body.plan_digest = preview["plan_digest"]
            with (
                mock.patch("benchtop.server._run_core_budget", return_value=8),
                mock.patch("benchtop.server._queue_or_start_run"),
            ):
                launched = asyncio.run(server.start_run(self.pipeline.name, body))
        session = server.mgr.get(launched["session"]["id"])
        session.status = "queued"
        (self.root / "Snakefile").write_text(
            "rule all:\n    input: []\n# mutation while queued\n",
            encoding="utf-8",
        )

        with (
            mock.patch("benchtop.server.pipelines.get", return_value=self.pipeline),
            mock.patch("benchtop.pipelines._conda_exe", return_value=""),
        ):
            allowed = server._prepare_run_capsule(session)

        self.assertFalse(allowed)
        self.assertTrue(session.meta["run_plan_changed_while_queued"])
        self.assertNotEqual(
            session.meta["plan_digest"], session.meta["current_plan_digest"]
        )

    def test_slurm_preview_submission_job_history_and_capsule_share_digest(
        self,
    ) -> None:
        base = self._body()
        body = SlurmSubmitRequest(**base.model_dump(), job_name="plan-test")
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=self.pipeline),
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
                    "output": "12345\n",
                    "error": "",
                },
            ),
            mock.patch("benchtop.pipelines.resolve_env", return_value=""),
            mock.patch("benchtop.pipelines._conda_exe", return_value=""),
            mock.patch.dict(
                os.environ,
                {
                    "AWS_SECRET_ACCESS_KEY": "do-not-export-aws-secret",
                    "OPENAI_API_KEY": "do-not-export-openai-secret",
                },
            ),
        ):
            preview = server.slurm_run_preview(self.pipeline.name, body)
            body.plan_digest = preview["plan_digest"]
            submitted = server.slurm_submit(self.pipeline.name, body)

        digest = preview["plan_digest"]
        self.assertEqual(digest, submitted["plan_digest"])
        self.assertEqual(digest, submitted["job"]["plan_digest"])
        self.assertEqual(digest, store.history(self.pipeline.name)[0]["plan_digest"])
        capsule = provenance.load(self.pipeline, submitted["job"]["capsule_id"])
        self.assertEqual(digest, capsule["run_plan"]["digest"])
        self.assertEqual(capsule["session"]["status"], "submitted")
        self.assertNotIn("plan_record", submitted["job"])
        self.assertNotIn("script", submitted["job"])
        tracked = store.slurm_job(intent_id=submitted["job"]["id"])
        self.assertIsNotNone(tracked)
        plan_file = Path(tracked["plan_record"])
        script_file = Path(tracked["script"])
        self.assertEqual(stat.S_IMODE(plan_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(script_file.stat().st_mode), 0o600)
        self.assertEqual(runplan.verify_file(plan_file), [])
        script_text = script_file.read_text(encoding="utf-8")
        self.assertIn("benchtop.runplan verify", script_text)
        self.assertIn("#SBATCH --export=NIL", script_text)
        self.assertNotIn("do-not-export-aws-secret", script_text)
        self.assertNotIn("do-not-export-openai-secret", script_text)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", script_text)
        self.assertNotIn("OPENAI_API_KEY", script_text)

    def test_stale_slurm_plan_is_rejected_before_sbatch(self) -> None:
        base = self._body()
        body = SlurmSubmitRequest(**base.model_dump(), job_name="stale-plan")
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=self.pipeline),
            mock.patch("benchtop.pipelines.resolve_env", return_value=""),
            mock.patch("benchtop.pipelines._conda_exe", return_value=""),
        ):
            preview = server.slurm_run_preview(self.pipeline.name, body)
            body.plan_digest = preview["plan_digest"]
            config = self.root / "config" / "config.yaml"
            config.write_text(
                config.read_text(encoding="utf-8") + "# changed after preview\n",
                encoding="utf-8",
            )
            with mock.patch("benchtop.server._run_slurm_cmd") as submit:
                with self.assertRaises(HTTPException) as raised:
                    server.slurm_submit(self.pipeline.name, body)

        self.assertEqual(raised.exception.status_code, 409)
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
