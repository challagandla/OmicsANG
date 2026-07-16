# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""PTY-backed terminal sessions — the single core abstraction of OmicsANG.

Every pipeline run *and* every AI-agent invocation is a `Session`: a real
pseudo-terminal running a command in a working directory. Sessions are:

  * persistent   — they keep running even if no browser is attached;
  * re-attachable — a websocket can attach/detach at will and gets a replay of
    everything printed so far (ring buffer) before live output resumes;
  * optionally teed to disk — pipeline output can be retained for provenance,
    while agent terminals remain memory-only to avoid durable prompt/provider
    output capture.

This unification is what lets the UI treat a failed Snakemake run and a Claude
Code session the same way, and is what powers "debug this run with Claude".
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import shlex
import signal
import struct
import termios
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Optional

from .runplan import redact_argv, redact_mapping

# Sentinel pushed onto subscriber queues when a session ends.
EOF = object()

_MAX_BUFFER = 2_000_000  # bytes of scrollback retained in memory per session
_MAX_SUBSCRIBERS = 8
_SUBSCRIBER_QUEUE_ITEMS = 256
_AGENT_RUNTIME_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "COLORTERM",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_NUMERIC",
        "LC_TIME",
        "LC_COLLATE",
        "LC_MONETARY",
        "LC_PAPER",
        "LC_NAME",
        "LC_ADDRESS",
        "LC_TELEPHONE",
        "LC_MEASUREMENT",
        "LC_IDENTIFICATION",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "GIT_SSL_CAINFO",
    }
)
_AGENT_PROVIDER_ENV = {
    "claude": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CONFIG_DIR",
            "CLAUDE_CODE_OAUTH_TOKEN",
        }
    ),
    "codex": frozenset(
        {
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORG_ID",
            "OPENAI_ORGANIZATION",
            "OPENAI_PROJECT_ID",
            "CODEX_HOME",
        }
    ),
}
_PUBLIC_META_INTERNAL_KEYS = {
    "cwd",
    "log_path",
    "logfile",
    "plan_record",
    "script",
    "state_dir",
    "trash_path",
    "trashed_to",
}


def _child_environment(
    kind: str,
    source: Mapping[str, object],
    overrides: Mapping[str, object] | None = None,
    *,
    tool: str = "",
) -> dict[str, str]:
    """Build a child environment without exposing server secrets to agents.

    Non-agent pipeline/terminal sessions retain the historical full-environment
    behavior.  Agent sessions receive only runtime/locale/network trust settings
    plus the credentials/configuration for the explicitly selected provider.
    Filtering explicit overrides as well as the parent environment prevents an
    internal caller from accidentally bypassing the boundary.
    """
    combined = {str(key): str(value) for key, value in source.items()}
    combined.update({str(key): str(value) for key, value in (overrides or {}).items()})
    if kind != "agent":
        return combined
    allowed = _AGENT_RUNTIME_ENV | _AGENT_PROVIDER_ENV.get(str(tool), frozenset())
    return {key: combined[key] for key in allowed if key in combined}


