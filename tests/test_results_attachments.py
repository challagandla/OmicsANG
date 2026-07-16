# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.responses import FileResponse

from benchtop import provenance, qc, results, server, settings, state_db, store
from benchtop.pipelines import Pipeline


class ResultsAttachmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.allowed = self.workspace / "allowed"
        self.pipeline_root = self.allowed / "pipeline"
        self.external = self.allowed / "RESULTS" / "run-one"
        self.outside = self.workspace / "outside"
        for directory in (
            self.pipeline_root / "results",
            self.external,
            self.outside,
        ):
            directory.mkdir(parents=True)
        self.pipeline = Pipeline(
            name="pipe",
            path=self.pipeline_root,
            kind="snakemake",
        )
        self.old_state_dir = settings.STATE_DIR
        self.old_state_file = settings.STATE_FILE
        self.old_run_log_dir = settings.RUN_LOG_DIR
        self.old_result_roots = settings.RESULTS_ROOTS
        settings.STATE_DIR = Path(self.state_tmp.name)
        settings.STATE_FILE = settings.STATE_DIR / "state.json"
        settings.RUN_LOG_DIR = settings.STATE_DIR / "sessions"
        settings.RESULTS_ROOTS = (self.allowed,)

    def tearDown(self) -> None:
        settings.STATE_DIR = self.old_state_dir
        settings.STATE_FILE = self.old_state_file
        settings.RUN_LOG_DIR = self.old_run_log_dir
        settings.RESULTS_ROOTS = self.old_result_roots
        self.state_tmp.cleanup()
        self.tmp.cleanup()

    def test_persistence_checkout_isolation_and_idempotence(self) -> None:
        first, created = results.attach(self.pipeline, str(self.external))
        second, created_again = results.attach(self.pipeline, str(self.external))

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["source_id"], second["source_id"])
        self.assertEqual(
            store.results_attachments(self.pipeline.path),
            [str(self.external.resolve())],
        )
        same_checkout = Pipeline(
            name="renamed",
            path=self.pipeline_root,
            kind="snakemake",
        )
        self.assertEqual(len(results.attachments(same_checkout)), 1)
        other_root = self.allowed / "other-pipeline"
        other_root.mkdir()
        self.assertEqual(store.results_attachments(other_root), [])

        database = Path(store.database_status()["path"])
        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT key,value_json FROM configuration "
                "WHERE key LIKE 'results_attachments:%'",
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn(str(self.external.resolve()), rows[0][1])

        key, checkout = store._results_attachment_key(self.pipeline.path)
        state_db.set_configuration(
            key,
            {
                "version": 1,
                "pipeline_root": checkout,
                "paths": ["bad\x00path"],
            },
        )
        malformed = results.attachments(self.pipeline)
        self.assertEqual(len(malformed), 1)
        self.assertFalse(malformed[0]["available"])
        self.assertTrue(malformed[0]["source_id"].startswith("result-"))
        remaining, removed = results.detach(self.pipeline, malformed[0]["path"])
        self.assertTrue(removed)
        self.assertEqual(remaining, [])

    def test_authorized_external_configured_gallery_uses_source_relative_paths(
        self,
    ) -> None:
        configured = self.external
        (configured / "plot.png").write_bytes(b"png")
        config = self.pipeline_root / "config.yaml"
        config.write_text(f"results_dir: {configured}\n", encoding="utf-8")
        self.pipeline.configs = ["config.yaml"]

        gallery = results.gather(self.pipeline)

        self.assertIn(str(configured.resolve()), gallery["roots"])
        entry = next(item for item in gallery["images"] if item["name"] == "plot.png")
        self.assertEqual(entry["path"], "plot.png")
        self.assertEqual(
            entry["display_path"],
            f"{configured.resolve()}/plot.png",
        )
        self.assertNotIn("legacy_path", entry)
        self.assertEqual(
            results.resolve_source_file(
                self.pipeline,
                entry["source_id"],
                entry["path"],
            ),
            (configured / "plot.png").resolve(),
        )

    def test_configured_gallery_outside_allowlist_is_not_exposed(self) -> None:
        configured = self.outside / "configured-results"
        configured.mkdir()
        (configured / "private-plot.png").write_bytes(b"png")
        config = self.pipeline_root / "config.yaml"
        config.write_text(f"results_dir: {configured}\n", encoding="utf-8")
        self.pipeline.configs = ["config.yaml"]

        gallery = results.gather(self.pipeline)

        self.assertNotIn(str(configured.resolve()), gallery["roots"])
        self.assertFalse(
            any(item["name"] == "private-plot.png" for item in gallery["images"])
        )
        with self.assertRaises(results.ResultPathError):
            results.resolve_source_file(
                self.pipeline,
                results._source_id(configured),
                "private-plot.png",
            )

    def test_attach_detach_qc_and_provenance_boundary(self) -> None:
        (self.external / "S1.flagstat.txt").write_text(
            "100 + 0 in total\n90 + 0 mapped (90.0% : N/A)\n",
            encoding="utf-8",
        )
        attachment, created = results.attach(self.pipeline, str(self.external))

        self.assertTrue(created)
        self.assertIn(
            self.external.resolve(),
            [
                path.resolve()
                for path in results._results_dirs(
                    self.pipeline,
                    include_attachments=True,
                )
            ],
        )
        self.assertNotIn(
            self.external.resolve(),
            [path.resolve() for path in results._results_dirs(self.pipeline)],
        )
        report = qc.gather(self.pipeline)
        self.assertIn(str(self.external.resolve()), report["scanned"])
        self.assertEqual(
            next(panel for panel in report["panels"] if panel["id"] == "flagstat")[
                "rows"
            ][0][0],
            "S1",
        )
        inventory = provenance._result_inventory(self.pipeline)
        self.assertEqual(inventory["external_roots"], 0)
        self.assertFalse(
            any("S1.flagstat" in item["path"] for item in inventory["items"])
        )

        remaining, removed = results.detach(self.pipeline, attachment["path"])
        remaining_again, removed_again = results.detach(
            self.pipeline,
            attachment["path"],
        )
        self.assertTrue(removed)
        self.assertFalse(removed_again)
        self.assertEqual(remaining, [])
        self.assertEqual(remaining_again, [])

    def test_allowed_root_rejection_and_symlink_escape(self) -> None:
        with self.assertRaises(results.ResultPathError):
            results.attach(self.pipeline, str(self.outside))

        directory_link = self.allowed / "escaped-directory"
        directory_link.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(results.ResultPathError):
            results.validate_directory(self.pipeline, str(directory_link))

        secret = self.outside / "secret.txt"
        secret.write_text("do not serve", encoding="utf-8")
        (self.external / "escape.txt").symlink_to(secret)
        attachment, _ = results.attach(self.pipeline, str(self.external))
        with self.assertRaises(results.ResultPathError):
            results.resolve_source_file(
                self.pipeline,
                attachment["source_id"],
                "escape.txt",
            )
        with self.assertRaises(results.ResultPathError):
            results.resolve_source_file(
                self.pipeline,
                attachment["source_id"],
                "../outside.txt",
            )
        leaked_qc = self.outside / "leak.flagstat.txt"
        leaked_qc.write_text(
            "100 + 0 in total\n90 + 0 mapped (90.0% : N/A)\n",
            encoding="utf-8",
        )
        (self.external / "leak.flagstat.txt").symlink_to(leaked_qc)
        self.assertFalse(
            any(
                panel["id"] == "flagstat"
                for panel in qc.gather(self.pipeline)["panels"]
            )
        )

    def test_result_file_endpoint_streams_text_artifacts(self) -> None:
        table = self.external / "large.tsv"
        table.write_text("sample\tvalue\nS1\t2\n", encoding="utf-8")
        attachment, _ = results.attach(self.pipeline, str(self.external))

        with mock.patch(
            "benchtop.server._require_pipeline", return_value=self.pipeline
        ):
            response = server.pipeline_result_file(
                self.pipeline.name,
                attachment["source_id"],
                "large.tsv",
            )

        self.assertIsInstance(response, FileResponse)
        self.assertEqual(Path(response.path), table.resolve())

    def test_bounded_search_matches_and_skips_generated_directories(self) -> None:
        match = self.allowed / "RESULTS" / "project" / "run_alpha"
        match.mkdir(parents=True)
        (self.allowed / ".secret" / "run_alpha").mkdir(parents=True)
        (self.allowed / ".snakemake" / "run_alpha").mkdir(parents=True)

        found = results.search_directories(
            self.pipeline,
            query="run_alpha",
            limit=2,
        )

        self.assertEqual([item["path"] for item in found["candidates"]], [str(match)])
        self.assertTrue(found["allowed_roots"])
        self.assertLessEqual(
            found["bounds"]["visited"],
            found["bounds"]["max_visited"],
        )
        self.assertEqual(
            found["bounds"]["max_depth"],
            results.MAX_DIRECTORY_SEARCH_DEPTH,
        )
        with self.assertRaises(results.ResultPathError):
            results.search_directories(
                self.pipeline,
                root_id="allowed-unrecognized",
                limit=10,
            )

    def test_search_selects_configured_root_by_id_without_allowing_root_attach(
        self,
    ) -> None:
        configured = self.workspace / "configured-results"
        match = configured / "cohort" / "run_beta"
        match.mkdir(parents=True)
        settings.RESULTS_ROOTS = (self.allowed, configured)
        root_record = next(
            item
            for item in results.allowed_roots(self.pipeline)
            if Path(item["path"]) == configured.resolve()
        )

        found = results.search_directories(
            self.pipeline,
            root_id=root_record["root_id"],
            query="run_beta",
            limit=10,
        )
        with mock.patch(
            "benchtop.server._require_pipeline",
            return_value=self.pipeline,
        ):
            endpoint_found = server.pipeline_result_directories_search(
                self.pipeline.name,
                server.ResultDirectorySearchRequest(
                    root_id=root_record["root_id"],
                    query="run_beta",
                    limit=10,
                ),
            )

        self.assertEqual(found["root_id"], root_record["root_id"])
        self.assertEqual(
            [item["path"] for item in found["candidates"]],
            [str(match.resolve())],
        )
        self.assertEqual(endpoint_found["candidates"], found["candidates"])
        with self.assertRaises(results.ResultPathError):
            results.attach(self.pipeline, str(configured))

    def test_fair_gallery_scan_does_not_starve_attachment(self) -> None:
        for index in range(12):
            (self.pipeline_root / "results" / f"internal-{index}.png").write_bytes(b"i")
        (self.external / "attached.png").write_bytes(b"e")
        results.attach(self.pipeline, str(self.external))

        gallery = results.gather(self.pipeline, limit=2)
        names = {entry["name"] for entry in gallery["images"]}

        self.assertIn("attached.png", names)
        self.assertEqual(gallery["counts"]["images"], 2)

    def test_qc_deduplicates_overlapping_result_sources(self) -> None:
        flagstat = self.pipeline_root / "results" / "S2.flagstat.txt"
        flagstat.write_text(
            "100 + 0 in total\n90 + 0 mapped (90.0% : N/A)\n",
            encoding="utf-8",
        )
        results.attach(self.pipeline, str(self.pipeline_root))

        report = qc.gather(self.pipeline)
        panel = next(item for item in report["panels"] if item["id"] == "flagstat")

        self.assertEqual([row[0] for row in panel["rows"]], ["S2"])


if __name__ == "__main__":
    unittest.main()
