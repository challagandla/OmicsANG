# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: MIT
"""Per-process browser authentication and local-origin enforcement.

OmicsANG is intentionally a loopback-only, trusted-user application.  This
module does not turn pipeline execution into a sandbox; it prevents unrelated
web pages and unauthenticated local processes from silently driving the browser
control plane.
"""

from __future__ import annotations

import ipaddress
import json
import secrets
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Awaitable, Callable

AUTH_COOKIE = "benchtop_session"
CSRF_HEADER = "x-benchtop-csrf"
MAX_REQUEST_BYTES = 2_000_000
SESSION_TTL_SECONDS = 12 * 60 * 60
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class BrowserSession:
    token: str
    csrf: str
    expires: float


class AuthState:
    """In-memory capability state; all values disappear when the server exits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, BrowserSession] = {}
        self.reset()

    def reset(self, bootstrap_token: str | None = None) -> str:
        """Invalidate all sessions and issue a new one-time bootstrap capability."""
        with self._lock:
            self._bootstrap_token = bootstrap_token or secrets.token_urlsafe(48)
            self._bootstrap_used = False
            self._sessions = {}
            return self._bootstrap_token

    @property
    def bootstrap_token(self) -> str:
        return self._bootstrap_token

    def exchange(self, candidate: str) -> BrowserSession | None:
        """Consume the launch capability exactly once and create a browser session."""
        now = time.time()
        with self._lock:
            if self._bootstrap_used or not secrets.compare_digest(
                str(candidate or ""), self._bootstrap_token
            ):
                return None
            self._bootstrap_used = True
            # Remove the usable bootstrap value immediately after comparison.
            self._bootstrap_token = secrets.token_urlsafe(48)
            token = secrets.token_urlsafe(48)
            session = BrowserSession(
                token=token,
                csrf=secrets.token_urlsafe(32),
                expires=now + SESSION_TTL_SECONDS,
            )
            self._sessions[token] = session
            return session

    def session(self, token: str) -> BrowserSession | None:
        now = time.time()
        with self._lock:
            expired = [
                key for key, value in self._sessions.items() if value.expires <= now
            ]
            for key in expired:
                self._sessions.pop(key, None)
            value = self._sessions.get(str(token or ""))
            if value and secrets.compare_digest(value.token, str(token or "")):
                return value
            return None

    def session_from_cookie(self, raw_cookie: str) -> BrowserSession | None:
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie or "")
        except Exception:
            return None
        morsel = cookie.get(AUTH_COOKIE)
        return self.session(morsel.value if morsel else "")


AUTH_STATE = AuthState()


def is_loopback_host(value: str) -> bool:
    """Accept only exact loopback host names, with an optional numeric port."""
    host = str(value or "").strip()
    if not host or any(ch in host for ch in "\r\n\t ,/@"):
        return False
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return False
        name = host[1:end].lower()
        suffix = host[end + 1 :]
        if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
            return False
    else:
        name = host
        if host.count(":") == 1:
            possible_name, possible_port = host.rsplit(":", 1)
            if possible_port.isdigit():
                name = possible_name
            elif possible_port:
                return False
        elif host.count(":") > 1:
            # A raw IPv6 Host is ambiguous; RFC-compliant clients use brackets.
            return False
        name = name.lower()
    return name in ALLOWED_HOSTS


def is_loopback_address(value: str) -> bool:
    text = str(value or "").strip().lower()
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _headers(scope: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        if name in result:
            result[name] += "," + value
        else:
            result[name] = value
    return result


def _expected_origin(scope: dict, host: str) -> str:
    scheme = str(scope.get("scheme") or "http").lower()
    if scope.get("type") == "websocket":
        scheme = "https" if scheme == "wss" else "http"
    return f"{scheme}://{host}"


def same_origin(scope: dict, headers: dict[str, str]) -> bool:
    origin = headers.get("origin", "")
    host = headers.get("host", "")
    return bool(
        origin and secrets.compare_digest(origin, _expected_origin(scope, host))
    )


SECURITY_HEADERS = (
    (
        b"content-security-policy",
        (
            b"default-src 'self'; base-uri 'none'; object-src 'none'; "
            b"frame-ancestors 'none'; form-action 'self'; script-src 'self'; "
            b"style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            b"font-src 'self'; connect-src 'self' ws://127.0.0.1:* "
            b"ws://localhost:* ws://[::1]:*; media-src 'none'; "
            b"worker-src 'none'; frame-src 'none'"
        ),
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (
        b"permissions-policy",
        (
            b"accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            b"magnetometer=(), microphone=(), payment=(), usb=()"
        ),
    ),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
)


async def _json_rejection(
    send: Callable[[dict], Awaitable[None]], status: int, detail: str
) -> None:
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        *SECURITY_HEADERS,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class SecurityMiddleware:
    """Pure ASGI guard for Host, same-origin, auth, CSRF, and response headers."""

    def __init__(self, app, auth: AuthState = AUTH_STATE) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: dict, receive, send) -> None:
        scope_type = scope.get("type")
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        host = headers.get("host", "")
        server = scope.get("server") or ("", 0)
        bound_host = str(server[0] or "")
        if not is_loopback_host(host) or not is_loopback_address(bound_host):
            if scope_type == "websocket":
                await send(
                    {"type": "websocket.close", "code": 4403, "reason": "invalid Host"}
                )
            else:
                await _json_rejection(send, 400, "invalid Host header")
            return

        if scope_type == "websocket":
            session = self.auth.session_from_cookie(headers.get("cookie", ""))
            if not session:
                await send(
                    {
                        "type": "websocket.close",
                        "code": 4401,
                        "reason": "authentication required",
                    }
                )
                return
            if not same_origin(scope, headers):
                await send(
                    {
                        "type": "websocket.close",
                        "code": 4403,
                        "reason": "invalid Origin",
                    }
                )
                return
            scope.setdefault("state", {})["benchtop_session"] = session
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "GET").upper()
        content_length = headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > MAX_REQUEST_BYTES:
            await _json_rejection(send, 413, "request body is too large")
            return

        session: BrowserSession | None = None
        is_api = path == "/api" or path.startswith("/api/")
        is_bootstrap = path == "/api/auth/bootstrap"
        is_session_recovery = path == "/api/auth/session"
        if is_api:
            if is_bootstrap:
                if method != "POST":
                    await _json_rejection(send, 405, "method not allowed")
                    return
                if not same_origin(scope, headers):
                    await _json_rejection(send, 403, "same-origin request required")
                    return
            else:
                session = self.auth.session_from_cookie(headers.get("cookie", ""))
                if not session:
                    await _json_rejection(send, 401, "authentication required")
                    return
                if not is_session_recovery:
                    csrf = headers.get(CSRF_HEADER, "")
                    if not csrf or not secrets.compare_digest(csrf, session.csrf):
                        await _json_rejection(send, 403, "valid CSRF token required")
                        return
                if method in {"POST", "PUT", "PATCH", "DELETE"}:
                    if not same_origin(scope, headers):
                        await _json_rejection(send, 403, "same-origin request required")
                        return
                scope.setdefault("state", {})["benchtop_session"] = session

        guarded_receive = receive
        if is_api and method in {"POST", "PUT", "PATCH", "DELETE"}:
            body = bytearray()
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    break
                if message.get("type") != "http.request":
                    continue
                body.extend(message.get("body", b""))
                if len(body) > MAX_REQUEST_BYTES:
                    await _json_rejection(send, 413, "request body is too large")
                    return
                if not message.get("more_body"):
                    break
            delivered = False

            async def replay_body() -> dict:
                nonlocal delivered
                if delivered:
                    return {"type": "http.disconnect"}
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            guarded_receive = replay_body

        async def guarded_send(message: dict) -> None:
            if message.get("type") == "http.response.start":
                response_headers = list(message.get("headers", []))
                existing = {name.lower() for name, _ in response_headers}
                for name, value in SECURITY_HEADERS:
                    if name not in existing:
                        response_headers.append((name, value))
                if is_api and b"cache-control" not in existing:
                    response_headers.append((b"cache-control", b"no-store"))
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, guarded_receive, guarded_send)


def auth_cookie_header(session: BrowserSession) -> str:
    """Return an HTTP-only local session cookie without persisting the value."""
    return (
        f"{AUTH_COOKIE}={session.token}; Path=/; HttpOnly; SameSite=Strict; "
        f"Max-Age={SESSION_TTL_SECONDS}"
    )
