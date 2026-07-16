# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Thin git / worktree / gh helpers used by the agent control center."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urlunsplit

from . import settings

GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
OUTBOUND_DISABLED = "outbound Git/GitHub mutations are disabled in this release"
PROTECTED_STAGE_PARTS = {
    ".git",
    ".benchtop",
    ".benchtop-dev",
    ".claude",
    ".codex",
    "graphify-out",
    "instruction.md",
    "handoff.md",
    "AGENTS.md",
    "CLAUDE.md",
}


def _redact_remote(value: str) -> str:
    text = str(value or "").strip()
    if "://" not in text:
        # Git's SCP-like syntax is not understood by ``urlparse``.  Strip the
        # optional SSH/userinfo prefix so embedded OAuth tokens never reach the
        # browser, while retaining the useful host and repository path.
        _, separator, host_path = text.rpartition("@")
        if separator and ":" in host_path:
            host, remote_path = host_path.split(":", 1)
            if host and remote_path:
                return f"{host}:{remote_path}"
        return text
    parsed = urlparse(text)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _run_capture(args: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:  # pragma: no cover
        return 1, "", str(exc)


def _run(args: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    rc, stdout, stderr = _run_capture(args, cwd, timeout=timeout)
    return rc, (stdout + stderr).strip()


def _run_json(args: list[str], cwd: Path, timeout: int = 30):
    rc, stdout, stderr = _run_capture(args, cwd, timeout=timeout)
    out = (stdout + stderr).strip()
    if rc != 0:
        return rc, out, None
    try:
        return rc, out, json.loads(stdout or "null")
    except json.JSONDecodeError:
        return 1, out or "invalid JSON", None


def status(cwd: Path) -> dict:
    rc_repo, inside = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd)
    inside_lines = [line.strip() for line in inside.splitlines()]
    if rc_repo != 0 or "true" not in inside_lines:
        return {
            "branch": None,
            "remote": None,
            "upstream": None,
            "dirty": 0,
            "dirty_files": [],
            "ahead": None,
            "behind": None,
            "is_repo": False,
        }
    rc_b, branch = _run(["git", "branch", "--show-current"], cwd)
    if rc_b != 0 or not branch:
        rc_b, branch = _run(["git", "symbolic-ref", "--short", "HEAD"], cwd)
    _, porcelain = _run(["git", "status", "--porcelain"], cwd)
    rc_r, remote = _run(["git", "remote", "get-url", "origin"], cwd)
    _, ahead = _run(["git", "rev-list", "--count", "@{u}..HEAD"], cwd)
    rc_u, upstream = _run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd
    )
    _, counts = _run(["git", "rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd)
    behind, ahead_count = None, ahead if ahead.isdigit() else None
    if counts:
        parts = counts.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            behind, ahead_count = parts[0], parts[1]
    dirty_lines = [line for line in porcelain.splitlines() if line.strip()]
    return {
        "branch": branch if rc_b == 0 else None,
        "remote": _redact_remote(remote) if rc_r == 0 else None,
        "upstream": upstream if rc_u == 0 and upstream else None,
        "dirty": len(dirty_lines),
        "dirty_files": dirty_lines[:50],
        "ahead": ahead_count,
        "behind": behind,
        "is_repo": True,
    }


def github_slug(remote: str | None) -> str:
    remote = (remote or "").strip()
    if not remote:
        return ""
    if remote.startswith("git@github.com:"):
        slug = remote.split(":", 1)[1]
    elif remote.startswith("github.com:"):
        # ``status`` strips SCP-like userinfo before returning remote metadata.
        slug = remote.split(":", 1)[1]
    else:
        parsed = urlparse(remote)
        if (parsed.hostname or "").lower() != "github.com":
            return ""
        slug = parsed.path.lstrip("/")
    slug = slug.split("?", 1)[0].split("#", 1)[0].removesuffix(".git").strip("/")
    parts = [part for part in slug.split("/") if part]
    if len(parts) >= 2:
        slug = "/".join(parts[:2])
    return slug if GITHUB_REPO_RE.match(slug) else ""


def _branches(cwd: Path) -> dict:
    _, local = _run(["git", "branch", "--format=%(refname:short)"], cwd)
    _, remote = _run(["git", "branch", "-r", "--format=%(refname:short)"], cwd)
    return {
        "local": [b.strip() for b in local.splitlines() if b.strip()],
        "remote": [
            b.strip() for b in remote.splitlines() if b.strip() and "HEAD" not in b
        ],
    }


def github_status(cwd: Path) -> dict:
    git = status(cwd)
    slug = github_slug(git.get("remote"))
    gh_available = bool(shutil.which("gh"))
    gh_auth = "gh CLI not found"
    gh_rc = 1
    if gh_available:
        gh_rc, gh_auth = _run(["gh", "auth", "status"], cwd, timeout=12)
    auth_ok = gh_rc == 0
    user = ""
    if auth_ok:
        _, out = _run(["gh", "api", "user", "--jq", ".login"], cwd, timeout=12)
        if out and "\n" not in out and "error" not in out.lower():
            user = out.strip()
    repo = None
    prs = []
    repo_error = ""
    if slug and auth_ok:
        rc, out, data = _run_json(
            [
                "gh",
                "repo",
                "view",
                slug,
                "--json",
                "nameWithOwner,url,description,isPrivate,defaultBranchRef,pushedAt,viewerPermission",
            ],
            cwd,
            timeout=18,
        )
        if rc == 0 and isinstance(data, dict):
            repo = data
        else:
            repo_error = out[-500:]
        rc, out, data = _run_json(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                slug,
                "--limit",
                "20",
                "--json",
                "number,title,state,headRefName,baseRefName,url,updatedAt,isDraft",
            ],
            cwd,
            timeout=18,
        )
        if rc == 0 and isinstance(data, list):
            prs = data
    return {
        "git": git,
        "github_slug": slug,
        "gh_available": gh_available,
        "gh_authenticated": auth_ok,
        "gh_error": "" if auth_ok else gh_auth[-500:],
        "gh_user": user,
        "repo": repo,
        "repo_error": repo_error,
        "branches": _branches(cwd)
        if git.get("is_repo")
        else {"local": [], "remote": []},
        "prs": prs,
    }


def fetch(cwd: Path) -> dict:
    rc, out = _run(["git", "fetch", "--all", "--prune"], cwd, timeout=120)
    return {"ok": rc == 0, "output": out}


def pull_ff_only(cwd: Path) -> dict:
    rc, out = _run(["git", "pull", "--ff-only"], cwd, timeout=120)
    return {"ok": rc == 0, "output": out}


def diff(cwd: Path, staged: bool = False) -> str:
    args = ["git", "diff"] + (["--staged"] if staged else [])
    _, out = _run(args, cwd)
    return out


def diffstat(cwd: Path) -> dict:
    """Working-tree change summary (tracked + untracked)."""
    _, porcelain = _run(["git", "status", "--porcelain"], cwd)
    files = [line for line in porcelain.splitlines() if line.strip()]
    _, stat = _run(["git", "diff", "--shortstat", "HEAD"], cwd)
    return {"files": len(files), "shortstat": stat.strip(), "changed": files[:60]}


def commit_all(cwd: Path, message: str) -> dict:
    """Legacy entry point retained as an explicit fail-closed safety barrier."""
    return {"ok": False, "output": OUTBOUND_DISABLED}


def validate_branch(cwd: Path, branch: str) -> tuple[bool, str]:
    value = str(branch or "").strip()
    if not value or len(value.encode("utf-8")) > 240:
        return False, "branch name is required and must be at most 240 bytes"
    if value.startswith("-") or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False, "invalid branch name"
    rc, out = _run(["git", "check-ref-format", "--branch", value], cwd)
    return rc == 0, value if rc == 0 else (out or "invalid branch name")


def validate_revision(cwd: Path, revision: str) -> tuple[bool, str]:
    value = str(revision or "").strip()
    if not value or len(value.encode("utf-8")) > 240 or value.startswith("-"):
        return False, "invalid base revision"
    rc, out = _run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{value}^{{commit}}",
        ],
        cwd,
    )
    return rc == 0, value if rc == 0 else (out or "base revision is not a commit")


