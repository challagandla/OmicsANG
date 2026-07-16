# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Schema-aware config forms.

These pipelines ship no JSON schema, so we *infer* one:
  * field types from the current YAML values,
  * help text + enum options mined from the inline comments you already wrote
    (e.g. `species: mouse   # human | mouse | rat`  ->  a dropdown).

Validation catches the failures we kept hitting: wrong enum value, wrong type,
and — crucially — referenced files that don't exist (the MissingInput class).

Saving round-trips through ruamel.yaml so your comments and layout survive.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Optional

from ruamel.yaml import YAML

from . import fs_policy
from .pipelines import Pipeline

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096

_BANNER = re.compile(r"-{4,}")
_PATH_KEY = re.compile(
    r"(_tsv|_yaml|_yml|_dir|_fa|_fasta|_gtf|_gff|_bed|_bam|_csv|_json|"
    r"path|file|conf|index|ref|genome|annotation|fasta|gtf)$",
    re.I,
)
# output locations — pathlike for display, but we must NOT warn if they don't exist yet
_OUTPUT_KEY = re.compile(
    r"(results?_?dir|out_?dir|outdir|output|tmp_?dir|temp_?dir|log_?dir|work_?dir)$",
    re.I,
)
_SPECIES_KEY = re.compile(r"(^|[._-])(species|organism)$", re.I)
_PATH_OPTION_LIMIT = 80
_PATH_SCAN_LIMIT = 5000
_PATH_SKIP_PARTS = {
    ".git",
    ".snakemake",
    ".benchtop",
    ".benchtop-worktrees",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".ipynb_checkpoints",
    "build",
    "dist",
    "logs",
    "benchmarks",
    "tmp",
    "results",
    "result",
    "output",
    "outputs",
    "analysis",
}
_DEFAULT_SPECIES = ["human", "mouse", "rat"]


def _load(path: Path):
    with open(path) as fh:
        return _yaml.load(fh)


def _dump(doc) -> str:
    buf = io.StringIO()
    _yaml.dump(doc, buf)
    return buf.getvalue()


def _inline_comment(parent, key) -> Optional[str]:
    """The end-of-line comment on `key`, cleaned of trailing section banners."""
    try:
        tok = parent.ca.items.get(key)
    except Exception:
        return None
    if not tok or len(tok) < 3 or not tok[2]:
        return None
    first = tok[2].value.split("\n", 1)[0].strip()
    if not first.startswith("#"):
        return None  # the comment is on the next line, not inline
    text = first.lstrip("#").strip()
    if not text or _BANNER.search(text):
        return None
    return text


def _mine_enum(help_text: str, current) -> Optional[list[str]]:
    """Pull `a | b | c` (or `one of: a, b`) option lists out of a comment."""
    if not help_text:
        return None
    base = help_text.split("(", 1)[0]  # drop trailing parentheticals
    cands: list[str] = []
    if "|" in base:
        cands = [c.strip() for c in base.split("|")]
    elif re.search(r"\bor\b", base, re.I):
        quoted = re.findall(r"['\"]([\w.\-]{1,24})['\"]", base)
        cands = quoted or [c.strip() for c in re.split(r"\bor\b", base, flags=re.I)]
    else:
        m = re.search(r"(?:one of|options?|choices?)\s*:?\s*(.+)", base, re.I)
        if m and "," in m.group(1):
            cands = [c.strip() for c in m.group(1).split(",")]
    cands = [c.strip().strip("'\"") for c in cands]
    cands = [c for c in cands if re.fullmatch(r"[\w.\-]{1,24}", c)]
    if 2 <= len(cands) <= 8:
        if current is not None and str(current) not in cands:
            cands.append(str(current))
        return cands
    return None


