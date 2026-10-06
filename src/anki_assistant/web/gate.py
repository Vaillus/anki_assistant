"""The tailnet gate: who may reach the phone app. Spec: specs/mobile.md#tailnet-gate.

The phone app listens on its own port, the only one `tailscale serve` exposes. Tailscale
forwards each request with the sender's login in `Tailscale-User-Login` (absent for tagged
devices). Every request to the phone app must carry the owner's login; the Host header,
which the sender chooses, plays no part.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Receive, Scope, Send

LOGIN_HEADER = b"tailscale-user-login"


def allows(logins: list[str], owner: str | None) -> bool:
    """Whether a request carrying these `Tailscale-User-Login` values may pass: exactly one,
    equal to the owner's login (surrounding spaces ignored). An unset owner refuses all."""
    if not owner or not owner.strip():
        return False
    return len(logins) == 1 and logins[0].strip() == owner.strip()


class TailnetGate:
    """ASGI middleware answering 403 to every request `allows` refuses."""

    def __init__(self, app: ASGIApp, owner: str | None = None) -> None:
        self.app = app
        self.owner = owner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        logins = [v.decode("latin-1") for k, v in scope["headers"] if k.lower() == LOGIN_HEADER]
        if allows(logins, self.owner):
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
