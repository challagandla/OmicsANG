# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Runtime configuration for OmicsANG.

Everything is overridable via environment variables so the IDE can be pointed at
any directory of pipelines, not just this one.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path

from .env_compat import environment_value

_PKG_DIR = Path(__file__).resolve().parent


def _lexical_absolute(path: Path) -> Path:
    """Return an absolute path without dereferencing any filesystem links."""
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise ValueError("state and root paths may not contain parent traversal")
    return Path(os.path.abspath(os.fspath(expanded)))


_DEFAULT_ROOT = _lexical_absolute(Path.cwd())


def _default_state_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    base = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "state"
    )
    current = _lexical_absolute(base / "omicsang")
    legacy = _lexical_absolute(base / "benchtop")

    def entry_exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ValueError(
                "default state location cannot be inspected safely"
            ) from exc
        return True

    current_exists = entry_exists(current)
    legacy_exists = entry_exists(legacy)
    if current_exists and legacy_exists:
        raise ValueError(
            "both OmicsANG and pre-rename state directories exist; "
            "choose one explicitly with OMICSANG_STATE or --state"
        )
    if legacy_exists:
        return legacy
    return current


def _env_path(suffix: str, default: Path) -> Path:
    val = environment_value(suffix)
    return _lexical_absolute(Path(val)) if val else default


def _env_paths(suffix: str, default: tuple[Path, ...]) -> tuple[Path, ...]:
    """Read an ``os.pathsep``-delimited list of absolute filesystem roots."""
    raw = environment_value(suffix)
    if raw is None:
        values = default
    else:
        values = tuple(
            Path(value).expanduser() for value in raw.split(os.pathsep) if value
        )
    seen: set[Path] = set()
    paths: list[Path] = []
    for value in values:
        resolved = _lexical_absolute(value)
        if resolved not in seen:
            seen.add(resolved)
            paths.append(resolved)
    return tuple(paths)


# Root directory that holds all pipeline repos.
ROOT: Path = _env_path("ROOT", _DEFAULT_ROOT)

# Additional result roots must be opted into explicitly.  The default remains
# inside the registered pipeline root instead of granting access to its parent.
RESULTS_ROOTS: tuple[Path, ...] = _env_paths("RESULTS_ROOTS", (ROOT,))


def _configured_state_dir() -> Path:
    """Resolve explicit state first, then perform safe default discovery."""
    configured = environment_value("STATE")
    return _lexical_absolute(Path(configured)) if configured else _default_state_dir()


# Where OmicsANG keeps run logs + persisted state. Resolve the explicit setting
# before inspecting defaults so --state remains an unambiguous escape hatch when
# both old and new default directories exist.
STATE_DIR: Path = _configured_state_dir()
RUN_LOG_DIR: Path = STATE_DIR / "sessions"
STATE_FILE: Path = STATE_DIR / "state.json"

HOST: str = str(environment_value("HOST", "127.0.0.1"))
PORT: int = int(str(environment_value("PORT", "8787")))
LOG_RETENTION_DAYS: int = max(
    1, int(str(environment_value("LOG_RETENTION_DAYS", "30")))
)


def _env_int(suffix: str, default: int) -> int:
    raw = environment_value(suffix, str(default))
    try:
        return max(1, int(str(raw)))
    except ValueError:
        return default


# Local core budget used by the run scheduler before any UI override is saved.
MAX_LOCAL_CORES: int = _env_int("MAX_LOCAL_CORES", os.cpu_count() or 8)

# Directories under ROOT that are never treated as pipelines.
EXCLUDE_DIRS = {
    "omicsang",
    "benchtop",
    ".git",
    ".github",
    ".claude",
    ".benchtop",
    "__pycache__",
    ".idea",
    ".vscode",
    "node_modules",
}

# Agent CLIs. Keys are the tool ids surfaced in the UI.
AGENT_TOOLS = {
    "claude": {"label": "Claude Code", "exe": "claude"},
    "codex": {"label": "Codex", "exe": "codex"},
    "shell": {"label": "Shell", "exe": os.environ.get("SHELL", "bash")},
}


def _inside(root: Path, target: Path) -> bool:
    return target == root or root in target.parents


