# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Bio-specific QC parsers — turn the artifacts pipelines already emit into
per-sample summary tables for the Results tab.

Each collector returns a "panel": {id, title, columns, rows, note} or None.
Everything is defensive: a malformed file is skipped, never fatal.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from fnmatch import fnmatch
from pathlib import Path

from . import results
from .pipelines import Pipeline

QC_SCAN_SECONDS = 1.5
QC_SCAN_VISITED = 100_000


class _FileInventory(list[Path]):
    """Marker for a bounded, source-safe scan shared by all QC collectors."""


def _scan_files(roots) -> _FileInventory:
    deadline = time.monotonic() + QC_SCAN_SECONDS
    active = deque()
    for root in roots:
        try:
            resolved = root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir():
            active.append(results._walk_source(resolved, deadline))
    files = _FileInventory()
    seen_files: set[Path] = set()
    visited = 0
    while active and visited < QC_SCAN_VISITED and time.monotonic() < deadline:
        iterator = active.popleft()
        try:
            path = next(iterator)
        except StopIteration:
            continue
        active.append(iterator)
        visited += 1
        if path is None:
            continue
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved in seen_files or not resolved.is_file():
            continue
        seen_files.add(resolved)
        files.append(resolved)
    return files


def _iter(roots, patterns, limit=500):
    pats = [p.lower() for p in patterns]
    out = []
    inventory = roots if isinstance(roots, _FileInventory) else _scan_files(roots)
    for f in inventory:
        if len(out) >= limit:
            return out
        if f.is_symlink():
            continue
        n = f.name.lower()
        if any(fnmatch(n, pat) for pat in pats):
            out.append(f)
    return out


def _fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _fmt_pct(x) -> str:
    try:
        return f"{float(x):.1f}%"
    except Exception:
        return str(x)


def _count_lines(f: Path, cap: int = 5_000_000) -> int:
    n = 0
    with open(f, "rb") as fh:
        for _ in fh:
            n += 1
            if n >= cap:
                break
    return n


# ---- collectors ----------------------------------------------------------
def _flagstat(roots):
    rows = []
    for f in _iter(roots, ["*flagstat*"]):
        try:
            txt = f.read_text(errors="replace")
        except Exception:
            continue
        m_total = re.search(r"^(\d+) \+ \d+ in total", txt, re.M)
        m_map = re.search(r"^(\d+) \+ \d+ mapped \(([\d.]+)%", txt, re.M)
        m_pp = re.search(r"^(\d+) \+ \d+ properly paired \(([\d.]+)%", txt, re.M)
        m_dup = re.search(r"^(\d+) \+ \d+ duplicates", txt, re.M)
        if not (m_total and m_map):
            continue
        sample = re.sub(r"\.flagstat.*$", "", f.name, flags=re.I)
        rows.append(
            [
                sample,
                _fmt_int(m_total.group(1)),
                _fmt_int(m_map.group(1)),
                _fmt_pct(m_map.group(2)),
                _fmt_pct(m_pp.group(2)) if m_pp else "—",
                _fmt_int(m_dup.group(1)) if m_dup else "—",
            ]
        )
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    return {
        "id": "flagstat",
        "title": "Alignment (samtools flagstat)",
        "columns": [
            "Sample",
            "Total reads",
            "Mapped",
            "Mapped %",
            "Properly paired %",
            "Duplicates",
        ],
        "rows": rows,
        "note": f"{len(rows)} files",
    }


def _fastp(roots):
    rows = []
    for f in _iter(roots, ["*.fastp.json"]):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        s = d.get("summary", {})
        bf, af = s.get("before_filtering", {}), s.get("after_filtering", {})
        sample = re.sub(r"\.fastp\.json$", "", f.name, flags=re.I)
        rows.append(
            [
                sample,
                _fmt_int(bf.get("total_reads", 0)),
                _fmt_int(af.get("total_reads", 0)),
                _fmt_pct(af.get("q30_rate", 0) * 100),
                _fmt_pct(af.get("gc_content", 0) * 100),
                _fmt_pct(d.get("duplication", {}).get("rate", 0) * 100),
            ]
        )
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    return {
        "id": "fastp",
        "title": "Read QC (fastp)",
        "columns": [
            "Sample",
            "Reads (raw)",
            "Reads (filtered)",
            "Q30 %",
            "GC %",
            "Dup %",
        ],
        "rows": rows,
        "note": f"{len(rows)} samples",
    }


