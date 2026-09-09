"""Claude chat backend: prompt assembly, tools, streaming. Spec: specs/chat.md.

No FastAPI or Anki imports here — `web/routes_chat.py` turns `ChatEvent`s into SSE lines and
builds the read tools. From `sources.py` only `new_source_id` is used (a pure function), so that
the create-source proposal can announce the id the source will carry once the user applies it.

Model choice
------------
Default model id is `claude-opus-4-6` (see `DEFAULT_MODEL`, and specs/chat.md#llm-configuration),
overridable with `ANKI_CHAT_MODEL` — set that rather than editing this.

Note: when thinking is enabled, thinking tokens count against `max_tokens` (8192 per the spec).
If replies ever come back truncated, lower the effort or raise `max_tokens`.

Context is not fetched here: `stream_chat` takes injectable callables (`load_note`,
`load_corpus`, `load_source`, and the `read_tools` map) so this module imports neither
`review.py` nor the Anki client. It only imports `models.strip_html`, a pure function.

Two kinds of tools (specs/chat.md):
- **proposal tools** (`propose_*`): the call is streamed to the client as a `proposal` event and
  answered with "ok"; applying is the user's click, never Claude's.
- **read tools** (`list_decks`, `search_notes`, `get_notes`, `get_note_type`, `read_source`):
  executed here through the injected callables, the text they return is the tool result. A
  `reading` event tells the client what was read.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from anki_assistant.models import strip_html
from anki_assistant.sources import new_source_id

# --------------------------------------------------------------------------- config

DEFAULT_MODEL = "claude-opus-4-6"
MAX_TOKENS = 8192
#: Total budget for the attached sources block (block 3 of the system prompt).
MAX_ATTACHED_CHARS = 150_000
#: A `search_notes` result in `full` detail longer than this is refused with the count instead.
MAX_FULL_RESULT_CHARS = 100_000
#: Model calls per turn. Reads count: search -> get_notes -> propose -> comment is four calls.
MAX_TOOL_LOOPS = 8
#: Characters kept per field in a `brief` line.
BRIEF_FIELD_CHARS = 120


def default_model() -> str:
    """Model id for the chat: `ANKI_CHAT_MODEL` if set and non-empty, else `DEFAULT_MODEL`."""
    return (os.environ.get("ANKI_CHAT_MODEL") or "").strip() or DEFAULT_MODEL


# ----------------------------------------------------------------- injected interfaces


class NoteLike(Protocol):
    """The shape `review.NoteView` exposes; only what the prompt needs."""

    note_id: int
    deck: str
    model: str
    tags: list[str]
    fields: dict[str, str]
    reason: str

    @property
    def flagged_cards(self) -> Sequence[FlaggedCardLike]: ...


class FlaggedCardLike(Protocol):
    """One flagged card of a note; `ord` + 1 is the cloze number on a Cloze note."""

    ord: int


class SourceLike(Protocol):
    """The shape `sources.Source` exposes; only what a source header needs.

    Read-only properties, not attributes: `Source.kind` is a `Literal`, which only satisfies a
    `str` member when that member cannot be written to.
    """

    @property
    def id(self) -> str: ...
    @property
    def deck(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def target(self) -> str: ...
    @property
    def pages(self) -> str: ...
    @property
    def note(self) -> str: ...


class SourceTextLike(Protocol):
    """The shape `sources.SourceText` exposes."""

    text: str
    truncated: bool
    n_pages: int | None
    warning: str


@dataclass
class CorpusEntry:
    """One line of the corpus index (block 2): a source's header, never its text."""

    id: str
    kind: str
    target: str
    pages: str = ""
    note: str = ""
    deck: str = ""
    missing: bool = False
    #: Notes anchored to this source; the index marks those that are in context.
    anchored_note_ids: list[int] = field(default_factory=list)


class DeckLike(Protocol):
    """The shape `review.DeckSummary` exposes; only what `format_decks` needs."""

    name: str
    depth: int
    flagged_own: int
    flagged_total: int


class NoteTypeLike(Protocol):
    """The shape `models.NoteType` exposes."""

    name: str
    fields: list[str]
    templates: dict[str, dict[str, str]]
    css: str


