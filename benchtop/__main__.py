# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Command-line launcher for the loopback-only OmicsANG application."""

from __future__ import annotations

import argparse
import errno
import os
import socket
import subprocess
import sys
from pathlib import Path

import uvicorn

from . import __version__, env_compat

_BROWSER_COMMANDS = (("/usr/bin/xdg-open",), ("/usr/bin/gio", "open"))


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
        help="show the one-time launch URL only on the controlling terminal",
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
    url: str,
    *,
    commands: tuple[tuple[str, ...], ...] = _BROWSER_COMMANDS,
) -> bool:
    """Launch a known Linux desktop opener without exposing its output streams."""
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
                [*command, url],
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
            # A still-running desktop handoff owns the request and all its streams
            # remain disconnected from OmicsANG.
            return True
        if status == 0:
            return True
    return False


def _deliver_launch_url(url: str, *, no_browser: bool) -> str:
    """Deliver the capability outside redirectable process output streams."""
    if not no_browser and _open_browser(url):
        return "browser"
    _write_launch_url_to_tty(url)
    return "terminal"


def _bind_listener(host: str, port: int) -> socket.socket:
    """Reserve the loopback listener before the application lifecycle starts."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family=family, type=socket.SOCK_STREAM)
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

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if not self.started:
            return
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
            print("  auth : one-time launch URL sent directly to the browser\n")
        else:
            print(
                "  auth : one-time launch URL shown only on the controlling terminal\n"
            )


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
