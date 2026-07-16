# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Focused tests for the tagged public-release metadata gate."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import scripts.check_public_release as release_gate
from scripts.check_public_release import check_public_release, main

PLACEHOLDER = "REPLACE_BEFORE_" + "PUBLICATION"
PRIVATE_HOME = "/" + "home" + "/" + "epigenetics"
PRIVATE_MEDIA = "/" + "media" + "/" + "epigenetics"
STALE_BRAND = "Bench" + "top"
LEGACY_LOWER = STALE_BRAND.lower()


def _write(root: Path, relative: str, text: str = "published\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _release_tree(root: Path) -> Path:
    for document in (
        "README.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "docs/BEGINNER_TUTORIAL.md",
        "docs/agent-sandbox.md",
        "docs/assets/omicsang-journey.svg",
        "docs/assets/omicsang-workspace-map.svg",
    ):
        _write(root, document)
    _write(
        root,
        "LICENSE",
        "MIT License\n\nCopyright (c) 2026 Example\n",
    )
    _write(root, "benchtop/web/vendor/xterm/5.3.0/LICENSE", "MIT License\n")
    _write(
        root,
        "benchtop/web/vendor/xterm-addon-fit/0.8.0/LICENSE",
        "MIT License\n",
    )
    _write(
        root,
        "pyproject.toml",
        """[project]
name = "omicsang"
license = "MIT"
license-files = [
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
]

[project.scripts]
omicsang = "benchtop.__main__:main"
benchtop = "benchtop.__main__:main"

[tool.setuptools]
packages = ["benchtop", "omicsang"]
""",
    )
    _write(
        root,
        "MANIFEST.in",
        """include README.md
include LICENSE
include THIRD_PARTY_NOTICES.md
include SECURITY.md
include docs/BEGINNER_TUTORIAL.md
include docs/agent-sandbox.md
recursive-include docs/assets *.svg
recursive-include benchtop/web *
recursive-include omicsang *.py
""",
    )
    _write(root, "benchtop/__init__.py", "__version__ = '1.0.0'\n")
    _write(root, "omicsang/__init__.py", "from benchtop import __version__\n")
    _write(
        root,
        "omicsang/__main__.py",
        "from benchtop.__main__ import main\nraise SystemExit(main())\n",
    )
    return root


def test_clean_release_tree_passes(tmp_path: Path) -> None:
    root = _release_tree(tmp_path)

    assert check_public_release(root) == []


def test_python_310_metadata_fallback_accepts_release_contract(
    tmp_path: Path, monkeypatch: object
) -> None:
    root = _release_tree(tmp_path)
    monkeypatch.setattr(release_gate, "tomllib", None)  # type: ignore[attr-defined]

    assert check_public_release(root) == []


def test_gate_reports_all_publication_blocker_categories(tmp_path: Path) -> None:
    root = _release_tree(tmp_path)
    (root / "README.md").unlink()
    (root / "benchtop/web/vendor/xterm-addon-fit/0.8.0/LICENSE").unlink()
    _write(
        root,
        "SECURITY.md",
        f"No version is currently published.\nContact {PLACEHOLDER}.\n",
    )
    _write(
        root,
        "benchtop/runtime.py",
        f"ROOT_A = {PRIVATE_HOME!r}\nROOT_B = {PRIVATE_MEDIA!r}\n",
    )
    _write(
        root,
        "pyproject.toml",
        """[project]
name = "benchtop"
license = "Apache-2.0"
license-files = ["LICENSE"]
""",
    )
    manifest = (root / "MANIFEST.in").read_text(encoding="utf-8")
    (root / "MANIFEST.in").write_text(
        manifest.replace("include SECURITY.md\n", ""), encoding="utf-8"
    )

    findings = check_public_release(root)
    codes = {finding.code for finding in findings}

    assert {
        "draft-security-version",
        "distribution-name-mismatch",
        "console-script-mismatch",
        "license-files-mismatch",
        "license-metadata-mismatch",
        "manifest-omission",
        "missing-public-document",
        "missing-vendor-license",
        "package-set-mismatch",
        "private-absolute-root",
        "publication-placeholder",
    } <= codes
    private_findings = [
        finding for finding in findings if finding.code == "private-absolute-root"
    ]
    assert len(private_findings) == 2


def test_generated_caches_and_minified_vendor_assets_are_not_scanned(
    tmp_path: Path,
) -> None:
    root = _release_tree(tmp_path)
    bad_text = f"{PLACEHOLDER} {PRIVATE_HOME} {PRIVATE_MEDIA}\n"
    for relative in (
        ".git/config",
        ".agents/local-state",
        ".cache/state",
        ".claude/local-state",
        ".codex/local-state",
        ".gitnexus/local-state",
        ".hypothesis/state",
        ".pytest_cache/state",
        ".ruff_cache/state",
        "__pycache__/module.pyc",
        "build/report.txt",
        "dist/report.txt",
        "graphify-out/graph.json",
        "package.egg-info/PKG-INFO",
        "benchtop/web/vendor/xterm/5.3.0/xterm.js",
        "benchtop/web/vendor/xterm/5.3.0/xterm.css",
        "benchtop/web/vendor/xterm-addon-fit/0.8.0/xterm-addon-fit.js",
    ):
        _write(root, relative, bad_text)

    assert check_public_release(root) == []


def test_cli_is_nonzero_and_prints_every_finding(
    tmp_path: Path, capsys: object
) -> None:
    root = _release_tree(tmp_path)
    _write(root, "SECURITY.md", f"{PLACEHOLDER}\nNo version is currently published.\n")
    _write(root, "benchtop/path.py", f"ROOT = {PRIVATE_HOME!r}\n")

    assert main(["--root", str(root)]) == 1
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert "Public release blocked" in captured.out
    assert "publication placeholder" in captured.out
    assert "private absolute checkout root" in captured.out
    assert "no version is published" in captured.out


def test_manifest_must_package_each_public_document_and_vendor_license(
    tmp_path: Path,
) -> None:
    root = _release_tree(tmp_path)
    _write(root, "MANIFEST.in", "include README.md\n")

    omissions = [
        finding
        for finding in check_public_release(root)
        if finding.code == "manifest-omission"
    ]

    assert {finding.message.rsplit(": ", 1)[-1] for finding in omissions} == {
        "LICENSE",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "docs/BEGINNER_TUTORIAL.md",
        "docs/agent-sandbox.md",
        "docs/assets/omicsang-journey.svg",
        "docs/assets/omicsang-workspace-map.svg",
        "benchtop/web/vendor/xterm/5.3.0/LICENSE",
        "benchtop/web/vendor/xterm-addon-fit/0.8.0/LICENSE",
        "omicsang/__init__.py",
        "omicsang/__main__.py",
    }


def test_beginner_tutorial_and_visuals_are_linked_safe_release_assets() -> None:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    tutorial = (root / "docs/BEGINNER_TUTORIAL.md").read_text(encoding="utf-8")

    assert "[beginner tutorial](docs/BEGINNER_TUTORIAL.md)" in readme
    assert "config/config.yaml" in tutorial
    assert "synthetic-summary.tsv" in tutorial
    assert "Ctrl-C" in tutorial

    for relative in (
        "docs/assets/omicsang-journey.svg",
        "docs/assets/omicsang-workspace-map.svg",
    ):
        path = root / relative
        ElementTree.parse(path)
        source = path.read_text(encoding="utf-8").lower()
        assert "<script" not in source
        assert "<foreignobject" not in source

    manifest_findings = release_gate._check_manifest(root)  # noqa: SLF001
    assert not [
        finding
        for finding in manifest_findings
        if finding.message.rsplit(": ", 1)[-1]
        in {
            "docs/BEGINNER_TUTORIAL.md",
            "docs/assets/omicsang-journey.svg",
            "docs/assets/omicsang-workspace-map.svg",
        }
    ]


def test_gate_rejects_visible_legacy_brand_but_allows_compatibility_tokens(
    tmp_path: Path,
) -> None:
    root = _release_tree(tmp_path)
    _write(
        root,
        "benchtop/compatibility.py",
        "\n".join(
            (
                "from benchtop import settings",
                'LEGACY_STATE = ".benchtop"',
                'LEGACY_ENV = "BENCHTOP_STATE"',
                'LEGACY_HEADER = "X-Benchtop-CSRF"',
                'LEGACY_SESSION = "benchtop_session"',
                'LEGACY_TAG = "benchtop:job"',
            )
        ),
    )
    _write(
        root,
        f"{LEGACY_LOWER}/store.py",
        f'actor: str = "{LEGACY_LOWER}"\n',
    )
    _write(
        root,
        f"{LEGACY_LOWER}/runplan.py",
        f'DOMAIN = b"{LEGACY_LOWER}-bounded-file-v1"\n',
    )
    _write(
        root,
        f"{LEGACY_LOWER}/web/app.js",
        f"const LEGACY_NAVIGATION_STATE_KEY = '{LEGACY_LOWER}NavigationV1';\n",
    )
    _write(
        root,
        "README.md",
        "OmicsANG supports the legacy `benchtop` command.\n",
    )

    assert check_public_release(root) == []

    _write(root, "README.md", f"{STALE_BRAND} is the public product name.\n")

    findings = check_public_release(root)
    stale = [finding for finding in findings if finding.code == "stale-public-brand"]
    assert len(stale) == 1
    assert stale[0].path == "README.md"
    assert stale[0].line == 1


def test_quoted_and_hyphenated_public_brand_are_not_blanket_allowlisted() -> None:
    for source in (
        f'print("{LEGACY_LOWER}")',
        f'print("{LEGACY_LOWER}-dashboard")',
        f"Welcome to {LEGACY_LOWER}",
    ):
        findings = release_gate._check_legacy_public_brand(  # noqa: SLF001
            Path("public.py"), source
        )
        assert [finding.code for finding in findings] == ["stale-public-brand"]


def test_distribution_scripts_and_both_packages_are_required(tmp_path: Path) -> None:
    root = _release_tree(tmp_path)
    _write(
        root,
        "pyproject.toml",
        """[project]
name = "not-omicsang"
license = "MIT"
license-files = ["LICENSE", "THIRD_PARTY_NOTICES.md"]

[project.scripts]
omicsang = "omicsang.__main__:main"

[tool.setuptools]
packages = ["omicsang"]
""",
    )

    findings = check_public_release(root)
    codes = {finding.code for finding in findings}

    assert {
        "distribution-name-mismatch",
        "console-script-mismatch",
        "package-set-mismatch",
    } <= codes