NoteLoader = Callable[[int], NoteLike]
CorpusLoader = Callable[[str], list[CorpusEntry]]
#: An attached source, loaded: the source and its extracted text.
Attached = tuple[SourceLike, SourceTextLike]
SourceLoader = Callable[[str], Attached]
#: A read tool: takes the tool input as given by the model, returns the text Claude will read.
ReadTool = Callable[[Mapping[str, Any]], str]


@dataclass
class ChatEvent:
    """One SSE event. `type` is one of "text", "reading", "proposal", "done", "error"."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------ system prompt

# Block 1. Only what specs/chat.md lists: language, proposal tools over prose, raw field syntax,
# and the conventions of the collection stated as facts. How to reason about a card (terseness,
# when to read, how many notes an idea deserves) is the conversation's business, not the prompt's.
STANDING_INSTRUCTIONS = """\
Tu assistes Hugo dans la revue de ses notes Anki signalées (« flaguées »). Ce prompt te donne le \
deck en cours, l'index de son corpus, les sources que l'utilisateur a jointes et les notes en \
cours d'examen. Le reste — les autres notes du deck ou de la collection, le texte d'une source \
non jointe, l'arborescence des decks, un type de note — se lit avec les outils de lecture.

Règles :
- Réponds dans la langue de l'utilisateur, français par défaut.
- Quand tu proposes un changement concret, utilise les outils de proposition (propose_edit, \
propose_split, propose_create, propose_move, propose_bulk_edit, propose_create_source, \
propose_edit_source) au lieu de le décrire en prose. Un même tour peut en contenir plusieurs. \
L'utilisateur applique lui-même chaque proposition d'un clic : tu n'écris jamais dans Anki ni \
dans le vault.
- Les champs sont des valeurs de champ Anki **brutes** : HTML, marqueurs de cloze \
`{{c1::réponse}}` ou `{{c1::réponse::indice}}` conservés. Produis les tiens dans la même syntaxe \
et garde-la valide : numéros contigus à partir de c1, accolades équilibrées, au moins un cloze \
dans une note Cloze.

