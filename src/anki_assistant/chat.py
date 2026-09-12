"""Claude chat backend: prompt assembly, tools, streaming. Spec: specs/chat.md.

No FastAPI or Anki imports here — `web/routes_chat.py` turns `ChatEvent`s into SSE lines and
builds the read tools. From `sources.py` only `new_source_id` is used (a pure function), so that
the add-source and create-source proposals can announce the id the source will carry once the
user applies it.

Model choice
------------
Default model id is `claude-opus-4-6` (see `DEFAULT_MODEL`, and specs/chat.md#llm-configuration),
overridable with `ANKI_CHAT_MODEL` — set that rather than editing this.

Note: when thinking is enabled, thinking tokens count against `max_tokens` (8192 per the spec).
If replies ever come back truncated, lower the effort or raise `max_tokens`.

Context is not fetched here: the cards of the workspace arrive as the client holds them (the
shown version may be a draft or a hand edit, so re-reading Anki would be wrong), and
`stream_chat` takes injectable callables (`load_corpus`, `load_source`, and the `read_tools`
map) so this module imports neither `review.py` nor the Anki client. It only imports
`models.strip_html`, a pure function.

Two kinds of tools (specs/chat.md):
- **proposal tools** (`propose_*`): the call is streamed to the client as a `proposal` event and
  answered with "ok"; it lands on the workspace as a version or a card, written at validation.
  `target` names a card by workspace id or an Anki note by id; an absent note is added to the
  workspace by the client, within the cap of `MAX_CARDS`, which is checked here.
- **read tools** (`list_decks`, `search_notes`, `get_notes`, `add_notes`, `get_note_type`,
  `read_source`): executed here through the injected callables, the text they return is the tool
  result. A `reading` event tells the client what was read; `add_notes` also sends an `added`
  event so that the notes become cards.
- **web tools** (`web_search`, `web_fetch`): run by Anthropic inside the model call, so there is
  nothing to execute here. Their results come back as extra content blocks of the assistant
  message; `_web_events` turns each one into a `reading` event carrying the URLs, and a long run
  comes back as `stop_reason: "pause_turn"`, which `stream_chat` resumes. The passages of the
  reply that rest on a page arrive with citations (`citations_delta` in the stream); `_Citer`
  numbers the pages per turn and emits a `citation` event the client renders as a [n] marker.
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
#: Cards a workspace holds at most (specs/workspace.md#how-notes-enter).
MAX_CARDS = 50
#: Web tool versions. These filter results server-side before they enter context, which needs
#: Opus 4.6 / Sonnet 4.6 or later — the floor `DEFAULT_MODEL` already sits on.
WEB_SEARCH_TYPE = "web_search_20260209"
WEB_FETCH_TYPE = "web_fetch_20260209"
#: Web tool calls per turn. Billed per search on top of tokens, hence a cap rather than none.
MAX_WEB_SEARCHES = 8
MAX_WEB_FETCHES = 5
#: URLs spelled out in a `web_search` reading line; the rest are counted.
WEB_URLS_SHOWN = 5
#: Characters of a citation's `cited_text` kept for the marker's tooltip. Search citations are
#: at most 150 by the API; fetch citations quote whole passages, hundreds of characters long.
CITED_TEXT_CHARS = 200


def default_model() -> str:
    """Model id for the chat: `ANKI_CHAT_MODEL` if set and non-empty, else `DEFAULT_MODEL`."""
    return (os.environ.get("ANKI_CHAT_MODEL") or "").strip() or DEFAULT_MODEL


# ----------------------------------------------------------------- injected interfaces


class NoteLike(Protocol):
    """The shape `review.NoteView` exposes; only what the read tools' output needs."""

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


@dataclass
class WorkspaceCard:
    """One card of the workspace as the client sent it (specs/chat.md#api), for block 4.

    `fields` are the raw values of the shown version; `original_fields` are v0's when the shown
    version is not v0 (None otherwise). `note_id` is None for a draft note.
    """

    wid: str
    note_id: int | None
    fields: dict[str, str]
    deck: str = ""
    model: str = ""
    tags: list[str] = field(default_factory=list)
    active: bool = True
    original_fields: dict[str, str] | None = None
    #: Cloze numbers (c{ord+1}) of the flagged cards.
    flagged_clozes: list[int] = field(default_factory=list)
    reason: str = ""
    anchor_ids: list[str] = field(default_factory=list)
    deleted: bool = False
    keep: bool = False
    move_to: str | None = None
    parent_wid: str | None = None


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