def _field_type(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _looks_pathlike(key: str, value) -> bool:
    if not isinstance(value, str):
        return False
    if value and (
        "://" in value or " " in value
    ):  # URLs and arg-strings aren't single paths
        return False
    if (
        key.lower() in {"genome", "assembly", "build"}
        and value
        and "/" not in value
        and not Path(value).suffix
    ):
        return False
    if _PATH_KEY.search(key):
        return True
    if not value:
        return False
    if "/" in value:
        return True
    # a real file extension starts with a letter (avoids '0.05', version numbers)
    return bool(re.search(r"\.[A-Za-z]\w{0,4}$", value))


def _species_options(pipeline: Pipeline, current) -> Optional[list[str]]:
    found: list[str] = []
    for rel in (
        "config/species.yaml",
        "config/species.yml",
        "species.yaml",
        "species.yml",
    ):
        path = pipeline.path / rel
        if not path.is_file():
            continue
        try:
            data = _yaml.load(path)
        except Exception:
            continue
        if hasattr(data, "keys"):
            found.extend(str(k) for k in data.keys())
    opts = found or list(_DEFAULT_SPECIES)
    if current is not None and str(current) not in opts:
        opts.append(str(current))
    clean: list[str] = []
    for opt in opts:
        if re.fullmatch(r"[\w.\-]{1,32}", opt) and opt not in clean:
            clean.append(opt)
    return clean if len(clean) >= 2 else None


def _builtin_enum(pipeline: Pipeline, path: list[str], current) -> Optional[list[str]]:
    key = ".".join(path)
    if _SPECIES_KEY.search(key):
        return _species_options(pipeline, current)
    return None


def _path_kind(key: str, value) -> str:
    key = key.lower()
    value = str(value or "")
    if _OUTPUT_KEY.search(key) or re.search(r"(^|[._-])(dir|folder)$", key):
        return "dir"
    if "index" in key and not Path(value).suffix:
        return "any"
    return "file"


def _path_extensions(key: str, value) -> tuple[str, ...]:
    key = key.lower()
    value = str(value or "").lower()
    if re.search(r"(sample|samples|sheet)", key):
        return (".tsv", ".csv", ".txt")
    if re.search(r"(fasta|fa|genome)$", key):
        return (".fa", ".fasta", ".fna", ".fas", ".fa.gz", ".fasta.gz", ".fna.gz")
    if re.search(r"(gtf|gff|annotation)", key):
        return (".gtf", ".gff", ".gff3", ".gtf.gz", ".gff.gz", ".gff3.gz")
    if re.search(r"(bed|blacklist|peaks|regions|chrom)", key):
        return (
            ".bed",
            ".bed.gz",
            ".narrowpeak",
            ".broadpeak",
            ".chrom.sizes",
            ".sizes",
            ".txt",
        )
    if "bam" in key:
        return (".bam", ".sam", ".cram")
    if "fastq" in key or re.search(r"(^|[._-])fq([._-]|$)", key):
        return (".fastq", ".fq", ".fastq.gz", ".fq.gz")
    if "yaml" in key or "yml" in key or "config" in key or "conf" in key:
        return (".yaml", ".yml", ".json", ".toml", ".conf", ".cfg")
    if "csv" in key:
        return (".csv",)
    if "tsv" in key:
        return (".tsv",)
    if "json" in key:
        return (".json", ".jsonl")
    if "index" in key:
        return (
            ".bt2",
            ".bt2l",
            ".ebwt",
            ".bwt",
            ".ann",
            ".amb",
            ".pac",
            ".sa",
            ".mmi",
            ".idx",
        )
    for suffixes in (
        (".fa.gz", ".fasta.gz", ".fq.gz", ".fastq.gz", ".gtf.gz", ".gff.gz", ".bed.gz"),
        (Path(value).suffix,),
    ):
        for suffix in suffixes:
            if suffix and value.endswith(suffix):
                return (suffix,)
    return ()


def _matches_path_field(path: Path, exts: tuple[str, ...]) -> bool:
    if not exts:
        return True
    name = path.name.lower()
    return any(name.endswith(ext) for ext in exts)


def _path_priority(rel: str, key: str, current: str) -> tuple[int, int, str]:
    first = rel.split("/", 1)[0]
    preferred = {
        "config": 0,
        "project_local": 1,
        ".test": 2,
        "resources": 3,
        "reference": 4,
        "references": 4,
        "refs": 4,
        "data": 5,
        "raw": 6,
        "workflow": 7,
        "envs": 8,
    }
    score = preferred.get(first, 20)
    key_bits = [b for b in re.split(r"[^a-z0-9]+", key.lower()) if len(b) > 2]
    rel_l = rel.lower()
    if current and rel == current:
        score -= 5
    if any(bit in rel_l for bit in key_bits):
        score -= 1
    return score, rel.count("/"), rel_l


def _path_options(pipeline: Pipeline, relpath: str, key: str, value) -> list[dict]:
    root = pipeline.path.resolve()
    kind = _path_kind(key, value)
    exts = _path_extensions(key, value)
    current = str(value or "")
    options: dict[str, str] = {}
    scanned = 0

    def add(path: Path, item_kind: str) -> None:
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except (OSError, ValueError):
            return
        if rel and rel not in options:
            options[rel] = item_kind

    if current:
        candidate = Path(current).expanduser()
        if not candidate.is_absolute():
            for base in (root, root / relpath):
                target = (base.parent if base.is_file() else base) / candidate
                if target.exists():
                    add(target, "dir" if target.is_dir() else "file")

    for dirpath, dirnames, filenames in os.walk(root):
        rel_parts = ()
        try:
            rel_parts = Path(dirpath).resolve().relative_to(root).parts
        except (OSError, ValueError):
            pass
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _PATH_SKIP_PARTS and not (d.startswith(".") and d != ".test")
        ]
        if any(part in _PATH_SKIP_PARTS for part in rel_parts):
            dirnames[:] = []
            continue
        scanned += 1
        if scanned > _PATH_SCAN_LIMIT:
            break
        here = Path(dirpath)
        if kind in {"dir", "any"}:
            for dirname in dirnames:
                add(here / dirname, "dir")
                if len(options) >= _PATH_OPTION_LIMIT * 3:
                    break
        if kind in {"file", "any"}:
            for filename in filenames:
                fpath = here / filename
                if _matches_path_field(fpath, exts):
                    add(fpath, "file")
                if len(options) >= _PATH_OPTION_LIMIT * 3:
                    break
        if len(options) >= _PATH_OPTION_LIMIT * 3:
            break

    selected = sorted(
        options.items(), key=lambda item: _path_priority(item[0], key, current)
    )
    return [
        {"value": rel, "kind": item_kind}
        for rel, item_kind in selected[:_PATH_OPTION_LIMIT]
    ]


