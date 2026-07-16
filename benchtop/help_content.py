# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Local, original help content and dependency hints for the browser UI.

This module deliberately performs no network requests.  Documentation URLs are
curated pointers to official project documentation; source text and detected names
remain on the local machine unless a user chooses to open an external link.
"""

from __future__ import annotations

import ast
import copy
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

TAB_HELP = (
    {
        "id": "overview",
        "label": "Monitor",
        "group": "workspace",
        "summary": "See pipeline readiness, recent activity, diagnostics, and the next useful action at a glance.",
        "how_to": (
            "Start here after selecting a pipeline.",
            "Resolve blocking diagnostics before launching a real run.",
            "Use the suggested action to move to the relevant workspace.",
        ),
        "tips": (
            "Refresh after external file changes to avoid acting on stale status.",
        ),
        "related_tabs": ("study", "run", "results"),
    },
    {
        "id": "study",
        "label": "Study",
        "group": "workspace",
        "summary": "Audit sample sheets, group balance, pairing, and design estimability before analysis.",
        "how_to": (
            "Choose the table that represents the biological study.",
            "Map sample, condition, batch, subject, and input-file columns.",
            "Bind only the reviewed authoritative table to a real run.",
        ),
        "tips": (
            "Treat confounding, duplicate samples, and missing inputs as design problems, not cosmetic warnings.",
        ),
        "related_tabs": ("config", "run"),
    },
    {
        "id": "run",
        "label": "Run",
        "group": "workspace",
        "summary": "Preview and launch bounded local or Slurm workflow runs with an explicit configuration.",
        "how_to": (
            "Select the config, target, cores, and execution mode.",
            "Run a dry-run first and review the exact command preview.",
            "Launch the real run only after study and path checks pass.",
        ),
        "tips": (
            "Extra arguments cannot replace controls that already have dedicated fields.",
        ),
        "related_tabs": ("study", "resources", "terminal"),
    },
    {
        "id": "results",
        "label": "Results",
        "group": "workspace",
        "summary": "Browse authorized outputs, quality-control files, figures, reports, tables, and freshness signals.",
        "how_to": (
            "Filter by result type or search by name.",
            "Attach an existing output directory when it is outside the discovered defaults.",
            "Check freshness before comparing a result with the latest run.",
        ),
        "tips": (
            "An attachment references an existing directory; removing it does not delete files.",
        ),
        "related_tabs": ("capsules", "terminal"),
    },
    {
        "id": "capsules",
        "label": "Capsules",
        "group": "workspace",
        "summary": "Review bounded provenance records that connect run intent, inputs, study context, and outputs.",
        "how_to": (
            "Select a run capsule to inspect its recorded context.",
            "Compare capsules when a result changes between runs.",
            "Use the recorded source state to reproduce or explain an analysis.",
        ),
        "tips": (
            "A capsule is evidence about a run, not a replacement for archiving primary data.",
        ),
        "related_tabs": ("run", "results", "github"),
    },
    {
        "id": "browse",
        "label": "Browse",
        "group": "workspace",
        "summary": "Navigate and search authorized pipeline directories without exposing local paths in request URLs.",
        "how_to": (
            "Open a directory to list its immediate children.",
            "Search beneath the current directory when you know part of a name or path.",
            "Open editable text in Code or explicitly download another bounded regular file.",
        ),
        "tips": (
            "Protected, hidden, symlinked, special, and oversized files are not opened from this view.",
        ),
        "related_tabs": ("code", "results"),
    },
    {
        "id": "code",
        "label": "Code",
        "group": "workspace",
        "summary": "Browse, search, edit, and diagnose supported workflow, configuration, and documentation files.",
        "how_to": (
            "Search the project tree and open files in tabs.",
            "Use line numbers, active indentation guides, and the status bar to stay oriented in nested workflow code.",
            "Use local inline completions as you type, and hover a line or press Alt+F1 for a concise explanation without sending code outside OmicsANG.",
            "Open Tool & package help beside the editor for caret-aware command parameters, local-use hints, and curated documentation.",
            "Save deliberately and review diagnostics before running the workflow.",
        ),
        "tips": (
            "Tab accepts a visible inline completion; otherwise Tab and Shift+Tab indent or outdent.",
            "Ctrl/Cmd+Space requests a local completion, and Ctrl/Cmd+/ toggles supported line comments.",
            "Alt+F1 explains the current line for keyboard and screen-reader use.",
            "Place the caret on a supported command or option and press F1 for the matching version-labelled guide.",
            "Press Escape, then Tab or Shift+Tab, to move keyboard focus out of the editor.",
            "Generated outputs, credentials, hidden state, and oversized or non-text files are not editable here.",
        ),
        "related_tabs": ("config", "dag", "run"),
    },
    {
        "id": "agents",
        "label": "Agents",
        "group": "workspace",
        "summary": "Prepare and explicitly approve a reviewed external-agent task for the selected pipeline.",
        "how_to": (
            "Choose the provider and describe one concrete repository task.",
            "Review the exact context and external-provider acknowledgement.",
            "Follow progress in the attached terminal session.",
        ),
        "tips": (
            "Nothing launches or receives context until you approve the confirmation.",
            "Git worktrees separate branches and file changes but do not restrict OS permissions.",
            "Do not include secrets, private sample data, or credentials in an external-agent prompt.",
        ),
        "related_tabs": ("code", "terminal"),
    },
    {
        "id": "dag",
        "label": "DAG",
        "group": "tool",
        "summary": "Inspect the workflow graph for a selected config and target before execution.",
        "how_to": (
            "Choose the same config and target you intend to run.",
            "Generate the graph and inspect unexpected branches or missing rules.",
            "Return to Run once the planned workflow matches the study intent.",
        ),
        "tips": (
            "The graph reflects the selected parameters and may change with a different config or target.",
        ),
        "related_tabs": ("config", "run"),
    },
    {
        "id": "config",
        "label": "Config",
        "group": "tool",
        "summary": "Edit discovered pipeline parameters with type, path, and validation guidance.",
        "how_to": (
            "Select a discovered configuration file.",
            "Review inferred field types and path suggestions before editing.",
            "Save, then re-run Study or DAG checks when analysis inputs change.",
        ),
        "tips": (
            "Keep environment definitions and analysis parameters versioned with the workflow.",
        ),
        "related_tabs": ("study", "dag", "code"),
    },
    {
        "id": "sra_geo",
        "label": "SRA/GEO",
        "group": "tool",
        "summary": "Find public NCBI studies and prepare tracked downloads into a chosen pipeline directory.",
        "how_to": (
            "Search by accession or terms and review the returned study metadata.",
            "Paste or load run accessions, then preview the normalized destination.",
            "Start the tracked download and monitor it in Terminal.",
        ),
        "tips": (
            "Verify consent, controlled-access status, and downstream data-use conditions before analysis.",
        ),
        "related_tabs": ("terminal", "study"),
    },
    {
        "id": "resources",
        "label": "Resources",
        "group": "tool",
        "summary": "Review available local and Slurm compute capabilities before selecting run resources.",
        "how_to": (
            "Compare detected capacity with workflow thread and memory expectations.",
            "Use conservative settings for the first dry-run.",
            "Check scheduler availability before choosing Slurm execution.",
        ),
        "tips": (
            "Requested cores should reflect both host capacity and per-rule workflow limits.",
        ),
        "related_tabs": ("run", "terminal"),
    },
    {
        "id": "github",
        "label": "GitHub",
        "group": "tool",
        "summary": "Inspect local repository status and review available repository context without hidden publication steps.",
        "how_to": (
            "Review branch, remotes, and dirty files before a run or handoff.",
            "Use the local status to identify unrecorded workflow changes.",
            "Perform unavailable outbound operations through a separately reviewed command-line workflow.",
        ),
        "tips": (
            "Never commit credentials, private datasets, generated secrets, or provider tokens.",
        ),
        "related_tabs": ("code", "capsules"),
    },
    {
        "id": "terminal",
        "label": "Terminal",
        "group": "tool",
        "summary": "Attach to live, queued, or completed sessions without losing them when switching tabs.",
        "how_to": (
            "Select a session from the monitor or session list.",
            "Follow output and interact only when the underlying command expects input.",
            "Use explicit cancellation controls rather than closing the browser.",
        ),
        "tips": (
            "Terminal output can contain sample names or paths; review it before sharing screenshots or logs.",
        ),
        "related_tabs": ("run", "agents", "resources"),
    },
    {
        "id": "fleet-health",
        "label": "Fleet Health",
        "group": "fleet",
        "summary": "Compare readiness, diagnostics, and activity across registered pipelines.",
        "how_to": (
            "Scan for blocked or stale pipelines.",
            "Open one pipeline to investigate its highest-severity issue.",
            "Return to Fleet Health to confirm the issue is resolved.",
        ),
        "tips": (
            "Fleet summaries are navigation aids; confirm important findings in the pipeline workspace.",
        ),
        "related_tabs": ("overview", "fleet-task"),
    },
    {
        "id": "fleet-task",
        "label": "Fleet Task",
        "group": "fleet",
        "summary": "Prepare one reviewed agent task across selected pipelines with explicit launch confirmation.",
        "how_to": (
            "Select only the pipelines required for the task.",
            "Review worktree and external-provider settings before launch.",
            "Inspect each target result independently instead of assuming fleet-wide success.",
        ),
        "tips": (
            "A fleet task multiplies exposure and impact; exclude repositories or data that the provider should not receive.",
        ),
        "related_tabs": ("agents", "terminal", "fleet-health"),
    },
)


SHORTCUT_HELP = (
    {"keys": ("Ctrl", "K"), "action": "Open the command palette", "context": "global"},
    {
        "keys": ("Meta", "K"),
        "action": "Open the command palette when the browser maps a Meta shortcut",
        "context": "global",
    },
    {
        "keys": ("Escape",),
        "action": "Close the active palette or popover",
        "context": "global",
    },
    {
        "keys": ("Ctrl", "PageUp"),
        "action": "Open the previous pipeline workspace tab",
        "context": "tab navigation",
    },
    {
        "keys": ("Ctrl", "PageDown"),
        "action": "Open the next pipeline workspace tab",
        "context": "tab navigation",
    },
    {
        "keys": ("Left", "Right"),
        "action": "Move between workspace tabs",
        "context": "tab navigation",
    },
    {
        "keys": ("Home", "End"),
        "action": "Move to the first or last tab or menu item",
        "context": "navigation",
    },
    {
        "keys": ("Ctrl", "S"),
        "action": "Save the active code file",
        "context": "code editor",
    },
    {
        "keys": ("Meta", "S"),
        "action": "Save when the browser maps a Meta shortcut",
        "context": "code editor",
    },
    {
        "keys": ("Tab", "Shift+Tab"),
        "action": "Accept a visible inline completion, or indent/outdent when no completion is visible",
        "context": "code editor",
    },
    {
        "keys": ("Ctrl/Cmd", "Space"),
        "action": "Request a local inline completion at the caret",
        "context": "code editor",
    },
    {
        "keys": ("Alt", "F1"),
        "action": "Explain the current line locally",
        "context": "code editor",
    },
    {
        "keys": ("Escape", "Tab"),
        "action": "Move keyboard focus out of the editor",
        "context": "code editor",
    },
    {
        "keys": ("Ctrl/Cmd", "/"),
        "action": "Toggle line comments for supported code files",
        "context": "code editor",
    },
    {
        "keys": ("Ctrl/Cmd", "G"),
        "action": "Go to a line in the active code file",
        "context": "code editor",
    },
    {
        "keys": ("Ctrl/Cmd", "W"),
        "action": "Close the active code-file tab",
        "context": "code editor",
    },
    {
        "keys": ("F1",),
        "action": "Open Tool & package help for the command or option at the caret",
        "context": "code editor",
    },
    {
        "keys": ("Up", "Down"),
        "action": "Move through the code tree",
        "context": "code tree",
    },
    {
        "keys": ("Left", "Right"),
        "action": "Collapse or expand a code-tree directory",
        "context": "code tree",
    },
    {
        "keys": ("Enter",),
        "action": "Open the focused code file",
        "context": "code tree",
    },
)


TAB_OPERATIONAL_CONTEXT = {
    "overview": {
        "reads": (
            "Pipeline discovery metadata, recent runs, diagnostics, and result freshness.",
        ),
        "writes": (),
        "network": (),
        "persistence": ("This view does not create persistent records.",),
        "cautions": (
            "Status can become stale after files or jobs change outside OmicsANG.",
        ),
        "prerequisites": ("A registered pipeline must be selected.",),
        "shortcuts": ("Ctrl/Cmd+K opens the command palette.",),
    },
    "study": {
        "reads": (
            "Selected config, candidate sample tables, and referenced local input paths.",
        ),
        "writes": (
            "Role selections affect run preparation; the audit itself does not edit study files.",
        ),
        "network": (),
        "persistence": (
            "A real run records the reviewed study identity in its run context.",
        ),
        "cautions": (
            "Overrides can permit a blocked design; use them only after documenting the reason.",
        ),
        "prerequisites": (
            "A readable sample or metadata table is needed for a complete audit.",
        ),
        "shortcuts": (),
    },
    "run": {
        "reads": (
            "Workflow files, selected config, study audit, environments, and resource settings.",
        ),
        "writes": (
            "Creates job state and logs; a real workflow may create, replace, or remove files according to its rules.",
        ),
        "network": (
            "OmicsANG does not add network access, but the selected workflow or environment tooling may use it.",
        ),
        "persistence": (
            "Run history, job state, logs, and provenance records persist in local OmicsANG state.",
        ),
        "cautions": (
            "A real run executes repository-defined commands with the local user's permissions.",
        ),
        "prerequisites": (
            "Review a dry-run, study gate, configuration, and resource budget first.",
        ),
        "shortcuts": (),
    },
    "results": {
        "reads": (
            "Authorized result roots, attachments, reports, figures, tables, and quality-control artifacts.",
        ),
        "writes": (
            "Attach and detach actions change only OmicsANG attachment metadata.",
        ),
        "network": (),
        "persistence": (
            "Result-directory attachments persist in local OmicsANG state.",
        ),
        "cautions": (
            "Result files may contain sensitive sample labels or embedded metadata.",
        ),
        "prerequisites": (
            "Results must be inside the pipeline or an explicitly configured result root.",
        ),
        "shortcuts": (),
    },
    "capsules": {
        "reads": (
            "Recorded run intent, study identity, source state, inputs, and output inventory.",
        ),
        "writes": (),
        "network": (),
        "persistence": (
            "Capsules are persistent local provenance records created during run finalization.",
        ),
        "cautions": (
            "A capsule may include local filenames and study identifiers; review before sharing.",
        ),
        "prerequisites": (
            "At least one finalized or recoverable run is needed for useful content.",
        ),
        "shortcuts": (),
    },
    "browse": {
        "reads": (
            "Names, types, and sizes of authorized entries beneath the selected pipeline directory.",
        ),
        "writes": (),
        "network": (),
        "persistence": ("Browsing and searching do not create persistent records.",),
        "cautions": (
            "Opening a file can expose its content in the browser; verify it is appropriate before sharing.",
        ),
        "prerequisites": (
            "A registered, readable, non-symlink pipeline root is required.",
        ),
        "shortcuts": (
            "Enter opens the focused directory or file when the browser control provides it.",
        ),
    },
    "code": {
        "reads": (
            "The selected supported UTF-8 code, config, or documentation file and local diagnostics.",
        ),
        "writes": (
            "Save, move, copy, create, and delete actions modify repository files; saves create private backups and deletes use private trash.",
        ),
        "network": (
            "Inline completions, hover explanations, and static package detection are local; only an explicit documentation-link or approved agent action leaves the app.",
        ),
        "persistence": (
            "Saved edits persist in the repository; backups and trash persist in private local app state.",
        ),
        "cautions": (
            "Edits can change analysis behavior and reproducibility; inspect the diff before running or publishing.",
        ),
        "prerequisites": (
            "The file must pass the repository path, type, size, symlink, and UTF-8 checks.",
        ),
        "shortcuts": (
            "Ctrl/Cmd+S saves; Tab accepts a completion or indents; Ctrl/Cmd+Space requests completion; Alt+F1 explains the current line; Ctrl/Cmd+/ toggles comments; Ctrl/Cmd+G goes to a line.",
        ),
    },
    "agents": {
        "reads": (
            "The reviewed prompt plus repository context the approved tool reads.",
        ),
        "writes": (
            "After confirmation, an approved agent CLI is contained to the selected repository and may modify or run commands there.",
        ),
        "network": (
            "External providers may receive prompt and repository context only after explicit acknowledgement.",
        ),
        "persistence": (
            "Agent terminal output is not a durable OmicsANG audit log; provider retention is governed outside OmicsANG.",
        ),
        "cautions": (
            "The OS sandbox restricts what an agent can read from disk; it does not restrict what the CLI sends to its provider.",
            "Exclude secrets, private human data, credentials, and unrelated repositories from prompts and scope.",
        ),
        "prerequisites": (
            "The selected agent CLI must be installed and external-provider use acknowledged.",
        ),
        "shortcuts": (),
    },
    "dag": {
        "reads": ("Workflow structure, target, and selected configuration.",),
        "writes": (
            "Workflow tooling may create its ordinary local metadata while building the graph.",
        ),
        "network": (
            "No network request is added by OmicsANG; workflow hooks remain repository-defined.",
        ),
        "persistence": (
            "The rendered graph is session UI state unless separately saved by the workflow.",
        ),
        "cautions": (
            "A graph for one config or target does not describe every possible run.",
        ),
        "prerequisites": (
            "A detected Snakemake workflow and valid selected config are required.",
        ),
        "shortcuts": (),
    },
    "config": {
        "reads": ("The selected configuration file and inferred local path options.",),
        "writes": (
            "Saving replaces the selected config after creating a private backup.",
        ),
        "network": (),
        "persistence": (
            "Saved values persist in the repository; the backup persists in private local app state.",
        ),
        "cautions": (
            "Path and parameter changes can redirect inputs, outputs, or scientific comparisons.",
        ),
        "prerequisites": (
            "A supported discovered configuration file must be selected.",
        ),
        "shortcuts": (),
    },
    "sra_geo": {
        "reads": (
            "Search terms, accession lists, selected public metadata, and destination settings.",
        ),
        "writes": (
            "Approved downloads create archive and FASTQ files beneath the normalized destination.",
        ),
        "network": (
            "Searches contact NCBI E-utilities; downloads contact repositories used by SRA Toolkit.",
        ),
        "persistence": (
            "Downloaded data and tracked session metadata persist locally.",
        ),
        "cautions": (
            "Confirm access conditions, expected data volume, identifiers, and available disk space before downloading.",
        ),
        "prerequisites": (
            "Network access is required; prefetch and fasterq-dump are required for their selected steps.",
        ),
        "shortcuts": ("Enter submits the focused search field.",),
    },
    "resources": {
        "reads": (
            "Detected host, environment, run-queue, and Slurm command availability.",
        ),
        "writes": (),
        "network": (),
        "persistence": (
            "This view does not change scheduler or environment configuration.",
        ),
        "cautions": (
            "Availability detection does not guarantee current quota, queue time, memory, or accelerator access.",
        ),
        "prerequisites": ("Scheduler commands must be installed for Slurm details.",),
        "shortcuts": (),
    },
    "github": {
        "reads": (
            "Local Git status, branches, worktrees, and configured remote metadata.",
        ),
        "writes": (
            "Available local worktree actions may create repository worktrees; outbound publication actions are disabled here.",
        ),
        "network": (
            "This public build does not dispatch fetch, pull, push, repository creation, or pull-request actions.",
        ),
        "persistence": (
            "Git and worktree changes persist in the repository filesystem.",
        ),
        "cautions": (
            "Review diffs for credentials, private data, generated files, and unintended history before any external publication workflow.",
        ),
        "prerequisites": (
            "Git is required; GitHub context also requires a configured repository.",
        ),
        "shortcuts": (),
    },
    "terminal": {
        "reads": (
            "Bounded session output and status for local, Slurm, download, or agent jobs.",
        ),
        "writes": (
            "Interactive input is sent to the selected live process; cancellation can terminate that process.",
        ),
        "network": (
            "Network behavior belongs to the attached command, not the terminal renderer.",
        ),
        "persistence": (
            "Eligible job logs and session metadata persist locally; terminal view state lasts in the browser session.",
        ),
        "cautions": (
            "Commands and output may reveal paths, sample identifiers, or secrets; cancellation can interrupt incomplete writes.",
        ),
        "prerequisites": (
            "A session must exist; interaction requires a live attached process.",
        ),
        "shortcuts": (),
    },
    "fleet-health": {
        "reads": (
            "Health summaries, diagnostics, scheduler state, and activity across registered pipelines.",
        ),
        "writes": (),
        "network": (),
        "persistence": ("This aggregate view does not create persistent records.",),
        "cautions": (
            "Open the individual pipeline before acting on a condensed fleet signal.",
        ),
        "prerequisites": ("At least one registered pipeline is required.",),
        "shortcuts": ("Left/Right and Home/End move between fleet tabs.",),
    },
    "fleet-task": {
        "reads": (
            "The reviewed task, selected pipeline metadata, and optional worktree context.",
        ),
        "writes": (
            "Approved agents may create worktrees and modify files within each target repository they are contained to.",
        ),
        "network": (
            "External-agent providers may receive the reviewed prompt and repository content their approved CLIs read after acknowledgement.",
        ),
        "persistence": (
            "Fleet task and target session metadata persist locally; repository edits persist on disk.",
        ),
        "cautions": (
            "Each agent is contained to its own target repository, but disclosure to providers is not restricted.",
            "Impact and disclosure multiply across targets; inspect every repository result independently.",
        ),
        "prerequisites": (
            "Select explicit targets, review worktree behavior, and acknowledge external-provider use.",
        ),
        "shortcuts": ("Left/Right and Home/End move between fleet tabs.",),
    },
}


PACKAGE_HELP_META = {
    "summary": "OmicsANG detects package references in the selected file and provides local-use hints plus curated project documentation.",
    "supported_ecosystems": (
        "python",
        "r",
        "javascript",
        "snakemake",
        "shell",
        "yaml",
    ),
    "documentation_policy": "Links are supplied only for a small curated set of official project documentation pages; an unrecognized package has no external link.",
    "privacy": "Detection runs locally on the selected UTF-8 file. OmicsANG does not send source text or package names to a help service.",
    "external_link_notice": "Opening a documentation link leaves the local application; its destination domain is shown before navigation.",
}

COMMAND_HELP_META = {
    "summary": "Caret-aware command and parameter guidance from a small, version-labelled local catalog.",
    "supported_commands": ("fastqc",),
    "privacy": "Command matching runs in the browser and catalog lookup runs on this OmicsANG server. No command is executed and no source text is uploaded.",
    "version_notice": "Catalog entries describe a named upstream release. Confirm the pipeline environment version before relying on version-specific behavior.",
}


def public_help_catalog() -> dict:
    """Return a fresh JSON-safe copy so request handlers cannot mutate constants."""
    tabs = [{**tab, **TAB_OPERATIONAL_CONTEXT[tab["id"]]} for tab in TAB_HELP]
    return {
        "tabs": copy.deepcopy(tabs),
        "shortcuts": copy.deepcopy(list(SHORTCUT_HELP)),
        "package_help": copy.deepcopy(PACKAGE_HELP_META),
        "command_help": copy.deepcopy(COMMAND_HELP_META),
    }


# These are short, original descriptions paired only with official project docs.
PACKAGE_CATALOG: dict[tuple[str, str], dict[str, str]] = {
    ("python", "numpy"): {
        "name": "NumPy",
        "summary": "Array computing primitives commonly used for numerical workflow steps.",
        "docs_url": "https://numpy.org/doc/stable/",
    },
    ("python", "pandas"): {
        "name": "pandas",
        "summary": "Labeled tables and data transformations for sample sheets and analysis results.",
        "docs_url": "https://pandas.pydata.org/docs/",
    },
    ("python", "scipy"): {
        "name": "SciPy",
        "summary": "Scientific algorithms for statistics, optimization, signal processing, and sparse data.",
        "docs_url": "https://docs.scipy.org/doc/scipy/",
    },
    ("python", "pysam"): {
        "name": "pysam",
        "summary": "Python access to common alignment and variant file operations.",
        "docs_url": "https://pysam.readthedocs.io/en/latest/",
    },
    ("python", "biopython"): {
        "name": "Biopython",
        "summary": "Python utilities for biological sequences, records, formats, and online identifiers.",
        "docs_url": "https://biopython.org/docs/latest/",
    },
    ("python", "scikit-learn"): {
        "name": "scikit-learn",
        "summary": "Classical machine-learning estimators, preprocessing, model selection, and metrics.",
        "docs_url": "https://scikit-learn.org/stable/",
    },
    ("python", "scanpy"): {
        "name": "Scanpy",
        "summary": "Single-cell analysis workflows built around annotated expression matrices.",
        "docs_url": "https://scanpy.readthedocs.io/en/stable/",
    },
    ("python", "anndata"): {
        "name": "AnnData",
        "summary": "Annotated matrix containers used by many single-cell Python workflows.",
        "docs_url": "https://anndata.readthedocs.io/en/stable/",
    },
    ("python", "matplotlib"): {
        "name": "Matplotlib",
        "summary": "Foundational Python plotting tools for static and interactive scientific figures.",
        "docs_url": "https://matplotlib.org/stable/",
    },
    ("python", "seaborn"): {
        "name": "seaborn",
        "summary": "Statistical visualization helpers built on Matplotlib and tabular data.",
        "docs_url": "https://seaborn.pydata.org/",
    },
    ("python", "fastapi"): {
        "name": "FastAPI",
        "summary": "Python tools for typed HTTP APIs using ASGI and schema-based validation.",
        "docs_url": "https://fastapi.tiangolo.com/",
    },
    ("python", "pydantic"): {
        "name": "Pydantic",
        "summary": "Typed validation and serialization for Python data models.",
        "docs_url": "https://docs.pydantic.dev/latest/",
    },
    ("python", "pyyaml"): {
        "name": "PyYAML",
        "summary": "Python parsing and serialization for YAML configuration files.",
        "docs_url": "https://pyyaml.org/wiki/PyYAMLDocumentation",
    },
    ("r", "deseq2"): {
        "name": "DESeq2",
        "summary": "Bioconductor methods for count-based differential expression analysis.",
        "docs_url": "https://bioconductor.org/packages/DESeq2/",
    },
    ("r", "edger"): {
        "name": "edgeR",
        "summary": "Bioconductor models for differential analysis of count data.",
        "docs_url": "https://bioconductor.org/packages/edgeR/",
    },
    ("r", "limma"): {
        "name": "limma",
        "summary": "Bioconductor linear-model tools for expression and other high-dimensional assays.",
        "docs_url": "https://bioconductor.org/packages/limma/",
    },
    ("r", "ggplot2"): {
        "name": "ggplot2",
        "summary": "Grammar-based statistical graphics for R.",
        "docs_url": "https://ggplot2.tidyverse.org/",
    },
    ("r", "dplyr"): {
        "name": "dplyr",
        "summary": "R verbs for selecting, joining, grouping, and transforming tabular data.",
        "docs_url": "https://dplyr.tidyverse.org/",
    },
    ("r", "data.table"): {
        "name": "data.table",
        "summary": "High-performance tabular data operations in R.",
        "docs_url": "https://rdatatable.gitlab.io/data.table/",
    },
    ("r", "seurat"): {
        "name": "Seurat",
        "summary": "R workflows and data structures for single-cell analysis.",
        "docs_url": "https://satijalab.org/seurat/",
    },
    ("r", "singlecellexperiment"): {
        "name": "SingleCellExperiment",
        "summary": "Bioconductor containers for single-cell assays and annotations.",
        "docs_url": "https://bioconductor.org/packages/SingleCellExperiment/",
    },
    ("javascript", "react"): {
        "name": "React",
        "summary": "Component-based rendering for browser interfaces.",
        "docs_url": "https://react.dev/",
    },
    ("javascript", "d3"): {
        "name": "D3",
        "summary": "Data-driven browser visualization primitives.",
        "docs_url": "https://d3js.org/",
    },
    ("javascript", "express"): {
        "name": "Express",
        "summary": "Minimal HTTP server and routing tools for Node.js.",
        "docs_url": "https://expressjs.com/",
    },
    ("javascript", "xterm"): {
        "name": "xterm.js",
        "summary": "A browser terminal component for rendering interactive command sessions.",
        "docs_url": "https://xtermjs.org/docs/",
    },
    ("snakemake", "snakemake"): {
        "name": "Snakemake",
        "summary": "A rule-based workflow engine for reproducible data analysis.",
        "docs_url": "https://snakemake.readthedocs.io/en/stable/",
    },
    ("snakemake", "conda"): {
        "name": "Snakemake Conda integration",
        "summary": "Per-rule environment selection declared by a Snakemake conda directive.",
        "docs_url": "https://snakemake.readthedocs.io/en/stable/snakefiles/deployment.html#integrated-package-management",
    },
    ("shell", "samtools"): {
        "name": "samtools",
        "summary": "Command-line operations for sequence alignment files.",
        "docs_url": "https://www.htslib.org/doc/samtools.html",
    },
    ("shell", "bcftools"): {
        "name": "bcftools",
        "summary": "Command-line querying and transformation of variant call files.",
        "docs_url": "https://samtools.github.io/bcftools/bcftools.html",
    },
    ("shell", "bedtools"): {
        "name": "bedtools",
        "summary": "Command-line comparisons and transformations for genomic intervals.",
        "docs_url": "https://bedtools.readthedocs.io/en/latest/",
    },
    ("shell", "bwa"): {
        "name": "BWA",
        "summary": "Read alignment tools for mapping sequences to a reference genome.",
        "docs_url": "https://bio-bwa.sourceforge.net/bwa.shtml",
    },
    ("shell", "bowtie2"): {
        "name": "Bowtie 2",
        "summary": "A command-line aligner for reads and longer sequences.",
        "docs_url": "https://bowtie-bio.sourceforge.net/bowtie2/manual.shtml",
    },
    ("shell", "minimap2"): {
        "name": "minimap2",
        "summary": "Pairwise alignment for long reads, assemblies, and related sequence data.",
        "docs_url": "https://lh3.github.io/minimap2/minimap2.html",
    },
    ("shell", "star"): {
        "name": "STAR",
        "summary": "Splice-aware alignment for RNA sequencing reads.",
        "docs_url": "https://github.com/alexdobin/STAR/blob/master/doc/STARmanual.pdf",
    },
    ("shell", "fastqc"): {
        "name": "FastQC",
        "summary": "Read-level quality summaries for high-throughput sequence data.",
        "docs_url": "https://www.bioinformatics.babraham.ac.uk/projects/fastqc/Help/",
    },
    ("shell", "multiqc"): {
        "name": "MultiQC",
        "summary": "Aggregation of many bioinformatics tool reports into one summary.",
        "docs_url": "https://docs.seqera.io/multiqc/",
    },
    ("shell", "cutadapt"): {
        "name": "Cutadapt",
        "summary": "Adapter and unwanted-sequence trimming for sequencing reads.",
        "docs_url": "https://cutadapt.readthedocs.io/en/stable/",
    },
    ("shell", "featurecounts"): {
        "name": "featureCounts",
        "summary": "Assignment of aligned reads to genomic features.",
        "docs_url": "https://subread.sourceforge.net/featureCounts.html",
    },
    ("shell", "salmon"): {
        "name": "Salmon",
        "summary": "Transcript abundance estimation from RNA sequencing data.",
        "docs_url": "https://salmon.readthedocs.io/en/latest/",
    },
    ("shell", "kallisto"): {
        "name": "kallisto",
        "summary": "Transcript abundance estimation using pseudoalignment.",
        "docs_url": "https://pachterlab.github.io/kallisto/manual",
    },
    ("shell", "prefetch"): {
        "name": "SRA Toolkit prefetch",
        "summary": "Retrieval of sequence archives from NCBI SRA.",
        "docs_url": "https://github.com/ncbi/sra-tools/wiki/08.-prefetch-and-fasterq-dump",
    },
    ("shell", "fasterq-dump"): {
        "name": "SRA Toolkit fasterq-dump",
        "summary": "Conversion of locally available SRA runs into FASTQ files.",
        "docs_url": "https://github.com/ncbi/sra-tools/wiki/08.-prefetch-and-fasterq-dump",
    },
    ("shell", "conda"): {
        "name": "conda",
        "summary": "Environment and package management used by many scientific workflows.",
        "docs_url": "https://docs.conda.io/projects/conda/en/stable/",
    },
    ("shell", "mamba"): {
        "name": "mamba",
        "summary": "A compatible environment solver and package manager for conda environments.",
        "docs_url": "https://mamba.readthedocs.io/en/latest/",
    },
}


# Original, concise descriptions of the command-line surface implemented by the
# tagged upstream launcher.  Keeping this static is deliberate: silently running
# a repository-controlled executable merely to obtain ``--help`` would execute
# untrusted code under the user's account.
COMMAND_HELP_CATALOG: dict[str, dict] = {
    "fastqc": {
        "command": "fastqc",
        "name": "FastQC",
        "catalog_version": "fastqc-0.12.1",
        "version_scope": "FastQC 0.12.1 launcher",
        "summary": "Generate per-input sequencing quality-control reports in interactive or non-interactive mode.",
        "synopsis": "fastqc [options] <sequence-file-or-directory> [...]",
        "docs_url": "https://www.bioinformatics.babraham.ac.uk/projects/fastqc/Help/",
        "source_url": "https://github.com/s-andrews/FastQC/blob/v0.12.1/fastqc",
        "arguments": (
            {
                "name": "sequence-file-or-directory",
                "repeatable": True,
                "summary": "One or more FASTQ, SAM/BAM, or supported Nanopore inputs. With no inputs FastQC opens its graphical application.",
            },
        ),
        "options": (
            {
                "flags": ("-h", "--help"),
                "category": "general",
                "summary": "Print the command-line help and exit.",
            },
            {
                "flags": ("-v", "--version"),
                "category": "general",
                "summary": "Print the FastQC version and exit.",
            },
            {
                "flags": ("-o", "--outdir"),
                "value": "<directory>",
                "category": "output",
                "summary": "Write reports to this existing directory instead of beside each input.",
                "caution": "FastQC does not create the directory; create and validate it before the run.",
            },
            {
                "flags": ("--casava",),
                "category": "input",
                "summary": "Treat raw CASAVA-named files from the same sample as a group and omit reads carrying the failed-filter header flag.",
            },
            {
                "flags": ("--nano",),
                "category": "input",
                "summary": "Use the legacy Nanopore FAST5 input mode, accepting directories and combining their supported FAST5 reads.",
                "caution": "Confirm that the installed FastQC release supports the Nanopore file layout in use.",
            },
            {
                "flags": ("--nofilter",),
                "category": "input",
                "summary": "Keep reads marked as poor quality when CASAVA mode is active.",
                "caution": "This has meaning only together with --casava.",
            },
            {
                "flags": ("--extract",),
                "category": "output",
                "summary": "Unpack each generated report archive after it is written.",
            },
            {
                "flags": ("--noextract",),
                "category": "output",
                "summary": "Keep the report archive compressed after non-interactive processing.",
            },
            {
                "flags": ("--delete",),
                "category": "output",
                "summary": "Remove the report zip after successful extraction.",
                "caution": "Destructive output option; it is effective only with --extract.",
            },
            {
                "flags": ("-j", "--java"),
                "value": "<path>",
                "category": "runtime",
                "summary": "Launch FastQC with this explicit Java executable instead of resolving java from PATH.",
            },
            {
                "flags": ("--nogroup",),
                "category": "report",
                "summary": "Show per-base results without grouping positions for reads longer than 50 bases.",
                "caution": "Long reads can make plots extremely large and can exhaust memory; do not combine with --expgroup.",
            },
            {
                "flags": ("--expgroup",),
                "category": "report",
                "summary": "Use the launcher's exponential base-grouping mode.",
                "caution": "Advanced version-specific option; it cannot be combined with --nogroup.",
            },
            {
                "flags": ("--min_length",),
                "value": "<bases>",
                "category": "report",
                "summary": "Set an artificial minimum displayed sequence length so reports from variable read lengths can use comparable groups.",
            },
            {
                "flags": ("--dup_length",),
                "value": "<bases>",
                "category": "analysis",
                "summary": "Truncate reads to this length when defining duplicates for duplication and overrepresented-sequence analyses.",
            },
            {
                "flags": ("-f", "--format"),
                "value": "<fastq|bam|sam|bam_mapped|sam_mapped>",
                "category": "input",
                "summary": "Override filename-based input-format detection with one supported format.",
            },
            {
                "flags": ("--memory",),
                "value": "<MB>",
                "category": "resources",
                "summary": "Set the base memory allocation per concurrently processed input; the 0.12.1 default is 512 MB.",
                "caution": "The 0.12.1 launcher accepts 100-10000 MB and multiplies this allocation by --threads.",
            },
            {
                "flags": ("--svg",),
                "category": "output",
                "summary": "Use SVG figures in the HTML report; the archive still contains both SVG and PNG figures.",
            },
            {
                "flags": ("-t", "--threads"),
                "value": "<count>",
                "category": "resources",
                "summary": "Process this many input files concurrently.",
                "caution": "This is file-level concurrency. Total Java memory scales with --memory times --threads and concurrent files can contend for I/O.",
            },
            {
                "flags": ("-c", "--contaminants"),
                "value": "<file>",
                "category": "analysis",
                "summary": "Use a readable tab-delimited name-and-sequence contaminant list for overrepresented-sequence screening.",
            },
            {
                "flags": ("-a", "--adapters"),
                "value": "<file>",
                "category": "analysis",
                "summary": "Use a readable tab-delimited name-and-sequence adapter list for adapter-content searches.",
            },
            {
                "flags": ("-l", "--limits"),
                "value": "<file>",
                "category": "analysis",
                "summary": "Use a custom limits file for module warning/failure thresholds and optional module suppression.",
                "caution": "Review changes carefully because this file changes QC interpretation, not only presentation.",
            },
            {
                "flags": ("-k", "--kmers"),
                "value": "<2-10>",
                "category": "analysis",
                "summary": "Set the K-mer length used by the K-mer content module; the documented default is 7.",
            },
            {
                "flags": ("-q", "--quiet"),
                "category": "general",
                "summary": "Suppress progress output while retaining error messages.",
            },
            {
                "flags": ("-d", "--dir"),
                "value": "<directory>",
                "category": "runtime",
                "summary": "Use this existing writable directory for temporary report-image files.",
            },
        ),
    },
}


def command_help_names() -> tuple[str, ...]:
    """Return the stable command identifiers available to the editor matcher."""
    return tuple(sorted(COMMAND_HELP_CATALOG))


def command_help(command: str) -> dict | None:
    """Return a fresh static command guide without resolving or executing a tool."""
    key = str(command or "").strip().casefold()
    guide = COMMAND_HELP_CATALOG.get(key)
    return copy.deepcopy(guide) if guide else None


PACKAGE_ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    ("python", "bio"): ("python", "biopython"),
    ("python", "sklearn"): ("python", "scikit-learn"),
    ("python", "yaml"): ("python", "pyyaml"),
    ("javascript", "xterm-addon-fit"): ("javascript", "xterm"),
    ("javascript", "@xterm/xterm"): ("javascript", "xterm"),
}


PYTHON_STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {
    "__future__",
    "builtins",
    "typing",
}
R_BASE_PACKAGES = {
    "base",
    "compiler",
    "datasets",
    "graphics",
    "grdevices",
    "grid",
    "methods",
    "parallel",
    "splines",
    "stats",
    "stats4",
    "tcltk",
    "tools",
    "utils",
}
JS_BUILTINS = {
    "assert",
    "buffer",
    "child_process",
    "crypto",
    "events",
    "fs",
    "http",
    "https",
    "module",
    "os",
    "path",
    "process",
    "stream",
    "url",
    "util",
    "worker_threads",
    "zlib",
}


def _python_modules(text: str) -> set[str]:
    modules: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".", 1)[0])
    else:
        for match in re.finditer(r"(?m)^\s*import\s+([A-Za-z_][\w.]*)", text):
            modules.add(match.group(1).split(".", 1)[0])
        for match in re.finditer(r"(?m)^\s*from\s+([A-Za-z_][\w.]*)\s+import\b", text):
            modules.add(match.group(1).split(".", 1)[0])
    return {name for name in modules if name.casefold() not in PYTHON_STDLIB}


def _r_packages(text: str) -> set[str]:
    packages = {
        match.group(2)
        for match in re.finditer(
            r"\b(?:library|require)\s*\(\s*(['\"]?)([A-Za-z][\w.]*)\1\s*\)",
            text,
            re.IGNORECASE,
        )
    }
    packages.update(
        match.group(1)
        for match in re.finditer(r"\b([A-Za-z][\w.]*)\s*:::{0,1}\s*[A-Za-z.]", text)
    )
    return {name for name in packages if name.casefold() not in R_BASE_PACKAGES}


def _js_package_name(specifier: str) -> str | None:
    value = specifier.strip()
    if (
        not value
        or value.startswith((".", "/", "node:", "http:", "https:", "data:"))
        or value in JS_BUILTINS
    ):
        return None
    if value.startswith("@"):
        parts = value.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else value
    return value.split("/", 1)[0]


def _javascript_packages(text: str) -> set[str]:
    specs: set[str] = set()
    patterns = (
        r"\b(?:import|export)\s+(?:[^'\";]+?\s+from\s+)?['\"]([^'\"]+)['\"]",
        r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
        r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
    )
    for pattern in patterns:
        specs.update(match.group(1) for match in re.finditer(pattern, text))
    return {name for spec in specs if (name := _js_package_name(spec))}


def _manifest_javascript_packages(text: str) -> set[str]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(data, dict):
        return set()
    packages: set[str] = set()
    for key in (
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    ):
        values = data.get(key)
        if isinstance(values, dict):
            packages.update(str(name) for name in values)
    return packages


def _dependency_name(raw: str) -> str | None:
    value = raw.strip().strip("'\"")
    if not value or value.startswith(("-", ".", "/", "http:", "https:", "git+")):
        return None
    value = value.split(";", 1)[0].strip()
    if "::" in value:
        value = value.rsplit("::", 1)[1]
    match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.-]*)", value)
    return match.group(1) if match else None


def _requirements_packages(text: str) -> set[str]:
    packages: set[str] = set()
    for line in text.splitlines():
        value = line.split("#", 1)[0].strip()
        if name := _dependency_name(value):
            packages.add(name)
    return packages


def _pyproject_packages(text: str) -> set[str]:
    packages: set[str] = set()
    for match in re.finditer(r"\bdependencies\s*=\s*\[(.*?)\]", text, re.DOTALL):
        for quoted in re.finditer(r"['\"]([^'\"]+)['\"]", match.group(1)):
            if name := _dependency_name(quoted.group(1)):
                packages.add(name)
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]").casefold()
            continue
        if section == "tool.poetry.dependencies":
            match = re.match(r"([A-Za-z0-9_.-]+)\s*=", stripped)
            if match and match.group(1).casefold() != "python":
                packages.add(match.group(1))
    return packages


def _yaml_dependencies(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    dependency_indent: int | None = None
    pip_indent: int | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if re.match(r"dependencies\s*:\s*(?:#.*)?$", stripped, re.IGNORECASE):
            dependency_indent = indent
            pip_indent = None
            continue
        if dependency_indent is not None and indent <= dependency_indent:
            dependency_indent = None
            pip_indent = None
        if dependency_indent is not None:
            item = re.match(r"-\s*([^#]+)", stripped)
            if item:
                raw = item.group(1).strip()
                if raw.casefold() == "pip:":
                    pip_indent = indent
                    found.append(("conda", "pip"))
                    continue
                if pip_indent is not None and indent <= pip_indent:
                    pip_indent = None
                if name := _dependency_name(raw):
                    found.append(
                        ("python" if pip_indent is not None else "conda", name)
                    )
        action = re.match(r"uses\s*:\s*['\"]?([^@'\"\s]+)(?:@[^'\"\s]+)?", stripped)
        if action:
            found.append(("yaml", action.group(1)))
    return found


def _shell_commands(text: str) -> set[str]:
    """Return only curated tools used in command position, never arbitrary code."""
    commands: set[str] = set()
    curated = {name for ecosystem, name in PACKAGE_CATALOG if ecosystem == "shell"}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]
        for name in curated:
            pattern = (
                rf"(?:^\s*|[|;&'\"]\s*)(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
                rf"(?:sudo\s+|env\s+)?(?:\S*/)?{re.escape(name)}(?=\s|$)"
            )
            if re.search(pattern, line, re.IGNORECASE):
                commands.add(name)
    return commands


def _lookup_package(ecosystem: str, name: str) -> dict[str, str] | None:
    key = (ecosystem, name.casefold())
    key = PACKAGE_ALIASES.get(key, key)
    if key in PACKAGE_CATALOG:
        return PACKAGE_CATALOG[key]
    # Environment YAML declares executable and language packages together.  A
    # unique curated name can still supply its official docs without changing
    # the ecosystem reported to the browser.
    matches = [
        value
        for (catalog_ecosystem, catalog_name), value in PACKAGE_CATALOG.items()
        if catalog_name == name.casefold() and catalog_ecosystem != "snakemake"
    ]
    return matches[0] if len(matches) == 1 else None


def _local_help(ecosystem: str, name: str) -> str:
    if ecosystem == "python":
        return f'In the active pipeline environment, use Python help("{name}") and confirm the environment manifest before changing versions.'
    if ecosystem == "r":
        return f'In the active R environment, use help(package = "{name}") and review the project environment before changing versions.'
    if ecosystem == "javascript":
        return f"Review {name} in package.json and its locally installed package metadata before changing the dependency."
    if ecosystem == "snakemake":
        return (
            "Use snakemake --help locally and validate workflow changes with a dry-run."
        )
    if ecosystem in {"conda", "yaml"}:
        return f"Review {name} in the environment or workflow YAML and confirm the resolved environment before running."
    return f"Run {name} --help in the pipeline environment and confirm the workflow pins the intended tool."


def detect_packages(path: Path, text: str, language: str) -> list[dict]:
    """Detect dependency references heuristically without importing or running them."""
    found: dict[tuple[str, str], dict] = {}

    def add(ecosystem: str, name: str, detected_from: str) -> None:
        cleaned = name.strip()
        if not cleaned:
            return
        key = (ecosystem, cleaned.casefold())
        item = found.setdefault(
            key,
            {"name": cleaned, "ecosystem": ecosystem, "sources": []},
        )
        if detected_from not in item["sources"]:
            item["sources"].append(detected_from)

    if language in {"python", "snakemake"}:
        for name in _python_modules(text):
            add("python", name, "import statement")
    if language == "r":
        for name in _r_packages(text):
            add("r", name, "R package reference")
    if language in {"javascript", "typescript"}:
        for name in _javascript_packages(text):
            add("javascript", name, "module import")
    if path.name == "package.json":
        for name in _manifest_javascript_packages(text):
            add("javascript", name, "package manifest")
    if (
        path.name.casefold().startswith("requirements")
        and path.suffix.casefold() == ".txt"
    ):
        for name in _requirements_packages(text):
            add("python", name, "requirements file")
    if path.name == "pyproject.toml":
        for name in _pyproject_packages(text):
            add("python", name, "project manifest")
    if language == "yaml":
        for ecosystem, name in _yaml_dependencies(text):
            add(ecosystem, name, "YAML dependency")
    if language == "snakemake":
        add("snakemake", "snakemake", "workflow syntax")
        if re.search(r"(?m)^\s*conda\s*:", text):
            add("snakemake", "conda", "conda directive")
    if language in {"shell", "snakemake", "docker"}:
        for name in _shell_commands(text):
            add("shell", name, "shell command")

    packages: list[dict] = []
    for item in found.values():
        catalog = _lookup_package(item["ecosystem"], item["name"])
        display_name = catalog["name"] if catalog else item["name"]
        packages.append(
            {
                "name": display_name,
                "ecosystem": item["ecosystem"],
                "summary": catalog["summary"]
                if catalog
                else f"This file references {item['name']}, but OmicsANG has no curated description for it.",
                "docs_url": catalog["docs_url"] if catalog else None,
                "docs_domain": urlparse(catalog["docs_url"]).hostname
                if catalog
                else None,
                "detected_from": ", ".join(item["sources"]),
                "local_help": _local_help(item["ecosystem"], item["name"]),
            }
        )
    packages.sort(key=lambda item: (item["ecosystem"], item["name"].casefold()))
    return packages
