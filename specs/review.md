# Review

> The main page of the app: a three-column layout plus the workspace overlay.

The page at `/` is a single screen split into three columns — the deck tree on the left, the queue of [notes](./notes.md) for the selected deck in the centre, and the Source tab ([sources.md](./sources.md)) on the right — plus the [workspace](./workspace.md) overlay that opens on a note for editing. This spec covers columns 1–2 and the decisions that write to Anki; the Source tab and the workspace have their own specs.

The goal is to show, for one deck, the notes that need attention (flagged) with the rest of the deck one click away, and let the user resolve each note with a single decision. A resolved note leaves the list immediately.

## Priority queue

A synthetic entry at the top of the deck tree, above all real decks. It aggregates flagged notes across every deck that match **urgency criteria** — cards the user keeps deferring during Anki reviews and that pile up instead of being resolved.

### Criteria

A flagged note is "priority" when at least one of its flagged cards matches any of:

| Criterion | Anki state | Why it's urgent |
|---|---|---|
| **Buried** | `is:buried` (queue −2 or −3) | The user saw the card and deferred it — it will come back tomorrow unchanged |
| **Due within budget** | `is:due`, within the deck's remaining review count | A deferred card that came back AND that Anki will present today — resolve it before the review session |
| **New** | `is:new` | A new card flagged as not fit for learning; it blocks the new-card queue |

