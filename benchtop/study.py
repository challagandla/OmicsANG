# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Bioinformatics study-design and sample-sheet preflight analysis."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from .pipelines import Pipeline

MAX_SHEET_BYTES = 8_000_000
MAX_ROWS = 10_000
MAX_COLUMNS = 256
MAX_ISSUES = 500
MAX_CONTENT_INPUTS = 2_000
INPUT_SAMPLE_BYTES = 4_096
TABLE_SUFFIXES = {".csv", ".tsv", ".txt"}
SKIP_PARTS = {
    ".git",
    ".snakemake",
    ".benchtop",
    ".cache",
    ".pytest_cache",
    "node_modules",
    "results",
    "result",
    "outputs",
    "output",
    "analysis",
}
ROLE_ALIASES = {
    "sample_id": (
        "sample_id",
        "sampleid",
        "sample",
        "library_id",
        "library",
        "id",
    ),
    "condition": (
        "condition",
        "group",
        "treatment",
        "phenotype",
        "diagnosis",
        "status",
        "genotype",
        "cell_type",
        "celltype",
        "tissue",
    ),
    "batch": (
        "batch",
        "sequencing_batch",
        "seq_batch",
        "plate",
        "lane",
        "site",
        "center",
    ),
    "subject": (
        "subject_id",
        "subject",
        "donor_id",
        "donor",
        "patient_id",
        "patient",
        "individual_id",
        "individual",
    ),
    "replicate": (
        "biological_replicate",
        "bio_replicate",
        "biorep",
        "replicate",
        "rep",
    ),
    "read1": (
        "fastq_r1",
        "raw_fastq_r1",
        "fastq_1",
        "fastq1",
        "fq1",
        "r1",
        "read1",
        "fastq",
        "path",
        "file",
    ),
    "read2": (
        "fastq_r2",
        "raw_fastq_r2",
        "fastq_2",
        "fastq2",
        "fq2",
        "r2",
        "read2",
    ),
    "data": (
        "path",
        "input",
        "input_path",
        "data",
        "data_path",
        "counts",
        "matrix",
        "file",
    ),
}
PATH_KEY_RE = re.compile(r"(sample|sheet|metadata|manifest|cohort|design)", re.I)
FORMULA_KEY_RE = re.compile(r"(^|[._])(design|formula)([._]|$)", re.I)
REMOTE_INPUT_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _inside(root: Path, path: Path) -> bool:
    root = root.resolve()
    path = path.resolve()
    return path == root or root in path.parents


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _config_path(pipeline: Pipeline, raw: str) -> Path | None:
    if raw:
        path = (pipeline.path / raw).resolve()
        if not _inside(pipeline.path, path) or not path.is_file():
            raise ValueError("config file must exist inside the pipeline directory")
        return path
    choices = [*pipeline.configs, *pipeline.project_local, *pipeline.test_configs]
    for rel in choices:
        path = (pipeline.path / rel).resolve()
        if path.is_file() and _inside(pipeline.path, path):
            return path
    return None


