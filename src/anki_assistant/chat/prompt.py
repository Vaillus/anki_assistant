"""System prompt assembly and read-tool output formatters.

Builds the four-block system prompt Claude receives each turn
(specs/chat.md#what-claude-receives) and formats the text returned by
the read tools (decks, notes, note types, sources).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from anki_assistant.models import strip_html

from .types import (
    BRIEF_FIELD_CHARS,
    MAX_ATTACHED_CHARS,
    Attached,
    CorpusEntry,
    DeckLike,
    NoteLike,
    NoteTypeLike,
    SourceLike,
    SourceTextLike,
    WorkspaceCard,
)

# Block 1. Only what specs/chat.md lists: language, proposal tools over prose, raw field syntax,
# and the conventions of the collection stated as facts. How to reason about a card (terseness,
# when to read, how many notes an idea deserves) is the conversation's business, not the prompt's.
STANDING_INSTRUCTIONS = """\
You assist the user in reviewing their flagged Anki notes. The user works in a workspace: \
the cards they are looking at (the note the workspace was opened on, drafts prepared for it, \
notes added since) are listed below with their identifier (1, 2…). This prompt also gives you \
the current deck, the note types (names and fields), the index of their corpus, and the \
attached sources. Everything else — other notes in the deck or collection, the text of a \
source that is not attached, the deck tree — is read with the read tools, and whatever is \
nowhere in the collection is searched for on the web.

Rules:
- Reply in the user's language, English by default.
- When the user asks a question, requests an explanation, a check, or information, **reply \
without proposing a change**. Only propose a change if the user explicitly asks for one or \
your answer reveals a clear factual error in a card — and in that case, flag the error first.
- Refer to a card by its workspace identifier (`target: "3"`); a note not yet in the \
workspace is referred to by its Anki id (a large number), and it will be added.
- When you propose a concrete change, **call** the proposal tools (propose_edit, \
propose_split, propose_create, propose_move, propose_add_source, propose_create_source, \
propose_edit_source) instead of describing it in prose. A single turn may contain several; \
the same defect on several notes is one propose_edit per note. Each proposal becomes a \
version or a card that the user reviews, tweaks, or discards, then applies as a batch: you \
never write to Anki directly. To show the user notes without changing them, add_notes adds \
them to the workspace.
- The bracketed markers in the history (`[proposal: …]`, `[read: …]`, `[added: …]`) are \
generated automatically when you call a tool. **Never write them yourself** in your reply: \
they trigger nothing. For a proposal to take effect, always call the matching tool.
- The web (web_search, then web_fetch to read a full page) serves two purposes: checking a \
card against the outside world when the corpus is not enough, and **finding sources to add** \
— an article, a book, a reference page. The corpus is not closed. **As soon as you cite a web \
page in your reply and that page is a good reference for the deck (a Wikipedia article, a \
course, documentation), call propose_add_source with its URL in the same turn** so the user \
can add it to the corpus in one click; the server re-reads the page when it needs it, nothing \
is copied. For a summary you write yourself, use propose_create_source (an Obsidian note in \
the vault). Card content you draw from the web arrives with the source proposal that grounds \
it, never on its own. Do not paste a URL into your reply: passages drawn from the web are \
cited automatically (a numbered reference to the page, a source list under the reply); a bare \
URL is only for recommending a page you have not cited.
- Only read a source (`read_source`) when you genuinely need to for your reply: to verify a \
doubtful fact, correct a factual error, or fill in a field with information missing from the \
card and the context. If the card, the attached sources, and your own knowledge of the topic \
are enough, don't read.
- Fields are **raw** Anki field values: HTML, cloze markers `{{c1::answer}}` or \
`{{c1::answer::hint}}` preserved. Produce your own in the same syntax and keep it valid: \
contiguous numbers starting at c1, balanced braces, at least one cloze in a Cloze note.

