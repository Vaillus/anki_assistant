"""Error mapping shared by the /api routes: unknown note -> 404, Anki unreachable -> 503,
any other AnkiConnect failure -> 502 (specs/review.md#api)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException

from anki_assistant.client import AnkiConnectError
from anki_assistant.review import NoteNotFound

# `AnkiClient.invoke` wraps a URLError with this prefix; it is the one AnkiConnect failure that
# means "Anki is not running" rather than "Anki refused the request".
UNREACHABLE_PREFIX = "Cannot reach"


@contextmanager
def anki_errors() -> Iterator[None]:
    try:
        yield
    except NoteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AnkiConnectError as exc:
        message = str(exc)
        status = 503 if message.startswith(UNREACHABLE_PREFIX) else 502
        raise HTTPException(status_code=status, detail=message) from exc
