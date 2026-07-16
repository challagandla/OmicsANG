# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""OmicsANG durable state facade.

The public history/template/Slurm helpers retain their original return shapes,
but all persistence now uses the SQLite WAL control-plane database.  New job,
event, and lease helpers are intentionally exposed here so callers do not need
to know the physical schema.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import state_db

InvalidTransition = state_db.InvalidTransition
StateError = state_db.StateError
StateRootMismatch = state_db.StateRootMismatch
TERMINAL_STATES = state_db.TERMINAL_STATES


def database_status() -> dict[str, Any]:
    return state_db.status()


def initialize() -> dict[str, Any]:
    """Apply migrations/import eagerly; safe to call repeatedly."""
    return database_status()


def record(entry: dict[str, Any]) -> None:
    state_db.record_history(entry)


def history(pipeline: str | None = None, limit: int = 100) -> list[dict]:
    return state_db.history(pipeline, limit)


def run_queue_config(default_max_cores: int) -> dict:
    cfg = state_db.configuration("run_queue", {})
    try:
        max_cores = max(1, int((cfg or {}).get("max_cores", default_max_cores)))
    except (AttributeError, TypeError, ValueError):
        max_cores = default_max_cores
    return {"max_cores": max_cores}


def set_run_queue_config(max_cores: int) -> dict:
    saved = {"max_cores": max(1, int(max_cores))}
    state_db.set_configuration("run_queue", saved)
    return saved


def _results_attachment_key(checkout: str | Path) -> tuple[str, str]:
    root = str(Path(checkout).expanduser().resolve())
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()
    return f"results_attachments:v1:{digest}", root


def _attachment_paths(value: Any, checkout: str) -> list[str]:
    if not isinstance(value, dict) or value.get("pipeline_root") != checkout:
        return []
    paths = value.get("paths")
    if not isinstance(paths, list):
        return []
    return list(dict.fromkeys(str(path) for path in paths if str(path).strip()))


def results_attachments(checkout: str | Path) -> list[str]:
    """Return canonical UI result attachments for one pipeline checkout."""
    key, root = _results_attachment_key(checkout)
    return _attachment_paths(state_db.configuration(key, {}), root)


def attach_results_directory(
    checkout: str | Path,
    directory: str | Path,
) -> tuple[list[str], bool]:
    """Persist an attachment once; the read/modify/write is process-safe."""
    key, root = _results_attachment_key(checkout)
    target = str(Path(directory).expanduser().resolve())
    created = False

    def update(value: Any) -> dict[str, Any]:
        nonlocal created
        paths = _attachment_paths(value, root)
        if target not in paths:
            paths.append(target)
            created = True
        return {"version": 1, "pipeline_root": root, "paths": paths}

    saved = state_db.mutate_configuration(key, update, {})
    return _attachment_paths(saved, root), created


