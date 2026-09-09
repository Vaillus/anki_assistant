# Chat

> The Chat tab in column 3: a conversation with Claude that has the deck, the notes under review and the corpus index in context, that **reads** sources and the rest of the deck's notes on demand, and that answers with **proposals** the user applies in one click and can undo.

General terms (*corpus*, *source*, *anchor*, *decision*…) are defined in [00-overview.md § Vocabulary](./00-overview.md#vocabulary).

## Mental model

Claude enters each turn already knowing the note the user is looking at, the deck's list of sources, and any sources or notes the user has attached to the conversation. That context travels in a **system prompt** the server rebuilds on every request from four blocks (detailed in [Context](#context)).

From there Claude can do two things. It can **read** — search for other notes, fetch a source's full text, list decks — to pull in information the system prompt does not already carry. And it can **propose** — suggest a structured change (edit a note, split it, create a new one, fix a source passage) that appears as a card in the UI. Nothing is written until the user clicks « Appliquer ».

The **client** is the browser page (`app.js`); the **server** is the local FastAPI process — "server" never means Anthropic's side. The rest of this spec details each piece: conversation lifetime, the four prompt blocks, the tools, undo, and the server API.

## Purpose

Discussing a flagged note is where the time goes: is the card wrong or just badly worded, should it be two cards, what does the source actually say. The chat makes that discussion fast because Claude has the notes in front of it and can retrieve sources on demand, and because its suggestions come out as structured changes the user applies directly rather than prose to retype.

The conversation itself is free-form. The infrastructure's job is to make **what Claude can see** explicit and cheap to control — which note, which source, what it retrieved — not to script how Claude should reason about a card.

## Conversation lifetime

A conversation is the message log plus the notes and sources attached to it. It is stored per deck, in browser memory, and kept across notes: moving to the next flagged note does not reset it.

Two things reset it:

- **Changing deck.** Silent, no confirmation.
- **« nouvelle conversation »**, an icon button at the right of the column-3 header, shown only on the Chat tab. Same reset minus the deck: drops the log, the attached notes (see [Context](#what-claude-receives)) and the draft. Disabled while a reply streams or when there is nothing to clear (empty log and no attached note — an unsent draft alone does not count). No confirmation either.

## Context

The server keeps nothing between requests. On every **turn** — one user message and the reply to it — the client sends the whole conversation, and the server rebuilds the system prompt from the four blocks below.

### What Claude receives

1. **Standing instructions** — reply in the user's language (French by default); when suggesting a concrete change, use the proposal tools rather than describing it in prose; fields are raw Anki HTML with cloze markers, keep the syntax valid. It also states the conventions of the note collection (field syntax, context headers) from [notes.md](./notes.md) as facts, so that Claude reads and writes fields the way they are actually shaped. Nothing else: no instructions on terseness, on when to read, on how many notes an idea deserves, or on what to check before creating. Those are the conversation's business.

2. **Corpus index** — one line per source of the deck's corpus: id, kind, target, page range, optional note, and a ⚠ marker when the file is missing. Sources anchored to the selected note are marked « ancrée à la note #… » ([sources.md § Anchors](./sources.md#anchors)). **The corpus index contains no source text.** It tells Claude what sources exist and which ones it has not read.

3. **Attached sources** — the full text of each source the user has attached (see [How source text enters context](#how-source-text-enters-context) below): its header and the output of `Source.text()`, truncation warning included. An **attached source** stays in the prompt on every turn until the user removes it, the deck changes, or the conversation resets. Total attached text is capped at 150 000 characters (sources included in order, the last one truncated); the cap is stated in the prompt.

4. **Notes in context** — the selected note, plus the **attached notes**: notes the user added to the context with « → chat », sent as `note_ids` next to the selected note and kept across turns until removed or the deck changes. For each note: id, **note type** (Anki's schema for a note — its fields, card templates and CSS; `model` in Anki's API, but "note type" throughout this spec so that "model" only ever means the LLM), tags, the flagged cards as cloze labels (« c2 »), the **raw field values** (each field as stored: HTML with the `{{c1::…}}` markers intact), the reason, and the note's anchors (source ids and targets) if any. Claude must produce fields in the same raw syntax.

### Prompt caching

The system prompt includes the selected note (block 4) so that Claude always knows which note the user is looking at. Because the conversation is per-deck and not per-note, block 4 changes every time the user moves to the next note in the **queue** (the ordered list of flagged notes in column 2) — but blocks 1–3 (instructions, corpus index, attached sources) stay the same.

Without caching, the Anthropic API would process all four blocks from scratch on every request, even though only block 4 changed. The API provides a way to avoid this.

A system prompt is sent not as one string but as a list of **content blocks**. Any block can carry the mark `cache_control: ephemeral`. That mark says: take every block from the start of the prompt through this one and cache the result of processing them. On the next request, if those blocks are byte-identical, reuse the cached result instead of re-processing. The reusable run of blocks is the **cached prefix**; everything after it is processed on every request. ("Ephemeral" means the cache entry may be evicted after a few minutes of inactivity — it is a cost optimisation, not a guarantee.)

The mark goes on block 3. That makes the cached prefix blocks 1–3, and selecting another note only re-processes block 4. Attaching or detaching a source, or changing deck, invalidates the prefix — that is the expected and acceptable cost.

### How source text enters context

Source text enters the system prompt in two ways, both visible to the user:

- **The user attaches it.** Above the input, a row of **chips** — small clickable labels, one per source — lists the corpus. Anchored sources appear as individual chips (« ⚓ joindre différentiabilité »); the rest are under a « + source » menu. Clicking a chip attaches that source (block 3 above); the × on the chip detaches it. Attaching is per source, never the whole corpus at once — a large corpus is exactly the scenario this mechanism is designed for.

- **Claude reads it.** The `read_source` tool ([Read tools](#the-tools) below) returns a source's text into the current turn, and the reply shows a muted line — « lit : différentiabilité (obsidian, 3 200 car.) » — so the user always knows what was loaded and when. Reads are not replayed on later turns; attaching is the way to keep a source in front of Claude.

## Read tools

Claude sees only what the system prompt pushes (deck name, corpus index, attached sources, notes in context). Everything else it **pulls** through read tools, which the server executes against Anki or the `SourceStore` and returns as text. Read tools never write.

### The tool-use loop

A single turn (one user message and the reply to it, see [Context](#context)) can involve multiple round trips between the server and the LLM. Each round trip is a **model call**: the server sends the conversation to the LLM, and the LLM responds. If the response contains tool invocations, the server executes them, appends the results to the conversation, and makes another model call. This cycle is the **tool loop**: it repeats until the LLM responds without invoking tools, or the cap is reached.

The cap is `MAX_TOOL_LOOPS` = 8 model calls per turn. A realistic sequence is: search (brief) → get 5 notes → propose → comment, four model calls.

Tool results land in the conversation that is sent on the next model call within the same turn, but they are **not** replayed to the LLM across turns (the server is stateless and only the message text is re-sent). Instead, the client summarises each read into the assistant text between turns as a bracket notation — e.g. « [lecture: search_notes re:lagrang → 6 notes] » — so Claude knows what it has already looked at. If Claude needs the actual content again, it reads again. Anki is local; a re-read costs a model call and tokens, not meaningful latency.

Each read is streamed to the client as a `reading` event (`{ id, tool, input, summary }`) and displayed as a **reading summary**: a muted line in the reply (e.g. « lit : 140 notes du deck »). The result text itself stays server-side. Failures (unknown note, Anki unreachable) come back to Claude as an error tool result, not as a client-visible error.

### The tools

| Tool | Input | Returns |
|---|---|---|
| `list_decks` | `{}` | The deck tree: one line per deck, indented by depth, with own and rolled-up flagged-note counts. |
| `search_notes` | `{ query, detail?, fields? }` | The matching notes, in the shape controlled by `detail` (see below). |
| `get_notes` | `{ note_ids }` | The full notes, in the same format as the notes in context (raw field values, tags, flags, reason). |
| `get_note_type` | `{ model }` | Field names, card templates (front / back) and CSS of a note type. |
| `read_source` | `{ source_id }` | The source's header and `Source.text()` (capped at 60 000 characters, warning included). Unknown id → error result. |

**`search_notes`** is the entry point into the rest of the note collection beyond the notes in context. Claude controls both **what to match** and **what comes back**, because the two questions it serves need different response shapes:

- `query` is Anki search syntax, so matching happens inside Anki, not by Claude scanning text: `re:lagrang`, `Text:*KKT*`, `tag:convexité`, `flag:1`, or `deck:courant::00-Thèse` for everything. The server scopes the query to the current deck and its sub-decks unless the query names a deck explicitly.

- `detail` controls the response shape:
  - `count` — how many notes match, nothing else.
  - `brief` (default) — one line per note: `#id`, deck (when it differs from the current one), `⚑` if flagged, each field's plain text truncated to 120 characters. Roughly 100 characters per note.
  - `full` — raw field values, tags, flags, reason. The same format as the notes in context.

  « Are there other cards on this subject? » is a narrow query with `brief`; « which other notes have this format? » is a wide query with `full`, because truncated plain text drops exactly what such an audit needs (the **context header** — a `<div class="context">…</div>` topic label at the start of a field, see [notes.md](./notes.md) — the HTML, the cloze structure).

- `fields` optionally restricts `full` results to named fields (e.g. `["Text"]`), for audits where the other fields are noise.

Results are ordered flagged first. A `full` result that would exceed 100 000 characters is not returned: the tool responds with the count and asks Claude to narrow the query or pass `fields`, so one badly scoped search cannot exhaust the token budget available for the rest of the turn.

## Proposal tools

Read tools let Claude look; proposal tools are how it suggests a change. A proposal is not a write: it is a structured description of a change (which note, which fields, why) that the client renders as a card, and only the user's click on that card performs the write.

Claude gets seven proposal tools. Each invocation is streamed to the client as a `proposal` event and rendered as a **proposal card**: a UI element that shows the diff against the current note (unchanged fields collapsed, changed fields with before/after rendered through the **display renderer** — the module `render.py` that converts raw field values into styled HTML) and an « Appliquer » button that calls the matching review or sources endpoint ([review.md § API](./review.md#api), [sources.md § API](./sources.md#api)). Tools return `"ok"` to Claude immediately — applying is the user's decision, not Claude's.

| Tool | Input | Applies via |
|---|---|---|
| `propose_edit` | `{ note_id, fields: {name: raw}, tags?: [], rationale }` | `PATCH /api/notes/{id}` |
| `propose_split` | `{ note_id, original: {fields} \| null, new_notes: [{ model?, fields }], rationale }` (`model` = Anki note type name) | `POST /api/notes/{id}/split` |
| `propose_create` | `{ fields, model?, source_ids?, rationale }` (`model` = Anki note type name; deck = current deck, tags = selected note's; `source_ids` defaults to the selected note's anchors) | `POST /api/notes` |
| `propose_move` | `{ note_id, deck, rationale }` | `POST /api/notes/{id}/move` |
| `propose_bulk_edit` | `{ edits: [{ note_id, fields: {name: raw} }], rationale }` | `PATCH /api/notes/{id}`, once per edit |
| `propose_create_source` | `{ name, content, anchor_note_ids?, rationale }` (deck = current deck) | `POST /api/sources/notes` |
| `propose_edit_source` | `{ source_id, old, new, rationale }` | `PATCH /api/sources/{source_id}/text` |

`fields` in proposals are **raw field values** (HTML, cloze markers kept). Once applied, the proposal card turns into « appliqué ✓ » and the queue refreshes as after any decision.

`propose_bulk_edit` is for the same change across many notes (add a context header to 20 notes, fix a recurring typo; see [notes.md § Context header](./notes.md#context-header)). Its proposal card is a table — one row per note: `#id`, then each changed field as before → after in plain text — with a single « Appliquer tout ». The client applies the edits in order and stops at the first failure: rows already applied stay applied and are marked, the error is shown on the card, and « Appliquer tout » becomes « Reprendre » for the rest. The queue refreshes once at the end.

**Source proposals** are how a conversation ends up in the vault. `propose_create_source` renders the Markdown `content` and an editable `name` field (vault-relative path, no default folder) so the user can adjust it before applying; applying creates the file, adds it to the deck's corpus, and (with `anchor_note_ids`) appends it to those notes' anchors. `propose_edit_source` shows `old` → `new` as a diff on the source's text; applying performs the exact-match replacement ([sources.md § Writing to the vault](./sources.md#writing-to-the-vault)). Neither writes anything until « Appliquer » — the vault is the user's, and a refused replacement (passage not found or ambiguous) is shown on the card, not retried.

The typical *clarify a card* flow needs no special instruction: the user attaches an anchored source, asks what is unclear, and Claude may propose a source edit first and a card edit after — or the reverse, or neither. The typical *create from a conversation* flow is `propose_create_source` followed by `propose_create` calls carrying the new source id. That id exists before the user applies anything: the server generates it when it streams the `propose_create_source` call, returns it to Claude in the tool result, and sends it to the client in the `proposal` event (`source_id`); applying passes it to `POST /api/sources/notes` so the created source carries the id Claude was told.

Tool results are fed back so Claude can continue (e.g. propose an edit, then explain). One assistant turn may contain several proposals.

## Undo

Applying a proposal writes straight into Anki, so a wrong click needs a way back. Undo exists only for the proposal kinds where "back" means restoring field values (`edit`, `bulk_edit`, `edit_source`); the mechanism is a copy taken just before the write.

Applying an `edit` or `bulk_edit` proposal first takes a **snapshot**: for each note touched, a copy of its raw field values, tags, and flagged card ids, read from the queue in browser memory (or fetched with `GET /api/notes/{id}` when the note is not in the queue). The snapshot is stored on the proposal object, next to `applied`, and lives as long as the conversation — it is dropped on conversation reset or page reload.

An applied `edit` / `bulk_edit` proposal card shows « Annuler ». Clicking it re-applies the snapshot through `PATCH /api/notes/{id}` with `{ fields, tags, unflag: false, reflag: [card ids] }`, so the note comes back flagged and in the queue. Before writing, the client fetches the note and compares its current fields with the values the proposal wrote; if they differ (the note was edited since, in the app or in Anki) the undo is refused with « modifiée depuis, annulation impossible » and nothing is written. After a successful undo the proposal card returns to its unapplied state, « Appliquer » included.

An applied `edit_source` proposal card also shows « Annuler »: it calls the same endpoint with `old` and `new` swapped, and the exact-match rule refuses it if the passage changed since (« modifiée depuis, annulation impossible »).

No undo for `split`, `create`, `move`, and `create_source` — undoing them would mean deleting or moving notes or files. Their proposal cards have no « Annuler ». Nothing is persisted beyond Anki and the vault: a page reload or a deck change drops the snapshots with the conversation.

## LLM configuration

Uses the Anthropic Python SDK (`anthropic`), streaming, with the model id from `ANKI_CHAT_MODEL` (default `claude-opus-4-6`). `max_tokens` is 8 192 (thinking tokens, when enabled, count against this budget). API key from `ANTHROPIC_API_KEY` (environment variable or `.env` via `python-dotenv`, loaded in `web/main.py`).

System prompt layout and the `cache_control: ephemeral` placement: see [Prompt caching](#prompt-caching).

## API

`POST /api/chat` — body:

```json
{ "deck": "courant::00-Thèse", "note_ids": [1732375559262], "source_ids": ["k7q2vd"], "flagged_count": 7,
  "messages": [ { "role": "user", "content": "Cette carte est trop vague, tu proposes quoi ?" } ] }
```

`source_ids` is optional (default: no attached sources). `flagged_count` is optional (the deck's flagged-note count, for the prompt header). `messages` is the prior conversation as plain `{role, content: string}` turns — assistant text only; prior proposals and reads are not replayed. The client summarises them into the assistant text as bracket notations: « [proposition: edit #5262] », « [proposition: bulk_edit ×19] », « [lecture: search_notes re:lagrang → 6 notes] », « [lecture: read_source → différentiabilité] ».

Response: `text/event-stream`, SSE events:

```
event: text        data: {"delta": "Je pense que…"}
event: reading     data: {"id": "toolu_…", "tool": "search_notes", "input": {…}, "summary": "6 notes"}
event: proposal    data: {"id": "toolu_…", "kind": "edit", "input": {…}}            (+ "source_id" when kind is create_source)
event: done        data: {"stop_reason": "end_turn", "usage": {"input_tokens": 1234, "output_tokens": 210}}
event: error       data: {"detail": "…"}
```

`GET /api/chat/status` → `{ "configured": true, "model": "…" }`. When `configured` is false the Chat tab shows: « Ajoute ANTHROPIC_API_KEY dans .env puis relance anki-web » and disables the input.

## Module `chat.py`

```python
def build_system(deck, corpus_index, attached, notes, flagged_count=None) -> list[dict]   # content blocks, cached
def tools() -> list[dict]                       # proposal tools + read tools
def format_decks(decks) -> str                  # the text a read tool returns
def format_notes_brief(notes, deck) -> str        # search_notes, detail=brief
def format_notes(notes) -> str
def format_note_type(note_type) -> str
def format_source(source, text) -> str             # what read_source returns / an attached block
async def stream_chat(client, deck, note_ids, source_ids, messages, load_note, load_corpus,
                      load_source, read_tools, model=None, flagged_count=None) -> AsyncIterator[ChatEvent]
```

`load_corpus(deck)` returns the corpus index entries (headers and anchors, no text); `load_source(source_id)` returns `(Source, SourceText)` and backs both the attached-source blocks and `read_source`. `read_tools` maps a read tool name to a callable `(input: dict) -> str`; `routes_chat.py` builds it from `review.py` / `AnkiClient` and the `format_*` functions, so `chat.py` imports neither FastAPI nor Anki. Unit tests use a fake Anthropic client that replays a recorded stream and a dict of fake read tools.

## Out of scope for v1

Retrieval inside long PDFs (the user sets `pages` instead), persistence of conversations, Claude acting without a click, undo of split / create / move / create_source, Claude editing note types (CSS, templates — read-only through `get_note_type`), creating or editing PDF sources.