def _emit(pipeline: Pipeline, relpath: str, parent, key, val, p, section, fields):
    """Build a field for `key` whose comment lives on `parent` (a ruamel node)."""
    help_text = _inline_comment(parent, key)
    if hasattr(val, "items"):  # nested mapping -> flatten
        _walk(pipeline, relpath, val, p, section, fields)
        return
    label = ".".join(p[1:]) if len(p) > 1 else p[0]
    if isinstance(val, list):
        scalars = all(not hasattr(i, "items") and not isinstance(i, list) for i in val)
        if scalars:
            fields.append(
                {
                    "id": ".".join(p),
                    "path": p,
                    "section": section,
                    "label": label,
                    "type": "array",
                    "value": [str(x) for x in val],
                    "help": help_text,
                }
            )
        else:  # list of dicts -> raw yaml subtree
            fields.append(
                {
                    "id": ".".join(p),
                    "path": p,
                    "section": section,
                    "label": label,
                    "type": "yaml",
                    "value": _dump({key: val}).rstrip(),
                    "help": help_text,
                }
            )
        return
    ftype = _field_type(val)
    field = {
        "id": ".".join(p),
        "path": p,
        "section": section,
        "label": label,
        "type": ftype,
        "value": val,
        "help": help_text,
    }
    enum = _mine_enum(help_text or "", val) or _builtin_enum(pipeline, p, val)
    if enum and ftype == "string":
        field["type"] = "enum"
        field["enum"] = enum
    if _looks_pathlike(str(key), val):
        field["pathlike"] = True
        field["path_kind"] = _path_kind(str(key), val)
        options = _path_options(pipeline, relpath, str(key), val)
        if options:
            field["path_options"] = options
    fields.append(field)


def _walk(pipeline: Pipeline, relpath: str, node, path, section, fields):
    if not hasattr(node, "items"):
        return
    for key, val in node.items():
        _emit(pipeline, relpath, node, key, val, path + [str(key)], section, fields)


