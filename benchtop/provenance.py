# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Run capsules: bounded, durable execution evidence and scientific diffs."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from . import pipelines as pipeline_tools
from . import qc, results, runplan, settings, study
from .pipelines import Pipeline

SCHEMA_VERSION = 1
FULL_HASH_LIMIT = 4_000_000
SAMPLE_HASH_BYTES = 512_000
RESULT_SAMPLE_BYTES = 32_768
MAX_INVENTORY_FILES = 1_200
CAPSULE_ID_RE = re.compile(r"^[0-9a-f]{12,32}$")
SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|credential|api[_-]?key)", re.I
)
SOURCE_SUFFIXES = {".smk", ".py", ".r", ".R", ".sh", ".yaml", ".yml", ".toml", ".json"}
INVENTORY_SKIP = {".git", ".snakemake", ".cache", "logs", "benchmarks", "tmp"}


def _inside(root: Path, path: Path) -> bool:
    root = root.resolve()
    path = path.resolve()
    return path == root or root in path.parents


def _pipeline_key(pipeline: Pipeline) -> str:
    slug = (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", pipeline.path.name).strip("._") or "pipeline"
    )
    suffix = hashlib.sha256(str(pipeline.path.resolve()).encode()).hexdigest()[:10]
    return f"{slug}-{suffix}"


def _capsule_dir(pipeline: Pipeline) -> Path:
    return settings.STATE_DIR / "capsules" / _pipeline_key(pipeline)


def _capsule_path(pipeline: Pipeline, capsule_id: str) -> Path:
    if not CAPSULE_ID_RE.fullmatch(capsule_id or ""):
        raise ValueError("invalid capsule id")
    return _capsule_dir(pipeline) / f"{capsule_id}.json"


def _write(pipeline: Pipeline, capsule: dict) -> None:
    path = _capsule_path(pipeline, str(capsule["id"]))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    payload = json.dumps(capsule, indent=2, sort_keys=True, ensure_ascii=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(payload)
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def load(pipeline: Pipeline, capsule_id: str) -> dict:
    path = _capsule_path(pipeline, capsule_id)
    if not path.is_file():
        raise FileNotFoundError("capsule not found")
    try:
        capsule = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"capsule is unreadable: {exc}") from exc
    recorded_root = capsule.get("pipeline", {}).get("root")
    if not recorded_root or Path(recorded_root).resolve() != pipeline.path.resolve():
        raise ValueError("capsule belongs to a different pipeline checkout")
    return capsule


def capsule_reference(
    pipeline: Pipeline,
    capsule: dict,
    *,
    job_id: str,
    status: str,
) -> dict:
    """Return the bounded database projection for a filesystem capsule."""
    capsule_id = str(capsule.get("id") or "")
    return {
        "id": capsule_id,
        "pipeline": pipeline.name,
        "job_id": job_id,
        "status": status,
        "path": str(_capsule_path(pipeline, capsule_id)),
        "fingerprint": str(capsule.get("fingerprint") or ""),
        "created": float(capsule.get("created") or time.time()),
        "plan_digest": str((capsule.get("run_plan") or {}).get("digest") or ""),
    }


def _hash_file(path: Path) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    if stat.st_size <= FULL_HASH_LIMIT:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        mode = "full"
    else:
        with path.open("rb") as handle:
            digest.update(handle.read(SAMPLE_HASH_BYTES))
            handle.seek(max(0, stat.st_size - SAMPLE_HASH_BYTES))
            digest.update(handle.read(SAMPLE_HASH_BYTES))
        digest.update(str(stat.st_size).encode())
        mode = "sampled"
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "digest": digest.hexdigest(),
        "hash_mode": mode,
    }


def _sample_hash_file(path: Path, chunk_size: int = RESULT_SAMPLE_BYTES) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    offsets = (
        0,
        max(0, stat.st_size // 2 - chunk_size // 2),
        max(0, stat.st_size - chunk_size),
    )
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            digest.update(handle.read(chunk_size))
    digest.update(str(stat.st_size).encode())
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "digest": digest.hexdigest(),
        "hash_mode": "sampled-3",
    }


