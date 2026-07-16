# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Immutable, content-addressed execution contracts for pipeline runs."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

import yaml

from . import pipelines as pipeline_tools
from .pipelines import Pipeline

SCHEMA_VERSION = 1
SECRET_RE = re.compile(
    r"(token|secret|password|passwd|credential|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret|authorization|cookie)",
    re.I,
)
WORKFLOW_SUFFIXES = {
    ".smk",
    ".py",
    ".r",
    ".R",
    ".sh",
    ".bash",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ipynb",
    ".def",
}
WORKFLOW_DIRS = (
    "workflow",
    "rules",
    "modules",
    "scripts",
    "src",
    "lib",
    "bin",
    "wrappers",
    "notebooks",
)
ENVIRONMENT_NAMES = {
    "environment.yml",
    "environment.yaml",
    "Dockerfile",
    "Singularity",
    "Apptainer",
    "requirements.txt",
    "pyproject.toml",
    "renv.lock",
}
MAX_SOURCE_FILES = 800
MAX_PACKAGE_RECORDS = 800
MAX_DIRECTIVE_RECORDS = 1200
MAX_REFERENCE_ARTIFACTS = 256
MAX_ARTIFACT_FILES = 128
MAX_ARTIFACT_HASH_BYTES = 8 * 1024 * 1024
ARTIFACT_SAMPLE_BYTES = 1024 * 1024
MAX_CONFIG_PARSE_BYTES = 4 * 1024 * 1024
STATIC_DIRECTIVE_RE = re.compile(
    r"(?m)^\s*(include|snakefile|script|notebook|conda):\s*['\"]([^'\"]+)['\"]"
)
SNAKEMAKE_DIRECTIVE_RE = re.compile(
    r"^\s*(include|snakefile|configfile|script|notebook|conda|wrapper|container|singularity)"
    r"\s*:\s*(.*?)\s*$"
)
SNAKEMAKE_SOURCE_SUFFIXES = {".smk", ".snakefile", ".rule"}
REFERENCE_KEY_RE = re.compile(
    r"(?:^|_)(?:reference|ref|genome|fasta|fa|gtf|gff3?|annotation|blacklist|"
    r"exclude(?:d)?_regions?|chrom(?:osome)?_sizes?|index|indices|bowtie2?|bwa|"
    r"star|salmon|kallisto)(?:_|$)",
    re.I,
)
REFERENCE_SUFFIX_RE = re.compile(
    r"\.(?:fa|fasta|fna|gtf|gff3?|bed|bedgraph|2bit|dict|sizes|chrom\.sizes|"
    r"bt2l?|bwt|sa|ann|amb|pac|mmi|idx|index|sif)(?:\.(?:gz|bgz))?$",
    re.I,
)
REMOTE_SCHEMES = {
    "docker",
    "oras",
    "library",
    "http",
    "https",
    "ftp",
    "s3",
    "gs",
    "az",
    "oci",
    "shub",
    "git",
    "git+https",
    "git+ssh",
}
VERSION_COMPONENT_RE = re.compile(r"^v?\d+(?:[._-]\d+)*(?:[-+._][A-Za-z0-9.-]+)?$")


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _declared_path(root: Path, raw: str) -> Path | None:
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity_fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _remote_scheme(raw: str) -> str:
    scheme = urlsplit(str(raw or "")).scheme.lower()
    return scheme if scheme in REMOTE_SCHEMES else ""


