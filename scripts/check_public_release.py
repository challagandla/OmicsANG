# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Fail closed when public-release metadata is incomplete or checkout-specific.

This check intentionally uses only the Python standard library.  It is designed
for tagged-release CI, where a single run should report every known publication
blocker rather than making maintainers fix them one at a time.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    tomllib = None  # type: ignore[assignment]


EXPECTED_LICENSE_ID = "MIT"
EXPECTED_DISTRIBUTION_NAME = "omicsang"
IMPLEMENTATION_PACKAGE = "bench" + "top"
EXPECTED_CONSOLE_SCRIPTS = {
    "omicsang": f"{IMPLEMENTATION_PACKAGE}.__main__:main",
    IMPLEMENTATION_PACKAGE: f"{IMPLEMENTATION_PACKAGE}.__main__:main",
}
EXPECTED_PACKAGES = {IMPLEMENTATION_PACKAGE, "omicsang"}
PUBLIC_DOCUMENTS = (
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/BEGINNER_TUTORIAL.md",
    "docs/agent-sandbox.md",
    "docs/assets/omicsang-journey.svg",
    "docs/assets/omicsang-workspace-map.svg",
)
VENDORED_LICENSES = (
    "benchtop/web/vendor/xterm/5.3.0/LICENSE",
    "benchtop/web/vendor/xterm-addon-fit/0.8.0/LICENSE",
)
SHIM_FILES = (
    "omicsang/__init__.py",
    "omicsang/__main__.py",
)
MANIFEST_REQUIRED_PATHS = PUBLIC_DOCUMENTS + VENDORED_LICENSES + SHIM_FILES

# Split publication-only values so a completed gate does not flag its own
# implementation after maintainers replace the SPDX header above.
PUBLICATION_PLACEHOLDER = "REPLACE_BEFORE_" + "PUBLICATION"
PRIVATE_ROOTS = (
    "/" + "home" + "/" + "epigenetics",
    "/" + "media" + "/" + "epigenetics",
)
DRAFT_SECURITY_PHRASE = "No version is currently published"
LEGACY_PUBLIC_BRAND = "bench" + "top"
LEGACY_PUBLIC_BRAND_PATTERN = re.compile(re.escape(LEGACY_PUBLIC_BRAND), re.IGNORECASE)
_LEGACY_RE = re.escape(LEGACY_PUBLIC_BRAND)
EXPLICIT_COMPATIBILITY_LINE_PATTERNS = {
    "benchtop/env_compat.py": (
        rf'^LEGACY_PREFIX\s*=\s*["\']{_LEGACY_RE.upper()}["\']$',
    ),
    "benchtop/runplan.py": (
        rf"{_LEGACY_RE}-bounded-(?:file|directory|index-prefix)-v1",
        rf"{_LEGACY_RE}-discovered-default",
    ),
    "benchtop/server.py": (
        rf"""INSTANCE_ID\s*=\s*f?["']{_LEGACY_RE}-""",
        rf"""resources\.files\(["']{_LEGACY_RE}["']\)""",
        rf"""row\[["']actor["']\]\s*=\s*["']{_LEGACY_RE}["']""",
        rf"""name=["']{_LEGACY_RE}-(?:local-scheduler|controller-maintenance)["']""",
    ),
    "benchtop/sessions.py": (rf"""name=f?["']{_LEGACY_RE}-exit-""",),
    "benchtop/settings.py": (rf"""^[ ]*["']{_LEGACY_RE}["'],?$""",),
    "benchtop/state_db.py": (rf"""\bactor\b.*["']{_LEGACY_RE}["']""",),
    "benchtop/store.py": (rf"""\bactor\b.*["']{_LEGACY_RE}["']""",),
    "benchtop/web/app.js": (
        rf"""LEGACY_NAVIGATION_STATE_KEY\s*=\s*["']{_LEGACY_RE}NavigationV1["']""",
    ),
    "scripts/benchmark_control_plane.py": (
        rf"{_LEGACY_RE}-control-plane-benchmark-",
        rf"{_LEGACY_RE}-durable-control-plane",
    ),
    "scripts/check_public_release.py": (
        rf"""parts\[:3\]\s*==\s*\(["']{_LEGACY_RE}["']""",
    ),
    "tests/test_frontend_security.py": (
        rf"""LEGACY_NAVIGATION_STATE_KEY\s*=\s*["']{_LEGACY_RE}NavigationV1["']""",
    ),
    "tests/test_omicsang_runtime.py": (
        rf"""\(["']omicsang["'],\s*["']{_LEGACY_RE}["']\)""",
    ),
    "tests/test_pipeline_git_security.py": (rf"{_LEGACY_RE}-test@example\.invalid",),
    "tests/test_public_release_gate.py": (
        rf"""^name\s*=\s*["']{_LEGACY_RE}["']$""",
        rf"""^{_LEGACY_RE}\s*=\s*["']{_LEGACY_RE}\.__main__:main["']$""",
        rf"""packages\s*=\s*\[["']{_LEGACY_RE}["']""",
        rf"LEGACY_(?:ACTOR|THREAD).*{_LEGACY_RE}",
    ),
    "tests/test_security.py": (
        rf"""^[ ]*["']{_LEGACY_RE}["'],?$""",
        rf"/tmp/private-{_LEGACY_RE}-state",
    ),
}

EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".hypothesis",
        ".ipynb_checkpoints",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        ".cache",
        ".agents",
        ".claude",
        ".codex",
        ".gitnexus",
        "__pycache__",
        "build",
        "dist",
        "graphify-out",
        "htmlcov",
        "node_modules",
        "venv",
    }
)


@dataclass(frozen=True)
class Finding:
    """One actionable public-release blocker."""

    code: str
    path: str
    message: str
    line: int | None = None

    def render(self) -> str:
        location = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{location}: {self.message}"


def _is_excluded_directory(name: str) -> bool:
    return name in EXCLUDED_DIRECTORY_NAMES or name.endswith(".egg-info")


def _is_vendored_minified_asset(relative: Path) -> bool:
    parts = relative.parts
    return (
        len(parts) >= 4
        and parts[:3] == ("benchtop", "web", "vendor")
        and relative.suffix.lower() in {".css", ".js"}
    )


def _read_text(path: Path) -> str | None:
    """Return UTF-8 text, or ``None`` for binary/non-UTF-8 content."""

    try:
        payload = path.read_bytes()
    except OSError:
        raise
    if b"\x00" in payload:
        return None
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _release_text_files(root: Path) -> tuple[list[tuple[Path, str]], list[Finding]]:
    files: list[tuple[Path, str]] = []
    findings: list[Finding] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not _is_excluded_directory(name)
            and not (current_path / name).is_symlink()
        )
        for name in sorted(file_names):
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root)
            if _is_vendored_minified_asset(relative):
                continue
            try:
                text = _read_text(path)
            except OSError as error:
                findings.append(
                    Finding(
                        "unreadable-text",
                        relative.as_posix(),
                        f"release-controlled file could not be read: {error}",
                    )
                )
                continue
            if text is not None:
                files.append((relative, text))
    return files, findings


def _first_line_containing(text: str, value: str) -> int | None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        if value in line:
            return line_number
    return None


