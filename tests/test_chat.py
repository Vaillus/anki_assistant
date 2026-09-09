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

NOTE_ID = 1732375559262


@dataclass
class FakeNote:
    note_id: int = NOTE_ID
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


@dataclass
class FakeSource:
    id: str = "k7q2vd"
    deck: str = "courant"
    kind: str = "obsidian"
    target: str = "Allocation sur des angles disjoints"
    pages: str = ""
    note: str = ""


@dataclass
class FakeText:
    text: str = "Le contenu de la note Obsidian."
    truncated: bool = False
    n_pages: int | None = None
    warning: str = ""


def entry(**overrides: Any) -> chat.CorpusEntry:
    base: dict[str, Any] = {
        "id": "k7q2vd",
        "kind": "obsidian",
        "target": "Allocation sur des angles disjoints",
        "deck": "courant",
    }
    base.update(overrides)
    return chat.CorpusEntry(**base)


def attached(*texts: str) -> list[chat.Attached]:
    return [
        (FakeSource(id=f"src{i}", kind="pdf", target=f"~/doc{i}.pdf", pages="1-3"), FakeText(t))
        for i, t in enumerate(texts, start=1)
    ]


def drain(agen: Any) -> list[chat.ChatEvent]:
    async def run() -> list[chat.ChatEvent]:
        return [event async for event in agen]

    return asyncio.run(run())


def run_chat(client: Any, **overrides: Any) -> list[chat.ChatEvent]:
    kwargs: dict[str, Any] = {
        "deck": "courant::00-Thèse",
        "note_ids": [NOTE_ID],
        "source_ids": [],
        "messages": [{"role": "user", "content": "Cette carte est trop vague, tu proposes quoi ?"}],
        "load_note": lambda note_id: FakeNote(note_id=note_id),
        "load_corpus": lambda deck: [entry()],
        "load_source": lambda source_id: (FakeSource(id=source_id), FakeText()),
        "model": "test-model",
    }
    kwargs.update(overrides)
    return drain(chat.stream_chat(client, **kwargs))


# ------------------------------------------------------------------------ build_system


def test_build_system_has_four_blocks_and_caches_through_the_attached_sources() -> None:
    blocks = chat.build_system(
        "courant::00-Thèse",
        [entry(anchored_note_ids=[NOTE_ID]), entry(id="m3x8pa", kind="pdf", target="~/s.pdf")],
        [(FakeSource(), FakeText())],
        [FakeNote()],
        flagged_count=3,
    )
    assert [block["type"] for block in blocks] == ["text"] * 4
    assert blocks[2]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in blocks[i] for i in (0, 1, 3))

    index = blocks[1]["text"]
    assert "[k7q2vd] obsidian : Allocation sur des angles disjoints" in index
    assert "déclarée sur le deck courant" in index
    assert f"ancrée à la note #{NOTE_ID}" in index
    assert "[m3x8pa] pdf : ~/s.pdf" in index
    assert "Le contenu de la note Obsidian." not in index  # the index carries no text

    sources = blocks[2]["text"]
    assert "## Allocation sur des angles disjoints (obsidian, 31 car.)" in sources
    assert "Le contenu de la note Obsidian." in sources

    context = blocks[3]["text"]
    assert "courant::00-Thèse" in context
    assert "Notes signalées dans ce deck : 3." in context
    assert str(NOTE_ID) in context
    assert "{{c1::disjoint}}" in context  # raw fields, cloze markers kept
    assert "raison du flag : For a given sensor ?" in context
    assert "carte(s) flaguée(s) : c2" in context
    assert "ancres : [k7q2vd] Allocation sur des angles disjoints" in context


def test_standing_instructions_say_only_what_the_spec_lists() -> None:
    text = chat.build_system("d", [], [], [])[0]["text"]
    for needle in ("français", "propose_create_source", "propose_edit_source", "cloze"):
        assert needle in text
    assert 'class="context"' in text  # conventions of the collection, stated as facts
    # Not the prompt's business (specs/chat.md#what-claude-receives).
    for absent in ("Sois bref", "Une note = une idée", "list_deck_notes", "Avant propose_create"):
        assert absent not in text


