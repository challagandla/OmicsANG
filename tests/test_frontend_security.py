# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Static contracts for security-sensitive browser behavior.

The application deliberately has no frontend build toolchain.  These focused
checks complement ``node --check`` by preventing native browser navigations
from bypassing the CSRF-aware fetch layer and by preserving workspace scoping
for browser-persisted Assistant notes.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import urlparse

from benchtop import help_content

APP_JS = Path(__file__).parents[1] / "benchtop" / "web" / "app.js"
AUTH_BOOTSTRAP_JS = Path(__file__).parents[1] / "benchtop" / "web" / "auth-bootstrap.js"
INDEX_HTML = Path(__file__).parents[1] / "benchtop" / "web" / "index.html"
STYLES_CSS = Path(__file__).parents[1] / "benchtop" / "web" / "styles.css"


class FrontendSecurityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS.read_text(encoding="utf-8")
        cls.auth_source = AUTH_BOOTSTRAP_JS.read_text(encoding="utf-8")
        cls.index_source = INDEX_HTML.read_text(encoding="utf-8")
        cls.styles_source = STYLES_CSS.read_text(encoding="utf-8")

    def test_visible_branding_uses_omicsang_without_renaming_protocols(self) -> None:
        tagline = "All your omics. One workspace."
        self.assertIn(
            "<title>OmicsANG · All your omics. One workspace.</title>",
            self.index_source,
        )
        self.assertIn("<strong>OmicsANG</strong>", self.index_source)
        self.assertIn(tagline, self.index_source)
        self.assertIn(".brand-tagline", self.styles_source)
        self.assertNotIn(">Bench" + "top<", self.index_source)
        self.assertIn("`OmicsANG failed to start:", self.source)
        self.assertEqual(self.source.count("Bench" + "top"), 1)
        self.assertIn("headers.set('X-Bench" + "top-CSRF', csrfToken);", self.source)
        self.assertIn("j.benchtop_owned", self.source)

    def test_bootstrap_fragment_is_cleared_before_vendor_scripts(self) -> None:
        auth_position = self.index_source.index('/auth-bootstrap.js?v=20260716-restore')
        xterm_position = self.index_source.index(
            '<script src="/vendor/xterm/5.3.0/xterm.js">'
        )
        app_position = self.index_source.index('/app.js?v=20260716-restore')
        self.assertLess(auth_position, xterm_position)
        self.assertLess(auth_position, app_position)
        self.assertIn("window.history.replaceState(", self.auth_source)
        self.assertIn("window.location.hash.slice(1)", self.auth_source)
        self.assertIn("delete window.__benchtopTakeBootstrapToken", self.auth_source)
        self.assertNotIn("localStorage", self.auth_source)
        self.assertNotIn("sessionStorage", self.auth_source)
        self.assertIn("window.__benchtopTakeBootstrapToken", self.source)

    def test_api_file_surfaces_use_authenticated_blob_fetches(self) -> None:
        self.assertIn("async function authenticatedObjectUrl(raw)", self.source)
        self.assertIn("const response = await apiFetch(target);", self.source)
        self.assertIn("loadAuthenticatedImage(image, fileUrl(im));", self.source)
        self.assertIn("openAuthenticatedFile(fileUrl(rep), rep.name)", self.source)
        self.assertNotRegex(
            self.source,
            re.compile(r"el\(['\"]img['\"],\s*\{[^}]*\bsrc:\s*fileUrl\("),
        )
        self.assertNotIn("openSameOrigin(`/api/", self.source)
        self.assertNotIn("openSameOrigin(fileUrl(", self.source)

    def test_saved_code_notes_are_workspace_scoped_and_clearable(self) -> None:
        self.assertIn("const CODE_NOTES_PREFIX = 'omicsang.codeNotes.v2';", self.source)
        self.assertIn(
            "const LEGACY_WORKSPACE_CODE_NOTES_PREFIX = 'benchtop.codeNotes.v2';",
            self.source,
        )
        self.assertIn("function workspaceCodeNotesPrefix()", self.source)
        self.assertIn("state.meta.workspace_id", self.source)
        self.assertNotIn(
            "`${CODE_NOTES_PREFIX}:${encodeURIComponent(root)}:`", self.source
        )
        self.assertIn("function clearWorkspaceCodeNotes()", self.source)
        self.assertIn("LEGACY_CODE_NOTES_PREFIX", self.source)
        self.assertIn("legacyWorkspaceCodeNoteStorageKey", self.source)
        self.assertIn("localStorage.setItem(key, legacy);", self.source)
        self.assertIn(
            "for (const storageKey of [key, legacyKey].filter(Boolean))", self.source
        )
        load_function = re.search(
            r"function loadCodeNote\(pipeline, path\) \{(.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(load_function)
        self.assertNotIn("LEGACY_CODE_NOTES_PREFIX", load_function.group(1))
        self.assertIn("Clear saved notes", self.source)

    def test_preferences_migrate_to_omicsang_and_remain_rollback_safe(self) -> None:
        self.assertIn("const UI_PREFS_KEY = 'omicsang.uiPrefs.v1';", self.source)
        self.assertIn("const LEGACY_UI_PREFS_KEY = 'benchtop.uiPrefs.v1';", self.source)
        self.assertIn(
            "const legacy = current === null ? storageJsonObject(LEGACY_UI_PREFS_KEY) : null;",
            self.source,
        )
        self.assertIn("writeStorageObject([UI_PREFS_KEY], preferences);", self.source)
        self.assertIn(
            "writeStorageObject([UI_PREFS_KEY, LEGACY_UI_PREFS_KEY], state.uiPrefs);",
            self.source,
        )

    def test_resizable_panel_visibility_is_persisted_accessible_and_responsive(
        self,
    ) -> None:
        for key in (
            "sidebarOpen",
            "monitorOpen",
            "codeSidebarOpen",
            "codeAssistantOpen",
            "terminalOpen",
        ):
            self.assertIn(f"{key}: true", self.source)
            self.assertIn(f"{key}: p.{key} !== false", self.source)
        self.assertIn('id="sidebar-toggle"', self.index_source)
        self.assertIn('aria-controls="sidebar"', self.index_source)
        self.assertIn('id="monitor-toggle"', self.index_source)
        self.assertIn('aria-controls="monitor-panel"', self.index_source)
        self.assertIn('id="sidebar-collapse"', self.index_source)
        self.assertIn('class="ghost sm icon-only panel-collapse"', self.index_source)
        self.assertIn('id="panel-restore-dock"', self.index_source)
        self.assertIn('role="group" aria-label="Restore hidden panels"', self.index_source)
        for restore_id, key, label in (
            ("sidebar-restore", "sidebarOpen", "pipelines"),
            ("monitor-restore", "monitorOpen", "run monitor"),
            ("code-sidebar-restore", "codeSidebarOpen", "file tree"),
            ("code-assistant-restore", "codeAssistantOpen", "assistant"),
            ("terminal-restore", "terminalOpen", "terminal"),
        ):
            self.assertIn(f'id="{restore_id}"', self.index_source)
            self.assertIn(f'data-panel-restore="{key}"', self.index_source)
            self.assertIn(f'data-panel-label="{label}"', self.index_source)
        self.assertGreater(
            self.index_source.index('id="panel-restore-dock"'),
            self.index_source.index('</aside>', self.index_source.index('id="sidebar"')),
        )
        self.assertGreater(
            self.index_source.index('id="panel-restore-dock"'),
            self.index_source.index(
                '</aside>', self.index_source.index('id="monitor-panel"')
            ),
        )
        self.assertIn("sidebar.classList.toggle('hidden', !p.sidebarOpen)", self.source)
        self.assertIn("monitorPanel.classList.toggle('hidden', !open)", self.source)
        self.assertIn("'data-panel-pref': 'codeSidebarOpen'", self.source)
        self.assertIn("'data-panel-pref': 'codeAssistantOpen'", self.source)
        self.assertIn("'data-panel-pref': 'terminalOpen'", self.source)
        self.assertIn("stage.classList.toggle('hidden', !p.terminalOpen)", self.source)
        self.assertIn("button.setAttribute('aria-label'", self.source)
        self.assertIn("function syncPanelRestoreDock(preferences = state.uiPrefs)", self.source)
        self.assertIn("syncPanelRestoreDock(p);", self.source)
        self.assertIn("function focusRestoredPanel(key)", self.source)
        self.assertIn("requestAnimationFrame(() => focusRestoredPanel(key))", self.source)
        self.assertIn("requestAnimationFrame(() => $('#sidebar-restore').focus())", self.source)
        self.assertIn("requestAnimationFrame(() => $('#monitor-restore').focus())", self.source)
        self.assertIn("requestAnimationFrame(() => $('#code-sidebar-restore').focus())", self.source)
        self.assertIn("requestAnimationFrame(() => $('#code-assistant-restore').focus())", self.source)
        self.assertIn("fitTerminal(state.focusedSession)", self.source)
        self.assertIn("document.body.classList.toggle('panel-restore-visible'", self.source)
        self.assertIn('class="monitor-panel" tabindex="-1"', self.index_source)
        self.assertIn("'Collapse folders'", self.source)
        self.assertNotIn("'Collapse all'", self.source)
        self.assertIn("class: 'code-panel-head'", self.source)
        self.assertIn("class: 'assistant-head-actions'", self.source)
        self.assertIn(".terminal-stage", self.styles_source)
        self.assertIn(".panel-restore-dock", self.styles_source)
        self.assertIn(".panel-restore-button:focus-visible", self.styles_source)
        self.assertIn(".panel-restore-symbol-left::before", self.styles_source)
        self.assertIn(".panel-restore-symbol-right::before", self.styles_source)
        self.assertIn(".panel-restore-symbol-bottom::before", self.styles_source)
        self.assertIn("body.panel-restore-visible #view", self.styles_source)
        self.assertIn("body.panel-restore-visible #view .term-mount", self.styles_source)
        self.assertIn("min-height: 44px", self.styles_source)
        self.assertIn("body.monitor-open .panel-restore-dock", self.styles_source)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.styles_source)
        self.assertIn("@media (forced-colors: active)", self.styles_source)
        self.assertIn(
            ".code-workbench.code-sidebar-collapsed > .code-resizer",
            self.styles_source,
        )
        self.assertIn(
            ".code-workbench.code-assistant-collapsed > .assistant-resizer",
            self.styles_source,
        )
        self.assertIn(
            ".code-workbench.code-sidebar-collapsed.code-assistant-collapsed",
            self.styles_source,
        )

    def test_editor_assistance_is_local_toggleable_and_persisted(self) -> None:
        for key in ("codeInlineCompletions", "codeHoverHelp"):
            self.assertIn(f"{key}: true", self.source)
            self.assertIn(f"{key}: p.{key} !== false", self.source)
        self.assertIn("EditorCore.inlineCompletionAt(editor.value", self.source)
        self.assertIn("EditorCore.explainLine(editorLines[lineIndex]", self.source)
        self.assertIn("Local explanation · no upload", self.source)
        self.assertIn(
            "Inline completions and hover explanations stay local", self.source
        )
        self.assertNotIn("/api/code/completion", self.source)

    def test_submission_intents_reuse_and_mirror_legacy_idempotency_keys(self) -> None:
        self.assertIn(
            "const SUBMISSION_INTENTS_KEY = 'omicsang.submissionIntents.v1';",
            self.source,
        )
        self.assertIn(
            "const LEGACY_SUBMISSION_INTENTS_KEY = 'benchtop.submissionIntents.v1';",
            self.source,
        )
        self.assertIn("function mergeSubmissionIntents(current, legacy)", self.source)
        self.assertIn(
            "submissionIntentTimestamp(currentIntent) < submissionIntentTimestamp(legacyIntent)",
            self.source,
        )
        self.assertIn(
            "writeStorageObject([SUBMISSION_INTENTS_KEY, LEGACY_SUBMISSION_INTENTS_KEY], bounded);",
            self.source,
        )
        self.assertIn("const bounded = persistSubmissionIntents(intents);", self.source)
        self.assertIn("persistSubmissionIntents(intents);", self.source)

    def test_agent_risk_is_compact_but_launch_acknowledgement_stays_explicit(
        self,
    ) -> None:
        self.assertIn("function agentDisclosure(context = '')", self.source)
        self.assertIn("Agent access & privacy · approval required", self.source)
        self.assertIn("does not launch a tool or supply context until", self.source)
        self.assertIn("OmicsANG does not add an OS sandbox", self.source)
        self.assertIn("provider-specific environment allowlist", self.source)
        self.assertIn("Environment filtering does not limit", self.source)
        self.assertIn("Review before launch", self.source)
        self.assertIn("acknowledge_external_agent: true", self.source)
        self.assertNotIn("class: 'agent-warning'", self.source)
        self.assertNotIn("unsandboxed", self.source.lower())
        self.assertIn(".agent-disclosure", self.styles_source)
        self.assertIn("Worktrees separate branches and file changes", self.source)

    def test_navigation_history_reads_legacy_state_and_writes_omicsang_state(
        self,
    ) -> None:
        self.assertIn(
            "const NAVIGATION_STATE_KEY = 'omicsang.navigation.v1';", self.source
        )
        self.assertIn(
            "const LEGACY_NAVIGATION_STATE_KEY = 'benchtopNavigationV1';",
            self.source,
        )
        self.assertIn("function navigationFromHistoryState(historyState)", self.source)
        self.assertIn(
            "const navigation = navigationFromHistoryState(e.state);", self.source
        )

    def test_private_navigation_and_workspace_inputs_stay_out_of_urls(self) -> None:
        snapshot = re.search(
            r"function navigationSnapshot\(\) \{(.*?)\n\}", self.source, re.DOTALL
        )
        self.assertIsNotNone(snapshot)
        snapshot_source = snapshot.group(1)
        self.assertIn("pipelineIndex", snapshot_source)
        self.assertNotRegex(snapshot_source, r"\bpipeline\s*:")
        self.assertIn(
            "api.post(`/api/pipelines/${encodeURIComponent(d.name)}/code/read`, { path })",
            self.source,
        )
        self.assertIn("/browse/search`, {", self.source)
        self.assertIn("/directories/search`, {", self.source)
        self.assertNotIn("/code?path=", self.source)
        self.assertNotIn("/results/directories?", self.source)

    def test_official_documentation_allowlist_matches_curated_catalog(self) -> None:
        match = re.search(
            r"const OFFICIAL_DOC_HOSTS = new Set\(\[(.*?)\]\);",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        frontend_hosts = set(re.findall(r"'([^']+)'", match.group(1)))
        catalog_hosts = {
            urlparse(item["docs_url"]).hostname
            for item in help_content.PACKAGE_CATALOG.values()
        }
        catalog_hosts.update(
            urlparse(item[field]).hostname
            for item in help_content.COMMAND_HELP_CATALOG.values()
            for field in ("docs_url", "source_url")
        )
        self.assertEqual(frontend_hosts, catalog_hosts)
        self.assertIn("if (!OFFICIAL_DOC_HOSTS.has(host)) return null;", self.source)

    def test_contextual_command_help_is_static_safe_and_cursor_aware(self) -> None:
        self.assertIn(
            "const CONTEXTUAL_COMMANDS = Object.freeze(['fastqc']);", self.source
        )
        self.assertIn("EditorCore.commandContextAt(", self.source)
        self.assertIn("/api/code/command-help/", self.source)
        self.assertIn("Tool & package help", self.source)
        self.assertIn("All ${options.length}", self.source)
        self.assertIn("referrerpolicy: 'no-referrer'", self.source)
        self.assertIn("OmicsANG did not run the repository command", self.source)
        self.assertIn(".command-help-option.matched", self.styles_source)
        self.assertIn(".command-help-options", self.styles_source)
        self.assertNotIn("fastqc --help`,", self.source)


if __name__ == "__main__":
    unittest.main()
