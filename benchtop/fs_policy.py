# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Central filesystem authorization for browser-supplied relative paths."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path, PurePosixPath

MAX_RELATIVE_PATH = 1024
MAX_COMPONENT = 255
ALLOWED_DOT_NAMES = {".dockerignore", ".gitignore", ".github", ".test"}
FORBIDDEN_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".benchtop",
    ".benchtop-dev",
    ".claude",
    ".codex",
    ".cursor",
    ".continue",
    ".windsurf",
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".config",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "graphify-out",
    "dist",
    "build",
}
FORBIDDEN_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "authorized_keys",
}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".keystore"}
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
ENCODED_SEPARATOR_RE = re.compile(r"%(?:00|2e|2f|5c)", re.IGNORECASE)


class PathPolicyError(ValueError):
    """Raised when a browser-supplied path crosses the repository policy."""


def _lexical_absolute(path: Path) -> Path:
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise PathPolicyError("registered root contains parent traversal")
    return Path(os.path.abspath(os.fspath(expanded)))


def _reject_symlink_components(path: Path) -> Path:
    target = _lexical_absolute(path)
    current = Path(target.anchor)
    for part in target.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PathPolicyError("path is inaccessible") from exc
        if stat.S_ISLNK(info.st_mode):
            raise PathPolicyError("symlink traversal is not accepted")
    return target


def clean_relative_path(raw: str) -> str:
    value = str(raw or "")
    if not value or value != value.strip():
        raise PathPolicyError("a non-empty normalized relative path is required")
    if len(value.encode("utf-8", errors="ignore")) > MAX_RELATIVE_PATH:
        raise PathPolicyError("path is too long")
    if (
        CONTROL_RE.search(value)
        or "\\" in value
        or WINDOWS_DRIVE_RE.match(value)
        or ENCODED_SEPARATOR_RE.search(value)
    ):
        raise PathPolicyError("path contains forbidden characters")
    posix = PurePosixPath(value)
    if posix.is_absolute() or value.startswith(("/", "~")):
        raise PathPolicyError("absolute paths are not accepted")
    if not posix.parts or any(part in {"", ".", ".."} for part in posix.parts):
        raise PathPolicyError("path traversal is not accepted")
    for part in posix.parts:
        if len(part.encode("utf-8", errors="ignore")) > MAX_COMPONENT:
            raise PathPolicyError("path component is too long")
        lower = part.casefold()
        if lower in FORBIDDEN_PARTS or lower in FORBIDDEN_NAMES:
            raise PathPolicyError("path is protected")
        if lower.startswith(".env.") or Path(lower).suffix in FORBIDDEN_SUFFIXES:
            raise PathPolicyError("credential-like paths are protected")
        if part.startswith(".") and part not in ALLOWED_DOT_NAMES:
            raise PathPolicyError("hidden paths are protected")
    return posix.as_posix()


def _inside(root: Path, target: Path) -> bool:
    return target == root or root in target.parents


def resolve_root(base: Path) -> Path:
    """Resolve a registered directory root without accepting symlink traversal."""
    lexical_base = _reject_symlink_components(base)
    try:
        root = lexical_base.resolve(strict=True)
        info = lexical_base.lstat()
    except (OSError, RuntimeError) as exc:
        raise PathPolicyError("registered root is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PathPolicyError("registered root is not a regular directory")
    return root


def resolve_relative(
    base: Path,
    raw: str,
    *,
    must_exist: bool | None = None,
    expected: str = "any",
    reject_hardlinks: bool = True,
) -> tuple[str, Path]:
    """Resolve a strict relative path without following any existing symlink."""
    rel = clean_relative_path(raw)
    root = resolve_root(base)

    current = root
    parts = PurePosixPath(rel).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if index < len(parts) - 1:
                # Missing descendants are permitted for create operations; their
                # first existing ancestor was already checked above.
                break
            continue
        except OSError as exc:
            raise PathPolicyError("path is inaccessible") from exc
        if stat.S_ISLNK(info.st_mode):
            raise PathPolicyError("symlink traversal is not accepted")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise PathPolicyError("a parent path is not a directory")

    try:
        target = (root / rel).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathPolicyError("path cannot be resolved") from exc
    if not _inside(root, target):
        raise PathPolicyError("path escapes the registered root")
    exists = target.exists()
    if must_exist is True and not exists:
        raise FileNotFoundError("path not found")
    if must_exist is False and exists:
        raise FileExistsError("path already exists")
    if exists:
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PathPolicyError("symlink traversal is not accepted")
        if expected == "file" and not stat.S_ISREG(info.st_mode):
            raise PathPolicyError("path is not a regular file")
        if expected == "directory" and not stat.S_ISDIR(info.st_mode):
            raise PathPolicyError("path is not a directory")
        if expected == "any" and not (
            stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
        ):
            raise PathPolicyError("special filesystem objects are not accepted")
        if reject_hardlinks and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise PathPolicyError("hard-linked files are not accepted")
    return rel, target


def atomic_write_text(
    target: Path,
    content: str,
    *,
    mode: int | None = None,
) -> None:
    """Replace UTF-8 text, preserving safe source modes or using ``0644`` new."""
    target = _lexical_absolute(target)
    _reject_symlink_components(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(target.parent)
    try:
        info = target.lstat()
    except FileNotFoundError:
        write_mode = 0o644 if mode is None else mode
    except OSError as exc:
        raise PathPolicyError("write target is inaccessible") from exc
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise PathPolicyError("write target is not an ordinary file")
        write_mode = stat.S_IMODE(info.st_mode) if mode is None else mode
    if write_mode < 0 or write_mode > 0o777:
        raise ValueError("file mode must contain only ordinary permission bits")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, write_mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def private_backup(root: Path, target: Path, *, namespace: str) -> str | None:
    """Copy a regular source file into private application state, never beside it."""
    if not target.exists():
        return None
    lexical_root = _reject_symlink_components(root)
    lexical_target = _reject_symlink_components(target)
    resolved_root = lexical_root.resolve(strict=True)
    resolved_target = lexical_target.resolve(strict=True)
    if not _inside(resolved_root, resolved_target):
        raise PathPolicyError("backup target escapes the registered root")
    info = resolved_target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise PathPolicyError("only ordinary non-hard-linked files can be backed up")

    from . import settings

    settings.ensure_dirs()
    root_key = hashlib.sha256(str(resolved_root).encode("utf-8")).hexdigest()[:16]
    rel = resolved_target.relative_to(resolved_root).as_posix()
    rel_key = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]
    stamp = f"{time.time_ns()}-{rel_key}-{resolved_target.name}"
    backup_dir = settings.STATE_DIR
    for part in ("backups", namespace, root_key):
        backup_dir = backup_dir / part
        try:
            info = backup_dir.lstat()
        except FileNotFoundError:
            backup_dir.mkdir(mode=0o700)
            info = backup_dir.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise PathPolicyError("private backup path is not a regular directory")
        backup_dir.chmod(0o700)
    destination = backup_dir / stamp
    with resolved_target.open("rb") as source:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as sink:
            shutil.copyfileobj(source, sink)
    destination.chmod(0o600)
    return f"{namespace}/{root_key}/{stamp}"