def push(cwd: Path, branch: str, *, confirmed: bool = False) -> dict:
    if not confirmed:
        return {"ok": False, "output": OUTBOUND_DISABLED}
    if not branch:
        _, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    valid, detail = validate_branch(cwd, branch)
    if not valid:
        return {"ok": False, "output": detail}
    rc, out = _run(["git", "push", "-u", "origin", "--", branch], cwd)
    return {"ok": rc == 0, "output": out}


def open_pr(
    cwd: Path,
    title: str,
    body: str,
    draft: bool = False,
    *,
    confirmed: bool = False,
) -> dict:
    """Create a PR with the gh CLI (requires an authenticated gh)."""
    if not confirmed:
        return {"ok": False, "url": "", "output": OUTBOUND_DISABLED}
    args = ["gh", "pr", "create", "--title", title, "--body", body]
    if draft:
        args.append("--draft")
    rc, out = _run(args, cwd)
    url = ""
    for line in out.splitlines():
        if line.startswith("http"):
            url = line.strip()
    return {"ok": rc == 0, "url": url, "output": out}


def create_github_repo(
    cwd: Path,
    full_name: str,
    private: bool = True,
    description: str = "",
    push_initial: bool = False,
    *,
    confirmed: bool = False,
) -> dict:
    if not confirmed:
        return {"ok": False, "output": OUTBOUND_DISABLED}
    full_name = full_name.strip()
    if not GITHUB_REPO_RE.match(full_name):
        return {"ok": False, "output": "repository must look like owner/name"}
    args = ["gh", "repo", "create", full_name, "--source", ".", "--remote", "origin"]
    args.append("--private" if private else "--public")
    if description.strip():
        args.extend(["--description", description.strip()])
    if push_initial:
        args.append("--push")
    rc, out = _run(args, cwd, timeout=120)
    return {"ok": rc == 0, "output": out}