def detach_results_directory(
    checkout: str | Path,
    directory: str | Path,
) -> tuple[list[str], bool]:
    """Remove an attachment idempotently while retaining checkout isolation."""
    key, root = _results_attachment_key(checkout)
    raw_target = str(directory)
    try:
        target = str(Path(raw_target).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        # Persisted configuration can outlive a mount or become malformed.
        # Exact-value removal remains safe because this function never touches
        # the referenced filesystem path.
        target = raw_target
    removed = False

    def update(value: Any) -> dict[str, Any]:
        nonlocal removed
        paths = _attachment_paths(value, root)
        if target in paths:
            paths.remove(target)
            removed = True
        return {"version": 1, "pipeline_root": root, "paths": paths}

    saved = state_db.mutate_configuration(key, update, {})
    return _attachment_paths(saved, root), removed


def run_templates(pipeline: str) -> list[dict]:
    return state_db.templates(pipeline)


def save_run_template(pipeline: str, template: dict[str, Any]) -> dict:
    return state_db.save_template(pipeline, template)


def delete_run_template(pipeline: str, template_id: str) -> bool:
    return state_db.delete_template(pipeline, template_id)


def record_slurm_job(job: dict[str, Any]) -> dict:
    return state_db.record_slurm_job(job)


def slurm_jobs(pipeline: str | None = None, limit: int = 100) -> list[dict]:
    return state_db.slurm_jobs(pipeline, limit)


def slurm_job(*, intent_id: str = "", scheduler_job_id: str = "") -> dict | None:
    return state_db.slurm_job(intent_id=intent_id, scheduler_job_id=scheduler_job_id)


def record_capsule_ref(capsule: dict[str, Any]) -> dict:
    return state_db.record_capsule_ref(capsule)


def capsule_refs(pipeline: str | None = None, limit: int = 100) -> list[dict]:
    return state_db.capsule_refs(pipeline, limit)


def create_job(
    *,
    job_id: str,
    kind: str,
    executor: str,
    pipeline: str = "",
    state: str = "created",
    idempotency_key: str = "",
    plan_digest: str = "",
    session_id: str = "",
    scheduler_job_id: str = "",
    payload: dict[str, Any] | None = None,
    private: dict[str, Any] | None = None,
    actor: str = "benchtop",
    lease_owner: str = "",
    lease_ttl: float = 0.0,
) -> tuple[dict[str, Any], bool]:
    return state_db.create_job(
        job_id=job_id,
        kind=kind,
        executor=executor,
        pipeline=pipeline,
        state=state,
        idempotency_key=idempotency_key,
        plan_digest=plan_digest,
        session_id=session_id,
        scheduler_job_id=scheduler_job_id,
        payload=payload,
        private=private,
        actor=actor,
        lease_owner=lease_owner,
        lease_ttl=lease_ttl,
    )


def get_job(job_id: str, *, include_private: bool = False) -> dict | None:
    return state_db.get_job(job_id, include_private=include_private)


def job_by_idempotency(key: str, *, include_private: bool = False) -> dict | None:
    return state_db.job_by_idempotency(key, include_private=include_private)


def job_by_scheduler_id(job_id: str, *, include_private: bool = False) -> dict | None:
    return state_db.job_by_scheduler_id(job_id, include_private=include_private)


def jobs_by_scheduler_id(
    job_id: str,
    *,
    include_private: bool = False,
) -> list[dict]:
    return state_db.jobs_by_scheduler_id(job_id, include_private=include_private)


def list_jobs(
    *,
    pipeline: str | None = None,
    kind: str | None = None,
    executor: str | None = None,
    states: Iterable[str] | None = None,
    limit: int = 100,
    oldest_first: bool = False,
    include_private: bool = False,
) -> list[dict]:
    return state_db.list_jobs(
        pipeline=pipeline,
        kind=kind,
        executor=executor,
        states=states,
        limit=limit,
        oldest_first=oldest_first,
        include_private=include_private,
    )


def update_job(
    job_id: str,
    *,
    payload_update: dict[str, Any] | None = None,
    private_update: dict[str, Any] | None = None,
    expected_states: Sequence[str] | None = None,
    expected_version: int | None = None,
    expected_payload: dict[str, Any] | None = None,
    fence_resource: str = "",
    fence_owner: str = "",
    fence_token: str = "",
    event_type: str = "job_updated",
    reason: str = "",
    actor: str = "benchtop",
    **fields: Any,
) -> dict:
    return state_db.update_job(
        job_id,
        payload_update=payload_update,
        private_update=private_update,
        expected_states=expected_states,
        expected_version=expected_version,
        expected_payload=expected_payload,
        fence_resource=fence_resource,
        fence_owner=fence_owner,
        fence_token=fence_token,
        event_type=event_type,
        reason=reason,
        actor=actor,
        **fields,
    )


def transition_job(
    job_id: str,
    to_state: str,
    *,
    expected_states: Sequence[str] | None = None,
    expected_version: int | None = None,
    event_type: str = "state_changed",
    reason: str = "",
    actor: str = "benchtop",
    payload_update: dict[str, Any] | None = None,
    private_update: dict[str, Any] | None = None,
    **fields: Any,
) -> dict:
    return state_db.transition_job(
        job_id,
        to_state,
        expected_states=expected_states,
        expected_version=expected_version,
        event_type=event_type,
        reason=reason,
        actor=actor,
        payload_update=payload_update,
        private_update=private_update,
        **fields,
    )


def append_event(
    job_id: str,
    event_type: str,
    *,
    reason: str = "",
    actor: str = "benchtop",
    payload: dict[str, Any] | None = None,
) -> dict:
    return state_db.append_event(
        job_id,
        event_type,
        reason=reason,
        actor=actor,
        payload=payload,
    )


def job_events(job_id: str, *, after: int = 0, limit: int = 500) -> list[dict]:
    return state_db.job_events(job_id, after=after, limit=limit)


def acquire_job_lease(
    job_id: str,
    owner: str,
    *,
    ttl: float = 30.0,
    expected_states: Sequence[str] = ("queued",),
) -> dict | None:
    return state_db.acquire_job_lease(
        job_id,
        owner,
        ttl=ttl,
        expected_states=expected_states,
    )


def release_job_lease(job_id: str, owner: str) -> bool:
    return state_db.release_job_lease(job_id, owner)


def renew_job_lease(
    job_id: str,
    owner: str,
    *,
    ttl: float = 15.0,
    expected_states: Sequence[str] = (
        "preparing",
        "running",
        "cancel_requested",
        "cancelling",
    ),
) -> bool:
    return state_db.renew_job_lease(
        job_id,
        owner,
        ttl=ttl,
        expected_states=expected_states,
    )


def acquire_lease(
    resource: str,
    owner: str,
    *,
    ttl: float = 30.0,
    payload: dict[str, Any] | None = None,
) -> str | None:
    return state_db.acquire_lease(resource, owner, ttl=ttl, payload=payload)


def release_lease(resource: str, owner: str, token: str = "") -> bool:
    return state_db.release_lease(resource, owner, token)


def lease(resource: str) -> dict | None:
    return state_db.lease(resource)
