# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Thin git / worktree / gh helpers used by the agent control center."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
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


# Repository-level configuration is attacker-influenced: a pipeline checkout may
# arrive with a prepared ``.git/config``, and a contained agent can rewrite the
# one in its working repository.  Git executes several configured values as
# commands even for read-only operations such as ``git status``, so every
# host-side invocation neutralizes the repository-scoped settings below while
# leaving the user's own global and system configuration in effect.
_EXECUTABLE_CONFIG_RE = re.compile(
    r"^(?:"
    r"core\.(?:fsmonitor|sshcommand|hookspath|pager|editor|gitproxy|askpass"
    r"|alternaterefscommand)"
    r"|filter\..*\.(?:clean|smudge|process)"
    r"|diff\.external|diff\..*\.(?:textconv|command)"
    r"|merge\..*\.driver"
    r"|credential\.helper|credential\..*\.helper"
    r"|remote\..*\.(?:uploadpack|receivepack|vcs)"
    r"|protocol\..*|alias\..*|pager\..*|gpg\..*|sequence\.editor"
    r"|uploadpack\.packobjectshook|submodule\..*\.update"
    r"|interactive\.difffilter"
    r")$",
    re.S,
)
_ALWAYS_CONFIG = (
    # Hooks live in the repository's git directory, not its configuration.
    ("core.hooksPath", os.devnull),
    ("core.fsmonitor", ""),
    ("protocol.ext.allow", "never"),
)
_REPOSITORY_SCOPES = {"local", "worktree"}
_MIN_ENV_CONFIG_VERSION = (2, 31)
_config_cache: dict[str, tuple[tuple, tuple[tuple[str, str], ...] | None]] = {}


@functools.lru_cache(maxsize=1)
def _git_version() -> tuple[int, ...]:
    try:
        out = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else ()


def _normalized_config_key(key: str) -> str:
    """Lower-case the section and variable name; subsections are case-sensitive."""
    first, _, rest = key.partition(".")
    middle, dot, last = rest.rpartition(".")
    if not dot:
        return f"{first.lower()}.{rest.lower()}"
    return f"{first.lower()}.{middle}.{last.lower()}"


def _config_signature(cwd: Path) -> tuple | None:
    """Cheap identity for a main checkout's config; None means do not cache."""
    try:
        info = os.lstat(cwd / ".git" / "config")
    except OSError:
        return None
    # ctime cannot be set by the file's owner, unlike mtime.  A config changed
    # within the timestamp granularity of the scan is never cached.
    if time.time() - info.st_ctime < 2.0:
        return None
    return (info.st_ino, info.st_ctime_ns, info.st_mtime_ns, info.st_size)