def build(pipeline: Pipeline, relpath: str) -> dict:
    _, target = fs_policy.resolve_relative(
        pipeline.path,
        relpath,
        must_exist=True,
        expected="file",
    )
    doc = _load(target)
    fields: list[dict] = []
    general: list[dict] = []
    # top-level scalars go to a "General" section; dict blocks become sections
    if hasattr(doc, "items"):
        for key, val in doc.items():
            if hasattr(val, "items"):
                _walk(pipeline, relpath, val, [str(key)], str(key), fields)
            else:
                _emit(pipeline, relpath, doc, key, val, [str(key)], "General", general)
    # order: General first, then sections in file order
    sections: list[dict] = []
    if general:
        sections.append({"name": "General", "fields": general})
    seen = {}
    for f in fields:
        seen.setdefault(f["section"], []).append(f)
    for name, fs in seen.items():
        sections.append({"name": name, "fields": fs})
    return {"path": relpath, "sections": sections, "schema_present": False}


# ---- coercion + validation ----------------------------------------------
def _coerce(ftype: str, value):
    if ftype == "boolean":
        return (
            bool(value)
            if isinstance(value, bool)
            else str(value).lower() in ("1", "true", "yes", "on")
        )
    if ftype == "integer":
        return int(value)
    if ftype == "number":
        return float(value)
    if ftype == "array":
        items = value if isinstance(value, list) else str(value).splitlines()
        out = []
        for x in items:
            x = str(x).strip()
            if x == "":
                continue
            try:
                out.append(int(x))
            except ValueError:
                try:
                    out.append(float(x))
                except ValueError:
                    out.append(x)
        return out
    if ftype == "yaml":
        return _yaml.load(value)
    return str(value)


def validate(pipeline: Pipeline, fields: list[dict]) -> list[dict]:
    errs: list[dict] = []
    for f in fields:
        where = f.get("id") or ".".join(f.get("path", []))
        ftype, raw = f.get("type", "string"), f.get("value")
        try:
            val = _coerce(ftype, raw)
        except Exception as exc:
            errs.append(
                {
                    "level": "error",
                    "where": where,
                    "message": f"expected {ftype}: {exc}",
                }
            )
            continue
        if (
            ftype == "enum"
            and f.get("enum")
            and str(val) not in [str(e) for e in f["enum"]]
        ):
            errs.append(
                {
                    "level": "error",
                    "where": where,
                    "message": f"must be one of {f['enum']}",
                }
            )
        key = (f.get("path") or [""])[-1]
        if (
            f.get("pathlike")
            and isinstance(val, str)
            and val
            and "://" not in val
            and not _OUTPUT_KEY.search(key)
        ):
            if not (pipeline.path / val).exists():
                errs.append(
                    {
                        "level": "warn",
                        "where": where,
                        "message": f"referenced path not found: {val}",
                    }
                )
    return errs


def save(pipeline: Pipeline, relpath: str, fields: list[dict]) -> dict:
    relpath, target = fs_policy.resolve_relative(
        pipeline.path,
        relpath,
        must_exist=True,
        expected="file",
    )
    doc = _load(target)
    for f in fields:
        path = f.get("path") or []
        if not path:
            continue
        try:
            val = _coerce(f.get("type", "string"), f.get("value"))
        except Exception:
            continue
        if f.get("type") == "yaml":
            # yaml subtree was serialized as {lastkey: subtree}
            val = val.get(path[-1], val) if hasattr(val, "get") else val
        node = doc
        ok = True
        for k in path[:-1]:
            if hasattr(node, "__contains__") and k in node:
                node = node[k]
            else:
                ok = False
                break
        if ok and hasattr(node, "__setitem__"):
            node[path[-1]] = val
    backup = fs_policy.private_backup(
        pipeline.path,
        target,
        namespace=pipeline.name,
    )
    rendered = io.StringIO()
    _yaml.dump(doc, rendered)
    fs_policy.atomic_write_text(target, rendered.getvalue())
    return {"ok": True, "path": relpath, "backup": backup}
