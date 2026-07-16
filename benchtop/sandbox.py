# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Kernel-enforced OS containment for agent sessions.

OmicsANG already refuses to hand an agent the server's whole environment
(`sessions._child_environment`).  That boundary stops credential *inheritance*,
but it does nothing about the filesystem: an agent CLI launched as the operator's
OS account can still read every other pipeline on the machine, `~/.ssh`, and any
genomic or clinical data the account can reach.

This module closes that gap at the same choke point, using `bubblewrap` to build
an unprivileged mount namespace around the agent process:

  * the working repository (and, for a linked worktree, its parent git
    directory) is the only writable project path;
  * `$HOME` becomes a tmpfs, so credentials and dotfiles are *absent* rather
    than merely unreadable — only the selected provider's own configuration is
    bound back in;
  * sibling pipelines are never materialised into the namespace at all.

Two deliberate non-goals:

  * **Network egress is not restricted.**  Agent CLIs must reach their provider
    to function, and a namespace cannot distinguish `api.anthropic.com` from
    exfiltration.  Domain-level control belongs to each CLI's own sandbox
    configuration (see `docs/agent-sandbox.md`); this layer is about the
    filesystem.  The SECURITY.md note that provider tools may transmit
    repository content still holds in full.
  * **The provider's own credentials remain reachable**, because the CLI cannot
    authenticate without them.  Containment protects everything *else*.

The wrapper is applied outside the agent process, so unlike a CLI's own settings
file it cannot be edited or disabled by the agent it constrains.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

from .env_compat import environment_value

BWRAP = "bwrap"

# Modes for OMICSANG_AGENT_SANDBOX.
MODE_AUTO = "auto"  # contain when the kernel supports it; note when it does not
MODE_REQUIRE = "require"  # refuse to launch an agent that cannot be contained
MODE_OFF = "off"  # legacy behaviour: agent runs as the full OS account
MODES = (MODE_AUTO, MODE_REQUIRE, MODE_OFF)


class SandboxUnavailable(RuntimeError):
    """Raised when containment was required but the kernel could not provide it."""


def mode() -> str:
    raw = (
        str(environment_value("AGENT_SANDBOX", MODE_AUTO) or MODE_AUTO).strip().lower()
    )
    return raw if raw in MODES else MODE_AUTO


def _env_roots(suffix: str) -> tuple[Path, ...]:
    """Read an ``os.pathsep``-delimited list of extra roots to expose."""
    raw = environment_value(suffix)
    if not raw:
        return ()
    roots: list[Path] = []
    for value in raw.split(os.pathsep):
        if not value:
            continue
        try:
            resolved = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue  # a root that does not exist cannot be bound
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def read_roots() -> tuple[Path, ...]:
    """Extra read-only roots — reference genomes, shared annotation bundles."""
    return _env_roots("AGENT_SANDBOX_READ")


def write_roots() -> tuple[Path, ...]:
    """Extra writable roots — scratch or results volumes outside the repo."""
    return _env_roots("AGENT_SANDBOX_WRITE")


def _home() -> Path:
    try:
        return Path(os.path.expanduser("~")).resolve(strict=True)
    except (OSError, RuntimeError):
        return Path("/nonexistent")


def _tool_config_paths(tool: str) -> tuple[list[Path], list[Path]]:
    """Return (writable, read-only) paths for one provider's own configuration.

    Mirrors the per-tool split in `sessions._AGENT_PROVIDER_ENV`: a Claude
    session is not given Codex's credentials, and vice versa.  Config dirs are
    writable because both CLIs persist session and auth state as they run.
    """
    home = _home()
    writable: list[Path] = []
    readonly: list[Path] = []
    if tool == "claude":
        config = os.environ.get("CLAUDE_CONFIG_DIR")
        writable.append(Path(config).expanduser() if config else home / ".claude")
        writable.append(home / ".claude.json")
        # Versioned install payload; read-only so an agent cannot rewrite its
        # own binary and survive the next launch.
        readonly.append(home / ".local" / "share" / "claude")
    elif tool == "codex":
        config = os.environ.get("CODEX_HOME")
        writable.append(Path(config).expanduser() if config else home / ".codex")
    return writable, readonly


