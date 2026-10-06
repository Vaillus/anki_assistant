"""The tailnet gate: what a request arriving through `tailscale serve` may reach.
Spec: specs/mobile.md#tailnet-gate.

The server is bound to 127.0.0.1; `tailscale serve` forwards tailnet requests to it with the
`.ts.net` name as `Host` and the sender's login in `Tailscale-User-Login` (absent for tagged
devices). A request whose Host is not localhost is **remote**: it may only reach the phone
app's routes, and only when it comes from the owner's login.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Receive, Scope, Send

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
LOGIN_HEADER = "tailscale-user-login"
#: Path prefixes a remote request may reach (besides `/m` itself).
REMOTE_PREFIXES = ("/m/", "/static/", "/api/mobile/")


def is_remote(host: str | None) -> bool:
    """True unless the Host header, port removed, is localhost or 127.0.0.1."""
    if not host:
        return True
    name = host.strip().lower()
    if not name.startswith("["):  # an IPv6 literal is never one of the local names
        name = name.split(":", 1)[0]
    return name not in LOCAL_HOSTS


def remote_path_allowed(path: str) -> bool:
    """`/m`, or under `/m/`, `/static/`, `/api/mobile/`, with no `..` segment."""
    if ".." in path.split("/"):
        return False
    return path == "/m" or path.startswith(REMOTE_PREFIXES)


def allows(host: str | None, path: str, login: str | None, owner: str | None) -> bool:
    """Whether the gate lets this request through."""
    if not is_remote(host):
        return True
    if not owner or not owner.strip():
        return False
    if login is None or login.strip() != owner.strip():
        return False
    return remote_path_allowed(path)


class TailnetGate:
    """ASGI middleware answering 403 to every request `allows` refuses."""

    def __init__(self, app: ASGIApp, owner: str | None = None) -> None:
        self.app = app
        self.owner = owner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if allows(headers.get("host"), scope["path"], headers.get(LOGIN_HEADER), self.owner):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": "Forbidden"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