def connect_github_repo(cwd: Path, full_name: str, *, confirmed: bool = False) -> dict:
    """Set or update origin to an existing GitHub repository slug."""
    if not confirmed:
        return {"ok": False, "output": OUTBOUND_DISABLED}
    full_name = full_name.strip()
    if not GITHUB_REPO_RE.match(full_name):
        return {"ok": False, "output": "repository must look like owner/name"}
    if not status(cwd).get("is_repo"):
        return {"ok": False, "output": "not a git repository"}
    remote_url = f"https://github.com/{full_name}.git"
    rc, _ = _run(["git", "remote", "get-url", "origin"], cwd)
    if rc == 0:
        rc, out = _run(["git", "remote", "set-url", "origin", remote_url], cwd)
        action = "updated"
    else:
        rc, out = _run(["git", "remote", "add", "origin", remote_url], cwd)
        action = "added"
    if rc != 0:
        return {"ok": False, "output": out}
    return {
        "ok": True,
        "remote": remote_url,
        "output": f"{action} origin -> {remote_url}",
    }


def list_worktrees(cwd: Path) -> list[dict]:
    rc, out = _run(["git", "worktree", "list", "--porcelain"], cwd)
    if rc != 0:
        return []
    trees, cur = [], {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur:
                trees.append(cur)
            cur = {"path": line.split(" ", 1)[1]}
        elif line.startswith("branch "):
            cur["branch"] = line.split(" ", 1)[1].replace("refs/heads/", "")
        elif line.startswith("HEAD "):
            cur["head"] = line.split(" ", 1)[1][:8]
    if cur:
        trees.append(cur)
    return trees


def _git_resolved_path(cwd: Path, selector: str) -> Path | None:
    """Resolve a path reported by ``git rev-parse`` without trusting stderr."""
    rc, stdout, _ = _run_capture(["git", "rev-parse", selector], cwd)
    value = stdout.strip()
    if rc != 0 or not value or "\n" in value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _git_common_dir(cwd: Path) -> Path | None:
    return _git_resolved_path(cwd, "--git-common-dir")


def _git_toplevel(cwd: Path) -> Path | None:
    return _git_resolved_path(cwd, "--show-toplevel")


def _worktree_destination(cwd: Path, branch: str) -> tuple[Path | None, str]:
    """Build a private-state destination without following attacker symlinks."""
    common_dir = _git_common_dir(cwd)
    if common_dir is None:
        return None, "not a Git repository"

    state_dir = settings.STATE_DIR
    try:
        state_dir = settings._validated_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_root = state_dir.resolve(strict=True)
    except (OSError, ValueError) as exc:
        return None, f"could not prepare OmicsANG state directory: {exc}"
    if not state_root.is_dir():
        return None, "OmicsANG state path is not a directory"

    repo_key = hashlib.sha256(str(common_dir).encode("utf-8")).hexdigest()[:16]
    parent = state_root / "worktrees"
    for directory in (parent, parent / repo_key):
        if directory.is_symlink():
            return None, "worktree state path must not be a symlink"
        if directory.exists() and not directory.is_dir():
            return None, "worktree state path is not a directory"
        if not directory.exists():
            try:
                directory.mkdir(mode=0o700)
            except OSError as exc:
                return None, f"could not prepare worktree state: {exc}"
        if directory.is_symlink() or not directory.is_dir():
            return None, "worktree state path changed while it was prepared"

    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip(".-") or "branch"
    safe = safe[:80] + "-" + hashlib.sha256(branch.encode("utf-8")).hexdigest()[:12]
    return parent / repo_key / safe, ""


def _verify_existing_worktree(
    cwd: Path, candidate: Path, expected_branch: str
) -> tuple[bool, str]:
    """Verify path, repository identity, registration, and checked-out branch."""
    if candidate.is_symlink() or not candidate.is_dir():
        return False, "existing worktree path is not a regular directory"
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return False, "existing worktree path could not be resolved"

    intended_common = _git_common_dir(cwd)
    candidate_common = _git_common_dir(resolved)
    if intended_common is None or candidate_common != intended_common:
        return False, "existing path belongs to a different Git repository"
    if _git_toplevel(resolved) != resolved:
        return False, "existing path is not the root of a Git worktree"

    rc, stdout, _ = _run_capture(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], resolved
    )
    checked_out = stdout.strip()
    if rc != 0 or checked_out != expected_branch:
        return False, "existing worktree has an unexpected branch"

    registered = False
    for item in list_worktrees(cwd):
        raw_path = item.get("path")
        if not raw_path or item.get("branch") != expected_branch:
            continue
        try:
            listed_path = Path(raw_path).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if listed_path == resolved:
            registered = True
            break
    if not registered:
        return False, "existing path is not a registered worktree of this repository"
    return True, ""