def _artifact(
    root: Path, path: Path, role: str, *, bounded_large: bool = False
) -> dict | None:
    try:
        path = path.resolve()
        if not _inside(root, path) or not path.is_file() or path.is_symlink():
            return None
        stat = path.stat()
        if bounded_large and stat.st_size > FULL_HASH_LIMIT:
            data = _sample_hash_file(path)
        else:
            data = _hash_file(path)
    except OSError:
        return None
    return {"path": path.relative_to(root.resolve()).as_posix(), "role": role, **data}


def _aggregate(items: list[dict]) -> str:
    records = [
        f"{item.get('role', '')}|{item.get('path', '')}|{item.get('size', '')}|{item.get('digest', '')}"
        for item in sorted(
            items, key=lambda value: (value.get("role", ""), value.get("path", ""))
        )
    ]
    return hashlib.sha256("\n".join(records).encode()).hexdigest()


def _redact_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(
        r"(?i)(\b(?:api[_-]?key|token|secret|password|passwd|credential)\b\s*=\s*)[^\s,]+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)(--?(?:api[-_]?key|token|secret|password|passwd|credential)\s+)[^\s]+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1<redacted>@", text)
    return runplan.redact_text(text)


def redact_text(value: str) -> str:
    """Remove common credential forms before durable history is written."""
    return _redact_text(value)


def _sanitize_remote(value: str) -> str:
    text = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1<redacted>@", str(value or ""))
    return re.sub(
        r"(?i)(?<![A-Za-z0-9._-])[^/@:\s]+@(?=[A-Za-z0-9.-]+:)",
        "<redacted>@",
        text,
    )


