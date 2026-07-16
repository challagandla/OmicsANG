# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
from __future__ import annotations

import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDITOR_CORE = ROOT / "benchtop" / "web" / "editor-core.js"
INDEX_HTML = ROOT / "benchtop" / "web" / "index.html"
APP_JS = ROOT / "benchtop" / "web" / "app.js"
STYLES_CSS = ROOT / "benchtop" / "web" / "styles.css"


class EditorAssetContractTests(unittest.TestCase):
    def test_editor_core_loads_before_the_application(self) -> None:
        source = INDEX_HTML.read_text(encoding="utf-8")
        self.assertLess(
            source.index("/editor-core.js?v=20260716-restore"),
            source.index("/app.js?v=20260716-restore"),
        )
        self.assertIn("window.OmicsEditorCore", APP_JS.read_text(encoding="utf-8"))

    def test_editor_chrome_and_accessibility_fallback_are_shipped(self) -> None:
        app = APP_JS.read_text(encoding="utf-8")
        styles = STYLES_CSS.read_text(encoding="utf-8")
        for contract in (
            "code-line-numbers",
            "code-indent-layer",
            "code-active-line",
            "code-editor-statusbar",
            "code-inline-completion",
            "code-line-hover",
            "EditorCore.applyIndentEdit",
            "EditorCore.applyEnterEdit",
            "EditorCore.toggleLineComment",
            "EditorCore.inlineCompletionAt",
            "EditorCore.explainLine",
            "acceptInlineCompletion",
            "scheduleLineHoverHelp",
        ):
            self.assertIn(contract, app)
        self.assertIn("@media (forced-colors: active)", styles)
        self.assertIn("-webkit-text-fill-color: CanvasText", styles)
        self.assertIn(".code-editor-wrap.large-file-mode .code-editor", styles)
        self.assertIn(".code-inline-completion", styles)
        self.assertIn(".code-line-hover", styles)
        self.assertIn("min-height: 0; resize: none", styles)
        self.assertIn("if (line.length > 5000) return escapeHtml(line);", app)
        self.assertIn("HIGHLIGHT_RANGE_LIMIT = 12000", app)
        self.assertIn("Press Tab or Shift+Tab to move focus out", app)
        self.assertIn("omicsang:editor-metrics", app)
        self.assertIn("that path is already open in another tab", app)
        self.assertIn("const selectedPath = comparablePath(c.selected)", app)
        self.assertIn("caret <= firstCode ? lineStart : firstCode", app)


