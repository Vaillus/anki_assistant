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


def server_tool_use_block(block_id: str, name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    """A web tool call as Anthropic reports it — never dispatched by us, only reported."""
    return SimpleNamespace(type="server_tool_use", id=block_id, name=name, input=tool_input)


def web_result(url: str, title: str = "") -> SimpleNamespace:
    return SimpleNamespace(type="web_search_result", url=url, title=title)


def web_result_block(tool_use_id: str, content: Any, tool: str = "web_search") -> SimpleNamespace:
    return SimpleNamespace(type=f"{tool}_tool_result", tool_use_id=tool_use_id, content=content)


def web_error_block(tool_use_id: str, code: str, tool: str = "web_search") -> SimpleNamespace:
    return web_result_block(
        tool_use_id, SimpleNamespace(type=f"{tool}_tool_result_error", error_code=code), tool
    )


def block_start(content_block: Any) -> SimpleNamespace:
    return SimpleNamespace(type="content_block_start", content_block=content_block)


def text_start() -> SimpleNamespace:
    return block_start(SimpleNamespace(type="text", text=""))


def block_stop() -> SimpleNamespace:
    return SimpleNamespace(type="content_block_stop")


def citation_delta(**citation: Any) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="citations_delta", citation=SimpleNamespace(**citation)),
    )


def search_citation(url: str, title: str = "", cited_text: str = "…") -> SimpleNamespace:
    return citation_delta(
        type="web_search_result_location", url=url, title=title, cited_text=cited_text
    )


def fetch_citation(title: str, index: int, cited_text: str = "…") -> SimpleNamespace:
    return citation_delta(
        type="char_location", document_title=title, document_index=index, cited_text=cited_text
    )


def fetched_page(tool_use_id: str, url: str, title: str) -> SimpleNamespace:
    document = SimpleNamespace(
        type="web_fetch_result", url=url, content=SimpleNamespace(type="document", title=title)
    )
    return web_result_block(tool_use_id, document, tool="web_fetch")


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


def card(**overrides: Any) -> chat.WorkspaceCard:
    """The root card of a workspace opened on the note of `FakeNote`."""
    note = FakeNote()
    base: dict[str, Any] = {
        "wid": "w1",
        "note_id": note.note_id,
        "fields": dict(note.fields),
        "deck": note.deck,
        "model": note.model,
        "tags": list(note.tags),
        "flagged_clozes": [2],
        "reason": note.reason,
        "anchor_ids": ["k7q2vd"],
    }
    base.update(overrides)
    return chat.WorkspaceCard(**base)


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
        "cards": [card()],
        "source_ids": [],
        "messages": [{"role": "user", "content": "Cette carte est trop vague, tu proposes quoi ?"}],
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
        [
            card(original_fields={"Text": "avant", "Back Extra": ""}),
            card(wid="w2", note_id=None, parent_wid="w1", active=False, flagged_clozes=[]),
        ],
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
    assert "Cartes dans l'espace de travail : 2, dont 1 active(s)." in context
    assert f"### Carte w1 — note {NOTE_ID}" in context
    assert "- état : active" in context
    assert "{{c1::disjoint}}" in context  # raw fields, cloze markers kept
    assert "champs bruts (version affichée)" in context
    assert "version d'origine (Anki) :\n  - Text : avant" in context
    assert "raison du flag : For a given sensor ?" in context
    assert "carte(s) flaguée(s) : c2" in context
    assert "ancres : [k7q2vd] Allocation sur des angles disjoints" in context
    assert "### Carte w2 — brouillon, pas encore dans Anki" in context
    assert "- état : inactive" in context
    assert "fragment de la carte w1" in context
    assert context.index("Carte w1") < context.index("Carte w2")


def test_standing_instructions_say_only_what_the_spec_lists() -> None:
    text = chat.build_system("d", [], [], [])[0]["text"]
    for needle in ("français", "propose_create_source", "propose_edit_source", "cloze", "actives"):
        assert needle in text
    assert 'class="context"' in text  # conventions of the collection, stated as facts
    # Not the prompt's business (specs/chat.md#what-claude-receives).
    for absent in ("Sois bref", "Une note = une idée", "propose_bulk_edit", "Avant propose_create"):
        assert absent not in text


