# Chat

> The right half of the [workspace](./workspace.md) overlay: a scrolling log, a row of source chips, and a message box — a conversation with Claude that reads the deck's notes and sources on demand and answers with [proposals](#proposal-tools) that land on the workspace as versions and cards.

Three parts: a scrolling **log** of messages and tool-call lines (reading summaries, pointer lines, source-proposal cards), a **chips row** for attaching sources to the context, and a **message box** with the model id and « Envoyer ». The **client** is the browser; the **server** is the local FastAPI process — "server" never means Anthropic's side.

A **conversation** is the message log plus the sources attached to it. It belongs to one workspace: it starts empty when the workspace opens and is dropped when the workspace closes. The conversation serves the [workspace](./workspace.md).

When no `ANTHROPIC_API_KEY` is configured, the pane shows a banner and disables the input. The model is set by `ANKI_CHAT_MODEL` (default `claude-opus-4-6`); `GET /api/chat/status` reports both.

## What Claude sees

The server keeps nothing between requests. On every **turn** — one user message and the reply to it — the client sends the whole conversation and the current state of every card, and the server rebuilds the **system prompt** from four blocks:

1. **Standing instructions** — reply in the user's language; use the proposal tools rather than describing changes in prose; the next message is about the [active](./workspace.md#card-head) cards, target them by [workspace id](./workspace.md#cards); fields are [raw Anki HTML](./notes.md#fields-are-raw-anki-html) with cloze markers; the web is for checking a card against the outside world and for finding sources worth adding, and content taken from it should arrive with the source proposal that grounds it. Also states the conventions of the note collection ([context headers](./notes.md#context-header), [cloze syntax](./notes.md#cloze-markers)) as facts. Nothing else: the prompt does not instruct Claude on how to reason about a card.

2. **Corpus index** — one line per source of the deck's [corpus](./sources.md): id, kind, target, page range, and a ⚠ marker when the file is missing. Sources [anchored](./sources.md#anchors) to a card of the workspace are marked. No source text — that is what attaching and `read_source` are for.

