# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import ValidationError

from benchtop import server
from benchtop.pipelines import Pipeline

EXPECTED_HELP_TABS = {
    "overview",
    "study",
    "run",
    "results",
    "capsules",
    "browse",
    "code",
    "agents",
    "dag",
    "config",
    "sra_geo",
    "resources",
    "github",
    "terminal",
    "fleet-health",
    "fleet-task",
}


class HelpCatalogTest(unittest.TestCase):
    def test_catalog_covers_every_tab_with_operational_context(self) -> None:
        catalog = server.help_catalog()
        tabs = {item["id"]: item for item in catalog["tabs"]}

        self.assertEqual(set(tabs), EXPECTED_HELP_TABS)
        self.assertTrue(catalog["shortcuts"])
        self.assertEqual(
            catalog["package_help"]["supported_ecosystems"],
            ("python", "r", "javascript", "snakemake", "shell", "yaml"),
        )
        self.assertEqual(catalog["command_help"]["supported_commands"], ("fastqc",))
        for tab in tabs.values():
            with self.subTest(tab=tab["id"]):
                for field in (
                    "summary",
                    "how_to",
                    "reads",
                    "writes",
                    "network",
                    "persistence",
                    "cautions",
                    "prerequisites",
                    "shortcuts",
                ):
                    self.assertIn(field, tab)
                self.assertTrue(tab["summary"])
                self.assertTrue(tab["how_to"])
                self.assertTrue(tab["persistence"])
                self.assertTrue(tab["cautions"])

    def test_fastqc_command_catalog_is_complete_static_and_versioned(self) -> None:
        guide = server.code_command_help("fastqc")
        options = guide["options"]
        flags = [flag for option in options for flag in option["flags"]]
        expected_long_flags = {
            "--help",
            "--version",
            "--quiet",
            "--svg",
            "--nogroup",
            "--expgroup",
            "--outdir",
            "--extract",
            "--noextract",
            "--delete",
            "--format",
            "--memory",
            "--threads",
            "--kmers",
            "--casava",
            "--nano",
            "--nofilter",
            "--contaminants",
            "--adapters",
            "--limits",
            "--dir",
            "--java",
            "--min_length",
            "--dup_length",
        }

        self.assertEqual(guide["catalog_version"], "fastqc-0.12.1")
        self.assertEqual(
            {flag for flag in flags if flag.startswith("--")}, expected_long_flags
        )
        self.assertEqual(len(flags), len(set(flags)))
        self.assertTrue(all(option.get("summary") for option in options))
        self.assertIn(
            "memory",
            next(option for option in options if "--threads" in option["flags"])[
                "caution"
            ].lower(),
        )

        # Every request receives a fresh copy, and lookup never resolves a binary.
        guide["options"][0]["summary"] = "mutated"
        self.assertNotEqual(
            server.code_command_help("fastqc")["options"][0]["summary"], "mutated"
        )
        for command in ("unknown-tool", "../fastqc", "FASTQC"):
            with (
                self.subTest(command=command),
                self.assertRaises(HTTPException) as error,
            ):
                server.code_command_help(command)
            self.assertEqual(error.exception.status_code, 404)

    def test_agent_help_does_not_overstate_sandbox_or_log_boundaries(self) -> None:
        tabs = {item["id"]: item for item in server.help_catalog()["tabs"]}
        agents = " ".join(
            str(value)
            for field in ("summary", "writes", "network", "persistence", "cautions")
            for value in (
                tabs["agents"][field]
                if isinstance(tabs["agents"][field], (list, tuple))
                else (tabs["agents"][field],)
            )
        )
        fleet = " ".join(
            str(value)
            for field in ("summary", "writes", "network", "persistence", "cautions")
            for value in (
                tabs["fleet-task"][field]
                if isinstance(tabs["fleet-task"][field], (list, tuple))
                else (tabs["fleet-task"][field],)
            )
        )
        self.assertIn("does not restrict what the CLI sends to its provider", agents)
        self.assertIn("not a durable OmicsANG audit log", agents)
        self.assertIn("contained", fleet)
        self.assertIn("disclosure to providers is not restricted", fleet)
        self.assertNotIn("within the task scope", fleet)
        self.assertNotIn("scoped repository context", fleet)

    def test_sensitive_help_and_search_inputs_are_post_bodies(self) -> None:
        methods = {
            route.path: set(route.methods or ())
            for route in server.app.routes
            if hasattr(route, "methods")
        }
        self.assertEqual(
            methods["/api/pipelines/{name}/browse/search"],
            {"POST"},
        )
        self.assertEqual(
            methods["/api/pipelines/{name}/browse/open"],
            {"POST"},
        )
        self.assertEqual(
            methods["/api/pipelines/{name}/code/help"],
            {"POST"},
        )
        self.assertEqual(
            methods["/api/pipelines/{name}/code/read"],
            {"POST"},
        )
        self.assertEqual(
            methods["/api/pipelines/{name}/results/directories/search"],
            {"POST"},
        )
        self.assertNotIn(
            "/api/pipelines/{name}/results/directories",
            methods,
        )


class PackageHelpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pipeline = Pipeline(name="pipe", path=self.root, kind="snakemake")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _help(self, path: str) -> dict:
        with mock.patch(
            "benchtop.server._require_pipeline",
            return_value=self.pipeline,
        ):
            return server.code_help("pipe", server.CodeHelpRequest(path=path))

    @staticmethod
    def _by_name(report: dict) -> dict[str, dict]:
        return {item["name"].casefold(): item for item in report["packages"]}

    def test_python_packages_are_static_curated_and_unknown_safe(self) -> None:
        (self.root / "analysis.py").write_text(
            "import os\nimport numpy as np\nfrom pandas import DataFrame\n"
            "import mystery_lab\n",
            encoding="utf-8",
        )

        report = self._help("analysis.py")
        packages = self._by_name(report)

        self.assertEqual(report["language"], "python")
        self.assertIn("numpy", packages)
        self.assertIn("pandas", packages)
        self.assertIn("mystery_lab", packages)
        self.assertNotIn("os", packages)
        self.assertEqual(packages["numpy"]["docs_domain"], "numpy.org")
        self.assertTrue(packages["numpy"]["docs_url"].startswith("https://"))
        self.assertIsNone(packages["mystery_lab"]["docs_url"])
        self.assertIsNone(packages["mystery_lab"]["docs_domain"])
        self.assertIn("static and local", " ".join(report["notes"]))

        with mock.patch(
            "benchtop.server._require_pipeline",
            return_value=self.pipeline,
        ):
            read = server.code_read_private(
                "pipe",
                server.CodeHelpRequest(path="analysis.py"),
            )
        self.assertEqual(read["path"], "analysis.py")
        self.assertIn("import mystery_lab", read["content"])

    def test_r_javascript_snakemake_shell_and_yaml_patterns(self) -> None:
        fixtures = {
            "analysis.R": 'library(DESeq2)\nedgeR::DGEList()\nrequire("mysteryR")\n',
            "app.js": (
                "import React from 'react';\n"
                "const helper = require('@private-scope/lab-tool/subpath');\n"
            ),
            "Snakefile": (
                "import pandas\n"
                "rule align:\n"
                "    conda: 'envs/align.yaml'\n"
                "    shell: 'samtools view input.bam > output.sam'\n"
            ),
            "workflow.sh": "samtools sort input.bam\nmystery-aligner --mode safe\n",
            "environment.yml": (
                "name: analysis\nchannels: [conda-forge, bioconda]\n"
                "dependencies:\n"
                "  - python=3.11\n"
                "  - bioconda::samtools=1.20\n"
                "  - pip:\n"
                "      - scanpy>=1.10\n"
            ),
        }
        for path, content in fixtures.items():
            (self.root / path).write_text(content, encoding="utf-8")

        reports = {path: self._help(path) for path in fixtures}
        r_names = self._by_name(reports["analysis.R"])
        js_names = self._by_name(reports["app.js"])
        snake_names = self._by_name(reports["Snakefile"])
        shell_names = self._by_name(reports["workflow.sh"])
        yaml_names = self._by_name(reports["environment.yml"])

        self.assertTrue({"deseq2", "edger", "mysteryr"} <= set(r_names))
        self.assertTrue({"react", "@private-scope/lab-tool"} <= set(js_names))
        self.assertIsNone(js_names["@private-scope/lab-tool"]["docs_url"])
        self.assertTrue(
            {"snakemake", "snakemake conda integration", "samtools", "pandas"}
            <= set(snake_names)
        )
        self.assertIn("samtools", shell_names)
        self.assertNotIn("mystery-aligner", shell_names)
        self.assertTrue({"python", "samtools", "pip", "scanpy"} <= set(yaml_names))
        self.assertEqual(yaml_names["scanpy"]["ecosystem"], "python")

    def test_shell_help_omits_coreutils_and_custom_commands(self) -> None:
        (self.root / "commands.sh").write_text(
            "rm -f temporary.txt\n"
            "awk '{print $1}' samples.tsv\n"
            "./scripts/custom-align --input reads.fq\n"
            "/usr/bin/samtools view input.bam\n"
            "echo samtools is documented in a comment-like message\n",
            encoding="utf-8",
        )

        packages = self._by_name(self._help("commands.sh"))

        self.assertEqual(set(packages), {"samtools"})

    def test_code_help_rejects_traversal_and_symlinks(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.py"
        outside.write_text("import numpy\n", encoding="utf-8")
        try:
            (self.root / "linked.py").symlink_to(outside)
            with mock.patch(
                "benchtop.server._require_pipeline",
                return_value=self.pipeline,
            ):
                for path in ("../outside.py", "linked.py"):
                    with (
                        self.subTest(path=path),
                        self.assertRaises(HTTPException) as error,
                    ):
                        server.code_help("pipe", server.CodeHelpRequest(path=path))
                    self.assertEqual(error.exception.status_code, 400)
        finally:
            outside.unlink(missing_ok=True)


class CodeSaveConflictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pipeline = Pipeline(name="pipe", path=self.root, kind="snakemake")
        self.target = self.root / "workflow.py"
        self.target.write_text("original = True\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _read(self) -> dict:
        with mock.patch(
            "benchtop.server._require_pipeline",
            return_value=self.pipeline,
        ):
            return server.code_read_private(
                "pipe",
                server.CodeHelpRequest(path="workflow.py"),
            )

    def _save(self, **overrides: object) -> dict:
        payload: dict[str, object] = {
            "path": "workflow.py",
            "content": "updated = True\n",
            **overrides,
        }
        with (
            mock.patch(
                "benchtop.server._require_pipeline",
                return_value=self.pipeline,
            ),
            mock.patch("benchtop.server._backup_file", return_value="backup/ref"),
        ):
            return server.code_save("pipe", server.CodeSave(**payload))

    def test_read_revision_allows_an_unchanged_optimistic_save(self) -> None:
        opened = self._read()

        saved = self._save(expected_revision=opened["revision"])

        self.assertEqual(self.target.read_text(encoding="utf-8"), "updated = True\n")
        self.assertEqual(saved["revision"], server._code_revision("updated = True\n"))
        self.assertEqual(saved["backup"], "backup/ref")

    def test_stale_revision_rejects_without_overwriting_external_change(self) -> None:
        opened = self._read()
        self.target.write_text("external = True\n", encoding="utf-8")

        with self.assertRaises(HTTPException) as error:
            self._save(expected_revision=opened["revision"])

        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail["code"], "external-file-change")
        self.assertEqual(
            error.exception.detail["current_revision"],
            server._code_revision("external = True\n"),
        )
        self.assertEqual(self.target.read_text(encoding="utf-8"), "external = True\n")

    def test_existing_file_requires_a_revision_or_explicit_overwrite(self) -> None:
        with self.assertRaises(HTTPException) as error:
            self._save()
        self.assertEqual(error.exception.status_code, 428)
        self.assertEqual(error.exception.detail["code"], "save-precondition-required")

        saved = self._save(overwrite=True)
        self.assertEqual(saved["revision"], server._code_revision("updated = True\n"))

    def test_removed_file_conflicts_but_new_file_without_revision_is_allowed(
        self,
    ) -> None:
        opened = self._read()
        self.target.unlink()
        with self.assertRaises(HTTPException) as error:
            self._save(expected_revision=opened["revision"])
        self.assertEqual(error.exception.status_code, 409)

        created = self._save()
        self.assertTrue(self.target.is_file())
        self.assertEqual(created["revision"], server._code_revision("updated = True\n"))


class PipelineBrowseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.outside_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.outside = Path(self.outside_tmp.name)
        (self.root / "config").mkdir()
        (self.root / "data").mkdir()
        (self.root / "results").mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "config" / "workflow.py").write_text(
            "import pandas\n",
            encoding="utf-8",
        )
        (self.root / "scripts" / "analyze.py").write_text(
            "print('analysis')\n",
            encoding="utf-8",
        )
        (self.root / "results" / "report.html").write_text(
            "<h1>Report</h1>\n",
            encoding="utf-8",
        )
        (self.root / "data" / "reads.fastq.gz").write_bytes(b"reads")
        (self.root / "large.bin").write_bytes(b"")
        with (self.root / "large.bin").open("r+b") as handle:
            handle.truncate(server.BROWSE_OPEN_FILE_LIMIT + 1)
        (self.root / ".env").write_text("SECRET=hidden\n", encoding="utf-8")
        (self.outside / "secret.txt").write_text("outside\n", encoding="utf-8")
        (self.root / "linked-dir").symlink_to(self.outside, target_is_directory=True)
        (self.root / "linked-file.txt").symlink_to(self.outside / "secret.txt")
        deep = self.root
        for index in range(server.BROWSE_MAX_DEPTH + 1):
            deep = deep / f"level-{index}"
            deep.mkdir()
        (deep / "too-deep.txt").write_text("deep\n", encoding="utf-8")
        self.pipeline = Pipeline(name="pipe", path=self.root, kind="snakemake")

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.outside_tmp.cleanup()

    def _search(self, **values) -> dict:
        body = server.BrowseSearchRequest(**values)
        with mock.patch(
            "benchtop.server._require_pipeline",
            return_value=self.pipeline,
        ):
            return server.pipeline_browse_search("pipe", body)

    def test_root_and_directory_browsing_returns_breadcrumbs_and_actions(self) -> None:
        root = self._search(path="", query="", scope="all", limit=100)
        by_path = {item["path"]: item for item in root["results"]}

        self.assertEqual(root["path"], "")
        self.assertEqual(root["breadcrumbs"], [{"name": "pipe", "path": ""}])
        self.assertEqual(by_path["config"]["open_action"], "browse")
        self.assertEqual(
            by_path["large.bin"]["size"], server.BROWSE_OPEN_FILE_LIMIT + 1
        )
        self.assertIsNone(by_path["large.bin"]["open_action"])
        self.assertNotIn(".env", by_path)
        self.assertNotIn("linked-dir", by_path)
        self.assertNotIn("linked-file.txt", by_path)
        self.assertNotIn("config/workflow.py", by_path)

        config = self._search(path="config", query="", scope="all", limit=20)
        self.assertEqual(
            config["breadcrumbs"],
            [
                {"name": "pipe", "path": ""},
                {"name": "config", "path": "config"},
            ],
        )
        self.assertEqual(config["results"][0]["path"], "config/workflow.py")
        self.assertEqual(config["results"][0]["kind"], "config")
        self.assertEqual(config["results"][0]["open_action"], "code")
        self.assertEqual(config["results"][0]["parent"], "config")

    def test_recursive_search_is_bounded_and_never_follows_symlinks(self) -> None:
        found = self._search(query="workflow", scope="files", limit=20)
        self.assertEqual(
            [item["path"] for item in found["results"]],
            ["config/workflow.py"],
        )

        escaped = self._search(query="secret", scope="all", limit=20)
        self.assertEqual(escaped["results"], [])
        too_deep = self._search(query="too-deep", scope="files", limit=20)
        self.assertEqual(too_deep["results"], [])
        self.assertTrue(too_deep["truncated"])
        self.assertIn("depth cap", too_deep["truncated_reason"])
        self.assertLessEqual(too_deep["scanned"], server.BROWSE_SCAN_LIMIT)

    def test_browse_scopes_use_deterministic_categories(self) -> None:
        cases = {
            "code": ("analyze", "scripts/analyze.py", "code", "code"),
            "config": ("workflow", "config/workflow.py", "config", "code"),
            "results": ("report", "results/report.html", "results", "results"),
        }
        for scope, (query, path, kind, action) in cases.items():
            with self.subTest(scope=scope):
                report = self._search(query=query, scope=scope, limit=20)
                self.assertEqual([item["path"] for item in report["results"]], [path])
                self.assertEqual(report["results"][0]["kind"], kind)
                self.assertEqual(report["results"][0]["category"], kind)
                self.assertEqual(report["results"][0]["open_action"], action)

        directories = self._search(path="", query="", scope="directories", limit=100)
        self.assertTrue(directories["results"])
        self.assertTrue(
            all(item["kind"] == "directory" for item in directories["results"])
        )
        files = self._search(path="data", query="", scope="files", limit=20)
        self.assertEqual(
            [item["path"] for item in files["results"]], ["data/reads.fastq.gz"]
        )

        limited = self._search(path="", query="", scope="all", limit=1)
        self.assertTrue(limited["truncated"])
        self.assertIn("result limit", limited["truncated_reason"])

    def test_browse_rejects_traversal_symlink_roots_and_unbounded_models(self) -> None:
        for values in (
            {"path": "../outside"},
            {"path": "linked-dir"},
            {"query": "../outside"},
        ):
            with self.subTest(values=values), self.assertRaises(HTTPException):
                self._search(**values)

        alias = self.root.parent / f"{self.root.name}-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        try:
            linked_pipeline = Pipeline(name="linked", path=alias, kind="snakemake")
            with (
                mock.patch(
                    "benchtop.server._require_pipeline",
                    return_value=linked_pipeline,
                ),
                self.assertRaises(HTTPException),
            ):
                server.pipeline_browse_search("linked", server.BrowseSearchRequest())
        finally:
            alias.unlink(missing_ok=True)

        for values in (
            {"limit": server.BROWSE_MAX_LIMIT + 1},
            {"query": "q" * (server.BROWSE_MAX_QUERY + 1)},
            {"scope": "secrets"},
            {"path": "p" * (server.fs_policy.MAX_RELATIVE_PATH + 1)},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                server.BrowseSearchRequest(**values)

    def test_open_is_post_only_path_fenced_and_size_capped(self) -> None:
        with mock.patch(
            "benchtop.server._require_pipeline",
            return_value=self.pipeline,
        ):
            response = server.pipeline_browse_open(
                "pipe",
                server.BrowseOpenRequest(path="data/reads.fastq.gz"),
            )
            self.assertIsInstance(response, FileResponse)
            self.assertEqual(Path(response.path), self.root / "data" / "reads.fastq.gz")
            with self.assertRaises(HTTPException) as large:
                server.pipeline_browse_open(
                    "pipe",
                    server.BrowseOpenRequest(path="large.bin"),
                )
            self.assertEqual(large.exception.status_code, 413)
            for path in ("../outside", "linked-file.txt"):
                with self.subTest(path=path), self.assertRaises(HTTPException):
                    server.pipeline_browse_open(
                        "pipe",
                        server.BrowseOpenRequest(path=path),
                    )

    def test_open_rejects_a_direct_hardlink_request(self) -> None:
        hardlink = self.root / "data" / "linked.fastq.gz"
        try:
            os.link(self.root / "data" / "reads.fastq.gz", hardlink)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        with (
            mock.patch(
                "benchtop.server._require_pipeline",
                return_value=self.pipeline,
            ),
            self.assertRaises(HTTPException) as error,
        ):
            server.pipeline_browse_open(
                "pipe",
                server.BrowseOpenRequest(path="data/linked.fastq.gz"),
            )
        self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