def test_empty_index_and_no_attached_source_are_said_explicitly() -> None:
    blocks = chat.build_system("d", [], [], [])
    assert "Aucune source n'est associée" in blocks[1]["text"]
    assert "Aucune source jointe" in blocks[2]["text"]
    assert "read_source" in blocks[2]["text"]
    assert "(Aucune carte.)" in blocks[3]["text"]


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


def test_tools_are_the_proposals_then_the_read_tools_then_the_web_tools() -> None:
    defs = chat.tools()
    names = [tool["name"] for tool in defs]
    proposals = [
        "propose_edit",
        "propose_split",
        "propose_create",
        "propose_move",
        "propose_create_source",
        "propose_edit_source",
    ]
    assert names[: len(proposals)] == proposals
    assert set(proposals) == set(chat.TOOL_KINDS)
    assert tuple(names[len(proposals) : -len(chat.WEB_TOOLS)]) == chat.READ_TOOLS
    assert chat.READ_TOOLS == (
        "list_decks",
        "search_notes",
        "get_notes",
        "add_notes",
        "get_note_type",
        "read_source",
    )
    assert tuple(names[-len(chat.WEB_TOOLS) :]) == chat.WEB_TOOLS
    assert not (set(chat.READ_TOOLS) & set(chat.TOOL_KINDS))
    # The web tools are Anthropic-defined: a `type`, no schema of ours (specs/chat.md#web-tools).
    assert chat.web_tool_defs() == [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": chat.MAX_WEB_SEARCHES,
            "allowed_callers": ["direct"],
        },
        {
            "type": "web_fetch_20260209",
            "name": "web_fetch",
            "max_uses": chat.MAX_WEB_FETCHES,
            "allowed_callers": ["direct"],
            "citations": {"enabled": True},
        },
    ]
    for tool in chat.proposal_tools() + chat.read_tool_defs():
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        assert tool["description"]
    for tool in chat.proposal_tools():
        assert "rationale" in tool["input_schema"]["required"]


def test_tool_schemas_match_the_spec_tables() -> None:
    by_name = {
        tool["name"]: tool["input_schema"] for tool in chat.proposal_tools() + chat.read_tool_defs()
    }
    assert by_name["propose_edit"]["properties"]["fields"]["additionalProperties"] == {
        "type": "string"
    }
    assert by_name["propose_edit"]["required"] == ["target", "fields", "rationale"]
    assert by_name["propose_edit"]["properties"]["target"]["type"] == "string"
    assert by_name["propose_move"]["required"] == ["target", "deck", "rationale"]
    assert "propose_bulk_edit" not in by_name
    assert "source_ids" in by_name["propose_create"]["properties"]
    split_new = by_name["propose_split"]["properties"]["new_notes"]
    assert split_new["items"]["required"] == ["fields"]
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
    assert by_name["add_notes"]["required"] == ["note_ids", "rationale"]
    assert str(chat.MAX_CARDS) in next(
        tool["description"] for tool in chat.tools() if tool["name"] == "add_notes"
    )
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
        "toolu_01", "propose_split", {"target": "w1", "original": None, "new_notes": []}
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
        tool_use_block("toolu_a", "propose_edit", {"target": "w1", "fields": {}, "rationale": "r"}),
        tool_use_block("toolu_b", "propose_move", {"target": "w1", "deck": "x", "rationale": "r"}),
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


def _one_tool_turn(block: Any) -> FakeAnthropic:
    return FakeAnthropic(
        [([], final_message([block], "tool_use")), ([], final_message([], "end_turn"))]
    )


def _first_result(client: FakeAnthropic) -> dict[str, Any]:
    return client.messages.calls[1]["messages"][2]["content"][0]


def test_unknown_target_is_an_error_result_and_no_event() -> None:
    block = tool_use_block(
        "toolu_x", "propose_edit", {"target": "w9", "fields": {}, "rationale": "r"}
    )
    client = _one_tool_turn(block)
    events = run_chat(client)
    assert [event.type for event in events] == ["done"]
    result = _first_result(client)
    assert result["is_error"] is True
    assert "w9" in result["content"]


def test_note_id_target_absent_from_the_workspace_is_admitted() -> None:
    block = tool_use_block(
        "toolu_n", "propose_edit", {"target": "424242", "fields": {}, "rationale": "r"}
    )
    client = _one_tool_turn(block)
    events = run_chat(client)
    assert [event.type for event in events] == ["proposal", "done"]
    assert events[0].data["input"]["target"] == "424242"
    assert _first_result(client)["content"] == "ok"


