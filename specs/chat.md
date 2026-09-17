# Chat

> The right half of the [workspace](./workspace.md) overlay: a scrolling log, a row of source chips, and a message box — a conversation with Claude that reads the deck's notes and sources on demand and answers with [proposals](#proposal-tools) that land on the workspace as versions and cards.

Three parts: a scrolling **log** of messages and tool-call lines (reading summaries, pointer lines, source-proposal cards), a **chips row** for attaching sources to the context, and a **message box** with the model id and « Envoyer ». The **client** is the browser; the **server** is the local FastAPI process — "server" never means Anthropic's side.

A **conversation** is the message log plus the sources attached to it. It belongs to one workspace: it starts empty when the workspace opens and is dropped when the workspace closes. The conversation serves the [workspace](./workspace.md).

When no `ANTHROPIC_API_KEY` is configured, the pane shows a banner and disables the input. The model is set by `ANKI_CHAT_MODEL` (default `claude-opus-4-6`); `GET /api/chat/status` reports both.

## What Claude sees

The server keeps nothing between requests. On every **turn** — one user message and the reply to it — the client sends the whole conversation and the current state of every card, and the server rebuilds the **system prompt** from four blocks:

1. **Standing instructions** — reply in the user's language; answer questions and information requests without proposing changes — only propose when the user asks for a change or the answer reveals a clear factual error in a card; use the proposal tools rather than describing changes in prose; target [active](./workspace.md#card-head) cards by [workspace id](./workspace.md#cards); fields are [raw Anki HTML](./notes.md#fields-are-raw-anki-html) with cloze markers; web content should arrive with the source proposal that grounds it; URLs are not pasted into the reply because [citations](#citations) already link the passages. Also states the conventions of the note collection ([context headers](./notes.md#context-header), [cloze syntax](./notes.md#cloze-markers)) as facts.

2. **Corpus index** — one line per source of the deck's [corpus](./sources.md): id, kind, target, page range, and a ⚠ marker when the file is missing. Sources [anchored](./sources.md#anchors) to a card of the workspace are marked. PDF sources include a compact **structural index** (from the PDF's bookmarks or heuristic headings) so Claude knows which pages to target with `read_source`. No source text — that is what attaching and `read_source` are for.

