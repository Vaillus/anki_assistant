"""Streaming orchestrator: runs one chat turn and yields SSE events.

This is the async core that ties prompt, tools, and the Anthropic SDK
together.  It owns `_Roster` (workspace cap), `_Citer` (citation numbering),
and the web-event helpers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from anki_assistant.sources import new_source_id

from .prompt import build_system
from .tools import NEW_SOURCE_KINDS, TARGETED, TOOL_KINDS, WEB_RESULTS, tools
from .types import (
    CITED_TEXT_CHARS,
    MAX_CARDS,
    MAX_TOKENS,
    MAX_TOOL_LOOPS,
    WEB_URLS_SHOWN,
    ChatEvent,
    CorpusLoader,
    ReadTool,
    SourceLoader,
    WorkspaceCard,
    default_model,
)


def _tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def _summary(text: str) -> str:
    """First line of a read result, without its Markdown heading marker."""
    first = text.strip().split("\n", 1)[0] if text.strip() else ""
    return first.lstrip("#").strip()


def _attr(obj: Any, name: str) -> Any:
    """Read `name` off an SDK block or off the plain dict the test fake replays."""
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def _field(obj: Any, name: str) -> str:
    value = _attr(obj, name)
    return "" if value is None else str(value)


# -------------------------------------------------------------------- web helpers


def _web_outcome(block: Any) -> tuple[list[Any], str]:
    """`(results, error_code)` of a web result block.

    A web tool that fails still comes back HTTP 200 with the error inside the block, so this is
    the only place that distinguishes the two — and the shapes differ per tool: `web_search`
    succeeds with a *list* of results (empty when nothing matched), `web_fetch` with a single
    result object. So an error is recognised by its `error_code`, never by not being a list.
    """
    content = _attr(block, "content")
    code = _field(content, "error_code")
    if code or _field(content, "type").endswith("_error"):
        return [], code or "unknown error"
    if isinstance(content, list):
        return content, ""
    return ([] if content is None else [content]), ""


def _web_summary(tool: str, call_input: Mapping[str, Any], results: Sequence[Any], err: str) -> str:
    """The reading line for one web call. It spells out the URLs: that is the whole provenance
    guarantee (specs/chat.md#web-tools) — the user must never learn of a page after the fact."""
    if tool == "web_fetch":
        url = str(call_input.get("url") or "") or (_field(results[0], "url") if results else "")
        return f"error: {err}" if err else (url or "page")
    query = str(call_input.get("query") or "")
    head = f"« {query} »" if query else "search"
    if err:
        return f"{head} → error: {err}"
    if not results:
        return f"{head} → no results"
    urls = [url for url in (_field(r, "url") for r in results) if url]
    rest = len(urls) - WEB_URLS_SHOWN
    tail = f" (+{rest})" if rest > 0 else ""
    return f"{head} → {len(results)} result(s): {', '.join(urls[:WEB_URLS_SHOWN])}{tail}"


def _web_events(content: Sequence[Any], calls: dict[str, dict[str, Any]]) -> list[ChatEvent]:
    """`reading` events for the web tools Anthropic ran inside one model call.

    `calls` accumulates `server_tool_use` inputs by id **across the turn**: when Claude calls a
    web tool and a local one in the same batch the API defers the search, so the call block and
    its result land in different messages. An event is emitted on the *result* block only —
    that one appears exactly once, where a deferred call block is re-sent with the next message.
    """
    events: list[ChatEvent] = []
    for block in content:
        kind = _field(block, "type")
        if kind == "server_tool_use":
            calls[_field(block, "id")] = dict(_attr(block, "input") or {})
            continue
        tool = WEB_RESULTS.get(kind)
        if tool is None:
            continue
        call_input = calls.get(_field(block, "tool_use_id"), {})
        results, err = _web_outcome(block)
        events.append(
            ChatEvent(
                "reading",
                {
                    "id": _field(block, "tool_use_id"),
                    "tool": tool,
                    "input": call_input,
                    "summary": _web_summary(tool, call_input, results, err),
                },
            )
        )
    return events


# --------------------------------------------------------------------- citation tracker


class _Citer:
    """Numbers the pages one turn cites and resolves fetch citations to a URL.

    Pages are numbered per turn, by URL, in order of first citation (specs/chat.md#web-tools).
    A `web_search_result_location` names its page directly; a `char_location` (a fetched page)
    only names the document by title and index, so the pages fetched during the turn are kept in
    order — from the stream's `content_block_start` events and from each final message, since a
    deferred fetch lands in a later message than its call.
    """

    def __init__(self) -> None:
        self.numbers: dict[str, int] = {}
        #: `(title, url)` of the fetched pages, in order of fetching.
        self.fetched: list[tuple[str, str]] = []

    def saw_block(self, block: Any) -> None:
        if _field(block, "type") != "web_fetch_tool_result":
            return
        results, _err = _web_outcome(block)
        for result in results:
            url = _field(result, "url")
            document = _attr(result, "content")
            title = "" if isinstance(document, str | None) else _field(document, "title")
            if url and (title, url) not in self.fetched:
                self.fetched.append((title, url))

    def event(self, citation: Any) -> ChatEvent | None:
        kind = _field(citation, "type")
        if kind == "web_search_result_location":
            url, title = _field(citation, "url"), _field(citation, "title")
        elif kind == "char_location":
            url, title = self._resolve_fetch(citation)
        else:
            return None
        if not url:
            return None
        n = self.numbers.setdefault(url, len(self.numbers) + 1)
        cited = " ".join(_field(citation, "cited_text").split())
        if len(cited) > CITED_TEXT_CHARS:
            cited = cited[: CITED_TEXT_CHARS - 1].rstrip() + "…"
        return ChatEvent("citation", {"n": n, "url": url, "title": title, "cited_text": cited})

    def _resolve_fetch(self, citation: Any) -> tuple[str, str]:
        title = _field(citation, "document_title")
        if title:
            for fetched_title, url in self.fetched:
                if fetched_title == title:
                    return url, title
        index = _attr(citation, "document_index")
        if isinstance(index, int) and 0 <= index < len(self.fetched):
            fetched_title, url = self.fetched[index]
            return url, fetched_title or title
        return "", title


# --------------------------------------------------------------------- usage helper


def _usage_dict(message: Any) -> dict[str, Any]:
    usage = getattr(message, "usage", None)
    if usage is None:
        return {}
    out: dict[str, Any] = {}
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = getattr(usage, name, None)
        if value is not None:
            out[name] = value
    return out


# ---------------------------------------------------------------------- workspace roster


class _Roster:
    """What the workspace holds, kept up to date within a turn so the cap can be enforced.

    Cards added by an `add_notes` or by a proposal on an absent note count from that moment on,
    since the client will add them; a second call in the same turn sees the updated count.
    """

    def __init__(self, cards: Sequence[WorkspaceCard]) -> None:
        self.wids = {card.wid for card in cards}
        self.note_ids = {card.note_id for card in cards if card.note_id is not None}
        self.count = len(cards)

    def room_for(self, new_ids: Sequence[int]) -> bool:
        return self.count + len(new_ids) <= MAX_CARDS

    def admit(self, new_ids: Sequence[int]) -> None:
        for nid in new_ids:
            if nid not in self.note_ids:
                self.note_ids.add(nid)
                self.count += 1

    def new_among(self, ids: Sequence[int]) -> list[int]:
        seen: set[int] = set()
        out: list[int] = []
        for nid in ids:
            if nid not in self.note_ids and nid not in seen:
                seen.add(nid)
                out.append(nid)
        return out

    def check_target(self, target: Any) -> str | None:
        """None when the target is fine (and admitted if new), else the error text for Claude."""
        text = str(target or "").strip()
        if text in self.wids:
            return None
        if not text.isdigit():
            return f"unknown target: « {text} » (card number or Anki id)"
        nid = int(text)
        if nid in self.note_ids:
            return None
        if not self.room_for([nid]):
            return _FULL
        self.admit([nid])
        return None


_FULL = (
    f"workspace full ({MAX_CARDS} cards): narrow the selection or ask the user to drop some cards."
)


# ---------------------------------------------------------------------- main entry point


async def stream_chat(
    anthropic_client: Any,
    deck: str,
    cards: Sequence[WorkspaceCard],
    source_ids: Sequence[str],
    messages: Sequence[Mapping[str, Any]],
    load_corpus: CorpusLoader,
    load_source: SourceLoader,
    read_tools: Mapping[str, ReadTool] | None = None,
    model: str | None = None,
    flagged_count: int | None = None,
    note_types: Mapping[str, Sequence[str]] | None = None,
) -> AsyncIterator[ChatEvent]:
    """Run one chat turn (with its tool loop) and yield the events the client should receive.

    `messages` are plain `{role, content: str}` turns. Proposal calls are surfaced as `proposal`
    events and answered with a `"ok"` tool result so Claude can keep talking; they land on the
    workspace and are written at validation. A targeted proposal is checked against the roster
    first: an unknown target or a full workspace is an error tool result and no event. An
    add-source or create-source proposal is answered with the id the source will carry, so that
    Claude can anchor the notes it proposes next to it; the same id travels in the event
    (`source_id`).
    Read calls are executed through `read_tools`, surfaced as `reading` events, and answered
    with the text the tool returned (or an error tool result, so Claude can react instead of
    the turn failing). `add_notes` runs `get_notes` and, when the cap allows, also streams an
    `added` event so the client turns the notes into cards.
    """
    try:
        corpus_index = load_corpus(deck)
        attached = [load_source(str(source_id)) for source_id in source_ids]
        system = build_system(deck, corpus_index, attached, cards, flagged_count, note_types)
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI, never raised into the SSE body
        yield ChatEvent("error", {"detail": f"Contexte indisponible : {exc}"})
        return

    roster = _Roster(cards)
    convo: list[dict[str, Any]] = [
        {"role": str(message["role"]), "content": message["content"]} for message in messages
    ]
    tool_defs = tools()
    final: Any = None
    #: `server_tool_use` inputs by id, kept for the whole turn (see `_web_events`).
    web_calls: dict[str, dict[str, Any]] = {}
    citer = _Citer()

    try:
        for _ in range(MAX_TOOL_LOOPS):
            async with anthropic_client.messages.stream(
                model=model or default_model(),
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tool_defs,
                messages=convo,
            ) as stream:
                pending: list[ChatEvent] = []
                async for event in stream:
                    event_type = getattr(event, "type", None)
                    if event_type == "content_block_start":
                        citer.saw_block(getattr(event, "content_block", None))
                        continue
                    if event_type == "content_block_stop":
                        for cited in pending:
                            yield cited
                        pending = []
                        continue
                    if event_type != "content_block_delta":
                        continue
                    delta: Any = getattr(event, "delta", None)
                    delta_type = getattr(delta, "type", None)
                    if delta_type == "text_delta":
                        yield ChatEvent("text", {"delta": delta.text})
                    elif delta_type == "citations_delta":
                        cited = citer.event(getattr(delta, "citation", None))
                        if cited is not None:
                            pending.append(cited)
                for cited in pending:
                    yield cited
                final = await stream.get_final_message()

            content = getattr(final, "content", None) or []
            for block in content:
                citer.saw_block(block)
            for event in _web_events(content, web_calls):
                yield event

            stop_reason = getattr(final, "stop_reason", None)
            if stop_reason == "pause_turn":
                convo.append({"role": "assistant", "content": final.content})
                continue
            if stop_reason != "tool_use":
                yield ChatEvent(
                    "done",
                    {"stop_reason": stop_reason, "usage": _usage_dict(final)},
                )
                return

            calls = [block for block in final.content if getattr(block, "type", None) == "tool_use"]
            results: list[dict[str, Any]] = []
            for block in calls:
                kind = TOOL_KINDS.get(block.name)
                tool_input = dict(block.input or {})
                if kind is not None:
                    if block.name in TARGETED:
                        problem = roster.check_target(tool_input.get("target"))
                        if problem:
                            results.append(_tool_result(block.id, problem, is_error=True))
                            continue
                    data: dict[str, Any] = {"id": block.id, "kind": kind, "input": tool_input}
                    answer = "ok"
                    if kind in NEW_SOURCE_KINDS:
                        data["source_id"] = new_source_id()
                        answer = (
                            f"ok — once applied, the source will carry the identifier "
                            f"{data['source_id']} (usable in propose_create's source_ids)."
                        )
                    yield ChatEvent("proposal", data)
                    results.append(_tool_result(block.id, answer))
                    continue
                if block.name == "add_notes":
                    ids = [int(i) for i in tool_input.get("note_ids") or []]
                    new_ids = roster.new_among(ids)
                    if not roster.room_for(new_ids):
                        results.append(_tool_result(block.id, _FULL, is_error=True))
                        continue
                    tool = (read_tools or {}).get("get_notes")
                else:
                    tool = (read_tools or {}).get(block.name)
                if tool is None:
                    results.append(
                        _tool_result(block.id, f"Outil inconnu : {block.name}", is_error=True)
                    )
                    continue
                try:
                    text = tool(tool_input)
                except Exception as exc:  # noqa: BLE001 — Claude gets the error, not the UI
                    text, failed = f"{type(exc).__name__}: {exc}", True
                else:
                    failed = False
                results.append(_tool_result(block.id, text, is_error=failed))
                if block.name == "add_notes" and not failed:
                    roster.admit(new_ids)
                    yield ChatEvent(
                        "added",
                        {
                            "id": block.id,
                            "note_ids": ids,
                        },
                    )
                    continue
                yield ChatEvent(
                    "reading",
                    {
                        "id": block.id,
                        "tool": block.name,
                        "input": tool_input,
                        "summary": ("error: " if failed else "") + _summary(text),
                    },
                )
            convo.append({"role": "assistant", "content": final.content})
            convo.append({"role": "user", "content": results})

        yield ChatEvent("done", {"stop_reason": "max_tool_loops", "usage": _usage_dict(final)})
    except Exception as exc:  # noqa: BLE001 — the SSE stream reports, it does not crash
        yield ChatEvent("error", {"detail": f"{type(exc).__name__}: {exc}"})
