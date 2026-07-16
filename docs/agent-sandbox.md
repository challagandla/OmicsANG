<!--
SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
SPDX-License-Identifier: MIT
-->

# Containing external agents

OmicsANG launches Claude Code and Codex as ordinary processes on your machine.
Without containment they run as your OS account, which means an agent asked to
debug one pipeline can also read every other pipeline on the box, your `~/.ssh`
keys, and any genomic or clinical data your account can reach.

OmicsANG now wraps those agents in an unprivileged
[bubblewrap](https://github.com/containers/bubblewrap) mount namespace before
exec. Because the wrapper is applied *outside* the agent process, the agent
cannot edit or disable it — unlike a CLI's own settings file, which is just
another file the agent can write.

## What the boundary is

| | Inside the sandbox |
| --- | --- |
| Working repository | read/write (the only writable project path) |
| Linked worktree's parent `.git` | read/write, so git keeps working |
| Selected provider's config (`~/.claude`, or `~/.codex`) | read/write — the CLI cannot authenticate otherwise |
| The *other* provider's config | absent |
| Rest of `$HOME` (`.ssh`, `.aws`, `.gnupg`, dotfiles) | absent — `$HOME` is a tmpfs |
| Sibling pipelines | absent — never mounted into the namespace |
| System runtime (`/usr`, `/etc`, `/bin`, …) | read-only |
| `/tmp` | private tmpfs |
| Network | **unrestricted** |

Filesystem containment mirrors the per-tool split OmicsANG already applies to
environment variables: a Claude session is not handed Codex's credentials, and
vice versa.

## What it is not

**Network egress is not restricted.** An agent CLI has to reach its provider to
work at all, and a mount namespace cannot tell `api.anthropic.com` from
exfiltration. Anything the agent can read, it can still send. The SECURITY.md
warning that provider tools may transmit repository, prompt, or log content
applies unchanged — containment shrinks *what the agent can read*, and that is
the whole of the claim.

If you want domain-level egress control, configure it in the CLI itself; it
composes with this layer rather than replacing it:

- **Claude Code** — `sandbox.network.allowedDomains` in `settings.json`
  (`claude` also enforces its own bubblewrap sandbox for Bash tool calls).
- **Codex** — `codex --sandbox workspace-write`, whose network policy is set in
  `~/.codex/config.toml`.

Two other honest limits: the selected provider's own credentials stay reachable
by necessity, and an operator `shell` session is deliberately **not** contained —
that is your terminal, not an external agent.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OMICSANG_AGENT_SANDBOX` | `auto` | `auto` contains when the kernel allows and prints a visible note when it cannot; `require` refuses to launch an agent that cannot be contained; `off` restores the old uncontained behaviour. |
| `OMICSANG_AGENT_SANDBOX_READ` | — | `:`-separated extra read-only roots. |
| `OMICSANG_AGENT_SANDBOX_WRITE` | — | `:`-separated extra writable roots. |

Reference data lives outside the repo more often than not, so expect to need
the read roots:

```bash
export OMICSANG_AGENT_SANDBOX=require
export OMICSANG_AGENT_SANDBOX_READ=/data/references:/data/annotations
export OMICSANG_AGENT_SANDBOX_WRITE=/scratch/$USER
```

Every agent session prints its boundary into the terminal scrollback on launch,
including when containment was *not* applied, so an uncontained agent is never
silent.

## Requirements

Bubblewrap must be installed and unprivileged user namespaces must be permitted:

```bash
sudo apt-get install bubblewrap        # Debian/Ubuntu
sysctl kernel.apparmor_restrict_unprivileged_userns   # must be 0 on Ubuntu 24.04+
```

OmicsANG probes for a working sandbox rather than trusting that the binary
exists, because several distributions ship bubblewrap while restricting the user
namespaces it depends on. On a host without it, `auto` degrades with a note and
`require` refuses to start the agent.

## Troubleshooting

- **The agent cannot read a reference genome.** Add it to
  `OMICSANG_AGENT_SANDBOX_READ`.
- **`git` fails inside a worktree.** OmicsANG binds the parent `.git` directory
  automatically; if you launched the agent against a path that is not a
  registered worktree, use the worktree API instead.
- **"agent NOT contained" in the scrollback.** The kernel refused the sandbox —
  check the AppArmor sysctl above. Set `OMICSANG_AGENT_SANDBOX=require` if you
  would rather fail than proceed uncontained.
