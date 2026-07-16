# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Environment-name compatibility for the OmicsANG rebrand.

The public ``OMICSANG_*`` names are authoritative.  Legacy ``BENCHTOP_*``
names remain readable so an existing local installation does not silently
change its root, state, or network settings during the rename.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

PRIMARY_PREFIX = "OMICSANG"
LEGACY_PREFIX = "BENCHTOP"
_SUFFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class EnvironmentConflict(ValueError):
    """Raised when public and legacy names specify different values."""


def environment_names(suffix: str) -> tuple[str, str]:
    """Return the public and legacy names for one validated setting suffix."""
    normalized = str(suffix or "").strip().upper()
    if not _SUFFIX_RE.fullmatch(normalized):
        raise ValueError("environment setting suffix is invalid")
    return f"{PRIMARY_PREFIX}_{normalized}", f"{LEGACY_PREFIX}_{normalized}"


def environment_value(
    suffix: str,
    default: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve a public setting with a conflict-checked legacy fallback.

    Values are deliberately omitted from conflict messages because roots and
    other settings can contain private local information.
    """
    values = os.environ if environ is None else environ
    primary_name, legacy_name = environment_names(suffix)
    primary = values.get(primary_name)
    legacy = values.get(legacy_name)
    if primary is not None and legacy is not None and primary != legacy:
        raise EnvironmentConflict(
            f"{primary_name} and {legacy_name} are both set but disagree; "
            f"remove one or make them identical"
        )
    if primary is not None:
        return primary
    if legacy is not None:
        return legacy
    return default
