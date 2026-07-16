# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Security regression tests for OmicsANG's local control plane.

These tests deliberately avoid the owner's real repositories, credentials, and
external commands.  Starlette's TestClient has an optional ``httpx2`` runtime
dependency, so the middleware tests use a tiny direct ASGI harness instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import json
import os
import pty
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException, Response
from pydantic import ValidationError

from benchtop import __main__ as cli
from benchtop import (
    fleet,
    fs_policy,
    git_ops,
    provenance,
    runplan,
    security,
    server,
    settings,
    store,
)
from benchtop.sessions import EOF, Session, _child_environment


async def _inert_asgi_app(scope: dict, receive, send) -> None:
    """Return a harmless response after the real security middleware permits it."""
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 1000})
        return
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"ok":true}'})


def _http_request(
    app,
    *,
    method: str = "GET",
    path: str = "/api/private",
    host: str = "127.0.0.1:8787",
    origin: str = "",
    cookie: str = "",
    csrf: str = "",
    content_length: int | None = None,
) -> tuple[int, dict[str, str], bytes]:
    headers = [(b"host", host.encode("latin-1"))]
    for name, value in (
        ("origin", origin),
        ("cookie", cookie),
        (security.CSRF_HEADER, csrf),
    ):
        if value:
            headers.append((name.encode("latin-1"), value.encode("latin-1")))
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "server": ("127.0.0.1", 8787),
        "client": ("127.0.0.1", 12345),
    }
    messages: list[dict] = []
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    response_headers: dict[str, str] = {}
    for raw_name, raw_value in start.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        response_headers[name] = (
            f"{response_headers[name]}, {value}" if name in response_headers else value
        )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], response_headers, body


def _websocket_messages(
    app,
    *,
    host: str = "127.0.0.1:8787",
    origin: str = "http://127.0.0.1:8787",
    cookie: str = "",
) -> list[dict]:
    headers = [
        (b"host", host.encode("latin-1")),
        (b"origin", origin.encode("latin-1")),
    ]
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    scope = {
        "type": "websocket",
        "scheme": "ws",
        "path": "/api/sessions/example/ws",
        "raw_path": b"/api/sessions/example/ws",
        "query_string": b"",
        "headers": headers,
        "server": ("127.0.0.1", 8787),
        "client": ("127.0.0.1", 12345),
        "subprotocols": [],
    }
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    return messages


class AuthenticationSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = security.AuthState()
        self.middleware = security.SecurityMiddleware(_inert_asgi_app, auth=self.auth)
        bootstrap = self.auth.reset("b" * 48)
        self.assertEqual(bootstrap, "b" * 48)
        self.session = self.auth.exchange(bootstrap)
        self.assertIsNotNone(self.session)
        assert self.session is not None
        self.cookie = f"{security.AUTH_COOKIE}={self.session.token}"

    def test_unauthenticated_rest_is_rejected_and_sensitive_headers_are_set(
        self,
    ) -> None:
        status, headers, body = _http_request(self.middleware)
        self.assertEqual(status, 401)
        self.assertIn(b"authentication required", body)
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        self.assertIn(
            "frame-ancestors 'none'", headers.get("content-security-policy", "")
        )

    def test_malicious_host_is_rejected_before_authentication(self) -> None:
        for host in (
            "evil.example",
            "127.0.0.1.evil.example",
            "127.0.0.1@evil.example",
        ):
            with self.subTest(host=host):
                status, _, body = _http_request(
                    self.middleware,
                    host=host,
                    cookie=self.cookie,
                )
                self.assertEqual(status, 400)
                self.assertIn(b"invalid Host", body)

    def test_mutating_request_requires_exact_origin_and_csrf(self) -> None:
        cases = (
            ({}, 403),
            ({"origin": "http://evil.example", "csrf": self.session.csrf}, 403),
            ({"origin": "http://127.0.0.1:8787"}, 403),
            ({"origin": "http://127.0.0.1:8787", "csrf": "wrong"}, 403),
            ({"origin": "http://127.0.0.1:8787", "csrf": self.session.csrf}, 200),
        )
        for values, expected in cases:
            with self.subTest(values=values):
                status, _, _ = _http_request(
                    self.middleware,
                    method="POST",
                    cookie=self.cookie,
                    **values,
                )
                self.assertEqual(status, expected)

    def test_all_authenticated_api_requests_require_csrf_except_session_recovery(
        self,
    ) -> None:
        status, _, _ = _http_request(self.middleware, cookie=self.cookie)
        self.assertEqual(status, 403)
        status, _, _ = _http_request(
            self.middleware, cookie=self.cookie, csrf=self.session.csrf
        )
        self.assertEqual(status, 200)
        status, _, _ = _http_request(
            self.middleware,
            path="/api/auth/session",
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)

        request = SimpleNamespace(state=SimpleNamespace(benchtop_session=self.session))
        recovered = server.recover_browser_session(request)
        self.assertEqual(recovered["csrf"], self.session.csrf)
        self.assertNotIn(self.session.token, json.dumps(recovered))

    def test_unauthenticated_and_cross_origin_websockets_are_rejected(self) -> None:
        unauthenticated = _websocket_messages(self.middleware)
        self.assertEqual(unauthenticated[0]["type"], "websocket.close")
        self.assertEqual(unauthenticated[0]["code"], 4401)

        cross_origin = _websocket_messages(
            self.middleware,
            cookie=self.cookie,
            origin="http://evil.example",
        )
        self.assertEqual(cross_origin[0]["type"], "websocket.close")
        self.assertEqual(cross_origin[0]["code"], 4403)

        accepted = _websocket_messages(self.middleware, cookie=self.cookie)
        self.assertEqual(accepted[0]["type"], "websocket.accept")

    def test_oversized_request_is_rejected_before_dispatch(self) -> None:
        status, _, _ = _http_request(
            self.middleware,
            cookie=self.cookie,
            content_length=security.MAX_REQUEST_BYTES + 1,
        )
        self.assertEqual(status, 413)

    def test_bootstrap_is_one_time_and_never_echoed(self) -> None:
        state = security.AuthState()
        bootstrap = state.reset("launch-capability-" + "x" * 32)
        first = state.exchange(bootstrap)
        self.assertIsNotNone(first)
        self.assertIsNone(state.exchange(bootstrap))
        self.assertNotEqual(state.bootstrap_token, bootstrap)
        assert first is not None
        self.assertNotIn(bootstrap, first.token)
        self.assertNotIn(bootstrap, first.csrf)

        response = Response()
        with mock.patch.object(security, "AUTH_STATE", state):
            # Reset because the isolated state was consumed above.
            route_token = state.reset("route-capability-" + "y" * 32)
            payload = server.bootstrap_browser(
                server.BootstrapRequest(token=route_token),
                response,
            )
        self.assertEqual(set(payload), {"ok", "csrf", "expires"})
        serialized = json.dumps(payload)
        self.assertNotIn(route_token, serialized)
        cookie = response.headers.get("set-cookie", "")
        self.assertIn(f"{security.AUTH_COOKIE}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn(route_token, cookie)
        with self.assertRaises(HTTPException) as caught:
            with mock.patch.object(security, "AUTH_STATE", state):
                server.bootstrap_browser(
                    server.BootstrapRequest(token=route_token),
                    Response(),
                )
        self.assertEqual(caught.exception.status_code, 401)

    def test_only_supported_loopback_bind_addresses_are_accepted(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(host=host):
                self.assertEqual(settings.validate_bind_host(host), host)
        for host in ("", "0.0.0.0", "192.168.1.20", "8.8.8.8", "example.test"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    settings.validate_bind_host(host)

    def test_cli_rejects_unsupported_native_platforms_with_actionable_message(
        self,
    ) -> None:
        cli._require_supported_platform("linux")
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), self.assertRaises(SystemExit) as ctx:
                cli._require_supported_platform(platform)
            message = str(ctx.exception)
            self.assertIn("Linux only", message)
            self.assertIn("WSL", message)

    def test_cli_never_writes_bootstrap_capability_to_redirectable_streams(
        self,
    ) -> None:
        capability = "launch-capability-" + "z" * 48
        url = f"http://127.0.0.1:8787/#bootstrap={capability}"
        stdout = io.StringIO()
        stderr = io.StringIO()
        master_fd, terminal_fd = pty.openpty()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                cli._write_launch_url_to_tty(url, tty_path=os.ttyname(terminal_fd))
            terminal_text = os.read(master_fd, 4096).decode("utf-8")
        finally:
            os.close(master_fd)
            os.close(terminal_fd)

        self.assertIn(capability, terminal_text)
        self.assertNotIn(capability, stdout.getvalue())
        self.assertNotIn(capability, stderr.getvalue())

    def test_cli_delivers_capability_directly_to_browser_or_tty(self) -> None:
        url = "http://127.0.0.1:8787/#bootstrap=secret"
        with mock.patch.object(cli, "_open_browser", return_value=True) as browser:
            self.assertEqual(cli._deliver_launch_url(url, no_browser=False), "browser")
        browser.assert_called_once_with(url)

        with mock.patch.object(cli, "_write_launch_url_to_tty") as terminal:
            self.assertEqual(cli._deliver_launch_url(url, no_browser=True), "terminal")
        terminal.assert_called_once_with(url)

    def test_cli_browser_opener_is_fixed_silent_and_ignores_browser_env(self) -> None:
        capability = "browser-capability-" + "r" * 48
        url = f"http://127.0.0.1:8787/#bootstrap={capability}"
        with (
            mock.patch.dict(os.environ, {"BROWSER": "echo %s"}),
            mock.patch.object(cli.subprocess, "Popen") as launch,
        ):
            launch.return_value.wait.return_value = 0
            self.assertTrue(cli._open_browser(url, commands=(("/bin/true",),)))

        args, kwargs = launch.call_args
        self.assertEqual(args[0], ["/bin/true", url])
        self.assertNotIn("BROWSER", kwargs["env"])
        self.assertEqual(kwargs["env"]["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")
        self.assertIs(kwargs["stdin"], cli.subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], cli.subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], cli.subprocess.DEVNULL)
        self.assertTrue(kwargs["close_fds"])
        self.assertTrue(kwargs["start_new_session"])
        launch.return_value.wait.assert_called_once_with(timeout=2.0)

    def test_cli_real_opener_child_cannot_echo_capability_to_service_streams(
        self,
    ) -> None:
        capability = "real-child-capability-" + "e" * 48
        url = f"http://127.0.0.1:8787/#bootstrap={capability}"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"BROWSER": "echo %s"}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertTrue(cli._open_browser(url, commands=(("/bin/echo",),)))
        self.assertNotIn(capability, stdout.getvalue())
        self.assertNotIn(capability, stderr.getvalue())

    def test_cli_failed_browser_opener_falls_back_without_stream_echo(self) -> None:
        url = "http://127.0.0.1:8787/#bootstrap=fallback-secret"
        with (
            mock.patch.object(cli, "_open_browser", return_value=False),
            mock.patch.object(cli, "_write_launch_url_to_tty") as terminal,
        ):
            self.assertEqual(cli._deliver_launch_url(url, no_browser=False), "terminal")
        terminal.assert_called_once_with(url)

    def test_cli_refuses_insecure_no_tty_fallback_without_echoing_token(
        self,
    ) -> None:
        capability = "must-not-escape-" + "q" * 48
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                cli._write_launch_url_to_tty(
                    f"http://127.0.0.1/#bootstrap={capability}",
                    tty_path="/definitely/not/a/controlling/terminal",
                )
        self.assertNotIn(capability, str(caught.exception))
        self.assertNotIn(capability, stdout.getvalue())
        self.assertNotIn(capability, stderr.getvalue())

    def test_cli_delivers_only_after_its_server_has_started(self) -> None:
        url = "http://127.0.0.1:8787/#bootstrap=readiness-secret"
        config = cli.uvicorn.Config("benchtop.server:app")
        instance = cli._LaunchServer(config, launch_url=url, no_browser=True)
        delivery_observed_started = False

        async def mark_this_server_started(server, sockets=None) -> None:
            server.started = True

        def deliver(value: str, *, no_browser: bool) -> str:
            nonlocal delivery_observed_started
            delivery_observed_started = instance.started
            self.assertEqual(value, url)
            self.assertTrue(no_browser)
            return "terminal"

        with (
            mock.patch.object(
                cli.uvicorn.Server, "startup", new=mark_this_server_started
            ),
            mock.patch.object(cli, "_deliver_launch_url", side_effect=deliver),
        ):
            asyncio.run(instance.startup())

        self.assertTrue(delivery_observed_started)
        self.assertIsNone(instance.launch_error)

    def test_cli_delivery_failure_requests_clean_server_shutdown(self) -> None:
        config = cli.uvicorn.Config("benchtop.server:app")
        instance = cli._LaunchServer(
            config,
            launch_url="http://127.0.0.1/#bootstrap=never-echoed",
            no_browser=True,
        )

        async def mark_this_server_started(server, sockets=None) -> None:
            server.started = True

        shutdown_calls = 0

        async def record_shutdown(server, sockets=None) -> None:
            nonlocal shutdown_calls
            shutdown_calls += 1

        with (
            mock.patch.object(
                cli.uvicorn.Server, "startup", new=mark_this_server_started
            ),
            mock.patch.object(cli.uvicorn.Server, "shutdown", new=record_shutdown),
            mock.patch.object(
                cli, "_deliver_launch_url", side_effect=SystemExit("safe failure")
            ),
        ):
            asyncio.run(instance.startup())

        self.assertTrue(instance.should_exit)
        self.assertFalse(instance.started)
        self.assertEqual(shutdown_calls, 1)
        self.assertIsInstance(instance.launch_error, SystemExit)
        self.assertEqual(str(instance.launch_error), "safe failure")

    def test_cli_does_not_deliver_when_its_server_did_not_start(self) -> None:
        config = cli.uvicorn.Config("benchtop.server:app")
        instance = cli._LaunchServer(
            config,
            launch_url="http://127.0.0.1/#bootstrap=not-delivered",
            no_browser=False,
        )

        async def leave_server_unstarted(server, sockets=None) -> None:
            server.started = False

        with (
            mock.patch.object(
                cli.uvicorn.Server, "startup", new=leave_server_unstarted
            ),
            mock.patch.object(cli, "_deliver_launch_url") as deliver,
        ):
            asyncio.run(instance.startup())
        deliver.assert_not_called()

    def test_http_access_log_is_explicitly_opt_in(self) -> None:
        self.assertFalse(cli._parser().parse_args([]).access_log)
        self.assertTrue(cli._parser().parse_args(["--access-log"]).access_log)

        config = object()
        listener = mock.Mock()
        server = mock.Mock()
        server.launch_error = None
        with (
            mock.patch.object(
                cli.uvicorn, "Config", return_value=config
            ) as make_config,
            mock.patch.object(
                cli, "_bind_listener", return_value=listener
            ) as bind_listener,
            mock.patch.object(cli, "_preflight_state") as preflight_state,
            mock.patch.object(cli, "_LaunchServer", return_value=server) as make_server,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cli._run_server(
                host="127.0.0.1",
                port=8787,
                launch_url="http://127.0.0.1:8787/#bootstrap=not-logged",
                no_browser=True,
                access_log=False,
            )

        self.assertFalse(make_config.call_args.kwargs["access_log"])
        make_server.assert_called_once_with(
            config,
            launch_url="http://127.0.0.1:8787/#bootstrap=not-logged",
            no_browser=True,
        )
        bind_listener.assert_called_once_with("127.0.0.1", 8787)
        preflight_state.assert_called_once_with()
        server.run.assert_called_once_with(sockets=[listener])
        listener.close.assert_called_once_with()

    def test_cli_rejects_occupied_port_before_server_start(self) -> None:
        listener = mock.Mock()
        listener.bind.side_effect = OSError(errno.EADDRINUSE, "Address in use")
        with (
            mock.patch.object(cli.socket, "socket", return_value=listener),
            mock.patch.object(cli, "_preflight_state") as preflight_state,
            mock.patch.object(cli, "_LaunchServer") as make_server,
            self.assertRaises(SystemExit) as caught,
        ):
            cli._run_server(
                host="127.0.0.1",
                port=8787,
                launch_url="http://127.0.0.1:8787/#bootstrap=not-delivered",
                no_browser=True,
                access_log=False,
            )

        self.assertIn("127.0.0.1:8787 is already in use", str(caught.exception))
        self.assertIn("another --port", str(caught.exception))
        preflight_state.assert_not_called()
        make_server.assert_not_called()
        listener.set_inheritable.assert_not_called()
        listener.close.assert_called_once_with()

    def test_cli_reserved_listener_is_not_inherited_by_children(self) -> None:
        listener = mock.Mock()
        with mock.patch.object(cli.socket, "socket", return_value=listener) as create:
            reserved = cli._bind_listener("127.0.0.1", 8787)

        self.assertIs(reserved, listener)
        create.assert_called_once_with(
            family=cli.socket.AF_INET,
            type=cli.socket.SOCK_STREAM,
        )
        listener.bind.assert_called_once_with(("127.0.0.1", 8787))
        listener.set_inheritable.assert_called_once_with(False)
        listener.close.assert_not_called()

    def test_cli_state_root_mismatch_fails_before_server_start(self) -> None:
        listener = mock.Mock()
        mismatch = store.StateRootMismatch(
            "OmicsANG state belongs to a different pipeline root"
        )
        with (
            mock.patch.object(cli, "_bind_listener", return_value=listener),
            mock.patch.object(
                cli, "_preflight_state", side_effect=SystemExit(mismatch)
            ),
            mock.patch.object(cli, "_LaunchServer") as make_server,
            self.assertRaises(SystemExit) as caught,
        ):
            cli._run_server(
                host="127.0.0.1",
                port=8787,
                launch_url="http://127.0.0.1:8787/#bootstrap=not-delivered",
                no_browser=True,
                access_log=False,
            )

        self.assertIn("different pipeline root", str(caught.exception))
        make_server.assert_not_called()
        listener.close.assert_called_once_with()

    def test_cli_main_omits_bootstrap_capability_from_redirected_output(self) -> None:
        capability = "main-capability-" + "m" * 48
        stdout = io.StringIO()
        stderr = io.StringIO()
        security.AUTH_STATE.reset(capability)
        try:
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "benchtop",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8787",
                        "--no-browser",
                    ],
                ),
                mock.patch.object(cli, "_run_server", return_value=None) as run_server,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                cli.main()
        finally:
            security.AUTH_STATE.reset()

        delivered_url = run_server.call_args.kwargs["launch_url"]
        self.assertFalse(run_server.call_args.kwargs["access_log"])
        self.assertIn(capability, delivered_url)
        self.assertNotIn(capability, stdout.getvalue())
        self.assertNotIn(capability, stderr.getvalue())