def _git_command(root: Path, args: list[str], timeout: int = 8) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_snapshot(root: Path) -> dict:
    inside = _git_command(root, ["rev-parse", "--is-inside-work-tree"])
    if inside != "true":
        return {
            "is_repo": False,
            "branch": "",
            "commit": "",
            "remote": "",
            "dirty": 0,
            "dirty_files": [],
            "state_digest": "",
        }
    status = _git_command(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    lines = [line for line in status.splitlines() if line.strip()]
    return {
        "is_repo": True,
        "branch": _git_command(root, ["branch", "--show-current"]),
        "commit": _git_command(root, ["rev-parse", "HEAD"]),
        "remote": _sanitize_remote(_git_command(root, ["remote", "get-url", "origin"])),
        "dirty": len(lines),
        "dirty_files": lines[:200],
        "state_digest": hashlib.sha256(status.encode()).hexdigest(),
    }


def _safe_file(root: Path, raw: str) -> Path | None:
    if not raw:
        return None
    path = (root / raw).resolve()
    return path if _inside(root, path) and path.is_file() else None


def _source_files(pipeline: Pipeline, launch: dict) -> list[dict]:
    root = pipeline.path.resolve()
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(path: Path | None, role: str) -> None:
        if path is None:
            return
        item = _artifact(root, path, role)
        if item and (role, item["path"]) not in seen:
            seen.add((role, item["path"]))
            items.append(item)

    add(_safe_file(root, pipeline.snakefile or ""), "workflow")
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix in SOURCE_SUFFIXES:
            add(path, "workflow")
    for folder_name in ("workflow", "rules", "modules", "scripts", "src", "lib", "bin"):
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if len(items) >= 400:
                break
            if path.suffix in SOURCE_SUFFIXES:
                add(path, "workflow")

    config_path = _safe_file(root, str(launch.get("configfile") or ""))
    if config_path is None:
        config_path = next(
            (
                _safe_file(root, rel)
                for rel in pipeline.configs
                if _safe_file(root, rel)
            ),
            None,
        )
    add(config_path, "config")
    add(_safe_file(root, pipeline.env_file or ""), "environment")
    for path in (
        sorted((root / "envs").glob("*.y*ml")) if (root / "envs").is_dir() else []
    ):
        add(path, "environment")
    profile = str(launch.get("profile") or "")
    if profile:
        for name in ("config.yaml", "config.yml"):
            add(_safe_file(root, f"profiles/{profile}/{name}"), "profile")
    for path, role in (
        (root / ".gitnexus" / "meta.json", "gitnexus-index"),
        (root / "graphify-out" / "graph.json", "graphify-index"),
    ):
        add(path, role)
    items.sort(key=lambda item: (item["role"], item["path"]))
    return items


def _flatten_config(value: Any, prefix: str = "", out: dict | None = None) -> dict:
    out = out if out is not None else {}
    if len(out) >= 500:
        return out
    if isinstance(value, dict):
        for key in sorted(value, key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            if SECRET_KEY_RE.search(str(key)):
                out[child] = "<redacted>"
            else:
                _flatten_config(value[key], child, out)
    elif isinstance(value, list):
        for index, child_value in enumerate(value[:100]):
            _flatten_config(child_value, f"{prefix}[{index}]", out)
    else:
        text = (
            json.dumps(value, ensure_ascii=True)
            if not isinstance(value, str)
            else value
        )
        out[prefix or "value"] = _redact_text(text[:240])
    return out


def _config_snapshot(pipeline: Pipeline, launch: dict) -> dict:
    raw = str(launch.get("configfile") or "")
    path = _safe_file(pipeline.path, raw)
    if path is None:
        path = next(
            (
                _safe_file(pipeline.path, rel)
                for rel in pipeline.configs
                if _safe_file(pipeline.path, rel)
            ),
            None,
        )
    if path is None:
        return {"path": "", "digest": "", "values": {}}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        values = _flatten_config(data if data is not None else {})
        digest = _hash_file(path)["digest"]
    except Exception:
        values, digest = {}, ""
    return {
        "path": path.relative_to(pipeline.path.resolve()).as_posix(),
        "digest": digest,
        "values": values,
    }


def _result_category(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "figure"
    if suffix in {".csv", ".tsv", ".txt", ".parquet"}:
        return "table"
    if suffix in {".html", ".pdf"}:
        return "report"
    if suffix == ".ipynb":
        return "notebook"
    return "artifact"


def _pipeline_for_launch(pipeline: Pipeline, launch: dict | None) -> Pipeline:
    configfile = str((launch or {}).get("configfile") or "")
    config_path = _safe_file(pipeline.path, configfile)
    if config_path is None:
        return pipeline
    rel = config_path.relative_to(pipeline.path.resolve()).as_posix()
    return replace(pipeline, configs=[rel])


def _result_inventory(pipeline: Pipeline, launch: dict | None = None) -> dict:
    scan_pipeline = _pipeline_for_launch(pipeline, launch)
    root = pipeline.path.resolve()
    items: list[dict] = []
    external_roots = 0
    seen_paths: set[Path] = set()
    candidates: set[Path] = set()
    for result_root in results._results_dirs(scan_pipeline):
        try:
            candidates.add(result_root.resolve())
        except OSError:
            continue
    resolved_roots: list[Path] = []
    for resolved in sorted(candidates, key=lambda path: len(path.parts)):
        if any(
            parent == resolved or parent in resolved.parents
            for parent in resolved_roots
        ):
            continue
        resolved_roots.append(resolved)
    for resolved in resolved_roots:
        is_external = not _inside(root, resolved)
        if is_external:
            external_roots += 1
        external_label = f"{resolved.name or 'results'}-{hashlib.sha256(str(resolved).encode()).hexdigest()[:8]}"
        for path in resolved.rglob("*"):
            if len(items) >= MAX_INVENTORY_FILES:
                break
            try:
                actual = path.resolve()
                if actual in seen_paths or not _inside(resolved, actual):
                    continue
                rel_parts = actual.relative_to(resolved).parts
                if any(part in INVENTORY_SKIP for part in rel_parts):
                    continue
                if not actual.is_file() or path.is_symlink():
                    continue
            except (OSError, ValueError):
                continue
            seen_paths.add(actual)
            try:
                data = (
                    _sample_hash_file(actual)
                    if actual.stat().st_size > FULL_HASH_LIMIT
                    else _hash_file(actual)
                )
            except OSError:
                continue
            display = (
                actual.relative_to(root).as_posix()
                if not is_external
                else f"external/{external_label}/{actual.relative_to(resolved).as_posix()}"
            )
            items.append({"path": display, "role": _result_category(actual), **data})
        if len(items) >= MAX_INVENTORY_FILES:
            break
    items.sort(key=lambda item: item["path"])
    return {
        "items": items,
        "count": len(items),
        "digest": _aggregate(items),
        "truncated": len(items) >= MAX_INVENTORY_FILES,
        "external_roots": external_roots,
    }


def _inventory_delta(before: dict, after: dict) -> dict:
    left = {item["path"]: item for item in before.get("items", [])}
    right = {item["path"]: item for item in after.get("items", [])}
    added = sorted(set(right) - set(left))
    removed = sorted(set(left) - set(right))
    changed = sorted(
        path
        for path in set(left) & set(right)
        if left[path].get("digest") != right[path].get("digest")
        or left[path].get("size") != right[path].get("size")
    )
    return {
        "added": added[:300],
        "removed": removed[:300],
        "changed": changed[:300],
        "attribution": "observed between this run's start and end snapshots",
    }


def _qc_snapshot(pipeline: Pipeline, launch: dict | None = None) -> dict:
    scan_pipeline = _pipeline_for_launch(pipeline, launch)
    try:
        data = qc.gather(scan_pipeline)
    except Exception as exc:
        return {"panels": [], "digest": "", "error": str(exc)}
    panels = data.get("panels", [])[:20]
    payload = json.dumps(
        panels, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return {
        "panels": panels,
        "digest": hashlib.sha256(payload.encode()).hexdigest(),
        "error": "",
    }


def _study_snapshot(pipeline: Pipeline, launch: dict) -> dict:
    try:
        report = study.audit(
            pipeline,
            configfile=str(launch.get("configfile") or ""),
            sheet="",
            roles=dict(launch.get("study_roles") or {}),
        )
    except Exception as exc:
        return {
            "gate": "unknown",
            "fingerprint": "",
            "inputs_fingerprint": "",
            "error": str(exc),
            "summary": {},
        }
    selected = report.get("selected") or {}
    return {
        "gate": report.get("gate", "unknown"),
        "score": report.get("score", 0),
        "fingerprint": report.get("fingerprint", ""),
        "inputs_fingerprint": report.get("inputs_fingerprint", ""),
        "sheet": selected.get("path", ""),
        "roles": report.get("roles", {}),
        "summary": report.get("summary", {}),
        "issues": [
            {key: item.get(key) for key in ("severity", "code", "title", "message")}
            for item in report.get("issues", [])[:100]
        ],
        "error": "",
    }


def _installed_environment(driver_env: str) -> dict:
    if not driver_env:
        return {"kind": "", "package_count": 0, "packages": [], "digest": ""}
    try:
        index = pipeline_tools.env_index()
    except Exception:
        index = {}
    entry = index.get(driver_env, {})
    raw_path = str(entry.get("path") or "")
    if not raw_path and Path(driver_env).is_absolute():
        raw_path = driver_env
    conda_meta = Path(raw_path) / "conda-meta" if raw_path else None
    if conda_meta is None or not conda_meta.is_dir():
        return {"kind": "unresolved", "package_count": 0, "packages": [], "digest": ""}
    try:
        packages = sorted(
            path.stem for path in conda_meta.glob("*.json") if path.is_file()
        )
        history = conda_meta / "history"
        history_digest = _hash_file(history)["digest"] if history.is_file() else ""
    except OSError:
        return {"kind": "conda", "package_count": 0, "packages": [], "digest": ""}
    digest = hashlib.sha256(
        ("\n".join(packages) + "\nhistory=" + history_digest).encode()
    ).hexdigest()
    return {
        "kind": "conda",
        "package_count": len(packages),
        "packages": packages[:400],
        "truncated": len(packages) > 400,
        "digest": digest,
    }


def _environment_snapshot(
    pipeline: Pipeline, launch: dict, source_files: list[dict]
) -> dict:
    files = [
        item for item in source_files if item["role"] in {"environment", "profile"}
    ]
    driver_env = str(launch.get("env") or pipeline.conda_env or "")
    installed = _installed_environment(driver_env)
    runtime = {
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    digest = hashlib.sha256(
        json.dumps(
            {
                "files": _aggregate(files),
                "installed": installed.get("digest", ""),
                **runtime,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "driver_env": driver_env,
        "declared_env": pipeline.conda_env or "",
        "conda_exe": pipeline_tools._conda_exe() or "",
        **runtime,
        "files": files,
        "installed": installed,
        "digest": digest,
    }


def _launch_from_meta(meta: dict) -> dict:
    keys = (
        "human",
        "target",
        "configfile",
        "profile",
        "cores",
        "dryrun",
        "use_conda",
        "extra",
        "env",
        "study_sheet",
        "study_roles",
        "study_override",
    )
    launch = {key: meta.get(key) for key in keys if key in meta}
    for key in ("human", "extra"):
        if key in launch:
            launch[key] = runplan.redact_command_text(str(launch[key] or ""))
    return launch


def _base_capsule(
    pipeline: Pipeline,
    capsule_id: str,
    launch: dict,
    run_plan: Any | None = None,
) -> dict:
    source_files = _source_files(pipeline, launch)
    workflow = [item for item in source_files if item["role"] == "workflow"]
    indexes = [
        item
        for item in source_files
        if item["role"] in {"gitnexus-index", "graphify-index"}
    ]
    config = _config_snapshot(pipeline, launch)
    cohort = _study_snapshot(pipeline, launch)
    environment = _environment_snapshot(pipeline, launch, source_files)
    before = _result_inventory(pipeline, launch)
    plan_record = run_plan.record() if run_plan is not None else None
    core = {
        "git": _git_snapshot(pipeline.path),
        "workflow": _aggregate(workflow),
        "config": config.get("digest", ""),
        "cohort": cohort.get("fingerprint", ""),
        "inputs": cohort.get("inputs_fingerprint", ""),
        "environment": environment.get("digest", ""),
        "launch": launch,
        "run_plan": (plan_record or {}).get("digest", ""),
    }
    fingerprint = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "id": capsule_id,
        "pipeline": {
            "name": pipeline.name,
            "local_name": pipeline.path.name,
            "root": str(pipeline.path.resolve()),
        },
        "session": {"id": capsule_id, "status": "running", "started": time.time()},
        "launch": launch,
        "run_plan": plan_record,
        "source": {
            "git": core["git"],
            "files": source_files,
            "workflow_digest": core["workflow"],
            "indexes": indexes,
        },
        "config": config,
        "environment": environment,
        "cohort": cohort,
        "results": {"before": before, "after": None, "delta": None},
        "qc": {"panels": [], "digest": "", "error": "not finalized"},
        "log": {},
        "fingerprint": fingerprint,
        "errors": [],
        "created": time.time(),
    }


def capture(
    pipeline: Pipeline,
    session_id: str,
    meta: dict,
    *,
    run_plan: Any | None = None,
) -> dict:
    if not CAPSULE_ID_RE.fullmatch(session_id or ""):
        raise ValueError("session id cannot be used as a capsule id")
    capsule = _base_capsule(
        pipeline,
        session_id,
        _launch_from_meta(meta),
        run_plan=run_plan,
    )
    _write(pipeline, capsule)
    return capsule


def mark_slurm_submitted(pipeline: Pipeline, capsule_id: str, job_id: str) -> dict:
    capsule = load(pipeline, capsule_id)
    capsule["session"].update({"status": "submitted", "slurm_job_id": str(job_id)})
    capsule["submitted"] = time.time()
    _write(pipeline, capsule)
    return capsule


def mark_slurm_failed(pipeline: Pipeline, capsule_id: str, error: str) -> dict:
    capsule = load(pipeline, capsule_id)
    capsule["session"].update({"status": "submission-failed", "ended": time.time()})
    capsule.setdefault("errors", []).append(
        f"Slurm submission failed: {_redact_text(error)}"
    )
    capsule["finalized"] = time.time()
    _write(pipeline, capsule)
    return capsule


def finalize_slurm(
    pipeline: Pipeline,
    capsule_id: str,
    *,
    status: str,
    job_id: str,
    exit_code: int | None = None,
    logfile: str = "",
) -> dict:
    """Finalize a scheduler-owned capsule using the same result/QC capture as local runs."""
    capsule = load(pipeline, capsule_id)
    session_record = capsule.get("session", {})

    scheduler_session = SimpleNamespace(
        id=capsule_id,
        started=session_record.get("started") or capsule.get("created"),
        ended=time.time(),
        logfile=logfile,
        exit_code=exit_code,
        status=status,
    )
    finalized = finalize(pipeline, scheduler_session)
    finalized["session"].update(
        {
            "slurm_job_id": str(job_id),
            "scheduler_status": status,
        }
    )
    _write(pipeline, finalized)
    return finalized


def finalize(pipeline: Pipeline, session: Any) -> dict:
    capsule = load(pipeline, str(session.id))
    launch = capsule.get("launch", {})
    after = _result_inventory(pipeline, launch)
    before = capsule.get("results", {}).get("before") or {"items": [], "digest": ""}
    capsule["results"] = {
        "before": before,
        "after": after,
        "delta": _inventory_delta(before, after),
    }
    capsule["qc"] = _qc_snapshot(pipeline, launch)
    capsule["session"].update(
        {
            "status": session.status,
            "started": session.started,
            "ended": session.ended,
            "exit_code": session.exit_code,
            "duration_s": (
                round(session.ended - session.started, 3)
                if session.started is not None and session.ended is not None
                else None
            ),
        }
    )
    logfile = Path(session.logfile).resolve() if session.logfile else None
    if logfile and logfile.is_file() and _inside(settings.STATE_DIR, logfile):
        try:
            capsule["log"] = {"path": str(logfile), **_hash_file(logfile)}
        except OSError as exc:
            capsule["errors"].append(f"log fingerprint failed: {exc}")
    capsule["finalized"] = time.time()
    _write(pipeline, capsule)
    return capsule


def _summary(capsule: dict) -> dict:
    session = capsule.get("session", {})
    git = capsule.get("source", {}).get("git", {})
    cohort = capsule.get("cohort", {})
    after = capsule.get("results", {}).get("after") or {}
    delta = capsule.get("results", {}).get("delta") or {}
    plan_record = capsule.get("run_plan")
    plan_digest = plan_record.get("digest", "") if isinstance(plan_record, dict) else ""
    return {
        "id": capsule.get("id"),
        "status": session.get("status", "unknown"),
        "started": session.get("started") or capsule.get("created"),
        "ended": session.get("ended"),
        "duration_s": session.get("duration_s"),
        "fingerprint": capsule.get("fingerprint", ""),
        "plan_digest": plan_digest,
        "commit": git.get("commit", ""),
        "branch": git.get("branch", ""),
        "dirty": git.get("dirty", 0),
        "launch": capsule.get("launch", {}),
        "cohort": {
            "gate": cohort.get("gate", "unknown"),
            "summary": cohort.get("summary", {}),
        },
        "outputs": {
            "count": after.get("count", 0),
            **{key: len(delta.get(key, [])) for key in ("added", "changed", "removed")},
        },
        "complete": bool(capsule.get("finalized")),
    }


def list_capsules(pipeline: Pipeline, limit: int = 50) -> list[dict]:
    folder = _capsule_dir(pipeline)
    if not folder.is_dir():
        return []
    capsules = []
    for path in folder.glob("*.json"):
        try:
            capsule = json.loads(path.read_text(encoding="utf-8"))
            if (
                Path(capsule.get("pipeline", {}).get("root", "")).resolve()
                != pipeline.path.resolve()
            ):
                continue
            capsules.append(_summary(capsule))
        except Exception:
            continue
    capsules.sort(key=lambda item: item.get("started") or 0, reverse=True)
    return capsules[: max(1, min(int(limit), 200))]


def current(pipeline: Pipeline, launch: dict | None = None) -> dict:
    capsule = _base_capsule(pipeline, "current", dict(launch or {}))
    capsule["session"] = {
        "id": "current",
        "status": "working-tree",
        "started": time.time(),
    }
    capsule["qc"] = _qc_snapshot(pipeline, capsule.get("launch", {}))
    capsule["results"]["after"] = capsule["results"]["before"]
    capsule["results"]["delta"] = {"added": [], "removed": [], "changed": []}
    return capsule


def _display(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True, ensure_ascii=True)
    else:
        text = str(value)
    return text if len(text) <= 320 else text[:317] + "..."


def _entry(field: str, left: Any, right: Any, impact: str = "reproducibility") -> dict:
    return {
        "field": field,
        "left": _display(left),
        "right": _display(right),
        "changed": left != right,
        "impact": impact,
    }


def _artifact_entries(
    left: list[dict], right: list[dict], role: str | None = None, limit: int = 120
) -> list[dict]:
    if role:
        left = [item for item in left if item.get("role") == role]
        right = [item for item in right if item.get("role") == role]
    left_map = {item["path"]: item for item in left}
    right_map = {item["path"]: item for item in right}
    entries = []
    for path in sorted(set(left_map) | set(right_map)):
        a, b = left_map.get(path), right_map.get(path)
        left_value = (
            "missing"
            if a is None
            else f"{a.get('digest', '')[:12]} ({a.get('size', 0)} B)"
        )
        right_value = (
            "missing"
            if b is None
            else f"{b.get('digest', '')[:12]} ({b.get('size', 0)} B)"
        )
        if left_value != right_value:
            entries.append(_entry(path, left_value, right_value))
        if len(entries) >= limit:
            break
    return entries


def _config_entries(left: dict, right: dict) -> list[dict]:
    entries = [
        _entry("Config file", left.get("path", ""), right.get("path", "")),
        _entry("Config fingerprint", left.get("digest", ""), right.get("digest", "")),
    ]
    a, b = left.get("values", {}), right.get("values", {})
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            entries.append(
                _entry(
                    key, a.get(key, "<missing>"), b.get(key, "<missing>"), "scientific"
                )
            )
        if len(entries) >= 160:
            break
    return entries


def _qc_values(snapshot: dict) -> dict:
    values = {}
    for panel in snapshot.get("panels", []):
        panel_id = panel.get("id", "qc")
        columns = panel.get("columns", [])
        for row in panel.get("rows", [])[:250]:
            sample = str(row[0]) if row else "sample"
            for index, value in enumerate(row[1:], 1):
                column = columns[index] if index < len(columns) else str(index)
                values[f"{panel_id}/{sample}/{column}"] = value
    return values


def _section(section_id: str, label: str, entries: list[dict]) -> dict:
    changed = sum(bool(item.get("changed")) for item in entries)
    return {
        "id": section_id,
        "label": label,
        "status": "changed" if changed else "same",
        "changed": changed,
        "entries": entries,
    }


def compare(pipeline: Pipeline, left_id: str, right_id: str) -> dict:
    if left_id == right_id:
        raise ValueError("choose two different capsule states")
    left = None if left_id == "current" else load(pipeline, left_id)
    right = None if right_id == "current" else load(pipeline, right_id)
    if left is None:
        left = current(pipeline, (right or {}).get("launch", {}))
    if right is None:
        right = current(pipeline, (left or {}).get("launch", {}))

    left_source, right_source = left.get("source", {}), right.get("source", {})
    left_git, right_git = left_source.get("git", {}), right_source.get("git", {})
    left_qc, right_qc = _qc_values(left.get("qc", {})), _qc_values(right.get("qc", {}))
    sections = [
        _section(
            "launch",
            "Launch",
            [
                _entry(
                    key.replace("_", " ").title(),
                    left.get("launch", {}).get(key),
                    right.get("launch", {}).get(key),
                )
                for key in (
                    "target",
                    "configfile",
                    "profile",
                    "cores",
                    "dryrun",
                    "use_conda",
                    "extra",
                    "env",
                )
            ],
        ),
        _section(
            "code",
            "Code and workflow",
            [
                _entry(
                    "Git commit",
                    left_git.get("commit", ""),
                    right_git.get("commit", ""),
                ),
                _entry(
                    "Git branch",
                    left_git.get("branch", ""),
                    right_git.get("branch", ""),
                ),
                _entry(
                    "Dirty files", left_git.get("dirty", 0), right_git.get("dirty", 0)
                ),
                _entry(
                    "Working state",
                    left_git.get("state_digest", ""),
                    right_git.get("state_digest", ""),
                ),
                _entry(
                    "Workflow fingerprint",
                    left_source.get("workflow_digest", ""),
                    right_source.get("workflow_digest", ""),
                ),
                *_artifact_entries(
                    left_source.get("files", []),
                    right_source.get("files", []),
                    "workflow",
                ),
                *_artifact_entries(
                    left_source.get("indexes", []), right_source.get("indexes", [])
                ),
            ],
        ),
        _section(
            "config",
            "Configuration",
            _config_entries(left.get("config", {}), right.get("config", {})),
        ),
        _section(
            "cohort",
            "Cohort and inputs",
            [
                _entry(
                    "Study fingerprint",
                    left.get("cohort", {}).get("fingerprint", ""),
                    right.get("cohort", {}).get("fingerprint", ""),
                    "scientific",
                ),
                _entry(
                    "Input fingerprint",
                    left.get("cohort", {}).get("inputs_fingerprint", ""),
                    right.get("cohort", {}).get("inputs_fingerprint", ""),
                    "scientific",
                ),
                _entry(
                    "Study gate",
                    left.get("cohort", {}).get("gate", "unknown"),
                    right.get("cohort", {}).get("gate", "unknown"),
                    "scientific",
                ),
                _entry(
                    "Biological units",
                    left.get("cohort", {})
                    .get("summary", {})
                    .get("biological_units", 0),
                    right.get("cohort", {})
                    .get("summary", {})
                    .get("biological_units", 0),
                    "scientific",
                ),
                _entry(
                    "Groups",
                    left.get("cohort", {}).get("summary", {}).get("groups", 0),
                    right.get("cohort", {}).get("summary", {}).get("groups", 0),
                    "scientific",
                ),
            ],
        ),
        _section(
            "environment",
            "Environment",
            [
                _entry(
                    "Driver environment",
                    left.get("environment", {}).get("driver_env", ""),
                    right.get("environment", {}).get("driver_env", ""),
                ),
                _entry(
                    "Environment fingerprint",
                    left.get("environment", {}).get("digest", ""),
                    right.get("environment", {}).get("digest", ""),
                ),
                _entry(
                    "Platform",
                    left.get("environment", {}).get("platform", ""),
                    right.get("environment", {}).get("platform", ""),
                ),
            ],
        ),
        _section(
            "outputs",
            "Outputs",
            [
                _entry(
                    "Output fingerprint",
                    (
                        left.get("results", {}).get("after")
                        or left.get("results", {}).get("before")
                        or {}
                    ).get("digest", ""),
                    (
                        right.get("results", {}).get("after")
                        or right.get("results", {}).get("before")
                        or {}
                    ).get("digest", ""),
                    "scientific",
                ),
                *_artifact_entries(
                    (
                        left.get("results", {}).get("after")
                        or left.get("results", {}).get("before")
                        or {}
                    ).get("items", []),
                    (
                        right.get("results", {}).get("after")
                        or right.get("results", {}).get("before")
                        or {}
                    ).get("items", []),
                ),
            ],
        ),
        _section(
            "qc",
            "Quality control",
            [
                _entry(
                    key,
                    left_qc.get(key, "<missing>"),
                    right_qc.get(key, "<missing>"),
                    "scientific",
                )
                for key in sorted(set(left_qc) | set(right_qc))
            ][:160],
        ),
        _section(
            "outcome",
            "Outcome",
            [
                _entry(
                    "Status",
                    left.get("session", {}).get("status"),
                    right.get("session", {}).get("status"),
                ),
                _entry(
                    "Exit code",
                    left.get("session", {}).get("exit_code"),
                    right.get("session", {}).get("exit_code"),
                ),
                _entry(
                    "Duration seconds",
                    left.get("session", {}).get("duration_s"),
                    right.get("session", {}).get("duration_s"),
                ),
            ],
        ),
    ]
    changed_domains = [section["id"] for section in sections if section["changed"]]
    scientific = any(
        section_id in changed_domains for section_id in ("cohort", "outputs", "qc")
    )
    reproducibility = any(
        section_id in changed_domains
        for section_id in ("code", "config", "environment", "launch")
    )
    verdict = "identical"
    if reproducibility and scientific:
        verdict = "execution and scientific drift"
    elif reproducibility:
        verdict = "execution drift"
    elif scientific:
        verdict = "scientific result drift"
    elif changed_domains:
        verdict = "outcome drift"
    return {
        "left": _summary(left),
        "right": _summary(right),
        "verdict": verdict,
        "changed_domains": changed_domains,
        "sections": sections,
        "changed": sum(section["changed"] for section in sections),
    }
