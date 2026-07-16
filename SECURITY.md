# Security policy

## Support boundary

OmicsANG is a local-only application for one trusted OS user and trusted local pipeline repositories. It is not a remote service, multi-user service, or clinical diagnostic system. Never bind it to a non-loopback address or expose it through a proxy or network tunnel. Pipelines and shells execute with the current user's OS permissions. External agent tools are the exception: they are launched inside an unprivileged bubblewrap mount namespace whose only writable project path is the working repository, with the rest of the home directory and every sibling pipeline absent rather than merely unreadable. That containment restricts what an agent can *read from disk*; it deliberately does not restrict network egress, because an agent CLI must reach its provider to function. See `docs/agent-sandbox.md` for the exact boundary, its limits, and `OMICSANG_AGENT_SANDBOX`. Pipeline and shell logs and results can contain genomic, clinical, personal, or confidential data. OmicsANG never assigns a durable terminal log to agent sessions: injected prompts and provider output are retained only in bounded process memory while the session is alive and are cleared when it ends. Fleet retains a one-way prompt digest and byte count rather than a second raw prompt copy. External agent tools may independently transmit or retain repository, prompt, or log content under their providers' configuration and terms. OmicsANG has no telemetry or analytics by default.

The optional public-data search sends search terms, selected NCBI database names, and accession identifiers to NCBI E-utilities over HTTPS. SRA Toolkit downloads make their own outbound requests under that tool's configuration and terms. Do not use confidential, patient-derived, or unpublished identifiers as public search terms.

Browse and contextual help are local. Package help reads supported text files without importing or executing project modules. Command-parameter help uses a bundled, version-labelled catalog and bounded browser-side caret matching; it never resolves or executes a repository command. Neither feature fetches package metadata or documentation. Opening a displayed official-documentation link is an explicit outbound browser action. HTTP request-line logging is disabled by default; `--access-log` is an opt-in diagnostic mode whose output may contain pipeline names, paths, or search terms from legacy URL-based file surfaces.

Security fixes are supported only for the latest public release. The latest
supported version is `0.2.1`; earlier versions are unsupported. When a newer
public release is published, users must upgrade to it to continue receiving
security fixes.

## Reporting a vulnerability

Security contact: Anil Kumar Challagandla.

Private reporting route:
[challagandla.anil@gmail.com](mailto:challagandla.anil@gmail.com).

Do not put credentials, patient data, unpublished genomic data, or exploit details
in a public issue. Send only the minimum information needed to reproduce the
problem. If GitHub private vulnerability reporting is enabled later, that route
may be added here as an alternative. No response-time or remediation-time
commitment is made by this document.

## Operational precautions

- Launch OmicsANG only on a trusted machine and keep the generated launch URL private.
- Review pipeline code before running it; OmicsANG does not isolate child processes.
- Review every repository, worktree, prompt, and context category presented before approving external-agent access. Run-debug has a structured prompt preview; other launch surfaces may show a scope summary or truncated task rather than an exact complete payload.
- Agent subprocesses receive a provider-specific allowlisted environment. Unrelated tokens, SSH-agent sockets, and generic cloud/Kubernetes credentials are stripped; configure any additional authentication mode through a separately reviewed integration rather than a broad inherited environment.
- Treat the no-agent-log policy as local data minimization, not as a provider retention guarantee. Stop the agent and OmicsANG process when its in-memory output is no longer needed.
- Keep runtime state outside repositories, set an appropriate retention period, and clear it when no longer needed.
- Keep Python dependencies, browsers, Git, and optional command-line tools updated under their own support and licensing terms.