def test_absent_target_is_refused_when_the_workspace_is_full() -> None:
    full = [card(wid=f"w{i}", note_id=i) for i in range(1, chat.MAX_CARDS + 1)]
    block = tool_use_block(
        "toolu_f", "propose_edit", {"target": "424242", "fields": {}, "rationale": "r"}
    )
    client = _one_tool_turn(block)
    events = run_chat(client, cards=full)
    assert [event.type for event in events] == ["done"]
    assert "plein" in _first_result(client)["content"]
    # A card already in the workspace is still a valid target at the cap.
    block2 = tool_use_block(
        "toolu_g", "propose_edit", {"target": "w3", "fields": {}, "rationale": "r"}
    )
    client2 = _one_tool_turn(block2)
    assert [e.type for e in run_chat(client2, cards=full)] == ["proposal", "done"]


def test_add_notes_runs_get_notes_and_emits_an_added_event() -> None:
    block = tool_use_block(
        "toolu_add", "add_notes", {"note_ids": [7, 8], "rationale": "même défaut"}
    )
    client = _one_tool_turn(block)
    seen: list[dict[str, Any]] = []

    def get_notes(inp: dict[str, Any]) -> str:
        seen.append(dict(inp))
        return "# 2 notes\n…"

    events = run_chat(client, read_tools={"get_notes": get_notes})
    assert [event.type for event in events] == ["added", "done"]
    assert events[0].data == {"id": "toolu_add", "note_ids": [7, 8], "rationale": "même défaut"}
    assert seen == [{"note_ids": [7, 8], "rationale": "même défaut"}]
    assert _first_result(client)["content"] == "# 2 notes\n…"


def test_add_notes_over_the_cap_is_refused_without_reading() -> None:
    full = [card(wid=f"w{i}", note_id=i) for i in range(1, chat.MAX_CARDS)]  # one seat left
    block = tool_use_block("toolu_add", "add_notes", {"note_ids": [700, 800], "rationale": "r"})
    client = _one_tool_turn(block)
    calls: list[Any] = []
    events = run_chat(client, cards=full, read_tools={"get_notes": lambda inp: calls.append(inp)})
    assert [event.type for event in events] == ["done"]
    assert calls == []
    assert "plein" in _first_result(client)["content"]
    # Notes already in the workspace do not count: re-adding w1's note is fine.
    block2 = tool_use_block("toolu_ok", "add_notes", {"note_ids": [1, 700], "rationale": "r"})
    client2 = _one_tool_turn(block2)
    events2 = run_chat(client2, cards=full, read_tools={"get_notes": lambda inp: "# 2 notes"})
    assert [event.type for event in events2] == ["added", "done"]


def test_add_notes_failure_is_an_error_result_and_no_added_event() -> None:
    block = tool_use_block("toolu_add", "add_notes", {"note_ids": [7], "rationale": "r"})
    client = _one_tool_turn(block)

    def boom(inp: dict[str, Any]) -> str:
        raise KeyError("note inconnue")

    events = run_chat(client, read_tools={"get_notes": boom})
    assert [event.type for event in events] == ["reading", "done"]
    assert events[0].data["summary"].startswith("erreur")
    assert _first_result(client)["is_error"] is True


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
        tool_use_block("toolu_a", "propose_edit", {"target": "w1", "fields": {}, "rationale": "r"}),
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


# ----------------------------------------------------------------------------- web tools


def test_web_search_reading_names_the_query_and_spells_out_the_urls() -> None:
    call = server_tool_use_block("srvtoolu_1", "web_search", {"query": "conditions KKT"})
    results = [web_result(f"https://ex{i}.org/p") for i in range(1, 8)]
    client = FakeAnthropic(
        [([], final_message([call, web_result_block("srvtoolu_1", results)], "end_turn"))]
    )
    events = run_chat(client)

    assert [event.type for event in events] == ["reading", "done"]
    reading = events[0].data
    assert reading == {
        "id": "srvtoolu_1",
        "tool": "web_search",
        "input": {"query": "conditions KKT"},
        "summary": reading["summary"],
    }
    # The URLs are the provenance guarantee; past WEB_URLS_SHOWN the rest is counted.
    assert reading["summary"].startswith("« conditions KKT » → 7 résultat(s) : https://ex1.org/p")
    assert "https://ex5.org/p" in reading["summary"]
    assert "https://ex6.org/p" not in reading["summary"]
    assert reading["summary"].endswith("(+2)")


