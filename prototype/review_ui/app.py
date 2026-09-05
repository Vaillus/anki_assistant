"""PROTOTYPE — throwaway. Three UI variants for the flagged-card review screen.

Question answered: what should the review UI look like (deck picker, card queue, sources, chat)?
Run:  uv run --group proto python prototype/review_ui/app.py   →  http://localhost:5099/prototype/review?variant=A
Read-only against AnkiConnect. Nothing is written to Anki. Chat is a stub.
"""

from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from anki_assistant import AnkiClient, SourceStore
from anki_assistant.sources import Source

HERE = Path(__file__).parent
app = FastAPI(title="PROTOTYPE review ui")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
anki = AnkiClient()
store = SourceStore()

# Demo-only sources so the panels have something to show. Not saved to sources.json.
DEMO_SOURCES = {
    "courant::00-Thèse": Source(
        deck="courant::00-Thèse", kind="obsidian", target="Allocation sur des angles disjoints"
    ),
    "courant::01-AI::little book of deep learning": Source(
        deck="courant::01-AI::little book of deep learning",
        kind="pdf",
        target="~/Documents/the-little-book-of-deep-learning.pdf",
        note="pages 41-52 (chapitre 4)",
    ),
    "courant::01-AI::On the measure of intelligence": Source(
        deck="courant::01-AI::On the measure of intelligence",
        kind="obsidian",
        target="On the measure of intelligence",
    ),
}


def effective_source(deck: str) -> Source | None:
    src = store.get(deck)
    if src:
        return src
    parts = deck.split("::")
    for cut in range(len(parts), 0, -1):
        cand = "::".join(parts[:cut])
        if cand in DEMO_SOURCES:
            return DEMO_SOURCES[cand]
    return None


_CLOZE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)


def render_field(raw: str) -> str:
    """Anki field HTML -> safe display HTML with cloze spans and line breaks kept."""
    txt = re.sub(r"<br\s*/?>", "\n", raw)
    txt = re.sub(r"</(p|div|li)>", "\n", txt)
    txt = re.sub(r"<img[^>]*alt=\"([^\"]*)\"[^>]*>", r"\1", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html.unescape(txt)
    txt = html.escape(txt.strip())
    txt = _CLOZE.sub(lambda m: f'<span class="cloze" data-n="{m.group(1)}">{m.group(2)}</span>', txt)
    return txt.replace("\n", "<br>")


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/prototype/review?variant=A")


@app.get("/prototype/review", response_class=HTMLResponse)
def page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/decks")
def api_decks() -> list[dict]:
    names = anki.deck_names()
    flagged = anki.find_cards("-flag:0")
    own = Counter(c.deck_name for c in flagged)
    total: Counter[str] = Counter()
    for deck, n in own.items():
        parts = deck.split("::")
        for cut in range(1, len(parts) + 1):
            total["::".join(parts[:cut])] += n
    out = []
    for name in names:
        src = effective_source(name)
        out.append(
            {
                "name": name,
                "depth": name.count("::"),
                "leaf": name.split("::")[-1],
                "flagged_own": own.get(name, 0),
                "flagged_total": total.get(name, 0),
                "source": src.kind if src else None,
            }
        )
    return out


@app.get("/api/notes")
def api_notes(deck: str = Query(...), limit: int = 300) -> dict:
    """Notes of a deck (the review unit), flagged first. A note is flagged if any card is."""
    cards = anki.cards_in_deck(deck)
    by_note: dict[int, list] = defaultdict(list)
    for c in cards:
        by_note[c.note_id].append(c)
    notes = []
    for note_id, cs in by_note.items():
        first = cs[0]
        flagged_cards = [c for c in cs if c.flag]
        notes.append(
            {
                "note_id": note_id,
                "deck": first.deck_name,
                "model": first.model_name,
                "n_cards": len(cs),
                "flagged": bool(flagged_cards),
                "flag_colors": sorted({c.flag_name for c in flagged_cards}),
                "fields": {k: render_field(v) for k, v in first.fields.items()},
                "reason": render_field(first.fields.get("Back Extra", "")) if "Back Extra" in first.fields else "",
            }
        )
    notes.sort(key=lambda n: (not n["flagged"], n["note_id"]))
    return {"deck": deck, "total": len(notes), "flagged": sum(n["flagged"] for n in notes), "notes": notes[:limit]}


@app.get("/api/source")
def api_source(deck: str = Query(...)) -> dict | None:
    src = effective_source(deck)
    if not src:
        return None
    excerpt = ""
    if src.kind == "obsidian":
        p = src.note_path(store.vault)
        if p and p.exists():
            excerpt = p.read_text(encoding="utf-8")[:4000]
    return {
        "kind": src.kind,
        "target": src.target,
        "note": src.note,
        "on_deck": src.deck,
        "inherited": src.deck != deck,
        "exists": src.exists(store.vault),
        "uri": src.uri(store.vault),
        "excerpt": excerpt,
    }


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=5099, reload=True, app_dir=str(HERE))
