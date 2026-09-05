"""Claude chat backend: prompt assembly, proposal tools, streaming. Spec: specs/chat.md.

No FastAPI imports here — `web/routes_chat.py` turns `ChatEvent`s into SSE lines.

Model choice
------------
Default model id is `claude-opus-5` (see `DEFAULT_MODEL`), overridable with `ANKI_CHAT_MODEL`.
Rationale, per the `claude-api` skill reference: Opus 5 is the recommended default for an
interactive assistant that reads long documents and drives tools — 1M-token context (the deck
corpus is capped at 150k characters, but notes plus corpus plus conversation still get large),
strong tool use, and adaptive thinking on by default. Sonnet 5 / Haiku 4.5 are the cheaper
step-downs if the corpus grows or latency matters; set `ANKI_CHAT_MODEL` rather than editing this.

Note: on Opus 5 thinking is on by default, and thinking tokens count against `max_tokens`
(8192 per the spec). If replies ever come back truncated, lower the effort or raise `max_tokens`.

Notes and corpus are not fetched here: `stream_chat` takes two injectable callables
(`load_note`, `load_corpus`) so this module does not import `review.py` or `sources.py`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

# --------------------------------------------------------------------------- config

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 8192
#: Total budget for all corpus text injected in the system prompt.
MAX_CORPUS_CHARS = 150_000
#: Safety net on the tool-use loop: one turn per proposal round, then we stop.
MAX_TOOL_LOOPS = 6


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


@dataclass
class CorpusText:
    """One source of the deck's corpus, already extracted to text.

    Built by the caller from `sources.Source` + `Source.text(vault)`; kept as a plain dataclass
    so this module never imports `sources.py`.
    """

    kind: str
    target: str
    text: str
    pages: str = ""
    deck: str = ""
    truncated: bool = False
    warning: str = ""
    n_pages: int | None = None


NoteLoader = Callable[[int], NoteLike]
CorpusLoader = Callable[[str], list[CorpusText]]


@dataclass
class ChatEvent:
    """One SSE event. `type` is one of "text", "proposal", "done", "error"."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------ system prompt

STANDING_INSTRUCTIONS = """\
Tu assistes Hugo dans la revue de ses cartes Anki signalées (« flaguées »). Il te montre le \
deck en cours, le corpus dont ce deck est tiré, et les notes en cours d'examen. Ton rôle : \
l'aider à décider quoi faire de chaque note, et lui proposer les changements concrets.

Règles :
- Réponds dans la langue de l'utilisateur, français par défaut.
- Sois bref. Pas de préambule, pas de récapitulatif de ce qu'il vient de dire.
- Quand tu proposes un changement concret, utilise les outils (propose_edit, propose_split, \
propose_create, propose_move) au lieu de le décrire en prose. Un même tour peut contenir \
plusieurs propositions. L'utilisateur les applique lui-même d'un clic : tu ne modifies jamais \
Anki toi-même.
- Les champs que tu produis sont des valeurs de champ Anki **brutes** : même syntaxe que celles \
qu'on te montre (HTML autorisé, marqueurs de cloze `{{c1::réponse}}` conservés). Vérifie que la \
syntaxe cloze reste valide : numéros contigus à partir de c1, accolades équilibrées, au moins \
un cloze dans une note Cloze.
- N'invente jamais un fait absent du corpus. Si le corpus ne couvre pas le point, dis-le \
explicitement plutôt que de combler.
- Une note = une idée. Préfère plusieurs notes courtes à une note longue.
- Quand une note porte une « raison du flag », traite-la en premier : c'est la question à \
laquelle il faut répondre.\
"""


def _fmt_note(note: NoteLike) -> str:
    lines = [
        f"### Note {note.note_id}",
        f"- deck : {note.deck}",
        f"- modèle : {note.model}",
        f"- tags : {', '.join(note.tags) if note.tags else '(aucun)'}",
    ]
    reason = (note.reason or "").strip()
    lines.append(f"- raison du flag : {reason}" if reason else "- raison du flag : (aucune)")
    if note.flagged_cards:
        labels = ", ".join(f"c{c.ord + 1}" for c in note.flagged_cards)
        lines.append(
            f"- carte(s) flaguée(s) : {labels} (le flag a été posé en voyant ce cloze masqué)"
        )
    lines.append("- champs bruts :")
    for name, value in note.fields.items():
        lines.append(f"  - {name} : {value}")
    return "\n".join(lines)