def test_web_search_without_results_says_so() -> None:
    call = server_tool_use_block("srvtoolu_1", "web_search", {"query": "rien"})
    client = FakeAnthropic(
        [([], final_message([call, web_result_block("srvtoolu_1", [])], "end_turn"))]
    )
    events = run_chat(client)
    assert events[0].data["summary"] == "« rien » → aucun résultat"


def test_web_tool_error_is_a_reading_not_a_chat_error() -> None:
    call = server_tool_use_block("srvtoolu_1", "web_search", {"query": "x"})
    failed = web_error_block("srvtoolu_1", "max_uses_exceeded")
    client = FakeAnthropic([([], final_message([call, failed], "end_turn"))])
    events = run_chat(client)
    assert [event.type for event in events] == ["reading", "done"]
    assert events[0].data["summary"] == "« x » → erreur : max_uses_exceeded"


def test_web_fetch_succeeds_with_a_single_object_and_reads_as_its_url() -> None:
    url = "https://en.wikipedia.org/wiki/Karush-Kuhn-Tucker_conditions"
    call = server_tool_use_block("srvtoolu_2", "web_fetch", {"url": url})
    document = SimpleNamespace(type="web_fetch_result", url=url, content="…")
    client = FakeAnthropic(
        [
            (
                [],
                final_message(
                    [call, web_result_block("srvtoolu_2", document, tool="web_fetch")], "end_turn"
                ),
            )
        ]
    )
    events = run_chat(client)
    assert [event.type for event in events] == ["reading", "done"]
    assert events[0].data == {
        "id": "srvtoolu_2",
        "tool": "web_fetch",
        "input": {"url": url},
        "summary": url,
    }


def test_pause_turn_resumes_with_the_assistant_message_unchanged() -> None:
    """A long search: the API pauses, and the trailing call block is what resumes it.

    Also covers the deferred case — the call lands in one message and its result in the next,
    and the reading still names the query.
    """
    call = server_tool_use_block("srvtoolu_1", "web_search", {"query": "conditions KKT"})
    paused = final_message([text_block("Je cherche."), call], "pause_turn")
    resumed = final_message(
        [web_result_block("srvtoolu_1", [web_result("https://ex.org/kkt")])], "end_turn"
    )
    client = FakeAnthropic([([], paused), ([text_delta("Voilà.")], resumed)])
    events = run_chat(client)

    # Text streams as it is generated; the search is only known from the final message. The log
    # renders reads above the body either way (workspace.js#msgHtml).
    assert [event.type for event in events] == ["text", "reading", "done"]
    assert events[1].data["summary"] == "« conditions KKT » → 1 résultat(s) : https://ex.org/kkt"
    assert events[-1].data["stop_reason"] == "end_turn"

    # Resumed with the assistant message verbatim (encrypted content intact) and nothing added.
    assert len(client.messages.calls) == 2
    convo = client.messages.calls[1]["messages"]
    assert convo[-1] == {"role": "assistant", "content": paused.content}


def test_a_web_call_without_its_result_yet_emits_nothing() -> None:
    """Mixed batch: the API defers the search, so only the local tool result comes back now."""
    call = server_tool_use_block("srvtoolu_1", "web_search", {"query": "conditions KKT"})
    local = tool_use_block("toolu_1", "list_decks", {})
    client = FakeAnthropic(
        [
            ([], final_message([call, local], "tool_use")),
            ([text_delta("ok")], final_message([], "end_turn")),
        ]
    )
    events = run_chat(client, read_tools={"list_decks": lambda _inp: "# 2 decks"})
    assert [event.type for event in events] == ["reading", "text", "done"]
    assert events[0].data["tool"] == "list_decks"


# ------------------------------------------------------------------------------ citations


def test_web_tools_are_called_directly_and_fetch_is_cited() -> None:
    """Filtered calls (the default) run inside code execution and yield no citations at all."""
    defs = {tool["name"]: tool for tool in chat.web_tool_defs()}
    assert defs["web_search"]["allowed_callers"] == ["direct"]
    assert defs["web_fetch"]["allowed_callers"] == ["direct"]
    assert defs["web_fetch"]["citations"] == {"enabled": True}