Buried and new flagged cards are always included. Due flagged cards are included only when they fall within the study deck's remaining review budget. The **study deck** — the single root deck the user launches Anki reviews from — is set via the `STUDY_DECK` env var (e.g. `courant`). The server fetches that deck's `review_count` from `getDeckStats`, sorts all due cards under it by `(deck_name, due)` — deck alphabetical first (matching Anki's deck-order presentation), then most overdue first within each deck — and keeps only flagged cards whose position is within the budget. Without `STUDY_DECK`, all flagged due cards are included (no budget filtering).

### Deck tree row

The priority row appears at the top of the deck tree, visually separated from real decks. It shows:

- A fixed label — **"Priority"**.
- A count — the number of distinct flagged notes matching the criteria.

The row is hidden when the count is zero (nothing urgent). It is always visible otherwise, regardless of the "show all decks" toggle.

### Queue behaviour

When the priority row is selected, the queue (column 2) loads the matching notes as a flat list. Each note carries its `deck` field as usual, and the queue renders the deck name on each note's identity line so the user knows where it lives.

Sort order: flagged first (they all are), then by note id ascending. The same decisions apply (Keep, Skip, Open); after a decision the queue refreshes with the same priority query. The Source tab is inactive in priority mode (no corpus to show across decks).

## Deck tree (column 1)

Column 1 lists every deck as a row indented by its depth in the `::` hierarchy. Each row shows:

- The deck's **leaf name** (the last `::` segment).
- **Source-kind indicators** — the kinds of the deck's effective [corpus](./sources.md) (own or inherited), deduplicated, in order. Empty when the deck has no corpus.
- **Own count** — the number of flagged notes whose cards live in exactly this deck.
- **Rolled-up count** — the number of flagged notes in this deck and all its sub-decks, counting each note once even when its flagged cards straddle two sub-decks. Shown as an outlined badge only when it exceeds the own count.

By default only decks with a rolled-up count above zero are shown; a toggle shows all decks. Selecting a deck loads its queue (column 2) and its Source tab (column 3).

### Creating a deck

Two entry points, same outcome:

1. **Add-deck button** — a row at the top of the deck list (below the header), labelled "+ Add a deck". Clicking it opens an inline input where the user types the full deck name. An autocomplete dropdown suggests existing deck paths as the user types, so they can pick a parent prefix (e.g. selecting `Médecine::Cardio` and then typing `::Arythmies`).
2. **Context menu** — right-clicking a deck row opens a context menu with two items: "Nouveau sous-paquet" (opens the inline input pre-filled with the right-clicked deck's name followed by `::`) and "Supprimer le paquet" (deletes the deck and its cards after a `confirm()` prompt).

Pressing `Entrée` in the input submits; `Échap` cancels and closes the input. The input is dismissed after a successful creation or on cancel.

**Validation.** Before creating, the frontend checks that the name is non-empty and does not match an existing deck name (case-sensitive, against the loaded deck list). On conflict the input shows an inline error ("Ce paquet existe déjà") and stays open.

**Creation.** `POST /api/decks` with `{ "name": "<full deck name>" }`. The server calls AnkiConnect's `createDeck`, which creates intermediate decks if needed. Returns the created deck name. On success the deck tree refreshes and the new deck is selected.

## Queue (column 2)

The **queue** is the list of notes for the selected deck and all its sub-decks. Each note carries a `deck` field saying where it actually lives. Flagged notes come first, then unflagged; within each group by note id ascending (creation order). The queue shows flagged notes only by default; a toggle ("show all") shows the whole deck, unflagged notes at 55 % opacity. When the queue is empty the column shows "Nothing to review here."

One note is selected at a time. After a deck change the first flagged note is selected. After a decision resolves a note, the next flagged note in the visible list is selected (or nothing if the queue is empty). Keyboard navigation: `j`/`↓` and `k`/`↑` move the selection.

Each note shows three things, top to bottom:

1. **Identity line** — short id (`#` + last 4 digits), note type, card count, tags. Flagged notes get a flag badge and a "keep" control. When the note is a flagged Cloze, the badge names the flagged cards ("⚑ c2", one label per flagged card).
2. **Fields** — every field except `Back Extra`, rendered through the [display transform](#rendering). Flagged Cloze notes are shown with their flagged clozes hidden by default (see [Question state](#question-state)).
3. **Reason callout** — when the note is flagged and the [reason](./notes.md#reason-back-extra) is non-empty, a labelled block below the fields shows it. This is the only place `Back Extra` appears in the queue. Shown regardless of question state.

## Question state

A flagged Cloze note starts with its flagged clozes hidden and the rest visible — the question side the reviewer saw when they placed the flag. A hidden cloze shows its hint, or `[…]` when there is no hint; the cloze-number label stays.

"Reveal" (`Space` on the selected note, or clicking a hidden cloze) switches to full view; the same gesture switches back. The state is per note, reset when the deck changes or the queue is refetched after a decision.

Notes without hideable clozes — unflagged, no cloze deletions in their fields, or flagged cards whose ordinals match no cloze — are shown in full, with no reveal control.

## Decisions

The queue offers three gestures on a note. Everything that edits a note happens in the [workspace](./workspace.md); the queue's own decisions are flag-only.

| Gesture | Trigger | Anki writes | Flag | Queue effect |
|---|---|---|---|---|
| **Keep** | "keep" control, or `g` on selected note | none | cleared on all cards | note leaves the queue |
| **Skip** | `p`, or `j`/`k` | none | kept | selection moves to the next note |
| **Open** | click on note, or `Enter` on selected note | none (until the workspace validates) | — | the workspace opens |

**Keep** clears nothing but the flag: the flag was a false alarm, the note is fine, its `Back Extra` stays. No confirmation. Clearing a flag means setting the flag value to zero on every card of the note.

After Keep and after a workspace validation, the deck tree and queue refresh and the next flagged note is selected. A discarded workspace refreshes too but leaves the selection where it was. The queue header shows "Undo last validation" while the server holds a snapshot from the last workspace validation ([workspace.md § Undo](./workspace.md#undo)).

What each workspace action writes — splits, moves, deletes, how [anchors](./sources.md#anchors) follow — is specified in [workspace.md § Validation](./workspace.md#validation).

## Rendering

Every note field except `Back Extra` goes through a display transform before it appears in the queue or the workspace. The transform turns a raw Anki field value into safe display HTML. There is a Python implementation (`render.py`) and a JS port in the frontend; both follow the same rules.

The transform guarantees:

- **No injection.** The raw value is escaped before any display markup is introduced. Nothing from the note can produce arbitrary markup.
- **Line breaks preserved.** Anki's `<br>` and block-closing tags each become a line break.
- **Images replaced.** An image with an alt attribute becomes the alt text (Anki's rendered LaTeX images carry the LaTeX source as alt). An image without useful alt becomes `[image]`.
- **All other tags stripped.**
- **Cloze deletions marked up.** Each cloze deletion is displayed with its number as a label and, when present, its hint. The number is needed because most notes carry several clozes and the flag is per card.
- **Context header extracted.** A [context header](./notes.md#context-header) at the start of the field is extracted and styled separately from the body.
- **MathJax rendered.** `\(…\)`, `\[…\]`, `[$]…[/$]`, `[$$]…[/$$]` delimiters pass through the escape step and are rendered by MathJax 3, loaded in the page. After each DOM update the app calls `MathJax.typesetPromise()`.

## API

All routes are under `/api`. Errors: AnkiConnect failure → 502, unknown note → 404, Anki unreachable → 503. All error bodies are `{ "detail": "<message>" }`.

| Route | Purpose |
|---|---|
| `GET /api/decks` | `{ decks, priority_count }` — all decks with flagged counts and source kinds, plus the priority queue note count |
| `POST /api/decks` | Create a new deck |
| `DELETE /api/decks` | Delete a deck (cards move to Default) |
| `GET /api/notes/priority` | Flagged notes matching the priority criteria (cross-deck) |
| `GET /api/notes?deck=` | Notes for a deck and its sub-decks |
| `GET /api/notes/{id}` | One note |
| `POST /api/notes/lookup` | Several notes by id |
| `POST /api/notes/{id}/keep` | Clear the flag, change nothing else |
| `PATCH /api/notes/{id}` | Update fields, tags, flag state |
| `POST /api/notes/{id}/split` | Split a note into fragments |
| `POST /api/notes` | Create a note |
| `POST /api/notes/{id}/move` | Move a note to another deck |
| `DELETE /api/notes/{id}` | Delete a note |
| `GET /api/models` | All note types and their field names |

The workspace's own routes (`/api/workspace/…`) are specified in [workspace.md § API](./workspace.md#api).

## Frontend

Single page, vanilla JS, no framework or bundler. Scripts load in order: `state.js` (state and selectors), `display.js` (field rendering, cloze hiding, Markdown + math), `api.js`, `render.js` (three-column layout), `ws-state.js` (workspace card data model), `ws-render.js` (workspace + chat HTML), `workspace.js` (chat stream, validation, events), `sources-tab.js` (source-tab data logic), `app.js` (top-level events and boot). One in-memory state object; every state change triggers a full re-render.

### Keyboard

With the workspace closed and no input focused:

| Key | Action |
|---|---|
| `j` / `↓` | Select next note |
| `k` / `↑` | Select previous note |
| `g` | Keep (clear flag on selected note) |
| `p` | Skip (skip to next note) |
| `Enter` | Open (open workspace on selected note) |
| `Space` | Toggle question state (reveal / hide flagged clozes) |

Keys are ignored while an input, textarea, or select is focused. Workspace keyboard shortcuts are specified in [workspace.md § Opening and closing](./workspace.md#opening-and-closing).

There are no dialogs: every edit happens on a workspace card. Native `confirm()` is used for the workspace's confirmations ([workspace.md](./workspace.md)).

The visual identity — terminal palette, two-layer colour architecture, theme picker, logo — is specified in [theme.md](./theme.md).