CorpusLoader = Callable[[str], list[CorpusEntry]]
#: An attached source, loaded: the source and its extracted text.
Attached = tuple[SourceLike, SourceTextLike]
SourceLoader = Callable[[str], Attached]
#: A read tool: takes the tool input as given by the model, returns the text Claude will read.
ReadTool = Callable[[Mapping[str, Any]], str]


@dataclass
class ChatEvent:
    """One SSE event. `type` is one of "text", "citation", "reading", "added", "proposal",
    "done", "error"."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------ system prompt

# Block 1. Only what specs/chat.md lists: language, proposal tools over prose, raw field syntax,
# and the conventions of the collection stated as facts. How to reason about a card (terseness,
# when to read, how many notes an idea deserves) is the conversation's business, not the prompt's.
STANDING_INSTRUCTIONS = """\
Tu assistes Hugo dans la revue de ses notes Anki signalées (« flaguées »). L'utilisateur \
travaille dans un espace de travail : les cartes qu'il regarde (la note qu'il a ouverte, les \
brouillons préparés pour elle, les notes ajoutées depuis) sont listées plus bas avec leur \
identifiant (w1, w2…). Ce prompt te donne aussi le deck en cours, l'index de son corpus et les \
sources jointes. Le reste — les autres notes du deck ou de la collection, le texte d'une source \
non jointe, l'arborescence des decks, un type de note — se lit avec les outils de lecture, et \
ce qui n'est nulle part dans la collection se cherche sur le web.

Règles :
- Réponds dans la langue de l'utilisateur, français par défaut.
- Le message de l'utilisateur porte sur les cartes **actives**. Désigne une carte par son \
identifiant d'espace (`target: "w3"`) ; une note qui n'est pas encore dans l'espace se désigne \
par son identifiant Anki en chiffres, elle y sera ajoutée.
- Quand tu proposes un changement concret, utilise les outils de proposition (propose_edit, \
propose_split, propose_create, propose_move, propose_add_source, propose_create_source, \
propose_edit_source) au lieu de le décrire en prose. Un même tour peut en contenir plusieurs ; \
le même défaut sur plusieurs notes = un propose_edit par note. Chaque proposition devient une \
version ou une carte que l'utilisateur relit, retouche ou écarte, puis valide en bloc : tu \
n'écris jamais dans Anki. Pour montrer des notes à l'utilisateur sans les modifier, add_notes \
les ajoute à l'espace.
- Le web (web_search, puis web_fetch pour lire une page en entier) sert à deux choses : \
confronter une carte à l'extérieur quand le corpus ne suffit pas, et **trouver des sources à \
ajouter** — un article, un livre, une page de référence. Le corpus n'est pas fermé : une page \
qui mérite d'être gardée s'ajoute au corpus par propose_add_source (son URL ; le serveur relit \
la page quand il en a besoin), ou se consigne dans le vault par propose_create_source quand \
c'est une synthèse qu'il faut garder ; et le contenu de carte que tu tires du web arrive avec \
la proposition de source qui le fonde, pas tout seul. Ne recopie pas d'URL dans ta réponse : \
les passages tirés du web sont cités automatiquement (renvoi numéroté vers la page, liste des \
sources sous la réponse) ; une URL en clair ne sert qu'à recommander une page que tu n'as pas \
citée.
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


def _fmt_card(card: WorkspaceCard, anchors: Sequence[CorpusEntry] = ()) -> str:
    """One card of block 4: identity, states, note facts, the shown version, v0 when changed."""
    if card.note_id is None:
        head = f"### Carte {card.wid} — brouillon, pas encore dans Anki"
    else:
        head = f"### Carte {card.wid} — note {card.note_id}"
    states = ["active" if card.active else "inactive"]
    if card.deleted:
        states.append("marquée supprimée")
    if card.keep:
        states.append("marquée à garder telle quelle")
    if card.move_to:
        states.append(f"à déplacer vers {card.move_to}")
    lines = [head, f"- état : {', '.join(states)}"]
    if card.parent_wid:
        lines.append(f"- fragment de la carte {card.parent_wid}")
    lines += [
        f"- deck : {card.deck}",
        f"- type de note : {card.model}",
        f"- tags : {', '.join(card.tags) if card.tags else '(aucun)'}",
    ]
    reason = (card.reason or "").strip()
    if card.note_id is not None:
        lines.append(f"- raison du flag : {reason}" if reason else "- raison du flag : (aucune)")
    if card.flagged_clozes:
        labels = ", ".join(f"c{n}" for n in card.flagged_clozes)
        lines.append(
            f"- carte(s) flaguée(s) : {labels} (le flag a été posé en voyant ce cloze masqué)"
        )
    if anchors:
        lines.append("- ancres : " + ", ".join(f"[{entry.id}] {entry.target}" for entry in anchors))
    shown = "- champs bruts (version affichée)" if card.original_fields else "- champs bruts"
    lines.append(f"{shown} :")
    for name, value in card.fields.items():
        lines.append(f"  - {name} : {value}")
    if card.original_fields is not None:
        lines.append("- version d'origine (Anki) :")
        for name, value in card.original_fields.items():
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
    cards: Sequence[WorkspaceCard],
    flagged_count: int | None = None,
) -> list[dict[str, Any]]:
    """System prompt as four text blocks (specs/chat.md#context).

    1. standing instructions, 2. corpus index, 3. attached sources, 4. deck + workspace cards.
    Block 3 carries `cache_control: ephemeral`: blocks 1–3 depend only on the deck and on what
    the user attached, so a change on the workspace (which only changes block 4) reuses the cache.
    """
    note_ids = [card.note_id for card in cards if card.note_id is not None]
    active = sum(1 for card in cards if card.active)

    header = [f"# Deck en cours\n\n{deck}"]
    if flagged_count is not None:
        header.append(f"Notes signalées dans ce deck : {flagged_count}.")
    header.append(f"Cartes dans l'espace de travail : {len(cards)}, dont {active} active(s).")

    notes_part = ["# Cartes de l'espace de travail", ""]
    if cards:
        notes_part.append(
            "La première est la note sur laquelle l'espace a été ouvert (la racine). Champs "
            "donnés bruts (HTML et marqueurs de cloze compris), tels que l'utilisateur les voit "
            "en ce moment — une version proposée ou retouchée, pas forcément ce qu'Anki "
            "contient. Produis les tiens dans la même syntaxe."
        )
        for card in cards:
            anchors = [entry for entry in corpus_index if entry.id in card.anchor_ids]
            notes_part += ["", _fmt_card(card, anchors)]
    else:
        notes_part.append("(Aucune carte.)")

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
    "propose_add_source": "add_source",
    "propose_create_source": "create_source",
    "propose_edit_source": "edit_source",
}