def test_empty_index_and_no_attached_source_are_said_explicitly() -> None:
    blocks = chat.build_system("d", [], [], [])
    assert "Aucune source n'est associée" in blocks[1]["text"]
    assert "Aucune source jointe" in blocks[2]["text"]
    assert "read_source" in blocks[2]["text"]
    assert "(Aucune note sélectionnée.)" in blocks[3]["text"]


def test_attached_sources_are_capped_in_total_and_the_cap_is_stated() -> None:
    big = "a" * (chat.MAX_ATTACHED_CHARS - 10)
    text = chat.build_system("d", [], attached(big, "b" * 5000, "c" * 100), [])[2]["text"]
    assert big in text  # first source fits whole
    assert "b" * 10 in text and "b" * 11 not in text  # second cut to the remaining budget
    assert "c" * 100 not in text  # third source omitted entirely
    assert "150 000 caractères au total" in text
    assert "Source coupée ici" in text
    assert "Source omise" in text


def test_attached_source_relays_warning_and_truncation() -> None:
    source = FakeSource(kind="pdf", target="~/big.pdf")
    text = FakeText("x", truncated=True, warning="PDF entier (312 pages) sans plage de pages.")
    block = chat.build_system("d", [], [(source, text)], [])[2]["text"]
    assert "PDF entier (312 pages)" in block
    assert "tronqué" in block


def test_index_marks_missing_files() -> None:
    text = chat.build_system("d", [entry(missing=True)], [], [])[1]["text"]
    assert "⚠ fichier introuvable" in text


# ------------------------------------------------------------------------------- tools


def test_tools_cover_the_seven_proposals_then_the_five_read_tools() -> None:
    defs = chat.tools()
    names = [tool["name"] for tool in defs]
    proposals = [
        "propose_edit",
        "propose_split",
        "propose_create",
        "propose_move",
        "propose_bulk_edit",
        "propose_create_source",
        "propose_edit_source",
    ]
    assert names[: len(proposals)] == proposals
    assert set(proposals) == set(chat.TOOL_KINDS)
    assert tuple(names[len(proposals) :]) == chat.READ_TOOLS
    assert chat.READ_TOOLS == (
        "list_decks",
        "search_notes",
        "get_notes",
        "get_note_type",
        "read_source",
    )
    assert not (set(chat.READ_TOOLS) & set(chat.TOOL_KINDS))
    for tool in defs:
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        assert tool["description"]
    for tool in chat.proposal_tools():
        assert "rationale" in tool["input_schema"]["required"]


def test_tool_schemas_match_the_spec_tables() -> None:
    by_name = {tool["name"]: tool["input_schema"] for tool in chat.tools()}
    assert by_name["propose_edit"]["properties"]["fields"]["additionalProperties"] == {
        "type": "string"
    }
    assert by_name["propose_move"]["required"] == ["note_id", "deck", "rationale"]
    assert "source_ids" in by_name["propose_create"]["properties"]
    split_new = by_name["propose_split"]["properties"]["new_notes"]
    assert split_new["items"]["required"] == ["fields"]
    bulk = by_name["propose_bulk_edit"]
    assert bulk["required"] == ["edits", "rationale"]
    assert bulk["properties"]["edits"]["items"]["required"] == ["note_id", "fields"]
    assert by_name["propose_create_source"]["required"] == ["name", "content", "rationale"]
    assert "anchor_note_ids" in by_name["propose_create_source"]["properties"]
    assert by_name["propose_edit_source"]["required"] == ["source_id", "old", "new", "rationale"]

    search = by_name["search_notes"]
    assert search["required"] == ["query"]
    assert search["properties"]["detail"]["enum"] == ["count", "brief", "full"]
    assert search["properties"]["fields"]["items"] == {"type": "string"}
    assert "limit" not in search["properties"]
    assert by_name["read_source"]["required"] == ["source_id"]
    assert by_name["get_notes"]["required"] == ["note_ids"]
    assert by_name["list_decks"]["required"] == []


