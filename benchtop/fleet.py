# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Fleet jobs — one AI task fanned out across many pipelines at once.

A job holds a one-way prompt digest, tool metadata, and one *target* per
pipeline. The request carries the raw prompt only while dispatching each agent;
the registry never keeps a second prompt copy. Each target runs its own agent
Session (usually in a separate git worktree) so pipelines can be reviewed
independently.

Job metadata is kept in memory and mirrored to the run history store so it
survives a browser reload; the live agent Sessions live in the SessionManager.
"""

from __future__ import annotations

import hashlib
import time
import uuid

from . import store

_JOBS: dict[str, dict] = {}
_PROMPT_PREVIEW = "[prompt omitted]"


def _discard_prompt(job: dict) -> None:
    """Remove transient prompt text while retaining non-secret audit metadata."""
    if "prompt" in job:
        job.pop("prompt", None)
        job["prompt_discarded"] = time.time()


def new_job(prompt: str, tool: str, use_worktree: bool) -> dict:
    prompt_bytes = prompt.encode("utf-8")
    created = time.time()
    job = {
        "id": "fleet-" + uuid.uuid4().hex,
        "created": created,
        "prompt_digest": "sha256:" + hashlib.sha256(prompt_bytes).hexdigest(),
        "prompt_length": len(prompt_bytes),
        "prompt_preview": _PROMPT_PREVIEW,
        # The request body remains available to server.py for dispatch.  Avoid
        # making a second, indefinitely retained copy in the fleet registry.
        "prompt_discarded": created,
        "tool": tool,
        "use_worktree": use_worktree,
        "targets": [],  # {pipeline, cwd, branch, session_id, worktree, error}
    }
    _JOBS[job["id"]] = job
    return job


def add_target(job: dict, **target) -> None:
    job["targets"].append(target)
    if target.get("session_id") and not target.get("error"):
        # server.py carries the prompt separately while fanning out.  The job
        # record no longer needs raw text as soon as the first agent accepted it.
        _discard_prompt(job)


def persist(job: dict) -> None:
    # Persist is also the terminal path for an aborted fleet preparation.  Such
    # jobs have no retry operation, so retaining their unused prompt is needless.
    _discard_prompt(job)
    store.record(
        {
            "id": job["id"],
            "kind": "fleet",
            "pipeline": "(fleet)",
            "title": f"fleet · {job['tool']} · {len(job['targets'])} pipelines",
            "status": "launched",
            "started": job["created"],
            "ended": job["created"],
            "command": "[agent prompt omitted]",
            "prompt_digest": job["prompt_digest"],
            "prompt_length": job["prompt_length"],
            "prompt_preview": job["prompt_preview"],
            "targets": [
                {
                    "pipeline": t["pipeline"],
                    "branch": t.get("branch"),
                    "session_id": t.get("session_id"),
                }
                for t in job["targets"]
            ],
        }
    )


def get(job_id: str) -> dict | None:
    return _JOBS.get(job_id)


def all_jobs() -> list[dict]:
    return sorted(_JOBS.values(), key=lambda j: j["created"], reverse=True)