def _walk_values(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_values(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_values(child, f"{prefix}[{index}]")
    else:
        yield prefix, value


def _load_config(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_reference(root: Path, config: Path | None, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    root_path = (root / candidate).resolve()
    if root_path.exists() or config is None:
        return root_path
    config_path = (config.parent / candidate).resolve()
    return config_path if config_path.exists() else root_path


def _table_lines(path: Path) -> tuple[list[str], str]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot stat sample sheet: {exc}") from exc
    if size > MAX_SHEET_BYTES:
        raise ValueError(
            f"sample sheet exceeds the {MAX_SHEET_BYTES // 1_000_000} MB analysis limit"
        )
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ValueError(f"cannot read sample sheet: {exc}") from exc
    lines = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        raise ValueError("sample sheet has no header or data rows")
    if path.suffix.lower() == ".tsv":
        delimiter = "\t"
    elif path.suffix.lower() == ".csv":
        delimiter = ","
    else:
        try:
            delimiter = (
                csv.Sniffer().sniff("\n".join(lines[:20]), delimiters="\t,;").delimiter
            )
        except csv.Error:
            delimiter = "\t"
    return lines, delimiter


def _read_table(path: Path) -> tuple[list[str], list[dict[str, str]], list[str]]:
    lines, delimiter = _table_lines(path)
    try:
        reader = csv.reader(lines, delimiter=delimiter)
        raw_header = next(reader)
        headers = [cell.strip() for cell in raw_header]
        duplicate_headers = [
            name
            for name, count in Counter(_norm(h) for h in headers if h).items()
            if count > 1
        ]
        if not headers or any(not header for header in headers):
            raise ValueError("sample sheet contains an empty column name")
        if len(headers) > MAX_COLUMNS:
            raise ValueError(
                f"sample sheet exceeds the {MAX_COLUMNS}-column analysis limit"
            )
        rows: list[dict[str, str]] = []
        for index, cells in enumerate(reader, 2):
            if len(rows) >= MAX_ROWS:
                raise ValueError(
                    f"sample sheet exceeds the {MAX_ROWS}-row analysis limit"
                )
            cells = [str(cell).strip() for cell in cells]
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            rows.append(
                {
                    header: cells[i] if i < len(cells) else ""
                    for i, header in enumerate(headers)
                }
            )
    except csv.Error as exc:
        raise ValueError(f"sample sheet is malformed: {exc}") from exc
    return headers, rows, duplicate_headers


def _peek(path: Path) -> dict:
    try:
        columns, rows, _ = _read_table(path)
        return {"rows": len(rows), "columns": columns, "error": ""}
    except ValueError as exc:
        return {"rows": 0, "columns": [], "error": str(exc)}


def discover(pipeline: Pipeline, configfile: str = "") -> list[dict]:
    config_path = _config_path(pipeline, configfile)
    config = _load_config(config_path)
    found: dict[Path, dict] = {}

    def add(path: Path, *, source: str, key: str = "", score: int = 0) -> None:
        path = path.resolve()
        if path.suffix.lower() not in TABLE_SUFFIXES:
            return
        if any(part in SKIP_PARTS for part in path.parts):
            return
        previous = found.get(path)
        entry = {
            "path": _display_path(pipeline.path, path),
            "exists": path.is_file(),
            "external": not _inside(pipeline.path, path),
            "source": source,
            "config_key": key,
            "score": score + (40 if path.is_file() else 0),
        }
        if previous is None or entry["score"] > previous["score"]:
            found[path] = entry

    for key, value in _walk_values(config):
        if not isinstance(value, str) or not PATH_KEY_RE.search(key):
            continue
        suffix = Path(value.split("?", 1)[0]).suffix.lower()
        if suffix in TABLE_SUFFIXES:
            add(
                _resolve_reference(pipeline.path, config_path, value),
                source="config",
                key=key,
                score=100,
            )

    search_roots = [pipeline.path]
    for name in ("config", "configs", "metadata", "samples", "project_local"):
        root = pipeline.path / name
        if root.is_dir():
            search_roots.append(root)
    patterns = (
        "*sample*.tsv",
        "*sample*.csv",
        "*metadata*.tsv",
        "*metadata*.csv",
        "*manifest*.tsv",
        "*manifest*.csv",
        "*cohort*.tsv",
        "*cohort*.csv",
    )
    for root in search_roots:
        for pattern in patterns:
            for path in (
                root.glob(pattern) if root == pipeline.path else root.rglob(pattern)
            ):
                add(path, source="discovery", score=25)

    out = []
    for path, entry in found.items():
        entry.update(
            _peek(path)
            if path.is_file()
            else {"rows": 0, "columns": [], "error": "file not found"}
        )
        header_norm = {_norm(column) for column in entry["columns"]}
        has_sample = bool(header_norm & set(ROLE_ALIASES["sample_id"]))
        has_study_data = bool(
            header_norm
            & (
                set(ROLE_ALIASES["condition"])
                | set(ROLE_ALIASES["read1"])
                | set(ROLE_ALIASES["data"])
            )
        )
        entry["likely_sample_sheet"] = has_sample and has_study_data
        if has_sample:
            entry["score"] += 20
        if header_norm & set(ROLE_ALIASES["condition"]):
            entry["score"] += 10
        out.append(entry)
    out.sort(key=lambda item: (-item["score"], item["path"].lower()))
    return out[:50]


def _resolve_selected(
    pipeline: Pipeline, candidates: list[dict], raw: str
) -> tuple[dict | None, Path | None]:
    if not candidates:
        return None, None
    if not raw:
        selected = next(
            (
                item
                for item in candidates
                if item.get("source") == "config"
                and re.search(
                    r"(sample|sheet|metadata|cohort)", item.get("config_key", ""), re.I
                )
            ),
            None,
        ) or next(
            (item for item in candidates if item.get("likely_sample_sheet")), None
        )
        if selected is None:
            return None, None
    else:
        selected = next((item for item in candidates if item["path"] == raw), None)
        if selected is None:
            raise ValueError(
                "sample sheet must be one of the discovered pipeline candidates"
            )
    path = Path(selected["path"])
    if not path.is_absolute():
        path = pipeline.path / path
    path = path.resolve()
    if path.suffix.lower() not in TABLE_SUFFIXES:
        raise ValueError("selected sample sheet is not a supported table")
    return selected, path


def _infer_roles(
    columns: list[str], overrides: dict[str, str] | None = None
) -> dict[str, str]:
    normalized = {_norm(column): column for column in columns}
    roles: dict[str, str] = {}
    for role, aliases in ROLE_ALIASES.items():
        roles[role] = next(
            (normalized[name] for name in aliases if name in normalized), ""
        )
    for role, column in (overrides or {}).items():
        if role in ROLE_ALIASES:
            roles[role] = column if column in columns else ""
    return roles


def _issue(
    severity: str, code: str, title: str, message: str, rows: list[int] | None = None
) -> dict:
    item = {"severity": severity, "code": code, "title": title, "message": message}
    if rows:
        item["rows"] = rows[:100]
    return item


def _resolve_input(root: Path, sheet: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    from_root = (root / path).resolve()
    if from_root.exists():
        return from_root
    return (sheet.parent / path).resolve()


def _split_input_paths(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    return [value.strip() for value in re.split(r"[;,]", raw) if value.strip()]


def _input_identity(path: Path, include_content: bool) -> str:
    stat = path.stat()
    if not include_content:
        return f"{path}|{stat.st_size}|{stat.st_mtime_ns}|metadata"
    digest = hashlib.sha256()
    offsets = (
        0,
        max(0, stat.st_size // 2 - INPUT_SAMPLE_BYTES // 2),
        max(0, stat.st_size - INPUT_SAMPLE_BYTES),
    )
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            digest.update(handle.read(INPUT_SAMPLE_BYTES))
    digest.update(str(stat.st_size).encode())
    return f"{path}|{stat.st_size}|{digest.hexdigest()}|sampled-3"


def _matrix_rank(matrix: list[list[float]], tolerance: float = 1e-9) -> int:
    if not matrix or not matrix[0]:
        return 0
    data = [row[:] for row in matrix]
    rows, cols = len(data), len(data[0])
    rank = 0
    for col in range(cols):
        pivot = max(range(rank, rows), key=lambda r: abs(data[r][col]), default=rank)
        if pivot >= rows or abs(data[pivot][col]) <= tolerance:
            continue
        data[rank], data[pivot] = data[pivot], data[rank]
        value = data[rank][col]
        data[rank] = [cell / value for cell in data[rank]]
        for row in range(rows):
            if row == rank:
                continue
            factor = data[row][col]
            if abs(factor) <= tolerance:
                continue
            data[row] = [
                cell - factor * base for cell, base in zip(data[row], data[rank])
            ]
        rank += 1
        if rank == rows:
            break
    return rank


def _find_formula(config: dict, columns: list[str]) -> str:
    for key, value in _walk_values(config):
        if (
            isinstance(value, str)
            and value.strip().startswith("~")
            and FORMULA_KEY_RE.search(key)
        ):
            return value.strip()
    if "condition" in {_norm(column) for column in columns}:
        return "~ condition"
    return ""


def _design_matrix(rows: list[dict[str, str]], formula: str) -> dict:
    if not formula or not rows:
        return {
            "formula": formula,
            "factors": [],
            "unresolved": [],
            "constant": [],
            "columns": [],
            "rank": 0,
            "full_rank": True,
            "partial": False,
        }
    headers = list(rows[0])
    by_norm = {_norm(header): header for header in headers}
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", formula.split("~", 1)[-1])
    factors = []
    unresolved = []
    constant = []
    has_function_expression = any(
        marker in formula for marker in ("(", ")", '"', "'", "=")
    )
    formula_functions = {
        "C",
        "I",
        "factor",
        "scale",
        "log",
        "poly",
        "ns",
        "bs",
        "strata",
    }
    for token in tokens:
        column = by_norm.get(_norm(token))
        if column and column not in factors:
            factors.append(column)
        elif (
            not column
            and not has_function_expression
            and token not in formula_functions
            and "." not in token
            and token not in unresolved
        ):
            unresolved.append(token)
    rhs = formula.split("~", 1)[-1]
    no_intercept = bool(re.search(r"(^|[+\s])(?:0|-\s*1)(?:$|[+\s])", rhs))
    matrix: list[list[float]] = [[] for _ in rows]
    names: list[str] = []
    if not no_intercept:
        names.append("Intercept")
        for vector in matrix:
            vector.append(1.0)
    categorical_seen = 0
    for factor in factors:
        values = [row.get(factor, "") for row in rows]
        numeric: list[float] = []
        is_numeric = True
        for value in values:
            try:
                numeric.append(float(value))
            except (TypeError, ValueError):
                is_numeric = False
                break
        if is_numeric and len(set(numeric)) > 1:
            names.append(factor)
            for vector, value in zip(matrix, numeric):
                vector.append(value)
            continue
        levels = sorted({value for value in values if value != ""})
        if len(levels) <= 1:
            constant.append(factor)
        encoded = levels if no_intercept and categorical_seen == 0 else levels[1:]
        categorical_seen += 1
        for level in encoded:
            names.append(f"{factor}[{level}]")
            for vector, value in zip(matrix, values):
                vector.append(1.0 if value == level else 0.0)
    rank = _matrix_rank(matrix)
    return {
        "formula": formula,
        "factors": factors,
        "unresolved": unresolved,
        "constant": constant,
        "columns": names,
        "rank": rank,
        "full_rank": rank == len(names),
        "partial": any(marker in formula for marker in ("*", ":", "/", "(", ")")),
    }


def _distribution(
    rows: list[dict[str, str]], column: str, unit_column: str
) -> list[dict]:
    values: dict[str, set[str]] = defaultdict(set)
    for index, row in enumerate(rows, 1):
        level = row.get(column, "").strip() or "(missing)"
        unit = row.get(unit_column, "").strip() if unit_column else str(index)
        values[level].add(unit or "(missing biological unit)")
    return [
        {"level": level, "n": len(units)} for level, units in sorted(values.items())
    ]


def audit(
    pipeline: Pipeline,
    *,
    configfile: str = "",
    sheet: str = "",
    roles: dict[str, str] | None = None,
) -> dict:
    config_path = _config_path(pipeline, configfile)
    config = _load_config(config_path)
    candidates = discover(pipeline, configfile)
    selected, sheet_path = _resolve_selected(pipeline, candidates, sheet)
    authoritative, _ = _resolve_selected(pipeline, candidates, "")
    authoritative_path = (
        str(authoritative.get("path") or "")
        if authoritative
        and authoritative.get("source") == "config"
        and authoritative.get("config_key")
        else ""
    )
    for candidate in candidates:
        candidate["authoritative"] = bool(
            authoritative_path and candidate.get("path") == authoritative_path
        )
    if selected is None or sheet_path is None:
        return {
            "generated": None,
            "pipeline": pipeline.name,
            "configfile": _display_path(pipeline.path, config_path)
            if config_path
            else "",
            "candidates": candidates,
            "selected": None,
            "roles": {},
            "summary": {
                "samples": 0,
                "biological_units": 0,
                "groups": 0,
                "batches": 0,
                "errors": 0,
                "warnings": 0,
            },
            "distributions": {},
            "balance": {"conditions": [], "batches": [], "cells": []},
            "design": {
                "formula": "",
                "factors": [],
                "unresolved": [],
                "constant": [],
                "columns": [],
                "rank": 0,
                "full_rank": True,
                "partial": False,
            },
            "issues": [],
            "rows": [],
            "columns": [],
            "gate": "unknown",
            "score": 0,
            "fingerprint": "",
            "inputs_fingerprint": "",
        }

    if not sheet_path.is_file():
        fingerprint = hashlib.sha256(f"missing|{sheet_path}".encode()).hexdigest()
        return {
            "generated": None,
            "pipeline": pipeline.name,
            "configfile": _display_path(pipeline.path, config_path)
            if config_path
            else "",
            "candidates": candidates,
            "selected": selected,
            "roles": {},
            "summary": {
                "samples": 0,
                "biological_units": 0,
                "groups": 0,
                "batches": 0,
                "errors": 1,
                "warnings": 0,
                "issues_shown": 1,
                "issues_total": 1,
                "paired": False,
                "input_files": 0,
            },
            "distributions": {},
            "balance": {"conditions": [], "batches": [], "cells": []},
            "design": {
                "formula": "",
                "factors": [],
                "unresolved": [],
                "constant": [],
                "columns": [],
                "rank": 0,
                "full_rank": True,
                "partial": False,
            },
            "issues": [
                _issue(
                    "error",
                    "missing-study-sheet",
                    "Configured sample sheet is missing",
                    f"{selected['path']} does not resolve to a readable table.",
                )
            ],
            "rows": [],
            "columns": [],
            "gate": "blocked",
            "score": 0,
            "fingerprint": fingerprint,
            "inputs_fingerprint": hashlib.sha256(
                f"missing|{sheet_path}".encode()
            ).hexdigest(),
        }

    columns, rows, duplicate_headers = _read_table(sheet_path)
    inferred = _infer_roles(columns, roles)
    formula = _find_formula(config, columns)
    design = _design_matrix(rows, formula)
    issues: list[dict] = []
    if not rows:
        issues.append(
            _issue(
                "error",
                "empty-study",
                "Sample sheet has no data rows",
                "Add at least one biological sample before running the workflow.",
            )
        )
    if duplicate_headers:
        issues.append(
            _issue(
                "error",
                "duplicate-columns",
                "Duplicate column names",
                f"Normalized duplicate columns: {', '.join(duplicate_headers)}",
            )
        )

    sample_col = inferred["sample_id"]
    condition_col = inferred["condition"]
    batch_col = inferred["batch"]
    subject_col = inferred["subject"]
    replicate_col = inferred["replicate"]
    unit_col = subject_col or replicate_col or sample_col

    if subject_col or replicate_col:
        missing_unit = [
            index
            for index, row in enumerate(rows, 2)
            if not row.get(unit_col, "").strip()
        ]
        if missing_unit:
            severity = "error" if unit_col in design["factors"] else "warning"
            issues.append(
                _issue(
                    severity,
                    "missing-biological-unit",
                    "Missing biological-unit identifiers",
                    f"{len(missing_unit)} rows cannot be assigned to an independent subject/donor/replicate.",
                    missing_unit,
                )
            )

    if not sample_col:
        issues.append(
            _issue(
                "error",
                "sample-id-column",
                "Sample identifier not found",
                "Map a unique sample or library identifier column before running.",
            )
        )
    else:
        sample_values = [row.get(sample_col, "").strip() for row in rows]
        missing = [index for index, value in enumerate(sample_values, 2) if not value]
        if missing:
            issues.append(
                _issue(
                    "error",
                    "missing-sample-id",
                    "Missing sample identifiers",
                    f"{len(missing)} rows have no sample identifier.",
                    missing,
                )
            )
        normalized = defaultdict(list)
        for index, value in enumerate(sample_values, 2):
            if value:
                normalized[_norm(value)].append(index)
        collisions = [indices for indices in normalized.values() if len(indices) > 1]
        if collisions:
            flat = [index for group in collisions for index in group]
            issues.append(
                _issue(
                    "error",
                    "duplicate-sample-id",
                    "Duplicate sample identifiers",
                    f"{len(collisions)} identifiers collide after normalization.",
                    flat,
                )
            )

    input_records: list[str] = []
    used_paths: dict[str, list[tuple[int, str]]] = defaultdict(list)
    content_fingerprints = 0
    paired_rows = 0
    for row_index, row in enumerate(rows, 2):
        r1 = (
            _split_input_paths(row.get(inferred["read1"], ""))
            if inferred["read1"]
            else []
        )
        r2 = (
            _split_input_paths(row.get(inferred["read2"], ""))
            if inferred["read2"]
            else []
        )
        data_paths = (
            _split_input_paths(row.get(inferred["data"], ""))
            if inferred["data"]
            else []
        )
        if r2:
            paired_rows += 1
        if r1 and r2 and len(r1) != len(r2):
            issues.append(
                _issue(
                    "error",
                    "lane-count-mismatch",
                    "R1/R2 lane counts differ",
                    f"Row has {len(r1)} R1 paths and {len(r2)} R2 paths.",
                    [row_index],
                )
            )
        inputs = [("read1", raw) for raw in r1] + [("read2", raw) for raw in r2]
        inputs.extend(("data", raw) for raw in data_paths if raw not in {*r1, *r2})
        normalized_inputs = [
            (
                role,
                raw,
                f"remote|{raw}"
                if REMOTE_INPUT_RE.match(raw)
                else str(_resolve_input(pipeline.path, sheet_path, raw)),
            )
            for role, raw in inputs
        ]
        duplicate_keys = {
            key
            for key, count in Counter(key for _, _, key in normalized_inputs).items()
            if count > 1
        }
        duplicate_inputs = list(
            dict.fromkeys(
                raw for _, raw, key in normalized_inputs if key in duplicate_keys
            )
        )
        if duplicate_inputs:
            issues.append(
                _issue(
                    "error",
                    "duplicate-lane-path",
                    "Input path repeated within a sample",
                    f"Repeated paths: {', '.join(duplicate_inputs[:6])}.",
                    [row_index],
                )
            )
        for role, raw, path_key in normalized_inputs:
            if REMOTE_INPUT_RE.match(raw):
                input_records.append(f"remote|{raw}")
                used_paths[path_key].append((row_index, role))
                continue
            path = Path(path_key)
            try:
                if not path.exists():
                    raise FileNotFoundError(str(path))
                if not path.is_file():
                    raise IsADirectoryError(str(path))
                stat = path.stat()
                include_content = content_fingerprints < MAX_CONTENT_INPUTS
                input_records.append(_input_identity(path, include_content))
                if include_content:
                    content_fingerprints += 1
                if stat.st_size == 0:
                    issues.append(
                        _issue(
                            "error",
                            "empty-input",
                            "Empty input file",
                            f"{raw} is zero bytes.",
                            [row_index],
                        )
                    )
            except IsADirectoryError:
                input_records.append(f"{path}|not-file")
                issues.append(
                    _issue(
                        "error",
                        "non-file-input",
                        "Input path is not a regular file",
                        f"{raw} resolves to a directory or non-file object.",
                        [row_index],
                    )
                )
            except OSError:
                input_records.append(f"{path}|missing")
                issues.append(
                    _issue(
                        "error",
                        "missing-input",
                        "Input file not found",
                        f"{raw} does not resolve to an existing file.",
                        [row_index],
                    )
                )
            used_paths[str(path)].append((row_index, role))
        r1_paths = {_resolve_input(pipeline.path, sheet_path, raw) for raw in r1}
        r2_paths = {_resolve_input(pipeline.path, sheet_path, raw) for raw in r2}
        if r1_paths & r2_paths:
            issues.append(
                _issue(
                    "error",
                    "same-read-pair",
                    "R1 and R2 are the same file",
                    "Paired reads must point to different files.",
                    [row_index],
                )
            )
    if inferred["read2"] and 0 < paired_rows < len(rows):
        incomplete = [
            index
            for index, row in enumerate(rows, 2)
            if not row.get(inferred["read2"], "").strip()
        ]
        issues.append(
            _issue(
                "error",
                "mixed-read-layout",
                "Incomplete paired-end rows",
                f"{len(incomplete)} rows lack R2 while other rows are paired.",
                incomplete,
            )
        )
    reused = {
        path: refs
        for path, refs in used_paths.items()
        if len({row for row, _ in refs}) > 1
    }
    if reused:
        refs = [row for items in reused.values() for row, _ in items]
        issues.append(
            _issue(
                "error",
                "reused-input",
                "Input files reused across samples",
                f"{len(reused)} input paths occur in multiple sample rows.",
                refs,
            )
        )

    distributions = {}
    if condition_col:
        distributions["condition"] = _distribution(rows, condition_col, unit_col)
        missing_condition = [
            index
            for index, row in enumerate(rows, 2)
            if not row.get(condition_col, "").strip()
        ]
        if missing_condition:
            severity = "error" if condition_col in design["factors"] else "warning"
            issues.append(
                _issue(
                    severity,
                    "missing-condition",
                    "Missing condition values",
                    f"{len(missing_condition)} rows have no condition/group value.",
                    missing_condition,
                )
            )
        singleton = [
            item
            for item in distributions["condition"]
            if item["level"] != "(missing)" and item["n"] < 2
        ]
        if singleton:
            detail = ", ".join(f"{item['level']} (n={item['n']})" for item in singleton)
            issues.append(
                _issue(
                    "error",
                    "no-biological-replication",
                    "No biological replication",
                    f"Groups with fewer than two independent units: {detail}.",
                )
            )
        low_power = [
            item
            for item in distributions["condition"]
            if item["level"] != "(missing)" and item["n"] == 2
        ]
        if low_power:
            detail = ", ".join(f"{item['level']} (n={item['n']})" for item in low_power)
            issues.append(
                _issue(
                    "warning",
                    "low-replication",
                    "Low biological replication",
                    f"Groups below three independent units: {detail}.",
                )
            )
        if len(distributions["condition"]) < 2:
            issues.append(
                _issue(
                    "warning",
                    "single-condition",
                    "Only one condition level",
                    "A between-condition contrast cannot be estimated from this sheet.",
                )
            )
    else:
        issues.append(
            _issue(
                "warning",
                "condition-column",
                "Condition column not found",
                "Map a condition/group column to evaluate replication and contrasts.",
            )
        )
    if batch_col:
        distributions["batch"] = _distribution(rows, batch_col, unit_col)
        missing_batch = [
            index
            for index, row in enumerate(rows, 2)
            if not row.get(batch_col, "").strip()
        ]
        if missing_batch:
            severity = "error" if batch_col in design["factors"] else "warning"
            issues.append(
                _issue(
                    severity,
                    "missing-batch",
                    "Missing batch values",
                    f"{len(missing_batch)} rows have no batch value.",
                    missing_batch,
                )
            )
    if subject_col:
        distributions["subject"] = _distribution(rows, subject_col, subject_col)

    condition_levels = [item["level"] for item in distributions.get("condition", [])]
    batch_levels = [item["level"] for item in distributions.get("batch", [])]
    balance_cells = []
    if condition_col and batch_col:
        units_by_cell: dict[tuple[str, str], set[str]] = defaultdict(set)
        for index, row in enumerate(rows, 1):
            condition = row.get(condition_col, "").strip() or "(missing)"
            batch = row.get(batch_col, "").strip() or "(missing)"
            unit = row.get(unit_col, "").strip() if unit_col else str(index)
            units_by_cell[(condition, batch)].add(unit or str(index))
        for condition in condition_levels:
            for batch in batch_levels:
                balance_cells.append(
                    {
                        "condition": condition,
                        "batch": batch,
                        "n": len(units_by_cell[(condition, batch)]),
                    }
                )
        condition_sets = defaultdict(set)
        for (condition, batch), units in units_by_cell.items():
            if units:
                condition_sets[condition].add(batch)
        disjoint = all(
            not (condition_sets[left] & condition_sets[right])
            for index, left in enumerate(condition_sets)
            for right in list(condition_sets)[index + 1 :]
        )
        if len(condition_sets) > 1 and len(batch_levels) > 1 and disjoint:
            issues.append(
                _issue(
                    "error",
                    "condition-batch-confounded",
                    "Condition is confounded with batch",
                    "No batch contains biological units from more than one condition; condition and batch effects cannot be separated reliably.",
                )
            )

    if design["unresolved"]:
        severity = "warning" if design["partial"] else "error"
        issues.append(
            _issue(
                severity,
                "unknown-design-factor",
                "Design formula references unknown columns",
                f"Unknown factors: {', '.join(design['unresolved'])}.",
            )
        )
    if design["constant"]:
        issues.append(
            _issue(
                "error",
                "constant-design-factor",
                "Design factors have no variation",
                f"Constant factors: {', '.join(design['constant'])}.",
            )
        )
    if design["columns"] and not design["full_rank"]:
        issues.append(
            _issue(
                "error",
                "rank-deficient-design",
                "Design matrix is rank deficient",
                f"Formula {formula!r} has rank {design['rank']} for {len(design['columns'])} columns.",
            )
        )
    if design["partial"]:
        issues.append(
            _issue(
                "info",
                "complex-formula",
                "Complex formula detected",
                "Main-effect rank was checked; interaction/transform estimability still requires the downstream statistical engine.",
            )
        )
    if subject_col and condition_col and subject_col in design["factors"]:
        expected_conditions = {
            row.get(condition_col, "").strip()
            for row in rows
            if row.get(condition_col, "").strip()
        }
        observed_by_subject: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            subject = row.get(subject_col, "").strip()
            condition = row.get(condition_col, "").strip()
            if subject and condition:
                observed_by_subject[subject].add(condition)
        incomplete = [
            subject
            for subject, observed in observed_by_subject.items()
            if expected_conditions and observed != expected_conditions
        ]
        if incomplete:
            issues.append(
                _issue(
                    "warning",
                    "incomplete-subject-pairing",
                    "Incomplete repeated-measures subjects",
                    f"{len(incomplete)} subjects do not contain every condition level: {', '.join(incomplete[:12])}.",
                )
            )

    errors = sum(item["severity"] == "error" for item in issues)
    warnings = sum(item["severity"] == "warning" for item in issues)
    biological_units = len(
        {
            (
                f"{row.get(condition_col, '').strip()}|{row.get(unit_col, '').strip()}"
                if replicate_col and not subject_col and condition_col
                else row.get(unit_col, "").strip()
                if unit_col
                else str(index)
            )
            or "(missing biological unit)"
            for index, row in enumerate(rows, 1)
        }
    )
    inputs_fingerprint = hashlib.sha256(
        "\n".join(sorted(input_records)).encode()
    ).hexdigest()
    normalized_payload = {
        "sheet_sha256": hashlib.sha256(sheet_path.read_bytes()).hexdigest(),
        "sheet": selected.get("path", ""),
        "config_key": selected.get("config_key", ""),
        "config": _display_path(pipeline.path, config_path) if config_path else "",
        "roles": inferred,
        "formula": formula,
        "inputs_fingerprint": inputs_fingerprint,
        "error_codes": sorted(
            item["code"] for item in issues if item["severity"] == "error"
        ),
    }
    fingerprint = hashlib.sha256(
        json.dumps(normalized_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    gate = "blocked" if errors else "review" if warnings else "ready"
    preview_columns = list(
        dict.fromkeys(column for column in inferred.values() if column)
    )
    ordered_issues = sorted(
        issues, key=lambda item: {"error": 0, "warning": 1, "info": 2}[item["severity"]]
    )
    return {
        "generated": None,
        "pipeline": pipeline.name,
        "configfile": _display_path(pipeline.path, config_path) if config_path else "",
        "candidates": candidates,
        "selected": selected,
        "roles": inferred,
        "summary": {
            "samples": len(rows),
            "biological_units": biological_units,
            "groups": len(distributions.get("condition", [])),
            "batches": len(distributions.get("batch", [])),
            "errors": errors,
            "warnings": warnings,
            "issues_shown": min(len(ordered_issues), MAX_ISSUES),
            "issues_total": len(ordered_issues),
            "paired": bool(inferred["read2"] and paired_rows),
            "input_files": len(used_paths),
        },
        "distributions": distributions,
        "balance": {
            "conditions": condition_levels,
            "batches": batch_levels,
            "cells": balance_cells,
        },
        "design": design,
        "issues": ordered_issues[:MAX_ISSUES],
        "rows": [
            {column: row.get(column, "") for column in preview_columns}
            for row in rows[:250]
        ],
        "columns": columns,
        "gate": gate,
        "score": max(0, 100 - errors * 25 - warnings * 8),
        "fingerprint": fingerprint,
        "inputs_fingerprint": inputs_fingerprint,
    }