Conventions de la collection :
- En-tête de contexte : beaucoup de notes commencent par une courte étiquette de sujet (ex. \
« Stone's Model - FAB and KKT Conditions: ») portée par `<div class="context">…</div>` en \
première ligne du champ, pour qu'un cloze lu isolément ne soit pas ambigu. Une première ligne \
qui est le début grammatical de la phrase (« There are several ways to: ») n'est pas un \
en-tête : c'est le wrapper qui fait la différence, et le CSS du type de note le style.\
"""


def _num(n: int) -> str:
    """1234567 -> '1 234 567' (the French thousands separator, as in the UI copy)."""
    return f"{n:,}".replace(",", " ")


def _fmt_note(note: NoteLike, anchors: Sequence[CorpusEntry] = ()) -> str:
    lines = [
        f"### Note {note.note_id}",
        f"- deck : {note.deck}",
        f"- type de note : {note.model}",
        f"- tags : {', '.join(note.tags) if note.tags else '(aucun)'}",
    ]
    reason = (note.reason or "").strip()
    lines.append(f"- raison du flag : {reason}" if reason else "- raison du flag : (aucune)")
    if note.flagged_cards:
        labels = ", ".join(f"c{c.ord + 1}" for c in note.flagged_cards)
        lines.append(
            f"- carte(s) flaguée(s) : {labels} (le flag a été posé en voyant ce cloze masqué)"
        )
    if anchors:
        lines.append("- ancres : " + ", ".join(f"[{entry.id}] {entry.target}" for entry in anchors))
    lines.append("- champs bruts :")
    for name, value in note.fields.items():
        lines.append(f"  - {name} : {value}")
    return "\n".join(lines)


def _index_line(entry: CorpusEntry, note_ids: Sequence[int]) -> str:
    bits = [f"- [{entry.id}] {entry.kind} : {entry.target}"]
    meta: list[str] = []
    if entry.pages:
        meta.append(f"pages {entry.pages}")
    if entry.note:
        meta.append(entry.note)
    if entry.deck:
        meta.append(f"déclarée sur le deck {entry.deck}")
    if meta:
        bits.append("(" + ", ".join(meta) + ")")
    if entry.missing:
        bits.append("⚠ fichier introuvable")
    anchored = [nid for nid in note_ids if nid in entry.anchored_note_ids]
    if anchored:
        bits.append("— ancrée à la note " + ", ".join(f"#{nid}" for nid in anchored))
    return " ".join(bits)


def _index_block(corpus_index: Sequence[CorpusEntry], note_ids: Sequence[int]) -> str:
    """Block 2: what sources exist, by id. No text — that is what attaching and read_source do."""
    parts = [
        "# Corpus du deck — index",
        "",
        "Une ligne par source : [id] type : cible (pages, note). Cet index ne contient pas le "
        "texte des sources. Pour lire une source : l'utilisateur la joint (elle apparaît alors "
        "dans « Sources jointes » ci-dessous), ou tu appelles read_source avec son id.",
        "",
    ]
    if not corpus_index:
        parts.append("(Aucune source n'est associée à ce deck.)")
        return "\n".join(parts)
    parts += [_index_line(entry, note_ids) for entry in corpus_index]
    return "\n".join(parts)


def _source_head(source: SourceLike, text: SourceTextLike, level: int) -> list[str]:
    n = len(text.text or "")
    lines = [f"{'#' * level} {source.target} ({source.kind}, {_num(n)} car.)"]
    meta = [f"source {source.id}"]
    if source.pages:
        meta.append(f"pages {source.pages}")
    if text.n_pages is not None:
        meta.append(f"{text.n_pages} page(s) extraite(s)")
    if source.note:
        meta.append(source.note)
    if source.deck:
        meta.append(f"déclarée sur le deck {source.deck}")
    lines.append(" · ".join(meta))
    if text.warning:
        lines.append(f"⚠ {text.warning}")
    if text.truncated:
        lines.append("⚠ Texte déjà tronqué à l'extraction : la fin de la source manque.")
    return lines


def format_source(source: SourceLike, text: SourceTextLike, level: int = 1) -> str:
    """A source as Claude reads it: header, warnings, then the extracted text.

    The first line — « différentiabilité (obsidian, 3 200 car.) » — is what the client shows as
    the reading summary. Used by `read_source` and, at `level` 2, by the attached-sources block.
    """
    body = text.text or "(aucun texte : fichier introuvable ou vide)"
    return "\n".join([*_source_head(source, text, level), "", body])


def _attached_block(attached: Sequence[Attached]) -> str:
    """Block 3: the full text of the attached sources, in order, capped at MAX_ATTACHED_CHARS."""
    parts = [
        "# Sources jointes",
        "",
        f"Texte des sources jointes par l'utilisateur, dans l'ordre, plafonné à "
        f"{_num(MAX_ATTACHED_CHARS)} caractères au total. Ce qui est coupé, tu ne peux pas le "
        "voir : dis-le plutôt que de deviner.",
    ]
    if not attached:
        parts += [
            "",
            "(Aucune source jointe. L'index ci-dessus dit ce qui existe ; read_source lit une "
            "source à la demande.)",
        ]
        return "\n".join(parts)

    remaining = MAX_ATTACHED_CHARS
    for source, text in attached:
        parts += ["", *_source_head(source, text, level=2)]
        body = text.text or ""
        if remaining <= 0:
            parts.append(
                f"⚠ Source omise : le plafond de {_num(MAX_ATTACHED_CHARS)} caractères était "
                "déjà atteint."
            )
            continue
        if len(body) > remaining:
            body = body[:remaining]
            parts.append(
                f"⚠ Source coupée ici : le plafond de {_num(MAX_ATTACHED_CHARS)} caractères est "
                "atteint, la suite manque."
            )
        remaining -= len(body)
        parts += ["", body or "(aucun texte : fichier introuvable ou vide)"]
    return "\n".join(parts)


def build_system(
    deck: str,
    corpus_index: Sequence[CorpusEntry],
    attached: Sequence[Attached],
    notes: Sequence[NoteLike],
    flagged_count: int | None = None,
) -> list[dict[str, Any]]:
    """System prompt as four text blocks (specs/chat.md#context).

    1. standing instructions, 2. corpus index, 3. attached sources, 4. deck + notes in context.
    Block 3 carries `cache_control: ephemeral`: blocks 1–3 depend only on the deck and on what
    the user attached, so moving to the next note (which only changes block 4) reuses the cache.
    """
    note_ids = [note.note_id for note in notes]

    header = [f"# Deck en cours\n\n{deck}"]
    if flagged_count is not None:
        header.append(f"Notes signalées dans ce deck : {flagged_count}.")
    header.append(f"Notes en contexte : {len(notes)}.")

    notes_part = ["# Notes en cours d'examen", ""]
    if notes:
        notes_part.append(
            "La première est la note sélectionnée. Champs donnés bruts (HTML et marqueurs de "
            "cloze compris). Produis les tiens dans la même syntaxe."
        )
        for note in notes:
            anchors = [entry for entry in corpus_index if note.note_id in entry.anchored_note_ids]
            notes_part += ["", _fmt_note(note, anchors)]
    else:
        notes_part.append("(Aucune note sélectionnée.)")

    return [
        {"type": "text", "text": STANDING_INSTRUCTIONS},
        {"type": "text", "text": _index_block(corpus_index, note_ids)},
        {
            "type": "text",
            "text": _attached_block(attached),
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "\n\n".join(header) + "\n\n" + "\n".join(notes_part)},
    ]


# ------------------------------------------------------------------- read tool output


def _trunc(text: str, limit: int = BRIEF_FIELD_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_decks(decks: Sequence[DeckLike]) -> str:
    """The deck tree: one line per deck, indented by depth, full name kept (needed by move)."""
    lines = [
        "# Decks",
        "",
        "Un deck par ligne, indenté selon la hiérarchie. Nom complet entre crochets, puis notes "
        "flaguées : propres / avec sous-decks.",
        "",
    ]
    for deck in decks:
        indent = "  " * deck.depth
        lines.append(f"{indent}- [{deck.name}]  ⚑ {deck.flagged_own} / {deck.flagged_total}")
    if not decks:
        lines.append("(aucun deck)")
    return "\n".join(lines)


def format_notes_brief(notes: Sequence[NoteLike], deck: str = "") -> str:
    """`search_notes` in `brief` detail: one line per note, fields as truncated plain text."""
    where = f" du deck {deck}" if deck else ""
    lines = [
        f"# {len(notes)} note(s){where}",
        "",
        f"Une ligne par note : #id · deck (si différent) · ⚑ si flaguée · champs en texte brut "
        f"tronqués à {BRIEF_FIELD_CHARS} caractères. get_notes (ou search_notes en detail=full) "
        "donne les champs bruts complets.",
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
        bits.append(fields or "(champs vides)")
        lines.append(" · ".join(bits))
    if not notes:
        lines.append("(aucune note)")
    return "\n".join(lines)


def format_notes(notes: Sequence[NoteLike]) -> str:
    """Full notes, same layout as the notes in context."""
    if not notes:
        return "(aucune note trouvée)"
    return "\n\n".join(_fmt_note(note) for note in notes)


def format_note_type(note_type: NoteTypeLike) -> str:
    lines = [
        f"# Type de note {note_type.name}",
        "",
        f"Champs : {', '.join(note_type.fields) if note_type.fields else '(aucun)'}",
    ]
    for card, sides in note_type.templates.items():
        lines += ["", f"## Carte {card}"]
        for side, label in (("Front", "Recto"), ("Back", "Verso")):
            lines += ["", f"### {label}", "```html", sides.get(side, ""), "```"]
    lines += ["", "## CSS", "```css", note_type.css, "```"]
    return "\n".join(lines)


# ------------------------------------------------------------------------------- tools

#: tool name -> the `kind` carried by the `proposal` SSE event.
TOOL_KINDS: dict[str, str] = {
    "propose_edit": "edit",
    "propose_split": "split",
    "propose_create": "create",
    "propose_move": "move",
    "propose_bulk_edit": "bulk_edit",
    "propose_create_source": "create_source",
    "propose_edit_source": "edit_source",
}

#: Read tools, executed server-side through the `read_tools` map given to `stream_chat`.
READ_TOOLS: tuple[str, ...] = (
    "list_decks",
    "search_notes",
    "get_notes",
    "get_note_type",
    "read_source",
)

_FIELDS_DESC = (
    "Valeurs de champ Anki brutes, par nom de champ. HTML autorisé, marqueurs de cloze conservés."
)


def _fields_schema(description: str = _FIELDS_DESC) -> dict[str, Any]:
    # Open map: field names come from the note type, so `additionalProperties` is a schema
    # rather than `false`. That is also why the tools do not set `strict: true` — strict tool use
    # requires `additionalProperties: false` on every object.
    return {
        "type": "object",
        "description": description,
        "additionalProperties": {"type": "string"},
    }


_RATIONALE = {
    "type": "string",
    "description": "Une phrase, en français : pourquoi ce changement.",
}
_TAGS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Liste complète des tags après changement. Omettre pour ne pas y toucher.",
}
_NOTE_ID = {"type": "integer", "description": "Identifiant de la note."}
_MODEL = {
    "type": "string",
    "description": "Nom du type de note Anki. Omettre pour reprendre celui de la note de départ.",
}


def _obj(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def tools() -> list[dict[str, Any]]:
    """Every tool definition sent to the model: the proposal tools, then the read tools."""
    return proposal_tools() + read_tool_defs()


def proposal_tools() -> list[dict[str, Any]]:
    """The seven proposal tools. Each call becomes one `proposal` event for the client."""
    return [
        {
            "name": "propose_edit",
            "description": (
                "Proposer une réécriture d'une note existante. Ne renvoyer que les champs qui "
                "changent, en valeur brute complète."
            ),
            "input_schema": _obj(
                {
                    "note_id": _NOTE_ID,
                    "fields": _fields_schema(
                        "Champs modifiés uniquement, valeurs brutes complètes. " + _FIELDS_DESC
                    ),
                    "tags": _TAGS,
                    "rationale": _RATIONALE,
                },
                required=["note_id", "fields", "rationale"],
            ),
        },
        {
            "name": "propose_split",
            "description": (
                "Proposer de couper une note en plusieurs. Par défaut l'originale est gardée et "
                "devient le premier fragment (donner ses champs dans `original`) ; mettre "
                "`original` à null pour la supprimer après création des nouvelles notes."
            ),
            "input_schema": _obj(
                {
                    "note_id": _NOTE_ID,
                    "original": {
                        "description": (
                            "Champs de la note originale après découpe, ou null pour la supprimer."
                        ),
                        "anyOf": [
                            _obj({"fields": _fields_schema()}, required=["fields"]),
                            {"type": "null"},
                        ],
                    },
                    "new_notes": {
                        "type": "array",
                        "description": "Notes à créer, dans le même deck et avec les mêmes tags.",
                        "minItems": 1,
                        "items": _obj(
                            {"model": _MODEL, "fields": _fields_schema()}, required=["fields"]
                        ),
                    },
                    "rationale": _RATIONALE,
                },
                required=["note_id", "original", "new_notes", "rationale"],
            ),
        },
        {
            "name": "propose_create",
            "description": (
                "Proposer une note en plus, dans le deck courant, sans toucher aux notes "
                "existantes. Les tags de la note sélectionnée sont repris automatiquement."
            ),
            "input_schema": _obj(
                {
                    "fields": _fields_schema(),
                    "model": _MODEL,
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Sources (ids de l'index du corpus) dont la note est tirée : ses "
                            "ancres. Omettre pour reprendre celles de la note sélectionnée."
                        ),
                    },
                    "rationale": _RATIONALE,
                },
                required=["fields", "rationale"],
            ),
        },
        {
            "name": "propose_move",
            "description": "Proposer de déplacer une note vers un autre deck.",
            "input_schema": _obj(
                {
                    "note_id": _NOTE_ID,
                    "deck": {
                        "type": "string",
                        "description": "Deck de destination, nom complet avec les `::`.",
                    },
                    "rationale": _RATIONALE,
                },
                required=["note_id", "deck", "rationale"],
            ),
        },
        {
            "name": "propose_bulk_edit",
            "description": (
                "Proposer la même modification sur plusieurs notes à la fois (ex. ajouter un "
                "en-tête de contexte à vingt notes). Une seule carte, un seul clic pour tout "
                "appliquer. Pour chaque note, ne renvoyer que les champs qui changent, en valeur "
                "brute complète."
            ),
            "input_schema": _obj(
                {
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "items": _obj(
                            {
                                "note_id": _NOTE_ID,
                                "fields": _fields_schema(
                                    "Champs modifiés uniquement, valeurs brutes complètes. "
                                    + _FIELDS_DESC
                                ),
                            },
                            required=["note_id", "fields"],
                        ),
                    },
                    "rationale": _RATIONALE,
                },
                required=["edits", "rationale"],
            ),
        },
        {
            "name": "propose_create_source",
            "description": (
                "Proposer de créer une note Obsidian dans le vault et de l'ajouter au corpus du "
                "deck courant — pour consigner ce qu'une conversation a établi. Le résultat de "
                "l'outil donne l'identifiant que la source aura une fois créée : réutilise-le "
                "dans `source_ids` de propose_create."
            ),
            "input_schema": _obj(
                {
                    "name": {
                        "type": "string",
                        "description": (
                            "Nom de la note, relatif à la racine du vault, `/` autorisé pour un "
                            "dossier, sans `.md`. L'utilisateur peut le modifier avant d'appliquer."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Contenu Markdown complet de la note.",
                    },
                    "anchor_note_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Notes Anki à ancrer à cette source une fois créée.",
                    },
                    "rationale": _RATIONALE,
                },
                required=["name", "content", "rationale"],
            ),
        },
        {
            "name": "propose_edit_source",
            "description": (
                "Proposer de remplacer un passage d'une source Obsidian par un autre. `old` doit "
                "apparaître exactement une fois dans la source (copie-le tel quel) ; le "
                "remplacement est refusé sinon."
            ),
            "input_schema": _obj(
                {
                    "source_id": {
                        "type": "string",
                        "description": "Identifiant de la source (voir l'index du corpus).",
                    },
                    "old": {"type": "string", "description": "Passage actuel, verbatim."},
                    "new": {"type": "string", "description": "Passage de remplacement."},
                    "rationale": _RATIONALE,
                },
                required=["source_id", "old", "new", "rationale"],
            ),
        },
    ]


def read_tool_defs() -> list[dict[str, Any]]:
    """The read tools. Executed by the server; their text output is the tool result."""
    return [
        {
            "name": "list_decks",
            "description": (
                "Lire l'arborescence des decks avec le nombre de notes flaguées de chacun. "
                "À utiliser pour situer le deck courant ou choisir une destination de déplacement."
            ),
            "input_schema": _obj({}),
        },
        {
            "name": "search_notes",
            "description": (
                "Chercher des notes avec la syntaxe de recherche Anki (ex. `re:lagrang`, "
                "`Text:*KKT*`, `tag:convexité`, `flag:1`). La recherche est limitée au deck "
                "courant et à ses sous-decks sauf si la requête nomme un deck (`deck:…`). "
                "`detail` choisit la forme du résultat : `count` (le nombre seul), `brief` (une "
                "ligne par note, champs en texte brut tronqués — pour repérer des notes), `full` "
                "(champs bruts complets, tags, flags, raison — pour un audit de format ou de "
                "structure, que le texte tronqué ne permet pas)."
            ),
            "input_schema": _obj(
                {
                    "query": {"type": "string", "description": "Requête Anki."},
                    "detail": {
                        "type": "string",
                        "enum": ["count", "brief", "full"],
                        "description": "Forme du résultat. Défaut : brief.",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            'Avec `full` : ne renvoyer que ces champs (ex. ["Text"]), quand les '
                            "autres sont du bruit."
                        ),
                    },
                },
                required=["query"],
            ),
        },
        {
            "name": "get_notes",
            "description": (
                "Lire des notes en entier (champs bruts, tags, flags, raison), dans le même "
                "format que les notes en contexte."
            ),
            "input_schema": _obj(
                {
                    "note_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                        "description": "Identifiants des notes.",
                    }
                },
                required=["note_ids"],
            ),
        },
        {
            "name": "get_note_type",
            "description": (
                "Lire un type de note : ses champs, ses templates de carte (recto / verso) et "
                "son CSS."
            ),
            "input_schema": _obj(
                {"model": {"type": "string", "description": "Nom du type de note."}},
                required=["model"],
            ),
        },
        {
            "name": "read_source",
            "description": (
                "Lire le texte d'une source du corpus (voir l'index) dans ce tour. Le texte "
                "n'est pas conservé au tour suivant : si tu en as encore besoin, relis-le, ou "
                "demande à l'utilisateur de joindre la source."
            ),
            "input_schema": _obj(
                {
                    "source_id": {
                        "type": "string",
                        "description": "Identifiant de la source, tel que donné dans l'index.",
                    }
                },
                required=["source_id"],
            ),
        },
    ]


# ------------------------------------------------------------------------------ streaming


def _tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def _summary(text: str) -> str:
    """First line of a read result, without its Markdown heading marker."""
    first = text.strip().split("\n", 1)[0] if text.strip() else ""
    return first.lstrip("#").strip()


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


async def stream_chat(
    anthropic_client: Any,
    deck: str,
    note_ids: Sequence[int],
    source_ids: Sequence[str],
    messages: Sequence[Mapping[str, Any]],
    load_note: NoteLoader,
    load_corpus: CorpusLoader,
    load_source: SourceLoader,
    read_tools: Mapping[str, ReadTool] | None = None,
    model: str | None = None,
    flagged_count: int | None = None,
) -> AsyncIterator[ChatEvent]:
    """Run one chat turn (with its tool loop) and yield the events the client should receive.

    `messages` are plain `{role, content: str}` turns. Proposal calls are surfaced as `proposal`
    events and answered with a `"ok"` tool result so Claude can keep talking; applying a
    proposal is the user's decision, made later through the review and sources endpoints. A
    create-source proposal is answered with the id the source will carry, so that Claude can
    anchor the notes it proposes next to it; the same id travels in the event (`source_id`).
    Read calls are executed through `read_tools`, surfaced as `reading` events, and answered
    with the text the tool returned (or an error tool result, so Claude can react instead of
    the turn failing).
    """
    try:
        notes = [load_note(int(note_id)) for note_id in note_ids]
        corpus_index = load_corpus(deck)
        attached = [load_source(str(source_id)) for source_id in source_ids]
        system = build_system(deck, corpus_index, attached, notes, flagged_count)
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI, never raised into the SSE body
        yield ChatEvent("error", {"detail": f"Contexte indisponible : {exc}"})
        return

    convo: list[dict[str, Any]] = [
        {"role": str(message["role"]), "content": message["content"]} for message in messages
    ]
    tool_defs = tools()
    final: Any = None

    try:
        for _ in range(MAX_TOOL_LOOPS):
            async with anthropic_client.messages.stream(
                model=model or default_model(),
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tool_defs,
                messages=convo,
            ) as stream:
                async for event in stream:
                    if getattr(event, "type", None) != "content_block_delta":
                        continue
                    delta: Any = getattr(event, "delta", None)
                    if getattr(delta, "type", None) == "text_delta":
                        yield ChatEvent("text", {"delta": delta.text})
                final = await stream.get_final_message()

            if getattr(final, "stop_reason", None) != "tool_use":
                yield ChatEvent(
                    "done",
                    {
                        "stop_reason": getattr(final, "stop_reason", None),
                        "usage": _usage_dict(final),
                    },
                )
                return

            calls = [block for block in final.content if getattr(block, "type", None) == "tool_use"]
            results: list[dict[str, Any]] = []
            for block in calls:
                kind = TOOL_KINDS.get(block.name)
                if kind is not None:
                    data: dict[str, Any] = {"id": block.id, "kind": kind, "input": block.input}
                    answer = "ok"
                    if kind == "create_source":
                        data["source_id"] = new_source_id()
                        answer = (
                            f"ok — une fois appliquée, la source aura l'identifiant "
                            f"{data['source_id']} (utilisable dans source_ids de propose_create)."
                        )
                    yield ChatEvent("proposal", data)
                    results.append(_tool_result(block.id, answer))
                    continue
                tool = (read_tools or {}).get(block.name)
                if tool is None:
                    results.append(
                        _tool_result(block.id, f"Outil inconnu : {block.name}", is_error=True)
                    )
                    continue
                tool_input = dict(block.input or {})
                try:
                    text = tool(tool_input)
                except Exception as exc:  # noqa: BLE001 — Claude gets the error, not the UI
                    text, failed = f"{type(exc).__name__}: {exc}", True
                else:
                    failed = False
                results.append(_tool_result(block.id, text, is_error=failed))
                yield ChatEvent(
                    "reading",
                    {
                        "id": block.id,
                        "tool": block.name,
                        "input": tool_input,
                        "summary": ("erreur : " if failed else "") + _summary(text),
                    },
                )
            # Feed the tool results back so Claude can comment on what it just proposed or read.
            convo.append({"role": "assistant", "content": final.content})
            convo.append({"role": "user", "content": results})

        yield ChatEvent("done", {"stop_reason": "max_tool_loops", "usage": _usage_dict(final)})
    except Exception as exc:  # noqa: BLE001 — the SSE stream reports, it does not crash
        yield ChatEvent("error", {"detail": f"{type(exc).__name__}: {exc}"})