# ---------------------------------------------------------------------- read tool output


def test_format_notes_brief_is_one_compact_line_per_note() -> None:
    long_note = FakeNote(
        note_id=2, fields={"Text": "x" * 300, "Back Extra": ""}, reason="", flagged_cards=[]
    )
    text = chat.format_notes_brief([FakeNote(), long_note], "courant::00-Thèse")
    lines = text.splitlines()
    assert lines[0] == "# 2 note(s) du deck courant::00-Thèse"
    row1 = next(line for line in lines if line.startswith(f"#{NOTE_ID}"))
    assert "⚑" in row1
    assert "Text: L'angle est {{c1::disjoint}} du précédent." in row1
    assert "courant::00-Thèse" not in row1  # same deck as asked: omitted
    row2 = next(line for line in lines if line.startswith("#2"))
    assert "⚑" not in row2
    assert "Back Extra" not in row2  # empty fields are skipped
    assert "x" * (chat.BRIEF_FIELD_CHARS - 1) + "…" in row2
    assert "x" * chat.BRIEF_FIELD_CHARS not in row2


def test_format_notes_brief_names_the_deck_when_it_differs() -> None:
    text = chat.format_notes_brief([FakeNote()], "autre::deck")
    assert f"#{NOTE_ID} · courant::00-Thèse · ⚑ · " in text
    assert "(aucune note)" in chat.format_notes_brief([], "d")


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
    assert f"### Note {NOTE_ID}" in text
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


def test_format_source_first_line_is_the_reading_summary() -> None:
    text = chat.format_source(FakeSource(pages="12-19", note="chap. 2"), FakeText("x" * 3200))
    lines = text.splitlines()
    assert lines[0] == "# Allocation sur des angles disjoints (obsidian, 3 200 car.)"
    assert lines[1] == "source k7q2vd · pages 12-19 · chap. 2 · déclarée sur le deck courant"
    assert lines[-1] == "x" * 3200
    empty = chat.format_source(FakeSource(), FakeText(""))
    assert "aucun texte" in empty


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
    # The request carries the four-block system prompt and every tool (proposals + reads).
    call = client.messages.calls[0]
    assert call["model"] == "test-model"
    assert call["max_tokens"] == 8192
    assert len(call["system"]) == 4
    assert call["system"][2]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in call["tools"]] == [t["name"] for t in chat.tools()]


def test_attached_sources_are_loaded_by_id_into_block_three() -> None:
    client = FakeAnthropic([([], final_message([], "end_turn"))])
    loaded: list[str] = []

    def load_source(source_id: str) -> chat.Attached:
        loaded.append(source_id)
        return FakeSource(id=source_id, target=f"note {source_id}"), FakeText(f"texte {source_id}")

    run_chat(client, source_ids=["aaaaaa", "bbbbbb"], load_source=load_source)
    assert loaded == ["aaaaaa", "bbbbbb"]
    block = client.messages.calls[0]["system"][2]["text"]
    assert block.index("texte aaaaaa") < block.index("texte bbbbbb")


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
    assert proposal == {"kind": "split", "id": "toolu_01", "input": tool_block.input}

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
        tool_use_block("toolu_c", "propose_edit_source", {"source_id": "k7q2vd", "old": "a"}),
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
        "edit_source",
    ]