#: Proposals that bring a new source into the corpus: answered with the id it will carry.
NEW_SOURCE_KINDS: frozenset[str] = frozenset({"add_source", "create_source"})

#: Proposal tools whose `target` names a card (or a note to add as a card).
TARGETED: frozenset[str] = frozenset({"propose_edit", "propose_split", "propose_move"})

#: Read tools, executed server-side through the `read_tools` map given to `stream_chat`.
#: `add_notes` runs `get_notes` and additionally streams an `added` event.
READ_TOOLS: tuple[str, ...] = (
    "list_decks",
    "search_notes",
    "get_notes",
    "add_notes",
    "get_note_type",
    "read_source",
)

#: Tools Anthropic executes. Nothing here dispatches them; they are reported, not run.
WEB_TOOLS: tuple[str, ...] = ("web_search", "web_fetch")

#: Result block type -> the web tool that produced it.
_WEB_RESULTS: dict[str, str] = {
    "web_search_tool_result": "web_search",
    "web_fetch_tool_result": "web_fetch",
}

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
_TARGET = {
    "type": "string",
    "description": (
        "Carte visée : son identifiant d'espace de travail (« w3 »), ou l'identifiant Anki en "
        "chiffres d'une note qui n'est pas encore dans l'espace (elle y sera ajoutée)."
    ),
}
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
    """Every tool definition: the proposal tools, then the read tools, then the web tools."""
    return proposal_tools() + read_tool_defs() + web_tool_defs()