def _check_required_files(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for relative in PUBLIC_DOCUMENTS:
        path = root / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            findings.append(
                Finding(
                    "missing-public-document",
                    relative,
                    "required public document is missing, empty, or a symlink",
                )
            )
    for relative in VENDORED_LICENSES:
        path = root / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            findings.append(
                Finding(
                    "missing-vendor-license",
                    relative,
                    "required vendored dependency license is missing, empty, or a symlink",
                )
            )
    for relative in SHIM_FILES:
        path = root / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            findings.append(
                Finding(
                    "missing-compatibility-shim",
                    relative,
                    "required OmicsANG import/launch shim is missing, empty, or a symlink",
                )
            )
    return findings


def _fallback_project_metadata(text: str) -> dict[str, Any]:
    """Parse the narrow release contract needed by this gate on Python 3.10."""

    project: dict[str, Any] = {}
    scripts: dict[str, str] = {}
    setuptools: dict[str, Any] = {}
    section = ""
    collecting_license_files = False
    license_files: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            collecting_license_files = False
            continue
        if not line or line.startswith("#"):
            continue
        if section == "project" and collecting_license_files:
            before_close, separator, _ = line.partition("]")
            license_files.extend(_quoted_values(before_close))
            if separator:
                collecting_license_files = False
                project["license-files"] = license_files
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if section == "project" and key in {"name", "license"}:
            values = _quoted_values(value)
            project[key] = values[0] if len(values) == 1 else None
        elif section == "project" and key == "license-files":
            before_close, close, _ = value.partition("]")
            license_files.extend(_quoted_values(before_close))
            if close:
                project[key] = license_files
            else:
                collecting_license_files = True
        elif section == "project.scripts":
            values = _quoted_values(value)
            scripts[key] = values[0] if len(values) == 1 else ""
        elif section == "tool.setuptools" and key == "packages":
            setuptools[key] = _quoted_values(value)
    project["scripts"] = scripts
    return {"project": project, "tool": {"setuptools": setuptools}}


def _quoted_values(text: str) -> list[str]:
    values: list[str] = []
    quote: str | None = None
    start = 0
    escaped = False
    for index, character in enumerate(text):
        if quote is None:
            if character in {'"', "'"}:
                quote = character
                start = index + 1
            continue
        if escaped:
            escaped = False
        elif character == "\\" and quote == '"':
            escaped = True
        elif character == quote:
            values.append(text[start:index])
            quote = None
    return values


def _check_pyproject(root: Path) -> list[Finding]:
    relative = "pyproject.toml"
    path = root / relative
    if not path.is_file() or path.is_symlink():
        return [
            Finding(
                "missing-project-metadata",
                relative,
                "project metadata is missing or is a symlink",
            )
        ]
    try:
        text = path.read_text(encoding="utf-8")
        metadata = (
            tomllib.loads(text)
            if tomllib is not None
            else _fallback_project_metadata(text)
        )
    except (OSError, UnicodeError, ValueError) as error:
        return [
            Finding(
                "invalid-project-metadata",
                relative,
                f"project metadata could not be parsed: {error}",
            )
        ]

    findings: list[Finding] = []
    project = metadata.get("project")
    if not isinstance(project, dict):
        project = {}
    distribution_name = project.get("name")
    if distribution_name != EXPECTED_DISTRIBUTION_NAME:
        findings.append(
            Finding(
                "distribution-name-mismatch",
                relative,
                f"project.name must be {EXPECTED_DISTRIBUTION_NAME!r}; "
                f"found {distribution_name!r}",
            )
        )
    scripts = project.get("scripts")
    if not isinstance(scripts, dict):
        scripts = {}
    for script, target in EXPECTED_CONSOLE_SCRIPTS.items():
        if scripts.get(script) != target:
            findings.append(
                Finding(
                    "console-script-mismatch",
                    relative,
                    f"project.scripts.{script} must target {target!r}; "
                    f"found {scripts.get(script)!r}",
                )
            )
    tool = metadata.get("tool")
    if not isinstance(tool, dict):
        tool = {}
    setuptools = tool.get("setuptools")
    if not isinstance(setuptools, dict):
        setuptools = {}
    packages = setuptools.get("packages")
    if not isinstance(packages, list):
        packages = []
    missing_packages = sorted(EXPECTED_PACKAGES - set(packages))
    if missing_packages:
        findings.append(
            Finding(
                "package-set-mismatch",
                relative,
                "tool.setuptools.packages is missing: " + ", ".join(missing_packages),
            )
        )
    license_id = project.get("license")
    if license_id != EXPECTED_LICENSE_ID:
        findings.append(
            Finding(
                "license-metadata-mismatch",
                relative,
                f"project.license must be {EXPECTED_LICENSE_ID!r}; found {license_id!r}",
            )
        )
    license_files = project.get("license-files")
    required_license_files = {"LICENSE", "THIRD_PARTY_NOTICES.md"}
    if not isinstance(license_files, list):
        license_files = []
    missing = sorted(required_license_files - set(license_files))
    if missing:
        findings.append(
            Finding(
                "license-files-mismatch",
                relative,
                f"project.license-files is missing: {', '.join(missing)}",
            )
        )
    return findings


def _manifest_patterns(text: str) -> list[tuple[str, tuple[str, ...]]]:
    directives: list[tuple[str, tuple[str, ...]]] = []
    for raw_line in text.splitlines():
        try:
            fields = shlex.split(raw_line, comments=True)
        except ValueError:
            continue
        if fields:
            directives.append((fields[0], tuple(fields[1:])))
    return directives


def _manifest_includes(
    relative: str, directives: list[tuple[str, tuple[str, ...]]]
) -> bool:
    for command, arguments in directives:
        if command in {"include", "global-include"} and any(
            fnmatch.fnmatchcase(relative, pattern) for pattern in arguments
        ):
            return True
        if command == "graft" and any(
            relative == directory or relative.startswith(f"{directory.rstrip('/')}/")
            for directory in arguments
        ):
            return True
        if command == "recursive-include" and len(arguments) >= 2:
            directory, *patterns = arguments
            prefix = f"{directory.rstrip('/')}/"
            if relative.startswith(prefix):
                nested = relative[len(prefix) :]
                if any(fnmatch.fnmatchcase(nested, pattern) for pattern in patterns):
                    return True
    return False


def _check_manifest(root: Path) -> list[Finding]:
    relative = "MANIFEST.in"
    path = root / relative
    if not path.is_file() or path.is_symlink():
        return [
            Finding(
                "missing-manifest",
                relative,
                "source-distribution manifest is missing or is a symlink",
            )
        ]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return [
            Finding(
                "invalid-manifest",
                relative,
                f"source-distribution manifest could not be read: {error}",
            )
        ]
    directives = _manifest_patterns(text)
    return [
        Finding(
            "manifest-omission",
            relative,
            f"source distribution does not include required file: {required}",
        )
        for required in MANIFEST_REQUIRED_PATHS
        if not _manifest_includes(required, directives)
    ]


def _is_allowed_legacy_brand_token(
    relative: Path, line: str, start: int, end: int
) -> bool:
    """Allow only concrete compatibility identifiers, paths, and protocol names."""

    token = line[start:end]
    before = line[start - 1] if start else ""
    after = line[end] if end < len(line) else ""

    if (
        line[max(0, start - 2) : start].lower() == "x-"
        and line[end : end + 5].lower() == "-csrf"
    ):
        return True
    if token.isupper() and after == "_":
        return True
    if any(
        re.search(pattern, line)
        for pattern in EXPLICIT_COMPATIBILITY_LINE_PATTERNS.get(relative.as_posix(), ())
    ):
        return True
    if token != LEGACY_PUBLIC_BRAND:
        return False

    if relative.suffix == ".py" and re.search(r"\b(?:from|import)\s+$", line[:start]):
        return True

    # Markdown compatibility names must be explicit code spans, never prose.
    if line[:start].count("`") % 2 == 1:
        return True

    # Package/module paths, state keys, protocol tokens, and ownership tags.
    if (before and before in "./_:") or (after and after in "./_:"):
        return True

    # A quoted path component is an implementation/state compatibility path,
    # not a public-facing product label.
    if (
        before == after
        and before in {'"', "'"}
        and (
            line[: start - 1].rstrip().endswith("/")
            or line[end + 1 :].lstrip().startswith("/")
        )
    ):
        return True

    if relative.as_posix() == "pyproject.toml":
        stripped = line.strip()
        return (
            stripped.startswith(f"{IMPLEMENTATION_PACKAGE} =")
            or f'"{IMPLEMENTATION_PACKAGE}"' in stripped
            or f"'{IMPLEMENTATION_PACKAGE}'" in stripped
        )
    return False


def _check_legacy_public_brand(relative: Path, text: str) -> list[Finding]:
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in LEGACY_PUBLIC_BRAND_PATTERN.finditer(line):
            if _is_allowed_legacy_brand_token(
                relative, line, match.start(), match.end()
            ):
                continue
            return [
                Finding(
                    "stale-public-brand",
                    relative.as_posix(),
                    "legacy public product branding remains; use OmicsANG or "
                    "an approved compatibility identifier",
                    line_number,
                )
            ]
    return []


def check_public_release(root: Path) -> list[Finding]:
    """Return every blocker found under ``root`` in stable display order."""

    root = root.resolve()
    findings = _check_required_files(root)
    findings.extend(_check_pyproject(root))
    findings.extend(_check_manifest(root))

    text_files, read_findings = _release_text_files(root)
    findings.extend(read_findings)
    for relative, text in text_files:
        relative_text = relative.as_posix()
        findings.extend(_check_legacy_public_brand(relative, text))
        placeholder_line = _first_line_containing(text, PUBLICATION_PLACEHOLDER)
        if placeholder_line is not None:
            findings.append(
                Finding(
                    "publication-placeholder",
                    relative_text,
                    "publication placeholder must be replaced before tagging",
                    placeholder_line,
                )
            )
        for private_root in PRIVATE_ROOTS:
            private_line = _first_line_containing(text, private_root)
            if private_line is not None:
                findings.append(
                    Finding(
                        "private-absolute-root",
                        relative_text,
                        f"private absolute checkout root is present: {private_root}",
                        private_line,
                    )
                )

    security_path = root / "SECURITY.md"
    if security_path.is_file() and not security_path.is_symlink():
        try:
            security_text = security_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            security_text = ""
        draft_line = _first_line_containing(security_text, DRAFT_SECURITY_PHRASE)
        if draft_line is not None:
            findings.append(
                Finding(
                    "draft-security-version",
                    "SECURITY.md",
                    "release policy still says that no version is published",
                    draft_line,
                )
            )

    return sorted(
        set(findings),
        key=lambda finding: (
            finding.path,
            finding.line or 0,
            finding.code,
            finding.message,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject public release tags with incomplete publication metadata."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the checkout containing this script)",
    )
    arguments = parser.parse_args(argv)
    if not arguments.root.is_dir():
        parser.error(f"repository root is not a directory: {arguments.root}")

    findings = check_public_release(arguments.root)
    if findings:
        print(f"Public release blocked by {len(findings)} issue(s):")
        for finding in findings:
            print(f"- {finding.render()}")
        return 1
    print("Public release metadata gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