def test_search_citations_number_the_pages_by_url_and_land_where_their_block_closes() -> None:
    wiki, blog = "https://en.wikipedia.org/wiki/KKT", "https://blog.example/kkt"
    events_in = [
        text_start(),
        text_delta("Les conditions KKT généralisent Lagrange"),
        search_citation(wiki, title="KKT - Wikipedia", cited_text="The KKT conditions…"),
        block_stop(),
        text_start(),
        search_citation(blog, title="Notes on KKT"),  # before its text: still lands after it
        text_delta(". Elles sont nécessaires sous qualification"),
        block_stop(),
        text_start(),
        text_delta(", et suffisantes en convexe"),
        search_citation(wiki, title="KKT - Wikipedia", cited_text="In the convex case…"),
        # No block_stop: the stream ending flushes the pending citation all the same.
    ]
    client = FakeAnthropic([(events_in, final_message([], "end_turn"))])
    events = run_chat(client)

    # Each citation is emitted when its block closes, after the block's text, so the client's
    # text ends where the marker goes; the same page keeps its number across the turn.
    assert [event.type for event in events] == [
        "text", "citation", "text", "citation", "text", "citation", "done"
    ]  # fmt: skip
    cites = [event.data for event in events if event.type == "citation"]
    assert cites[0] == {
        "n": 1,
        "url": wiki,
        "title": "KKT - Wikipedia",
        "cited_text": "The KKT conditions…",
    }
    assert cites[1]["n"] == 2 and cites[1]["url"] == blog
    assert cites[2]["n"] == 1 and cites[2]["cited_text"] == "In the convex case…"


def test_cited_text_is_collapsed_and_capped_for_the_tooltip() -> None:
    long = "In\n[mathematical optimization](https://x)   the KKT conditions " * 8
    events_in = [text_delta("KKT"), search_citation("https://ex.org", cited_text=long)]
    client = FakeAnthropic([(events_in, final_message([], "end_turn"))])
    cited = [e for e in run_chat(client) if e.type == "citation"][0].data["cited_text"]
    assert "\n" not in cited and "   " not in cited
    assert len(cited) == chat.CITED_TEXT_CHARS
    assert cited.endswith("…")


def test_fetch_citations_resolve_to_the_fetched_page_by_title_then_by_index() -> None:
    first, second = "https://ex.org/a", "https://ex.org/b"
    events_in = [
        block_start(fetched_page("srvtoolu_1", first, "Page A")),
        block_stop(),
        block_start(fetched_page("srvtoolu_2", second, "Page B")),
        block_stop(),
        text_start(),
        text_delta("Selon B"),
        fetch_citation("Page B", index=0),  # the title wins over a wrong index
        block_stop(),
        text_start(),
        text_delta(", et selon A"),
        fetch_citation("", index=0),  # no title: the index into the turn's fetches
        block_stop(),
        text_start(),
        text_delta(", et d'après rien"),
        fetch_citation("Unknown", index=7),  # unresolvable: dropped, no marker
        block_stop(),
    ]
    client = FakeAnthropic([(events_in, final_message([], "end_turn"))])
    events = run_chat(client)

    cites = [event.data for event in events if event.type == "citation"]
    assert [(c["n"], c["url"], c["title"]) for c in cites] == [
        (1, second, "Page B"),
        (2, first, "Page A"),
    ]


def test_a_deferred_fetch_is_known_from_the_final_message_for_later_citations() -> None:
    url = "https://ex.org/kkt"
    call = server_tool_use_block("srvtoolu_1", "web_fetch", {"url": url})
    paused = final_message([call, fetched_page("srvtoolu_1", url, "KKT")], "pause_turn")
    client = FakeAnthropic(
        [
            ([], paused),
            ([text_delta("Donc"), fetch_citation("KKT", index=0)], final_message([], "end_turn")),
        ]
    )
    events = run_chat(client)
    assert [event.type for event in events] == ["reading", "text", "citation", "done"]
    assert events[2].data["url"] == url


def test_api_failure_becomes_an_error_event() -> None:
    client = SimpleNamespace(messages=BoomMessages())
    events = run_chat(client)
    assert [event.type for event in events] == ["error"]
    assert "connection lost" in events[0].data["detail"]


def test_context_failure_becomes_an_error_event() -> None:
    def boom(deck: str) -> Any:
        raise RuntimeError("Anki injoignable")

    events = run_chat(FakeAnthropic([]), load_corpus=boom)
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
            "cards": [],
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