def add_worktree(cwd: Path, branch: str, base: str = "HEAD") -> dict:
    """Create .benchtop-worktrees/<repo>/<branch> on a new branch off `base`.

    Idempotent: if that worktree already exists we reuse it rather than failing
    (which would otherwise tempt the caller into touching the main checkout).
    """
    valid_branch, branch_detail = validate_branch(cwd, branch)
    if not valid_branch:
        return {"ok": False, "output": branch_detail}
    valid_base, base_detail = validate_revision(cwd, base)
    if not valid_base:
        return {"ok": False, "output": base_detail}
    wt_path, destination_error = _worktree_destination(cwd, branch)
    if wt_path is None:
        return {"ok": False, "output": destination_error}
    if wt_path.exists() or wt_path.is_symlink():
        valid_existing, reason = _verify_existing_worktree(cwd, wt_path, branch)
        if not valid_existing:
            return {"ok": False, "path": str(wt_path), "output": reason}
        return {
            "ok": True,
            "path": str(wt_path),
            "branch": branch,
            "output": "reused existing worktree",
            "reused": True,
        }
    rc, out = _run(["git", "worktree", "add", "-b", branch, str(wt_path), base], cwd)
    if rc != 0:
        return {"ok": False, "path": str(wt_path), "branch": branch, "output": out}
    valid_created, reason = _verify_existing_worktree(cwd, wt_path, branch)
    return {
        "ok": valid_created,
        "path": str(wt_path),
        "branch": branch,
        "output": out if valid_created else reason,
        "reused": False,
    }


def worktree_for_branch(cwd: Path, branch: str) -> Path | None:
    valid, _ = validate_branch(cwd, branch)
    if not valid:
        return None
    for item in list_worktrees(cwd):
        if item.get("branch") == branch and item.get("path"):
            try:
                candidate = Path(item["path"]).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            valid_existing, _ = _verify_existing_worktree(cwd, candidate, branch)
            if valid_existing:
                return candidate
    return None


def remove_worktree(cwd: Path, path: str) -> dict:
    rc, out = _run(["git", "worktree", "remove", "--force", path], cwd)
    return {"ok": rc == 0, "output": out}
