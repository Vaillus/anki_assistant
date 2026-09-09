# Review

> Columns 1 and 2 of the page, the note-level view of a deck, and the decisions that write to Anki.

## Purpose

Show, for one deck, the notes that need attention (flagged) with the rest of the deck one click away, and let the user resolve each note with a single decision. A resolved note leaves the queue immediately.

## Data model

### Deck (column 1)

```json
{ "name": "courant::01-AI::RL+LLM", "leaf": "RL+LLM", "depth": 2,
  "flagged_own": 3, "flagged_total": 3, "source_kinds": ["obsidian"] }
```

- `flagged_own` — number of **notes** (not cards) whose cards live in exactly this deck and carry a flag.
- `flagged_total` — same, summed over this deck and all its sub-decks. Shown as an outlined badge when it exceeds `flagged_own`.
- `source_kinds` — kinds of the deck's *effective* corpus (own or inherited), deduplicated, in order. Empty list when none.

Column 1 shows by default only decks with `flagged_total > 0`, indented by depth; a toggle shows all decks. Selecting a deck loads column 2 and the Source tab.

### Note (column 2)

```json
{ "note_id": 1732375559262, "deck": "courant::00-Thèse", "model": "Cloze",
  "tags": ["phd"], "card_ids": [1732375559263, 1732375559264],
  "flagged": true, "flag_colors": ["orange"],
  "flagged_cards": [{ "card_id": 1732375559264, "ord": 1, "flag_color": "orange" }],
  "fields": { "Text": "<raw anki html>", "Back Extra": "<raw>" },
  "fields_html": { "Text": "<display html>", "Back Extra": "…" },
  "reason": "For a given sensor ?" }
```

