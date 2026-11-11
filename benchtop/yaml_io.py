# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Bounded reads and safe YAML loading for repository-controlled files."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any

import yaml

try:  # libyaml parses the same safe schema several times faster
    from yaml import CSafeLoader as _DisplayLoader
except ImportError:  # pragma: no cover - depends on how PyYAML was built
    from yaml import SafeLoader as _DisplayLoader  # type: ignore[assignment]

# Pipeline configs and environment files are small; anything larger is not one.
MAX_TEXT_BYTES = 8 * 1024 * 1024


def read_text(
    path: Path, *, limit: int = MAX_TEXT_BYTES, encoding: str = "utf-8"
) -> str:
    """Read a regular file of at most *limit* bytes.

    A checkout may make any expected file name a symlink to a device, a FIFO,
    or an enormous file.  ``Path.read_text`` would block or exhaust memory on
    those, so only a bounded regular file is ever read.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        if info.st_size > limit:
            raise OSError(errno.EFBIG, "file exceeds the read limit", str(path))
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise OSError(errno.EFBIG, "file exceeds the read limit", str(path))
    return data.decode(encoding)


def display_load(text: str) -> Any:
    """Parse YAML for presentation with the fastest available safe loader.

    Launch plans, study gates, and provenance keep ``yaml.safe_load`` so their
    recorded behaviour does not depend on how PyYAML was built.
    """
    # Both candidates are safe loaders; bandit cannot see through the alias.
    return yaml.load(text, Loader=_DisplayLoader)  # nosec B506
