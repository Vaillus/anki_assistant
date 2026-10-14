"""Error mapping shared by the /api routes: unknown note -> 404, collection held by Anki desktop
-> 503, any other Anki failure -> 502 (specs/review.md#api)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from fastapi import HTTPException

from anki_assistant.client import AnkiError, CollectionLocked
from anki_assistant.review import NoteNotFound


@contextmanager
def anki_errors(anki: Any = None) -> Iterator[None]:
    """Map Anki failures to HTTP errors. Given the request's `AnkiClient`, also hold the
    collection for the whole block: one action, one open of the file."""
    try:
        with ExitStack() as stack:
            action = getattr(anki, "action", None)
            if action is not None:
                stack.enter_context(action())
            yield
    except NoteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CollectionLocked as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AnkiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
