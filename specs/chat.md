# Chat

> The Chat tab in column 3: a conversation with Claude that has the deck, the notes under review and the *index* of the corpus in context, that **reads** sources and the rest of the collection on demand, and that answers with **proposals** the user applies in one click and can undo.

## Purpose

Discussing a flagged note is where the time goes: is the card wrong or just badly worded, should it be two cards, what does the source actually say. The chat makes that discussion fast because Claude has the notes in front of it and the sources one click away, and because its suggestions come out as structured changes, not prose to retype.

The conversation itself is free-form. The infrastructure's job is to make **what Claude can see** explicit and cheap to control — which note, which source, what it went and read — not to script how Claude should reason about a card.

## Context

Each request carries the whole conversation (stateless server). The server rebuilds the system prompt on every call from:

1. **Deck**: name, count of flagged notes.
2. **Corpus index**: one line per source of `SourceStore.corpus(deck)` — `id`, kind, target, pages, note, ⚠ if missing — with « ancrée à la note #… » on each source the selected note is anchored to ([sources.md](./sources.md#anchors)). **No source text is pushed by default.** The index tells Claude what exists and what it has not read.
3. **Attached sources**: the text of each source in `source_ids`, with its header and `Source.text()` warning. Total attached text is capped at 150 000 characters (in order, the last one truncated) and the cap is stated.
4. **Notes in context**: the selected note plus the notes the user attached with « → chat ». For each: `note_id`, model, tags, the flagged cards (as cloze labels, « c2 »), raw fields, the `reason`, and its anchors (source ids and targets) if any. Raw fields (with `{{c1::…}}` markers and HTML) — Claude must produce fields in the same syntax.
5. **Standing instructions**, kept to what the tooling needs: reply in the user's language (French by default); when suggesting a concrete change use the proposal tools rather than describing it; fields are raw Anki HTML with cloze markers, keep the syntax valid. Plus one **fact about the collection**, not a rule of conduct: many notes start with a *context header* — a short topic label such as « Stone's Model - FAB and KKT Conditions: » — carried in a `<div class="context">…</div>` as the first line of the field, distinct from a first line that is the grammatical start of the sentence. Nothing else: no instructions on terseness, on when to read, on how many notes an idea deserves, or on what to check before creating. Those are the conversation's business.

### Source text enters context in two ways, both visible

- **The user attaches it.** Above the input, a chip row lists the corpus: the anchored sources first, one chip each « ⚓ joindre différentiabilité », the others under a « + source » menu. A clicked chip becomes an attached source, sent as `source_ids` on every turn until removed (×), a deck change, or « nouvelle conversation ». Attaching is per source, never « tout le corpus » — a big corpus is exactly what this is for.
- **Claude reads it.** The `read_source` tool below returns a source's text into the turn. It shows in the reply as « lit : différentiabilité (obsidian, 3 200 car.) » like every other read, so the user always knows what was loaded and when. Reads are not replayed across turns; attaching is the way to keep a source in front of Claude.

The frontend keeps the conversation in memory per deck and clears it when the deck changes. Attached notes (`→ chat`) are sent as `note_ids` alongside the selected note, and stay attached across turns until removed (×) or the deck changes. Attached sources follow the same rules.

## Conversation lifetime

One conversation per deck, kept across notes: moving to the next flagged note does **not** reset it, because a discussion often spans several related cards. Since the queue is reviewed note by note, the user needs a way to start over without changing deck: a « nouvelle conversation » icon button sits at the right of the column-3 header, next to the tabs, and is shown only on the Chat tab.

Clicking it drops the log, the attached notes and the draft — the same reset as a deck change, minus the deck. It is disabled while a reply streams, and when there is nothing to clear (empty log and no attached note; an unsent draft alone does not count, since typing does not re-render the header). No confirmation: a deck change already discards a conversation silently, and the context (deck, corpus, selected note) is rebuilt on the next message anyway.

## Reading (tools)

Claude sees only what the prompt pushes (deck name, corpus index, attached sources, notes in context). Everything else it **pulls** through read tools, which the server executes against Anki or the source store and answers with the data as text. They never write.

| Tool | Input | Returns |
|---|---|---|
| `list_decks` | `{}` | The deck tree: one line per deck, indented by depth, with own / rolled-up flagged-note counts. |
| `list_deck_notes` | `{ deck? }` (default: current deck) | The **deck index** of the deck and its sub-decks, flagged first. |
| `search_notes` | `{ query, limit? }` (Anki search syntax, default limit 50) | A deck index of the matching notes. |
| `get_notes` | `{ note_ids }` | The full notes, in the same format as the notes in context (raw fields, tags, flags, reason). |
| `get_note_type` | `{ model }` | Field names, card templates (front / back) and CSS of a note type. |
| `read_source` | `{ source_id }` | The source's header and `Source.text()` (capped at 60 000 characters, warning included). Unknown id → error result. |