3. **Attached sources** — the full text of each source the user has attached (see [How source text enters context](#how-source-text-enters-context)). An attached source stays in every turn's prompt until the user removes it. Total attached text is capped at 150 000 characters; the cap is stated in the prompt.

4. **Cards of the workspace** — every card, [root](./workspace.md#opening-and-closing) first, then in order of arrival: workspace id, note id or « brouillon », active or inactive, states (deleted, kept, moved), parent when it is a [fragment](./workspace.md#split), deck, [note type](./notes.md#note-card-note-type), tags, flagged cards as cloze labels, [reason](./notes.md#reason-back-extra), [anchors](./sources.md#anchors), and the raw field values of the [shown version](./workspace.md#versions). When the shown version is not v0, the v0 fields follow so Claude sees what has changed. Intermediate versions are not sent.

Blocks 1–3 are stable for the life of the workspace and marked for the API's prompt cache; a change on the workspace re-processes only block 4. Attaching or detaching a source invalidates the cache.

### How source text enters context

Source text enters the prompt in two ways, both visible to the user:

- **The user attaches it.** The chips row lists the corpus. Sources anchored to the root appear as individual chips (« ⚓ joindre … »); the rest are under a « + source » menu. Clicking attaches that source to block 3; the × detaches it. Attaching is per source, never the whole corpus at once.

- **Claude reads it.** The `read_source` tool returns a source's text into the current turn, and the log shows a reading summary so the user knows what was loaded. Reads are not carried to later turns; attaching is the way to keep a source in front of Claude.

### What Claude remembers between turns

Only message text is re-sent across turns. Tool results — reads, proposals, additions — are not replayed. The client summarises them into the assistant text as bracketed notes (« [lecture: search_notes → 6 notes] », « [proposition: edit → carte w1] », « [version rejetée : w3 v2] ») so Claude knows what happened. If Claude needs a read's content again, it reads again.

## Read tools

Claude sees only what the system prompt pushes. Everything else it pulls through read tools. They come in two families: **local** tools (`list_decks`, `search_notes`, `get_notes`, `add_notes`, `get_note_type`, `read_source`) run on the server against Anki or the source store; **web** tools (`web_search`, `web_fetch`, see [Web tools](#web-tools)) run on Anthropic's side. Read tools never write to Anki or the vault; `add_notes` changes what the workspace shows.

| Tool            | Returns                                                                                              |
| --------------- | ---------------------------------------------------------------------------------------------------- |
| `list_decks`    | The deck tree with flagged-note counts.                                                              |
| `search_notes`  | The matching notes, in the shape controlled by `detail` (see below).                                 |
| `get_notes`     | The full notes (raw field values, tags, flags, reason), in the same format as the cards in context.  |
| `add_notes`     | The same text as `get_notes`, and the notes become cards of the workspace.                           |
| `get_note_type` | Field names, card templates and CSS of a note type. When claude needs information about a note type. |
| `read_source`   | The source's full text.                                                                              |

### The tool loop

A single turn can involve multiple round trips between the server and the LLM. Each round trip is a **model call**: the server sends the conversation, the LLM responds, and if the response contains tool invocations the server executes them and makes another call. This **tool loop** repeats until the LLM responds without tools or the cap of 8 model calls per turn is reached.

A turn that uses a **web tool** does not go round the loop that way: Anthropic runs the search inside the model call and returns its results as extra content blocks of the same assistant message, so the server has nothing to execute and `stop_reason` is not `tool_use`. Two consequences the loop has to handle. A long search run comes back as `stop_reason: "pause_turn"`: the server appends the assistant message unchanged and calls again — no extra user message — and the API resumes where it left off, spending one more of the eight model calls. And when Claude calls a web tool and a local read tool in the same batch, the API returns `tool_use` and defers the search: the local results go back as usual and the search runs on the next call.

Each read is displayed in the log as a **reading summary**: a muted line naming the tool and summarising the result. Failures come back to Claude as an error tool result, not as a client-visible error.

**`search_notes`** searches the note collection beyond the cards in context. The query uses Anki search syntax; the server scopes it to the current deck and its sub-decks unless the query names a deck explicitly.

`detail` controls the response shape:

- `count` — how many notes match, nothing else.
- `brief` (default) — one line per note: id, deck when it differs, flag marker, each field's plain text truncated.
- `full` — raw field values, tags, flags, reason — the same format as the cards in context.

`fields` restricts `full` results to named fields. A `full` result exceeding 100 000 characters is not returned: the tool responds with the count and asks to narrow the query.

**`add_notes`** is how notes found by a search become cards the user can see and Claude can target. The server checks the workspace cap of 50 cards ([workspace.md § How notes enter](./workspace.md#how-notes-enter)) and refuses with an error when it would be exceeded. The log shows « ajoute : n notes ».

### Web tools

Two Anthropic server tools, declared alongside the others and reported as reads, put the open web behind the same conversation as the deck.

| Tool | Input | Returns |
|---|---|---|
| `web_search` | `{ query }` | Result pages (title, url, extract), filtered server-side before they reach context. Capped at `MAX_WEB_SEARCHES` = 8 per turn. |
| `web_fetch` | `{ url }` | The full text of a page whose URL is **already in the conversation** — a search result, a message from the user. Capped at `MAX_WEB_FETCHES` = 5 per turn. |

**Why both.** Search returns extracts, which are enough to answer « is my card's statement the standard one? » but too thin to become a source. `web_fetch` is what turns a page the user wants to keep into a `propose_create_source` with real content — the path by which a Wikipedia article or a set of lecture notes enters the corpus as an Obsidian note ([sources.md](./sources.md)).

**What the user sees.** Each call is streamed as a `reading` event like any other, and the summary carries the **URLs**, not just a count: « lit : web\_search → « KKT conditions » → 5 résultats : en.wikipedia.org/…, … ». That is the whole provenance guarantee — a card must never rest on a page the user was not told about. The reply's own citation markers are not rendered in the log; the reading line is where provenance lives.

**What the web is allowed to be.** Standing instructions ([What Claude sees](#what-claude-sees)) state it: the web is legitimate for checking a card against the outside world and for *finding sources to add*, and a proposal whose content comes from the web should come with the `propose_create_source` that grounds it. Nothing about this is enforced in code — the enforcement is that every write still waits for a click.

**Untrusted content.** A fetched page is text written by someone else, arriving in a context where Claude holds proposal tools. The design already answers this and no new mechanism is added: proposals land as versions and cards that the user reads before « Valider », and source proposals wait on « Appliquer ». No web content reaches Anki or the vault without a click.

**Results are not replayed.** The API requires search results to be sent back byte-identical (`encrypted_content`) or not at all; since the client replays assistant *text* only ([API](#api)), they are simply dropped, like every other read, and survive as the bracket notation « [lecture: web\_search → …] ». Re-reading a page costs a real round trip to the open web, not a local call — so `web_fetch` on something worth keeping is a reason to propose it as a source rather than fetch it twice.

## Proposal tools

A **proposal** is a structured description of a change — which card, which fields, why — that lands on the workspace as a [version](./workspace.md#versions), a [fragment](./workspace.md#split), a new [draft card](./workspace.md#how-notes-enter) or a [move badge](./workspace.md#card-head). Nothing is written to Anki until « Valider » ([workspace.md § Validation](./workspace.md#validation)).

| Tool                    | Lands as                                                                                                                     |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `propose_edit`          | A new version on the target card.                                                                                            |
| `propose_split`         | A new version on the target (or the card marked deleted when the original is not kept), plus one fragment card per new note. |
| `propose_create`        | A new draft card. Deck, tags and anchors default to the root's.                                                              |
| `propose_move`          | The destination deck as a badge on the target card.                                                                          |
| `propose_create_source` | An inline proposal card in the log (see [Source proposals](#source-proposals)).                                              |
| `propose_edit_source`   | An inline proposal card in the log (see [Source proposals](#source-proposals)).                                              |

Each invocation returns « ok » to Claude immediately — accepting, editing or dropping a version is the user's decision. One turn may contain several proposals; the same defect on several notes is several `propose_edit` calls, one card each.

**Target.** A proposal names its target by workspace id (`"w3"`) or Anki note id in digits. A note id absent from the workspace adds the note as a card first, within the cap. An unknown id is an error result. A `propose_edit` on a deleted card puts the card back: a rewrite supersedes a deletion.

**Fields.** Field values in proposals are [raw](./notes.md#fields-are-raw-anki-html). On `propose_edit` they are the changed fields only, as complete raw values; the client merges them into the shown version. When `model` names a different note type, `fields` are the complete set of the target type. On `propose_split` and `propose_create` every field is given.

In the log, a proposal that landed on the workspace shows as a muted pointer line naming the kind and the target card. The rationale appears under the version on the card, not in the log.

### Source proposals

Source proposals write into the vault on click, not at validation, and closing the workspace does not undo them. They stay in the log as **proposal cards** with « Appliquer ».

`propose_create_source` renders the Markdown content and an editable name (vault-relative path). Applying creates the file, adds it to the deck's corpus, and anchors the named notes to it. The server generates the source id when it streams the call and returns it to Claude, so Claude can anchor notes it proposes next to that source. Draft cards anchored to an unapplied source are flagged at « Valider ».

`propose_edit_source` shows old → new as a diff. Applying performs the [exact-match replacement](./sources.md#writing-to-the-vault); a refusal (passage not found or ambiguous) is shown on the card. An applied edit shows « Annuler »: the same replacement with old and new swapped, refused if the passage changed since.

Nothing else in the chat writes to Anki; undo lives in the [workspace](./workspace.md#undo).

## LLM configuration

Uses the Anthropic Python SDK (`anthropic`), streaming, with the model id from `ANKI_CHAT_MODEL` (default `claude-opus-4-6`). The web tools are declared as `web_search_20260209` / `web_fetch_20260209`, the variants that filter results server-side before they enter context; they need Opus 4.6 / Sonnet 4.6 or later, so a model set through `ANKI_CHAT_MODEL` must be one of those. Searches are billed per search on top of tokens ($10 per 1 000 at the time of writing), which is what `max_uses` is for. `max_tokens` is 8 192 (thinking tokens, when enabled, count against this budget). API key from `ANTHROPIC_API_KEY` (environment variable or `.env` via `python-dotenv`, loaded in `web/main.py`).

## API

| Route | Purpose |
|---|---|
| `POST /api/chat` | One chat turn, streamed as server-sent events. |
| `GET /api/chat/status` | Whether the API key is configured and which model is active. |

## Out of scope for v1

Retrieval inside long PDFs (the user sets `pages` instead), persistence of conversations, Claude acting without a click, editing note type definitions (read-only through `get_note_type`; changing which note type a note belongs to is supported via `propose_edit`), creating or editing PDF sources.
