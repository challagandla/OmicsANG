# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Regression tests for bounded presentation work and fresh authorization."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from benchtop import pipelines, results, server, settings

REPO = Path(__file__).resolve().parents[1]


class DisplayPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.checkout = self.workspace / "pipe"
        self.outputs = self.checkout / "results"
        self.outputs.mkdir(parents=True)
        (self.checkout / "README.md").write_text("fixture")
        patcher = mock.patch.multiple(
            settings,
            ROOT=self.workspace,
            STATE_DIR=self.root / "state",
            STATE_FILE=self.root / "state" / "state.json",
            RUN_LOG_DIR=self.root / "state" / "sessions",
            RESULTS_ROOTS=(self.workspace,),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        results.invalidate_inventory()
        self.addCleanup(results.invalidate_inventory)
        self.pipeline = pipelines.get("pipe")

    def test_lookup_does_not_discover_siblings_and_reads_fresh_metadata(self):
        with mock.patch.object(pipelines, "discover", side_effect=AssertionError):
            self.assertEqual(pipelines.get("pipe").kind, "unknown")
            (self.checkout / "Snakefile").write_text("rule all:\n    input: []\n")
            self.assertEqual(pipelines.get("pipe").kind, "snakemake")
        (self.workspace / "alias").symlink_to(self.checkout)
        self.assertIsNone(pipelines.get("alias"))
        self.assertIsNone(pipelines.get("../pipe"))

    def test_summary_avoids_git_and_environment_probes(self):
        with (
            mock.patch.object(server.git_ops, "status", side_effect=AssertionError),
            mock.patch.object(
                server.git_ops, "list_worktrees", side_effect=AssertionError
            ),
            mock.patch.object(pipelines, "real_envs", side_effect=AssertionError),
            mock.patch.object(server.store, "history", return_value=[]),
        ):
            summary = server.pipeline_detail("pipe", summary=True)
        self.assertTrue(summary["summary_only"])
        self.assertEqual(summary["name"], "pipe")
        self.assertNotIn("git", summary)

    def test_boot_summary_avoids_environment_discovery(self):
        with mock.patch.object(pipelines, "real_envs", side_effect=AssertionError):
            summary = server.meta(summary=True)
        self.assertEqual(summary["envs"], [])
        self.assertIn("workspace_id", summary)

    def test_source_scan_never_enters_excluded_or_symlink_directories(self):
        (self.outputs / "hidden.py").write_text("hidden")
        (self.checkout / "main.py").write_text("pass")
        external = self.root / "external"
        external.mkdir()
        (external / "secret.py").write_text("secret")
        (self.checkout / "linked").symlink_to(external)
        real_scandir = server.os.scandir
        entered = []

        def scandir(path):
            entered.append(Path(path))
            return real_scandir(path)

        scan = {}
        with mock.patch.object(server.os, "scandir", side_effect=scandir):
            files = list(server._iter_code_files(self.checkout, scan=scan))
        self.assertNotIn(self.outputs, entered)
        self.assertNotIn(external, entered)
        self.assertIn(self.checkout / "main.py", files)
        self.assertFalse(scan["truncated"])

    def test_source_scan_reports_incomplete_at_file_limit(self):
        for i in range(3):
            (self.checkout / f"code{i}.py").write_text("pass")
        scan = {}
        self.assertEqual(
            len(list(server._iter_code_files(self.checkout, 1, scan=scan))), 1
        )
        self.assertTrue(scan["truncated"])

    def test_pages_and_search_reuse_inventory_without_losing_images(self):
        for i in range(225):
            (self.outputs / f"plot-{i:03}.png").write_bytes(b"png")
        with mock.patch.object(
            results, "_gather_sources", wraps=results._gather_sources
        ) as scan:
            pages = [results.page(self.pipeline, offset=i) for i in (0, 60, 120, 180)]
            match = results.page(self.pipeline, query="plot-224", kind="figures")
        self.assertEqual(scan.call_count, 1)
        names = [item["name"] for page in pages for item in page["images"]]
        self.assertEqual(len(names), 225)
        self.assertEqual(len(set(names)), 225)
        self.assertEqual(match["pagination"]["total"], 1)
        self.assertEqual(match["images"][0]["name"], "plot-224.png")
        self.assertFalse(pages[-1]["pagination"]["has_more"])

    def test_nested_changes_refresh_and_expire(self):
        nested = self.outputs / "nested"
        nested.mkdir()
        results.page(self.pipeline)
        (nested / "new.png").write_bytes(b"png")
        self.assertEqual(results.page(self.pipeline)["counts"]["images"], 0)
        self.assertEqual(
            results.page(self.pipeline, refresh=True)["counts"]["images"], 1
        )
        (nested / "second.png").write_bytes(b"png")
        with mock.patch.object(results, "INVENTORY_TTL", -1):
            self.assertEqual(results.page(self.pipeline)["counts"]["images"], 2)
        (nested / "third.png").write_bytes(b"png")
        results.invalidate_inventory()
        self.assertEqual(results.page(self.pipeline)["counts"]["images"], 3)

    def test_cached_inventory_cannot_retain_revoked_external_source(self):
        external = self.workspace / "external"
        external.mkdir()
        (external / "external.png").write_bytes(b"png")
        results.attach(self.pipeline, str(external))
        self.assertEqual(results.page(self.pipeline)["counts"]["images"], 1)
        with mock.patch.object(settings, "RESULTS_ROOTS", ()):
            self.assertEqual(results.page(self.pipeline)["counts"]["images"], 0)
        results.detach(self.pipeline, str(external))
        self.assertEqual(results.page(self.pipeline)["counts"]["images"], 0)

    def test_concurrent_pages_share_one_scan(self):
        (self.outputs / "plot.png").write_bytes(b"png")
        # Initialize disposable state before concurrent readers.
        results.attachments(self.pipeline)
        with mock.patch.object(
            results, "_gather_sources", wraps=results._gather_sources
        ) as scan:
            with ThreadPoolExecutor(max_workers=4) as pool:
                pages = list(pool.map(lambda _: results.page(self.pipeline), range(4)))
        self.assertEqual(scan.call_count, 1)
        self.assertTrue(all(page["counts"]["images"] == 1 for page in pages))

    def test_directory_bound_is_reported_as_partial_inventory(self):
        nested = self.outputs / "nested"
        nested.mkdir()
        (nested / "plot.png").write_bytes(b"png")
        with mock.patch.object(results, "MAX_GALLERY_DIRS_PER_SOURCE", 1):
            page = results.page(self.pipeline)
        self.assertTrue(page["scan"]["truncated"])

    def test_invalidation_during_scan_cannot_repopulate_cache(self):
        original = results._gather_sources

        def interrupted(*args, **kwargs):
            payload = original(*args, **kwargs)
            results.invalidate_inventory()
            return payload

        with mock.patch.object(
            results, "_gather_sources", side_effect=interrupted
        ) as scan:
            results.page(self.pipeline)
            results.page(self.pipeline)
        self.assertEqual(scan.call_count, 2)


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class BrowserPerformanceTest(unittest.TestCase):
    def test_cache_and_polling_lifecycle(self):
        script = r"""
const assert = require('node:assert/strict');
const {createDisplayCache, createPoller} = require('./benchtop/web/ui-performance.js');
(async () => {
  let now = 0, calls = 0, finish;
  const cache = createDisplayCache({ttl: 10, now: () => now});
  const loader = () => { calls++; return new Promise(resolve => { finish = resolve; }); };
  const first = cache.read('a', loader), second = cache.read('a', loader);
  await Promise.resolve();
  assert.equal(calls, 1);
  finish({value: 1});
  const values = await Promise.all([first, second]);
  values[0].value = 9;
  assert.equal((await cache.read('a', loader)).value, 1);
  now = 11;
  assert.equal((await cache.read('a', () => ({value: 2}))).value, 2);
  const old = cache.read('pending', loader);
  await Promise.resolve();
  cache.clear();
  await cache.read('pending', () => ({value: 3}));
  finish({value: 0}); await old;
  assert.equal((await cache.read('pending', loader)).value, 3);
  await assert.rejects(cache.read('error', () => Promise.reject(new Error('offline'))));
  assert.equal(await cache.read('error', () => 'recovered'), 'recovered');

  let visible = true, active = true, executions = 0, end;
  const timers = new Map(); let id = 0;
  const poller = createPoller({
    task: () => { executions++; return new Promise(resolve => { end = resolve; }); },
    visible: () => visible, delay: () => active ? 2500 : 15000,
    schedule: (fn, ms) => { timers.set(++id, {fn, ms}); return id; },
    cancel: id => timers.delete(id),
  });
  const p1 = poller.run(), p2 = poller.run();
  await Promise.resolve(); assert.equal(executions, 1);
  visible = false; poller.visibilityChanged();
  end(); await Promise.all([p1, p2]); assert.equal(timers.size, 0);
  await poller.run(); assert.equal(executions, 1);
  visible = true; active = false; poller.visibilityChanged();
  await Promise.resolve(); assert.equal(executions, 2);
  const p3 = poller.run(); end(); await p3;
  assert.equal(timers.size, 1); assert.equal([...timers.values()][0].ms, 15000);
  active = true;
  const p4 = poller.run(); await Promise.resolve(); end(); await p4;
  assert.equal(timers.size, 1); assert.equal([...timers.values()][0].ms, 2500);
  visible = false; poller.visibilityChanged(); assert.equal(timers.size, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script], cwd=REPO, capture_output=True, text=True, timeout=20
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_late_pipeline_response_cannot_replace_new_selection(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('./benchtop/web/app.js', 'utf8');
const start = source.indexOf('async function selectPipeline(name)');
const end = source.indexOf('/* ---------------- tabs', start);
const pending = new Map();
const context = {
  state: {navigation: {restoreToken: 0}, tab: 'overview'},
  renderSidebar() {}, render() {},
  displayRead: url => new Promise(resolve => pending.set(url, resolve)),
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
(async () => {
  const a = context.selectPipeline('alpha');
  const b = context.selectPipeline('beta');
  pending.get('/api/pipelines/beta?summary=true')({name: 'beta'}); await b;
  pending.get('/api/pipelines/alpha?summary=true')({name: 'alpha'}); await a;
  assert.equal(context.state.detail.name, 'beta');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script], cwd=REPO, capture_output=True, text=True, timeout=20
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
