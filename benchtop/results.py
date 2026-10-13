# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Discover pipeline outputs and safely attach external result directories."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from . import fs_policy, settings, store
from .pipelines import Pipeline

_IMG = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_TABLE = {".csv", ".tsv", ".txt"}
_REPORT_HINTS = ("multiqc_report.html", "report.html")
_SKIP_DIRS = {".snakemake", "logs", "benchmarks", "tmp", ".cache"}
_SEARCH_SKIP_DIRS = _SKIP_DIRS | {
    ".benchtop",
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "__pycache__",
    "graphify-out",
    "node_modules",
}

MAX_DIRECTORY_SEARCH_DEPTH = 5
MAX_DIRECTORY_SEARCH_VISITED = 2_500
MAX_DIRECTORY_SEARCH_SECONDS = 0.75
MAX_DIRECTORY_CHILDREN = 1_000
MAX_GALLERY_DIRS_PER_SOURCE = 2_000
MAX_GALLERY_ENTRIES_PER_SOURCE = 50_000
MAX_GALLERY_SECONDS = 2.0
INVENTORY_TTL = 15.0
INVENTORY_LIMIT = 4_000
_INVENTORIES: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_INVENTORY_LOCK = threading.Lock()
_SCAN_LOCKS = [threading.Lock() for _ in range(32)]
_INVENTORY_GENERATION = 0


def invalidate_inventory() -> None:
    """Invalidate display inventories, including scans currently in flight."""
    global _INVENTORY_GENERATION
    with _INVENTORY_LOCK:
        _INVENTORIES.clear()
        _INVENTORY_GENERATION += 1


class ResultPathError(ValueError):
    """A requested result path is outside OmicsANG's filesystem boundary."""


