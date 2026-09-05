# Chat

> The Chat tab in column 3: a conversation with Claude that has the deck, its corpus and the notes under review in context, and that answers with **proposals** the user applies in one click.

## Purpose

Discussing a flagged note is where the time goes: is the card wrong or just badly worded, should it be two cards, what does the source actually say. The chat makes that discussion fast because Claude already has everything in front of it, and because its suggestions come out as structured changes, not prose to retype.

## Context

Each request carries the whole conversation (stateless server). The server rebuilds the system prompt on every call from:

1. **Deck**: name, count of flagged notes.
2. **Corpus**: for each source of `SourceStore.corpus(deck)`, its header (kind, target, pages) and `Source.text()`. Total corpus text is capped at 150 000 characters (sources in order, the last one truncated); the cap and any per-source warning are stated in the prompt so Claude knows what it cannot see.
3. **Notes in context**: the selected note plus the notes the user attached with « → chat ». For each: `note_id`, model, tags, raw fields, and the `reason`. Raw fields (with `{{c1::…}}` markers and HTML) — Claude must produce fields in the same syntax.
4. **Standing instructions**: reply in the user's language (French by default); be terse; when suggesting a concrete change use the tools rather than describing it; keep cloze syntax valid; never invent facts absent from the corpus — say when the corpus does not cover the point; one note = one idea; prefer several short notes over a long one; when a note has a `reason`, address it first.

The frontend keeps the conversation in memory per deck and clears it when the deck changes. Attached notes (`→ chat`) are sent as `note_ids` alongside the selected note, and stay attached across turns until removed (×) or the deck changes.

## Proposals (tools)

Claude gets four tools. Each call is streamed to the client as a `proposal` event and rendered as a card with an « Appliquer » button that calls the matching review endpoint ([review.md](./review.md#api)). Tools return `"ok"` to Claude immediately — applying is the user's decision, not Claude's.

| Tool | Input | Applies via |
|---|---|---|
| `propose_edit` | `{ note_id, fields: {name: raw}, tags?: [], rationale }` | `PATCH /api/notes/{id}` |
| `propose_split` | `{ note_id, original: {fields} \| null, new_notes: [{ model?, fields }], rationale }` | `POST /api/notes/{id}/split` |
| `propose_create` | `{ fields, model?, rationale }` (deck = current deck, tags = selected note's) | `POST /api/notes` |
| `propose_move` | `{ note_id, deck, rationale }` | `POST /api/notes/{id}/move` |

`fields` in proposals are **raw Anki field values** (HTML allowed, cloze markers kept). The proposal card shows the diff against the current note: unchanged fields collapsed, changed fields with before/after rendered through the display renderer. Once applied, the card turns into « appliqué ✓ » and the queue refreshes as after any decision.

Tool results are fed back so Claude can continue (e.g. propose an edit, then explain). One assistant turn may contain several proposals.

## Model

Use the Anthropic Python SDK (`anthropic`), streaming, with the model id from `ANKI_CHAT_MODEL` (default: the current recommended Claude model per the `claude-api` skill reference — check it when implementing; do not hardcode a guess). `max_tokens` 8 192 (Opus 5 thinks by default and thinking tokens count against it). System prompt as text blocks in this order: standing instructions, corpus, deck + notes; the corpus block carries `cache_control: ephemeral` so the cached prefix depends only on the deck and survives a change of selected note. API key from `ANTHROPIC_API_KEY` (env or `.env` via `python-dotenv`, loaded in `web/main.py`).

## API

`POST /api/chat` — body:

```json
{ "deck": "courant::00-Thèse", "note_ids": [1732375559262], "flagged_count": 7,
  "messages": [ { "role": "user", "content": "Cette carte est trop vague, tu proposes quoi ?" } ] }
```

`flagged_count` is optional (the deck's flagged-note count, for the prompt header). `messages` is the prior conversation as plain `{role, content: string}` turns (assistant text only; prior proposals are not replayed — they are summarised by the client into the assistant text as « [proposition: edit #5262] »).

Response: `text/event-stream`, events:

```
event: text        data: {"delta": "Je pense que…"}
event: proposal    data: {"id": "toolu_…", "kind": "edit", "input": {…}}
event: done        data: {"stop_reason": "end_turn", "usage": {"input_tokens": 1234, "output_tokens": 210}}
event: error       data: {"detail": "…"}
```

`GET /api/chat/status` → `{ "configured": true, "model": "…" }`. When `configured` is false the Chat tab shows: « Ajoute ANTHROPIC_API_KEY dans .env puis relance anki-web » and disables the input.

## Module `chat.py`

```python
def build_system(deck, corpus_texts, notes) -> list[dict]   # content blocks, cached
def tools() -> list[dict]
async def stream_chat(client, store, deck, note_ids, messages) -> AsyncIterator[ChatEvent]
```

No FastAPI imports in `chat.py`; `routes_chat.py` turns `ChatEvent`s into SSE lines. Unit tests use a fake Anthropic client that replays a recorded stream.

## Out of scope for v1

Retrieval inside long PDFs (the user sets `pages` instead), persistence of conversations, Claude acting without a click.