def _star(roots):
    rows = []
    for f in _iter(roots, ["*log.final.out"]):
        try:
            txt = f.read_text(errors="replace")
        except Exception:
            continue

        def grab(label):
            m = re.search(re.escape(label) + r"\s*\|\s*(.+)", txt)
            return m.group(1).strip() if m else "—"

        sample = re.sub(r"\.?log\.final\.out$", "", f.name, flags=re.I).rstrip("._")
        rows.append(
            [
                sample,
                grab("Number of input reads"),
                grab("Uniquely mapped reads %"),
                grab("% of reads mapped to multiple loci"),
                grab("% of reads unmapped: too short"),
            ]
        )
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    return {
        "id": "star",
        "title": "Alignment (STAR)",
        "columns": [
            "Sample",
            "Input reads",
            "Unique %",
            "Multi %",
            "Unmapped (short) %",
        ],
        "rows": rows,
        "note": f"{len(rows)} samples",
    }


def _featurecounts(roots):
    panels_rows = []
    for f in _iter(roots, ["*.summary"]):
        try:
            lines = [
                line.split("\t") for line in f.read_text().splitlines() if line.strip()
            ]
        except Exception:
            continue
        if not lines or lines[0][0] != "Status":
            continue
        samples = [Path(s).stem for s in lines[0][1:]]
        data = {
            row[0]: [float(x) for x in row[1:]] for row in lines[1:] if len(row) > 1
        }
        assigned = data.get("Assigned", [0] * len(samples))
        totals = [sum(vals[i] for vals in data.values()) for i in range(len(samples))]
        for i, sm in enumerate(samples):
            tot = totals[i] or 1
            panels_rows.append(
                [
                    sm,
                    _fmt_int(assigned[i]),
                    _fmt_int(totals[i]),
                    _fmt_pct(assigned[i] / tot * 100),
                ]
            )
    if not panels_rows:
        return None
    panels_rows.sort(key=lambda r: r[0])
    return {
        "id": "featurecounts",
        "title": "Quantification (featureCounts)",
        "columns": ["Sample", "Assigned", "Total", "Assigned %"],
        "rows": panels_rows,
        "note": f"{len(panels_rows)} samples",
    }


def _peaks(roots):
    rows = []
    for f in _iter(roots, ["*.narrowpeak", "*.broadpeak", "*peaks.bed"]):
        try:
            n = _count_lines(f)
        except Exception:
            continue
        sample = re.sub(
            r"(_peaks)?\.(narrow|broad)peak$|(_peaks)?\.bed$", "", f.name, flags=re.I
        )
        kind = (
            "narrow"
            if f.name.lower().endswith("narrowpeak")
            else ("broad" if f.name.lower().endswith("broadpeak") else "bed")
        )
        rows.append([sample, _fmt_int(n), kind])
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    return {
        "id": "peaks",
        "title": "Peaks (MACS / called peaks)",
        "columns": ["Sample", "Peak count", "Type"],
        "rows": rows,
        "note": f"{len(rows)} files",
    }


def _multiqc(roots):
    files = _iter(roots, ["multiqc_general_stats.txt"], limit=5)
    for f in files:
        try:
            lines = [line.split("\t") for line in f.read_text().splitlines() if line]
        except Exception:
            continue
        if len(lines) < 2:
            continue
        header, body = lines[0], lines[1:]
        # keep only columns that have at least one non-empty value
        keep = [
            i
            for i in range(len(header))
            if i == 0 or any(i < len(r) and r[i].strip() for r in body)
        ]
        keep = keep[:13]

        def clean(col):
            return col.split("-", 1)[-1].replace("_", " ").strip()[:22]

        def cell(v):
            v = v.strip()
            try:
                fv = float(v)
                return f"{fv:.3g}"
            except Exception:
                return v or "—"

        cols = ["Sample"] + [clean(header[i]) for i in keep[1:]]
        rows = [[cell(r[i]) if i < len(r) else "—" for i in keep] for r in body]
        if rows:
            return {
                "id": "multiqc",
                "title": "MultiQC general stats",
                "columns": cols,
                "rows": rows[:200],
                "note": f"{f.parent.name}",
            }
    return None


_COLLECTORS = [_multiqc, _fastp, _star, _flagstat, _featurecounts, _peaks]


def _root_label(pipeline: Pipeline, root: Path) -> str:
    resolved = root.resolve()
    try:
        return resolved.relative_to(pipeline.path.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def gather(p: Pipeline) -> dict:
    roots = results._results_dirs(p, include_attachments=True)
    inventory = _scan_files(roots)
    panels = []
    for fn in _COLLECTORS:
        try:
            panel = fn(inventory)
        except Exception as exc:  # pragma: no cover - keep the tab alive
            panel = {
                "id": fn.__name__,
                "title": fn.__name__,
                "columns": ["error"],
                "rows": [[str(exc)]],
                "note": "parser error",
            }
        if panel:
            panels.append(panel)
    return {"scanned": [_root_label(p, root) for root in roots], "panels": panels}