Collection conventions:
- Context header: many notes open with a short topic label (e.g. « Stone's Model - FAB and \
KKT Conditions: ») carried by `<div class="context">…</div>` on the field's first line, so a \
cloze read in isolation is not ambiguous. A first line that is the grammatical start of the \
sentence (« There are several ways to: ») is not a header: the wrapper is what makes the \
difference, and the note type's CSS styles it.\
"""


# --------------------------------------------------------------------- formatting helpers


def _num(n: int) -> str:
    """1234567 -> '1,234,567'."""
    return f"{n:,}"


def _fmt_note(note: NoteLike, anchors: Sequence[CorpusEntry] = ()) -> str:
    lines = [
        f"### Note {note.note_id}",
        f"- deck: {note.deck}",
        f"- note type: {note.model}",
        f"- tags: {', '.join(note.tags) if note.tags else '(none)'}",
    ]
    reason = (note.reason or "").strip()
    lines.append(f"- flag reason: {reason}" if reason else "- flag reason: (none)")
    if note.flagged_cards:
        labels = ", ".join(f"c{c.ord + 1}" for c in note.flagged_cards)
        lines.append(f"- flagged card(s): {labels} (the flag was set seeing this cloze hidden)")
    if anchors:
        lines.append("- anchors: " + ", ".join(f"[{entry.id}] {entry.target}" for entry in anchors))
    lines.append("- raw fields:")
    for name, value in note.fields.items():
        lines.append(f"  - {name}: {value}")
    return "\n".join(lines)


def _fmt_card(card: WorkspaceCard, anchors: Sequence[CorpusEntry] = ()) -> str:
    """One card of block 4: identity, states, note facts, the shown version, v0 when changed."""
    if card.note_id is None:
        head = f"### Card {card.wid} — draft, not yet in Anki"
    else:
        head = f"### Card {card.wid} — note {card.note_id}"
    states: list[str] = []
    if card.deleted:
        states.append("marked deleted")
    if card.keep:
        states.append("marked to keep as is")
    if card.defer:
        comment = (card.comment or "").strip()
        flag = "will be created flagged" if card.note_id is None else "flag stays"
        states.append(
            f"marked for later review ({flag}"
            + (f", planned comment: « {comment} »)" if comment else ")")
        )
    if card.move_to:
        states.append(f"to move to {card.move_to}")
    lines = [head]
    if states:
        lines.append(f"- state: {', '.join(states)}")
    if card.parent_wid:
        lines.append(f"- fragment of card {card.parent_wid}")
    lines += [
        f"- deck: {card.deck}",
        f"- note type: {card.model}",
        f"- tags: {', '.join(card.tags) if card.tags else '(none)'}",
    ]
    reason = (card.reason or "").strip()
    if card.note_id is not None:
        lines.append(f"- flag reason: {reason}" if reason else "- flag reason: (none)")
    if card.flagged_clozes:
        labels = ", ".join(f"c{n}" for n in card.flagged_clozes)
        lines.append(f"- flagged card(s): {labels} (the flag was set seeing this cloze hidden)")
    if anchors:
        lines.append("- anchors: " + ", ".join(f"[{entry.id}] {entry.target}" for entry in anchors))
    shown = "- raw fields (shown version)" if card.original_fields else "- raw fields"
    lines.append(f"{shown}:")
    for name, value in card.fields.items():
        lines.append(f"  - {name}: {value}")
    if card.original_fields is not None:
        lines.append("- original version (Anki):")
        for name, value in card.original_fields.items():
            lines.append(f"  - {name}: {value}")
    return "\n".join(lines)


def _index_line(entry: CorpusEntry, note_ids: Sequence[int]) -> str:
    bits = [f"- [{entry.id}] {entry.kind} : {entry.target}"]
    meta: list[str] = []
    if entry.pages:
        meta.append(f"pages {entry.pages}")
    if entry.note:
        meta.append(entry.note)
    if entry.deck:
        meta.append(f"declared on deck {entry.deck}")
    if meta:
        bits.append("(" + ", ".join(meta) + ")")
    if entry.kind == "pdf" and entry.n_pages is not None and not entry.pages:
        meta.append(f"{entry.n_pages} p.")
    if entry.missing:
        bits.append("⚠ file not found")
    if entry.toc:
        _MAX_TOC = 10
        toc_parts = [f"p.{p} {h}" for p, h in entry.toc[:_MAX_TOC]]
        if len(entry.toc) > _MAX_TOC:
            toc_parts.append(f"+{len(entry.toc) - _MAX_TOC}")
        bits.append("— structure: " + " · ".join(toc_parts))
    anchored = [nid for nid in note_ids if nid in entry.anchored_note_ids]
    if anchored:
        bits.append("— anchored to note " + ", ".join(f"#{nid}" for nid in anchored))
    return " ".join(bits)


def _index_block(corpus_index: Sequence[CorpusEntry], note_ids: Sequence[int]) -> str:
    """Block 2: what sources exist, by id. No text — that is what attaching and read_source do."""
    parts = [
        "# Deck corpus — index",
        "",
        "One line per source: [id] kind: target (pages, note). This index does not contain "
        "the sources' text. To read a source: the user attaches it (it then appears under "
        "« Attached sources » below), or you call read_source with its id. For a PDF, check "
        "the structure below first to identify the relevant pages, then call read_source with "
        "the pages parameter (e.g. « 18-21 »). Never read an entire PDF: target the sections "
        "that answer the question.",
        "",
    ]
    if not corpus_index:
        parts.append("(No source is attached to this deck.)")
        return "\n".join(parts)
    parts += [_index_line(entry, note_ids) for entry in corpus_index]
    return "\n".join(parts)


def _source_head(source: SourceLike, text: SourceTextLike, level: int) -> list[str]:
    label = text.extraction or source.kind
    pages_tag = f", pages {source.pages}" if source.pages else ""
    lines = [f"{'#' * level} {source.target} ({label}{pages_tag})"]
    meta = [f"source {source.id}"]
    if source.pages:
        meta.append(f"pages {source.pages}")
    if text.n_pages is not None:
        meta.append(f"{text.n_pages} page(s) extracted")
    if source.note:
        meta.append(source.note)
    if source.deck:
        meta.append(f"declared on deck {source.deck}")
    lines.append(" · ".join(meta))
    if text.warning:
        lines.append(f"⚠ {text.warning}")
    if text.truncated:
        lines.append("⚠ Text already truncated at extraction: the end of the source is missing.")
    return lines


def format_source(source: SourceLike, text: SourceTextLike, level: int = 1) -> str:
    """A source as Claude reads it: header, warnings, then the extracted text.

    The first line — « differentiability (obsidian, 3,200 chars) » — is what the client shows
    as the reading summary. Used by `read_source` and, at `level` 2, by the attached-sources
    block.
    """
    body = text.text or "(no text: file not found or empty)"
    return "\n".join([*_source_head(source, text, level), "", body])


def _attached_block(attached: Sequence[Attached]) -> str:
    """Block 3: the full text of the attached sources, in order, capped at MAX_ATTACHED_CHARS."""
    parts = [
        "# Attached sources",
        "",
        f"Text of the sources the user attached, in order, capped at "
        f"{_num(MAX_ATTACHED_CHARS)} characters total. What is cut off, you cannot see: say "
        "so rather than guess.",
    ]
    if not attached:
        parts += [
            "",
            "(No source attached. The index above says what exists; read_source reads a "
            "source on request.)",
        ]
        return "\n".join(parts)

    remaining = MAX_ATTACHED_CHARS
    for source, text in attached:
        parts += ["", *_source_head(source, text, level=2)]
        body = text.text or ""
        if remaining <= 0:
            parts.append(
                f"⚠ Source omitted: the {_num(MAX_ATTACHED_CHARS)}-character cap was already "
                "reached."
            )
            continue
        if len(body) > remaining:
            body = body[:remaining]
            parts.append(
                f"⚠ Source cut here: the {_num(MAX_ATTACHED_CHARS)}-character cap is reached, "
                "the rest is missing."
            )
        remaining -= len(body)
        parts += ["", body or "(no text: file not found or empty)"]
    return "\n".join(parts)


def _note_types_block(note_types: Mapping[str, Sequence[str]]) -> str:
    """A compact listing of every note type and its fields, so Claude can propose
    type conversions without a tool call."""
    if not note_types:
        return ""
    lines = ["# Note types of the collection", ""]
    for name, fields in sorted(note_types.items()):
        lines.append(f"- **{name}**: {', '.join(fields)}")
    return "\n".join(lines)


def build_system(
    deck: str,
    corpus_index: Sequence[CorpusEntry],
    attached: Sequence[Attached],
    cards: Sequence[WorkspaceCard],
    flagged_count: int | None = None,
    note_types: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """System prompt as four text blocks (specs/chat.md#context).

    1. standing instructions, 2. corpus index, 3. attached sources, 4. deck + workspace cards.
    Block 3 carries `cache_control: ephemeral`: blocks 1–3 depend only on the deck and on what
    the user attached, so a change on the workspace (which only changes block 4) reuses the cache.
    """
    active_cards = [card for card in cards if card.active]
    note_ids = [card.note_id for card in active_cards if card.note_id is not None]

    header = [f"# Current deck\n\n{deck}"]
    if flagged_count is not None:
        header.append(f"Flagged notes in this deck: {flagged_count}.")
    header.append(f"Cards in the workspace: {len(active_cards)} active.")

    types_part = _note_types_block(note_types or {})

    notes_part = ["# Cards of the workspace", ""]
    if active_cards:
        notes_part.append(
            "The first is the note the workspace was opened on (the root). Fields given raw "
            "(HTML and cloze markers included), as the user currently sees them — a proposed "
            "or edited version, not necessarily what Anki holds. Produce your own in the same "
            "syntax."
        )
        for card in active_cards:
            anchors = [entry for entry in corpus_index if entry.id in card.anchor_ids]
            notes_part += ["", _fmt_card(card, anchors)]
    else:
        notes_part.append("(No card.)")

    return [
        {"type": "text", "text": STANDING_INSTRUCTIONS},
        {"type": "text", "text": _index_block(corpus_index, note_ids)},
        {
            "type": "text",
            "text": _attached_block(attached),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": "\n\n".join(header) + "\n\n" + types_part + "\n\n" + "\n".join(notes_part),
        },
    ]


# ------------------------------------------------------------------- read tool output


def _trunc(text: str, limit: int = BRIEF_FIELD_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_decks(decks: Sequence[DeckLike]) -> str:
    """The deck tree: one line per deck, indented by depth, full name kept (needed by move)."""
    lines = [
        "# Decks",
        "",
        "One deck per line, indented by hierarchy. Full name in brackets, then flagged notes: "
        "own / with subdecks.",
        "",
    ]
    for deck in decks:
        indent = "  " * deck.depth
        lines.append(f"{indent}- [{deck.name}]  ⚑ {deck.flagged_own} / {deck.flagged_total}")
    if not decks:
        lines.append("(no deck)")
    return "\n".join(lines)


def format_notes_brief(notes: Sequence[NoteLike], deck: str = "") -> str:
    """`search_notes` in `brief` detail: one line per note, fields as truncated plain text."""
    where = f" in deck {deck}" if deck else ""
    lines = [
        f"# {len(notes)} note(s){where}",
        "",
        f"One line per note: #id · deck (if different) · ⚑ if flagged · fields as plain text "
        f"truncated to {BRIEF_FIELD_CHARS} characters. get_notes (or search_notes with "
        "detail=full) gives the complete raw fields.",
        "",
    ]
    for note in notes:
        bits = [f"#{note.note_id}"]
        if note.deck and note.deck != deck:
            bits.append(note.deck)
        if note.flagged_cards:
            bits.append("⚑")
        fields = " | ".join(
            f"{name}: {_trunc(strip_html(value))}"
            for name, value in note.fields.items()
            if strip_html(value)
        )
        bits.append(fields or "(empty fields)")
        lines.append(" · ".join(bits))
    if not notes:
        lines.append("(no note)")
    return "\n".join(lines)


def format_notes(notes: Sequence[NoteLike]) -> str:
    """Full notes, same layout as the notes in context."""
    if not notes:
        return "(no note found)"
    return "\n\n".join(_fmt_note(note) for note in notes)


def format_note_type(note_type: NoteTypeLike) -> str:
    lines = [
        f"# Note type {note_type.name}",
        "",
        f"Fields: {', '.join(note_type.fields) if note_type.fields else '(none)'}",
    ]
    for card, sides in note_type.templates.items():
        lines += ["", f"## Card {card}"]
        for side, label in (("Front", "Front"), ("Back", "Back")):
            lines += ["", f"### {label}", "```html", sides.get(side, ""), "```"]
    lines += ["", "## CSS", "```css", note_type.css, "```"]
    return "\n".join(lines)
