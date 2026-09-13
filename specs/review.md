# Review

> The main page of the app: a three-column layout plus the workspace overlay.

The page at `/` is a single screen split into three columns — the deck tree on the left, the queue of [notes](./notes.md) for the selected deck in the centre, and the Source tab ([sources.md](./sources.md)) on the right — plus the [workspace](./workspace.md) overlay that opens on a note for editing. This spec covers columns 1–2 and the decisions that write to Anki; the Source tab and the workspace have their own specs.

The goal is to show, for one deck, the notes that need attention (flagged) with the rest of the deck one click away, and let the user resolve each note with a single decision. A resolved note leaves the list immediately.

## Deck tree (column 1)

Column 1 lists every deck as a row indented by its depth in the `::` hierarchy. Each row shows:

- The deck's **leaf name** (the last `::` segment).
- **Source-kind indicators** — the kinds of the deck's effective [corpus](./sources.md) (own or inherited), deduplicated, in order. Empty when the deck has no corpus.
- **Own count** — the number of flagged notes whose cards live in exactly this deck.
- **Rolled-up count** — the number of flagged notes in this deck and all its sub-decks, counting each note once even when its flagged cards straddle two sub-decks. Shown as an outlined badge only when it exceeds the own count.

By default only decks with a rolled-up count above zero are shown; a toggle shows all decks. Selecting a deck loads its queue (column 2) and its Source tab (column 3).

## Queue (column 2)

The **queue** is the list of notes for the selected deck and all its sub-decks. Each note carries a `deck` field saying where it actually lives. Flagged notes come first, then unflagged; within each group by note id ascending (creation order). The queue shows flagged notes only by default; a toggle ("voir toutes") shows the whole deck, unflagged notes at 55 % opacity. When the queue is empty the column shows "Rien à revoir ici."

One note is selected at a time. After a deck change the first flagged note is selected. After a decision resolves a note, the next flagged note in the visible list is selected (or nothing if the queue is empty). Keyboard navigation: `j`/`↓` and `k`/`↑` move the selection.

Each note shows three things, top to bottom:

1. **Identity line** — short id (`#` + last 4 digits), note type, card count, tags. Flagged notes get a flag badge and a "garder" control. When the note is a flagged Cloze, the badge names the flagged cards ("⚑ c2", one label per flagged card).
2. **Fields** — every field except `Back Extra`, rendered through the [display transform](#rendering). Flagged Cloze notes are shown with their flagged clozes hidden by default (see [Question state](#question-state)).
3. **Reason callout** — when the note is flagged and the [reason](./notes.md#reason-back-extra) is non-empty, a labelled block below the fields shows it. This is the only place `Back Extra` appears in the queue. Shown regardless of question state.

## Question state

A flagged Cloze note starts with its flagged clozes hidden and the rest visible — the question side the reviewer saw when they placed the flag. A hidden cloze shows its hint, or `[…]` when there is no hint; the cloze-number label stays.

"Révéler" (`Espace` on the selected note, or clicking a hidden cloze) switches to full view; the same gesture switches back. The state is per note, reset when the deck changes or the queue is refetched after a decision.

Notes without hideable clozes — unflagged, no cloze deletions in their fields, or flagged cards whose ordinals match no cloze — are shown in full, with no reveal control.

## Decisions

The queue offers three gestures on a note. Everything that edits a note happens in the [workspace](./workspace.md); the queue's own decisions are flag-only.

| Gesture | Trigger | Anki writes | Flag | Queue effect |
|---|---|---|---|---|
| **Garder** | "garder" control, or `g` on selected note | none | cleared on all cards | note leaves the queue |
| **Passer** | `p`, or `j`/`k` | none | kept | selection moves to the next note |
| **Ouvrir** | click on note, or `Entrée` on selected note | none (until the workspace validates) | — | the workspace opens |

**Garder** clears nothing but the flag: the flag was a false alarm, the note is fine, its `Back Extra` stays. No confirmation. Clearing a flag means setting the flag value to zero on every card of the note.

After Garder and after a workspace validation, the deck tree and queue refresh and the next flagged note is selected. A discarded workspace refreshes too but leaves the selection where it was. The queue header shows "Annuler la dernière validation" while the server holds a snapshot from the last workspace validation ([workspace.md § Undo](./workspace.md#undo)).

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
- **MathJax untouched.** `\(…\)`, `\[…\]`, `[$]…[/$]` delimiters are left as text.

## API

All routes are under `/api`. Errors from AnkiConnect surface as HTTP 502 `{ "detail": "<message>" }`; unknown note → 404; Anki unreachable → 503.

| Method & path | Body | Returns |
|---|---|---|
| `GET /api/decks` | — | `Deck[]` |
| `GET /api/notes?deck=` | — | `{ deck, total, flagged, notes: Note[] }` |
| `GET /api/notes/{id}` | — | `Note` |
| `POST /api/notes/lookup` | `{ note_ids: [int] }` | `Note[]` (unknown ids dropped, order kept) |
| `POST /api/notes/{id}/keep` | — | `Note` (flags cleared) |
| `PATCH /api/notes/{id}` | `{ fields?, tags?, unflag?, reflag? }` | `Note` |
| `POST /api/notes/{id}/split` | `{ original: { fields, tags? } \| null, new_notes: [{ model?, fields, tags? }] }` | `{ original: Note \| null, created: Note[] }` |
| `POST /api/notes` | `{ deck, model, fields, tags?, source_ids? }` | `Note` |
| `POST /api/notes/{id}/move` | `{ deck }` | `Note` |
| `DELETE /api/notes/{id}` | — | `204` |
| `GET /api/models` | — | `{ "Cloze": ["Text", "Back Extra"], … }` |

Notes on specific routes:

- **`split`** — `original: null` deletes the original note after the new notes are created successfully. The route copies the original's anchors onto the created notes, and removes the original's anchors when it is deleted.
- **`PATCH`** — `unflag` defaults to `true` when omitted. `reflag` puts a flag back on the listed card ids (used by the workspace's undo); the colour is red, since colours carry no meaning.
- **`POST /api/notes`** — anchors the new note to `source_ids` when given.
- **`move`** — re-checks each of the note's anchors against the destination deck's corpus: an anchor survives only if its source is in that corpus.

The workspace's own routes (`/api/workspace/…`) are specified in [workspace.md § API](./workspace.md#api).

## Frontend

Single page, vanilla JS, no framework or bundler. Scripts load in order: `state.js` (state and helpers), `api.js`, `render.js` (three-column layout), `workspace.js` (the overlay), `app.js` (events and boot). One in-memory state object; every state change triggers a full re-render.

### Keyboard

With the workspace closed and no input focused:

| Key | Action |
|---|---|
| `j` / `↓` | Select next note |
| `k` / `↑` | Select previous note |
| `g` | Garder (clear flag on selected note) |
| `p` | Passer (skip to next note) |
| `Entrée` | Ouvrir (open workspace on selected note) |
| `Espace` | Toggle question state (reveal / hide flagged clozes) |

Keys are ignored while an input, textarea, or select is focused. Workspace keyboard shortcuts are specified in [workspace.md § Keyboard](./workspace.md#keyboard).

There are no dialogs: every edit happens on a workspace card. Native `confirm()` is used for the two confirmations (discarding a workspace with changes, validating a plan that deletes).

The visual identity — terminal palette, two-layer colour architecture, theme picker, logo — is specified in [theme.md](./theme.md).