def canonical_pipeline_root() -> Path:
    """Return the canonical directory identity used to bind durable state.

    A state database must never be silently shared by two pipeline roots.  Use
    a strict resolution so a missing or non-directory root cannot acquire (or
    reuse) a durable-state binding.
    """
    try:
        root = _lexical_absolute(ROOT).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("pipeline root cannot be resolved safely") from exc
    if not root.is_dir():
        raise ValueError("pipeline root is not a directory")
    return root


def _reject_symlink_components(path: Path) -> Path:
    """Validate every existing component without following symlinks."""
    target = _lexical_absolute(path)
    current = Path(target.anchor)
    for part in target.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(
                f"state path component is inaccessible: {current}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"refusing a symlinked state path component: {current}")
    return target


def _validated_state_dir() -> Path:
    target = _reject_symlink_components(STATE_DIR)
    pipeline_root = _lexical_absolute(ROOT)
    try:
        canonical_target = target.resolve(strict=False)
        canonical_root = pipeline_root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("state or pipeline root cannot be resolved safely") from exc
    if _inside(canonical_root, canonical_target) or _inside(
        canonical_target, canonical_root
    ):
        raise ValueError("OmicsANG state must be separate from the pipeline root")
    return target


def _state_child(path: Path, state_dir: Path, *, label: str) -> Path:
    child = _reject_symlink_components(path)
    if not _inside(state_dir, child) or child == state_dir:
        raise ValueError(f"{label} must be inside the OmicsANG state directory")
    return child


def _secure_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"state directory is unavailable: {path}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"state path is not an ordinary directory: {path}")
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
    except (NotImplementedError, OSError):
        pass


def ensure_dirs() -> None:
    state_dir = _validated_state_dir()
    run_log_dir = _state_child(RUN_LOG_DIR, state_dir, label="run log directory")
    state_file = _state_child(STATE_FILE, state_dir, label="state file")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_components(state_dir)
    _secure_directory(state_dir)
    run_log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_components(run_log_dir)
    _secure_directory(run_log_dir)
    try:
        info = state_file.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError("state file is inaccessible") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise ValueError("state file is not an ordinary private file")
    try:
        os.chmod(state_file, 0o600, follow_symlinks=False)
    except (NotImplementedError, OSError):
        pass


def validate_bind_host(host: str) -> str:
    """Refuse every non-loopback bind address; there is no remote override."""
    value = str(host or "").strip()
    if value.lower() == "localhost":
        return "localhost"
    if value in {"127.0.0.1", "::1"}:
        return value
    raise ValueError("OmicsANG only binds to localhost, 127.0.0.1, or ::1")


def prune_old_logs(now: float | None = None) -> int:
    """Delete only regular expired session logs from OmicsANG's private state."""
    ensure_dirs()
    cutoff = float(now or time.time()) - LOG_RETENTION_DAYS * 86400
    removed = 0
    try:
        candidates = list(RUN_LOG_DIR.iterdir())
    except OSError:
        return 0
    for path in candidates:
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def clear_state(*, confirmed: bool = False) -> None:
    """Remove only the configured OmicsANG state directory after explicit consent."""
    if not confirmed:
        raise ValueError("state deletion requires explicit confirmation")
    target = _validated_state_dir()
    forbidden = {
        _lexical_absolute(Path("/")),
        _lexical_absolute(Path.home()),
        _lexical_absolute(ROOT),
    }
    if target in forbidden or len(target.parts) < 3:
        raise ValueError("refusing to remove an unsafe state path")
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError("state directory is inaccessible") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("refusing to remove a non-directory state path")
    shutil.rmtree(target)


def harden_existing_state_permissions() -> None:
    """One-time startup repair for files created before private modes were enforced."""
    ensure_dirs()
    for root in (RUN_LOG_DIR, STATE_DIR / "slurm", STATE_DIR / "capsules"):
        try:
            info = root.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        try:
            os.chmod(root, 0o700, follow_symlinks=False)
        except (NotImplementedError, OSError):
            continue
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                path = Path(entry.path)
                try:
                    item = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(item.st_mode):
                    try:
                        os.chmod(path, 0o700, follow_symlinks=False)
                    except (NotImplementedError, OSError):
                        continue
                    pending.append(path)
                elif stat.S_ISREG(item.st_mode) and item.st_nlink == 1:
                    try:
                        os.chmod(path, 0o600, follow_symlinks=False)
                    except (NotImplementedError, OSError):
                        continue
