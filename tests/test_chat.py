"""Tests for chat.py and routes_chat.py. Spec: specs/chat.md.

The Anthropic client is faked: `messages.stream(...)` returns an async context manager that
replays scripted stream events and a final message. No network, no API key, no Anki.
pytest-asyncio is not installed, so the async generator is drained with `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from anki_assistant import chat

# --------------------------------------------------------------------------- fake SDK


def text_delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text)
    )


def thinking_delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="thinking_delta", thinking=text)
    )


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(block_id: str, name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


def final_message(content: list[Any], stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1234, output_tokens=210),
    )


class FakeStream:
    def __init__(self, events: list[Any], final: Any) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def __aiter__(self):  # noqa: ANN204 - async generator
        for event in self._events:
            yield event

    async def get_final_message(self) -> Any:
        return self._final


class FakeMessages:
    def __init__(self, turns: list[tuple[list[Any], Any]]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        events, final = self._turns.pop(0)
        return FakeStream(events, final)


class FakeAnthropic:
    def __init__(self, turns: list[tuple[list[Any], Any]]) -> None:
        self.messages = FakeMessages(turns)


class BoomMessages:
    def stream(self, **kwargs: Any) -> FakeStream:
        raise RuntimeError("connection lost")


# ------------------------------------------------------------------------- fake context


@dataclass
class FakeNote:
    note_id: int = 1732375559262
    deck: str = "courant::00-Thèse"
    model: str = "Cloze"
    tags: list[str] = field(default_factory=lambda: ["phd"])
    fields: dict[str, str] = field(
        default_factory=lambda: {
            "Text": "L'angle est {{c1::disjoint}} du précédent.",
            "Back Extra": "For a given sensor ?",
        }
    )
    reason: str = "For a given sensor ?"
    flagged_cards: list[Any] = field(default_factory=lambda: [SimpleNamespace(ord=1)])


def make_corpus(*texts: str) -> list[chat.CorpusText]:
    return [
        chat.CorpusText(kind="pdf", target=f"~/doc{i}.pdf", text=text, pages="1-3", n_pages=3)
        for i, text in enumerate(texts, start=1)
    ]


def drain(agen: Any) -> list[chat.ChatEvent]:
    async def run() -> list[chat.ChatEvent]:
        return [event async for event in agen]

    return asyncio.run(run())


def run_chat(client: Any, **overrides: Any) -> list[chat.ChatEvent]:
    kwargs: dict[str, Any] = {
        "deck": "courant::00-Thèse",
        "note_ids": [1732375559262],
        "messages": [{"role": "user", "content": "Cette carte est trop vague, tu proposes quoi ?"}],
        "load_note": lambda note_id: FakeNote(note_id=note_id),
        "load_corpus": lambda deck: make_corpus("Le capteur observe un angle."),
        "model": "test-model",
    }
    kwargs.update(overrides)
    return drain(chat.stream_chat(client, **kwargs))


# ------------------------------------------------------------------------ build_system


def test_build_system_includes_notes_fields_reason_and_source_headers() -> None:
    blocks = chat.build_system(
        "courant::00-Thèse",
        [
            chat.CorpusText(
                kind="obsidian",
                target="Allocation sur des angles disjoints",
                text="Le contenu de la note Obsidian.",
                deck="courant",
            )
        ],
        [FakeNote()],
        flagged_count=3,
    )
    assert [block["type"] for block in blocks] == ["text", "text", "text"]
    # Corpus block is last and is the cached one.
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[0]

    context = blocks[2]["text"]
    assert "courant::00-Thèse" in context
    assert "3" in context  # flagged count
    assert "1732375559262" in context
    assert "{{c1::disjoint}}" in context  # raw fields, cloze markers kept
    assert "raison du flag : For a given sensor ?" in context
    assert "carte(s) flaguée(s) : c2" in context

    corpus = blocks[1]["text"]
    assert "obsidian : Allocation sur des angles disjoints" in corpus
    assert "déclarée sur le deck courant" in corpus
    assert "Le contenu de la note Obsidian." in corpus


def test_standing_instructions_state_the_rules() -> None:
    text = chat.build_system("d", [], [])[0]["text"]
    for needle in (
        "français",
        "outils",
        "cloze",
        "corpus",
        "raison du flag",
        "list_deck_notes",
        "propose_create",
        'class="context"',
    ):
        assert needle in text


def test_build_system_caps_total_corpus_and_says_so() -> None:
    big = "a" * (chat.MAX_CORPUS_CHARS - 10)
    blocks = chat.build_system("d", make_corpus(big, "b" * 5000, "c" * 100), [FakeNote()])
    corpus = blocks[1]["text"]
    assert big in corpus  # first source fits whole
    assert "b" * 10 in corpus and "b" * 11 not in corpus  # second cut to the remaining budget
    assert "c" * 100 not in corpus  # third source omitted entirely
    assert f"plafond de {chat.MAX_CORPUS_CHARS} caractères est atteint" in corpus
    assert "Source omise" in corpus


def test_build_system_relays_source_warning_and_truncation() -> None:
    source = chat.CorpusText(
        kind="pdf",
        target="~/big.pdf",
        text="x",
        truncated=True,
        warning="PDF entier (312 pages) sans plage de pages.",
    )
    corpus = chat.build_system("d", [source], [])[1]["text"]
    assert "PDF entier (312 pages)" in corpus
    assert "tronqué" in corpus


# ------------------------------------------------------------------------------- tools


def test_tools_cover_the_five_proposals_then_the_read_tools() -> None:
    defs = chat.tools()
    names = [tool["name"] for tool in defs]
    proposals = [
        "propose_edit",
        "propose_split",
        "propose_create",
        "propose_move",
        "propose_bulk_edit",
    ]
    assert names[: len(proposals)] == proposals
    assert set(proposals) == set(chat.TOOL_KINDS)
    assert tuple(names[len(proposals) :]) == chat.READ_TOOLS
    assert not (set(chat.READ_TOOLS) & set(chat.TOOL_KINDS))
    for tool in defs:
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        assert tool["description"]
    for tool in chat.proposal_tools():
        assert tool["input_schema"]["required"]  # every proposal carries a rationale at least


def test_tool_schemas_take_raw_field_maps() -> None:
    by_name = {tool["name"]: tool["input_schema"] for tool in chat.tools()}
    assert by_name["propose_edit"]["properties"]["fields"]["additionalProperties"] == {
        "type": "string"
    }
    assert by_name["propose_move"]["required"] == ["note_id", "deck", "rationale"]
    split_new = by_name["propose_split"]["properties"]["new_notes"]
    assert split_new["items"]["required"] == ["fields"]
    bulk = by_name["propose_bulk_edit"]
    assert bulk["required"] == ["edits", "rationale"]
    assert bulk["properties"]["edits"]["items"]["required"] == ["note_id", "fields"]
    assert by_name["get_notes"]["required"] == ["note_ids"]
    assert by_name["list_decks"]["required"] == []


# ---------------------------------------------------------------------- read tool output


def test_format_deck_index_is_one_compact_line_per_note() -> None:
    long_note = FakeNote(
        note_id=2, fields={"Text": "x" * 300, "Back Extra": ""}, reason="", flagged_cards=[]
    )
    text = chat.format_deck_index([FakeNote(), long_note], "courant::00-Thèse")
    lines = text.splitlines()
    assert lines[0] == "# Index — 2 note(s) du deck courant::00-Thèse"
    row1 = next(line for line in lines if line.startswith("#1732375559262"))
    assert "⚑" in row1
    assert "Text: L'angle est {{c1::disjoint}} du précédent." in row1
    assert "courant::00-Thèse" not in row1  # same deck as asked: omitted
    row2 = next(line for line in lines if line.startswith("#2"))
    assert "⚑" not in row2
    assert "Back Extra" not in row2  # empty fields are skipped
    assert "x" * (chat.INDEX_FIELD_CHARS - 1) + "…" in row2
    assert "x" * chat.INDEX_FIELD_CHARS not in row2


def test_format_deck_index_names_the_deck_when_it_differs() -> None:
    text = chat.format_deck_index([FakeNote()], "autre::deck")
    assert "#1732375559262 · courant::00-Thèse · ⚑ · " in text
    assert "(aucune note)" in chat.format_deck_index([], "d")


def test_format_decks_indents_by_depth_and_keeps_full_names() -> None:
    decks = [
        SimpleNamespace(name="courant", depth=0, flagged_own=0, flagged_total=7),
        SimpleNamespace(name="courant::00-Thèse", depth=1, flagged_own=7, flagged_total=7),
    ]
    text = chat.format_decks(decks)
    assert "\n- [courant]  ⚑ 0 / 7\n" in text
    assert "\n  - [courant::00-Thèse]  ⚑ 7 / 7" in text


def test_format_notes_reuses_the_context_layout() -> None:
    text = chat.format_notes([FakeNote()])
    assert "### Note 1732375559262" in text
    assert "raison du flag : For a given sensor ?" in text
    assert "aucune note" in chat.format_notes([])


def test_format_note_type_lists_fields_templates_and_css() -> None:
    note_type = SimpleNamespace(
        name="Cloze",
        fields=["Text", "Back Extra"],
        templates={
            "Cloze": {"Front": "{{cloze:Text}}", "Back": "{{cloze:Text}}<br>{{Back Extra}}"}
        },
        css=".card { color: black; }",
    )
    text = chat.format_note_type(note_type)
    assert "# Type de note Cloze" in text
    assert "Champs : Text, Back Extra" in text
    assert "### Recto\n```html\n{{cloze:Text}}\n```" in text
    assert "## CSS\n```css\n.card { color: black; }\n```" in text


# ------------------------------------------------------------------------- stream_chat


def test_text_deltas_stream_then_done() -> None:
    client = FakeAnthropic(
        [
            (
                [text_delta("Je pense "), thinking_delta("(ignoré)"), text_delta("que…")],
                final_message([text_block("Je pense que…")], "end_turn"),
            )
        ]
    )
    events = run_chat(client)
    assert [event.type for event in events] == ["text", "text", "done"]
    assert [event.data["delta"] for event in events[:2]] == ["Je pense ", "que…"]
    assert events[-1].data == {
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1234, "output_tokens": 210},
    }
    # The request carries the cached system prompt and every tool (proposals + reads).
    call = client.messages.calls[0]
    assert call["model"] == "test-model"
    assert call["max_tokens"] == 8192
    assert call["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in call["tools"]] == [t["name"] for t in chat.tools()]


def test_tool_use_emits_a_proposal_and_the_loop_continues() -> None:
    tool_block = tool_use_block(
        "toolu_01", "propose_split", {"note_id": 5262, "original": None, "new_notes": []}
    )
    client = FakeAnthropic(
        [
            ([], final_message([text_block("Voilà :"), tool_block], "tool_use")),
            ([text_delta("Ça coupe la note en deux.")], final_message([], "end_turn")),
        ]
    )
    events = run_chat(client)
    assert [event.type for event in events] == ["proposal", "text", "done"]
    proposal = events[0].data
    assert proposal["kind"] == "split"
    assert proposal["id"] == "toolu_01"
    assert proposal["input"]["note_id"] == 5262

    # Second turn replays the assistant content plus a tool_result "ok".
    assert len(client.messages.calls) == 2
    convo = client.messages.calls[1]["messages"]
    assert convo[0]["role"] == "user"
    assert convo[1] == {"role": "assistant", "content": [text_block("Voilà :"), tool_block]}
    assert convo[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "ok"}],
    }


def test_several_proposals_in_one_turn() -> None:
    blocks = [
        tool_use_block("toolu_a", "propose_edit", {"note_id": 1, "fields": {}, "rationale": "r"}),
        tool_use_block("toolu_b", "propose_move", {"note_id": 1, "deck": "x", "rationale": "r"}),
    ]
    client = FakeAnthropic(
        [
            ([], final_message(blocks, "tool_use")),
            ([], final_message([], "end_turn")),
        ]
    )
    events = run_chat(client)
    assert [event.data.get("kind") for event in events if event.type == "proposal"] == [
        "edit",
        "move",
    ]


def test_bulk_edit_is_a_proposal_kind() -> None:
    block = tool_use_block(
        "toolu_b", "propose_bulk_edit", {"edits": [{"note_id": 1, "fields": {"Text": "x"}}]}
    )
    client = FakeAnthropic(
        [([], final_message([block], "tool_use")), ([], final_message([], "end_turn"))]
    )
    events = run_chat(client)
    assert events[0].type == "proposal"
    assert events[0].data["kind"] == "bulk_edit"
    assert events[0].data["input"]["edits"][0]["note_id"] == 1


def test_read_tool_is_executed_and_its_text_fed_back() -> None:
    block = tool_use_block("toolu_r", "list_deck_notes", {})
    client = FakeAnthropic(
        [
            ([], final_message([block], "tool_use")),
            ([text_delta("Deux notes.")], final_message([], "end_turn")),
        ]
    )
    index = "# Index — 2 note(s) du deck d\n\n#1 · ⚑ · Text: a\n#2 · Text: b"
    seen: list[dict[str, Any]] = []

    def list_deck_notes(inp: dict[str, Any]) -> str:
        seen.append(inp)
        return index

    events = run_chat(client, read_tools={"list_deck_notes": list_deck_notes})
    assert [event.type for event in events] == ["reading", "text", "done"]
    assert events[0].data == {
        "id": "toolu_r",
        "tool": "list_deck_notes",
        "input": {},
        "summary": "Index — 2 note(s) du deck d",
    }
    assert seen == [{}]
    convo = client.messages.calls[1]["messages"]
    assert convo[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_r", "content": index}],
    }


def test_read_tool_failure_becomes_an_error_tool_result_not_a_chat_error() -> None:
    block = tool_use_block("toolu_r", "get_notes", {"note_ids": [1]})
    client = FakeAnthropic(
        [([], final_message([block], "tool_use")), ([], final_message([], "end_turn"))]
    )

    def get_notes(inp: dict[str, Any]) -> str:
        raise LookupError("No note with id 1")

    events = run_chat(client, read_tools={"get_notes": get_notes})
    assert [event.type for event in events] == ["reading", "done"]
    assert events[0].data["summary"].startswith("erreur : ")
    result = client.messages.calls[1]["messages"][2]["content"][0]
    assert result["is_error"] is True
    assert "No note with id 1" in result["content"]


def test_unknown_tool_gets_an_error_result_and_no_event() -> None:
    block = tool_use_block("toolu_x", "frobnicate", {})
    client = FakeAnthropic(
        [([], final_message([block], "tool_use")), ([], final_message([], "end_turn"))]
    )
    events = run_chat(client, read_tools={})
    assert [event.type for event in events] == ["done"]
    result = client.messages.calls[1]["messages"][2]["content"][0]
    assert result["is_error"] is True
    assert "frobnicate" in result["content"]


def test_proposal_and_read_in_one_turn_keep_their_order() -> None:
    blocks = [
        tool_use_block("toolu_a", "propose_edit", {"note_id": 1, "fields": {}, "rationale": "r"}),
        tool_use_block("toolu_b", "list_decks", {}),
    ]
    client = FakeAnthropic(
        [([], final_message(blocks, "tool_use")), ([], final_message([], "end_turn"))]
    )
    events = run_chat(client, read_tools={"list_decks": lambda inp: "# Decks\n\n- [d]  ⚑ 1 / 1"})
    assert [event.type for event in events] == ["proposal", "reading", "done"]
    results = client.messages.calls[1]["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_a", "toolu_b"]
    assert results[0]["content"] == "ok"
    assert results[1]["content"].startswith("# Decks")


def test_tool_loop_is_capped() -> None:
    tool_block = tool_use_block("toolu_x", "propose_edit", {})
    client = FakeAnthropic(
        [([], final_message([tool_block], "tool_use"))] * (chat.MAX_TOOL_LOOPS + 2)
    )
    events = run_chat(client)
    assert len(client.messages.calls) == chat.MAX_TOOL_LOOPS
    assert events[-1].type == "done"
    assert events[-1].data["stop_reason"] == "max_tool_loops"


def test_api_failure_becomes_an_error_event() -> None:
    client = SimpleNamespace(messages=BoomMessages())
    events = run_chat(client)
    assert [event.type for event in events] == ["error"]
    assert "connection lost" in events[0].data["detail"]


def test_context_failure_becomes_an_error_event() -> None:
    def boom(note_id: int) -> Any:
        raise RuntimeError("Anki injoignable")

    events = run_chat(FakeAnthropic([]), load_note=boom)
    assert [event.type for event in events] == ["error"]
    assert "Anki injoignable" in events[0].data["detail"]


def test_default_model_reads_the_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("ANKI_CHAT_MODEL", raising=False)
    assert chat.default_model() == chat.DEFAULT_MODEL
    monkeypatch.setenv("ANKI_CHAT_MODEL", "claude-sonnet-5")
    assert chat.default_model() == "claude-sonnet-5"


# ----------------------------------------------------------------------- routes_chat


def make_client(monkeypatch: Any):  # noqa: ANN201 - TestClient
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from anki_assistant.web import routes_chat

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(routes_chat, "_clients", {})
    app = FastAPI()
    app.include_router(routes_chat.router, prefix="/api")
    return TestClient(app)


def test_status_reports_missing_key(monkeypatch: Any) -> None:
    client = make_client(monkeypatch)
    body = client.get("/api/chat/status").json()
    assert body == {"configured": False, "model": chat.default_model()}


def test_missing_api_key_yields_a_single_error_event(monkeypatch: Any) -> None:
    client = make_client(monkeypatch)
    response = client.post(
        "/api/chat",
        json={"deck": "d", "note_ids": [], "messages": [{"role": "user", "content": "salut"}]},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = response.text.strip().splitlines()
    assert lines[0] == "event: error"
    detail = json.loads(lines[1].removeprefix("data: "))["detail"]
    assert "ANTHROPIC_API_KEY" in detail