def _source_header(index: int, source: CorpusText) -> str:
    bits = [f"## Source {index} — {source.kind} : {source.target}"]
    meta: list[str] = []
    if source.pages:
        meta.append(f"pages {source.pages}")
    if source.n_pages is not None:
        meta.append(f"{source.n_pages} page(s) extraite(s)")
    if source.deck:
        meta.append(f"déclarée sur le deck {source.deck}")
    if meta:
        bits.append("(" + ", ".join(meta) + ")")
    return " ".join(bits)


def _corpus_block(corpus_texts: Sequence[CorpusText]) -> str:
    """Render the corpus, capped at `MAX_CORPUS_CHARS` in total, sources in order."""
    parts = [
        "# Corpus du deck",
        "",
        f"Texte des sources du deck, dans l'ordre, plafonné à {MAX_CORPUS_CHARS} caractères au "
        "total. Ce qui est coupé ci-dessous, tu ne peux pas le voir : dis-le plutôt que de "
        "deviner.",
    ]
    if not corpus_texts:
        parts += ["", "(Aucune source n'est associée à ce deck.)"]
        return "\n".join(parts)

    remaining = MAX_CORPUS_CHARS
    for index, source in enumerate(corpus_texts, start=1):
        parts += ["", _source_header(index, source)]
        if source.warning:
            parts.append(f"⚠ {source.warning}")
        text = source.text or ""
        if source.truncated:
            parts.append("⚠ Texte déjà tronqué à l'extraction : la fin de la source manque.")
        if remaining <= 0:
            parts.append(
                f"⚠ Source omise : le plafond de {MAX_CORPUS_CHARS} caractères était déjà atteint."
            )
            continue
        if len(text) > remaining:
            text = text[:remaining]
            parts.append(
                "⚠ Source coupée ici : le plafond de "
                f"{MAX_CORPUS_CHARS} caractères est atteint, la suite manque."
            )
        remaining -= len(text)
        parts += ["", text]
    return "\n".join(parts)


def build_system(
    deck: str,
    corpus_texts: Sequence[CorpusText],
    notes: Sequence[NoteLike],
    flagged_count: int | None = None,
) -> list[dict[str, Any]]:
    """System prompt as a list of text blocks.

    Order: standing instructions, then the corpus, then deck + notes under review. The corpus
    block carries `cache_control: ephemeral`: everything up to and including it is cached, and
    that prefix depends only on the deck, so changing the selected note (which only affects the
    trailing notes block) keeps the corpus cache warm.
    """
    header = [f"# Deck en cours\n\n{deck}"]
    if flagged_count is not None:
        header.append(f"Notes signalées dans ce deck : {flagged_count}.")
    header.append(f"Notes en contexte : {len(notes)}.")

    notes_part = ["# Notes en cours d'examen", ""]
    if notes:
        notes_part.append(
            "Champs donnés bruts (HTML et marqueurs de cloze compris). Produis les tiens dans la "
            "même syntaxe."
        )
        for note in notes:
            notes_part += ["", _fmt_note(note)]
    else:
        notes_part.append("(Aucune note sélectionnée.)")

    return [
        {"type": "text", "text": STANDING_INSTRUCTIONS},
        {
            "type": "text",
            "text": _corpus_block(corpus_texts),
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "\n\n".join(header) + "\n\n" + "\n".join(notes_part)},
    ]


# ------------------------------------------------------------------------------- tools

#: tool name -> the `kind` carried by the `proposal` SSE event.
TOOL_KINDS: dict[str, str] = {
    "propose_edit": "edit",
    "propose_split": "split",
    "propose_create": "create",
    "propose_move": "move",
}

_FIELDS_DESC = (
    "Valeurs de champ Anki brutes, par nom de champ. HTML autorisé, marqueurs de cloze conservés."
)


def _fields_schema(description: str = _FIELDS_DESC) -> dict[str, Any]:
    # Open map: field names come from the note's model, so `additionalProperties` is a schema
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