def _literal_string(expression: str) -> str | None:
    """Resolve only a Python string literal; never evaluate names or calls."""
    try:
        value = ast.literal_eval(expression.strip())
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _bounded_file_fingerprint(path: Path, size: int) -> tuple[str, str, int, str]:
    """Return (fingerprint, full sha256, bytes read, mode) within a fixed budget."""
    if size <= MAX_ARTIFACT_HASH_BYTES:
        digest = _sha256_file(path)
        return f"sha256:{digest}", digest, size, "full"
    sample = min(ARTIFACT_SAMPLE_BYTES, max(1, MAX_ARTIFACT_HASH_BYTES // 2))
    digest = hashlib.sha256()
    digest.update(b"benchtop-bounded-file-v1\0")
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        head = handle.read(sample)
        handle.seek(max(0, size - sample))
        tail = handle.read(sample)
    digest.update(head)
    digest.update(tail)
    return (
        f"sha256-sampled:{digest.hexdigest()}",
        "",
        len(head) + len(tail),
        "sampled-head-tail",
    )


def _bounded_collection_fingerprint(
    base: Path,
    paths: list[Path],
    *,
    domain: bytes,
) -> tuple[str, int, int, bool]:
    digest = hashlib.sha256()
    digest.update(domain + b"\0")
    bytes_hashed = 0
    total_size = 0
    truncated = len(paths) > MAX_ARTIFACT_FILES
    for path in paths[:MAX_ARTIFACT_FILES]:
        try:
            size = path.stat().st_size
            relative = path.relative_to(base).as_posix()
            total_size += size
            digest.update(relative.encode("utf-8", errors="replace"))
            digest.update(b"\0" + str(size).encode("ascii") + b"\0")
            if path.is_symlink():
                digest.update(b"symlink\0")
                digest.update(
                    path.readlink().as_posix().encode("utf-8", errors="replace")
                )
                digest.update(b"\0")
            remaining = max(0, MAX_ARTIFACT_HASH_BYTES - bytes_hashed)
            if size <= remaining:
                with path.open("rb") as handle:
                    content = handle.read()
                digest.update(b"full\0")
                digest.update(content)
                bytes_hashed += len(content)
            elif remaining:
                head_size = (remaining + 1) // 2
                tail_size = remaining // 2
                with path.open("rb") as handle:
                    head = handle.read(head_size)
                    handle.seek(max(0, size - tail_size))
                    tail = handle.read(tail_size)
                digest.update(b"sampled\0")
                digest.update(head)
                digest.update(tail)
                bytes_hashed += len(head) + len(tail)
                truncated = True
            elif size:
                truncated = True
        except OSError:
            digest.update(b"unreadable\0")
    return f"sha256-bounded:{digest.hexdigest()}", bytes_hashed, total_size, truncated


def _bounded_directory_files(root: Path) -> tuple[list[Path], bool]:
    files: list[Path] = []
    stack = [root]
    truncated = False
    while stack and len(files) <= MAX_ARTIFACT_FILES:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            truncated = True
            continue
        children: list[Path] = []
        for entry in entries:
            try:
                if entry.is_file():
                    files.append(entry)
                    if len(files) > MAX_ARTIFACT_FILES:
                        truncated = True
                        break
                elif entry.is_dir() and not entry.is_symlink():
                    children.append(entry)
            except OSError:
                truncated = True
        stack.extend(reversed(children))
    if stack:
        truncated = True
    return files, truncated


def _bounded_artifact_layer(
    root: Path,
    raw: str,
    kind: str,
    *,
    base: Path | None = None,
    allow_prefix: bool = False,
    prefix_suffixes: tuple[str, ...] = (),
) -> dict:
    declared = str(raw or "")
    if not declared:
        return {
            "kind": kind,
            "path": "",
            "declared_path": "",
            "exists": False,
            "symlink": False,
            "symlink_target": "",
            "artifact_type": "missing",
            "size": 0,
            "sha256": "",
            "content_fingerprint": "",
            "hash_mode": "none",
            "bytes_hashed": 0,
            "files_truncated": False,
            "resolution_status": "missing",
        }
    if declared.startswith("file://"):
        local_raw = unquote(urlsplit(declared).path)
    else:
        local_raw = declared
    candidate = Path(local_raw).expanduser()
    if candidate.is_absolute():
        lexical = candidate.absolute()
    else:
        root_candidate = (root / candidate).absolute()
        base_candidate = ((base or root) / candidate).absolute()
        lexical = (
            root_candidate
            if root_candidate.exists() or not base_candidate.exists()
            else base_candidate
        )
    path = lexical.resolve()
    display = _display_path(root, path)
    try:
        declared_display = lexical.relative_to(root.absolute()).as_posix()
    except ValueError:
        declared_display = lexical.as_posix()
    is_symlink = lexical.is_symlink()
    try:
        symlink_target = lexical.readlink().as_posix() if is_symlink else ""
    except OSError:
        symlink_target = "<unreadable>"
    try:
        if path.is_file():
            size = path.stat().st_size
            fingerprint, digest, bytes_hashed, mode = _bounded_file_fingerprint(
                path, size
            )
            return {
                "kind": kind,
                "path": display,
                "declared_path": declared_display,
                "exists": True,
                "symlink": is_symlink,
                "symlink_target": symlink_target,
                "artifact_type": "file",
                "size": size,
                "sha256": digest,
                "content_fingerprint": fingerprint,
                "hash_mode": mode,
                "bytes_hashed": bytes_hashed,
                "files_truncated": mode != "full",
                "resolution_status": "resolved-local-file",
            }
        if path.is_dir():
            files, discovery_truncated = _bounded_directory_files(path)
            fingerprint, bytes_hashed, size, truncated = (
                _bounded_collection_fingerprint(
                    path,
                    files,
                    domain=b"benchtop-bounded-directory-v1",
                )
            )
            truncated = truncated or discovery_truncated
            return {
                "kind": kind,
                "path": display,
                "declared_path": declared_display,
                "exists": True,
                "symlink": is_symlink,
                "symlink_target": symlink_target,
                "artifact_type": "directory",
                "size": size,
                "sha256": "",
                "content_fingerprint": fingerprint,
                "hash_mode": (
                    "bounded-directory-manifest"
                    if truncated
                    else "full-directory-manifest"
                ),
                "bytes_hashed": bytes_hashed,
                "file_count": min(len(files), MAX_ARTIFACT_FILES),
                "files_truncated": truncated,
                "resolution_status": "resolved-local-directory",
            }
        if allow_prefix and path.parent.is_dir():
            matches = sorted(
                (
                    item
                    for item in path.parent.glob(path.name + "*")
                    if item.is_file()
                    and (
                        not prefix_suffixes
                        or any(item.name.endswith(suffix) for suffix in prefix_suffixes)
                    )
                ),
                key=lambda item: item.as_posix(),
            )
            if matches:
                fingerprint, bytes_hashed, size, truncated = (
                    _bounded_collection_fingerprint(
                        path.parent,
                        matches,
                        domain=b"benchtop-bounded-index-prefix-v1",
                    )
                )
                return {
                    "kind": kind,
                    "path": display,
                    "declared_path": declared_display,
                    "exists": True,
                    "symlink": is_symlink,
                    "symlink_target": symlink_target,
                    "artifact_type": "index-prefix",
                    "size": size,
                    "sha256": "",
                    "content_fingerprint": fingerprint,
                    "hash_mode": (
                        "bounded-prefix-manifest"
                        if truncated
                        else "full-prefix-manifest"
                    ),
                    "bytes_hashed": bytes_hashed,
                    "file_count": min(len(matches), MAX_ARTIFACT_FILES),
                    "prefix_suffixes": list(prefix_suffixes),
                    "files_truncated": truncated,
                    "resolution_status": "resolved-local-index-prefix",
                }
    except OSError:
        return {
            "kind": kind,
            "path": display,
            "declared_path": declared_display,
            "exists": False,
            "symlink": is_symlink,
            "symlink_target": symlink_target,
            "artifact_type": "unreadable",
            "size": 0,
            "sha256": "",
            "content_fingerprint": "",
            "hash_mode": "none",
            "bytes_hashed": 0,
            "files_truncated": False,
            "resolution_status": "unreadable-local-artifact",
        }
    return {
        "kind": kind,
        "path": display,
        "declared_path": declared_display,
        "exists": False,
        "symlink": is_symlink,
        "symlink_target": symlink_target,
        "artifact_type": "missing",
        "size": 0,
        "sha256": "",
        "content_fingerprint": "",
        "hash_mode": "none",
        "bytes_hashed": 0,
        "files_truncated": False,
        "resolution_status": "missing-local-artifact",
    }


def _file_layer(root: Path, raw: str, kind: str) -> dict:
    path = _declared_path(root, raw)
    if path is None:
        return {"kind": kind, "path": "", "exists": False, "size": 0, "sha256": ""}
    exists = path.is_file()
    try:
        size = path.stat().st_size if exists else 0
        digest = _sha256_file(path) if exists else ""
    except OSError:
        exists, size, digest = False, 0, ""
    return {
        "kind": kind,
        "path": _display_path(root, path),
        "exists": exists,
        "size": size,
        "sha256": digest,
    }


def _aggregate_layers(layers: list[dict]) -> str:
    return hashlib.sha256(_canonical(layers).encode("utf-8")).hexdigest()


def _snakemake_source_candidates(pipeline: Pipeline) -> tuple[list[Path], bool]:
    root = pipeline.path.resolve()
    candidates: set[Path] = set()
    if pipeline.snakefile and (root / pipeline.snakefile).is_file():
        candidates.add((root / pipeline.snakefile).resolve())
    scan_roots = [root, *(root / name for name in ("workflow", "rules", "modules"))]
    truncated = False
    for scan_root in scan_roots:
        if not scan_root.is_dir():
            continue
        iterator = scan_root.iterdir() if scan_root == root else scan_root.rglob("*")
        for path in iterator:
            try:
                if not path.is_file() or path.is_symlink():
                    continue
            except OSError:
                continue
            if (
                path.name == "Snakefile"
                or path.suffix.lower() in SNAKEMAKE_SOURCE_SUFFIXES
            ):
                candidates.add(path.resolve())
                if len(candidates) > MAX_SOURCE_FILES * 2:
                    truncated = True
                    break
        if truncated:
            break
    ordered = sorted(candidates, key=lambda path: _display_path(root, path))
    return ordered[: MAX_SOURCE_FILES * 2], truncated


def _directive_local_target(root: Path, source: Path, declared: str) -> Path:
    candidate = Path(declared).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    source_candidate = (source.parent / candidate).resolve()
    root_candidate = (root / candidate).resolve()
    return (
        source_candidate
        if source_candidate.exists() or not root_candidate.exists()
        else root_candidate
    )


def _scan_snakemake_directives(pipeline: Pipeline) -> tuple[list[dict], bool]:
    """Scan deterministic literal surfaces and label everything else unresolved."""
    root = pipeline.path.resolve()
    candidates, source_candidates_truncated = _snakemake_source_candidates(pipeline)
    entrypoint = (root / pipeline.snakefile).resolve() if pipeline.snakefile else None
    ordered_sources = ([entrypoint] if entrypoint and entrypoint.is_file() else []) + [
        path for path in candidates if path != entrypoint
    ]
    visited: set[Path] = set()
    records: list[dict] = []
    truncated = False

    def visit(source: Path) -> None:
        nonlocal truncated
        if source in visited or truncated:
            return
        visited.add(source)
        try:
            if source.stat().st_size > 2_000_000:
                truncated = True
                return
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            truncated = True
            return
        for line_number, line in enumerate(lines, 1):
            match = SNAKEMAKE_DIRECTIVE_RE.match(line)
            if not match:
                continue
            if len(records) >= MAX_DIRECTIVE_RECORDS:
                truncated = True
                return
            directive, expression = match.groups()
            expression = expression.strip()
            declared = _literal_string(expression)
            record = {
                "order": len(records),
                "directive": directive,
                "source": _display_path(root, source),
                "line": line_number,
                "expression_sha256": hashlib.sha256(
                    redact_text(expression).encode("utf-8")
                ).hexdigest(),
            }
            target: Path | None = None
            if declared is None:
                record.update(
                    {
                        "declaration": "",
                        "expression": expression[:500],
                        "resolution_status": "unresolved-dynamic-python-expression",
                    }
                )
            elif "{" in declared or "}" in declared:
                record.update(
                    {
                        "declaration": declared,
                        "resolution_status": "unresolved-static-template",
                    }
                )
            elif _remote_scheme(declared):
                record.update(
                    {
                        "declaration": declared,
                        "resolution_status": "resolved-remote-identity-only",
                    }
                )
            else:
                record["declaration"] = declared
                if directive in {
                    "include",
                    "snakefile",
                    "configfile",
                    "script",
                    "notebook",
                    "conda",
                }:
                    target = _directive_local_target(root, source, declared)
                    kind = f"snakemake-{directive}-target"
                    material = _file_layer(root, str(target), kind)
                    record["material"] = material
                    record["resolution_status"] = (
                        "resolved-static-local-file"
                        if material["exists"]
                        else "missing-static-local-file"
                    )
                else:
                    record["resolution_status"] = "resolved-static-identity"
            records.append(record)
            if directive in {"include", "snakefile"} and target and target.is_file():
                visit(target)

    for source in ordered_sources:
        visit(source)
    return records, truncated or source_candidates_truncated


def _version_identity(reference: str) -> str:
    parsed = urlsplit(reference)
    parts = [part for part in parsed.path.split("/") if part]
    if not parsed.scheme:
        parts = [part for part in reference.split("/") if part]
    for part in parts:
        if VERSION_COMPONENT_RE.fullmatch(part):
            return part
    revision = reference.rsplit("@", 1)[-1] if "@" in reference else ""
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", revision):
        return revision.lower()
    return ""


def _wrapper_records(root: Path, directives: list[dict]) -> list[dict]:
    wrappers: list[dict] = []
    for directive in directives:
        if directive.get("directive") != "wrapper" or not directive.get("declaration"):
            continue
        reference = str(directive["declaration"])
        scheme = _remote_scheme(reference)
        version = _version_identity(reference)
        record = {
            "source": directive["source"],
            "line": directive["line"],
            "reference": reference,
            "scheme": scheme
            or ("file" if reference.startswith("file://") else "registry"),
            "version": version,
        }
        local_candidate = (
            reference.startswith("file://")
            or Path(reference).is_absolute()
            or reference.startswith(("./", "../"))
            or (root / reference).exists()
        )
        if local_candidate and not scheme:
            material = _bounded_artifact_layer(root, reference, "snakemake-wrapper")
            record["material"] = material
            immutable = bool(material.get("content_fingerprint"))
            identity_status = "local-content" if immutable else "missing-local-wrapper"
        else:
            immutable = bool(version)
            identity_status = (
                "version-pinned" if immutable else "mutable-remote-reference"
            )
        record.update(
            {
                "immutable": immutable,
                "identity_status": identity_status,
                "identity_fingerprint": _identity_fingerprint(
                    {
                        "reference": redact_text(reference),
                        "version": version,
                        "material": record.get("material"),
                    }
                ),
            }
        )
        wrappers.append(record)
    return wrappers


def _container_records(root: Path, directives: list[dict]) -> list[dict]:
    containers: list[dict] = []
    for directive in directives:
        if directive.get("directive") not in {"container", "singularity"}:
            continue
        if not directive.get("declaration"):
            continue
        reference = str(directive["declaration"])
        scheme = _remote_scheme(reference)
        digest_match = re.search(r"@(sha256:[0-9a-fA-F]{64})(?:$|[?#])", reference)
        digest_pin = digest_match.group(1).lower() if digest_match else ""
        last_component = reference.split("/", 2)[-1].rsplit("/", 1)[-1]
        tag = ""
        if not digest_pin and ":" in last_component:
            tag = last_component.rsplit(":", 1)[-1]
        record = {
            "directive": directive["directive"],
            "source": directive["source"],
            "line": directive["line"],
            "reference": reference,
            "scheme": scheme
            or ("file" if reference.startswith("file://") else "local"),
            "digest_pin": digest_pin,
            "tag": tag,
        }
        if scheme:
            immutable = bool(digest_pin)
            identity_status = (
                "pinned-digest" if immutable else "mutable-remote-reference"
            )
        else:
            material = _bounded_artifact_layer(
                root, reference, "snakemake-container-image"
            )
            record["material"] = material
            immutable = bool(material.get("content_fingerprint"))
            identity_status = (
                "local-content" if immutable else "missing-local-container"
            )
        record.update(
            {
                "immutable": immutable,
                "identity_status": identity_status,
                "identity_fingerprint": _identity_fingerprint(
                    {
                        "reference": redact_text(reference),
                        "digest_pin": digest_pin,
                        "material": record.get("material"),
                    }
                ),
            }
        )
        containers.append(record)
    return containers


def _snakemake_cli(argv: list[str]) -> tuple[list[str], list[str], list[str]]:
    start = next(
        (
            index
            for index, token in enumerate(argv)
            if Path(str(token)).name == "snakemake"
        ),
        -1,
    )
    tokens = [str(token) for token in (argv[start + 1 :] if start >= 0 else argv)]
    configfiles: list[str] = []
    overrides: list[str] = []
    profiles: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--configfile="):
            configfiles.append(token.split("=", 1)[1])
        elif token == "--configfile" and index + 1 < len(tokens):
            index += 1
            configfiles.append(tokens[index])
        elif token.startswith("--configfiles="):
            configfiles.extend(
                part for part in token.split("=", 1)[1].split(",") if part
            )
        elif token == "--configfiles":
            index += 1
            while index < len(tokens) and not tokens[index].startswith("-"):
                configfiles.append(tokens[index])
                index += 1
            index -= 1
        elif token.startswith("--config="):
            value = token.split("=", 1)[1]
            if value:
                overrides.append(value)
        elif token == "--config":
            index += 1
            while index < len(tokens) and not tokens[index].startswith("-"):
                overrides.append(tokens[index])
                index += 1
            index -= 1
        elif token.startswith(("--profile=", "--workflow-profile=")):
            profiles.append(token.split("=", 1)[1])
        elif token in {"--profile", "--workflow-profile"} and index + 1 < len(tokens):
            index += 1
            profiles.append(tokens[index])
        index += 1
    return configfiles, overrides, profiles


def _config_material(
    root: Path,
    declaration: str,
    *,
    source: Path | None = None,
    kind: str = "snakemake-config-layer",
) -> dict:
    if _remote_scheme(declaration):
        return {
            "kind": kind,
            "path": declaration,
            "exists": False,
            "size": 0,
            "sha256": "",
            "resolution_status": "remote-config-unfetched",
        }
    target = _directive_local_target(root, source or root / "Snakefile", declaration)
    material = _file_layer(root, str(target), kind)
    material["resolution_status"] = (
        "resolved-static-local-file"
        if material["exists"]
        else "missing-static-local-file"
    )
    return material


def _profile_configuration(root: Path, profile: str) -> tuple[dict | None, dict]:
    if not profile:
        return None, {}
    candidate = Path(profile)
    profile_dir = (
        candidate
        if candidate.is_absolute()
        else root
        / (
            candidate
            if candidate.parts[:1] == ("profiles",)
            else Path("profiles") / candidate
        )
    )
    for name in ("config.yaml", "config.yml"):
        path = profile_dir / name
        if not path.is_file():
            continue
        material = _file_layer(root, str(path), "snakemake-profile-settings")
        try:
            parsed = (
                {"__benchtop_static_parse_error__": True}
                if path.stat().st_size > MAX_CONFIG_PARSE_BYTES
                else yaml.safe_load(path.read_text(encoding="utf-8"))
            )
        except Exception:
            parsed = {"__benchtop_static_parse_error__": True}
        if not isinstance(parsed, dict):
            parsed = {"__benchtop_static_parse_error__": True}
        return material, parsed
    return _file_layer(
        root, str(profile_dir / "config.yaml"), "snakemake-profile-settings"
    ), {}


def _normal_config_path(root: Path, raw: str) -> str:
    if not raw:
        return ""
    if _remote_scheme(raw):
        return raw
    return _display_path(root, _declared_path(root, raw) or root / raw)


def _normal_profile_path(root: Path, raw: str) -> str:
    if not raw:
        return ""
    candidate = Path(raw)
    if not candidate.is_absolute() and candidate.parts[:1] != ("profiles",):
        candidate = Path("profiles") / candidate
    return _display_path(
        root, candidate if candidate.is_absolute() else root / candidate
    )


def _configuration_surfaces(
    pipeline: Pipeline,
    *,
    declared_configfile: str,
    study_configfile: str,
    profile: str,
    argv: list[str],
    directives: list[dict],
) -> tuple[list[dict], list[dict], dict]:
    """Return low-to-high config layers, literal overrides, and authority status."""
    root = pipeline.path.resolve()
    layers: list[dict] = []
    sequence = 0

    def add_layer(
        declaration: str,
        origin: str,
        precedence_rank: int,
        *,
        source: str = "",
        line: int = 0,
        material: dict | None = None,
        layer_type: str = "config-data",
        resolution_status: str = "",
    ) -> None:
        nonlocal sequence
        record = {
            "sequence": sequence,
            "origin": origin,
            "layer_type": layer_type,
            "precedence_rank": precedence_rank,
            "declaration": declaration,
            "source": source,
            "line": line,
            "material": material or _config_material(root, declaration),
        }
        material_status = record["material"].get("resolution_status") or (
            "resolved-static-local-file"
            if record["material"].get("exists")
            else "missing-static-local-file"
        )
        record["resolution_status"] = resolution_status or str(material_status)
        layers.append(record)
        sequence += 1

    workflow_configs: list[str] = []
    workflow_config_paths: list[str] = []
    for directive in directives:
        if directive.get("directive") != "configfile" or not directive.get(
            "declaration"
        ):
            continue
        declaration = str(directive["declaration"])
        workflow_configs.append(declaration)
        directive_material = dict(
            directive.get("material") or _config_material(root, declaration)
        )
        workflow_config_paths.append(str(directive_material.get("path") or declaration))
        add_layer(
            declaration,
            "workflow-configfile",
            100,
            source=str(directive.get("source") or ""),
            line=int(directive.get("line") or 0),
            material=directive_material,
            resolution_status=str(directive.get("resolution_status") or ""),
        )

    profile_material, profile_data = _profile_configuration(root, profile)
    profile_parse_error = bool(
        profile_data.pop("__benchtop_static_parse_error__", False)
    )
    profile_configfiles: list[str] = []
    if profile_material is not None:
        add_layer(
            str(profile_material.get("path") or profile),
            "profile-settings",
            150,
            material=profile_material,
            layer_type="cli-settings",
        )
        raw_profile_files = profile_data.get(
            "configfiles", profile_data.get("configfile", [])
        )
        if isinstance(raw_profile_files, str):
            profile_configfiles = [raw_profile_files]
        elif isinstance(raw_profile_files, list):
            profile_configfiles = [str(value) for value in raw_profile_files if value]
        for declaration in profile_configfiles:
            add_layer(declaration, "profile-configfile", 200)

    cli_configfiles, cli_overrides, cli_profiles = _snakemake_cli(argv)
    if (
        not workflow_configs
        and not cli_configfiles
        and not declared_configfile
        and study_configfile
    ):
        add_layer(
            study_configfile,
            "benchtop-discovered-default",
            50,
            resolution_status="discovered-default-not-command-declared",
        )
    for declaration in cli_configfiles:
        add_layer(declaration, "cli-configfile", 300)
    if declared_configfile and declared_configfile not in cli_configfiles:
        add_layer(
            declared_configfile,
            "launch-control-configfile",
            300,
            resolution_status="declared-launch-config-not-present-in-command",
        )

    profile_overrides = profile_data.get("config")
    profile_override_values: list[str] = []
    if isinstance(profile_overrides, dict):
        for key in sorted(profile_overrides):
            profile_override_values.append(f"{key}={profile_overrides[key]}")
    all_overrides = [*profile_override_values, *cli_overrides]
    override_records: list[dict] = []
    for index, declaration in enumerate(all_overrides):
        origin = (
            "profile-config-override"
            if index < len(profile_override_values)
            else "cli-config-override"
        )
        override_records.append(
            {
                "order": index,
                "origin": origin,
                "precedence_rank": 400 if origin.startswith("profile") else 500,
                "declaration": declaration,
                "identity_fingerprint": _identity_fingerprint(redact_text(declaration)),
                "resolution_status": "literal-override-untyped",
            }
        )

    layers.sort(key=lambda record: (record["precedence_rank"], record["sequence"]))
    for order, layer in enumerate(layers):
        layer["order"] = order
    override_records.sort(
        key=lambda record: (record["precedence_rank"], record["order"])
    )
    for order, override in enumerate(override_records):
        override["order"] = order

    study_path = _normal_config_path(root, study_configfile)
    launch_path = _normal_config_path(root, declared_configfile)
    cli_paths = [_normal_config_path(root, value) for value in cli_configfiles]
    workflow_paths = [
        _normal_config_path(root, value) for value in workflow_config_paths
    ]
    issues: list[str] = []
    if profile_parse_error:
        issues.append("profile-settings-not-statically-readable")
    if study_path and launch_path and study_path != launch_path:
        issues.append("study-config-differs-from-launch-config")
    if launch_path and launch_path not in cli_paths:
        issues.append("launch-config-differs-from-command-config")
    profile_paths = [_normal_config_path(root, value) for value in profile_configfiles]
    launch_profile = _normal_profile_path(root, profile)
    command_profiles = [_normal_profile_path(root, value) for value in cli_profiles]
    if launch_profile and launch_profile not in command_profiles:
        issues.append("launch-profile-differs-from-command-profile")
    if command_profiles and not launch_profile:
        issues.append("command-profile-not-declared-by-launch")
    if study_path and study_path not in {*cli_paths, *workflow_paths, *profile_paths}:
        issues.append("study-config-not-declared-by-command-or-static-workflow")
    authority = {
        "study_configfile": study_path,
        "launch_configfile": launch_path,
        "command_configfiles": cli_paths,
        "workflow_configfiles": workflow_paths,
        "profile_configfiles": profile_paths,
        "launch_profile": launch_profile,
        "command_profiles": command_profiles,
        "aligned": not issues,
        "issues": issues,
        "resolution_status": "aligned-static-authority"
        if not issues
        else "config-authority-conflict",
    }
    return layers, override_records, authority


def _reference_values(value: Any, key_path: tuple[str, ...] = (), active: bool = False):
    if isinstance(value, dict):
        for key in sorted(value, key=lambda item: str(item)):
            key_text = str(key)
            yield from _reference_values(
                value[key],
                (*key_path, key_text),
                active or bool(REFERENCE_KEY_RE.search(key_text)),
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _reference_values(child, (*key_path, str(index)), active)
    elif active and isinstance(value, str) and value.strip():
        yield ".".join(key_path), value.strip()


def _looks_like_reference_path(root: Path, raw: str, key_path: str, base: Path) -> bool:
    if _remote_scheme(raw) or raw.startswith("file://"):
        return True
    if any(marker in raw for marker in ("/", "\\", "{", "}", "$")):
        return True
    if REFERENCE_SUFFIX_RE.search(raw):
        return True
    if (root / raw).exists() or (base / raw).exists():
        return True
    return bool(re.search(r"(?:index|bowtie|bwa|star|salmon|kallisto)", key_path, re.I))


def _reference_record(
    root: Path,
    *,
    key_path: str,
    declaration: str,
    config_order: int,
    config_origin: str,
    config_path: str,
    base: Path,
) -> dict:
    record = {
        "key_path": key_path,
        "declaration": declaration,
        "config_order": config_order,
        "config_origin": config_origin,
        "config_path": config_path,
    }
    scheme = _remote_scheme(declaration)
    if scheme:
        record.update(
            {
                "scheme": scheme,
                "resolution_status": "remote-reference-identity-only",
                "identity_fingerprint": _identity_fingerprint(
                    {
                        "key_path": key_path,
                        "declaration": redact_text(declaration),
                    }
                ),
            }
        )
        return record
    if any(marker in declaration for marker in ("{", "}", "$(", "${")):
        record.update(
            {
                "scheme": "",
                "resolution_status": "unresolved-reference-template",
                "identity_fingerprint": _identity_fingerprint(
                    {
                        "key_path": key_path,
                        "declaration": redact_text(declaration),
                    }
                ),
            }
        )
        return record
    allow_prefix = bool(
        re.search(r"(?:index|bowtie|bwa|star|salmon|kallisto)", key_path, re.I)
    )
    prefix_suffixes: tuple[str, ...] = ()
    if re.search(r"bowtie", key_path, re.I):
        prefix_suffixes = (".bt2", ".bt2l")
    elif re.search(r"bwa", key_path, re.I):
        prefix_suffixes = (".amb", ".ann", ".bwt", ".pac", ".sa")
    material = _bounded_artifact_layer(
        root,
        declaration,
        "reference-artifact",
        base=base,
        allow_prefix=allow_prefix,
        prefix_suffixes=prefix_suffixes,
    )
    material["allow_prefix"] = allow_prefix
    material["prefix_suffixes"] = list(prefix_suffixes)
    record.update(
        {
            "scheme": "file",
            "material": material,
            "resolution_status": material["resolution_status"],
            "identity_fingerprint": _identity_fingerprint(
                {
                    "key_path": key_path,
                    "declaration": redact_text(declaration),
                    "material": material,
                }
            ),
        }
    )
    return record


def _reference_artifacts(
    root: Path,
    layers: list[dict],
    overrides: list[dict],
) -> tuple[list[dict], bool, list[str]]:
    records: list[dict] = []
    truncated = False
    issues: list[str] = []
    for layer in layers:
        if layer.get("layer_type") != "config-data":
            continue
        material = layer.get("material") or {}
        if not material.get("exists"):
            continue
        config_path = str(material.get("path") or "")
        path = _declared_path(root, config_path)
        if not path or not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_CONFIG_PARSE_BYTES:
                issues.append(
                    f"config-too-large-for-static-reference-scan:{config_path}"
                )
                continue
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            issues.append(f"config-unreadable-for-static-reference-scan:{config_path}")
            continue
        if not isinstance(parsed, dict):
            continue
        for key_path, declaration in _reference_values(parsed):
            if not _looks_like_reference_path(root, declaration, key_path, path.parent):
                continue
            if len(records) >= MAX_REFERENCE_ARTIFACTS:
                truncated = True
                break
            records.append(
                _reference_record(
                    root,
                    key_path=key_path,
                    declaration=declaration,
                    config_order=int(layer.get("order") or 0),
                    config_origin=str(layer.get("origin") or ""),
                    config_path=config_path,
                    base=path.parent,
                )
            )
        if truncated:
            break

    if not truncated:
        for override in overrides:
            declaration = str(override.get("declaration") or "")
            if "=" not in declaration:
                continue
            key_path, raw = declaration.split("=", 1)
            if not REFERENCE_KEY_RE.search(key_path):
                continue
            if not _looks_like_reference_path(root, raw, key_path, root):
                continue
            if len(records) >= MAX_REFERENCE_ARTIFACTS:
                truncated = True
                break
            records.append(
                _reference_record(
                    root,
                    key_path=key_path,
                    declaration=raw,
                    config_order=10_000 + int(override.get("order") or 0),
                    config_origin=str(override.get("origin") or ""),
                    config_path="<literal-override>",
                    base=root,
                )
            )

    latest: dict[str, int] = {}
    for index, record in enumerate(records):
        latest[record["key_path"]] = index
    for index, record in enumerate(records):
        record["effective_for_key"] = latest[record["key_path"]] == index
        record["order"] = index
    return records, truncated, list(dict.fromkeys(issues))


def _material_manifests(pipeline: Pipeline) -> tuple[list[dict], list[dict], bool]:
    root = pipeline.path.resolve()
    source_paths: set[Path] = set()
    environment_paths: set[Path] = set()

    def add(path: Path, *, environment: bool = False) -> None:
        try:
            resolved = path.resolve()
            if not resolved.is_file() or resolved.is_symlink():
                return
        except OSError:
            return
        (environment_paths if environment else source_paths).add(resolved)

    if pipeline.snakefile:
        add(root / pipeline.snakefile)
    for path in root.iterdir():
        if not path.is_file():
            continue
        if path.name in ENVIRONMENT_NAMES:
            add(path, environment=True)
        elif path.suffix in WORKFLOW_SUFFIXES or path.name == "Snakefile":
            add(path)
    for folder_name in WORKFLOW_DIRS:
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_file() and (
                path.suffix in WORKFLOW_SUFFIXES or path.name in ENVIRONMENT_NAMES
            ):
                add(path, environment=path.name in ENVIRONMENT_NAMES)
    envs = root / "envs"
    if envs.is_dir():
        for pattern in ("*.yml", "*.yaml", "*.lock", "*.txt"):
            for path in envs.rglob(pattern):
                add(path, environment=True)
    for folder_name in ("containers", "container"):
        folder = root / folder_name
        if folder.is_dir():
            for path in folder.rglob("*"):
                if path.is_file() and (
                    path.name in {"Dockerfile", "Singularity", "Apptainer"}
                    or path.suffix.lower()
                    in {".def", ".yaml", ".yml", ".json", ".toml"}
                ):
                    add(path, environment=True)

    scanned: set[Path] = set()
    queue = sorted(source_paths, key=lambda path: path.as_posix())
    while queue and len(scanned) < MAX_SOURCE_FILES * 2:
        source = queue.pop(0)
        if source in scanned or (
            source.suffix not in {".smk", ".py", ".r", ".R"}
            and source.name != "Snakefile"
        ):
            continue
        scanned.add(source)
        try:
            if source.stat().st_size > 2_000_000:
                continue
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for directive, raw in STATIC_DIRECTIVE_RE.findall(text):
            if "://" in raw or "{" in raw or "}" in raw:
                continue
            candidates = (source.parent / raw, root / raw)
            target = next(
                (
                    candidate.resolve()
                    for candidate in candidates
                    if candidate.is_file()
                ),
                None,
            )
            if target is None:
                continue
            is_environment = directive == "conda"
            before = len(environment_paths if is_environment else source_paths)
            add(target, environment=is_environment)
            if (
                not is_environment
                and len(source_paths) > before
                and target not in scanned
            ):
                queue.append(target)

    all_paths = sorted(
        source_paths | environment_paths, key=lambda path: path.as_posix()
    )
    truncated = len(all_paths) > MAX_SOURCE_FILES
    allowed = set(all_paths[:MAX_SOURCE_FILES])
    sources = [
        _file_layer(root, str(path), "workflow-source")
        for path in sorted(source_paths & allowed, key=lambda value: value.as_posix())
    ]
    environments = [
        _file_layer(root, str(path), "environment-declaration")
        for path in sorted(
            environment_paths & allowed, key=lambda value: value.as_posix()
        )
    ]
    return sources, environments, truncated


def _driver_environment(
    resolved_env: str,
    prefix_hint: str = "",
) -> tuple[dict, list[dict]]:
    prefix: Path | None = None
    if prefix_hint and Path(prefix_hint).is_dir():
        prefix = Path(prefix_hint).resolve()
    elif resolved_env:
        candidate = Path(resolved_env).expanduser()
        if candidate.is_dir():
            prefix = candidate.resolve()
        else:
            record = pipeline_tools.env_index().get(resolved_env, {})
            raw_prefix = str(record.get("path") or "")
            if raw_prefix:
                prefix = Path(raw_prefix).resolve()
    executable = (
        prefix / "bin" / "snakemake"
        if prefix and (prefix / "bin" / "snakemake").is_file()
        else Path(shutil.which("snakemake") or "").resolve()
        if shutil.which("snakemake")
        else None
    )
    executable_record = (
        _file_layer(prefix or Path("/"), str(executable), "snakemake-executable")
        if executable
        else {
            "kind": "snakemake-executable",
            "path": "",
            "exists": False,
            "size": 0,
            "sha256": "",
        }
    )
    if executable and not prefix:
        executable_record["path"] = executable.as_posix()
    packages: list[dict] = []
    conda_meta = prefix / "conda-meta" if prefix else None
    if conda_meta and conda_meta.is_dir():
        for path in sorted(conda_meta.glob("*.json"))[:MAX_PACKAGE_RECORDS]:
            packages.append(_file_layer(prefix, str(path), "conda-package-record"))
    driver = {
        "kind": "conda" if prefix else "host",
        "name": resolved_env,
        "prefix": prefix.as_posix() if prefix else "",
        "executable": executable_record,
        "packages_digest": _aggregate_layers(packages),
        "packages_truncated": bool(
            conda_meta
            and conda_meta.is_dir()
            and len(list(conda_meta.glob("*.json"))) > MAX_PACKAGE_RECORDS
        ),
        "resolution_status": (
            "resolved-prefix-package-manifest" if prefix else "host-executable-only"
        ),
    }
    return driver, packages


def _rehash_layers(root: Path, records: list[dict]) -> list[dict]:
    return [
        _file_layer(
            root, str(record.get("path") or ""), str(record.get("kind") or "file")
        )
        for record in records
    ]


def _current_config_material(root: Path, expected: Mapping[str, Any]) -> dict:
    path = str(expected.get("path") or "")
    kind = str(expected.get("kind") or "snakemake-config-layer")
    if _remote_scheme(path):
        return {
            "kind": kind,
            "path": path,
            "exists": False,
            "size": 0,
            "sha256": "",
            "resolution_status": "remote-config-unfetched",
        }
    current = _file_layer(root, path, kind)
    if "resolution_status" in expected:
        current["resolution_status"] = (
            "resolved-static-local-file"
            if current["exists"]
            else "missing-static-local-file"
        )
    return current


def _config_materials_changed(root: Path, layers: list[dict]) -> bool:
    for layer in layers:
        expected = layer.get("material")
        if not isinstance(expected, Mapping):
            continue
        if _canonical(_current_config_material(root, expected)) != _canonical(
            dict(expected)
        ):
            return True
    return False


def _bounded_materials_changed(root: Path, records: list[dict]) -> bool:
    for record in records:
        expected = record.get("material")
        if not isinstance(expected, Mapping):
            continue
        raw = str(expected.get("declared_path") or expected.get("path") or "")
        current = _bounded_artifact_layer(
            root,
            raw,
            str(expected.get("kind") or "artifact"),
            allow_prefix=bool(expected.get("allow_prefix")),
            prefix_suffixes=tuple(expected.get("prefix_suffixes") or ()),
        )
        if "allow_prefix" in expected:
            current["allow_prefix"] = bool(expected.get("allow_prefix"))
            current["prefix_suffixes"] = list(expected.get("prefix_suffixes") or [])
        if _canonical(current) != _canonical(dict(expected)):
            return True
    return False


def verify_record(record: Mapping[str, Any]) -> list[str]:
    """Return material drift errors for a persisted redacted RunPlan record."""
    errors: list[str] = []
    plan = record.get("plan") if isinstance(record, Mapping) else None
    if not isinstance(plan, dict):
        return ["RunPlan record has no plan payload"]
    projection = (
        "sha256:" + hashlib.sha256(_canonical(plan).encode("utf-8")).hexdigest()
    )
    if projection != record.get("projection_digest"):
        errors.append("RunPlan projection digest does not match the stored payload")
    root_raw = str((plan.get("pipeline") or {}).get("root") or "")
    root = Path(root_raw).resolve() if root_raw else Path("/")
    if not root.is_dir():
        return [*errors, f"pipeline root is unavailable: {root_raw}"]
    pipeline = pipeline_tools._discover_one(root)
    if pipeline is None:
        return [*errors, f"pipeline can no longer be discovered at {root}"]

    current_sources, current_environments, truncated = _material_manifests(pipeline)
    workflow = plan.get("workflow") or {}
    if _canonical(current_sources) != _canonical(workflow.get("sources") or []):
        errors.append("workflow source manifest changed after RunPlan creation")
    if truncated != bool(workflow.get("sources_truncated")):
        errors.append("workflow source manifest truncation state changed")
    environment = plan.get("environment") or {}
    if _canonical(current_environments) != _canonical(
        environment.get("declarations") or []
    ):
        errors.append("environment declaration manifest changed after RunPlan creation")

    configuration = plan.get("configuration") or {}
    expected_layers = list(configuration.get("layers") or [])
    if _canonical(_rehash_layers(root, expected_layers)) != _canonical(expected_layers):
        errors.append("configuration layers changed after RunPlan creation")
    ordered_layers = list(configuration.get("ordered_layers") or [])
    if ordered_layers and _config_materials_changed(root, ordered_layers):
        errors.append(
            "ordered configuration precedence materials changed after RunPlan creation"
        )

    wrappers = list(environment.get("wrappers") or [])
    if wrappers and _bounded_materials_changed(root, wrappers):
        errors.append("local wrapper material changed after RunPlan creation")
    containers = list(environment.get("containers") or [])
    if containers and _bounded_materials_changed(root, containers):
        errors.append("local container material changed after RunPlan creation")
    references = list((plan.get("inputs") or {}).get("reference_artifacts") or [])
    if references and _bounded_materials_changed(root, references):
        errors.append("reference artifact material changed after RunPlan creation")

    expected_driver = environment.get("driver") or {}
    current_driver, current_packages = _driver_environment(
        str(expected_driver.get("name") or ""),
        str(expected_driver.get("prefix") or ""),
    )
    if _canonical(current_driver) != _canonical(expected_driver):
        errors.append("driver environment changed after RunPlan creation")
    if _canonical(current_packages) != _canonical(environment.get("packages") or []):
        errors.append("installed package manifest changed after RunPlan creation")

    execution = plan.get("execution") or {}
    configfile = str(execution.get("configfile") or "")
    if not configfile and expected_layers:
        configfile = str(expected_layers[0].get("path") or "")
    study_plan = plan.get("study") or {}
    try:
        from . import study as study_tools

        report = study_tools.audit(
            pipeline,
            configfile=configfile,
            sheet="",
            roles=dict(study_plan.get("roles") or {}),
        )
        if report.get("fingerprint", "") != study_plan.get("fingerprint", ""):
            errors.append(
                "study, design, or input fingerprint changed after RunPlan creation"
            )
        expected_sheet = str((study_plan.get("sheet") or {}).get("path") or "")
        current_sheet = str((report.get("selected") or {}).get("path") or "")
        if current_sheet != expected_sheet:
            errors.append(
                "authoritative study selection changed after RunPlan creation"
            )
    except Exception as exc:
        errors.append(f"study verification failed: {exc}")
    return errors


def verify_file(path: Path) -> list[str]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"RunPlan record is unreadable: {exc}"]
    return verify_record(record)


def _configuration_layers(
    pipeline: Pipeline, configfile: str, profile: str
) -> list[dict]:
    layers: list[dict] = []
    selected = configfile or next(iter(pipeline.configs), "")
    if selected:
        layers.append(_file_layer(pipeline.path, selected, "run-config"))
    if profile:
        for name in ("config.yaml", "config.yml"):
            candidate = f"profiles/{profile}/{name}"
            if (pipeline.path / candidate).is_file():
                layers.append(_file_layer(pipeline.path, candidate, "profile"))
                break
    return layers


def _output_roots(pipeline: Pipeline, configfile: str) -> list[str]:
    roots: set[str] = set()
    selected = configfile or next(iter(pipeline.configs), "")
    path = _declared_path(pipeline.path, selected)
    if path and path.is_file():
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(config, dict) and config.get("results_dir"):
                raw = str(config["results_dir"])
                result_path = Path(raw).expanduser()
                if not result_path.is_absolute():
                    result_path = pipeline.path / result_path
                roots.add(_display_path(pipeline.path, result_path))
        except Exception:
            pass
    roots.update(("results", "result", "output", "outputs", "analysis"))
    return sorted(roots)


def redact_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(
        r"(?i)(?<![A-Za-z0-9._-])[^/@:\s]+@(?=[A-Za-z0-9.-]+:)",
        "<redacted>@",
        text,
    )
    assignment = re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*)=(.*)$", text, re.S)
    if assignment and SECRET_RE.search(assignment.group(1)):
        return assignment.group(1) + "=<redacted>"
    header = re.match(r"^([^:\s]+):\s*(.*)$", text, re.S)
    if header and SECRET_RE.search(header.group(1)):
        return header.group(1) + ": <redacted>"
    text = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1<redacted>@", text)
    text = re.sub(
        r"(?i)([?&](?:token|secret|password|passwd|credential|api[_-]?key)=)[^&\s]+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)(\b(?:token|secret|password|passwd|credential|api[_-]?key)\b\s*=\s*)[^\s,]+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)(--?(?:token|secret|password|passwd|credential|api[-_]?key|authorization|cookie)\s+)[^\s]+",
        r"\1<redacted>",
        text,
    )
    return text


def redact_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for raw in argv:
        token = str(raw)
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        if token.startswith("-") and SECRET_RE.search(token):
            if "=" in token:
                redacted.append(token.split("=", 1)[0] + "=<redacted>")
            else:
                redacted.append(token)
                hide_next = True
            continue
        redacted.append(redact_text(token))
    return redacted


def redact_command(argv: list[str]) -> str:
    """Render a command only after redacting its already-tokenized arguments.

    Redacting the rendered shell string is unsafe for quoted or multiword
    values: a pattern such as ``--token 'two words'`` can otherwise redact only
    the first word.  Keep the argument boundaries until every sensitive value
    has been replaced, then quote the safe projection for display/persistence.
    """
    return shlex.join(redact_argv([str(value) for value in argv]))


def redact_command_text(value: str) -> str:
    """Best-effort safe projection for a legacy serialized command string."""
    text = str(value or "")
    if not text:
        return ""
    try:
        return redact_command(shlex.split(text))
    except ValueError:
        # Malformed legacy text has no reliable argument boundary.  Retain the
        # conservative text redactor rather than returning the original value.
        return redact_text(text)


def redact_mapping(value: Any, key: str = "") -> Any:
    if key and SECRET_RE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(child_key): redact_mapping(child, str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(child) for child in value]
    if isinstance(value, tuple):
        return [redact_mapping(child) for child in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


@dataclass(frozen=True, slots=True)
class RunPlan:
    """A frozen canonical JSON payload whose digest is its durable identity."""

    canonical_json: str
    digest: str

    @classmethod
    def create(cls, payload: Mapping[str, Any]) -> "RunPlan":
        canonical = _canonical(dict(payload))
        digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(canonical_json=canonical, digest=digest)

    @property
    def payload(self) -> dict:
        return json.loads(self.canonical_json)

    @property
    def argv(self) -> list[str]:
        return list(self.payload.get("command", {}).get("argv", []))

    @property
    def cwd(self) -> str:
        return str(self.payload.get("command", {}).get("cwd", ""))

    def public_payload(self) -> dict:
        payload = self.payload
        command = payload.get("command", {})
        command["argv"] = redact_argv(list(command.get("argv", [])))
        return redact_mapping(payload)

    def public_command(self) -> str:
        return redact_command(self.argv)

    def record(self) -> dict:
        public = self.public_payload()
        return {
            "schema_version": SCHEMA_VERSION,
            "digest": self.digest,
            "digest_scope": "private-canonical-plan",
            "redacted": True,
            "projection_digest": "sha256:"
            + hashlib.sha256(_canonical(public).encode("utf-8")).hexdigest(),
            "plan": public,
        }


def build(
    pipeline: Pipeline,
    *,
    launch: Mapping[str, Any],
    argv: list[str],
    study_report: Mapping[str, Any],
    resolved_env: str,
    executor: str = "local",
    scheduler: Mapping[str, Any] | None = None,
) -> RunPlan:
    """Materialize RunPlan v1 from already-resolved launch inputs."""
    declared_configfile = str(launch.get("configfile") or "")
    study_configfile = str(study_report.get("configfile") or "")
    configfile = declared_configfile or study_configfile
    profile = str(launch.get("profile") or "")
    config_layers = _configuration_layers(pipeline, configfile, profile)
    workflow = _file_layer(pipeline.path, pipeline.snakefile or "", "entrypoint")
    workflow_sources, environment_files, source_manifest_truncated = (
        _material_manifests(pipeline)
    )
    directives, directives_truncated = _scan_snakemake_directives(pipeline)
    include_targets = [
        directive
        for directive in directives
        if directive.get("directive") in {"include", "snakefile"}
    ]
    unresolved_directives = [
        directive
        for directive in directives
        if str(directive.get("resolution_status") or "").startswith("unresolved-")
    ]
    wrappers = _wrapper_records(pipeline.path.resolve(), directives)
    containers = _container_records(pipeline.path.resolve(), directives)
    ordered_config_layers, config_overrides, config_authority = _configuration_surfaces(
        pipeline,
        declared_configfile=declared_configfile,
        study_configfile=study_configfile,
        profile=profile,
        argv=[str(value) for value in argv],
        directives=directives,
    )
    reference_artifacts, references_truncated, reference_resolution_issues = (
        _reference_artifacts(
            pipeline.path.resolve(),
            ordered_config_layers,
            config_overrides,
        )
    )
    driver_environment, package_records = _driver_environment(resolved_env)
    workflow_fingerprint = _identity_fingerprint(
        {
            "sources": workflow_sources or [workflow],
            "directives": redact_mapping(directives),
            "sources_truncated": source_manifest_truncated,
            "directives_truncated": directives_truncated,
        }
    ).split(":", 1)[1]
    environment_core = {
        "driver": driver_environment,
        "declarations": environment_files,
        "packages": package_records,
        "wrappers": wrappers,
        "containers": containers,
    }
    environment_fingerprint = hashlib.sha256(
        _canonical(redact_mapping(environment_core)).encode("utf-8")
    ).hexdigest()
    selected = dict(study_report.get("selected") or {})
    roles = dict(study_report.get("roles") or {})
    design = dict(study_report.get("design") or {})
    scheduler_payload = dict(scheduler or {}) if executor == "slurm" else None
    configuration_fingerprint = _identity_fingerprint(
        {
            "ordered_layers": redact_mapping(ordered_config_layers),
            "overrides": redact_mapping(config_overrides),
            "authority": redact_mapping(config_authority),
        }
    ).split(":", 1)[1]
    references_fingerprint = _identity_fingerprint(
        {
            "artifacts": redact_mapping(reference_artifacts),
            "truncated": references_truncated,
            "issues": reference_resolution_issues,
        }
    ).split(":", 1)[1]
    target = str(launch.get("target") or "")
    target_in_command = not target or target in {str(value) for value in argv}
    completeness_issues: list[str] = []
    if source_manifest_truncated:
        completeness_issues.append("workflow-source-manifest-truncated")
    if directives_truncated:
        completeness_issues.append("snakemake-directive-scan-truncated")
    if unresolved_directives:
        completeness_issues.append(
            f"unresolved-dynamic-directives:{len(unresolved_directives)}"
        )
    if references_truncated:
        completeness_issues.append("reference-artifact-scan-truncated")
    completeness_issues.extend(reference_resolution_issues)
    completeness_issues.extend(
        f"config-authority:{issue}" for issue in config_authority["issues"]
    )
    if not target_in_command:
        completeness_issues.append("launch-target-not-present-in-command")
    completeness_issues.extend(
        f"missing-config-layer:{layer['declaration']}"
        for layer in ordered_config_layers
        if layer["layer_type"] == "config-data"
        and not bool((layer.get("material") or {}).get("exists"))
    )
    completeness_issues.extend(
        f"missing-profile-settings:{layer['declaration']}"
        for layer in ordered_config_layers
        if layer["layer_type"] == "cli-settings"
        and not bool((layer.get("material") or {}).get("exists"))
    )
    completeness_issues.extend(
        f"missing-include-target:{record['source']}:{record['line']}"
        for record in include_targets
        if record.get("resolution_status") == "missing-static-local-file"
    )
    completeness_issues.extend(
        f"mutable-wrapper:{record['source']}:{record['line']}"
        for record in wrappers
        if not record["immutable"]
    )
    completeness_issues.extend(
        f"mutable-container:{record['source']}:{record['line']}"
        for record in containers
        if not record["immutable"]
    )
    completeness_issues.extend(
        f"bounded-wrapper-material:{record['source']}:{record['line']}"
        for record in wrappers
        if bool((record.get("material") or {}).get("files_truncated"))
    )
    completeness_issues.extend(
        f"bounded-container-material:{record['source']}:{record['line']}"
        for record in containers
        if bool((record.get("material") or {}).get("files_truncated"))
    )
    completeness_issues.extend(
        f"unresolved-reference:{record['key_path']}"
        for record in reference_artifacts
        if record["resolution_status"].startswith(
            ("missing-", "unresolved-", "remote-", "unreadable-")
        )
    )
    completeness_issues.extend(
        f"bounded-reference-material:{record['key_path']}"
        for record in reference_artifacts
        if bool((record.get("material") or {}).get("files_truncated"))
    )
    resolution = {
        "complete": not completeness_issues,
        "launch_safe": not completeness_issues,
        "status": "complete-static-materialization"
        if not completeness_issues
        else "bounded-partial",
        "issues": completeness_issues,
        "unresolved_dynamic_count": len(unresolved_directives),
        "source_manifest_truncated": source_manifest_truncated,
        "directives_truncated": directives_truncated,
        "reference_artifacts_truncated": references_truncated,
        "runtime_evaluation_attempted": False,
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "pipeline": {
            "name": pipeline.name,
            "local_name": pipeline.path.name,
            "root": pipeline.path.resolve().as_posix(),
            "engine": pipeline.kind,
        },
        "workflow": {
            "entrypoint": workflow,
            "sources": workflow_sources,
            "sources_truncated": source_manifest_truncated,
            "directives": directives,
            "directives_truncated": directives_truncated,
            "include_targets": include_targets,
            "unresolved_dynamic_directives": unresolved_directives,
            "resolution_status": (
                "bounded-static-complete"
                if not unresolved_directives and not directives_truncated
                else "bounded-static-partial"
            ),
        },
        "configuration": {
            "layers": config_layers,
            "ordered_layers": ordered_config_layers,
            "precedence_order": "low-to-high",
            "literal_overrides": config_overrides,
            "authority": config_authority,
            "fingerprint": configuration_fingerprint,
            "resolution_status": "bounded-static-precedence",
        },
        "study": {
            "sheet": {
                "path": selected.get("path", ""),
                "source": selected.get("source", ""),
                "config_key": selected.get("config_key", ""),
                "external": bool(selected.get("external", False)),
                "authoritative": bool(selected.get("authoritative", False)),
            },
            "roles": roles,
            "design": design,
            "contrasts": [],
            "gate": study_report.get("gate", "unknown"),
            "fingerprint": study_report.get("fingerprint", ""),
            "override": bool(launch.get("study_override", False)),
        },
        "inputs": {
            "count": int(
                (study_report.get("summary") or {}).get("input_files", 0) or 0
            ),
            "fingerprint": study_report.get("inputs_fingerprint", ""),
            "reference_artifacts": reference_artifacts,
            "reference_artifacts_truncated": references_truncated,
            "reference_artifacts_fingerprint": references_fingerprint,
            "reference_resolution_issues": reference_resolution_issues,
        },
        "environment": {
            **environment_core,
            "container": containers[0] if len(containers) == 1 else None,
        },
        "outputs": {
            "roots": _output_roots(pipeline, configfile),
            "resolution_status": "declared-and-conventional-partial",
        },
        "execution": {
            "mode": "dry-run" if launch.get("dryrun", True) else "run",
            "target": target,
            "targets": [target] if target else [],
            "targets_resolution_status": "declared-explicit"
            if target
            else "workflow-default-target",
            "target_present_in_command": target_in_command,
            "configfile": declared_configfile,
            "profile": profile,
            "use_conda": bool(launch.get("use_conda", True)),
            "executor": executor,
        },
        "resources": {
            "cores": max(1, int(launch.get("cores", 1) or 1)),
            "slurm": scheduler_payload,
        },
        "command": {
            "cwd": pipeline.path.resolve().as_posix(),
            "argv": [str(value) for value in argv],
        },
        "resolution": resolution,
        "fingerprints": {
            "workflow": workflow_fingerprint,
            "configuration": configuration_fingerprint,
            "study": study_report.get("fingerprint", ""),
            "inputs": study_report.get("inputs_fingerprint", ""),
            "environment": environment_fingerprint,
            "wrappers": _aggregate_layers(redact_mapping(wrappers)),
            "containers": _aggregate_layers(redact_mapping(containers)),
            "references": references_fingerprint,
        },
    }
    return RunPlan.create(payload)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "verify":
        print(
            "usage: python -m benchtop.runplan verify PLAN_RECORD.json", file=sys.stderr
        )
        return 2
    errors = verify_file(Path(args[1]))
    if errors:
        for error in errors:
            print(f"RunPlan verification failed: {error}", file=sys.stderr)
        return 1
    print(f"RunPlan materials verified: {args[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
