# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Command-line launcher for the loopback-only OmicsANG application."""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path

import uvicorn

from . import __version__, env_compat

_BROWSER_COMMANDS = (("/usr/bin/xdg-open",), ("/usr/bin/gio", "open"))
CONTROL_SOCKET_NAME = "control.sock"


def _require_supported_platform(platform: str | None = None) -> None:
    """Fail before importing POSIX-only server modules on unsupported systems."""
    current = platform or sys.platform
    if current != "linux":
        raise SystemExit(
            "error: OmicsANG currently supports Linux only. "
            "On Windows, run OmicsANG inside WSL; native Windows and macOS "
            "are not supported in this release."
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omicsang",
        description="Run OmicsANG for trusted local pipelines on loopback only.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("open",),
        help=(
            "open: ask the OmicsANG already running on this --state for a fresh "
            "one-time launch URL, for a new browser tab or after closing the last one"
        ),
    )
    parser.add_argument(
        "--root", type=Path, help="directory containing pipeline repositories"
    )
    parser.add_argument("--state", type=Path, help="private OmicsANG state directory")
    # Resolve environment fallbacks only after parsing.  Explicit command-line
    # values must be able to override conflicting or malformed legacy settings.
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help=(
            "do not open the sign-in page in a browser; the one-time launch URL "
            "is shown only on the controlling terminal either way"
        ),
    )
    parser.add_argument(
        "--access-log",
        action="store_true",
        help=(
            "log HTTP request lines (off by default because URLs can contain "
            "local paths or search terms)"
        ),
    )
    parser.add_argument(
        "--clear-state",
        action="store_true",
        help="delete the configured state directory and exit",
    )
    parser.add_argument("--yes", action="store_true", help="confirm --clear-state")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI values, consulting environment fallbacks only when needed."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.host is None:
            args.host = str(env_compat.environment_value("HOST", "127.0.0.1"))
        if args.port is None:
            args.port = int(str(env_compat.environment_value("PORT", "8787")))
    except (env_compat.EnvironmentConflict, ValueError) as exc:
        parser.error(str(exc))
    return args


def _write_launch_url_to_tty(url: str, *, tty_path: str = "/dev/tty") -> None:
    """Write a capability URL to the controlling terminal, never stdout/stderr."""
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(tty_path, flags)
    except OSError as exc:
        raise SystemExit(
            "error: a controlling terminal is required to display the one-time "
            "launch URL safely; rerun OmicsANG from an interactive terminal"
        ) from exc
    if not os.isatty(fd):
        os.close(fd)
        raise SystemExit(
            "error: the launch capability can only be displayed on a controlling "
            "terminal; OmicsANG was not started"
        )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as terminal:
            terminal.write(f"  one-time launch URL: {url}\n\n")
            terminal.flush()
    except OSError as exc:
        raise SystemExit(
            "error: could not display the one-time launch URL on the controlling "
            "terminal; OmicsANG was not started"
        ) from exc


