# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Read-only Linux capacity detection and conservative local admission policy."""

from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return ""


def _positive(value: str) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except ValueError:
        return None


def _ancestors(base: Path, relative: str):
    candidate = base / relative.lstrip("/")
    if ".." in candidate.parts:
        candidate = base
    yield candidate
    while candidate != base and base in candidate.parents:
        candidate = candidate.parent
        yield candidate


def detect(proc: Path = Path("/proc"), cgroup: Path = Path("/sys/fs/cgroup")) -> dict:
    """Account for affinity and readable ancestor cgroup v1/v2 limits.

    Memory is expressed in decimal MB, as used by Snakemake's mem_mb resource.
    Missing available-memory information leaves zero admission capacity.
    """
    host_cpus = os.cpu_count() or 1
    try:
        affinity_cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity_cpus = host_cpus
    cpu_capacity = float(max(1, min(host_cpus, affinity_cpus)))
    memory = {}
    for line in _read(proc / "meminfo").splitlines():
        key, _, value = line.partition(":")
        fields = value.split()
        if fields and fields[0].isdigit():
            memory[key] = int(fields[0]) * 1024
    total = memory.get("MemTotal", 0)
    available = memory.get("MemAvailable", 0)
    applied = []
    for line in _read(proc / "self" / "cgroup").splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        _, controllers, relative = fields
        if not controllers:
            candidates = [(path, True) for path in _ancestors(cgroup, relative)]
        else:
            candidates = []
            for controller in ("cpu", "cpu,cpuacct", "memory"):
                if controller.split(",")[0] in controllers.split(","):
                    candidates.extend(
                        (path, False)
                        for path in _ancestors(cgroup / controller, relative)
                    )
        for path, v2 in candidates:
            if v2:
                quota = _read(path / "cpu.max").split()
            else:
                quota = [
                    _read(path / "cpu.cfs_quota_us"),
                    _read(path / "cpu.cfs_period_us"),
                ]
            if len(quota) == 2:
                amount, period = _positive(quota[0]), _positive(quota[1])
                if amount and period:
                    cpu_capacity = min(cpu_capacity, amount / period)
                    applied.append("cpu quota")
            raw_limit = _read(path / ("memory.max" if v2 else "memory.limit_in_bytes"))
            limit = int(raw_limit) if raw_limit.isdigit() else None
            current = _read(
                path / ("memory.current" if v2 else "memory.usage_in_bytes")
            )
            if limit is not None and total and limit < total:
                total = limit
                available = min(
                    available, max(0, limit - int(current)) if current.isdigit() else 0
                )
                applied.append("memory limit")
            elif limit is not None and current.isdigit():
                available = min(available, max(0, limit - int(current)))
    cpus = max(1, math.floor(cpu_capacity))
    total_mb = total // 1_000_000
    available_mb = min(total, available) // 1_000_000
    reserved_cpus = min(cpus - 1, max(1, math.ceil(cpus * 0.1)))
    reserved_mb = max(min(2000, total_mb // 4), math.ceil(total_mb * 0.1))
    return {
        "host_cpus": host_cpus,
        "effective_cpus": cpus,
        "cpu_quota": cpu_capacity,
        "total_memory_mb": total_mb,
        "available_memory_mb": available_mb,
        "reserved_cpus": reserved_cpus,
        "reserved_memory_mb": reserved_mb,
        "usable_cpus": max(1, cpus - reserved_cpus),
        "usable_memory_mb": max(0, total_mb - reserved_mb),
        "available_for_launch_mb": max(0, available_mb - reserved_mb),
        "constraints": sorted(set(applied)),
        "memory_detected": bool(total_mb and "MemAvailable" in memory),
    }


def policy(config: dict, detected: dict) -> dict:
    cores = min(config["max_cores"], detected["usable_cpus"])
    memory = min(
        config.get("max_memory_mb") or detected["usable_memory_mb"],
        detected["usable_memory_mb"],
    )
    parallel = max(1, min(config.get("max_parallel", 2), cores))
    return {
        "max_cores": cores,
        "max_memory_mb": memory,
        "max_parallel": parallel,
        "detected": detected,
        "recommended": {
            "cores": max(1, cores // parallel),
            "memory_mb": min(
                memory // parallel, detected["available_for_launch_mb"] // parallel
            ),
        },
    }


def admission_reason(candidate: dict, active: list[dict], capacity: dict) -> str:
    """Return a queue reason; reservations survive controller/process recovery."""
    if active and not candidate.get("allow_parallel", False):
        return "Waiting for other pipelines; simultaneous execution was not approved"
    if active and not candidate.get("memory_mb"):
        return "Waiting for other pipelines; this run has no memory reservation"
    if len(active) >= capacity["max_parallel"]:
        return "Waiting for a parallel run slot"
    if any(job.get("pipeline") == candidate.get("pipeline") for job in active):
        return "Waiting for this pipeline's current run to finish"
    if (
        sum(max(1, int(job.get("cores") or 1)) for job in active)
        + max(1, int(candidate.get("cores") or 1))
        > capacity["max_cores"]
    ):
        return "Waiting for CPU capacity"
    # Legacy requests without an explicit reservation may run alone. While
    # active they reserve the whole budget, so newer jobs cannot overlap them.
    memory = int(
        candidate.get("memory_mb")
        or min(
            capacity["max_memory_mb"], capacity["detected"]["available_for_launch_mb"]
        )
    )
    reserved = sum(
        int(job.get("memory_mb") or capacity["max_memory_mb"]) for job in active
    )
    if memory + reserved > capacity["max_memory_mb"]:
        return "Waiting for reserved memory capacity"
    if not capacity["detected"]["memory_detected"]:
        return "Waiting for reliable system memory detection"
    if capacity["detected"]["available_for_launch_mb"] <= 0:
        return "Waiting for available system memory"
    if memory > capacity["detected"]["available_for_launch_mb"]:
        return "Waiting for available system memory"
    return ""


class Monitor:
    """Coalesced /proc samples; no subprocess commands or environment reads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.previous: dict = {}
        self.sampled_at = 0.0
        self.system: tuple[int, int] | None = None
        self.cached: dict = {}
        self.job_key: tuple = ()

    def sample(self, jobs: list[dict], proc: Path = Path("/proc")) -> dict:
        key = tuple((j["id"], j.get("pid"), j.get("process_start")) for j in jobs)
        with self.lock:
            now = time.monotonic()
            if self.cached and key == self.job_key and now - self.sampled_at < 1.0:
                return self.cached
            elapsed = now - self.sampled_at if self.sampled_at else 0
            cpu = _read(proc / "stat").splitlines()
            cpu_percent = None
            if cpu and cpu[0].startswith("cpu "):
                fields = [int(value) for value in cpu[0].split()[1:9]]
                total = sum(fields)
                idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
                if self.system and total > self.system[0]:
                    cpu_percent = round(
                        100 * (1 - (idle - self.system[1]) / (total - self.system[0])),
                        1,
                    )
                self.system = (total, idle)
            records = {}
            if any(job.get("pid") for job in jobs):
                for directory in proc.iterdir():
                    if not directory.name.isdigit():
                        continue
                    text = _read(directory / "stat")
                    try:
                        tail = text.rsplit(")", 1)[1].split()
                        pid = int(directory.name)
                        records[pid] = {
                            "parent": int(tail[1]),
                            "group": int(tail[2]),
                            "ticks": int(tail[11]) + int(tail[12]),
                            "start": tail[19],
                            "rss": max(0, int(tail[21])),
                        }
                    except (IndexError, ValueError):
                        continue
            per_job = {}
            ticks_per_second = os.sysconf("SC_CLK_TCK")
            page_bytes = os.sysconf("SC_PAGE_SIZE")
            for job in jobs:
                leader = records.get(job.get("pid"))
                if not leader or leader["start"] != str(job.get("process_start") or ""):
                    per_job[job["id"]] = {
                        "available": False,
                        "cpu_cores": None,
                        "memory_mb": None,
                    }
                    continue
                members = {
                    pid
                    for pid, record in records.items()
                    if record["group"] == leader["group"]
                }
                # Include descendants that created another process group/session.
                while True:
                    descendants = {
                        pid
                        for pid, record in records.items()
                        if record["parent"] in members
                    }
                    if descendants <= members:
                        break
                    members.update(descendants)
                delta = 0
                sampled = False
                for pid in members:
                    identity = (pid, records[pid]["start"])
                    previous = self.previous.get(identity)
                    if previous is not None:
                        delta += max(0, records[pid]["ticks"] - previous)
                        sampled = True
                per_job[job["id"]] = {
                    "available": True,
                    "cpu_cores": round(delta / ticks_per_second / elapsed, 2)
                    if elapsed and sampled
                    else None,
                    "memory_mb": round(
                        sum(records[pid]["rss"] for pid in members)
                        * page_bytes
                        / 1_000_000,
                        1,
                    ),
                    "processes": len(members),
                }
            self.previous = {
                (pid, record["start"]): record["ticks"]
                for pid, record in records.items()
            }
            self.sampled_at = now
            self.job_key = key
            self.cached = {
                "sampled_at": time.time(),
                "system_cpu_percent": cpu_percent,
                "jobs": per_job,
            }
            return self.cached


MONITOR = Monitor()
