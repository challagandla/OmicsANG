#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Smoke-check a running OmicsANG instance without browser dependencies."""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

COOKIE_JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIE_JAR))
CSRF_TOKEN = ""


def _origin(base: str) -> str:
    parsed = urllib.parse.urlsplit(base)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def fetch(base: str, path: str) -> tuple[int, bytes]:
    url = base.rstrip("/") + path
    headers = {"X-Benchtop-CSRF": CSRF_TOKEN} if CSRF_TOKEN else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with OPENER.open(request, timeout=10) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post_json(base: str, path: str, payload: dict) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json", "Origin": _origin(base)}
    if CSRF_TOKEN:
        headers["X-Benchtop-CSRF"] = CSRF_TOKEN
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with OPENER.open(request, timeout=30) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def expect_json(base: str, path: str, keys: set[str]) -> dict:
    status, body = fetch(base, path)
    if status != 200:
        raise SystemExit(f"{path}: HTTP {status}")
    data = json.loads(body.decode("utf-8"))
    missing = keys - set(data)
    if missing:
        raise SystemExit(f"{path}: missing keys {sorted(missing)}")
    print(f"ok {path}")
    return data


def expect_post_json(base: str, path: str, payload: dict, keys: set[str]) -> dict:
    status, body = post_json(base, path, payload)
    if status != 200:
        raise SystemExit(f"{path}: HTTP {status}")
    data = json.loads(body.decode("utf-8"))
    missing = keys - set(data)
    if missing:
        raise SystemExit(f"{path}: missing keys {sorted(missing)}")
    print(f"ok {path}")
    return data


def expect_post_bytes(base: str, path: str, payload: dict) -> bytes:
    status, body = post_json(base, path, payload)
    if status != 200:
        raise SystemExit(f"{path}: HTTP {status}")
    if not body:
        raise SystemExit(f"{path}: empty response")
    print(f"ok {path}")
    return body


def expect_asset(base: str, path: str, needle: bytes) -> None:
    status, body = fetch(base, path)
    if status != 200:
        raise SystemExit(f"{path}: HTTP {status}")
    if needle.lower() not in body.lower():
        raise SystemExit(f"{path}: expected asset marker not found")
    print(f"ok {path}")


def expect_slurm_status(base: str) -> None:
    path = "/api/slurm/status"
    status, body = fetch(base, path)
    if status != 200:
        raise SystemExit(f"{path}: HTTP {status}")
    data = json.loads(body.decode("utf-8"))
    missing = {"available", "capabilities", "tools", "partitions", "jobs"} - set(data)
    if missing:
        raise SystemExit(f"{path}: missing keys {sorted(missing)}")
    capability_keys = {"submit", "monitor", "accounting", "cancel", "partitions"}
    missing_capabilities = capability_keys - set(data.get("capabilities") or {})
    if missing_capabilities:
        raise SystemExit(f"{path}: missing capabilities {sorted(missing_capabilities)}")
    print(f"ok {path}")