def _inside(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def _directory_is_readable(path: Path) -> bool:
    try:
        return path.is_dir() and os.access(path, os.R_OK | os.X_OK)
    except OSError:
        return False


def _source_id(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:20]
    return f"result-{digest}"


def _root_label(pipeline: Pipeline, root: Path) -> str:
    resolved = root.resolve()
    try:
        relative = resolved.relative_to(pipeline.path.resolve()).as_posix()
        return relative or "."
    except ValueError:
        return str(resolved)


def _source_record(
    pipeline: Pipeline,
    root: Path,
    *,
    origins: list[str],
    attached: bool = False,
) -> dict[str, Any]:
    resolved = root.resolve()
    label = _root_label(pipeline, resolved)
    return {
        "source_id": _source_id(resolved),
        "path": str(resolved),
        "label": label,
        "display_path": label,
        "origins": list(dict.fromkeys(origins)),
        "attached": attached,
        "available": True,
    }


def _configured_roots(pipeline: Pipeline) -> Iterator[Path]:
    for config in pipeline.configs:
        try:
            data = yaml.safe_load((pipeline.path / config).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if not isinstance(data, dict) or not data.get("results_dir"):
            continue
        raw_values = data["results_dir"]
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        for value in values:
            try:
                yield pipeline.path / str(value)
            except (TypeError, ValueError):
                continue


def allowed_roots(pipeline: Pipeline) -> list[dict[str, Any]]:
    """Return readable roots within which users may search and attach results."""
    candidates = [
        (pipeline.path, "pipeline"),
        *((path, "configured") for path in settings.RESULTS_ROOTS),
    ]
    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for candidate, kind in candidates:
        try:
            resolved = fs_policy.resolve_root(candidate.expanduser())
        except fs_policy.PathPolicyError:
            continue
        if resolved in seen or not _directory_is_readable(resolved):
            continue
        seen.add(resolved)
        records.append(
            {
                "root_id": f"allowed-{_source_id(resolved).removeprefix('result-')}",
                "path": str(resolved),
                "label": "Pipeline checkout" if kind == "pipeline" else resolved.name,
                "kind": kind,
            }
        )
    return records


def validate_directory(
    pipeline: Pipeline,
    raw_path: str,
    *,
    allow_configured_root: bool = False,
) -> Path:
    """Resolve a directory and enforce the configured root fence.

    Interactive attachment/search flows reject an entire additional configured
    root to keep browsing bounded.  A pipeline's explicit ``results_dir`` may
    name that root itself because the operator already authorized it.
    """
    value = str(raw_path or "").strip()
    if not value or "\x00" in value:
        raise ResultPathError("result directory path is required")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = pipeline.path / candidate
    try:
        resolved = fs_policy.resolve_root(candidate)
    except fs_policy.PathPolicyError as exc:
        raise ResultPathError(
            "result directory does not exist or is inaccessible"
        ) from exc
    roots = [Path(record["path"]) for record in allowed_roots(pipeline)]
    if not roots or not any(_inside(root, resolved) for root in roots):
        raise ResultPathError("result directory is outside the allowed result roots")
    if not _directory_is_readable(resolved):
        raise ResultPathError("result directory does not exist or is not readable")
    configured_roots = {
        root.expanduser().resolve()
        for root in settings.RESULTS_ROOTS
        if root.expanduser().exists()
    }
    if (
        not allow_configured_root
        and resolved in configured_roots
        and resolved != pipeline.path.resolve()
    ):
        raise ResultPathError(
            "select a result subdirectory, not an entire configured root"
        )
    return resolved


def _attachment_records(pipeline: Pipeline) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for stored_path in store.results_attachments(pipeline.path):
        try:
            resolved = validate_directory(pipeline, stored_path)
        except ResultPathError as exc:
            try:
                fallback = Path(stored_path).expanduser().resolve(strict=False)
                source_id = _source_id(fallback)
            except (OSError, RuntimeError, ValueError):
                digest = hashlib.sha256(
                    str(stored_path).encode("utf-8", errors="replace"),
                ).hexdigest()[:20]
                source_id = f"result-{digest}"
            records.append(
                {
                    "source_id": source_id,
                    "path": stored_path,
                    "label": stored_path,
                    "display_path": stored_path,
                    "origins": ["attachment"],
                    "attached": True,
                    "available": False,
                    "error": str(exc),
                }
            )
            continue
        records.append(
            _source_record(
                pipeline,
                resolved,
                origins=["attachment"],
                attached=True,
            )
        )
    return records


def attachments(pipeline: Pipeline) -> list[dict[str, Any]]:
    return _attachment_records(pipeline)


def attach(pipeline: Pipeline, raw_path: str) -> tuple[dict[str, Any], bool]:
    resolved = validate_directory(pipeline, raw_path)
    _, created = store.attach_results_directory(pipeline.path, resolved)
    invalidate_inventory()
    record = _source_record(
        pipeline,
        resolved,
        origins=["attachment"],
        attached=True,
    )
    return record, created


def detach(pipeline: Pipeline, raw_path: str) -> tuple[list[dict[str, Any]], bool]:
    value = str(raw_path or "").strip()
    if not value:
        raise ResultPathError("result directory path is required")
    try:
        target = Path(value).expanduser()
        if not target.is_absolute():
            target = pipeline.path / target
        stored_value: str | Path = target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # Detach is deletion from OmicsANG's attachment list, not a filesystem
        # operation.  Preserve an unresolvable stored value so an unavailable
        # or corrupted attachment can still be cleared from the UI.
        stored_value = value
    _, removed = store.detach_results_directory(
        pipeline.path,
        stored_value,
    )
    invalidate_inventory()
    return attachments(pipeline), removed


def _result_sources(
    pipeline: Pipeline,
    *,
    include_attachments: bool = False,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    by_root: dict[Path, dict[str, Any]] = {}

    def add(candidate: Path, origin: str, attached: bool = False) -> None:
        try:
            # Config files are trusted workflow input, but they do not expand
            # OmicsANG's read boundary.  Every discovered, configured, and
            # attached source must pass the same explicit result-root fence.
            resolved = validate_directory(
                pipeline,
                str(candidate),
                allow_configured_root=origin == "configured",
            )
        except ResultPathError:
            return
        current = by_root.get(resolved)
        if current is None:
            current = _source_record(
                pipeline,
                resolved,
                origins=[origin],
                attached=attached,
            )
            by_root[resolved] = current
            sources.append(current)
            return
        if origin not in current["origins"]:
            current["origins"].append(origin)
        current["attached"] = bool(current["attached"] or attached)

    for configured in _configured_roots(pipeline):
        add(configured, "configured")
    for name in ("results", "result", "output", "outputs", "analysis"):
        add(pipeline.path / name, "discovered")
    if include_attachments:
        for record in _attachment_records(pipeline):
            if record["available"]:
                add(Path(record["path"]), "attachment", attached=True)
    return sources


def _results_dirs(
    pipeline: Pipeline,
    *,
    include_attachments: bool = False,
) -> list[Path]:
    """Return output roots; UI attachments are opt-in to protect provenance."""
    return [
        Path(source["path"])
        for source in _result_sources(
            pipeline,
            include_attachments=include_attachments,
        )
    ]


def _skip_search_directory(name: str) -> bool:
    return name.startswith(".") or name in _SEARCH_SKIP_DIRS


def _query_matches(path: Path, query: str) -> bool:
    if not query:
        return True
    haystack = str(path).casefold()
    return all(token in haystack for token in query.casefold().split())


def search_directories(
    pipeline: Pipeline,
    *,
    query: str = "",
    root_id: str = "",
    limit: int = 30,
) -> dict[str, Any]:
    """Bounded directory search selected by an opaque allowed-root identifier."""
    started = time.monotonic()
    deadline = started + MAX_DIRECTORY_SEARCH_SECONDS
    bounded_limit = max(1, min(int(limit), 100))
    allowed = allowed_roots(pipeline)
    if root_id:
        selected = next(
            (record for record in allowed if record["root_id"] == root_id),
            None,
        )
        if selected is None:
            raise ResultPathError("unknown result search root")
        scan_roots = [selected]
    else:
        scan_roots = allowed

    queues = [deque([(Path(root["path"]), 0)]) for root in scan_roots]
    active = deque(range(len(queues)))
    seen: set[Path] = set()
    candidates: list[dict[str, Any]] = []
    attached_paths = {
        record["path"]
        for record in _attachment_records(pipeline)
        if record["available"]
    }
    visited = 0
    child_cap_hit = False
    while (
        active
        and len(candidates) < bounded_limit
        and visited < MAX_DIRECTORY_SEARCH_VISITED
        and time.monotonic() < deadline
    ):
        index = active.popleft()
        queue = queues[index]
        if not queue:
            continue
        current, depth = queue.popleft()
        root = Path(scan_roots[index]["path"])
        try:
            resolved = current.resolve(strict=True)
        except (OSError, RuntimeError):
            resolved = None
        if resolved is not None and resolved not in seen and _inside(root, resolved):
            seen.add(resolved)
            visited += 1
            try:
                relative = resolved.relative_to(root).as_posix() or "."
            except ValueError:
                relative = "."
            if _directory_is_readable(resolved) and _query_matches(resolved, query):
                candidates.append(
                    {
                        "path": str(resolved),
                        "name": resolved.name or str(resolved),
                        "display_path": str(resolved),
                        "relative_path": relative,
                        "base": str(root),
                        "depth": depth,
                        "attached": str(resolved) in attached_paths,
                    }
                )
            if depth < MAX_DIRECTORY_SEARCH_DEPTH and len(candidates) < bounded_limit:
                children: list[Path] = []
                try:
                    with os.scandir(resolved) as entries:
                        for entry in entries:
                            if len(children) >= MAX_DIRECTORY_CHILDREN:
                                child_cap_hit = True
                                break
                            if time.monotonic() >= deadline:
                                break
                            if _skip_search_directory(entry.name) or entry.is_symlink():
                                continue
                            try:
                                if not entry.is_dir(follow_symlinks=False):
                                    continue
                                child = Path(entry.path).resolve(strict=True)
                            except (OSError, RuntimeError):
                                continue
                            if _inside(root, child):
                                children.append(child)
                except OSError:
                    pass
                queue.extend((child, depth + 1) for child in sorted(children))
        if queue:
            active.append(index)

    elapsed = time.monotonic() - started
    reasons: list[str] = []
    if len(candidates) >= bounded_limit:
        reasons.append("limit")
    if visited >= MAX_DIRECTORY_SEARCH_VISITED:
        reasons.append("visited")
    if elapsed >= MAX_DIRECTORY_SEARCH_SECONDS:
        reasons.append("time")
    if child_cap_hit:
        reasons.append("children")
    if any(queues):
        reasons.append("remaining")
    return {
        "query": query,
        "root_id": root_id,
        "allowed_roots": allowed,
        "candidates": candidates,
        "bounds": {
            "limit": bounded_limit,
            "max_depth": MAX_DIRECTORY_SEARCH_DEPTH,
            "max_visited": MAX_DIRECTORY_SEARCH_VISITED,
            "max_seconds": MAX_DIRECTORY_SEARCH_SECONDS,
            "visited": visited,
            "elapsed_ms": round(elapsed * 1000, 1),
            "truncated": bool(reasons),
            "reasons": list(dict.fromkeys(reasons)),
        },
    }


def _walk_source(
    root: Path, deadline: float, scan: dict | None = None
) -> Iterator[Path | None]:
    """Yield one directory entry per step so roots can be scanned round-robin."""
    root = root.resolve()
    scan = scan if scan is not None else {}
    scan.setdefault("truncated", False)
    queue = deque([root])
    visited_dirs = 0
    visited_entries = 0
    while (
        queue
        and visited_dirs < MAX_GALLERY_DIRS_PER_SOURCE
        and visited_entries < MAX_GALLERY_ENTRIES_PER_SOURCE
        and time.monotonic() < deadline
    ):
        current = queue.popleft()
        visited_dirs += 1
        try:
            with os.scandir(current) as entries:
                had_entry = False
                for entry in entries:
                    had_entry = True
                    visited_entries += 1
                    if (
                        visited_entries > MAX_GALLERY_ENTRIES_PER_SOURCE
                        or time.monotonic() >= deadline
                    ):
                        scan["truncated"] = True
                        return
                    if entry.is_symlink():
                        yield None
                        continue
                    if _skip_search_directory(entry.name):
                        yield None
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            # ``current`` is canonical and the entry is not a
                            # symlink, so its path is canonical too; skipping
                            # realpath avoids one lstat per path component.
                            queue.append(Path(entry.path))
                            yield None
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                        else:
                            yield None
                    except OSError:
                        scan["truncated"] = True
                        yield None
                if not had_entry:
                    yield None
        except OSError:
            scan["truncated"] = True
            yield None
    if queue:
        scan["truncated"] = True


def _display_path(source: dict[str, Any], relative: str) -> str:
    label = str(source["label"])
    if label == ".":
        return relative
    return f"{label.rstrip('/')}/{relative}"


def _gather_sources(
    pipeline: Pipeline,
    sources: list[dict[str, Any]],
    *,
    limit: int,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    notebooks: list[dict[str, Any]] = []
    deadline = time.monotonic() + MAX_GALLERY_SECONDS
    scan: dict = {}
    active = deque(
        (source, _walk_source(Path(source["path"]), deadline, scan))
        for source in sources
    )
    count = 0
    seen_files: set[Path] = set()
    while active and count < max(1, int(limit)) and time.monotonic() < deadline:
        source, iterator = active.popleft()
        try:
            path = next(iterator)
        except StopIteration:
            continue
        active.append((source, iterator))
        if path is None:
            continue
        # Classify by name first: most outputs (BAM, FASTQ, bigWig, ...) are
        # never displayed, so they should not cost a realpath and stat each.
        suffix = path.suffix.lower()
        category: list[dict[str, Any]] | None = None
        if path.name in _REPORT_HINTS or (
            suffix == ".html" and "report" in path.name.lower()
        ):
            category = reports
        elif suffix in _IMG:
            category = images
        elif suffix == ".ipynb":
            category = notebooks
        elif suffix in _TABLE:
            category = tables
        if category is None:
            continue
        root = Path(source["path"])
        try:
            actual = path.resolve(strict=True)
            if (
                actual in seen_files
                or not _inside(root, actual)
                or not actual.is_file()
            ):
                continue
            relative = path.relative_to(root).as_posix()
            stat_result = actual.stat()
        except (OSError, RuntimeError, ValueError):
            continue
        if any(part in _SKIP_DIRS for part in Path(relative).parts[:-1]):
            continue
        seen_files.add(actual)
        entry: dict[str, Any] = {
            "source_id": source["source_id"],
            "path": relative,
            "relative_path": relative,
            "display_path": _display_path(source, relative),
            "name": path.name,
            "size": stat_result.st_size,
        }
        try:
            entry["legacy_path"] = path.relative_to(pipeline.path.resolve()).as_posix()
        except ValueError:
            # External sources do not have a meaningful pipeline-relative
            # legacy path.  Omitting the optional field avoids leaking a JSON
            # null into older/generic result renderers.
            pass
        category.append(entry)
        count += 1
    return {
        "reports": reports,
        "images": images,
        "tables": tables,
        "notebooks": notebooks,
        "counts": {
            "reports": len(reports),
            "images": len(images),
            "tables": len(tables),
            "notebooks": len(notebooks),
        },
        "scan": {
            "limit": max(1, int(limit)),
            "returned": count,
            "truncated": bool(active) or scan.get("truncated", False),
        },
    }


def gather(pipeline: Pipeline, limit: int = 400) -> dict[str, Any]:
    sources = _result_sources(pipeline, include_attachments=True)
    payload = _gather_sources(pipeline, sources, limit=limit)
    payload.update(
        {
            "roots": [source["label"] for source in sources],
            "sources": sources,
            "attachments": _attachment_records(pipeline),
        }
    )
    return payload


def page(
    pipeline: Pipeline,
    *,
    offset: int = 0,
    page_size: int = 60,
    kind: str = "all",
    query: str = "",
    refresh: bool = False,
) -> dict[str, Any]:
    """Page a short-lived UI inventory; never cache file access authorization.

    Source configuration and attachment fences are resolved on every request.
    Nested output changes expire within the TTL, or immediately on Refresh.
    Execution/provenance callers continue to use uncached discovery.
    """
    sources = _result_sources(pipeline, include_attachments=True)
    attachment_records = _attachment_records(pipeline)
    signatures = []
    for source in sources:
        try:
            info = Path(source["path"]).stat()
            signatures.append(
                (source["path"], info.st_dev, info.st_ino, info.st_mtime_ns)
            )
        except OSError:
            signatures.append((source["path"], None))
    key = (str(settings.STATE_DIR), str(pipeline.path.resolve()), tuple(signatures))
    # Coalesce simultaneous scans of the same checkout without serializing all
    # requests behind one slow filesystem scan. Memory remains bounded.
    with _SCAN_LOCKS[hash(key) % len(_SCAN_LOCKS)]:
        with _INVENTORY_LOCK:
            cached = _INVENTORIES.get(key)
            generation = _INVENTORY_GENERATION
        if refresh or cached is None or time.monotonic() - cached[0] >= INVENTORY_TTL:
            inventory = _gather_sources(pipeline, sources, limit=INVENTORY_LIMIT)
            inventory["generated"] = time.time()
            with _INVENTORY_LOCK:
                if generation == _INVENTORY_GENERATION:
                    _INVENTORIES[key] = (time.monotonic(), inventory)
                    _INVENTORIES.move_to_end(key)
                    while len(_INVENTORIES) > 32:
                        _INVENTORIES.popitem(last=False)
        else:
            inventory = cached[1]
    categories = ("reports", "images", "tables", "notebooks")
    category = "images" if kind == "figures" else kind
    needle = query.strip().casefold()
    matching = [
        (group, item)
        for group in categories
        if category in ("all", group)
        for item in inventory[group]
        if not needle or needle in f"{item['display_path']} {item['name']}".casefold()
    ]
    matching.sort(
        key=lambda row: (row[0], row[1]["display_path"].casefold(), row[1]["source_id"])
    )
    page_size = max(1, min(page_size, 100))
    offset = max(0, offset)
    payload = {group: [] for group in categories}
    for group, item in matching[offset : offset + page_size]:
        payload[group].append(dict(item))
    payload.update(
        counts=dict(inventory["counts"]),
        scan=dict(inventory["scan"]),
        generated=inventory["generated"],
        roots=[source["label"] for source in sources],
        sources=sources,
        attachments=attachment_records,
        pagination={
            "offset": offset,
            "page_size": page_size,
            "total": len(matching),
            "has_more": offset + page_size < len(matching),
        },
    )
    return payload


def preview_directory(
    pipeline: Pipeline,
    raw_path: str,
    *,
    limit: int = 40,
) -> dict[str, Any]:
    resolved = validate_directory(pipeline, raw_path)
    attached = str(resolved) in store.results_attachments(pipeline.path)
    source = _source_record(
        pipeline,
        resolved,
        origins=["preview"],
        attached=attached,
    )
    payload = _gather_sources(pipeline, [source], limit=max(1, min(limit, 100)))
    return {
        "directory": source,
        "already_attached": attached,
        "counts": payload["counts"],
        "samples": {
            key: payload[key][:5]
            for key in ("reports", "images", "tables", "notebooks")
        },
        "scan": payload["scan"],
    }


def resolve_source_file(pipeline: Pipeline, source_id: str, raw_path: str) -> Path:
    source = next(
        (
            item
            for item in _result_sources(pipeline, include_attachments=True)
            if item["source_id"] == source_id
        ),
        None,
    )
    if source is None:
        raise ResultPathError("unknown result source")
    if not raw_path:
        raise ResultPathError("result file path must be root-relative")
    root = Path(source["path"]).resolve()
    try:
        _, target = fs_policy.resolve_relative(
            root,
            str(raw_path),
            must_exist=True,
            expected="file",
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError("result file not found") from exc
    except fs_policy.PathPolicyError as exc:
        raise ResultPathError(str(exc)) from exc
    if not os.access(target, os.R_OK):
        raise FileNotFoundError("result file not found")
    return target
