"""Tool definitions for the chat: proposal, read, and web tools.

Schema helpers and the three `*_defs()` functions that return the tool
definitions Claude receives.  Also the constants that `stream` uses to
dispatch tool calls (`TOOL_KINDS`, `TARGETED`, `READ_TOOLS`, `WEB_TOOLS`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .types import (
    MAX_CARDS,
    MAX_WEB_FETCHES,
    MAX_WEB_SEARCHES,
    WEB_FETCH_TYPE,
    WEB_SEARCH_TYPE,
)

# -------------------------------------------------------------- dispatch constants

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
WEB_RESULTS: dict[str, str] = {
    "web_search_tool_result": "web_search",
    "web_fetch_tool_result": "web_fetch",
}


# ------------------------------------------------------------------ schema helpers

_FIELDS_DESC = (
    "Valeurs de champ Anki brutes, par nom de champ. HTML autorisé, marqueurs de cloze conservés."
)


def _fields_schema(description: str = _FIELDS_DESC) -> dict[str, Any]:
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


# ------------------------------------------------------------------ public builders


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
                "champs de la version affichée sont repris tels quels. "
                "Pour changer le type de note (ex. Cloze → Basic) : passer `model` avec le nom "
                "du nouveau type et donner **tous** les champs du type cible (rien n'est repris "
                "de l'ancienne version, les schémas sont différents). L'historique de la c1 est "
                "conservé ; les c2+ deviennent orphelines jusqu'au prochain « Vérifier la base »."
            ),
            "input_schema": _obj(
                {
                    "target": _TARGET,
                    "model": {
                        "type": "string",
                        "description": (
                            "Nouveau type de note (ex. « Basic »). Omettre pour garder le type "
                            "actuel. Quand il change, `fields` doit donner tous les champs du "
                            "type cible."
                        ),
                    },
                    "fields": _fields_schema(
                        "Champs modifiés uniquement (ou tous les champs du type cible si `model` "
                        "change). Valeurs brutes complètes. " + _FIELDS_DESC
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
                            "Champs de la note originale après découpe, ou null pour la supprimer. "
                            "Toujours donner `model` (le type de note du fragment). "
                            "Quand le type change (ex. Cloze → Basic), donner **tous** les champs "
                            "du type cible (rien n'est repris de l'ancienne version)."
                        ),
                        "anyOf": [
                            _obj(
                                {"model": _MODEL, "fields": _fields_schema()},
                                required=["model", "fields"],
                            ),
                            {"type": "null"},
                        ],
                    },
                    "new_notes": {
                        "type": "array",
                        "description": (
                            "Notes à créer (tous leurs champs), dans le même deck et avec les "
                            "mêmes tags que la carte visée. Toujours donner `model`."
                        ),
                        "minItems": 1,
                        "items": _obj(
                            {"model": _MODEL, "fields": _fields_schema()},
                            required=["model", "fields"],
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
                "demande à l'utilisateur de joindre la source. Pour un PDF, tu peux demander "
                "des pages précises avec le paramètre pages."
            ),
            "input_schema": _obj(
                {
                    "source_id": {
                        "type": "string",
                        "description": "Identifiant de la source, tel que donné dans l'index.",
                    },
                    "pages": {
                        "type": "string",
                        "description": (
                            "PDF seulement : plage de pages à lire, ex. « 12-19 » ou "
                            "« 3-5,9 ». Sans ce paramètre, les pages associées à la source "
                            "sont lues."
                        ),
                    },
                },
                required=["source_id"],
            ),
        },
    ]
