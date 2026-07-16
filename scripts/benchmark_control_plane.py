#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Benchmark OmicsANG's durable SQLite execution control plane safely.

The benchmark redirects every OmicsANG state path to a temporary directory
before touching the store.  It measures the persistence/control-plane work
around local and Slurm jobs; it does not spawn PTYs or invoke Slurm commands.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import platform
import sqlite3
import stat
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from queue import Empty
from typing import Any, Callable, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchtop import settings, state_db, store  # noqa: E402


class BenchmarkError(RuntimeError):
    """Raised when a benchmark correctness invariant fails."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BenchmarkError(message)


def _bounded_int(name: str, minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {minimum} and {maximum}"
            )
        return parsed

    return parse


def _existing_directory(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"temporary parent is not a directory: {path}")
    return path


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def _metric(
    name: str,
    latencies: Sequence[float],
    elapsed: float,
    *,
    unit: str,
    operations: int | None = None,
    checks: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    count = len(latencies) if operations is None else operations
    latency_ms = [value * 1000.0 for value in latencies]
    return {
        "name": name,
        "unit": unit,
        "operations": count,
        "elapsed_s": elapsed,
        "throughput_ops_s": count / elapsed if elapsed else 0.0,
        "latency_ms": {
            "mean": statistics.fmean(latency_ms) if latency_ms else 0.0,
            "p50": _percentile(latency_ms, 0.50),
            "p95": _percentile(latency_ms, 0.95),
            "max": max(latency_ms, default=0.0),
        },
        "checks": checks or {},
        "detail": detail or {},
    }


def _configure_state_root(root: Path) -> None:
    settings.STATE_DIR = root
    settings.STATE_FILE = root / "state.json"
    settings.RUN_LOG_DIR = root / "sessions"


@contextmanager
def _temporary_state(parent: Path | None = None) -> Iterator[Path]:
    original = (settings.STATE_DIR, settings.STATE_FILE, settings.RUN_LOG_DIR)
    with tempfile.TemporaryDirectory(
        prefix="benchtop-control-plane-benchmark-",
        dir=parent,
    ) as tmp:
        root = Path(tmp).resolve()
        try:
            yield root
        finally:
            settings.STATE_DIR, settings.STATE_FILE, settings.RUN_LOG_DIR = original
            for path in list(state_db._INITIALIZED):  # noqa: SLF001
                if path.is_relative_to(root):
                    state_db._INITIALIZED.pop(path, None)  # noqa: SLF001


def _measure_passes(
    name: str,
    passes: Sequence[tuple[Sequence[Any], Callable[[Any], Any]]],
    *,
    unit: str,
    checks: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latencies: list[float] = []
    elapsed = 0.0
    for items, operation in passes:
        pass_started = time.perf_counter()
        for item in items:
            operation_started = time.perf_counter()
            operation(item)
            latencies.append(time.perf_counter() - operation_started)
        elapsed += time.perf_counter() - pass_started
    return _metric(
        name,
        latencies,
        elapsed,
        unit=unit,
        checks=checks,
        detail=detail,
    )


def _require_job_sequence(
    job_id: str,
    expected: Sequence[tuple[str, str | None, str | None]],
) -> None:
    events = store.job_events(job_id, limit=max(20, len(expected) + 1))
    actual = [
        (event["type"], event["from_state"], event["to_state"]) for event in events
    ]
    _require(
        actual == list(expected),
        f"{job_id}: lifecycle sequence mismatch: {actual!r}",
    )


def _process_worker(
    mode: str,
    state_root: str,
    index: int,
    ready: Any,
    start_gate: Any,
    results: Any,
    payload: dict[str, Any],
) -> None:
    """Run one independent-controller operation after a common start gate."""
    _configure_state_root(Path(state_root))
    try:
        if mode != "initialize":
            store.initialize()
        ready.put(os.getpid())
        if not start_gate.wait(timeout=30):
            raise BenchmarkError("process start gate timed out")
        started = time.perf_counter()
        if mode == "initialize":
            status = store.initialize()
            outcome = {
                "schema": status["schema_version"],
                "journal": status["journal_mode"],
            }
        elif mode == "idempotency":
            job, created = store.create_job(
                job_id=f"idempotency-candidate-{index}",
                kind="run",
                executor="local",
                pipeline="benchmark",
                state="created",
                idempotency_key=str(payload["key"]),
                payload={"claimant": index},
            )
            outcome = {"created": created, "job_id": job["id"]}
        elif mode == "job_lease":
            claimed = store.acquire_job_lease(
                str(payload["job_id"]),
                f"process-{index}",
                ttl=60.0,
                expected_states=("queued",),
            )
            outcome = {
                "claimed": claimed is not None,
                "owner": str((claimed or {}).get("lease_owner") or ""),
            }
        else:
            raise BenchmarkError(f"unknown process benchmark mode {mode!r}")
        ended = time.perf_counter()
        results.put(
            {
                "ok": True,
                "started": started,
                "ended": ended,
                "latency": ended - started,
                "outcome": outcome,
            }
        )
    except BaseException as exc:
        results.put({"ok": False, "error": repr(exc)})


def _run_process_contenders(
    mode: str,
    state_root: Path,
    workers: int,
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], float]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start_gate = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_worker,
            args=(
                mode,
                str(state_root),
                index,
                ready,
                start_gate,
                results,
                payload or {},
            ),
        )
        for index in range(workers)
    ]
    outcomes: list[dict[str, Any]] = []
    started_processes: list[multiprocessing.Process] = []
    try:
        for process in processes:
            process.start()
            started_processes.append(process)
        for _ in processes:
            try:
                ready.get(timeout=30)
            except Empty as exc:
                raise BenchmarkError(f"{mode}: a worker did not become ready") from exc
        start_gate.set()
        for _ in processes:
            try:
                outcomes.append(results.get(timeout=30))
            except Empty as exc:
                raise BenchmarkError(f"{mode}: a worker returned no result") from exc
        for process in processes:
            process.join(timeout=30)
        _require(
            all(not process.is_alive() for process in processes),
            f"{mode}: a worker did not exit",
        )
        failures = [outcome for outcome in outcomes if not outcome.get("ok")]
        _require(not failures, f"{mode}: worker failures: {failures}")
        started = min(float(outcome["started"]) for outcome in outcomes)
        ended = max(float(outcome["ended"]) for outcome in outcomes)
        return outcomes, ended - started
    finally:
        # A partial spawn failure leaves later Process objects in the initial
        # state, where is_alive()/join() raise AssertionError.  Only clean up
        # children whose start() call actually succeeded.
        for process in started_processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        for queue in (ready, results):
            queue.close()
            queue.join_thread()


def _benchmark_startup(temp_root: Path, workers: int) -> list[dict[str, Any]]:
    single_root = temp_root / "cold-single"
    _configure_state_root(single_root)
    started = time.perf_counter()
    status = store.initialize()
    elapsed = time.perf_counter() - started
    db_path = Path(status["path"])
    _require(status["journal_mode"] == "wal", "cold database did not enable WAL")
    _require(
        status["schema_version"] == state_db.SCHEMA_VERSION,
        "cold database schema version mismatch",
    )
    _require(stat.S_IMODE(db_path.stat().st_mode) == 0o600, "database mode is not 0600")
    serial = _metric(
        "cold_single_process_migration",
        [elapsed],
        elapsed,
        unit="initializations",
        checks={
            "journal_mode": status["journal_mode"],
            "schema_version": status["schema_version"],
            "database_mode": "0600",
        },
    )

    concurrent_root = temp_root / "cold-concurrent"
    outcomes, concurrent_elapsed = _run_process_contenders(
        "initialize", concurrent_root, workers
    )
    result_rows = [outcome["outcome"] for outcome in outcomes]
    all_valid = all(
        row["schema"] == state_db.SCHEMA_VERSION and row["journal"] == "wal"
        for row in result_rows
    )
    _require(all_valid, "not every concurrent initializer observed WAL schema v1")
    _configure_state_root(concurrent_root)
    with state_db.connect() as conn:
        migration_rows = int(
            conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        )
    _require(migration_rows == 1, "concurrent migration created duplicate records")
    concurrent = _metric(
        "cold_multiprocess_initialization",
        [float(outcome["latency"]) for outcome in outcomes],
        concurrent_elapsed,
        unit="initializers",
        operations=workers,
        checks={
            "all_initializers_succeeded": True,
            "migration_rows": migration_rows,
            "journal_mode": "wal",
        },
        detail={"process_start_method": "spawn"},
    )
    return [serial, concurrent]


def _benchmark_local_lifecycle(
    job_count: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    job_ids = [f"local-{index:05d}" for index in range(job_count)]

    def create(job_id: str) -> None:
        job, created = store.create_job(
            job_id=job_id,
            kind="run",
            executor="local",
            pipeline="benchmark",
            state="created",
            idempotency_key=f"benchmark-{job_id}",
            plan_digest=f"sha256:{job_id}",
            payload={"benchmark": True, "cores": 1},
            private={"command": ["snakemake", "--cores", "1"]},
        )
        _require(created and job["id"] == job_id, f"could not create {job_id}")

    create_metric = _measure_passes(
        "local_job_create",
        [(job_ids, create)],
        unit="jobs",
    )

    transition_latencies: list[float] = []
    transition_elapsed = 0.0

    def run_transition_pass(operation: Callable[[str], Any]) -> None:
        nonlocal transition_elapsed
        pass_started = time.perf_counter()
        for job_id in job_ids:
            operation_started = time.perf_counter()
            operation(job_id)
            transition_latencies.append(time.perf_counter() - operation_started)
        transition_elapsed += time.perf_counter() - pass_started

    run_transition_pass(
        lambda job_id: store.transition_job(
            job_id,
            "queued",
            expected_states=("created",),
            expected_version=0,
            queued_at=time.time(),
        )
    )

    def acquire(job_id: str) -> None:
        claimed = store.acquire_job_lease(
            job_id,
            f"controller-{job_id}",
            ttl=30.0,
            expected_states=("queued",),
        )
        _require(claimed is not None, f"could not lease {job_id}")

    acquire_metric = _measure_passes(
        "local_job_lease_acquire",
        [(job_ids, acquire)],
        unit="leases",
    )
    run_transition_pass(
        lambda job_id: store.transition_job(
            job_id, "preparing", expected_states=("queued",)
        )
    )
    run_transition_pass(
        lambda job_id: store.transition_job(
            job_id,
            "running",
            expected_states=("preparing",),
            started=time.time(),
        )
    )

    event_metric = _measure_passes(
        "local_event_append",
        [
            (
                job_ids,
                lambda job_id: store.append_event(
                    job_id,
                    "benchmark_progress",
                    actor="benchmark",
                    payload={"completed": 0.5},
                ),
            )
        ],
        unit="events",
    )

    def renew(job_id: str) -> None:
        renewed = store.renew_job_lease(
            job_id,
            f"controller-{job_id}",
            ttl=30.0,
            expected_states=("running",),
        )
        _require(renewed, f"could not renew {job_id}")

    renew_metric = _measure_passes(
        "local_job_lease_renew",
        [(job_ids, renew)],
        unit="renewals",
    )
    run_transition_pass(
        lambda job_id: store.transition_job(
            job_id,
            "succeeded",
            expected_states=("running",),
            lease_owner="",
            lease_expires=None,
        )
    )
    transition_metric = _metric(
        "local_legal_state_transition",
        transition_latencies,
        transition_elapsed,
        unit="transitions",
        detail={"transitions_per_job": 4},
    )

    with state_db.connect() as conn:
        terminal = int(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE executor='local' AND state='succeeded'"
            ).fetchone()[0]
        )
        progress_events = int(
            conn.execute(
                "SELECT COUNT(*) FROM job_events WHERE event_type='benchmark_progress'"
            ).fetchone()[0]
        )
        lifecycle_events = int(
            conn.execute(
                """SELECT COUNT(*) FROM job_events e JOIN jobs j ON j.id=e.job_id
               WHERE j.executor='local' AND j.id LIKE 'local-%'"""
            ).fetchone()[0]
        )
    _require(terminal == job_count, "not every local job reached succeeded")
    _require(progress_events == job_count, "local progress event count mismatch")
    _require(lifecycle_events == job_count * 7, "local lifecycle event count mismatch")
    local_sequence = (
        ("job_created", None, "created"),
        ("state_changed", "created", "queued"),
        ("lease_acquired", "queued", "queued"),
        ("state_changed", "queued", "preparing"),
        ("state_changed", "preparing", "running"),
        ("benchmark_progress", "running", "running"),
        ("state_changed", "running", "succeeded"),
    )
    for job_id in job_ids:
        _require_job_sequence(job_id, local_sequence)
        job = store.get_job(job_id)
        _require(
            job is not None and job["state_version"] == 4,
            f"{job_id}: expected state version 4",
        )
    transition_metric["checks"] = {
        "terminal_jobs": terminal,
        "events_per_job": lifecycle_events // job_count,
        "state_version": 4,
    }
    event_metric["checks"] = {"events_persisted": progress_events}
    return [
        create_metric,
        transition_metric,
        acquire_metric,
        renew_metric,
        event_metric,
    ], job_ids


def _benchmark_slurm_lifecycle(job_count: int) -> list[dict[str, Any]]:
    job_ids = [f"slurm-{index:05d}" for index in range(job_count)]
    scheduler_ids = {
        job_id: str(900_000 + index) for index, job_id in enumerate(job_ids)
    }

    def create(job_id: str) -> None:
        job, created = store.create_job(
            job_id=job_id,
            kind="run",
            executor="slurm",
            pipeline="benchmark",
            state="created",
            idempotency_key=f"benchmark-{job_id}",
            plan_digest=f"sha256:{job_id}",
            payload={"submission_tag": f"benchtop:{job_id}"},
        )
        _require(created and job["id"] == job_id, f"could not create {job_id}")

    create_metric = _measure_passes(
        "slurm_job_create",
        [(job_ids, create)],
        unit="jobs",
    )

    def persist_intent(job_id: str) -> None:
        store.transition_job(
            job_id,
            "submitting",
            expected_states=("created",),
            payload_update={"executor_state": "submitting"},
        )
        store.record_slurm_job(
            {
                "id": job_id,
                "job_id": "",
                "pipeline": "benchmark",
                "status": "submitting",
                "submission_tag": f"benchtop:{job_id}",
            }
        )

    intent_metric = _measure_passes(
        "slurm_intent_persistence",
        [(job_ids, persist_intent)],
        unit="submission intents",
        detail={"store_writes_per_intent": 2},
    )

    def bind_acceptance(job_id: str) -> None:
        scheduler_id = scheduler_ids[job_id]
        store.record_slurm_job(
            {
                "id": job_id,
                "job_id": scheduler_id,
                "pipeline": "benchmark",
                "status": "submitted",
                "scheduler_cluster": "benchmark-cluster",
                "submission_tag": f"benchtop:{job_id}",
            }
        )
        store.transition_job(
            job_id,
            "submitted",
            expected_states=("submitting",),
            scheduler_job_id=scheduler_id,
            started=time.time(),
            payload_update={
                "executor_state": "submitted",
                "scheduler_cluster": "benchmark-cluster",
            },
        )

    acceptance_metric = _measure_passes(
        "slurm_scheduler_acceptance_binding",
        [(job_ids, bind_acceptance)],
        unit="accepted jobs",
        detail={"store_writes_per_acceptance": 2},
    )

    def scheduler_lookup(job_id: str) -> None:
        found = store.job_by_scheduler_id(scheduler_ids[job_id])
        _require(
            found is not None and found["id"] == job_id, "scheduler lookup mismatch"
        )

    lookup_metric = _measure_passes(
        "slurm_scheduler_id_lookup",
        [(job_ids, scheduler_lookup)],
        unit="lookups",
    )

    observations: list[tuple[str, str]] = [
        ("pending", "submitted"),
        ("running", "pending"),
        ("succeeded", "running"),
    ]

    def reconcile(item: tuple[str, str, str]) -> None:
        job_id, target, expected = item
        store.record_slurm_job(
            {
                "id": job_id,
                "job_id": scheduler_ids[job_id],
                "pipeline": "benchmark",
                "status": target,
                "scheduler_cluster": "benchmark-cluster",
                "submission_tag": f"benchtop:{job_id}",
            }
        )
        store.transition_job(
            job_id,
            target,
            expected_states=(expected,),
            payload_update={
                "executor_state": target,
                "scheduler_observation": {"state": target.upper()},
            },
        )

    observation_items = [
        (job_id, target, expected)
        for target, expected in observations
        for job_id in job_ids
    ]
    reconciliation_metric = _measure_passes(
        "slurm_reconciliation_persistence",
        [(observation_items, reconcile)],
        unit="scheduler observations",
        detail={"store_writes_per_observation": 2, "observations_per_job": 3},
    )

    with state_db.connect() as conn:
        terminal = int(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE executor='slurm' AND state='succeeded'"
            ).fetchone()[0]
        )
        tracked = int(
            conn.execute(
                "SELECT COUNT(*) FROM slurm_jobs WHERE status='succeeded'"
            ).fetchone()[0]
        )
        lifecycle_events = int(
            conn.execute(
                """SELECT COUNT(*) FROM job_events e JOIN jobs j ON j.id=e.job_id
               WHERE j.executor='slurm'"""
            ).fetchone()[0]
        )
    _require(terminal == job_count, "not every Slurm job reached succeeded")
    _require(tracked == job_count, "Slurm compatibility tracking count mismatch")
    _require(lifecycle_events == job_count * 6, "Slurm lifecycle event count mismatch")
    slurm_sequence = (
        ("job_created", None, "created"),
        ("state_changed", "created", "submitting"),
        ("state_changed", "submitting", "submitted"),
        ("state_changed", "submitted", "pending"),
        ("state_changed", "pending", "running"),
        ("state_changed", "running", "succeeded"),
    )
    for job_id in job_ids:
        _require_job_sequence(job_id, slurm_sequence)
        job = store.get_job(job_id)
        _require(
            job is not None and job["state_version"] == 5,
            f"{job_id}: expected state version 5",
        )
    reconciliation_metric["checks"] = {
        "terminal_jobs": terminal,
        "compatibility_rows": tracked,
        "events_per_job": lifecycle_events // job_count,
        "state_version": 5,
    }
    return [
        create_metric,
        intent_metric,
        acceptance_metric,
        lookup_metric,
        reconciliation_metric,
    ]


def _benchmark_concurrent_events(
    workers: int, events_per_worker: int
) -> dict[str, Any]:
    job_id = "concurrent-event-target"
    store.create_job(
        job_id=job_id,
        kind="run",
        executor="local",
        pipeline="benchmark",
        state="running",
    )
    barrier = threading.Barrier(workers)

    def writer(index: int) -> tuple[float, float, list[float]]:
        barrier.wait(timeout=30)
        latencies: list[float] = []
        first_started = time.perf_counter()
        for event_index in range(events_per_worker):
            started = time.perf_counter()
            store.append_event(
                job_id,
                "benchmark_concurrent_event",
                actor=f"thread-{index}",
                payload={"index": event_index},
            )
            latencies.append(time.perf_counter() - started)
        return first_started, time.perf_counter(), latencies

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(writer, range(workers)))
    elapsed = max(result[1] for result in results) - min(
        result[0] for result in results
    )
    latencies = [latency for result in results for latency in result[2]]
    expected = workers * events_per_worker
    with state_db.connect() as conn:
        persisted, unique_ids = conn.execute(
            """SELECT COUNT(*),COUNT(DISTINCT event_id) FROM job_events
               WHERE job_id=? AND event_type='benchmark_concurrent_event'""",
            (job_id,),
        ).fetchone()
    _require(int(persisted) == expected, "concurrent event write count mismatch")
    _require(int(unique_ids) == expected, "concurrent event IDs are not unique")
    return _metric(
        "concurrent_append_only_events",
        latencies,
        elapsed,
        unit="events",
        checks={"persisted": int(persisted), "unique_event_ids": int(unique_ids)},
        detail={"threads": workers, "shared_job": True},
    )


def _benchmark_mixed_io(
    workers: int,
    operations_per_worker: int,
    read_targets: Sequence[str],
) -> list[dict[str, Any]]:
    writer_count = max(1, workers // 2)
    reader_count = workers - writer_count
    if reader_count == 0:
        reader_count = 1
        workers += 1
    writer_jobs = [f"mixed-writer-{index}" for index in range(writer_count)]
    for job_id in writer_jobs:
        store.create_job(
            job_id=job_id,
            kind="run",
            executor="local",
            pipeline="benchmark",
            state="running",
        )
    barrier = threading.Barrier(workers)

    def actor(index: int) -> tuple[str, float, float, list[float]]:
        role = "write" if index < writer_count else "read"
        barrier.wait(timeout=30)
        latencies: list[float] = []
        first_started = time.perf_counter()
        for operation_index in range(operations_per_worker):
            started = time.perf_counter()
            if role == "write":
                store.append_event(
                    writer_jobs[index],
                    "benchmark_mixed_event",
                    actor=f"mixed-thread-{index}",
                    payload={"index": operation_index},
                )
            else:
                target = read_targets[
                    ((index - writer_count) * operations_per_worker + operation_index)
                    % len(read_targets)
                ]
                _require(
                    store.get_job(target) is not None, f"mixed read missed {target}"
                )
            latencies.append(time.perf_counter() - started)
        return role, first_started, time.perf_counter(), latencies

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(actor, range(workers)))
    elapsed = max(result[2] for result in results) - min(
        result[1] for result in results
    )
    read_latencies = [
        latency
        for role, _, _, latencies in results
        if role == "read"
        for latency in latencies
    ]
    write_latencies = [
        latency
        for role, _, _, latencies in results
        if role == "write"
        for latency in latencies
    ]
    expected_writes = writer_count * operations_per_worker
    with state_db.connect() as conn:
        persisted = int(
            conn.execute(
                "SELECT COUNT(*) FROM job_events WHERE event_type='benchmark_mixed_event'"
            ).fetchone()[0]
        )
    _require(persisted == expected_writes, "mixed concurrent write count mismatch")
    common_detail = {
        "threads": workers,
        "reader_threads": reader_count,
        "writer_threads": writer_count,
        "shared_elapsed_s": elapsed,
    }
    return [
        _metric(
            "mixed_concurrent_reads",
            read_latencies,
            elapsed,
            unit="reads",
            checks={"misses": 0},
            detail=common_detail,
        ),
        _metric(
            "mixed_concurrent_writes",
            write_latencies,
            elapsed,
            unit="events",
            checks={"persisted": persisted},
            detail=common_detail,
        ),
    ]


def _benchmark_multiprocess_claims(
    state_root: Path, workers: int
) -> list[dict[str, Any]]:
    outcomes, elapsed = _run_process_contenders(
        "idempotency",
        state_root,
        workers,
        payload={"key": "multiprocess-idempotency-key"},
    )
    rows = [outcome["outcome"] for outcome in outcomes]
    created = sum(bool(row["created"]) for row in rows)
    unique_job_ids = len({str(row["job_id"]) for row in rows})
    _require(created == 1, "idempotency contention created more than one job")
    _require(unique_job_ids == 1, "idempotency contenders observed different jobs")
    idempotency_metric = _metric(
        "multiprocess_idempotency_contention",
        [float(outcome["latency"]) for outcome in outcomes],
        elapsed,
        unit="claim attempts",
        operations=workers,
        checks={"jobs_created": created, "unique_job_ids": unique_job_ids},
        detail={"processes": workers, "process_start_method": "spawn"},
    )

    lease_job_id = "multiprocess-lease-target"
    store.create_job(
        job_id=lease_job_id,
        kind="run",
        executor="local",
        pipeline="benchmark",
        state="queued",
    )
    outcomes, elapsed = _run_process_contenders(
        "job_lease",
        state_root,
        workers,
        payload={"job_id": lease_job_id},
    )
    rows = [outcome["outcome"] for outcome in outcomes]
    winners = [row for row in rows if row["claimed"]]
    durable = store.get_job(lease_job_id)
    _require(len(winners) == 1, "lease contention produced more than one winner")
    _require(
        durable is not None and durable["lease_owner"] == winners[0]["owner"],
        "durable lease owner does not match contention winner",
    )
    lease_metric = _metric(
        "multiprocess_job_lease_contention",
        [float(outcome["latency"]) for outcome in outcomes],
        elapsed,
        unit="claim attempts",
        operations=workers,
        checks={"winners": len(winners), "durable_owner_matches": True},
        detail={"processes": workers, "process_start_method": "spawn"},
    )
    return [idempotency_metric, lease_metric]


def _database_summary(args: argparse.Namespace) -> dict[str, Any]:
    status = store.database_status()
    with state_db.connect() as conn:
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("jobs", "job_events", "slurm_jobs", "leases")
        }
        synchronous = int(conn.execute("PRAGMA synchronous").fetchone()[0])
    path = Path(status["path"])
    _require(path.is_file(), "workload database is missing")
    journal_mode = str(status["journal_mode"]).lower()
    _require(
        journal_mode == "wal", f"workload journal mode is {journal_mode!r}, not WAL"
    )
    _require(
        int(status["schema_version"]) == state_db.SCHEMA_VERSION,
        "workload database schema version mismatch",
    )
    _require(
        synchronous == 2,
        f"workload synchronous mode is {synchronous}, not FULL (2)",
    )
    file_modes: dict[str, str] = {}
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if not candidate.exists():
            continue
        mode = stat.S_IMODE(candidate.stat().st_mode)
        file_modes[candidate.name] = f"{mode:04o}"
        _require(mode == 0o600, f"{candidate.name} mode is {mode:04o}, not 0600")

    writer_count = max(1, args.workers // 2)
    expected_counts = {
        "jobs": (args.jobs * 2) + 1 + writer_count + 2,
        "job_events": (
            (args.jobs * 7)
            + (args.jobs * 6)
            + 1
            + (args.workers * args.events_per_worker)
            + (writer_count * (1 + args.mixed_ops))
            + 1
            + 2
        ),
        "slurm_jobs": args.jobs,
        "leases": 0,
    }
    _require(
        counts == expected_counts,
        f"workload row counts differ: got {counts}, expected {expected_counts}",
    )
    return {
        "journal_mode": journal_mode,
        "schema_version": status["schema_version"],
        "synchronous": synchronous,
        "database_bytes": path.stat().st_size,
        "database_mode": file_modes[path.name],
        "database_file_modes": file_modes,
        "row_counts": counts,
        "expected_row_counts": expected_counts,
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    metrics: list[dict[str, Any]] = []
    with _temporary_state(args.temp_parent) as temp_root:
        temporary_parent = str(temp_root.parent)
        temporary_device = temp_root.stat().st_dev
        metrics.extend(_benchmark_startup(temp_root, args.workers))
        workload_root = temp_root / "workload"
        _configure_state_root(workload_root)
        store.initialize()
        local_metrics, local_job_ids = _benchmark_local_lifecycle(args.jobs)
        metrics.extend(local_metrics)
        metrics.extend(_benchmark_slurm_lifecycle(args.jobs))
        metrics.append(
            _benchmark_concurrent_events(args.workers, args.events_per_worker)
        )
        metrics.extend(_benchmark_mixed_io(args.workers, args.mixed_ops, local_job_ids))
        metrics.extend(_benchmark_multiprocess_claims(workload_root, args.workers))
        database = _database_summary(args)
        temporary_database = str(state_db.database_path())
    return {
        "benchmark": "benchtop-durable-control-plane",
        "scope": (
            "SQLite job/event/lease persistence and durable Slurm intent tracking; "
            "excludes PTY execution and external sbatch/squeue/sacct/scancel latency"
        ),
        "safety": {
            "isolated_temporary_state": True,
            "temporary_database": temporary_database,
            "temporary_parent": temporary_parent,
            "temporary_device": temporary_device,
            "temporary_state_removed_after_run": True,
        },
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "configuration": {
            "jobs_per_executor": args.jobs,
            "workers": args.workers,
            "events_per_worker": args.events_per_worker,
            "mixed_operations_per_worker": args.mixed_ops,
        },
        "database": database,
        "metrics": metrics,
        "validation": "PASS",
    }


def _print_human(result: dict[str, Any]) -> None:
    environment = result["environment"]
    config = result["configuration"]
    print("OmicsANG durable control-plane benchmark")
    print(f"Scope: {result['scope']}")
    print(
        "Safety: all state was redirected to a temporary directory and removed "
        "after the run"
    )
    print(f"State filesystem parent: {result['safety']['temporary_parent']}")
    print(
        f"Environment: Python {environment['python']} | SQLite {environment['sqlite']} | "
        f"CPUs {environment['cpu_count']}"
    )
    print(
        f"Workload: {config['jobs_per_executor']} jobs/executor | "
        f"{config['workers']} workers | "
        f"{config['events_per_worker']} events/worker | "
        f"{config['mixed_operations_per_worker']} mixed ops/worker"
    )
    print("")
    print(
        "Metric                                      ops/s     p50 ms     p95 ms      max ms"
    )
    print(
        "----------------------------------------  ---------  ---------  ---------  ----------"
    )
    for metric in result["metrics"]:
        latency = metric["latency_ms"]
        print(
            f"{metric['name']:<40}  {metric['throughput_ops_s']:>9.1f}  "
            f"{latency['p50']:>9.3f}  {latency['p95']:>9.3f}  {latency['max']:>10.3f}"
        )
    database = result["database"]
    print("")
    print(
        f"Database: {str(database['journal_mode']).upper()} | "
        f"schema {database['schema_version']} | "
        f"synchronous={database['synchronous']} | mode {database['database_mode']} | "
        f"{database['database_bytes']} bytes"
    )
    print(f"Rows: {json.dumps(database['row_counts'], sort_keys=True)}")
    print(f"Validation: {result['validation']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark OmicsANG's SQLite execution control plane in disposable state."
        )
    )
    parser.add_argument(
        "--jobs",
        type=_bounded_int("jobs", 1, 400),
        default=25,
        help="local jobs and Slurm jobs to exercise (default: 25)",
    )
    parser.add_argument(
        "--workers",
        type=_bounded_int("workers", 2, 32),
        default=min(8, max(2, os.cpu_count() or 2)),
        help="thread/process contenders (default: min(8, CPU count))",
    )
    parser.add_argument(
        "--events-per-worker",
        type=_bounded_int("events-per-worker", 1, 500),
        default=15,
        help="shared-job append-only events per thread (default: 15)",
    )
    parser.add_argument(
        "--mixed-ops",
        type=_bounded_int("mixed-ops", 1, 500),
        default=15,
        help="mixed reads or writes per thread (default: 15)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the table",
    )
    parser.add_argument(
        "--temp-parent",
        type=_existing_directory,
        default=None,
        help=(
            "parent for disposable state (default: system temp; choose a directory on "
            "the deployment filesystem for storage-representative results)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_benchmark(args)
    except BenchmarkError as exc:
        print(f"benchmark validation failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
