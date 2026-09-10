# Chat

> The conversation pane of the workspace: a conversation with Claude that has the cards of the workspace, the corpus index and the attached sources in context, that **reads** sources and the rest of the deck's notes on demand, and that answers with **proposals** which land on the workspace as versions and cards, written to Anki at validation.

General terms (*corpus*, *source*, *anchor*…) are defined in [00-overview.md § Vocabulary](./00-overview.md#vocabulary); workspace terms (*card*, *version*, *active*, *root*…) in [workspace.md § Vocabulary](./workspace.md#vocabulary).

## Mental model

Claude enters each turn already knowing the cards of the workspace — the note the user opened, the drafts prepared for it, any note pulled in since — the deck's list of sources, and any sources the user has attached to the conversation. That context travels in a **system prompt** the server rebuilds on every request from four blocks (detailed in [Context](#context)).

From there Claude can do two things. It can **read** — search for other notes, fetch a source's full text, list decks, bring notes into the workspace — to pull in information the system prompt does not already carry. And it can **propose** — suggest a structured change (a rewrite of a card, a split, a new note, a move, a fix to a source passage). A proposal on a note becomes a version or a card on the workspace; nothing is written to Anki until the user clicks « Valider » ([workspace.md § Validation](./workspace.md#validation)).

The **client** is the browser page (`app.js`, `workspace.js`); the **server** is the local FastAPI process — "server" never means Anthropic's side. The rest of this spec details each piece: conversation lifetime, the four prompt blocks, the tools, and the server API.

## Purpose

Discussing a flagged note is where the time goes: is the card wrong or just badly worded, should it be two cards, what does the source actually say. The chat makes that discussion fast because Claude has the cards in front of it and can retrieve sources on demand, and because its suggestions come out as structured changes the user reads, retouches and validates directly rather than prose to retype.

The conversation itself is free-form. The infrastructure's job is to make **what Claude can see** explicit and cheap to control — which cards, which are active, which source, what it retrieved — not to script how Claude should reason about a card.

## Conversation lifetime

A conversation is the message log plus the sources attached to it. It belongs to one **workspace** ([workspace.md](./workspace.md)): it starts empty when the workspace opens and is dropped when the workspace closes, whether by validation or by discarding. There is no conversation outside a workspace, no per-deck conversation, and no way to start a new one without closing the workspace.

## Context

The server keeps nothing between requests. On every **turn** — one user message and the reply to it — the client sends the whole conversation and the current state of every card, and the server rebuilds the system prompt from the four blocks below.

### What Claude receives

1. **Standing instructions** — reply in the user's language (French by default); when suggesting a concrete change, use the proposal tools rather than describing it in prose; the next message is about the **active** cards, target them by their workspace id; fields are raw Anki HTML with cloze markers, keep the syntax valid. It also states the conventions of the note collection (field syntax, context headers) from [notes.md](./notes.md) as facts, so that Claude reads and writes fields the way they are actually shaped. Nothing else: no instructions on terseness, on when to read, on how many notes an idea deserves, or on what to check before creating. Those are the conversation's business.

2. **Corpus index** — one line per source of the deck's corpus: id, kind, target, page range, optional note, and a ⚠ marker when the file is missing. Sources anchored to a card of the workspace are marked « ancrée à la note #… » ([sources.md § Anchors](./sources.md#anchors)). **The corpus index contains no source text.** It tells Claude what sources exist and which ones it has not read.

3. **Attached sources** — the full text of each source the user has attached (see [How source text enters context](#how-source-text-enters-context) below): its header and the output of `Source.text()`, truncation warning included. An **attached source** stays in the prompt on every turn until the user removes it or the workspace closes. Total attached text is capped at 150 000 characters (sources included in order, the last one truncated); the cap is stated in the prompt.

4. **Cards of the workspace** — every card, the root first, then in workspace order. For each card: its **workspace id** (`w1`), its `note_id` when it exists in Anki or « brouillon, pas encore dans Anki » otherwise, whether it is **active** or **inactive**, its states (« supprimée », « gardée », « → deck »), its parent when it is a fragment, the deck, the **note type** (Anki's schema for a note — its fields, card templates and CSS; `model` in Anki's API, but "note type" throughout this spec so that "model" only ever means the LLM), tags, the flagged cards as cloze labels (« c2 »), the reason, the anchors (source ids and targets) if any, and the **raw field values of the shown version** (each field as stored: HTML with the `{{c1::…}}` markers intact). When the shown version is not v0, the v0 fields follow under « version d'origine (Anki) », so that Claude sees what has already changed — hand edits included. Intermediate versions are not sent. Claude must produce fields in the same raw syntax.

### Prompt caching

The system prompt includes the cards (block 4) so that Claude always knows what the user is looking at. Block 4 changes on almost every turn — a version was added, a field was edited, a card was toggled — but blocks 1–3 (instructions, corpus index, attached sources) stay the same for the life of the workspace.

Without caching, the Anthropic API would process all four blocks from scratch on every request, even though only block 4 changed. The API provides a way to avoid this.

A system prompt is sent not as one string but as a list of **content blocks**. Any block can carry the mark `cache_control: ephemeral`. That mark says: take every block from the start of the prompt through this one and cache the result of processing them. On the next request, if those blocks are byte-identical, reuse the cached result instead of re-processing. The reusable run of blocks is the **cached prefix**; everything after it is processed on every request. ("Ephemeral" means the cache entry may be evicted after a few minutes of inactivity — it is a cost optimisation, not a guarantee.)

The mark goes on block 3. That makes the cached prefix blocks 1–3, and a change on the workspace only re-processes block 4. Attaching or detaching a source invalidates the prefix — that is the expected and acceptable cost.

### How source text enters context

Source text enters the system prompt in two ways, both visible to the user:

- **The user attaches it.** Above the input, a row of **chips** — small clickable labels, one per source — lists the corpus. Sources anchored to the root appear as individual chips (« ⚓ joindre différentiabilité »); the rest are under a « + source » menu. Clicking a chip attaches that source (block 3 above); the × on the chip detaches it. Attaching is per source, never the whole corpus at once — a large corpus is exactly the scenario this mechanism is designed for.

- **Claude reads it.** The `read_source` tool ([Read tools](#the-tools) below) returns a source's text into the current turn, and the reply shows a muted line — « lit : différentiabilité (obsidian, 3 200 car.) » — so the user always knows what was loaded and when. Reads are not replayed on later turns; attaching is the way to keep a source in front of Claude.

## Read tools

Claude sees only what the system prompt pushes (deck name, corpus index, attached sources, cards). Everything else it **pulls** through read tools, which the server executes against Anki or the `SourceStore` and returns as text. Read tools never write to Anki or the vault; one of them, `add_notes`, changes what the workspace shows.

### The tool-use loop

A single turn (one user message and the reply to it, see [Context](#context)) can involve multiple round trips between the server and the LLM. Each round trip is a **model call**: the server sends the conversation to the LLM, and the LLM responds. If the response contains tool invocations, the server executes them, appends the results to the conversation, and makes another model call. This cycle is the **tool loop**: it repeats until the LLM responds without invoking tools, or the cap is reached.

The cap is `MAX_TOOL_LOOPS` = 8 model calls per turn. A realistic sequence is: search (brief) → add 5 notes → propose → comment, four model calls.

Tool results land in the conversation that is sent on the next model call within the same turn, but they are **not** replayed to the LLM across turns (the server is stateless and only the message text is re-sent). Instead, the client summarises each read into the assistant text between turns as a bracket notation — e.g. « [lecture: search_notes re:lagrang → 6 notes] » — so Claude knows what it has already looked at. If Claude needs the actual content again, it reads again. Anki is local; a re-read costs a model call and tokens, not meaningful latency.

Each read is streamed to the client as a `reading` event (`{ id, tool, input, summary }`) and displayed as a **reading summary**: a muted line in the reply (e.g. « lit : 140 notes du deck »). The result text itself stays server-side. Failures (unknown note, Anki unreachable) come back to Claude as an error tool result, not as a client-visible error.

### The tools

| Tool | Input | Returns |
|---|---|---|
| `list_decks` | `{}` | The deck tree: one line per deck, indented by depth, with own and rolled-up flagged-note counts. |
| `search_notes` | `{ query, detail?, fields? }` | The matching notes, in the shape controlled by `detail` (see below). |
| `get_notes` | `{ note_ids }` | The full notes, in the same format as the cards in context (raw field values, tags, flags, reason). Nothing changes on the workspace. |
| `add_notes` | `{ note_ids, rationale }` | The same text as `get_notes`, **and** the notes become cards of the workspace (see below). |
| `get_note_type` | `{ model }` | Field names, card templates (front / back) and CSS of a note type. |
| `read_source` | `{ source_id }` | The source's header and `Source.text()` (capped at 60 000 characters, warning included). Unknown id → error result. |

**`search_notes`** is the entry point into the rest of the note collection beyond the cards in context. Claude controls both **what to match** and **what comes back**, because the two questions it serves need different response shapes:

- `query` is Anki search syntax, so matching happens inside Anki, not by Claude scanning text: `re:lagrang`, `Text:*KKT*`, `tag:convexité`, `flag:1`, or `deck:courant::00-Thèse` for everything. The server scopes the query to the current deck (the root's deck) and its sub-decks unless the query names a deck explicitly.

- `detail` controls the response shape:
  - `count` — how many notes match, nothing else.
  - `brief` (default) — one line per note: `#id`, deck (when it differs from the current one), `⚑` if flagged, each field's plain text truncated to 120 characters. Roughly 100 characters per note.
  - `full` — raw field values, tags, flags, reason. The same format as the cards in context.

  « Are there other cards on this subject? » is a narrow query with `brief`; « which other notes have this format? » is a wide query with `full`, because truncated plain text drops exactly what such an audit needs (the **context header** — a `<div class="context">…</div>` topic label at the start of a field, see [notes.md](./notes.md) — the HTML, the cloze structure).

- `fields` optionally restricts `full` results to named fields (e.g. `["Text"]`), for audits where the other fields are noise.

Results are ordered flagged first. A `full` result that would exceed 100 000 characters is not returned: the tool responds with the count and asks Claude to narrow the query or pass `fields`, so one badly scoped search cannot exhaust the token budget available for the rest of the turn.

**`add_notes`** is how notes found by a search become cards the user can see and Claude can target: « trouve les notes du deck qui ont le même problème » is a `search_notes` then an `add_notes`. The server first checks the cap — the workspace holds at most 50 cards ([workspace.md § How notes enter](./workspace.md#how-notes-enter)); ids already in the workspace do not count — and refuses with an error result asking to narrow down when it would be exceeded. Otherwise it returns the notes' text (so Claude can propose in the same turn) and streams an `added` event (`{ id, note_ids, rationale }`); the client fetches the notes and appends one card each. The reply shows « ajoute : 6 notes » where reads show « lit : … ». The rationale is one sentence for the user.

## Proposal tools

Read tools let Claude look; proposal tools are how it suggests a change. A proposal is not a write: it is a structured description of a change (which card, which fields, why) that the client turns into a **version** on a card, a **fragment card**, a new **draft card** or a **move badge** ([workspace.md § Cards](./workspace.md#cards)), and only the user's « Valider » performs the writes, all together.

Claude gets six proposal tools. Each invocation is streamed to the client as a `proposal` event. Tools return `"ok"` to Claude immediately — accepting, editing or dropping the version is the user's decision, not Claude's.

| Tool | Input | Lands as |
|---|---|---|
| `propose_edit` | `{ target, model?, fields: {name: raw}, tags?: [], rationale }` (`model` = target Anki note type name; when it differs from the card's current type, `fields` must be the **complete** field set of the target type — no merge) | a new version on the target card (with a model override when the type changes) |
| `propose_split` | `{ target, original: {fields} \| null, new_notes: [{ model?, fields }], rationale }` (`model` = Anki note type name) | a new version on the target card (`original.fields`) — or the card marked deleted when `original` is null — plus one fragment card per entry of `new_notes` |
| `propose_create` | `{ fields, model?, source_ids?, rationale }` (`model` = Anki note type name; deck, tags and `source_ids` default to the root's) | a new draft card, no parent |
| `propose_move` | `{ target, deck, rationale }` | the destination deck as a badge on the target card |
| `propose_create_source` | `{ name, content, anchor_note_ids?, rationale }` (deck = current deck) | an inline proposal card in the log, applied on click via `POST /api/sources/notes` |
| `propose_edit_source` | `{ source_id, old, new, rationale }` | an inline proposal card in the log, applied on click via `PATCH /api/sources/{source_id}/text` |

**`target`** is a string: a workspace id (`"w3"`) for any card, or an Anki note id in digits (`"1732375559262"`) for a note that has a card *or not*. A note id absent from the workspace adds the note as a card first (the client fetches it with `POST /api/notes/lookup`), then applies the proposal; the server refuses such a proposal with an error result when the workspace is at its cap. An unknown workspace id or note id is an error result. A `propose_edit` on a deleted card is accepted and puts the card back (a rewrite supersedes a deletion).

`fields` in proposals are **raw field values** (HTML, cloze markers kept). On `propose_edit` they are the **changed fields only**, as complete raw values; the client merges them into the shown version's fields to build the new version, so that a one-word fix does not make Claude retype `Back Extra`. **Exception: when `model` is given and differs from the card's current type, `fields` are the complete field set of the target type — no merge, since the field schemas differ.** On `propose_split` and `propose_create` every field of the new note is given.

In the log, a proposal that landed on the workspace shows as one muted pointer line — « → carte w3 », « → 3 cartes » for a split — with the rationale under the version on the card, not here. Several notes with the same defect are several `propose_edit` calls in one turn, one card each; there is no bulk tool.

**Source proposals** are how a conversation ends up in the vault, and they stay in the log as **proposal cards** with « Appliquer », because they write into the vault, not into Anki: they are applied on click, right away, and closing the workspace does not undo them. `propose_create_source` renders the Markdown `content` and an editable `name` field (vault-relative path, no default folder) so the user can adjust it before applying; applying creates the file, adds it to the deck's corpus, and (with `anchor_note_ids`) appends it to those notes' anchors. `propose_edit_source` shows `old` → `new` as a diff on the source's text; applying performs the exact-match replacement ([sources.md § Writing to the vault](./sources.md#writing-to-the-vault)). A refused replacement (passage not found or ambiguous) is shown on the card, not retried. An applied `edit_source` card shows « Annuler »: the same replacement with `old` and `new` swapped, refused by the exact-match rule if the passage changed since (« modifiée depuis, annulation impossible »).

The typical *clarify a card* flow needs no special instruction: the user attaches an anchored source, asks what is unclear, and Claude may propose a source edit first and a card edit after — or the reverse, or neither. The typical *create from a conversation* flow is `propose_create_source` followed by `propose_create` calls carrying the new source id. That id exists before the user applies anything: the server generates it when it streams the `propose_create_source` call, returns it to Claude in the tool result, and sends it to the client in the `proposal` event (`source_id`); applying passes it to `POST /api/sources/notes` so the created source carries the id Claude was told. The draft cards anchored to it are written at validation, which requires the source to have been applied by then; the client says so on « Valider » otherwise.

Tool results are fed back so Claude can continue (e.g. propose an edit, then explain). One assistant turn may contain several proposals.

## Undo

Nothing is written to Anki from the chat, so nothing is undone from it: a version is dropped with « invalider », a validation is reverted with « Annuler la dernière validation » ([workspace.md § Undo](./workspace.md#undo)). The one exception is `edit_source`, above.

## LLM configuration

Uses the Anthropic Python SDK (`anthropic`), streaming, with the model id from `ANKI_CHAT_MODEL` (default `claude-opus-4-6`). `max_tokens` is 8 192 (thinking tokens, when enabled, count against this budget). API key from `ANTHROPIC_API_KEY` (environment variable or `.env` via `python-dotenv`, loaded in `web/main.py`).

System prompt layout and the `cache_control: ephemeral` placement: see [Prompt caching](#prompt-caching).

## API

`POST /api/chat` — body:

```json
{ "deck": "courant::00-Thèse", "source_ids": ["k7q2vd"], "flagged_count": 7,
  "cards": [
    { "wid": "w1", "note_id": 1732375559262, "active": true, "deck": "courant::00-Thèse", "model": "Cloze",
      "tags": ["phd"], "flagged_clozes": [2], "reason": "For a given sensor ?", "anchor_ids": ["r9wt4n"],
      "fields": { "Text": "<raw, shown version>", "Back Extra": "<raw>" },
      "original_fields": { "Text": "<raw, v0>", "Back Extra": "<raw>" },
      "deleted": false, "keep": false, "move_to": null, "parent_wid": null },
    { "wid": "w2", "note_id": null, "active": true, "deck": "courant::00-Thèse", "model": "Cloze",
      "tags": ["phd"], "flagged_clozes": [], "reason": "", "anchor_ids": ["r9wt4n"],
      "fields": { "Text": "<raw>", "Back Extra": "" }, "original_fields": null,
      "deleted": false, "keep": false, "move_to": null, "parent_wid": "w1" } ],
  "messages": [ { "role": "user", "content": "Cette carte est trop vague, tu proposes quoi ?" } ] }
```

`cards` is the workspace, root first; `original_fields` is present only when the shown version is not v0. `source_ids` is optional (default: no attached sources). `flagged_count` is optional (the deck's flagged-note count, for the prompt header). `messages` is the prior conversation as plain `{role, content: string}` turns — assistant text only; prior proposals and reads are not replayed. The client summarises them into the assistant text as bracket notations: « [proposition: edit → carte w1] », « [proposition: split → 3 cartes] », « [proposition: create_source (appliquée)] », « [ajout: 6 notes] », « [version rejetée : w3 v2] », « [lecture: search_notes re:lagrang → 6 notes] », « [lecture: read_source → différentiabilité] ».

Response: `text/event-stream`, SSE events:

```
event: text        data: {"delta": "Je pense que…"}
event: reading     data: {"id": "toolu_…", "tool": "search_notes", "input": {…}, "summary": "6 notes"}
event: added       data: {"id": "toolu_…", "note_ids": [1732375559262, …], "rationale": "…"}
event: proposal    data: {"id": "toolu_…", "kind": "edit", "input": {…}}            (+ "source_id" when kind is create_source)
event: done        data: {"stop_reason": "end_turn", "usage": {"input_tokens": 1234, "output_tokens": 210}}
event: error       data: {"detail": "…"}
```

`GET /api/chat/status` → `{ "configured": true, "model": "…" }`. When `configured` is false the conversation pane shows: « Ajoute ANTHROPIC_API_KEY dans .env puis relance anki-web » and disables the input.

## Module `chat.py`

```python
def build_system(deck, corpus_index, attached, cards, flagged_count=None) -> list[dict]   # content blocks, cached
def tools() -> list[dict]                       # proposal tools + read tools
def format_decks(decks) -> str                  # the text a read tool returns
def format_notes_brief(notes, deck) -> str        # search_notes, detail=brief
def format_notes(notes) -> str                  # get_notes / add_notes / search_notes detail=full
def format_note_type(note_type) -> str
def format_source(source, text) -> str             # what read_source returns / an attached block
async def stream_chat(client, deck, cards, source_ids, messages, load_corpus, load_source,
                      read_tools, model=None, flagged_count=None) -> AsyncIterator[ChatEvent]
```

`cards` are `WorkspaceCard` values as the client sent them — the server does not re-read notes for block 4, since the shown version may be a draft or a hand edit. `load_corpus(deck)` returns the corpus index entries (headers and anchors, no text); `load_source(source_id)` returns `(Source, SourceText)` and backs both the attached-source blocks and `read_source`. `read_tools` maps a read tool name to a callable `(input: dict) -> str`; `routes_chat.py` builds it from `review.py` / `AnkiClient` and the `format_*` functions, so `chat.py` imports neither FastAPI nor Anki. `add_notes` and absent-target proposals are checked against the cap inside `stream_chat`, which knows the cards. Unit tests use a fake Anthropic client that replays a recorded stream and a dict of fake read tools.

## Out of scope for v1

Retrieval inside long PDFs (the user sets `pages` instead), persistence of conversations, Claude acting without a click, editing note type definitions (CSS, templates — read-only through `get_note_type`; **changing** which note type a note belongs to is supported via `propose_edit` with `model`), creating or editing PDF sources.
