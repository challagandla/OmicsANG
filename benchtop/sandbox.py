# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
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

  * within the provider's configuration directory, everything the CLI later
    *executes or loads as instructions* (settings, hooks, plugins, skills,
    commands, global memory) is read-only, other projects' transcripts and
    caches are replaced by empty scratch space, and the mixed state/MCP file
    is a private copy that is never written back;
  * unix-domain sockets cannot be created, so the desktop session's X11 and
    D-Bus sockets, which live in the shared network namespace and would accept
    this uid, are out of reach.

Two deliberate non-goals:

  * **Network egress is not restricted.**  Agent CLIs must reach their provider
    to function, and a namespace cannot distinguish `api.anthropic.com` from
    exfiltration.  Domain-level control belongs to each CLI's own sandbox
    configuration (see `docs/agent-sandbox.md`); this layer is about the
    filesystem.  The SECURITY.md note that provider tools may transmit
    repository content still holds in full.  The network namespace is shared,
    so TCP services listening on this machine's loopback are reachable too.
  * **The provider's own credentials remain reachable**, because the CLI cannot
    authenticate without them.  Containment protects everything *else*.

The wrapper is applied outside the agent process, so unlike a CLI's own settings
file it cannot be edited or disabled by the agent it constrains.
"""

from __future__ import annotations

import errno
import functools
import os
import platform
import re
import shutil
import stat
import struct
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
    # A value that is set but not understood (for example "required") was
    # meant to ask for something; refusing to run uncontained is the safe reading.
    return raw if raw in MODES else MODE_REQUIRE


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


def _private_dir() -> Path | None:
    """Private scratch space for per-launch copies and the socket filter."""
    from . import settings

    path = settings.STATE_DIR / "agent-sandbox"
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        return None
    return path


def _write_private(name: str, data: bytes) -> Path | None:
    directory = _private_dir()
    if directory is None:
        return None
    target = directory / name
    try:
        fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:
        return None
    return target


# What each CLI later executes, or loads as instructions, from its own config
# directory.  A contained agent that could write these would run code or steer
# the operator's next *uncontained* session, so they are mounted read-only.
_PROVIDER_READONLY = {
    "claude": (
        "settings.json",
        "settings.local.json",
        "CLAUDE.md",
        "keybindings.json",
        "hooks",
        "commands",
        "agents",
        "skills",
        "plugins",
        "output-styles",
        "rules",
        "local",
    ),
    "codex": (
        "config.toml",
        "AGENTS.md",
        "hooks",
        "plugins",
        "packages",
        "prompts",
        "skills",
        "rules",
    ),
}
# A name that does not exist yet cannot be mounted read-only, and the agent
# could simply create it.  These inert placeholders close that gap; an empty
# file or directory changes nothing about how the CLI behaves.
_PROVIDER_PLACEHOLDER_FILES = {
    "claude": {"settings.json": "{}\n", "CLAUDE.md": ""},
    "codex": {"config.toml": "", "AGENTS.md": ""},
}
_PROVIDER_PLACEHOLDER_DIRS = {
    "claude": ("plugins", "skills", "commands", "agents", "rules", "output-styles"),
    "codex": ("plugins", "prompts", "skills"),
}
# Mixed files: live state the CLI rewrites constantly, plus settings (MCP server
# commands) that would execute outside the sandbox.  The agent gets a private
# copy that is refreshed from the real file at every launch and never copied back.
_PROVIDER_PRIVATE_COPIES = {"claude": (".claude.json",), "codex": ()}
# Cross-project prompt history; replaced by an empty private file.
_PROVIDER_SCRATCH_FILES = ("history.jsonl",)


def _provider_config_dir(tool: str) -> Path | None:
    home = _home()
    if tool == "claude":
        config = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(config).expanduser() if config else home / ".claude"
    if tool == "codex":
        config = os.environ.get("CODEX_HOME")
        return Path(config).expanduser() if config else home / ".codex"
    return None


def _ensure_placeholders(tool: str, config: Path) -> None:
    for name, content in _PROVIDER_PLACEHOLDER_FILES.get(tool, {}).items():
        try:
            fd = os.open(
                config / name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            continue
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
    for name in _PROVIDER_PLACEHOLDER_DIRS.get(tool, ()):
        try:
            os.mkdir(config / name, 0o700)
        except OSError:
            continue


def _claude_project_dir(config: Path, workdir: Path) -> Path | None:
    """The one transcript directory this session may see and resume from."""
    projects = config / "projects"
    target = projects / re.sub(r"[^A-Za-z0-9]", "-", str(workdir))
    try:
        if projects.is_symlink():
            return None
        projects.mkdir(mode=0o700, exist_ok=True)
        if target.is_symlink():
            return None
        target.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        return None
    return target


def _private_copy_bind(tool: str, source: Path) -> list[str]:
    """Bind a throwaway copy of `source` over itself; read-only if that fails."""
    try:
        data = source.read_bytes() if source.stat().st_size <= 64 << 20 else None
    except OSError:
        data = None
    copy = _write_private(f"{tool}{source.name}", data) if data is not None else None
    if copy is None:
        return ["--ro-bind", str(source), str(source)]
    return ["--bind", str(copy), str(source)]


def _provider_config_args(tool: str, workdir: Path) -> list[str]:
    """Mount one provider's configuration with only its live state writable.

    Mirrors the per-tool split in `sessions._AGENT_PROVIDER_ENV`: a Claude
    session is not given Codex's credentials, and vice versa.  The directory
    itself stays writable because both CLIs replace their credential file as
    tokens refresh; everything else is narrowed on top of that bind.
    """
    args: list[str] = []
    home = _home()
    if tool == "claude":
        # Versioned install payload; read-only so an agent cannot rewrite its
        # own binary and survive the next launch.
        payload = home / ".local" / "share" / "claude"
        if payload.exists():
            _bind(args, "--ro-bind-try", payload)
    config = _provider_config_dir(tool)
    if config is None:
        return args
    try:
        config = config.resolve(strict=True)
    except (OSError, RuntimeError):
        config = None
    private_copies = _PROVIDER_PRIVATE_COPIES.get(tool, ())
    if config is not None and config.is_dir():
        _bind(args, "--bind", config)
        _ensure_placeholders(tool, config)
        readonly = set(_PROVIDER_READONLY.get(tool, ()))
        scratch = _write_private(f"{tool}-history.jsonl", b"")
        try:
            entries = sorted(os.scandir(config), key=lambda entry: entry.name)
        except OSError:
            entries = []
        for entry in entries:
            path = config / entry.name
            if entry.is_symlink():
                # A mount cannot be pinned onto a symlink; see docs/agent-sandbox.md.
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if entry.name in readonly or (
                tool == "codex" and not is_dir and ".toml" in entry.name
            ):
                _bind(args, "--ro-bind", path)
            elif entry.name in private_copies and not is_dir:
                args.extend(_private_copy_bind(tool, path))
            elif entry.name in _PROVIDER_SCRATCH_FILES and not is_dir and scratch:
                args.extend(["--bind", str(scratch), str(path)])
            elif is_dir:
                # Caches, logs, IDE and daemon control sockets, and every other
                # project's transcripts: present but empty and never persisted.
                args.extend(["--tmpfs", str(path)])
        if tool == "claude":
            project = _claude_project_dir(config, workdir)
            if project is not None:
                _bind(args, "--bind", project)
    if tool == "claude" and not os.environ.get("CLAUDE_CONFIG_DIR"):
        for name in private_copies:
            source = home / name
            if source.is_file() and not source.is_symlink():
                args.extend(_private_copy_bind(tool, source))
    return args


# --- unix-domain socket filter ---------------------------------------------
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
_SYSCALL_SOCKET = {"x86_64": 41, "aarch64": 198}
_SYSCALL_IO_URING_SETUP = 425
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_SOCKET_FILTER_FD = "9"


def unix_sockets_allowed() -> bool:
    raw = str(environment_value("AGENT_SANDBOX_UNIX_SOCKETS", "") or "")
    return raw.strip().lower() == "allow"


def _socket_filter_program(machine: str | None = None) -> bytes | None:
    """Classic-BPF seccomp program that refuses ``socket(AF_UNIX, ...)``.

    Abstract unix sockets belong to the network namespace, which stays shared
    so the agent can reach its provider.  Desktop X11 and D-Bus listeners there
    authenticate by peer uid, and the sandbox keeps the operator's uid, so the
    only way to keep them out of reach is to deny the socket family.
    ``socketpair`` is untouched; ``io_uring`` is refused because it can create
    sockets without the ``socket`` system call.
    """
    machine = machine or platform.machine()
    if machine not in _AUDIT_ARCH:
        return None
    ld_abs, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    deny = _SECCOMP_RET_ERRNO | errno.EPERM
    unsupported = _SECCOMP_RET_ERRNO | errno.ENOSYS
    program = [
        (ld_abs, 0, 0, 4),  # seccomp_data.arch
        (jeq, 1, 0, _AUDIT_ARCH[machine]),
        (ret, 0, 0, deny),  # foreign ABI: its socket calls are numbered differently
        (ld_abs, 0, 0, 0),  # seccomp_data.nr
    ]
    if machine == "x86_64":
        program += [(jge, 0, 1, 0x40000000), (ret, 0, 0, unsupported)]  # x32 ABI
    program += [
        (jeq, 0, 1, _SYSCALL_IO_URING_SETUP),
        (ret, 0, 0, unsupported),
        (jeq, 0, 3, _SYSCALL_SOCKET[machine]),
        (ld_abs, 0, 0, 16),  # low word of args[0]: the address family
        (jeq, 0, 1, 1),  # AF_UNIX
        (ret, 0, 0, deny),
        (ret, 0, 0, _SECCOMP_RET_ALLOW),
    ]
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in program)


def _socket_filter_path() -> Path | None:
    if unix_sockets_allowed():
        return None
    program = _socket_filter_program()
    if program is None:
        return None
    return _write_private("deny-unix-sockets.bpf", program)


def _git_common_dir(cwd: Path) -> Path | None:
    """Return the main repository's git directory for a genuine linked worktree.

    A linked worktree's `.git` is a file pointing into the main repository's
    `.git/worktrees/<name>`.  Without that path bound, every git operation in
    the worktree fails — which is exactly how OmicsANG's fleet and worktree
    launches run agents.

    The working directory is writable by earlier agent sessions, so nothing in
    it may choose what gets bound.  The pointer is therefore read without
    running git, and accepted only when the main repository itself registers
    this directory as one of its worktrees: an agent cannot write that
    registration into a repository it was never given.
    """
    pointer = cwd / ".git"
    try:
        if not stat.S_ISREG(os.lstat(pointer).st_mode):
            # A real directory is a main checkout (already inside the bind); a
            # symlink is never followed.
            return None
        with open(pointer, encoding="utf-8") as handle:
            text = handle.read(4096)
    except (OSError, UnicodeDecodeError):
        return None
    first = text.splitlines()[0] if text else ""
    if not first.startswith("gitdir:"):
        return None
    admin = Path(first[len("gitdir:") :].strip())
    if not admin.is_absolute():
        admin = cwd / admin
    try:
        admin = admin.resolve(strict=True)
        common = admin.parent.parent
        if admin.parent.name != "worktrees" or common.name != ".git":
            return None
        if not stat.S_ISDIR(os.lstat(common).st_mode):
            return None
        registered = (admin / "gitdir").read_text(encoding="utf-8").strip()
        if Path(registered).resolve(strict=True) != pointer.resolve(strict=True):
            return None
        shared = (admin / "commondir").read_text(encoding="utf-8").strip()
        if (admin / shared).resolve(strict=True) != common:
            return None
    except (OSError, RuntimeError, UnicodeDecodeError):
        return None
    return common


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

    args.extend(_provider_config_args(tool, workdir))

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
        ]
    )
    socket_filter = _socket_filter_path()
    if socket_filter is not None:
        args.extend(["--seccomp", _SOCKET_FILTER_FD])
    wrapped = args + ["--", *argv]
    if socket_filter is None:
        return wrapped
    # bubblewrap takes the compiled filter on an inherited descriptor, so a
    # fixed shell line opens it and then replaces itself with bubblewrap.
    opener = f'exec {_SOCKET_FILTER_FD}<"$0" && exec "$@"'
    return ["/bin/sh", "-c", opener, str(socket_filter), *wrapped]


def _within(target: Path, root: Path) -> bool:
    return target == root or root in target.parents


def describe(cwd: str, tool: str) -> str:
    """One-line summary for the session scrollback, so the boundary is visible."""
    extra_read = len(read_roots())
    extra_write = len(write_roots())
    detail = (
        f"repo {cwd} writable; $HOME hidden except {tool} state "
        "(its settings, hooks and plugins are read-only)"
    )
    if extra_read or extra_write:
        detail += f"; +{extra_read} read / +{extra_write} write roots"
    sockets = "allowed" if unix_sockets_allowed() else "blocked"
    return (
        f"OmicsANG: agent contained (bubblewrap) — {detail}; "
        f"unix sockets {sockets}; network unrestricted"
    )