@unittest.skipUnless(shutil.which("node"), "Node.js is required for editor tests")
class EditorCoreBehaviorTests(unittest.TestCase):
    def test_dependency_free_editing_primitives(self) -> None:
        script = textwrap.dedent(
            r"""
            const assert = require('node:assert/strict');
            const core = require('./benchtop/web/editor-core.js');

            assert.deepEqual(
              core.detectIndentation('def f():\n    if ready:\n        return 1\n', 4),
              {mode: 'spaces', size: 4},
            );
            assert.deepEqual(
              core.detectIndentation('sample:\n  reads:\n    - R1.fastq.gz\n', 2),
              {mode: 'spaces', size: 2},
            );
            assert.deepEqual(
              core.detectIndentation('root\n    one\n        two\n        three\n', 4),
              {mode: 'spaces', size: 4},
            );
            assert.deepEqual(
              core.detectIndentation('root\n  one\n    two\n    three\n', 2),
              {mode: 'spaces', size: 2},
            );
            assert.deepEqual(
              core.detectIndentation('root\n  one\n    a\n    b\n    c\n    d\n    e\n', 2),
              {mode: 'spaces', size: 2},
            );
            assert.deepEqual(
              core.detectIndentation('rule:\n\tinput:\n\t\t"reads.fq"\n', 4),
              {mode: 'tabs', size: 4},
            );

            assert.deepEqual(core.cursorPosition('\tvalue', 1, 4), {
              line: 1, lineIndex: 0, column: 2, visualColumn: 5, lineStart: 0,
            });
            const cachedStarts = core.lineStarts('one\n\tvalue');
            assert.deepEqual(core.cursorPositionFromStarts('one\n\tvalue', 5, cachedStarts, 4), {
              line: 2, lineIndex: 1, column: 2, visualColumn: 5, lineStart: 4,
            });
            assert.deepEqual(core.guideColumns('        value', 4), [4, 8]);
            assert.deepEqual(
              core.activeIndentScope(['if x:', '    one()', '    two()', 'tail()'], 2, 4),
              {column: 4, start: 1, end: 2},
            );
            assert.equal(
              core.activeIndentScope(['root', '        nested', '    ', 'tail'], 2, 4).column,
              4,
            );

            const fastqcLine = 'fastqc --threads 4 reads.fastq.gz';
            assert.deepEqual(
              core.commandContextAt(fastqcLine, fastqcLine.indexOf('--threads') + 4, ['fastqc']),
              {
                command: 'fastqc', option: '--threads', line: 1, commandLine: 1,
                context: fastqcLine,
              },
            );
            assert.equal(
              core.commandContextAt(fastqcLine, fastqcLine.indexOf('4'), ['fastqc']).option,
              '--threads',
            );
            assert.equal(
              core.commandContextAt('fastqc --memory=1024 reads.fq', 18, ['fastqc']).option,
              '--memory',
            );
            const pathCommand = '/usr/bin/fastqc -t 2 reads.fq';
            assert.equal(
              core.commandContextAt(pathCommand, pathCommand.indexOf('2'), ['fastqc']).option,
              '-t',
            );
            const envCommand = 'env SAMPLE=x fastqc --outdir results reads.fq';
            assert.equal(
              core.commandContextAt(envCommand, envCommand.indexOf('results') + 2, ['fastqc']).option,
              '--outdir',
            );
            const continued = 'fastqc \\\n  --memory 1024 \\\n  reads.fastq.gz';
            assert.deepEqual(
              core.commandContextAt(continued, continued.indexOf('1024') + 2, ['fastqc']),
              {
                command: 'fastqc', option: '--memory', line: 2, commandLine: 1,
                context: '--memory 1024 \\',
              },
            );
            const snakeShell = 'rule qc:\n    shell: "fastqc --quiet --threads 2 {input}"';
            assert.equal(
              core.commandContextAt(snakeShell, snakeShell.indexOf('--quiet') + 3, ['fastqc']).command,
              'fastqc',
            );
            assert.equal(core.commandContextAt('# fastqc --threads 8', 17, ['fastqc']), null);
            assert.equal(core.commandContextAt('echo fastqc --threads 8 # fastqc', 30, ['fastqc']), null);
            assert.equal(core.commandContextAt('multiqc --threads 2', 10, ['fastqc']), null);
            assert.equal(core.commandContextAt('x'.repeat(20001) + ' fastqc --quiet', 20010, ['fastqc']), null);

            const localCompletionText = 'sample_count = 3\nsam';
            assert.deepEqual(
              core.inlineCompletionAt(localCompletionText, localCompletionText.length, {language: 'python'}),
              {
                prefix: 'sam', candidate: 'sample_count', suffix: 'ple_count',
                source: 'document', start: 17, end: 20,
              },
            );
            assert.deepEqual(core.inlineCompletionAt('im', 2, {language: 'python'}), {
              prefix: 'im', candidate: 'import', suffix: 'port',
              source: 'language', start: 0, end: 2,
            });
            assert.deepEqual(core.inlineCompletionAt('thr', 3, {path: 'workflow/Snakefile'}), {
              prefix: 'thr', candidate: 'threads', suffix: 'eads',
              source: 'language', start: 0, end: 3,
            });
            assert.equal(core.inlineCompletionAt('sample_count', 3, {language: 'python'}), null);
            assert.equal(core.inlineCompletionAt('r', 1, {language: 'python'}), null);
            assert.equal(core.inlineCompletionAt('x'.repeat(130000) + '\ndis', 130004), null);

            assert.equal(
              core.explainLine('def align_reads(sample):', {language: 'python', lineNumber: 12}),
              'Line 12: Defines the Python function "align_reads".',
            );
            assert.equal(
              core.explainLine('threads: 8', {path: 'workflow/Snakefile', lineNumber: 4}),
              'Line 4: Sets the thread request for the current Snakemake rule.',
            );
            assert.equal(
              core.explainLine('samples:', {path: 'config.yaml', lineNumber: 2}),
              'Line 2: Opens the YAML mapping "samples".',
            );
            assert.equal(
              core.explainLine('rm -rf "$OUT"', {language: 'shell', lineNumber: 3}),
              'Line 3: Potentially destructive shell operation; verify its paths and inputs before running it.',
            );
            assert.equal(
              core.explainLine('# why this exists', {language: 'python'}),
              'Comment for human context; it is not executed as code.',
            );
            assert.ok(
              core.explainLine('mystery ' + 'x'.repeat(10000), {language: 'python'}).length < 180,
            );

            const tab = core.applyIndentEdit('abc', 1, 1, {mode: 'spaces', size: 4});
            assert.equal(tab.value, 'a   bc');
            assert.deepEqual([tab.start, tab.end], [4, 4]);

            const selected = core.applyIndentEdit('a\nb\nc', 0, 2, {mode: 'spaces', size: 2});
            assert.equal(selected.value, '  a\nb\nc');
            const outdented = core.applyIndentEdit(
              '  a\n    b', 0, 9, {mode: 'spaces', size: 2, outdent: true},
            );
            assert.equal(outdented.value, 'a\n  b');
            const mixed = core.applyIndentEdit(
              '  \tvalue', 3, 3, {mode: 'spaces', size: 4, outdent: true},
            );
            assert.equal(mixed.value, 'value');
            assert.ok(core.indentationColumns(mixed.value, 4) < 4);
            const collapsed = core.applyIndentEdit(
              '        value', 8, 8, {mode: 'spaces', size: 4, outdent: true},
            );
            assert.equal(collapsed.value, '    value');
            assert.deepEqual([collapsed.start, collapsed.end], [4, 4]);
            const lineStartCaret = core.applyIndentEdit(
              '        value', 0, 0, {mode: 'spaces', size: 4, outdent: true},
            );
            assert.deepEqual([lineStartCaret.start, lineStartCaret.end], [0, 0]);
            const expandingPrefix = core.applyIndentEdit(
              '\t  x', 0, 0, {mode: 'spaces', size: 4, outdent: true},
            );
            assert.equal(expandingPrefix.value, '    x');
            assert.deepEqual([expandingPrefix.start, expandingPrefix.end], [0, 0]);
            const codeOnlySelection = core.applyIndentEdit(
              '        alpha\n        beta', 8, 13,
              {mode: 'spaces', size: 4, outdent: true},
            );
            assert.equal(codeOnlySelection.value, '    alpha\n        beta');
            assert.deepEqual([codeOnlySelection.start, codeOnlySelection.end], [4, 9]);
            const tabCaret = core.applyIndentEdit(
              '\t\tvalue', 2, 2, {mode: 'tabs', size: 4, outdent: true},
            );
            assert.deepEqual([tabCaret.start, tabCaret.end], [1, 1]);
            const initialBlank = core.applyIndentEdit(
              '\nvalue', 0, 1, {mode: 'spaces', size: 2},
            );
            assert.equal(initialBlank.value, '  \nvalue');

            const entered = core.applyEnterEdit(
              'if ready:', 9, 9, {mode: 'spaces', size: 4, language: 'python'},
            );
            assert.equal(entered.value, 'if ready:\n    ');
            const prose = core.applyEnterEdit(
              'Note:', 5, 5, {mode: 'spaces', size: 4, language: 'markdown', path: 'README.md'},
            );
            assert.equal(prose.value, 'Note:\n');
            const paired = core.applyEnterEdit('{}', 1, 1, {mode: 'spaces', size: 2});
            assert.equal(paired.value, '{\n  \n}');
            assert.equal(paired.start, 4);

            const commented = core.toggleLineComment(
              '  one\n  two', 0, 11, {language: 'python'},
            );
            assert.equal(commented.value, '  # one\n  # two');
            const uncommented = core.toggleLineComment(
              commented.value, 0, commented.value.length, {language: 'python'},
            );
            assert.equal(uncommented.value, '  one\n  two');
            assert.equal(
              core.toggleLineComment('\nvalue', 0, 1, {language: 'python'}).value,
              '\nvalue',
            );
            assert.equal(
              core.toggleLineComment('{"ok": true}', 0, 0, {language: 'json'}).handled,
              false,
            );
            assert.equal(
              core.toggleLineComment('#\tfoo', 0, 5, {language: 'python'}).value,
              'foo',
            );
            """
        )
        completed = subprocess.run(
            [shutil.which("node") or "node", "-e", script],
            cwd=ROOT,
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == "__main__":
    unittest.main()