A **deck index** is compact on purpose: one line per note — `#id`, deck when it differs from the one asked for, `⚑` if flagged, then each field's plain text truncated to 120 characters. 140 notes ≈ 15 000 characters. Tool results land in the conversation, not in the cached prefix, and are re-sent on every later call of the same turn, so Claude reads the index first and `get_notes` only the few it needs.

Each read is streamed to the client as a `reading` event (`{ id, tool, input, summary }`) and shown as a muted line in the reply (« lit : 140 notes du deck »). The result text itself stays server-side. Failures (unknown note, Anki unreachable) come back to Claude as an error tool result, not as a chat error.

**Across turns** results are not replayed (the server is stateless and only text is resent): the client summarises each read into the assistant text as « [lecture: list_deck_notes → 140 notes] » so Claude knows what it has already looked at, and reads again if it needs the content. Anki is local; a re-read costs a round trip and tokens, not time.

The tool loop runs at most `MAX_TOOL_LOOPS` = 8 model calls per turn: a realistic sequence is index → get 5 notes → propose → comment, four calls.

## Proposals (tools)

Claude gets seven proposal tools. Each call is streamed to the client as a `proposal` event and rendered as a card with an « Appliquer » button that calls the matching review or sources endpoint ([review.md](./review.md#api), [sources.md](./sources.md#api)). Tools return `"ok"` to Claude immediately — applying is the user's decision, not Claude's.

| Tool | Input | Applies via |
|---|---|---|
| `propose_edit` | `{ note_id, fields: {name: raw}, tags?: [], rationale }` | `PATCH /api/notes/{id}` |
| `propose_split` | `{ note_id, original: {fields} \| null, new_notes: [{ model?, fields }], rationale }` | `POST /api/notes/{id}/split` |
| `propose_create` | `{ fields, model?, source_ids?, rationale }` (deck = current deck, tags = selected note's; `source_ids` defaults to the selected note's anchors) | `POST /api/notes` |
| `propose_move` | `{ note_id, deck, rationale }` | `POST /api/notes/{id}/move` |
| `propose_bulk_edit` | `{ edits: [{ note_id, fields: {name: raw} }], rationale }` | `PATCH /api/notes/{id}`, once per edit |
| `propose_create_source` | `{ name, content, anchor_note_ids?, rationale }` (deck = current deck) | `POST /api/sources/notes` |
| `propose_edit_source` | `{ source_id, old, new, rationale }` | `PATCH /api/sources/{source_id}/text` |

`fields` in proposals are **raw Anki field values** (HTML allowed, cloze markers kept). The proposal card shows the diff against the current note: unchanged fields collapsed, changed fields with before/after rendered through the display renderer. Once applied, the card turns into « appliqué ✓ » and the queue refreshes as after any decision.

`propose_bulk_edit` is for the same change on many notes (add a context header to 20 notes, fix a recurring typo). Its card is a table — one row per note: `#id`, then each changed field as before → after in plain text — with a single « Appliquer tout ». The client applies the edits in order and stops at the first failure: rows already applied stay applied and are marked, the error is shown on the card, and « Appliquer tout » becomes « Reprendre » for the rest. The queue refreshes once at the end.

**Source proposals** are how a conversation ends up in the vault. `propose_create_source` renders the Markdown `content` and an editable `name` field (vault-relative path, no default folder) so the user can move it before applying; applying creates the file, adds it to the deck's corpus and, with `anchor_note_ids`, appends it to those notes' anchors. `propose_edit_source` shows `old` → `new` as a diff on the source's text; applying does the exact-match replacement ([sources.md](./sources.md#writing-to-the-vault)). Neither writes anything until « Appliquer » — the vault is the user's, and a refused replacement (passage not found or ambiguous) is shown on the card, not retried.

The typical *clarify a card* flow needs no instruction to happen: the user attaches an anchored source, asks what is unclear, and Claude may propose a source edit first and a card edit after — or the reverse, or neither. The typical *create from a conversation* flow is `propose_create_source` followed by `propose_create` calls carrying the new source id (the create-source result returns it to Claude).

Tool results are fed back so Claude can continue (e.g. propose an edit, then explain). One assistant turn may contain several proposals.

## Undo

Applying `edit` or `bulk_edit` takes a **snapshot** first: for each note touched, its raw fields, tags and flagged card ids, read from the queue in browser memory (or fetched with `GET /api/notes/{id}` when the note is not in the queue). The snapshot is kept on the proposal object, next to `applied`, and lives as long as the conversation.

An applied `edit` / `bulk_edit` card shows « Annuler ». Clicking it re-applies the snapshot through `PATCH /api/notes/{id}` with `{ fields, tags, unflag: false, reflag: [card ids] }`, so the note comes back flagged and in the queue. Before writing, the client fetches the note and compares its current fields with the values the proposal wrote; if they differ (edited since, in the app or in Anki) the undo is refused with « modifiée depuis, annulation impossible » and nothing is written. After a successful undo the card returns to its unapplied state, « Appliquer » included.

An applied `edit_source` card also shows « Annuler »: it calls the same endpoint with `old` and `new` swapped, and the exact-match rule refuses it if the passage changed since (« modifiée depuis, annulation impossible »).

No undo for `split`, `create`, `move` and `create_source` (undoing them means deleting or moving notes or files); their cards have no « Annuler ». This is not a journal: nothing is persisted, and a page reload or a deck change drops the snapshots with the conversation.

## Model

Use the Anthropic Python SDK (`anthropic`), streaming, with the model id from `ANKI_CHAT_MODEL` (default: the current recommended Claude model per the `claude-api` skill reference — check it when implementing; do not hardcode a guess). `max_tokens` 8 192 (Opus 5 thinks by default and thinking tokens count against it). System prompt as text blocks in this order: standing instructions, corpus index + attached sources, deck + notes; the corpus block carries `cache_control: ephemeral` so the cached prefix depends only on the deck and the attached sources, and survives a change of selected note. API key from `ANTHROPIC_API_KEY` (env or `.env` via `python-dotenv`, loaded in `web/main.py`).

## API

`POST /api/chat` — body:

```json
{ "deck": "courant::00-Thèse", "note_ids": [1732375559262], "source_ids": ["k7q2vd"], "flagged_count": 7,
  "messages": [ { "role": "user", "content": "Cette carte est trop vague, tu proposes quoi ?" } ] }
```

`source_ids` is optional (default none — no source text in context). `flagged_count` is optional (the deck's flagged-note count, for the prompt header). `messages` is the prior conversation as plain `{role, content: string}` turns (assistant text only; prior proposals and reads are not replayed — they are summarised by the client into the assistant text as « [proposition: edit #5262] », « [proposition: bulk_edit ×19] », « [lecture: list_deck_notes → 140 notes] », « [lecture: read_source → différentiabilité] »).

Response: `text/event-stream`, events:

```
event: text        data: {"delta": "Je pense que…"}
event: reading     data: {"id": "toolu_…", "tool": "list_deck_notes", "input": {…}, "summary": "140 notes"}
event: proposal    data: {"id": "toolu_…", "kind": "edit", "input": {…}}
event: done        data: {"stop_reason": "end_turn", "usage": {"input_tokens": 1234, "output_tokens": 210}}
event: error       data: {"detail": "…"}
```

`GET /api/chat/status` → `{ "configured": true, "model": "…" }`. When `configured` is false the Chat tab shows: « Ajoute ANTHROPIC_API_KEY dans .env puis relance anki-web » and disables the input.

## Module `chat.py`

```python
def build_system(deck, corpus_index, attached, notes, flagged_count=None) -> list[dict]   # content blocks, cached
def tools() -> list[dict]                       # proposal tools + read tools
def format_decks(decks) -> str                  # the text a read tool returns
def format_deck_index(notes, deck) -> str
def format_notes(notes) -> str
def format_note_type(note_type) -> str
def format_source(source, text) -> str             # what read_source returns / an attached block
async def stream_chat(client, deck, note_ids, source_ids, messages, load_note, load_corpus,
                      load_source, read_tools, model=None, flagged_count=None) -> AsyncIterator[ChatEvent]
```

`load_corpus(deck)` returns the corpus index entries (headers and anchors, no text); `load_source(source_id)` returns `(Source, SourceText)` and backs both the attached blocks and `read_source`. `read_tools` maps a read tool name to a callable `(input: dict) -> str`; `routes_chat.py` builds it from `review.py` / `AnkiClient` and the `format_*` functions, so `chat.py` imports neither FastAPI nor Anki. Unit tests use a fake Anthropic client that replays a recorded stream and a dict of fake read tools.

## Out of scope for v1

Retrieval inside long PDFs (the user sets `pages` instead), persistence of conversations, Claude acting without a click, undo of split / create / move / create_source, Claude editing note types (CSS, templates — read-only through `get_note_type`), creating or editing PDF sources.