def _scan_repository_config(cwd: Path) -> tuple[tuple[str, str], ...] | None:
    """Return overrides that neutralize executable repository-scoped settings.

    None means the configuration could not be inspected and nothing should run.
    """
    try:
        proc = subprocess.run(
            ["git", "config", "--list", "--show-scope", "--includes", "-z"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        # Outside a repository there is no repository configuration to distrust.
        return ()
    fields = proc.stdout.split("\0")
    entries: list[tuple[str, str, str]] = []
    for scope, item in zip(fields[0::2], fields[1::2]):
        key, _, value = item.partition("\n")
        entries.append((scope, _normalized_config_key(key), value))
    hostile = []
    for scope, key, _ in entries:
        if scope in _REPOSITORY_SCOPES and _EXECUTABLE_CONFIG_RE.match(key):
            if key not in hostile:
                hostile.append(key)
    overrides: list[tuple[str, str]] = []
    for key in hostile:
        # An empty value disables the setting and resets multi-valued lists;
        # then the user's own values for that key are restored in order.
        overrides.append((key, ""))
        for scope, other, value in entries:
            if other == key and scope not in _REPOSITORY_SCOPES and value:
                overrides.append((key, value))
    return tuple(overrides)


def _repository_overrides(cwd: Path) -> tuple[tuple[str, str], ...] | None:
    signature = _config_signature(cwd)
    key = str(cwd)
    cached = _config_cache.get(key)
    # Includes can change without touching .git/config, so only a config with
    # no executable or include-driven surprises is served from the cache.
    if signature is not None and cached is not None and cached[0] == signature:
        return cached[1]
    overrides = _scan_repository_config(cwd)
    if signature is not None and overrides == () and not _has_includes(cwd):
        if len(_config_cache) > 256:
            _config_cache.clear()
        _config_cache[key] = (signature, overrides)
    else:
        _config_cache.pop(key, None)
    return overrides


def _has_includes(cwd: Path) -> bool:
    try:
        with open(cwd / ".git" / "config", "rb") as handle:
            head = handle.read(1 << 20).lower()
    except OSError:
        return True
    return b"[include" in head or b"worktreeconfig" in head


def hardened_git_env(
    cwd: Path, base: dict[str, str] | None = None
) -> dict[str, str] | None:
    """Environment for running git in an untrusted checkout, or None to refuse."""
    overrides = _repository_overrides(cwd)
    if overrides is None:
        return None
    if overrides and _git_version() < _MIN_ENV_CONFIG_VERSION:
        # Older git cannot take exact-key overrides from the environment.
        return None
    env = dict(os.environ if base is None else base)
    pairs = list(_ALWAYS_CONFIG) + list(overrides)
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    except ValueError:
        count = 0
    for key, value in pairs:
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    return env


def _run_capture(args: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str, str]:
    env = None
    if args and args[0] == "git":
        env = hardened_git_env(cwd)
        if env is None:
            return 1, "", "repository git configuration could not be verified"
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
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


_NOT_A_REPOSITORY = {
    "branch": None,
    "remote": None,
    "upstream": None,
    "dirty": 0,
    "dirty_files": [],
    "ahead": None,
    "behind": None,
    "is_repo": False,
}


def _short_status_path(path: str) -> str:
    # Porcelain v1 additionally quotes names containing a space.
    if " " in path and not path.startswith('"'):
        return f'"{path}"'
    return path


def _short_status_line(line: str) -> str | None:
    """Render one porcelain-v2 entry the way ``git status --porcelain`` does."""
    kind = line[:1]
    if kind in {"?", "!"}:
        return f"{kind}{kind} {_short_status_path(line[2:])}"
    if kind == "1":
        parts = line.split(" ", 8)
        if len(parts) == 9:
            return f"{parts[1].replace('.', ' ')} {_short_status_path(parts[8])}"
    elif kind == "2":
        parts = line.split(" ", 9)
        if len(parts) == 10:
            path, _, original = parts[9].partition("\t")
            return (
                f"{parts[1].replace('.', ' ')} "
                f"{_short_status_path(original)} -> {_short_status_path(path)}"
            )
    elif kind == "u":
        parts = line.split(" ", 10)
        if len(parts) == 11:
            return f"{parts[1]} {_short_status_path(parts[10])}"
    return None


def status(cwd: Path) -> dict:
    # One porcelain-v2 call reports the branch, upstream, divergence, and the
    # working-tree entries that previously took six separate git processes.
    rc, stdout, _ = _run_capture(["git", "status", "--porcelain=v2", "--branch"], cwd)
    if rc != 0:
        return dict(_NOT_A_REPOSITORY, dirty_files=[])
    branch = upstream = ahead = behind = None
    dirty_lines: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("# "):
            name, _, value = line[2:].partition(" ")
            if name == "branch.head" and value != "(detached)":
                branch = value or None
            elif name == "branch.upstream":
                upstream = value or None
            elif name == "branch.ab":
                counts = value.split()
                if (
                    len(counts) == 2
                    and counts[0][:1] == "+"
                    and counts[1][:1] == "-"
                    and counts[0][1:].isdigit()
                    and counts[1][1:].isdigit()
                ):
                    ahead, behind = counts[0][1:], counts[1][1:]
            continue
        rendered = _short_status_line(line)
        if rendered is not None:
            dirty_lines.append(rendered)
    if ahead is None:
        # Git omits branch.ab when the configured upstream no longer exists.
        upstream = None
    rc_r, remote = _run(["git", "remote", "get-url", "origin"], cwd)
    return {
        "branch": branch,
        "remote": _redact_remote(remote) if rc_r == 0 else None,
        "upstream": upstream,
        "dirty": len(dirty_lines),
        "dirty_files": dirty_lines[:50],
        "ahead": ahead,
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
    rc, out = _run(
        ["git", "fetch", "--upload-pack=git-upload-pack", "--all", "--prune"],
        cwd,
        timeout=120,
    )
    return {"ok": rc == 0, "output": out}


def pull_ff_only(cwd: Path) -> dict:
    rc, out = _run(
        ["git", "pull", "--upload-pack=git-upload-pack", "--ff-only"],
        cwd,
        timeout=120,
    )
    return {"ok": rc == 0, "output": out}


def diff(cwd: Path, staged: bool = False) -> str:
    args = ["git", "diff", "--no-ext-diff", "--no-textconv"] + (
        ["--staged"] if staged else []
    )
    _, out = _run(args, cwd)
    return out


def diffstat(cwd: Path) -> dict:
    """Working-tree change summary (tracked + untracked)."""
    _, porcelain = _run(["git", "status", "--porcelain"], cwd)
    files = [line for line in porcelain.splitlines() if line.strip()]
    _, stat = _run(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--shortstat", "HEAD"], cwd
    )
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
    rc, out = _run(
        [
            "git",
            "push",
            "--receive-pack=git-receive-pack",
            "-u",
            "origin",
            "--",
            branch,
        ],
        cwd,
    )
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
    rc, out = _run(["git", "worktree", "remove", "--force", "--", path], cwd)
    return {"ok": rc == 0, "output": out}
