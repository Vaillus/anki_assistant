# Review

> The review surface: columns 1–2 of the page, the note-level view of a deck, and the decisions that write to Anki.

## Purpose

Show, for one deck, the notes that need attention (flagged) with the rest of the deck one click away, and let the user resolve each note with a single decision. A resolved note leaves the list immediately.

## Vocabulary

Terms this spec uses but does not own — follow the link for the full definition:

- **Note**, **card**, **note type**, **field** — [notes.md](./notes.md). The review unit is the note, not the card.
- **Reason** — the plain text of a flagged note's `Back Extra` field. [notes.md § Reason](./notes.md#reason-back-extra).
- **Source**, **corpus**, **anchor** — [sources.md](./sources.md). A deck's corpus feeds the Source tab (column 3) and the chat's context.
- **Workspace** — the overlay opened on a note for editing. [workspace.md](./workspace.md). Everything that edits a note (rewrite, split, create, move, delete) happens there.

## Deck tree (column 1)

Column 1 lists every deck as a row indented by its depth in the `::` hierarchy. Each row shows:

- The deck's **leaf name** (the last `::` segment).
- **Source-kind indicators** — the kinds of the deck's effective corpus (own or inherited), deduplicated, in order. Empty when the deck has no corpus.
- **Own count** — the number of flagged notes whose cards live in exactly this deck.
- **Rolled-up count** — the number of flagged notes in this deck and all its sub-decks, counting each note once even when its flagged cards straddle two sub-decks. Shown as an outlined badge only when it exceeds the own count.

By default only decks with a rolled-up count above zero are shown; a toggle shows all decks. Selecting a deck loads its queue (column 2) and its Source tab (column 3).

## Queue (column 2)