def web_tool_defs() -> list[dict[str, Any]]:
    """The two Anthropic server tools. No `input_schema`: the API owns their shape.

    `allowed_callers` is pinned to direct calls. At its default these versions run the tools
    inside code execution and filter results before they reach context, and the reply then
    carries no citations at all (observed: a dozen code-execution round trips and not one
    `citations_delta`). Search results are always cited; fetched pages only when asked, hence
    `citations` on `web_fetch` (specs/chat.md#web-tools).
    """
    return [
        {
            "type": WEB_SEARCH_TYPE,
            "name": "web_search",
            "max_uses": MAX_WEB_SEARCHES,
            "allowed_callers": ["direct"],
        },
        {
            "type": WEB_FETCH_TYPE,
            "name": "web_fetch",
            "max_uses": MAX_WEB_FETCHES,
            "allowed_callers": ["direct"],
            "citations": {"enabled": True},
        },
    ]


def proposal_tools() -> list[dict[str, Any]]:
    """The seven proposal tools. Each call becomes one `proposal` event for the client."""
    return [
        {
            "name": "propose_edit",
            "description": (
                "Proposer une réécriture d'une carte : une nouvelle version de la carte visée. "
                "Ne renvoyer que les champs qui changent, en valeur brute complète ; les autres "
                "champs de la version affichée sont repris tels quels."
            ),
            "input_schema": _obj(
                {
                    "target": _TARGET,
                    "fields": _fields_schema(
                        "Champs modifiés uniquement, valeurs brutes complètes. " + _FIELDS_DESC
                    ),
                    "tags": _TAGS,
                    "rationale": _RATIONALE,
                },
                required=["target", "fields", "rationale"],
            ),
        },
        {
            "name": "propose_split",
            "description": (
                "Proposer de couper une carte en plusieurs. Par défaut la note originale est "
                "gardée et devient le premier fragment (donner tous ses champs dans "
                "`original`) ; mettre `original` à null pour la marquer supprimée, chaque "
                "fragment étant alors une nouvelle note. Les fragments apparaissent comme "
                "des cartes brouillon sous la carte visée."
            ),
            "input_schema": _obj(
                {
                    "target": _TARGET,
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
                        "description": (
                            "Notes à créer (tous leurs champs), dans le même deck et avec les "
                            "mêmes tags que la carte visée."
                        ),
                        "minItems": 1,
                        "items": _obj(
                            {"model": _MODEL, "fields": _fields_schema()}, required=["fields"]
                        ),
                    },
                    "rationale": _RATIONALE,
                },
                required=["target", "original", "new_notes", "rationale"],
            ),
        },
        {
            "name": "propose_create",
            "description": (
                "Proposer une note en plus (tous ses champs), dans le deck courant, sans toucher "
                "aux cartes existantes : une nouvelle carte brouillon. Les tags de la racine "
                "sont repris automatiquement."
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
                            "ancres. Omettre pour reprendre celles de la racine."
                        ),
                    },
                    "rationale": _RATIONALE,
                },
                required=["fields", "rationale"],
            ),
        },
        {
            "name": "propose_move",
            "description": (
                "Proposer de déplacer une carte vers un autre deck (un badge sur la carte, "
                "appliqué à la validation)."
            ),
            "input_schema": _obj(
                {
                    "target": _TARGET,
                    "deck": {
                        "type": "string",
                        "description": "Deck de destination, nom complet avec les `::`.",
                    },
                    "rationale": _RATIONALE,
                },
                required=["target", "deck", "rationale"],
            ),
        },
        {
            "name": "propose_add_source",
            "description": (
                "Proposer d'ajouter au corpus du deck courant une source qui existe déjà : une "
                "page web (son URL — le serveur la relit quand il en a besoin, rien n'est "
                "copié), une note du vault ou un PDF sur disque. Typiquement après un web_fetch "
                "sur une page qui mérite d'être gardée. Le résultat de l'outil donne "
                "l'identifiant que la source aura une fois ajoutée : réutilise-le dans "
                "`source_ids` de propose_create."
            ),
            "input_schema": _obj(
                {
                    "target": {
                        "type": "string",
                        "description": (
                            "URL http(s) de la page, nom d'une note du vault (relatif à sa "
                            "racine, sans `.md`) ou chemin d'un PDF. L'utilisateur peut le "
                            "corriger avant d'appliquer."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["web", "obsidian", "pdf"],
                        "description": (
                            "Type de la source. Omettre pour le déduire de la cible (URL → web, "
                            "`.pdf` → pdf, sinon obsidian)."
                        ),
                    },
                    "pages": {
                        "type": "string",
                        "description": "PDF seulement : plage de pages, ex. « 12-19 », « 3-5,9 ».",
                    },
                    "note": {
                        "type": "string",
                        "description": "Commentaire court affiché à côté de la source.",
                    },
                    "anchor_note_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Notes Anki à ancrer à cette source une fois ajoutée.",
                    },
                    "rationale": _RATIONALE,
                },
                required=["target", "rationale"],
            ),
        },
        {
            "name": "propose_create_source",
            "description": (
                "Proposer de créer une note Obsidian dans le vault et de l'ajouter au corpus du "
                "deck courant — pour consigner ce qu'une conversation a établi (pour une page "
                "web qui existe déjà, préférer propose_add_source). Le résultat de l'outil "
                "donne l'identifiant que la source aura une fois créée : réutilise-le dans "
                "`source_ids` de propose_create."
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
                "remplacement est refusé sinon. Une source pdf ou web ne se modifie pas."
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
                "format que les cartes en contexte. Rien ne change dans l'espace de travail."
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
            "name": "add_notes",
            "description": (
                "Ajouter des notes à l'espace de travail pour que l'utilisateur les voie et que "
                "tu puisses les viser — typiquement après un search_notes. Renvoie les notes en "
                f"entier comme get_notes. L'espace tient {MAX_CARDS} cartes au plus : au-delà, "
                "l'outil refuse et il faut resserrer."
            ),
            "input_schema": _obj(
                {
                    "note_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                        "description": "Identifiants des notes à ajouter.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Une phrase, en français : pourquoi ces notes.",
                    },
                },
                required=["note_ids", "rationale"],
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


def _attr(obj: Any, name: str) -> Any:
    """Read `name` off an SDK block or off the plain dict the test fake replays."""
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def _field(obj: Any, name: str) -> str:
    value = _attr(obj, name)
    return "" if value is None else str(value)


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
        return [], code or "erreur inconnue"
    if isinstance(content, list):
        return content, ""
    return ([] if content is None else [content]), ""


def _web_summary(tool: str, call_input: Mapping[str, Any], results: Sequence[Any], err: str) -> str:
    """The reading line for one web call. It spells out the URLs: that is the whole provenance
    guarantee (specs/chat.md#web-tools) — the user must never learn of a page after the fact."""
    if tool == "web_fetch":
        url = str(call_input.get("url") or "") or (_field(results[0], "url") if results else "")
        return f"erreur : {err}" if err else (url or "page")
    query = str(call_input.get("query") or "")
    head = f"« {query} »" if query else "recherche"
    if err:
        return f"{head} → erreur : {err}"
    if not results:
        return f"{head} → aucun résultat"
    urls = [url for url in (_field(r, "url") for r in results) if url]
    rest = len(urls) - WEB_URLS_SHOWN
    tail = f" (+{rest})" if rest > 0 else ""
    return f"{head} → {len(results)} résultat(s) : {', '.join(urls[:WEB_URLS_SHOWN])}{tail}"


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
        tool = _WEB_RESULTS.get(kind)
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
            return f"cible inconnue : « {text} » (identifiant d'espace w… ou identifiant Anki)"
        nid = int(text)
        if nid in self.note_ids:
            return None
        if not self.room_for([nid]):
            return _FULL
        self.admit([nid])
        return None


_FULL = (
    f"espace de travail plein ({MAX_CARDS} cartes) : resserre la sélection ou demande à "
    "l'utilisateur d'écarter des cartes."
)


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
        system = build_system(deck, corpus_index, attached, cards, flagged_count)
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
                # A block's citations may stream before or after its text deltas (both seen),
                # so they wait for the block to close: the [n] marker goes at the passage's end.
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
                # A long web-tool run. Hand the assistant message back untouched and call again:
                # the trailing server_tool_use block is what tells the API to resume, so there is
                # no user message to add. It spends one of the MAX_TOOL_LOOPS calls.
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
                            f"ok — une fois appliquée, la source aura l'identifiant "
                            f"{data['source_id']} (utilisable dans source_ids de propose_create)."
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
                            "rationale": str(tool_input.get("rationale") or ""),
                        },
                    )
                    continue
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