class FilesystemPolicyTests(unittest.TestCase):
    def test_traversal_absolute_ambiguous_and_encoded_paths_are_rejected(self) -> None:
        hostile = (
            "../secret",
            "safe/../../secret",
            "/etc/passwd",
            "~/secret",
            "C:/Windows/System32/config",
            "safe\\..\\secret",
            "safe\x00name",
            "safe\nname",
            "%2e%2e/secret",
            "safe/%2E%2E/secret",
            "%2fetc/passwd",
            "safe%5c..%5csecret",
            "x" * (fs_policy.MAX_RELATIVE_PATH + 1),
        )
        for path in hostile:
            with self.subTest(path=repr(path)):
                with self.assertRaises(fs_policy.PathPolicyError):
                    fs_policy.clean_relative_path(path)

    def test_vcs_state_credentials_and_hidden_secrets_are_rejected(self) -> None:
        protected = (
            ".git/config",
            ".hg/store",
            ".env",
            ".env.production",
            ".benchtop/state.json",
            ".codex/session.json",
            ".ssh/id_ed25519",
            ".secret/notes.txt",
            "credentials.json",
            "keys/server.pem",
            "node_modules/package/index.js",
        )
        for path in protected:
            with self.subTest(path=path):
                with self.assertRaises(fs_policy.PathPolicyError):
                    fs_policy.clean_relative_path(path)
        self.assertEqual(fs_policy.clean_relative_path(".gitignore"), ".gitignore")
        self.assertEqual(
            fs_policy.clean_relative_path(".github/workflows/ci.yml"),
            ".github/workflows/ci.yml",
        )

    def test_symlink_hardlink_and_fifo_objects_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            secret = outside / "secret.txt"
            secret.write_text("secret", encoding="utf-8")

            (root / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(fs_policy.PathPolicyError):
                fs_policy.resolve_relative(root, "escape/secret.txt", must_exist=True)

            (root / "linked-secret.txt").symlink_to(secret)
            with self.assertRaises(fs_policy.PathPolicyError):
                fs_policy.resolve_relative(root, "linked-secret.txt", must_exist=True)

            ordinary = root / "ordinary.txt"
            ordinary.write_text("ordinary", encoding="utf-8")
            hardlink = root / "hardlink.txt"
            os.link(ordinary, hardlink)
            with self.assertRaises(fs_policy.PathPolicyError):
                fs_policy.resolve_relative(
                    root,
                    "hardlink.txt",
                    must_exist=True,
                    expected="file",
                )

            if hasattr(os, "mkfifo"):
                fifo = root / "named-pipe"
                os.mkfifo(fifo)
                with self.assertRaises(fs_policy.PathPolicyError):
                    fs_policy.resolve_relative(
                        root,
                        "named-pipe",
                        must_exist=True,
                        expected="file",
                    )


class SessionAndAgentSecurityTests(unittest.TestCase):
    def test_agent_environment_is_provider_specific_and_fail_closed(self) -> None:
        parent = {
            "PATH": "/controlled/bin",
            "HOME": "/controlled/home",
            "LANG": "C.UTF-8",
            "HTTPS_PROXY": "http://proxy.invalid",
            "SSL_CERT_FILE": "/controlled/ca.pem",
            "OPENAI_API_KEY": "codex-credential",
            "OPENAI_PROJECT_ID": "codex-project",
            "ANTHROPIC_API_KEY": "claude-credential",
            "CLAUDE_CONFIG_DIR": "/controlled/claude",
            "GITHUB_TOKEN": "unrelated-token",
            "SSH_AUTH_SOCK": "/controlled/agent.sock",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "AZURE_CLIENT_SECRET": "azure-secret",
            "GOOGLE_APPLICATION_CREDENTIALS": "/controlled/google.json",
            "KUBECONFIG": "/controlled/kubeconfig",
            "UNRELATED_SECRET": "server-secret",
        }

        codex = _child_environment(
            "agent",
            parent,
            {"OPENAI_API_KEY": "explicit-codex", "UNRELATED_SECRET": "override"},
            tool="codex",
        )
        claude = _child_environment("agent", parent, tool="claude")
        shell = _child_environment("agent", parent, tool="shell")

        self.assertEqual(codex["OPENAI_API_KEY"], "explicit-codex")
        self.assertEqual(codex["OPENAI_PROJECT_ID"], "codex-project")
        self.assertNotIn("ANTHROPIC_API_KEY", codex)
        self.assertEqual(claude["ANTHROPIC_API_KEY"], "claude-credential")
        self.assertEqual(claude["CLAUDE_CONFIG_DIR"], "/controlled/claude")
        self.assertNotIn("OPENAI_API_KEY", claude)
        self.assertNotIn("OPENAI_API_KEY", shell)
        self.assertNotIn("ANTHROPIC_API_KEY", shell)
        for child in (codex, claude, shell):
            self.assertEqual(child["PATH"], "/controlled/bin")
            self.assertEqual(child["HTTPS_PROXY"], "http://proxy.invalid")
            self.assertEqual(child["SSL_CERT_FILE"], "/controlled/ca.pem")
            for forbidden in (
                "GITHUB_TOKEN",
                "SSH_AUTH_SOCK",
                "AWS_SECRET_ACCESS_KEY",
                "AZURE_CLIENT_SECRET",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "KUBECONFIG",
                "UNRELATED_SECRET",
            ):
                self.assertNotIn(forbidden, child)

        run = _child_environment(
            "run",
            parent,
            {"UNRELATED_SECRET": "explicit-run-secret"},
            tool="codex",
        )
        self.assertEqual(run["SSH_AUTH_SOCK"], "/controlled/agent.sock")
        self.assertEqual(run["UNRELATED_SECRET"], "explicit-run-secret")

    def test_fleet_prompt_is_forgotten_after_agent_dispatch(self) -> None:
        secret = "private cohort instruction that must not remain resident"
        job = fleet.new_job(secret, "codex", True)
        try:
            self.assertRegex(job["prompt_digest"], r"\Asha256:[0-9a-f]{64}\Z")
            self.assertEqual(job["prompt_length"], len(secret.encode("utf-8")))
            self.assertEqual(job["prompt_preview"], "[prompt omitted]")
            self.assertNotIn("prompt", job)
            self.assertNotIn(secret, json.dumps(job))

            fleet.add_target(
                job,
                pipeline="demo",
                session_id="agent-session",
                error=None,
            )
            self.assertNotIn("prompt", job)
            self.assertNotIn(secret, json.dumps(job))

            with mock.patch("benchtop.fleet.store.record") as record:
                fleet.persist(job)
            persisted = record.call_args.args[0]
            self.assertEqual(persisted["prompt_digest"], job["prompt_digest"])
            self.assertEqual(persisted["prompt_preview"], "[prompt omitted]")
            self.assertNotIn(secret, json.dumps(persisted))
        finally:
            fleet._JOBS.pop(job["id"], None)

    def test_session_ids_are_full_entropy_and_agent_prompts_are_not_serialized(
        self,
    ) -> None:
        secret_prompt = "PRIVATE AGENT PROMPT: patient cohort 123"
        sessions = [
            Session(
                kind="agent",
                title="agent",
                cwd="/tmp/inert-repository",
                argv=["codex", secret_prompt],
                meta={"tool": "codex", "prompt": secret_prompt},
            )
            for _ in range(32)
        ]
        self.assertEqual(len({item.id for item in sessions}), len(sessions))
        for item in sessions:
            self.assertGreaterEqual(len(item.id), 32)
            self.assertGreaterEqual(len(set(item.id)), 8)
            self.assertNotIn(secret_prompt, json.dumps(item.to_dict()))

    def test_agent_prompt_and_provider_output_are_never_durably_logged(self) -> None:
        secret_prompt = "CONTROLLED PRIVATE PROMPT with multiple words"
        with tempfile.TemporaryDirectory() as tmp:
            logfile = Path(tmp) / "agent.log"

            async def exercise() -> tuple[Session, bytes]:
                session = Session(
                    kind="agent",
                    title="prompt log test",
                    cwd=tmp,
                    argv=[
                        sys.executable,
                        "-c",
                        (
                            "import sys; value=sys.stdin.readline(); "
                            "print('provider-rendered:', value.rstrip(), flush=True)"
                        ),
                    ],
                    logfile=str(logfile),
                )
                queue, snapshot = session.subscribe()
                session.start()
                session.write_initial_input(secret_prompt.encode() + b"\n")
                deadline = time.monotonic() + 5.0
                while session.ended is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                if session.ended is None:
                    await session.cancel(interrupt_grace=0.1, terminate_grace=0.1)
                    self.fail("controlled prompt consumer did not exit")
                observed = [snapshot]
                while True:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                    if item is EOF:
                        break
                    observed.append(item)
                session.unsubscribe(queue)
                return session, b"".join(observed)

            session, live_output = asyncio.run(exercise())

        self.assertEqual(session.exit_code, 0)
        self.assertIsNone(session.logfile)
        self.assertFalse(logfile.exists())
        # A connected viewer receives the final output before EOF, but an ended
        # agent session cannot replay provider output to a later attachment.
        self.assertIn(b"provider-rendered:", live_output)
        self.assertEqual(session._buffer, bytearray())
        ended_queue, ended_snapshot = session.subscribe()
        self.assertEqual(ended_snapshot, b"")
        self.assertIs(ended_queue.get_nowait(), EOF)
        session.unsubscribe(ended_queue)

    def test_initial_input_fails_closed_when_pty_echo_cannot_be_suppressed(
        self,
    ) -> None:
        session = Session(
            kind="agent",
            title="inert",
            cwd="/tmp",
            argv=["true"],
        )
        session.status = "running"
        session.fd = 123
        with (
            mock.patch(
                "benchtop.sessions.termios.tcgetattr",
                side_effect=OSError("controlled failure"),
            ),
            mock.patch("benchtop.sessions.os.write") as write,
        ):
            with self.assertRaisesRegex(RuntimeError, "suppress PTY echo"):
                session.write_initial_input(b"controlled prompt\n")
        write.assert_not_called()

    def test_browser_cannot_supply_an_arbitrary_agent_cwd(self) -> None:
        with self.assertRaises(ValidationError):
            server.AgentRequest.model_validate(
                {
                    "pipeline": "demo",
                    "tool": "codex",
                    "prompt": "inspect this repository",
                    "cwd": "/tmp/unregistered-checkout",
                    "acknowledge_external_agent": True,
                }
            )

    def test_external_agent_requires_explicit_acknowledgement(self) -> None:
        pipeline = SimpleNamespace(name="demo", path=Path("/tmp/inert-repository"))
        body = server.AgentRequest(
            pipeline="demo",
            tool="codex",
            prompt="inspect",
            acknowledge_external_agent=False,
        )
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=pipeline),
            mock.patch("benchtop.server._spawn", new_callable=mock.AsyncMock) as spawn,
        ):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(server.start_agent(body))
        self.assertEqual(caught.exception.status_code, 428)
        spawn.assert_not_awaited()

    def test_local_shell_also_requires_explicit_acknowledgement(self) -> None:
        pipeline = SimpleNamespace(name="demo", path=Path("/tmp/inert-repository"))
        body = server.AgentRequest(
            pipeline="demo",
            tool="shell",
            acknowledge_external_agent=False,
        )
        with (
            mock.patch("benchtop.server._require_pipeline", return_value=pipeline),
            mock.patch("benchtop.server._spawn", new_callable=mock.AsyncMock) as spawn,
        ):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(server.start_agent(body))
        self.assertEqual(caught.exception.status_code, 428)
        spawn.assert_not_awaited()

    def test_fleet_requires_acknowledgement_before_any_side_effect(self) -> None:
        body = server.FleetRequest(
            prompt="inspect",
            tool="codex",
            pipelines=["demo"],
            use_worktree=True,
            acknowledge_external_agent=False,
        )
        with (
            mock.patch("benchtop.server.git_ops.add_worktree") as add_worktree,
            mock.patch("benchtop.server._spawn", new_callable=mock.AsyncMock) as spawn,
        ):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(server.fleet_launch(body))
        self.assertEqual(caught.exception.status_code, 428)
        add_worktree.assert_not_called()
        spawn.assert_not_awaited()

    def test_fleet_worktree_failure_never_falls_back_to_main_checkout(self) -> None:
        pipeline = SimpleNamespace(name="demo", path=Path("/tmp/inert-repository"))
        body = server.FleetRequest(
            prompt="inspect",
            tool="codex",
            pipelines=["demo"],
            use_worktree=True,
            acknowledge_external_agent=True,
        )
        with (
            mock.patch("benchtop.server.pipelines.get", return_value=pipeline),
            mock.patch(
                "benchtop.server.git_ops.add_worktree",
                return_value={"ok": False, "output": "controlled failure"},
            ),
            mock.patch("benchtop.server.fleet.persist"),
            mock.patch("benchtop.server._spawn", new_callable=mock.AsyncMock) as spawn,
        ):
            result = asyncio.run(server.fleet_launch(body))
        spawn.assert_not_awaited()
        self.assertEqual(result["prompt"], "[prompt omitted]")
        self.assertIn("no agent", result["targets"][0]["error"])

    def test_debug_agent_defaults_to_nonexecuting_exact_preview(self) -> None:
        log_path = "/tmp/inert-repository/run.log"
        source = Session(
            kind="run",
            title="failed run",
            cwd="/tmp/inert-repository",
            argv=["snakemake", "--dry-run"],
            meta={"pipeline": "demo", "human": "snakemake --dry-run"},
            logfile=log_path,
        )
        source.status = "failed"
        source.exit_code = 1
        pipeline = SimpleNamespace(name="demo", path=Path("/tmp/inert-repository"))
        with (
            mock.patch.object(server.mgr, "get", return_value=source),
            mock.patch("benchtop.server._require_pipeline", return_value=pipeline),
            mock.patch("benchtop.server._spawn", new_callable=mock.AsyncMock) as spawn,
        ):
            preview = asyncio.run(
                server.debug_run(server.DebugRequest(session_id=source.id))
            )
        spawn.assert_not_awaited()
        serialized = json.dumps(preview)
        self.assertIn(log_path, serialized)
        self.assertIn("snakemake --dry-run", serialized)
        self.assertNotIn('"session"', serialized)

    def test_debug_agent_transmits_exactly_the_sanitized_preview_prompt(self) -> None:
        secret = "multi word credential value"
        source = Session(
            kind="run",
            title="failed run",
            cwd="/tmp/inert-repository",
            argv=["snakemake", "--token", secret, "ordinary-target"],
            meta={"pipeline": "demo", "human": f"snakemake --token '{secret}'"},
            logfile="/tmp/inert-repository/run.log",
        )
        source.status = "failed"
        source.exit_code = 1
        pipeline = SimpleNamespace(name="demo", path=Path("/tmp/inert-repository"))
        launched = Session(
            kind="agent",
            title="debug",
            cwd=str(pipeline.path),
            argv=["claude"],
            meta={"pipeline": "demo", "tool": "claude"},
        )
        with (
            mock.patch.object(server.mgr, "get", return_value=source),
            mock.patch("benchtop.server._require_pipeline", return_value=pipeline),
            mock.patch("benchtop.server.shutil.which", return_value="/usr/bin/claude"),
            mock.patch(
                "benchtop.server._spawn",
                new_callable=mock.AsyncMock,
                return_value=launched,
            ) as spawn,
        ):
            preview = asyncio.run(
                server.debug_run(server.DebugRequest(session_id=source.id))
            )["preview"]
            asyncio.run(
                server.debug_run(
                    server.DebugRequest(
                        session_id=source.id,
                        preview_only=False,
                        acknowledge_external_agent=True,
                    )
                )
            )

        transmitted = spawn.await_args.kwargs["initial_input"]
        self.assertEqual(transmitted.encode("utf-8"), preview["prompt"].encode("utf-8"))
        self.assertNotIn(secret, transmitted)
        self.assertIn("ordinary-target", transmitted)
        self.assertIn("<redacted>", transmitted)

    def test_ordinary_api_projections_omit_private_state_and_log_paths(self) -> None:
        private_root = "/tmp/private-benchtop-state"
        job = {
            "id": "job-1",
            "command": "snakemake --token 'multi word secret' ordinary-target",
            "logfile": f"{private_root}/sessions/job-1.log",
            "session": {
                "id": "job-1",
                "cwd": "/tmp/repository",
                "logfile": f"{private_root}/sessions/job-1.log",
                "argv": ["snakemake", "--token", "multi word secret"],
                "meta": {"plan_record": f"{private_root}/job.runplan.json"},
            },
            "slurm_job": {
                "id": "slurm-1",
                "command": "snakemake --api-key 'another secret'",
                "script": f"{private_root}/slurm/job.sbatch",
                "plan_record": f"{private_root}/slurm/job.runplan.json",
                "output": f"{private_root}/slurm/job-%j.out",
                "error": f"{private_root}/slurm/job-%j.err",
            },
        }

        public_job = server._public_job(job)
        public_history = server._public_history_entry(
            {**job, "session": {}, "slurm_job": {}}
        )
        database = server._public_database_status(
            {
                "path": f"{private_root}/state.sqlite3",
                "journal_mode": "wal",
                "schema_version": 4,
                "migrations": [{"version": 4}],
                "legacy_import": {"path": f"{private_root}/state.json"},
            }
        )
        serialized = json.dumps(
            {"job": public_job, "history": public_history, "database": database}
        )

        self.assertNotIn(private_root, serialized)
        self.assertNotIn("multi word secret", serialized)
        self.assertNotIn("another secret", serialized)
        self.assertEqual(public_job["slurm_job"]["output"], "job-%j.out")
        self.assertNotIn("path", database)

    def test_workspace_identifier_is_stable_opaque_and_root_specific(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            equivalent = root / ".." / root.name
            workspace_id = server._workspace_identifier(root)

            self.assertEqual(workspace_id, server._workspace_identifier(equivalent))
            self.assertEqual(len(workspace_id), 64)
            self.assertRegex(workspace_id, r"\A[0-9a-f]{64}\Z")
            self.assertNotIn(str(root), workspace_id)
            self.assertNotEqual(
                workspace_id,
                server._workspace_identifier(Path(tmp) / "another-workspace"),
            )

    def test_debug_agent_execution_requires_acknowledgement(self) -> None:
        source = Session(
            kind="run",
            title="failed run",
            cwd="/tmp/inert-repository",
            argv=["snakemake", "--dry-run"],
            meta={"pipeline": "demo"},
        )
        pipeline = SimpleNamespace(name="demo", path=Path("/tmp/inert-repository"))
        body = server.DebugRequest(
            session_id=source.id,
            preview_only=False,
            acknowledge_external_agent=False,
        )
        with (
            mock.patch.object(server.mgr, "get", return_value=source),
            mock.patch("benchtop.server._require_pipeline", return_value=pipeline),
            mock.patch("benchtop.server._spawn", new_callable=mock.AsyncMock) as spawn,
        ):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(server.debug_run(body))
        self.assertEqual(caught.exception.status_code, 428)
        spawn.assert_not_awaited()


class ActiveContentSecurityTests(unittest.TestCase):
    def test_html_and_svg_are_forced_to_attachment_octet_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixtures = {
                "report.html": "<script>top.location='https://evil.example'</script>",
                "figure.svg": "<svg onload=alert(1)></svg>",
                "document.xhtml": "<html xmlns='http://www.w3.org/1999/xhtml'/>",
            }
            for name, content in fixtures.items():
                with self.subTest(name=name):
                    path = root / name
                    path.write_text(content, encoding="utf-8")
                    response = server._authorized_file_response(path)
                    disposition = response.headers.get("content-disposition", "")
                    self.assertTrue(disposition.lower().startswith("attachment;"))
                    self.assertIn(name, disposition)
                    self.assertEqual(response.media_type, "application/octet-stream")
                    self.assertEqual(
                        response.headers.get("content-type"),
                        "application/octet-stream",
                    )


class GitSafetyTests(unittest.TestCase):
    @staticmethod
    def _assert_disabled(call) -> None:
        try:
            result = call()
        except HTTPException as exc:
            if exc.status_code not in {403, 404, 405, 501, 503}:
                raise
            return
        if not isinstance(result, dict) or result.get("ok") is not False:
            raise AssertionError(f"outbound Git endpoint was not disabled: {result!r}")
        detail = " ".join(str(value) for value in result.values()).casefold()
        if not any(
            word in detail for word in ("disabled", "unavailable", "not enabled")
        ):
            raise AssertionError(f"disabled response lacks a clear reason: {result!r}")

    def test_outbound_git_endpoints_do_not_dispatch(self) -> None:
        pipeline = SimpleNamespace(name="demo", path=Path("/tmp/inert-repository"))
        endpoints = (
            ("fetch", lambda: server.pipeline_github_fetch("demo"), "fetch"),
            ("pull", lambda: server.pipeline_github_pull("demo"), "pull_ff_only"),
            (
                "push",
                lambda: server.pipeline_github_push(
                    "demo",
                    server.GitHubPushRequest(branch="main"),
                ),
                "push",
            ),
            (
                "pr",
                lambda: server.pipeline_github_pr(
                    "demo",
                    server.GitHubPRRequest(title="Do not publish"),
                ),
                "open_pr",
            ),
            (
                "repo",
                lambda: server.pipeline_github_create_repo(
                    "demo",
                    server.GitHubRepoCreateRequest(full_name="owner/repo"),
                ),
                "create_github_repo",
            ),
            (
                "connect",
                lambda: server.pipeline_github_connect_repo(
                    "demo",
                    server.GitHubRepoConnectRequest(full_name="owner/repo"),
                ),
                "connect_github_repo",
            ),
        )
        for label, invoke, helper in endpoints:
            with self.subTest(endpoint=label):
                with (
                    mock.patch(
                        "benchtop.server._require_pipeline", return_value=pipeline
                    ),
                    mock.patch(
                        f"benchtop.server.git_ops.{helper}",
                        side_effect=AssertionError("outbound helper was dispatched"),
                    ),
                ):
                    self._assert_disabled(invoke)

    def test_worktree_rejects_option_like_and_invalid_refs_before_dispatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            def validation_only(args: list[str], cwd: Path, timeout: int = 30):
                if args[:3] == ["git", "check-ref-format", "--branch"]:
                    return 1, "invalid ref"
                raise AssertionError(f"unsafe Git operation was dispatched: {args!r}")

            cases = (
                ("--force", "HEAD"),
                ("-c", "HEAD"),
                ("bad ref", "HEAD"),
                ("../../escape", "HEAD"),
                ("benchtop/safe", "--detach"),
            )
            for branch, base in cases:
                with self.subTest(branch=branch, base=base):
                    with mock.patch(
                        "benchtop.git_ops._run", side_effect=validation_only
                    ):
                        try:
                            result = git_ops.add_worktree(repo, branch, base)
                        except (ValueError, HTTPException):
                            continue
                    self.assertIsInstance(result, dict)
                    self.assertFalse(result.get("ok"), result)

    def test_git_staging_never_uses_add_all(self) -> None:
        calls: list[list[str]] = []

        def inert_run(args: list[str], cwd: Path, timeout: int = 30):
            calls.append(list(args))
            return 1, "operation disabled in security test"

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("benchtop.git_ops._run", side_effect=inert_run):
                try:
                    git_ops.commit_all(Path(tmp), "inert test commit")
                except (ValueError, HTTPException, RuntimeError):
                    pass
        self.assertNotIn(["git", "add", "-A"], calls)

    def test_scp_remote_and_common_access_key_names_are_redacted(self) -> None:
        remote = "oauth-secret-value@github.com:owner/repo.git"
        self.assertNotIn("oauth-secret-value", runplan.redact_text(remote))
        self.assertNotIn("oauth-secret-value", provenance._sanitize_remote(remote))
        redacted = runplan.redact_mapping(
            {"AWS_ACCESS_KEY_ID": "controlled-example-value"}
        )
        self.assertEqual(redacted["AWS_ACCESS_KEY_ID"], "<redacted>")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