The **queue** is the list of notes for the selected deck, comprising the deck and all its sub-decks (Anki's `deck:"X"` semantics). Each note carries a `deck` field saying where it actually lives.

### Ordering

Flagged notes first, then unflagged; within each group by note id ascending (creation order).

### Filtering

The queue shows flagged notes only by default. A toggle ("voir toutes") shows the whole deck; unflagged notes appear at 55 % opacity. When the queue is empty the column shows "Rien à revoir ici."

### Note display

Each note in the queue shows:

- An **identity line**: flag badge (with flagged-card labels for Cloze notes — see [Question state](#question-state)), short id (`#` + last 4 digits), note type, card count, tags, and a "garder" control for flagged notes.
- **Rendered fields** — every field except `Back Extra`, rendered through the [display transform](#rendering). In question state, the flagged clozes are hidden.
- **Reason callout** — a labelled block below the fields showing the reason, when the note is flagged and the reason is non-empty. This is the only place `Back Extra` appears in the queue.

### Selection

One note is selected at a time. After a deck change the first flagged note is selected. After a decision resolves a note, the next flagged note in the visible list is selected (or nothing if the queue is empty). Keyboard navigation: `j`/`↓` and `k`/`↑` move the selection.

## Question state

A card is flagged from its **question side**: the flagged cloze is hidden, the others are visible. Showing the whole note at once often makes the reason for the flag unreadable, because the answer is right there. Question state solves this by presenting a flagged note the way it looked when the flag was placed.

### Rules

A flagged note whose rendered fields contain cloze deletions is shown **hidden** by default:

- Every cloze whose number matches a flagged card (card ordinal N → cloze c(N+1)) is replaced by its hint (or `[…]` when there is no hint). Its cloze-number label is kept.
- Other clozes stay visible, as they would in Anki.
- The note header names the flagged cards: "⚑ c2" (one label per flagged card).
- "Révéler" (`Espace` on the selected note, or clicking a hidden cloze) switches the note to its full view; the same control switches back. The state is per note, kept in browser memory, reset when the deck changes or the queue is refetched after a decision.

Unflagged notes, notes with no cloze deletion in their rendered fields, and notes whose flagged cards match no cloze (numbering gap) are shown in full, with no reveal control.

The reason callout is shown in both states — it is about the flag, not part of the card.

## Decisions

The queue offers three gestures on a note. Everything that edits a note happens in the workspace; the queue's own decisions are flag-only.

| Gesture | Trigger | Anki writes | Flag | Queue effect |
|---|---|---|---|---|
| **Garder** | "garder" control, or `g` on selected note | none | cleared on all cards | note leaves the queue |
| **Passer** | `p`, or `j`/`k` | none | kept | selection moves to the next note |
| **Ouvrir** | click on note, or `Entrée` on selected note | none (until the workspace validates) | — | the workspace opens |

**Garder** clears nothing but the flag: the flag was a false alarm, the note is fine, its `Back Extra` stays. No confirmation. Clearing a flag means setting the flag value to zero on every card of the note.

After **Garder** and after a workspace validation, the client refetches the deck's notes and the deck counts, then selects the next flagged note. A discarded workspace refetches too but leaves the selection where it was.

The queue header shows "Annuler la dernière validation" while the server holds a snapshot from the last workspace validation ([workspace.md § Undo](./workspace.md#undo)).

The decisions themselves — what a split, a move or a delete writes, how anchors follow the note — are specified in [workspace.md § Validation](./workspace.md#validation). `review.py` holds the functions that perform them, used by `workspace.apply`, and the HTTP routes below stay available for the CLI and the tests.

## Rendering

`render_field(raw) → display HTML` turns one raw Anki field value into safe display HTML. There is a Python implementation (`render.py`) and a JS port in the frontend; both follow the same rules.

The transform guarantees:

- **No injection.** Nothing from the note can produce markup in the output. The raw value is escaped before any display markup is re-introduced.
- **Line breaks preserved.** Anki's `<br>`, closing `</p>`, `</div>`, `</li>` tags each become a line break.
- **Images replaced.** An `<img>` with an `alt` attribute becomes the alt text (Anki's rendered LaTeX images carry the source as alt). An `<img>` without useful alt text becomes `[image]`.
- **All other tags stripped.**
- **Cloze deletions marked up.** Each `{{cN::answer}}` or `{{cN::answer::hint}}` becomes a highlighted span carrying the cloze number and, when present, the hint. The UI shows the cloze number as a small label before each one, since most notes carry several clozes and the flag is per card.
- **Context header extracted.** A context header at the start of the field is extracted and styled separately from the body. See [notes.md § Context header](./notes.md#context-header).
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

Single page, vanilla JS, no framework or bundler. Scripts load in order: `state.js` (state object and helpers), `api.js`, `render.js` (three-column layout), `workspace.js` (the overlay), `app.js` (events and boot). One in-memory state object; every state change triggers a full re-render.

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

Keys are ignored while an input, textarea, or select is focused. Workspace-open keyboard: see [workspace.md § Keyboard](./workspace.md#keyboard).

There are no dialogs: every edit happens on a workspace card. Native `confirm()` is used for the two confirmations (discarding a workspace with changes, validating a plan that deletes).

## Theme

A terminal look: one monospace face throughout, no rounded corners, no drop shadows, and a palette taken from a terminal theme.

### Two layers

Colour lives in two layers, and nothing outside them hardcodes a colour.

**The palette layer** (`themes.css`) holds one block per theme, each a transcription of that theme's `colors.toml` from [Omarchy](https://github.com/basecamp/omarchy) (Basecamp's terminal configuration framework) — the same file Omarchy's terminal reads. The block uses upstream's 24 colour names verbatim and adds nothing. The file is generated by `scripts/fetch_omarchy_themes.py`; a bare `:root` block repeats the default dark theme so an unknown or missing theme still paints a full palette. The palette layer knows nothing about what a colour is for.

**The semantic layer** (the single `:root` block at the top of `app.css`) binds every name the app's rules use onto a palette-layer slot, once, for all themes at once. A new theme is a block in `themes.css` and nothing else; a change of meaning is one line in `app.css` and nothing else.

Two derivation rules govern the bindings:

- A tinted fill (flag background, accent background, etc.) is derived from its hue, not picked from the palette — terminal palettes have no slot for these.
- Recessive text is the foreground faded toward the background, not a border or background colour borrowed from the palette — those palette slots have too little contrast for text in several themes.

### Picker

The theme picker lives in the header of column 1. It lists every theme grouped by mode (dark / light) and repaints on change, so the list doubles as a way to try them on. The choice is stored in `localStorage` under `anki-theme` as a slug, degrading to session-only if `localStorage` throws.

With no stored choice, the OS preference (`prefers-color-scheme`) picks between two defaults — Tokyo Night (dark) and Catppuccin Latte (light) — and is followed live. The resolved theme is stamped on `<html>` as `data-theme` by an inline script in `index.html`, before the stylesheets, so a reload never flashes another theme.

## Logo

A pixel-art star (`static/star.svg`): a flat-colour sprite traced from source artwork. It appears as the mark in the header of column 1 (before the "Decks" title) and as the favicon. It is the one visual that does not follow the theme — the colours live in the image, not in `app.css`.

## Module `review.py`

Pure functions over `AnkiClient` (and `SourceStore` for deck source kinds), no FastAPI imports, so they can be unit-tested with a fake client:

```python
def list_decks(client, store) -> list[DeckSummary]
def list_notes(client, deck) -> DeckNotes
def get_note(client, note_id) -> NoteView
def keep(client, note_id) -> NoteView
def get_notes(client, note_ids) -> list[NoteView]
def search_notes(client, query, limit=50) -> list[NoteView]
def edit(client, note_id, fields=None, tags=None, unflag=True, reflag=None) -> NoteView
def split(client, note_id, original, new_notes) -> SplitResult
def create(client, deck, model, fields, tags) -> NoteView
def move(client, note_id, deck) -> NoteView
def delete(client, note_id) -> None
```

`AnkiClient` gains what these need (e.g. `unflag_note(note_id)`, `note_card_ids(note_id)`), nothing UI-specific.