- `fields` is what the user edits and what goes back to Anki. `fields_html` is display-only (see [Rendering](#rendering)).
- `flagged_cards` — the cards of the note that carry a flag, sorted by `ord` (Anki's card ordinal, 0-based). For a Cloze note, card `ord` is the card that hides cloze `c{ord+1}`. Empty when the note is not flagged. Used by [Question state](#question-state).
- `reason` — plain text of `Back Extra` if the model has that field and the note is flagged, else `""`. The UI shows it in an orange callout at the bottom of the note, below the fields, labelled « raison du flag ». `Back Extra` is not shown among the fields of the queue: the callout is the only place it appears, so that it is not read twice.
- Anchors are not part of the note payload: the workspace fetches them per card with `GET /api/sources/anchors?note_id=` ([sources.md](./sources.md#anchors)).
- Queue order: flagged notes first, then unflagged; within each group by `note_id` ascending (creation order). Column 2 shows flagged only by default; a toggle « voir toutes » shows the whole deck, unflagged notes at 55% opacity.
- A deck query returns notes of the deck **and its sub-decks** (Anki's `deck:"X"` semantics). `deck` on each note says where it actually lives.

## Decisions

The queue offers three gestures on a note. Everything that edits a note — rewrite, split, create a sibling, move, delete — happens in the **workspace** ([workspace.md](./workspace.md)), which writes at its own validation.

| Gesture | How | Anki writes | Flag | Queue |
|---|---|---|---|---|
| Garder | « garder » control in the note head, or `g` on the selected note | none | cleared on all cards | note leaves |
| Passer | `p`, or `j`/`k` | none | kept | selection moves to the next note, nothing else |
| Ouvrir | click on the note, or `Entrée` on the selected note | none (until the workspace validates) | — | the workspace opens on the note |

Rules:

- Clearing a flag = `setSpecificValueOfCard(card, ["flags"], [0])` for **every card of the note**.
- **Garder** clears nothing but the flag: the flag was a false alarm, the note is fine as is, its `Back Extra` stays. No confirmation.
- After **Garder** and after a workspace validates, the client re-fetches the deck's notes and the deck counts (`/api/decks`), then selects the next flagged note in the visible list (or nothing if the queue is empty, showing « Rien à revoir ici »). A discarded workspace re-fetches too but leaves the selection where it was.
- The queue header shows « Annuler la dernière validation » while the server holds one ([workspace.md § Undo](./workspace.md#undo)).

The decisions themselves — what a split, a move or a delete writes, how anchors follow the note — are specified in [workspace.md § Validation](./workspace.md#validation); `review.py` keeps the functions that perform them (`edit`, `split`, `create`, `move`, `delete`), used by `workspace.apply`, and their HTTP routes below stay available for the CLI and the tests.

## Rendering

`render.py::render_field(raw: str) -> str` turns Anki field HTML into safe display HTML:

1. `<br>`, `</p>`, `</div>`, `</li>` → newline.
2. `<img … alt="…">` → the alt text (Anki's LaTeX images carry the source as alt). Other `<img>` → `[image]`.
3. Strip remaining tags, unescape entities, then escape for HTML.
4. Cloze markers `{{cN::answer::hint}}` → `<span class="cloze" data-n="N" data-hint="hint">answer</span>` (`data-hint` omitted when there is no hint). The UI shows the cloze number as a small `cN` label in front of every cloze span (CSS, from `data-n`), since most notes carry several clozes and the flag is per card.
5. Newlines → `<br>`.

MathJax delimiters (`\(…\)`, `\[…\]`, `[$]…[/$]`) are left as text in v1.

### Question state

A card is flagged from its **question side**: the flagged cloze is hidden, the others are visible. Seeing the whole note at once often makes the reason for the flag unreadable, so a flagged note is first shown with the same clozes hidden:

- A flagged note whose fields contain cloze spans is shown **hidden** by default: every cloze `c{ord+1}` of a card in `flagged_cards` is replaced by `[…]` (or `[hint]` when the marker has a hint), keeping its `cN` label. Other clozes stay visible, as they would in Anki.
- The note header names the flagged cards: « ⚑ c2 » (one label per flagged card).
- « Révéler » (button in the note, `Espace` on the selected note, or clicking a hidden cloze) switches the note to its full view; the same control switches back. The state is per note, kept in browser memory, and reset when the deck changes or the queue is refetched after a decision (the note is gone anyway).
- Unflagged notes, notes with no cloze span, and notes whose flagged cards match no cloze (numbering gap) are shown in full, with no reveal control.
- The `reason` callout is shown in both states: it is the note *about* the flag, not part of the card.

## API

All under `/api`. Errors from AnkiConnect surface as HTTP 502 `{ "detail": "<message>" }`; unknown note → 404; Anki unreachable → 503.

| Method & path | Body | Returns |
|---|---|---|
| `GET /api/decks` | — | `Deck[]` |
| `GET /api/notes?deck=` | — | `{ deck, total, flagged, notes: Note[] }` |
| `GET /api/notes/{id}` | — | `Note` |
| `POST /api/notes/lookup` | `{ note_ids: [int] }` | `Note[]` (unknown ids dropped, order kept) — used by the workspace to materialise cards |
| `POST /api/notes/{id}/keep` | — | `Note` (flags cleared) |
| `PATCH /api/notes/{id}` | `{ fields?: {…}, tags?: [...], unflag?: true, reflag?: [card_id] }` | `Note` |
| `POST /api/notes/{id}/split` | `{ original: { fields, tags? } \| null, new_notes: [{ model?, fields, tags? }] }` | `{ original: Note \| null, created: Note[] }` |
| `POST /api/notes` | `{ deck, model, fields, tags?, source_ids? }` | `Note` (anchored to `source_ids` when given) |
| `POST /api/notes/{id}/move` | `{ deck }` | `Note` |
| `DELETE /api/notes/{id}` | — | `204` |
| `GET /api/models` | — | `{ "Cloze": ["Text", "Back Extra"], "Basic": ["Front", "Back"], … }` |

Anchors are read and written through `SourceStore` by the routes: `split` copies the original's anchors onto the created notes; `move` re-checks each against the destination corpus (`workspace.anchors_after_move`, shared with the workspace); `POST /api/notes` writes `source_ids`.

`original: null` in split deletes the original **after** the new notes were created successfully. `PATCH` with `unflag` omitted defaults to `true`. `reflag` puts a flag back on the listed cards (used by the workspace's rollback and undo, [workspace.md](./workspace.md#undo)); the colour is red, since colours carry no meaning here. The workspace's own endpoints (`/api/workspace/…`) are specified in [workspace.md § API](./workspace.md#api).

## Frontend

Single page, vanilla JS split in `state.js` (state and helpers), `api.js`, `render.js` (the three columns), `workspace.js` (the overlay), `app.js` (events, boot); one in-memory state object, full re-render on change (same approach as the prototype). Layout and classes may start from `prototype/review_ui/static/*` variant A, rewritten properly (the prototype is not imported).

Keyboard, workspace closed: `j`/`k` or `↓`/`↑` move the selection in column 2; `g` = Garder; `p` = Passer; `Entrée` = Ouvrir; `Espace` = Révéler (see [Question state](#question-state)). Keys are ignored while an input, textarea or select is focused. Workspace open: see [workspace.md § Keyboard](./workspace.md#keyboard).

There are no dialogs: every edit happens on a workspace card, raw value in a `<textarea>` on click, rendered client-side otherwise with the same rules as `render.py` (a JS port, display-only). Native `confirm()` is enough for the two confirmations left (discarding a workspace with changes, validating a plan that deletes).

### Theme

A terminal look: one monospace face throughout on a 20px line box, no rounded corners, no drop shadows, and a palette taken from a terminal theme. Colour lives in **two layers**, and nothing outside them hardcodes a colour.

**The palette layer**, `static/themes.css`, holds one block per theme, `:root[data-theme="<slug>"]`, of `--om-*` custom properties. Each is a transcription of that theme's `colors.toml` in [basecamp/omarchy](https://github.com/basecamp/omarchy) — the file `flutter_omarchy` reads — keeping upstream's own 24 names (`accent`, `selection`, `muted`, `background`, `dark_background`, `foreground`, `light_foreground`, the hues and their `bright_` variants) and adding nothing. `mode` in the source becomes `color-scheme`, so native widgets (scrollbars, `<select>`, checkboxes, `::backdrop`) match. The file and the theme index `static/themes.js` are both generated by `scripts/fetch_omarchy_themes.py`; a bare `:root` block repeats the default dark theme so an unknown or missing `data-theme` still paints a full palette. Neither file knows what a colour is *for*.

**The semantic layer**, the single `:root` block at the top of `app.css`, binds every name the rules use onto an `--om-*` slot, once, for all themes at once. So a new theme is a block in `themes.css` and nothing else; a change of meaning is one line in `app.css` and nothing else. Two rules govern the binding:

- *A tint is derived, never picked.* A terminal palette has no slot for `--accentbg`, `--flagbg`, `--okbg` and friends, so each is `color-mix` of its own hue into `--bg`. They exist only until the rules that use them move to reverse video.
- *Recessive text fades the foreground, it does not borrow a chrome slot.* `--om-muted` and `--om-dark-foreground` are border colours: across the 22 themes their contrast against the background falls as low as 1.5:1. `--muted` and `--dim` are therefore `--om-light-foreground` mixed toward `--bg` (85% and 50%), which keeps the ramp monotone and legible in every theme. `--om-muted` is used for `--line-strong`, a border, which is what it is.

The picker in the head of column 1 lists every theme, grouped by mode, and repaints on change so the list doubles as a way to try them on. The choice is stored in `localStorage` under `anki-theme` as a slug, degrading to session-only if `localStorage` throws. With no choice stored the OS preference (`prefers-color-scheme`) picks between two defaults — Tokyo Night and Catppuccin Latte — and is followed live through a `matchMedia` listener. The resolved slug is stamped on `<html>` as `data-theme` by a tiny inline script in `index.html`, **before** the stylesheets, so a reload never flashes another theme; that script repeats the two defaults because it has to beat the stylesheet.

### Logo

A pixel-art star, `static/star.svg`: the sprite traced from the source artwork at its
native **40×40** grid — one `<rect>` per horizontal run of identical pixels, three flat
tones (`#040d6e` outline, `#0759f4` body, `#388efe` bevel along the upper-left inner
edges), background left transparent. `shape-rendering="crispEdges"` on the SVG and
`image-rendering: pixelated` on the `<img>` keep the pixel edges hard at any size; the
header renders it at 20 CSS px, i.e. exactly one device pixel per sprite pixel at 2× DPR.

It appears twice: as the mark at the left of the head of column 1, before the `Decks`
title, and as the favicon (`<link rel="icon">` in `index.html`). It is the one visual that
does not follow the [theme](#theme) — the same sprite in light and dark, since the colours
live in the image and not in `app.css`.

## Module `review.py`

Pure functions over `AnkiClient`, no FastAPI imports, so they can be unit-tested with a fake client:

```python
def list_decks(client, store) -> list[DeckSummary]
def list_notes(client, deck) -> DeckNotes
def get_note(client, note_id) -> NoteView
def keep(client, note_id) -> NoteView
def get_notes(client, note_ids) -> list[NoteView]              # chat read tool, batched, unknown ids dropped
def search_notes(client, query, limit=50) -> list[NoteView]    # chat read tool, see chat.md
def edit(client, note_id, fields=None, tags=None, unflag=True, reflag=None) -> NoteView
def split(client, note_id, original, new_notes) -> SplitResult
def create(client, deck, model, fields, tags) -> NoteView
def move(client, note_id, deck) -> NoteView
def delete(client, note_id) -> None
```

`AnkiClient` gains what these need (e.g. `unflag_note(note_id)`, `note_cards(note_id)`), nothing UI-specific.