def expect_run_plan(base: str, pipeline: dict) -> None:
    name = urllib.parse.quote(str(pipeline["name"]), safe="")
    configs = list(pipeline.get("configs") or [])
    status, body = post_json(
        base,
        f"/api/pipelines/{name}/preview",
        {
            "configfile": configs[0] if configs else "",
            "cores": 1,
            "dryrun": True,
            "use_conda": False,
        },
    )
    if status != 200:
        raise SystemExit(
            f"/api/pipelines/{name}/preview: HTTP {status}: {body[:500]!r}"
        )
    data = json.loads(body.decode("utf-8"))
    missing = {
        "command",
        "plan",
        "plan_digest",
        "plan_digest_scope",
        "plan_projection_digest",
        "plan_redacted",
        "plan_schema_version",
        "study",
    } - set(data)
    if missing:
        raise SystemExit(
            f"/api/pipelines/{name}/preview: missing keys {sorted(missing)}"
        )
    if data["plan_schema_version"] != 1 or not str(data["plan_digest"]).startswith(
        "sha256:"
    ):
        raise SystemExit(f"/api/pipelines/{name}/preview: invalid RunPlan identity")
    if data["plan"].get("schema_version") != data["plan_schema_version"]:
        raise SystemExit(f"/api/pipelines/{name}/preview: RunPlan schema mismatch")
    if (
        data["plan_digest_scope"] != "private-canonical-plan"
        or not data["plan_redacted"]
    ):
        raise SystemExit(
            f"/api/pipelines/{name}/preview: RunPlan redaction contract missing"
        )
    projection = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                data["plan"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    if projection != data["plan_projection_digest"]:
        raise SystemExit(f"/api/pipelines/{name}/preview: projection digest mismatch")
    workflow_status = data["plan"].get("workflow", {}).get("resolution_status")
    if workflow_status not in {"bounded-static-complete", "bounded-static-partial"}:
        raise SystemExit(f"/api/pipelines/{name}/preview: workflow manifest missing")
    resolution = data["plan"].get("resolution") or {}
    if not {"complete", "launch_safe", "issues"}.issubset(resolution):
        raise SystemExit(f"/api/pipelines/{name}/preview: resolution contract missing")
    print(f"ok /api/pipelines/{name}/preview ({data['plan_digest'][:19]}...)")


def main(argv: list[str]) -> int:
    global CSRF_TOKEN
    base = argv[1] if len(argv) > 1 else "http://127.0.0.1:8787"
    bootstrap_token = os.environ.pop("OMICSANG_BOOTSTRAP_TOKEN", "")
    legacy_bootstrap_token = os.environ.pop("BENCHTOP_BOOTSTRAP_TOKEN", "")
    if (
        bootstrap_token
        and legacy_bootstrap_token
        and not secrets.compare_digest(bootstrap_token, legacy_bootstrap_token)
    ):
        bootstrap_token = ""
        legacy_bootstrap_token = ""
        raise SystemExit(
            "OMICSANG_BOOTSTRAP_TOKEN conflicts with the legacy bootstrap-token "
            "environment variable"
        )
    bootstrap_token = bootstrap_token or legacy_bootstrap_token
    legacy_bootstrap_token = ""
    if not bootstrap_token:
        raise SystemExit(
            "OMICSANG_BOOTSTRAP_TOKEN must contain the one-time token captured "
            "privately from the launch URL; the legacy environment-variable "
            "name remains a fallback"
        )
    status, body = post_json(base, "/api/auth/bootstrap", {"token": bootstrap_token})
    bootstrap_token = ""
    if status != 200:
        raise SystemExit(f"/api/auth/bootstrap: HTTP {status}")
    auth = json.loads(body.decode("utf-8"))
    CSRF_TOKEN = str(auth.get("csrf") or "")
    if not CSRF_TOKEN:
        raise SystemExit("/api/auth/bootstrap: missing CSRF value")
    print("ok /api/auth/bootstrap")
    meta = expect_json(
        base,
        "/api/meta",
        {"root", "version", "api", "tools", "run_queue", "durability"},
    )
    api_capabilities = meta.get("api") or {}
    if api_capabilities.get("results_directories") != 2:
        raise SystemExit("/api/meta: Results directory API capability missing")
    for capability in (
        "repository_browse",
        "contextual_help",
        "command_parameter_help",
        "private_code_read",
    ):
        if api_capabilities.get(capability) != 1:
            raise SystemExit(f"/api/meta: {capability} API capability missing")
    help_catalog = expect_json(
        base,
        "/api/help",
        {"tabs", "shortcuts", "package_help", "command_help"},
    )
    if len(help_catalog.get("tabs") or []) != 16:
        raise SystemExit("/api/help: expected contextual help for all 16 views")
    command_meta = help_catalog.get("command_help") or {}
    if "fastqc" not in (command_meta.get("supported_commands") or []):
        raise SystemExit("/api/help: FastQC command help metadata missing")
    fastqc_help = expect_json(
        base,
        "/api/code/command-help/fastqc",
        {
            "command",
            "name",
            "catalog_version",
            "version_scope",
            "summary",
            "synopsis",
            "docs_url",
            "source_url",
            "arguments",
            "options",
        },
    )
    if len(fastqc_help.get("options") or []) != 24:
        raise SystemExit("/api/code/command-help/fastqc: expected 24 option records")
    fastqc_flags = {
        flag
        for option in fastqc_help.get("options") or []
        for flag in option.get("flags") or []
    }
    if not {"--threads", "--memory", "--outdir"}.issubset(fastqc_flags):
        raise SystemExit("/api/code/command-help/fastqc: core resource flags missing")
    expect_json(
        base,
        "/api/run_queue",
        {"max_cores", "active_cores", "running", "queued", "lost", "durability"},
    )
    status, body = fetch(base, "/api/jobs?limit=5")
    if status != 200 or not isinstance(json.loads(body.decode("utf-8")), list):
        raise SystemExit(f"/api/jobs: invalid durable job response (HTTP {status})")
    print("ok /api/jobs")
    expect_slurm_status(base)
    expect_json(
        base,
        "/api/health?diagnostics=false",
        {"generated", "pipelines", "run_queue", "slurm"},
    )
    expect_asset(base, "/vendor/xterm/5.3.0/xterm.css", b"xterm")
    expect_asset(base, "/vendor/xterm/5.3.0/xterm.js", b"Terminal")
    expect_asset(base, "/vendor/xterm-addon-fit/0.8.0/xterm-addon-fit.js", b"FitAddon")
    expect_asset(base, "/", b'id="tabs" aria-label="Pipeline workspace"')
    expect_asset(base, "/", b'id="sidebar-collapse"')
    expect_asset(base, "/", b"20260716-restore")
    expect_asset(base, "/", b'id="panel-restore-dock"')
    expect_asset(base, "/", b'data-panel-restore="codeAssistantOpen"')
    expect_asset(base, "/auth-bootstrap.js", b"__benchtopTakeBootstrapToken")
    expect_asset(base, "/editor-core.js", b"commandContextAt")
    expect_asset(base, "/editor-core.js", b"inlineCompletionAt")
    expect_asset(base, "/editor-core.js", b"explainLine")
    expect_asset(base, "/app.js", b"function persistSubmissionIntents")
    expect_asset(base, "/app.js", b"function toolMenu")
    expect_asset(base, "/app.js", b"function slurmCapabilities")
    expect_asset(base, "/app.js", b"function resultDirectoryManager")
    expect_asset(base, "/app.js", b"function viewBrowse")
    expect_asset(base, "/app.js", b"Tool & package help")
    expect_asset(base, "/app.js", b"OmicsANG did not run the repository command")
    expect_asset(base, "/app.js", b"code-editor-statusbar")
    expect_asset(base, "/app.js", b"EditorCore.applyEnterEdit")
    expect_asset(base, "/app.js", b"code-inline-completion")
    expect_asset(base, "/app.js", b"scheduleLineHoverHelp")
    expect_asset(base, "/app.js", b"applyPanelVisibility")
    expect_asset(base, "/app.js", b"syncPanelRestoreDock")
    expect_asset(base, "/app.js", b"focusRestoredPanel")
    expect_asset(base, "/app.js", b"terminalOpen")
    expect_asset(base, "/app.js", b"Collapse folders")
    expect_asset(base, "/app.js", b"code-panel-head")
    expect_asset(base, "/app.js", b"function agentDisclosure")
    expect_asset(base, "/app.js", b"function renderHelpDrawer")
    expect_asset(base, "/app.js", b"Manage directories")
    expect_asset(base, "/styles.css", b"study-layout")
    expect_asset(base, "/styles.css", b".tool-menu-popover")
    expect_asset(base, "/styles.css", b".result-directory-panel")
    expect_asset(base, "/styles.css", b".result-directory-panel.collapsed")
    expect_asset(base, "/styles.css", b".code-indent-guide.active")
    expect_asset(base, "/styles.css", b".code-line-hover")
    expect_asset(base, "/styles.css", b".code-workbench.code-sidebar-collapsed")
    expect_asset(base, "/styles.css", b".terminal-stage")
    expect_asset(base, "/styles.css", b".panel-collapse")
    expect_asset(base, "/styles.css", b".panel-restore-dock")
    expect_asset(base, "/styles.css", b".panel-restore-button:focus-visible")
    expect_asset(base, "/styles.css", b".command-help-option.matched")
    expect_asset(base, "/styles.css", b".agent-disclosure")
    status, body = fetch(base, "/api/pipelines")
    pipelines = json.loads(body.decode("utf-8")) if status == 200 else []
    if pipelines:
        name = urllib.parse.quote(str(pipelines[0]["name"]), safe="")
        expect_json(
            base,
            f"/api/pipelines/{name}/results",
            {
                "roots",
                "sources",
                "attachments",
                "reports",
                "images",
                "tables",
                "notebooks",
                "counts",
            },
        )
        expect_post_json(
            base,
            f"/api/pipelines/{name}/results/directories/search",
            {"root_id": "", "query": "", "limit": 3},
            {"allowed_roots", "candidates", "bounds"},
        )
        browse = expect_post_json(
            base,
            f"/api/pipelines/{name}/browse/search",
            {"path": "", "query": "", "scope": "all", "limit": 3},
            {"path", "breadcrumbs", "results", "count", "truncated"},
        )
        if not isinstance(browse["results"], list):
            raise SystemExit(f"/api/pipelines/{name}/browse/search: invalid results")
        pipeline = pipelines[0]
        code_path = str(
            pipeline.get("snakefile")
            or pipeline.get("readme")
            or next(iter(pipeline.get("configs") or []), "")
        )
        if code_path:
            expect_post_json(
                base,
                f"/api/pipelines/{name}/code/read",
                {"path": code_path},
                {"path", "name", "size", "mtime", "language", "content", "revision"},
            )
            expect_post_json(
                base,
                f"/api/pipelines/{name}/code/help",
                {"path": code_path},
                {"path", "language", "packages", "notes"},
            )
            expect_post_bytes(
                base,
                f"/api/pipelines/{name}/browse/open",
                {"path": code_path},
            )
        expect_json(
            base,
            f"/api/pipelines/{name}/study",
            {"gate", "summary", "issues", "candidates", "fingerprint"},
        )
        expect_json(base, f"/api/pipelines/{name}/capsules", {"capsules"})
        snakemake = next(
            (pipeline for pipeline in pipelines if pipeline.get("kind") == "snakemake"),
            None,
        )
        if snakemake:
            expect_run_plan(base, snakemake)
    print(f"OmicsANG smoke check passed: {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