def _open_browser(
    page_url: str,
    *,
    commands: tuple[tuple[str, ...], ...] = _BROWSER_COMMANDS,
) -> bool:
    """Open the sign-in page, which carries no secret, in the desktop browser.

    A command-line argument is readable by every local account through
    ``/proc/<pid>/cmdline``, so anything with a fragment, query, or user
    information is refused outright: only the bare address may be passed.
    """
    if any(mark in page_url for mark in "#?@") or not page_url.startswith("http://"):
        raise ValueError("only the bare local address may be given to a browser")
    environment = os.environ.copy()
    # Do not let Python or desktop helpers honor a caller-supplied browser shell.
    environment.pop("BROWSER", None)
    environment["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    for command in commands:
        executable = Path(command[0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            continue
        try:
            process = subprocess.Popen(  # noqa: S603 - absolute executable allowlist
                [*command, page_url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                env=environment,
            )
        except OSError:
            continue
        try:
            status = process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            return True
        if status == 0:
            return True
    return False


def _deliver_launch_url(url: str, *, no_browser: bool = True) -> str:
    """Show the capability on the controlling terminal, and only there.

    Unless ``no_browser`` is set the browser is also opened at the bare address,
    where the page asks for the launch code shown on the terminal.
    """
    _write_launch_url_to_tty(url)
    if not no_browser and _open_browser(url.partition("#")[0]):
        return "browser"
    return "terminal"


def _control_socket_path(state_dir: Path) -> Path:
    return state_dir / CONTROL_SOCKET_NAME


def _peer_is_this_user(writer: asyncio.StreamWriter) -> bool:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return False
    try:
        raw = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
    except OSError:
        return False
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid == os.getuid()


async def _start_control_server(state_dir: Path, base_url: str):
    """Listen on a private socket that hands out fresh launch URLs.

    The socket lives in the 0700 state directory and the peer's uid is checked
    as well, so only this account can ask.  Contained agents cannot reach it:
    the state directory is not mounted for them and they cannot create
    unix-domain sockets.
    """
    from . import security

    path = _control_socket_path(state_dir)

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if _peer_is_this_user(writer) and line.strip() == b"launch-url":
                code = security.AUTH_STATE.issue_launch_code()
                reply = {"url": f"{base_url}#bootstrap={code}"}
            else:
                reply = {"error": "request refused"}
            writer.write(json.dumps(reply).encode("utf-8") + b"\n")
            await writer.drain()
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()

    try:
        path.unlink(missing_ok=True)
        previous = os.umask(0o177)
        try:
            server = await asyncio.start_unix_server(handle, path=str(path))
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)
    except (OSError, ValueError):
        # For example a state path too long for a socket address.  The server
        # still works; only `omicsang open` is unavailable.
        return None
    return server


def _request_launch_url(state_dir: Path) -> str:
    """Ask the running server for a new one-time launch URL."""
    path = _control_socket_path(state_dir)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5.0)
    try:
        client.connect(str(path))
        client.sendall(b"launch-url\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        raise SystemExit(
            f"error: no running OmicsANG answered for state {state_dir} "
            f"({exc.strerror or exc}); start it first, or pass the same --state"
        ) from None
    finally:
        client.close()
    try:
        url = json.loads(data.decode("utf-8")).get("url")
    except (ValueError, AttributeError):
        url = None
    if not isinstance(url, str) or "#bootstrap=" not in url:
        raise SystemExit("error: the running OmicsANG refused to issue a launch URL")
    return url


def _bind_listener(host: str, port: int) -> socket.socket:
    """Reserve the loopback listener before the application lifecycle starts."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    # Name TCP explicitly: asyncio only sets TCP_NODELAY on accepted sockets
    # whose protocol is IPPROTO_TCP, and without it Nagle plus delayed ACK adds
    # about 40 ms to every keep-alive response.
    listener = socket.socket(
        family=family, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
    except OSError as exc:
        listener.close()
        if exc.errno == errno.EADDRINUSE:
            raise SystemExit(
                f"error: {host}:{port} is already in use; OmicsANG may already "
                "be running. Stop the existing process or choose another --port."
            ) from None
        detail = exc.strerror or str(exc)
        raise SystemExit(f"error: could not bind {host}:{port}: {detail}") from None
    # The server runs in this process. Keep the listener out of PTY/agent child
    # execs so a child cannot accidentally keep the port occupied after exit.
    listener.set_inheritable(False)
    return listener


def _preflight_state() -> None:
    """Initialize durable state before importing or starting the web app."""
    from . import settings, store

    try:
        store.initialize()
        settings.prune_old_logs()
    except store.StateRootMismatch as exc:
        raise SystemExit(f"error: {exc}") from None
    except (store.StateError, ValueError) as exc:
        raise SystemExit(f"error: OmicsANG state is unavailable: {exc}") from None


class _LaunchServer(uvicorn.Server):
    """Uvicorn server that releases the capability only after its own bind."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        launch_url: str,
        no_browser: bool,
    ) -> None:
        super().__init__(config)
        self.launch_url = launch_url
        self.no_browser = no_browser
        self.launch_error: SystemExit | None = None
        self.control_server = None

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if not self.started:
            return
        from . import settings

        self.control_server = await _start_control_server(
            settings.STATE_DIR, self.launch_url.partition("#")[0]
        )
        try:
            delivery = _deliver_launch_url(self.launch_url, no_browser=self.no_browser)
        except SystemExit as exc:
            # Uvicorn 0.29 returns early when startup sets should_exit, while newer
            # versions conditionally shut down if ``started`` remains true. Perform
            # the lifespan/socket cleanup here and clear the flag to make it exactly
            # once across the supported range.
            self.launch_error = exc
            self.should_exit = True
            await self.shutdown(sockets=sockets)
            self.started = False
            return
        if delivery == "browser":
            print(
                "  auth : paste the launch URL shown on this terminal into the "
                "sign-in page that just opened, or open that URL directly"
            )
        else:
            print(
                "  auth : open the one-time launch URL shown on this terminal in "
                "a browser on this machine"
            )
        if self.control_server is not None:
            print("  later: `omicsang open` prints a fresh launch URL\n")

    async def shutdown(self, sockets=None) -> None:
        if self.control_server is not None:
            from . import settings

            self.control_server.close()
            self.control_server = None
            _control_socket_path(settings.STATE_DIR).unlink(missing_ok=True)
        await super().shutdown(sockets=sockets)


def _run_server(
    *,
    host: str,
    port: int,
    launch_url: str,
    no_browser: bool,
    access_log: bool,
) -> None:
    config = uvicorn.Config(
        "benchtop.server:app",
        host=host,
        port=port,
        log_level="info",
        ws_ping_interval=20,
        proxy_headers=False,
        server_header=False,
        access_log=access_log,
    )
    listener = _bind_listener(host, port)
    try:
        _preflight_state()
        from . import settings

        base_url = launch_url.partition("#")[0]
        print("\n  OmicsANG — bioinformatics mission control")
        print(f"  root : {settings.ROOT}")
        print(f"  state: {settings.STATE_DIR}")
        print(f"  url  : {base_url}")
        server = _LaunchServer(
            config,
            launch_url=launch_url,
            no_browser=no_browser,
        )
        server.run(sockets=[listener])
        if server.launch_error is not None:
            raise server.launch_error
    finally:
        listener.close()


def main() -> None:
    args = _parse_args()
    _require_supported_platform()
    if args.root:
        os.environ["OMICSANG_ROOT"] = str(args.root.expanduser().resolve())
        os.environ.pop("BENCHTOP_ROOT", None)
    if args.state:
        os.environ["OMICSANG_STATE"] = str(args.state.expanduser().resolve())
        os.environ.pop("BENCHTOP_STATE", None)
    # Parsed CLI values outrank either environment spelling for this process.
    os.environ["OMICSANG_HOST"] = args.host
    os.environ["OMICSANG_PORT"] = str(args.port)
    os.environ.pop("BENCHTOP_HOST", None)
    os.environ.pop("BENCHTOP_PORT", None)

    # Import only after command-line overrides are in the environment.
    try:
        from . import security, settings
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc

    try:
        host = settings.validate_bind_host(args.host)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    if not 1 <= args.port <= 65535:
        raise SystemExit("error: port must be between 1 and 65535")
    if args.clear_state:
        if not args.yes:
            raise SystemExit("error: --clear-state requires --yes")
        settings.clear_state(confirmed=True)
        print(f"Cleared OmicsANG state: {settings.STATE_DIR}")
        return

    if args.command == "open":
        _write_launch_url_to_tty(_request_launch_url(settings.STATE_DIR))
        print("One-time launch URL written to this terminal; it expires in 5 minutes.")
        return

    display_host = f"[{host}]" if ":" in host else host
    base_url = f"http://{display_host}:{args.port}/"
    url = f"{base_url}#bootstrap={security.AUTH_STATE.bootstrap_token}"
    _run_server(
        host=host,
        port=args.port,
        launch_url=url,
        no_browser=args.no_browser,
        access_log=args.access_log,
    )


if __name__ == "__main__":
    main()
