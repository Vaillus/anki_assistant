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
  "reason": "For a given sensor ?",
  "anchors": [{ "source_id": "r9wt4n", "kind": "obsidian", "target": "maths/différentiabilité", "status": "valid" }] }
```

- `fields` is what the user edits and what goes back to Anki. `fields_html` is display-only (see [Rendering](#rendering)).
- `flagged_cards` — the cards of the note that carry a flag, sorted by `ord` (Anki's card ordinal, 0-based). For a Cloze note, card `ord` is the card that hides cloze `c{ord+1}`. Empty when the note is not flagged. Used by [Question state](#question-state).
- `reason` — plain text of `Back Extra` if the model has that field and the note is flagged, else `""`. The UI shows it in an orange callout at the top of the note, above the fields, labelled « raison du flag ». The `Back Extra` field itself is still shown among the fields.
- `anchors` — the sources the note is anchored to ([sources.md](./sources.md#anchors)), in order, `[]` when none. `status` is `"valid"` or `"dangling"` (source not in the deck's effective corpus). Shown as one chip « ⚓ différentiabilité » per anchor under the fields, plus « ⚓ ancrer… » to add one.
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
- After **Garder** and after a workspace closes (validated or discarded), the client re-fetches the deck's notes and the deck counts (`/api/decks`), then selects the next flagged note in the visible list (or nothing if the queue is empty, showing « Rien à revoir ici »).
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

Anchors are read and written through `SourceStore` by the routes: `list_notes` / `get_note` attach `anchors`; `split` copies the original's anchors onto the created notes; `move` re-checks each against the destination corpus; `POST /api/notes` writes `source_ids`.

`original: null` in split deletes the original **after** the new notes were created successfully. `PATCH` with `unflag` omitted defaults to `true`. `reflag` puts a flag back on the listed cards (used by the workspace's rollback and undo, [workspace.md](./workspace.md#undo)); the colour is red, since colours carry no meaning here. The workspace's own endpoints (`/api/workspace/…`) are specified in [workspace.md § API](./workspace.md#api).

## Frontend

Single page, vanilla JS split in `state.js` (state and helpers), `api.js`, `render.js` (the three columns), `workspace.js` (the overlay), `app.js` (events, boot); one in-memory state object, full re-render on change (same approach as the prototype). Layout and classes may start from `prototype/review_ui/static/*` variant A, rewritten properly (the prototype is not imported).

Keyboard, workspace closed: `j`/`k` or `↓`/`↑` move the selection in column 2; `g` = Garder; `p` = Passer; `Entrée` = Ouvrir; `Espace` = Révéler (see [Question state](#question-state)). Keys are ignored while an input, textarea or select is focused. Workspace open: see [workspace.md § Keyboard](./workspace.md#keyboard).

There are no dialogs: every edit happens on a workspace card, raw value in a `<textarea>` on click, rendered client-side otherwise with the same rules as `render.py` (a JS port, display-only). Native `confirm()` is enough for the two confirmations left (discarding a workspace with changes, validating a plan that deletes).

### Theme

Light and dark. Every colour is a CSS custom property defined on `:root`; nothing else in `app.css` hardcodes one. The default is the OS preference (`prefers-color-scheme`), followed live through a `matchMedia` listener; the ☾ / ☀︎ toggle in the head of column 1 overrides it in both directions and stores the choice in `localStorage` under `anki-theme` (`"light"` | `"dark"`), degrading to session-only if `localStorage` throws. The resolved theme is stamped on `<html>` as `data-theme` by a tiny inline script in `index.html`, **before** the stylesheet, so a reload never flashes the other theme; `app.css` therefore holds exactly two palettes, `:root` (light) and `:root[data-theme="dark"]`, each setting `color-scheme` so native widgets (scrollbars, `<select>`, checkboxes, `::backdrop`) match.

## Module `review.py`

Pure functions over `AnkiClient`, no FastAPI imports, so they can be unit-tested with a fake client:

```python
def list_decks(client, store) -> list[DeckSummary]
def list_notes(client, store, deck) -> DeckNotes                # store: anchors
def get_note(client, store, note_id) -> NoteView
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