def tools() -> list[dict[str, Any]]:
    """The four proposal tools. Each call becomes one `proposal` event for the client."""
    return [
        {
            "name": "propose_edit",
            "description": (
                "Proposer une réécriture d'une note existante. Utilise-le dès que la note est "
                "récupérable en la reformulant. Ne renvoie que les champs qui changent."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "note_id": {
                        "type": "integer",
                        "description": "Identifiant de la note à modifier.",
                    },
                    "fields": _fields_schema(
                        "Champs modifiés uniquement, valeurs brutes complètes. " + _FIELDS_DESC
                    ),
                    "tags": _TAGS,
                    "rationale": _RATIONALE,
                },
                "required": ["note_id", "fields", "rationale"],
                "additionalProperties": False,
            },
        },
        {
            "name": "propose_split",
            "description": (
                "Proposer de couper une note en plusieurs. Par défaut l'originale est gardée et "
                "devient le premier fragment (donner ses champs dans `original`) ; mettre "
                "`original` à null pour la supprimer après création des nouvelles notes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "note_id": {
                        "type": "integer",
                        "description": "Identifiant de la note à couper.",
                    },
                    "original": {
                        "description": (
                            "Champs de la note originale après découpe, ou null pour la supprimer."
                        ),
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {"fields": _fields_schema()},
                                "required": ["fields"],
                                "additionalProperties": False,
                            },
                            {"type": "null"},
                        ],
                    },
                    "new_notes": {
                        "type": "array",
                        "description": "Notes à créer, dans le même deck et avec les mêmes tags.",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "model": {
                                    "type": "string",
                                    "description": (
                                        "Modèle Anki. Omettre pour reprendre celui de l'originale."
                                    ),
                                },
                                "fields": _fields_schema(),
                            },
                            "required": ["fields"],
                            "additionalProperties": False,
                        },
                    },
                    "rationale": _RATIONALE,
                },
                "required": ["note_id", "original", "new_notes", "rationale"],
                "additionalProperties": False,
            },
        },
        {
            "name": "propose_create",
            "description": (
                "Proposer une note sœur : une note en plus, dans le deck courant, sans toucher à "
                "l'originale. Les tags de la note sélectionnée sont repris automatiquement."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "fields": _fields_schema(),
                    "model": {
                        "type": "string",
                        "description": (
                            "Modèle Anki. Omettre pour reprendre celui de la note sélectionnée."
                        ),
                    },
                    "rationale": _RATIONALE,
                },
                "required": ["fields", "rationale"],
                "additionalProperties": False,
            },
        },
        {
            "name": "propose_move",
            "description": "Proposer de déplacer une note vers un autre deck.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "note_id": {
                        "type": "integer",
                        "description": "Identifiant de la note à déplacer.",
                    },
                    "deck": {
                        "type": "string",
                        "description": "Deck de destination, nom complet avec les `::`.",
                    },
                    "rationale": _RATIONALE,
                },
                "required": ["note_id", "deck", "rationale"],
                "additionalProperties": False,
            },
        },
    ]


# ------------------------------------------------------------------------------ streaming


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
    messages: Sequence[Mapping[str, Any]],
    load_note: NoteLoader,
    load_corpus: CorpusLoader,
    model: str | None = None,
    flagged_count: int | None = None,
) -> AsyncIterator[ChatEvent]:
    """Run one chat turn (with its tool loop) and yield the events the client should receive.

    `messages` are plain `{role, content: str}` turns. Tool calls are surfaced as `proposal`
    events and answered with a `"ok"` tool result so Claude can keep talking; applying a
    proposal is the user's decision, made later through the review endpoints.
    """
    try:
        notes = [load_note(int(note_id)) for note_id in note_ids]
        corpus = load_corpus(deck)
        system = build_system(deck, corpus, notes, flagged_count)
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
            for block in calls:
                kind = TOOL_KINDS.get(block.name)
                if kind is None:
                    continue
                yield ChatEvent("proposal", {"id": block.id, "kind": kind, "input": block.input})
            # Feed the tool results back so Claude can comment on what it just proposed.
            convo.append({"role": "assistant", "content": final.content})
            convo.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": block.id, "content": "ok"}
                        for block in calls
                    ],
                }
            )

        yield ChatEvent("done", {"stop_reason": "max_tool_loops", "usage": _usage_dict(final)})
    except Exception as exc:  # noqa: BLE001 — the SSE stream reports, it does not crash
        yield ChatEvent("error", {"detail": f"{type(exc).__name__}: {exc}"})