3. **Attached sources** — the full text of each source the user has attached (see [How source text enters context](#how-source-text-enters-context)). An attached source stays in every turn's prompt until the user removes it. Total attached text is capped at 150 000 characters; the cap is stated in the prompt.

4. **Cards of the workspace** — every card, [root](./workspace.md#opening-and-closing) first, then in order of arrival: workspace id, note id or « brouillon », active or inactive, states (deleted, kept, deferred with its comment, moved), parent when it is a [fragment](./workspace.md#split), deck, [note type](./notes.md#note-card-note-type), tags, flagged cards as cloze labels, [reason](./notes.md#reason-back-extra), [anchors](./sources.md#anchors), and the raw field values of the [shown version](./workspace.md#versions). When the shown version is not v0, the v0 fields follow so Claude sees what has changed. Intermediate versions are not sent.

Blocks 1–3 are stable for the life of the workspace and marked for the API's prompt cache; a change on the workspace re-processes only block 4. Attaching or detaching a source invalidates the cache.

### How source text enters context

Source text enters the prompt in two ways, both visible to the user:

- **The user attaches it.** The chips row lists the corpus. Sources anchored to the root appear as individual chips (« ⚓ joindre … »); the rest are under a « + source » menu. Clicking attaches that source to block 3; the × detaches it. Attaching is per source, never the whole corpus at once.

- **Claude reads it.** The `read_source` tool returns a source's text into the current turn, and the log shows a reading summary so the user knows what was loaded. Reads are not carried to later turns; attaching is the way to keep a source in front of Claude.

### What Claude remembers between turns

Only message text is re-sent across turns. Tool results — reads, proposals, additions — are not replayed. The client summarises them into the assistant text as bracketed notes (« [lecture: search_notes → 6 notes] », « [proposition: edit → carte w1] », « [version rejetée : w3 v2] ») so Claude knows what happened. Web citations survive the same way: the markers stay in the text and a bracket line lists the pages (« [sources : [1] https://…, [2] https://…] »); the numbering restarts at 1 on each turn. If Claude needs a read's content again, it reads again.

## Read tools

Claude sees only what the system prompt pushes. Everything else it pulls through read tools. They come in two families: **local** tools (`list_decks`, `search_notes`, `get_notes`, `add_notes`, `get_note_type`, `read_source`) run on the server against Anki or the source store; **web** tools (`web_search`, `web_fetch`, see [Web tools](#web-tools)) run on Anthropic's side. Read tools never write to Anki or the vault; `add_notes` changes what the workspace shows.

| Tool            | Returns                                                                                              |
| --------------- | ---------------------------------------------------------------------------------------------------- |
| `list_decks`    | The deck tree with flagged-note counts.                                                              |
| `search_notes`  | The matching notes, in the shape controlled by `detail` (see below).                                 |
| `get_notes`     | The full notes (raw field values, tags, flags, reason), in the same format as the cards in context.  |
| `add_notes`     | The same text as `get_notes`, and the notes become cards of the workspace.                           |
| `get_note_type` | Field names, card templates and CSS of a note type. When Claude needs information about a note type. |
| `read_source`   | The source's text. Optional `pages` parameter (PDF only) overrides the source's page range.          |

### The tool loop

A single turn can involve multiple round trips between the server and the LLM. Each round trip is a **model call**: the server sends the conversation, the LLM responds, and if the response contains tool invocations the server executes them and makes another call. This **tool loop** repeats until the LLM responds without tools or the cap of 8 model calls per turn is reached.

Web tools run on Anthropic's side within the model call, so the server has nothing to execute. The loop handles two consequences: a long search may need a continuation call, and a mixed batch of web and local tools is split across calls.

Each read is displayed in the log as a **reading summary**: a muted line naming the tool and summarising the result. Failures come back to Claude as an error tool result, not as a client-visible error.

**`search_notes`** searches the note collection beyond the cards in context. The query uses Anki search syntax; the server scopes it to the current deck and its sub-decks unless the query names a deck explicitly.

`detail` controls the response shape:

- `count` — how many notes match, nothing else.
- `brief` (default) — one line per note: id, deck when it differs, flag marker, each field's plain text truncated.
- `full` — raw field values, tags, flags, reason — the same format as the cards in context.

`fields` restricts `full` results to named fields. A `full` result exceeding 100 000 characters is not returned: the tool responds with the count and asks to narrow the query.

**`add_notes`** is how notes found by a search become cards the user can see and Claude can target. The server checks the workspace cap of 50 cards ([workspace.md § How notes enter](./workspace.md#how-notes-enter)) and refuses with an error when it would be exceeded. The log shows « ajoute : n notes ».

### Web tools

Two Anthropic server tools put the open web behind the same conversation as the deck. They are declared alongside the local tools and reported as reads.

| Tool         | Returns                                                                                                          |
| ------------ | ---------------------------------------------------------------------------------------------------------------- |
| `web_search` | Result pages (title, URL, extract). Capped at 8 searches per turn.                                              |
| `web_fetch`  | The full text of a page whose URL is already in the conversation — a search result or a message from the user. Capped at 5 fetches per turn. |

Search returns extracts — enough to check a card's statement but too thin to become a source. `web_fetch` is how Claude reads a page in full before proposing it: as a [web source](./sources.md#text) by `propose_add_source`, or as a vault note by `propose_create_source` when the content should be kept as it stands today.

**What the user sees.** Each call is streamed as a reading event, and the summary carries the URLs: « lit : web\_search → « KKT conditions » → 5 résultats : en.wikipedia.org/…, … ». URLs — in reading lines and in the reply — are rendered as links that open in a new tab.

#### Citations

The API attaches a **citation** to each passage of the reply that draws on a web result. The server numbers the cited pages per turn by URL, in order of first citation, and streams each as a `citation` event. The client renders a **[n]** marker at the end of the cited passage as a link to the page, with the cited text as tooltip. Under the reply, a **Sources** list gives one line per page: the title and the host, linking to the page. A page cited three times is one entry and three markers.

The standing instructions tell Claude not to paste URLs into its prose — citations already provide the link — except to recommend a page the reply did not quote from. The instructions also ask Claude to call `propose_add_source` whenever it cites a page that is a good reference for the deck. Independently of Claude, the Sources list shows a **« + corpus »** button next to every cited page that is not already in the corpus; clicking adds it via `POST /api/sources`.

Web results are not replayed across turns: they are dropped like any other read and survive as bracket notation. Re-reading a page costs a round trip to the open web — so `web_fetch` on something worth keeping is a reason to propose it as a source rather than fetch it twice.

## Proposal tools

A **proposal** is a structured description of a change — which card, which fields, why — that lands on the workspace as a [version](./workspace.md#versions), a [fragment](./workspace.md#split), a new [draft card](./workspace.md#how-notes-enter) or a [move badge](./workspace.md#card-head). Nothing is written to Anki until « Valider » ([workspace.md § Validation](./workspace.md#validation)).

| Tool                    | Lands as                                                                                                                     |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `propose_edit`          | A new version on the target card.                                                                                            |
| `propose_split`         | A new version on the target (or the card marked deleted when the original is not kept), plus one fragment card per new note. |
| `propose_create`        | A new draft card. Deck, tags and anchors default to the root's.                                                              |
| `propose_move`          | The destination deck as a badge on the target card.                                                                          |
| `propose_add_source`    | An inline proposal card in the log (see [Source proposals](#source-proposals)).                                              |
| `propose_create_source` | An inline proposal card in the log (see [Source proposals](#source-proposals)).                                              |
| `propose_edit_source`   | An inline proposal card in the log (see [Source proposals](#source-proposals)).                                              |

Each invocation returns « ok » to Claude immediately — accepting, editing or dropping a version is the user's decision. One turn may contain several proposals; the same defect on several notes is several `propose_edit` calls, one card each.

**Target.** A proposal names its target by workspace id (`"w3"`) or Anki note id in digits. A note id absent from the workspace adds the note as a card first, within the cap. An unknown id is an error result. A `propose_edit` on a deleted card puts the card back: a rewrite supersedes a deletion.

**Fields.** Field values in proposals are [raw](./notes.md#fields-are-raw-anki-html). On `propose_edit` they are the changed fields only, as complete raw values; the client merges them into the shown version. When `model` names a different note type, `fields` are the complete set of the target type. On `propose_split` and `propose_create` every field is given.

In the log, a proposal that landed on the workspace shows as a muted pointer line naming the kind and the target card. The rationale appears under the version on the card, not in the log.

### Source proposals

Source proposals change the [corpus](./sources.md) or the [vault](./sources.md#writing-to-the-vault) on click, not at validation, and closing the workspace does not undo them. They stay in the log as **proposal cards** with « Appliquer ».

`propose_add_source` renders the kind chip and an editable target (a URL, a vault note name or a PDF path) with the optional pages and annotation, so the user can correct the target before applying. Applying appends the entry to the deck's corpus and (with `anchor_note_ids`) to those notes' anchors.

`propose_create_source` renders the Markdown content and an editable name (vault-relative path). Applying creates the file, adds it to the deck's corpus, and anchors the named notes to it. Draft cards anchored to an unapplied source are flagged at « Valider ».

`propose_edit_source` shows old → new as a diff. Applying performs the [exact-match replacement](./sources.md#writing-to-the-vault); a refusal (passage not found or ambiguous) is shown on the card. An applied edit shows « Annuler »: the same replacement with old and new swapped, refused if the passage changed since.

For `propose_add_source` and `propose_create_source`, the server generates the source id when it streams the call and returns it to Claude, so Claude can anchor notes it proposes next to that source.

Nothing else in the chat writes to Anki; undo lives in the [workspace](./workspace.md#undo).

## API

| Route | Purpose |
|---|---|
| `POST /api/chat` | One chat turn, streamed as server-sent events. |
| `GET /api/chat/status` | Whether the API key is configured and which model is active. |

## Out of scope for v1

Retrieval inside long PDFs (the user sets `pages` instead), persistence of conversations, Claude acting without a click, editing note type definitions (read-only through `get_note_type`; changing which note type a note belongs to is supported via `propose_edit`), creating or editing PDF sources, editing a web source (a web source is read-only; `propose_edit_source` refuses it).