def _git_common_dir(cwd: Path) -> Path | None:
    """Resolve the real git directory backing `cwd`, if it is a linked worktree.

    A linked worktree's `.git` is a file pointing into the main repository's
    `.git/worktrees/<name>`.  Without that path bound, every git operation in
    the worktree fails — which is exactly how OmicsANG's fleet and worktree
    launches run agents.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


@functools.lru_cache(maxsize=1)
def available() -> bool:
    """Report whether this kernel can actually give us an unprivileged sandbox.

    Presence of the binary is not enough: several distributions ship bubblewrap
    while restricting unprivileged user namespaces (Ubuntu's
    `kernel.apparmor_restrict_unprivileged_userns`), so probe for real.  Cached:
    the answer is a property of the kernel, and every agent launch consults it.
    """
    if not shutil.which(BWRAP):
        return False
    try:
        probe = subprocess.run(
            [
                BWRAP,
                "--ro-bind",
                "/",
                "/",
                "--unshare-user",
                "--unshare-pid",
                "--die-with-parent",
                "true",
            ],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _bind(args: list[str], flag: str, path: Path) -> None:
    args.extend([flag, str(path), str(path)])


def _executable_paths(command: str) -> tuple[Path, ...]:
    """Resolve the launcher plus any symlink target that must stay reachable.

    `command` may be a bare name, so consult PATH the way exec would.  Both the
    launcher and its real target are returned: agent CLIs are commonly a symlink
    in `~/.local/bin` pointing at a versioned payload elsewhere, and binding only
    one of the two leaves a dangling link inside the namespace.
    """
    located = shutil.which(command)
    if not located:
        return ()
    launcher = Path(located)
    paths = [launcher]
    try:
        real = launcher.resolve(strict=True)
    except (OSError, RuntimeError):
        return tuple(paths)
    if real != launcher:
        paths.append(real)
    return tuple(paths)


def build_argv(argv: list[str], *, cwd: str, tool: str) -> list[str]:
    """Wrap `argv` so the agent sees only the repo plus its own provider config.

    Raises SandboxUnavailable if the kernel cannot provide containment; callers
    decide whether that is fatal (`require`) or a downgrade (`auto`).
    """
    if not argv:
        raise ValueError("cannot contain an empty command")
    if not available():
        raise SandboxUnavailable("bubblewrap cannot create an unprivileged sandbox")

    try:
        workdir = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SandboxUnavailable(
            f"agent working directory is unusable: {cwd!r}"
        ) from exc

    home = _home()
    args: list[str] = [BWRAP]

    # System runtime, read-only.  /etc carries the CA bundle the CLIs need.
    for system in ("/usr", "/etc", "/opt"):
        if Path(system).exists():
            _bind(args, "--ro-bind", Path(system))
    for link in ("/bin", "/sbin", "/lib", "/lib64"):
        if Path(link).exists():
            _bind(args, "--ro-bind", Path(link))

    # These are mount targets inside the agent's own namespace, not paths this
    # process ever opens: /tmp is given a private tmpfs precisely so the agent
    # cannot see or tamper with the host's shared temp directory.
    args.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])  # nosec B108

    # /run is tmpfs'd to hide other services' sockets, but systemd-resolved's
    # stub is bound back: /etc/resolv.conf symlinks into it, and without it the
    # agent cannot resolve its own provider's API.
    args.extend(["--tmpfs", "/run"])
    args.extend(["--ro-bind-try", "/run/systemd/resolve", "/run/systemd/resolve"])

    # $HOME as tmpfs: dotfiles and credentials are absent, not merely denied.
    args.extend(["--tmpfs", str(home)])

    writable, readonly = _tool_config_paths(tool)
    for path in readonly:
        if path.exists():
            _bind(args, "--ro-bind-try", path)
    for path in writable:
        if path.exists():
            _bind(args, "--bind-try", path)

    # The launcher and its resolved target, read-only.  Callers may pass a bare
    # command name, and $HOME is a tmpfs here: without an explicit bind, a CLI
    # installed under ~/.local/bin would simply vanish from the namespace.
    for path in _executable_paths(argv[0]):
        _bind(args, "--ro-bind-try", path)

    for root in read_roots():
        _bind(args, "--ro-bind-try", root)
    for root in write_roots():
        _bind(args, "--bind-try", root)

    # The project itself.  Bound last so it wins over any narrower root above.
    _bind(args, "--bind", workdir)
    git_dir = _git_common_dir(workdir)
    if git_dir and not _within(git_dir, workdir):
        _bind(args, "--bind", git_dir)

    args.extend(
        [
            "--unshare-user",
            "--unshare-ipc",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-cgroup-try",
            # Network is deliberately shared; see the module docstring.
            "--die-with-parent",
            "--chdir",
            str(workdir),
            "--",
        ]
    )
    return args + list(argv)


def _within(target: Path, root: Path) -> bool:
    return target == root or root in target.parents


def describe(cwd: str, tool: str) -> str:
    """One-line summary for the session scrollback, so the boundary is visible."""
    extra_read = len(read_roots())
    extra_write = len(write_roots())
    detail = f"repo {cwd} writable; $HOME hidden except {tool} config"
    if extra_read or extra_write:
        detail += f"; +{extra_read} read / +{extra_write} write roots"
    return f"OmicsANG: agent contained (bubblewrap) — {detail}; network unrestricted"
