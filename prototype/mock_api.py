"""THROWAWAY mock backend for the frontend work. Not part of the app, not linted.

Serves the real `src/anki_assistant/web/templates/index.html` + `/static` with fixture
JSON matching the shapes in specs/*.md, so app.js can be exercised without Anki.

    uv run python prototype/mock_api.py     # http://localhost:5071
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

WEB = Path(__file__).resolve().parents[1] / "src" / "anki_assistant" / "web"

app = FastAPI(title="MOCK anki-assistant")
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")

_CLOZE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)


def render_field(raw: str) -> str:
    txt = re.sub(r"<br\s*/?>", "\n", raw)
    txt = re.sub(r"</(p|div|li)>", "\n", txt)
    txt = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*>', r"\1", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html.unescape(txt)
    txt = html.escape(txt.strip())
    txt = _CLOZE.sub(lambda m: f'<span class="cloze" data-n="{m.group(1)}">{m.group(2)}</span>', txt)
    return txt.replace("\n", "<br>")


MODELS = {
    "Cloze": ["Text", "Back Extra"],
    "Basic": ["Front", "Back"],
    "Basic (and reversed card)": ["Front", "Back"],
}

DECK_NAMES = [
    "courant",
    "courant::00-Thèse",
    "courant::01-AI",
    "courant::01-AI::RL+LLM",
    "courant::01-AI::little book of deep learning",
    "archive",
    "archive::vieux trucs",
]

NOTES: dict[int, dict] = {}


def add_note(nid, deck, model, fields, tags, flagged, cards=2):
    NOTES[nid] = {
        "note_id": nid,
        "deck": deck,
        "model": model,
        "tags": tags,
        "card_ids": [nid + 1 + i for i in range(cards)],
        "flagged": flagged,
        "flag_colors": ["orange"] if flagged else [],
        "fields": fields,
    }


add_note(
    1732375559262,
    "courant::00-Thèse",
    "Cloze",
    {
        "Text": "L'allocation sur des angles disjoints permet de {{c1::décorréler les mesures}}.",
        "Back Extra": "For a given sensor ?",
    },
    ["phd"],
    True,
)
add_note(
    1732375559300,
    "courant::00-Thèse",
    "Cloze",
    {
        "Text": "La borne de {{c1::Cramér-Rao}} donne la variance minimale d'un estimateur "
        "{{c2::non biaisé}}.<br>Elle vaut $1/I(\\theta)$.",
        "Back Extra": "trop dense, à splitter",
    },
    ["phd", "stats"],
    True,
)
add_note(
    1732375559400,
    "courant::00-Thèse",
    "Basic",
    {"Front": "Qu'est-ce qu'un estimateur efficace ?", "Back": "Un estimateur qui atteint la borne."},
    ["phd"],
    False,
)
add_note(
    1732375559500,
    "courant::01-AI::RL+LLM",
    "Cloze",
    {"Text": "RLHF entraîne un {{c1::reward model}} à partir de comparaisons.", "Back Extra": ""},
    ["ai"],
    True,
)
add_note(
    1732375559600,
    "courant::01-AI::little book of deep learning",
    "Basic",
    {"Front": "Qu'est-ce que le dropout ?", "Back": "Une régularisation stochastique."},
    [],
    False,
)

SOURCES: dict[str, list[dict]] = {
    "courant::00-Thèse": [
        {"kind": "obsidian", "target": "Allocation sur des angles disjoints"},
        {"kind": "pdf", "target": "~/Documents/these/stone_search.pdf", "pages": "12-19", "note": "chap. 2"},
    ],
    "courant::01-AI": [{"kind": "pdf", "target": "~/Documents/lbdl.pdf"}],
}

VAULT_NOTES = [
    "Allocation sur des angles disjoints",
    "Borne de Cramér-Rao",
    "Estimateurs efficaces",
    "RLHF",
    "Transformers",
]

LOREM = ("Texte extrait de la source. " * 40 + "\n\n") * 12


def view_note(n: dict) -> dict:
    fields = n["fields"]
    reason = ""
    if n["flagged"] and "Back Extra" in fields:
        reason = re.sub(r"<[^>]+>", "", fields["Back Extra"]).strip()
    return {
        **n,
        "fields_html": {k: render_field(v) for k, v in fields.items()},
        "reason": reason,
    }


def corpus_for(deck: str):
    parts = deck.split("::")
    for cut in range(len(parts), 0, -1):
        cand = "::".join(parts[:cut])
        if cand in SOURCES:
            return cand, SOURCES[cand]
    return None, []


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB / "templates" / "index.html").read_text(encoding="utf-8"))


@app.get("/api/decks")
def decks():
    own: dict[str, int] = {}
    for n in NOTES.values():
        if n["flagged"]:
            own[n["deck"]] = own.get(n["deck"], 0) + 1
    total: dict[str, int] = {}
    for deck, cnt in own.items():
        parts = deck.split("::")
        for cut in range(1, len(parts) + 1):
            key = "::".join(parts[:cut])
            total[key] = total.get(key, 0) + cnt
    out = []
    for name in DECK_NAMES:
        on_deck, srcs = corpus_for(name)
        kinds = []
        for s in srcs:
            if s["kind"] not in kinds:
                kinds.append(s["kind"])
        out.append(
            {
                "name": name,
                "leaf": name.split("::")[-1],
                "depth": name.count("::"),
                "flagged_own": own.get(name, 0),
                "flagged_total": total.get(name, 0),
                "source_kinds": kinds,
            }
        )
    return out


@app.get("/api/models")
def models():
    return MODELS


@app.get("/api/notes")
def list_notes(deck: str = Query(...)):
    sel = [n for n in NOTES.values() if n["deck"] == deck or n["deck"].startswith(deck + "::")]
    sel.sort(key=lambda n: (not n["flagged"], n["note_id"]))
    return {
        "deck": deck,
        "total": len(sel),
        "flagged": sum(1 for n in sel if n["flagged"]),
        "notes": [view_note(n) for n in sel],
    }


def _get(nid: int) -> dict:
    if nid not in NOTES:
        raise HTTPException(404, "note inconnue")
    return NOTES[nid]


@app.get("/api/notes/{nid}")
def get_note(nid: int):
    return view_note(_get(nid))


@app.post("/api/notes/{nid}/keep")
def keep(nid: int):
    n = _get(nid)
    n["flagged"], n["flag_colors"] = False, []
    return view_note(n)


@app.patch("/api/notes/{nid}")
def patch(nid: int, body: dict = Body(...)):
    n = _get(nid)
    if body.get("fields"):
        n["fields"].update(body["fields"])
    if body.get("tags") is not None:
        n["tags"] = body["tags"]
    if body.get("unflag", True):
        n["flagged"], n["flag_colors"] = False, []
    return view_note(n)


@app.post("/api/notes/{nid}/split")
def split(nid: int, body: dict = Body(...)):
    n = _get(nid)
    created = []
    for spec in body.get("new_notes", []):
        new_id = max(NOTES) + 100
        add_note(
            new_id,
            n["deck"],
            spec.get("model") or n["model"],
            dict(spec.get("fields") or {}),
            spec.get("tags") or n["tags"],
            False,
            1,
        )
        created.append(view_note(NOTES[new_id]))
    original = None
    if body.get("original") is None:
        NOTES.pop(nid, None)
    else:
        n["fields"].update(body["original"].get("fields") or {})
        if body["original"].get("tags") is not None:
            n["tags"] = body["original"]["tags"]
        n["flagged"], n["flag_colors"] = False, []
        original = view_note(n)
    return {"original": original, "created": created}


@app.post("/api/notes")
def create(body: dict = Body(...)):
    new_id = max(NOTES) + 100
    add_note(
        new_id,
        body["deck"],
        body["model"],
        dict(body.get("fields") or {}),
        body.get("tags") or [],
        False,
        1,
    )
    return view_note(NOTES[new_id])


@app.post("/api/notes/{nid}/move")
def move(nid: int, body: dict = Body(...)):
    n = _get(nid)
    n["deck"] = body["deck"]
    n["flagged"], n["flag_colors"] = False, []
    return view_note(n)


@app.delete("/api/notes/{nid}", status_code=204)
def delete(nid: int):
    _get(nid)
    NOTES.pop(nid, None)
    return None


@app.get("/api/sources/corpus")
def corpus(deck: str = Query(...)):
    on_deck, srcs = corpus_for(deck)
    views = []
    for s in srcs:
        views.append(
            {
                "kind": s["kind"],
                "target": s["target"],
                "pages": s.get("pages", ""),
                "note": s.get("note", ""),
                "on_deck": on_deck,
                "exists": not s["target"].endswith("stone_search.pdf"),
                "uri": "file:///tmp/x.pdf" if s["kind"] == "pdf" else "obsidian://open?vault=Vault&file=x",
                "text": LOREM,
                "truncated": False,
                "n_pages": 8 if s["kind"] == "pdf" else None,
                "warning": "PDF entier sans plage de pages." if s["kind"] == "pdf" and not s.get("pages") else "",
            }
        )
    return {"deck": deck, "inherited_from": on_deck if on_deck != deck else None, "sources": views}


@app.put("/api/sources")
def put_sources(deck: str = Query(...), entries: list[dict] = Body(...)):
    if entries:
        SOURCES[deck] = entries
    else:
        SOURCES.pop(deck, None)
    return {"deck": deck, "sources": entries}


@app.get("/api/vault/notes")
def vault_notes(q: str = Query("")):
    return [n for n in VAULT_NOTES if q.lower() in n.lower()][:50]


@app.get("/api/chat/status")
def chat_status():
    return {"configured": True, "model": "claude-mock-1"}


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    note_ids = body.get("note_ids") or []
    nid = note_ids[0] if note_ids else next(iter(NOTES))

    async def gen():
        for chunk in ["Cette carte ", "mélange deux idées. ", "Je propose de la reformuler.\n"]:
            yield f"event: text\ndata: {json.dumps({'delta': chunk})}\n\n"
            await asyncio.sleep(0.15)
        note = NOTES.get(nid)
        fields = dict(note["fields"]) if note else {"Text": "?"}
        first = next(iter(fields))
        fields[first] = fields[first] + " (reformulé par le mock)"
        if "Back Extra" in fields:
            fields["Back Extra"] = ""
        payload = {
            "id": "toolu_mock",
            "kind": "edit",
            "input": {
                "note_id": nid,
                "fields": fields,
                "rationale": "La raison du flag portait sur le capteur : je précise le champ.",
            },
        }
        yield f"event: proposal\ndata: {json.dumps(payload)}\n\n"
        await asyncio.sleep(0.1)
        done = {"stop_reason": "end_turn", "usage": {"input_tokens": 12, "output_tokens": 34}}
        yield f"event: done\ndata: {json.dumps(done)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=5071, log_level="warning")
