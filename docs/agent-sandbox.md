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
| Linked worktree's parent `.git` | read/write, so git keeps working — only for a worktree that repository itself registered |
| Selected provider's credentials and live state (`~/.claude`, or `~/.codex`) | read/write — the CLI cannot authenticate otherwise |
| What that provider executes or loads as instructions (`settings.json`, `CLAUDE.md`, `hooks`, `plugins`, `skills`, `commands`, `agents`, `rules`; Codex `*.toml`, `AGENTS.md`, `prompts`) | **read-only** |
| `~/.claude.json` (state mixed with MCP server commands) | a private copy, refreshed at each launch and never written back |
| Other projects' transcripts, prompt history, caches, logs, IDE and daemon control files | absent — replaced by empty scratch space; only this repository's transcript directory is kept |
| The *other* provider's config | absent |
| Rest of `$HOME` (`.ssh`, `.aws`, `.gnupg`, dotfiles) | absent — `$HOME` is a tmpfs |
| Sibling pipelines | absent — never mounted into the namespace |
| System runtime (`/usr`, `/etc`, `/bin`, …) | read-only |
| `/tmp` | private tmpfs |
| Unix-domain sockets | **blocked** — `socket(AF_UNIX)` is refused, so desktop X11 and D-Bus sockets cannot be reached |
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

**Loopback services are reachable.** The network namespace is shared with the
host, so any TCP service listening on `127.0.0.1` (a notebook server, a local
database, a dashboard) can be contacted by the agent. Do not run unauthenticated
local services next to an agent you do not trust. Abstract unix sockets live in
that same namespace and authenticate by uid, which is why the socket family is
refused outright instead.

Other honest limits:

- The selected provider's own credentials stay reachable by necessity.
- An operator `shell` session is deliberately **not** contained — that is your
  terminal, not an external agent.
- The first contained launch creates a few empty placeholders in the provider's
  config directory (for Claude: `CLAUDE.md`, `settings.json` containing `{}`,
  and empty `plugins`, `skills`, `commands`, `agents`, `rules`,
  `output-styles`) when they do not exist. A name that does not exist cannot be
  mounted read-only, and the agent could otherwise create it; the placeholders
  do not change how the CLI behaves.
- A protected entry that is a **symlink** (for example `settings.json` managed
  by a dotfiles repository) cannot be pinned read-only and is left as it is.
  Replace the symlink with a real file if you rely on this protection.
- Settings the CLI would normally save from inside a session — a changed theme,
  a "don't ask again" permission, a trusted-folder answer, a newly added MCP
  server — are not persisted from a contained session. Make those changes in
  the CLI outside OmicsANG.
- OmicsANG's own git calls ignore the repository's hooks and any command its
  `.git/config` names (filters, external diff, fsmonitor, ssh command,
  credential helper). Your global git configuration still applies. A
  repository that relies on a Git LFS pre-push hook should be pushed from your
  own terminal.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OMICSANG_AGENT_SANDBOX` | `auto` | `auto` contains when the kernel allows and prints a visible note when it cannot; `require` refuses to launch an agent that cannot be contained; `off` restores the old uncontained behaviour. Any other value is treated as `require`, so a typo never launches an uncontained agent. |
| `OMICSANG_AGENT_SANDBOX_UNIX_SOCKETS` | blocked | Set to `allow` only if an agent tool genuinely needs unix-domain sockets; this re-exposes the desktop session's X11 and D-Bus sockets. |
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
- **A tool inside the agent reports "Operation not permitted" opening a
  socket.** It tried to create a unix-domain socket. See
  `OMICSANG_AGENT_SANDBOX_UNIX_SOCKETS` above and its trade-off.
- **The CLI says it cannot save a setting.** Its configuration is read-only
  inside the sandbox by design; change it from the CLI outside OmicsANG.
- **"agent NOT contained" in the scrollback.** The kernel refused the sandbox —
  check the AppArmor sysctl above. Set `OMICSANG_AGENT_SANDBOX=require` if you
  would rather fail than proceed uncontained.