class Session:
    def __init__(
        self,
        *,
        kind: str,  # 'run' | 'agent' | 'shell'
        title: str,
        cwd: str,
        argv: list[str],
        env: Optional[dict] = None,
        cols: int = 120,
        rows: int = 32,
        meta: Optional[dict] = None,
        logfile: Optional[str] = None,
        on_exit: Optional[Callable[["Session"], None]] = None,
        session_id: str = "",
        created: Optional[float] = None,
    ):
        self.id = session_id or uuid.uuid4().hex
        self.kind = kind
        self.title = title
        self.cwd = cwd
        self.argv = argv
        self.env = env or {}
        self.cols = cols
        self.rows = rows
        self.meta = meta or {}
        # Agent prompts and provider output may contain supplied repository or
        # personal context.  Agent terminals are deliberately memory-only even
        # if a caller accidentally supplies a path.
        self.logfile = None if kind == "agent" else logfile
        self._on_exit = on_exit

        self.created = float(created or time.time())
        self.started: Optional[float] = None
        self.ended: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.status = "created"  # created|queued|running|exited|failed|killed
        self.pid: Optional[int] = None
        self.pgid: Optional[int] = None
        self.process_start = ""
        self.boot_id = self._boot_id()
        self.fd: Optional[int] = None

        self._buffer = bytearray()
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._logf = None
        self._eof_done = False
        self._exit_callback_started = False
        self._initial_echo_candidates: tuple[bytes, ...] = ()
        self._initial_echo_buffer = bytearray()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.status == "killed":
            return
        if self.status not in {"created", "queued"}:
            raise RuntimeError(f"session {self.id} cannot start from {self.status!r}")
        self._loop = asyncio.get_running_loop()
        env = _child_environment(
            self.kind,
            os.environ,
            self.env,
            tool=str(self.meta.get("tool") or ""),
        )
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("FORCE_COLOR", "1")

        pid, fd = pty.fork()
        if pid == 0:  # ---- child ----
            try:
                os.chdir(self.cwd)
            except Exception as exc:
                os.write(2, f"OmicsANG: cannot enter {self.cwd!r}: {exc}\n".encode())
                os._exit(126)
            try:
                os.execvpe(self.argv[0], self.argv, env)
            except Exception as exc:  # pragma: no cover - exec failure path
                os.write(2, f"OmicsANG: cannot exec {self.argv!r}: {exc}\n".encode())
                os._exit(127)
        # ---- parent ----
        self.pid = pid
        try:
            self.pgid = os.getpgid(pid)
        except OSError:
            self.pgid = None
        self.process_start = self._process_start_token(pid)
        self.fd = fd
        self.started = time.time()
        self.status = "running"
        try:
            self._set_winsize(self.rows, self.cols)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            if self.logfile:
                try:
                    log_fd = os.open(
                        self.logfile,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                        0o600,
                    )
                    os.fchmod(log_fd, 0o600)
                    self._logf = os.fdopen(log_fd, "wb")
                except OSError:
                    self._logf = None
            self._loop.add_reader(fd, self._on_readable)
        except Exception:
            # The fork boundary has already been crossed. Ensure a caller never
            # receives an exception while an untracked child and PTY remain.
            self.send_signal(signal.SIGTERM)
            self._on_eof()
            raise

    def note(self, message: str) -> None:
        """Append an OmicsANG system note to the scrollback before/around exec."""
        if not message.endswith("\n"):
            message += "\n"
        self._append(message.encode())

    def _on_readable(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                return
            data = b""  # EIO etc -> treat as EOF
        if not data:
            self._on_eof()
            return
        self._append(data)

    def _append(self, data: bytes) -> None:
        data = self._filter_initial_input_echo(data)
        if not data:
            return
        self._buffer.extend(data)
        if len(self._buffer) > _MAX_BUFFER:
            del self._buffer[: len(self._buffer) - _MAX_BUFFER]
        if self._logf:
            try:
                self._logf.write(data)
                self._logf.flush()
            except Exception:
                pass
        for q in list(self._subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass  # slow client; it still has scrollback on reattach

    def _filter_initial_input_echo(self, data: bytes) -> bytes:
        """Remove only the terminal driver's leading echo of private input."""
        candidates = self._initial_echo_candidates
        if not candidates:
            return data
        self._initial_echo_buffer.extend(data)
        buffered = bytes(self._initial_echo_buffer)
        for candidate in candidates:
            index = buffered.find(candidate)
            if index >= 0:
                public = buffered[:index] + buffered[index + len(candidate) :]
                self._initial_echo_candidates = ()
                self._initial_echo_buffer.clear()
                return public
        if any(candidate.startswith(buffered) for candidate in candidates):
            return b""
        # Echo suppression succeeded and the first child output is unrelated,
        # or the terminal used an unknown transformation.  Never retain a
        # partial secret candidate, but pass unrelated output through.
        secret_prefix = max(
            (
                size
                for candidate in candidates
                for size in range(1, min(len(candidate), len(buffered)) + 1)
                if buffered.endswith(candidate[:size])
            ),
            default=0,
        )
        if secret_prefix:
            public = buffered[:-secret_prefix]
            self._initial_echo_buffer[:] = buffered[-secret_prefix:]
            return public
        self._initial_echo_candidates = ()
        self._initial_echo_buffer.clear()
        return buffered

    def _on_eof(self) -> None:
        if self._eof_done:
            return
        self._eof_done = True
        if self._loop and self.fd is not None:
            try:
                self._loop.remove_reader(self.fd)
            except Exception:
                pass
        fd = self.fd
        self.fd = None
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        self._poll_child_exit()

    def _poll_child_exit(self) -> None:
        """Reap without blocking the API event loop when PTY EOF precedes exit."""
        if self.ended is not None:
            return
        pid = self.pid
        if not pid:
            self._finish_exit(None)
            return
        try:
            waited, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            self._finish_exit(None)
            return
        except OSError:
            waited, status = 0, 0
        if waited == 0:
            if self._loop and not self._loop.is_closed():
                self._loop.call_later(0.05, self._poll_child_exit)
            return
        code = None
        if os.WIFEXITED(status):
            code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            code = -os.WTERMSIG(status)
        self._finish_exit(code)

    def _finish_exit(self, code: int | None) -> None:
        if self.ended is not None:
            return
        self.exit_code = code
        self.ended = time.time()
        if self.status != "killed":
            self.status = "exited" if code == 0 else "failed"
        if self._logf:
            try:
                self._logf.close()
            except Exception:
                pass
        self.pid = None
        self.pgid = None
        for q in list(self._subscribers):
            try:
                q.put_nowait(EOF)
            except Exception:
                pass
        self._clear_agent_private_buffers()
        self._invoke_exit_callback()

    def _clear_agent_private_buffers(self) -> None:
        """Forget ended agent output after live subscribers receive final data."""
        if self.kind != "agent":
            return
        self._buffer = bytearray()
        self._initial_echo_candidates = ()
        self._initial_echo_buffer = bytearray()

    def _invoke_exit_callback(self) -> None:
        if not self._on_exit or self._exit_callback_started:
            return
        self._exit_callback_started = True
        callback = self._on_exit

        def invoke_callback() -> None:
            try:
                callback(self)
            except Exception:
                pass

        threading.Thread(
            target=invoke_callback,
            name=f"benchtop-exit-{self.id}",
            daemon=True,
        ).start()

    # -- io ----------------------------------------------------------------
    def write(self, data: bytes) -> None:
        if self.fd is not None and self.status == "running":
            try:
                os.write(self.fd, data)
            except OSError:
                pass

    def write_initial_input(self, data: bytes) -> None:
        """Write bootstrap input without teeing the terminal driver's echo.

        Agent prompts are input, not process output, and must not be persisted
        merely because a newly allocated PTY starts with echo enabled.  This
        path fails closed if echo cannot be disabled.  A child process can
        still deliberately print supplied input, but agent output remains
        memory-only and is never assigned a durable OmicsANG terminal log.
        """
        if not data:
            return
        fd = self.fd
        if fd is None or self.status != "running":
            raise RuntimeError("session is not ready for initial input")
        echo_flags = 0
        for name in ("ECHO", "ECHOE", "ECHOK", "ECHONL", "ECHOCTL", "ECHOKE"):
            echo_flags |= int(getattr(termios, name, 0))
        try:
            original = termios.tcgetattr(fd)
            muted = list(original)
            muted[3] &= ~echo_flags
            termios.tcsetattr(fd, termios.TCSANOW, muted)
        except (OSError, termios.error) as exc:
            raise RuntimeError("could not suppress PTY echo for initial input") from exc

        failure: BaseException | None = None
        try:
            crlf_echo = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            self._initial_echo_candidates = tuple(dict.fromkeys((data, crlf_echo)))
            self._initial_echo_buffer.clear()
            pending = memoryview(data)
            while pending:
                written = os.write(fd, pending)
                if written <= 0:
                    raise OSError("PTY accepted no initial-input bytes")
                pending = pending[written:]
            # A PTY master write can return before the slave line discipline
            # consumes its input queue.  Drain while echo is still disabled so
            # restoring the normal flags cannot race with prompt processing.
            termios.tcdrain(fd)
        except BaseException as exc:
            failure = exc
        try:
            current = termios.tcgetattr(fd)
            current[3] = (current[3] & ~echo_flags) | (original[3] & echo_flags)
            termios.tcsetattr(fd, termios.TCSANOW, current)
        except (OSError, termios.error) as exc:
            if failure is None:
                failure = exc
        if failure is not None:
            self._initial_echo_candidates = ()
            self._initial_echo_buffer.clear()
            raise RuntimeError("could not deliver private initial input") from failure

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        if self.fd is not None:
            try:
                self._set_winsize(rows, cols)
            except Exception:
                pass

    def _set_winsize(self, rows: int, cols: int) -> None:
        if self.fd is None:
            return
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    @staticmethod
    def _boot_id() -> str:
        try:
            return (
                Path("/proc/sys/kernel/random/boot_id")
                .read_text(encoding="utf-8")
                .strip()
            )
        except OSError:
            return ""

    @staticmethod
    def _process_start_token(pid: int | None) -> str:
        if not pid:
            return ""
        try:
            # Field 22 follows the parenthesized comm field; split from its last ')'.
            tail = (
                Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1]
            )
            return tail.split()[19]
        except (OSError, IndexError):
            return ""

    def is_alive(self) -> bool:
        pid = self.pid
        if not pid or self.ended is not None:
            return False
        if self.process_start and self._process_start_token(pid) != self.process_start:
            return False
        try:
            process_state = (
                Path(f"/proc/{pid}/stat")
                .read_text(
                    encoding="utf-8",
                )
                .rsplit(")", 1)[1]
                .split()[0]
            )
            if process_state == "Z":
                return False
        except (OSError, IndexError):
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def send_signal(self, sig: int) -> bool:
        """Signal only the identity-checked process group owned by this session."""
        pid = self.pid
        if not pid or self.ended is not None or not self.is_alive():
            return False
        try:
            current_pgid = os.getpgid(pid)
        except OSError:
            return False
        if self.pgid is not None and current_pgid != self.pgid:
            return False
        try:
            os.killpg(current_pgid, sig)
            return True
        except OSError:
            try:
                os.kill(pid, sig)
                return True
            except OSError:
                return False

    def kill(self, sig: int = signal.SIGTERM) -> bool:
        if not self.pid:
            if self.status in {"created", "queued"}:
                self.status = "killed"
                self.exit_code = -sig
                self.ended = time.time()
                self.note(f"OmicsANG: session killed before start (signal {sig})")
                for q in list(self._subscribers):
                    try:
                        q.put_nowait(EOF)
                    except Exception:
                        pass
                self._clear_agent_private_buffers()
                self._invoke_exit_callback()
                return True
            return False
        if self.status not in {"running", "killed"} or self.ended is not None:
            return False
        sent = self.send_signal(sig)
        if sent:
            self.status = "killed"
        return sent

    async def cancel(
        self,
        *,
        interrupt_grace: float = 2.0,
        terminate_grace: float = 2.0,
    ) -> dict:
        """Request graceful stop, then escalate without ever signaling a stale PID."""
        if self.status in {"created", "queued"}:
            changed = self.kill(signal.SIGINT)
            return {
                "requested": changed,
                "signalled": False,
                "escalated": False,
                "alive": False,
                "cancelled_before_start": changed,
            }
        if self.status != "running" or self.ended is not None or not self.is_alive():
            return {
                "requested": False,
                "signalled": False,
                "escalated": False,
                "alive": self.is_alive(),
            }
        signalled = self.send_signal(signal.SIGINT)
        if signalled:
            self.status = "killed"
        escalated = False

        async def wait_for_exit(seconds: float) -> bool:
            deadline = time.monotonic() + max(0.0, seconds)
            while self.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            return not self.is_alive()

        if signalled and await wait_for_exit(interrupt_grace):
            return {
                "requested": True,
                "signalled": True,
                "escalated": False,
                "alive": False,
            }
        if self.is_alive():
            term_sent = self.send_signal(signal.SIGTERM)
            escalated = term_sent or escalated
            signalled = term_sent or signalled
            if term_sent:
                self.status = "killed"
        if signalled and await wait_for_exit(terminate_grace):
            return {
                "requested": True,
                "signalled": True,
                "escalated": escalated,
                "alive": False,
            }
        if self.is_alive():
            kill_sent = self.send_signal(signal.SIGKILL)
            escalated = kill_sent or escalated
            signalled = kill_sent or signalled
            if kill_sent:
                self.status = "killed"
        return {
            "requested": signalled,
            "signalled": signalled,
            "escalated": escalated,
            "alive": self.is_alive(),
        }

    # -- subscription ------------------------------------------------------
    def subscribe(self) -> tuple[asyncio.Queue, bytes]:
        """Attach a consumer. Returns its queue and a replay snapshot."""
        if len(self._subscribers) >= _MAX_SUBSCRIBERS:
            raise RuntimeError("terminal subscriber limit reached")
        q: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_ITEMS)
        self._subscribers.add(q)
        snapshot = bytes(self._buffer)
        if self.status not in ("created", "queued", "running"):
            q.put_nowait(EOF)
        return q, snapshot

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    # -- serialization -----------------------------------------------------
    def to_dict(self) -> dict:
        public_argv = redact_argv(self.argv)
        public_meta = dict(self.meta)
        for key in _PUBLIC_META_INTERNAL_KEYS:
            public_meta.pop(key, None)
        if self.kind == "agent":
            public_argv = public_argv[:1] + (
                ["[prompt omitted]"] if len(self.argv) > 1 else []
            )
            for key in ("prompt", "initial_input", "instructions"):
                public_meta.pop(key, None)
        payload = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "argv": public_argv,
            "command": shlex.join(public_argv),
            "status": self.status,
            "exit_code": self.exit_code,
            "created": self.created,
            "started": self.started,
            "ended": self.ended,
            "meta": redact_mapping(public_meta),
        }
        # Repository paths and log locations are disclosed only by dedicated,
        # authenticated preview flows when they are actually needed.
        return payload


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}

    def create(self, **kwargs) -> Session:
        """Build + register a session WITHOUT starting it (so the caller can use
        the assigned id, e.g. to name a log file, before exec)."""
        s = Session(**kwargs)
        self.sessions[s.id] = s
        return s

    def create_and_start(self, **kwargs) -> Session:
        s = self.create(**kwargs)
        s.start()
        return s

    def register(self, session: Session) -> Session:
        self.sessions[session.id] = session
        return session

    def get(self, sid: str) -> Optional[Session]:
        return self.sessions.get(sid)

    def list(self) -> list[Session]:
        return sorted(self.sessions.values(), key=lambda s: s.created, reverse=True)

    def active(self) -> list[Session]:
        return [s for s in self.sessions.values() if s.status == "running"]

    def shutdown(self) -> None:
        for s in list(self.sessions.values()):
            if s.status == "running":
                s.kill()
