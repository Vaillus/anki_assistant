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
Tu assistes Hugo dans la revue de ses notes Anki signalées (« flaguées »). L'utilisateur \
travaille dans un espace de travail : les cartes qu'il regarde (la note qu'il a ouverte, les \
brouillons préparés pour elle, les notes ajoutées depuis) sont listées plus bas avec leur \
identifiant (1, 2…). Ce prompt te donne aussi le deck en cours, les types de notes \
(noms et champs), l'index de son corpus et les sources jointes. Le reste — les autres \
notes du deck ou de la collection, le texte d'une source non jointe, l'arborescence des \
decks — se lit avec les outils de lecture, et ce qui n'est nulle part dans la collection \
se cherche sur le web.

Règles :
- Réponds dans la langue de l'utilisateur, français par défaut.
- Quand l'utilisateur pose une question, demande une explication, une vérification ou un \
renseignement, **réponds sans proposer de modification**. Ne propose un changement que si \
l'utilisateur le demande explicitement ou si ta réponse révèle une erreur factuelle manifeste \
dans une carte — et dans ce cas, signale l'erreur d'abord.
- Le message de l'utilisateur porte sur les cartes **actives**. Désigne une carte par son \
identifiant d'espace (`target: "3"`) ; une note qui n'est pas encore dans l'espace se désigne \
par son identifiant Anki (un grand nombre), elle y sera ajoutée.
- Quand tu proposes un changement concret, **appelle** les outils de proposition (propose_edit, \
propose_split, propose_create, propose_move, propose_add_source, propose_create_source, \
propose_edit_source) au lieu de le décrire en prose. Un même tour peut en contenir plusieurs ; \
le même défaut sur plusieurs notes = un propose_edit par note. Chaque proposition devient une \
version ou une carte que l'utilisateur relit, retouche ou écarte, puis valide en bloc : tu \
n'écris jamais dans Anki. Pour montrer des notes à l'utilisateur sans les modifier, add_notes \
les ajoute à l'espace.
- Les marqueurs entre crochets dans l'historique (`[proposition: …]`, `[lecture: …]`, \
`[ajout: …]`) sont générés automatiquement quand tu appelles un outil. **Ne les écris jamais \
toi-même** dans ta réponse : ils ne déclenchent rien. Pour qu'une proposition soit effective, \
appelle toujours l'outil correspondant.
- Le web (web_search, puis web_fetch pour lire une page en entier) sert à deux choses : \
confronter une carte à l'extérieur quand le corpus ne suffit pas, et **trouver des sources à \
ajouter** — un article, un livre, une page de référence. Le corpus n'est pas fermé. **Dès que \
tu cites une page web dans ta réponse et que cette page est une bonne référence pour le deck \
(un article Wikipédia, un cours, une documentation), appelle propose_add_source avec son URL \
dans le même tour** pour que l'utilisateur puisse l'ajouter au corpus en un clic ; le serveur \
relit la page quand il en a besoin, rien n'est copié. Pour une synthèse que tu rédiges \
toi-même, utilise propose_create_source (note Obsidian dans le vault). Le contenu de carte que \
tu tires du web arrive avec la proposition de source qui le fonde, pas tout seul. Ne recopie \
pas d'URL dans ta réponse : les passages tirés du web sont cités automatiquement (renvoi \
numéroté vers la page, liste des sources sous la réponse) ; une URL en clair ne sert qu'à \
recommander une page que tu n'as pas citée.
- Ne lis une source (`read_source`) que si tu en as réellement besoin pour répondre : vérifier \
un fait douteux, corriger une erreur factuelle, ou compléter un champ avec de l'information \
absente de la carte et du contexte. Si la carte, les sources jointes et ta connaissance du \
sujet suffisent, ne lis pas.
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


# --------------------------------------------------------------------- formatting helpers


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
    if card.defer:
        comment = (card.comment or "").strip()
        flag = "sera créée flaguée" if card.note_id is None else "le flag reste"
        states.append(
            f"marquée à revoir plus tard ({flag}"
            + (f", commentaire prévu : « {comment} »)" if comment else ")")
        )
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
    if entry.kind == "pdf" and entry.n_pages is not None and not entry.pages:
        meta.append(f"{entry.n_pages} p.")
    if entry.missing:
        bits.append("⚠ fichier introuvable")
    if entry.toc:
        _MAX_TOC = 10
        toc_parts = [f"p.{p} {h}" for p, h in entry.toc[:_MAX_TOC]]
        if len(entry.toc) > _MAX_TOC:
            toc_parts.append(f"+{len(entry.toc) - _MAX_TOC}")
        bits.append("— structure : " + " · ".join(toc_parts))
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
        "dans « Sources jointes » ci-dessous), ou tu appelles read_source avec son id. Pour un "
        "PDF, consulte d'abord la structure ci-dessous pour identifier les pages pertinentes, "
        "puis appelle read_source avec le paramètre pages (ex. « 18-21 »). Ne lis jamais un "
        "PDF entier : cible les sections qui répondent à la question.",
        "",
    ]
    if not corpus_index:
        parts.append("(Aucune source n'est associée à ce deck.)")
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


def _note_types_block(note_types: Mapping[str, Sequence[str]]) -> str:
    """A compact listing of every note type and its fields, so Claude can propose
    type conversions without a tool call."""
    if not note_types:
        return ""
    lines = ["# Types de notes de la collection", ""]
    for name, fields in sorted(note_types.items()):
        lines.append(f"- **{name}** : {', '.join(fields)}")
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
    note_ids = [card.note_id for card in cards if card.note_id is not None]
    active = sum(1 for card in cards if card.active)

    header = [f"# Deck en cours\n\n{deck}"]
    if flagged_count is not None:
        header.append(f"Notes signalées dans ce deck : {flagged_count}.")
    header.append(f"Cartes dans l'espace de travail : {len(cards)}, dont {active} active(s).")

    types_part = _note_types_block(note_types or {})

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