def test_create_source_proposal_announces_the_future_source_id() -> None:
    block = tool_use_block(
        "toolu_s", "propose_create_source", {"name": "maths/kkt", "content": "# KKT"}
    )
    client = FakeAnthropic(
        [([], final_message([block], "tool_use")), ([], final_message([], "end_turn"))]
    )
    events = run_chat(client)
    proposal = events[0].data
    assert proposal["kind"] == "create_source"
    source_id = proposal["source_id"]
    assert len(source_id) == 6 and source_id.islower()
    result = client.messages.calls[1]["messages"][2]["content"][0]
    assert result["tool_use_id"] == "toolu_s"
    assert source_id in result["content"]
    assert "propose_create" in result["content"]


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
    block = tool_use_block("toolu_r", "search_notes", {"query": "re:lagrang"})
    client = FakeAnthropic(
        [
            ([], final_message([block], "tool_use")),
            ([text_delta("Deux notes.")], final_message([], "end_turn")),
        ]
    )
    index = "# 2 note(s) du deck d\n\n#1 · ⚑ · Text: a\n#2 · Text: b"
    seen: list[dict[str, Any]] = []

    def search_notes(inp: dict[str, Any]) -> str:
        seen.append(inp)
        return index

    events = run_chat(client, read_tools={"search_notes": search_notes})
    assert [event.type for event in events] == ["reading", "text", "done"]
    assert events[0].data == {
        "id": "toolu_r",
        "tool": "search_notes",
        "input": {"query": "re:lagrang"},
        "summary": "2 note(s) du deck d",
    }
    assert seen == [{"query": "re:lagrang"}]
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


def test_unknown_attached_source_is_a_context_error() -> None:
    def load_source(source_id: str) -> chat.Attached:
        raise KeyError(f"source inconnue : {source_id}")

    events = run_chat(FakeAnthropic([]), source_ids=["zzzzzz"], load_source=load_source)
    assert [event.type for event in events] == ["error"]
    assert "zzzzzz" in events[0].data["detail"]


def test_default_model_reads_the_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("ANKI_CHAT_MODEL", raising=False)
    assert chat.default_model() == chat.DEFAULT_MODEL
    monkeypatch.setenv("ANKI_CHAT_MODEL", "claude-sonnet-5")
    assert chat.default_model() == "claude-sonnet-5"


# ----------------------------------------------------------------------- routes_chat


def test_scope_query_restricts_to_the_deck_unless_the_query_names_one() -> None:
    from anki_assistant.web.routes_chat import scope_query

    assert scope_query("re:lagrang", "courant::00-Thèse") == '"deck:courant::00-Thèse" (re:lagrang)'
    assert scope_query("deck:courant Text:*KKT*", "d") == "deck:courant Text:*KKT*"
    assert scope_query('"deck:a b" tag:x', "d") == '"deck:a b" tag:x'
    assert scope_query("-deck:autre flag:1", "d") == "-deck:autre flag:1"


def test_search_notes_tool_honours_detail_fields_and_the_full_cap(monkeypatch: Any) -> None:
    from anki_assistant.web import routes_chat

    notes = [
        FakeNote(note_id=1, fields={"Text": "a" * 60_000, "Back Extra": "b"}),
        FakeNote(note_id=2, fields={"Text": "c" * 60_000, "Back Extra": "d"}),
    ]
    anki: Any = SimpleNamespace(find_note_ids=lambda q: [1, 2, 3])
    store: Any = SimpleNamespace()
    no_source: Any = lambda sid: None  # noqa: E731 - read_source is not exercised here
    monkeypatch.setattr(routes_chat.review, "search_notes", lambda a, q, limit: notes)
    tools = routes_chat.read_tools_for(anki, store, "d", no_source)
    search = tools["search_notes"]

    assert search({"query": "x", "detail": "count"}) == "# 3 note(s) correspondent à x"
    assert search({"query": "x"}).startswith("# 2 note(s) du deck d")
    refused = search({"query": "x", "detail": "full"})
    assert "non renvoyé" in refused and "fields" in refused
    narrowed = search({"query": "x", "detail": "full", "fields": ["Back Extra"]})
    assert "Back Extra : b" in narrowed and "aaaa" not in narrowed


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
        json={
            "deck": "d",
            "note_ids": [],
            "source_ids": ["k7q2vd"],
            "messages": [{"role": "user", "content": "salut"}],
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = response.text.strip().splitlines()
    assert lines[0] == "event: error"
    detail = json.loads(lines[1].removeprefix("data: "))["detail"]
    assert "ANTHROPIC_API_KEY" in detail
