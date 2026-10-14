# Review

> The main page of the app: a three-column layout plus the workspace overlay.

The page at `/` is a single screen split into three columns — the deck tree on the left, the queue of [notes](./notes.md) for the selected deck in the centre, and the Source tab ([sources.md](./sources.md)) on the right — plus the [workspace](./workspace.md) overlay that opens on a note for editing. This spec covers columns 1–2 and the decisions that write to Anki; the Source tab and the workspace have their own specs.

The goal is to show, for one deck, the notes that need attention (flagged) with the rest of the deck one click away, and let the user resolve each note with a single decision. A resolved note leaves the list immediately.

The [priority queue](./priority.md) appears at the top of the deck tree, above all real decks.

## Deck tree (column 1)

Column 1 lists every deck as a row, in the **app's tree**: the decks of the collection arranged by the app, with Anki's deck names untouched. Each row shows:

- The deck's **leaf name** (the last `::` segment), its [number prefix](#number-prefixes) hidden. Hovering a row shows its full Anki name.
- Its [label](#labels), when it has one: a colored bar and the label's name.
- **Source-kind indicators** — the kinds of the deck's effective [corpus](./sources.md) (own or inherited), deduplicated, in order. Empty when the deck has no corpus.
- **Own count** — the number of flagged notes whose cards live in exactly this deck.
- **Rolled-up count** — the number of flagged notes in this deck and all its sub-decks, counting each note once even when its flagged cards straddle two sub-decks. Shown as an outlined badge only when it exceeds the own count.

By default only decks with a rolled-up count above zero are shown (the **flagged-only view**); a toggle shows all decks. Selecting a deck loads its queue (column 2) and its Source tab (column 3).

Anki desktop and AnkiMobile know nothing of the app's tree: they keep Anki's deck order and show no label.

### Tree parent and the app deck order

A deck's **tree parent** is the deck the app's tree shows it under: its Anki parent, or, when that parent is a group turned into a [label](#labels), that group's own tree parent. Top-level decks share one tree parent, the top of the tree.

The **app deck order** is the order of the decks under each tree parent, stored by the app. The tree is read top to bottom: the decks under each tree parent in the app deck order, each deck's sub-decks right after it. **Anki's deck order** is the order in which Anki lists sibling decks: by each `::` part of the name, case ignored, a parent before its sub-decks.

- A tree parent whose decks were never reordered lists them in Anki's deck order, a group turned into a label replaced by its decks, in their order.
- A tree parent whose decks were reordered stores the list of its decks. A deck it does not hold yet (created in Anki, in the app or on another device, or put under this parent in Anki) goes after the stored ones, in Anki's deck order. A deck deleted in Anki, or put under another tree parent, leaves the list, so one put back later lands like a created deck.
- The order is keyed by **deck id** (Anki's number for a deck, stable across renames and syncs), so a deck renamed in Anki desktop keeps its place.

### Moving a deck

A **move** drags a deck row to a new place among the decks of its tree parent; its sub-decks move with it. The drop line decides the place: the line right above a deck of the same tree parent places the dragged deck before it; the line below the last row of that tree parent's decks (the last deck's own sub-decks included) places it after the last one. A line anywhere else (among another deck's sub-decks, or under another tree parent) refuses the drop, and the row goes back.

A move rewrites the one list of the app deck order stored under that tree parent, and is saved at once. A drop that leaves the order as it is (on the deck's own place) stores nothing. Deck names, Anki's collection and every other list stay as they are. In the flagged-only view the dropped deck lands right before the visible deck under the line (after the last visible one at the end), and hidden decks keep their order.

### Number prefixes

A **number prefix** is the digits and the dash at the start of a deck's own name (`01-AI`, `00-fundamentals`, `1-lignes`), there only to place the deck in Anki's deck order. The app's tree hides them (`01-AI` shows as `AI`). Digits with no dash after them stay (`3D`), and a name that is nothing but a prefix (`01-`) shows as it is. Two decks under one tree parent whose names would read the same once hidden (`01-AI` and `AI`) both keep theirs.

Only a row's text changes, and the heading of a selected deck's queue hides the prefix the same way. Everything else keeps the full Anki name: the new-deck input, search, the corpus, the chat.

### Labels

A **label** is a name and a color the app stores, carried by a deck. It covers the deck that carries it and every deck under it, which show its color. **Labels never overlap**: no deck a label covers carries another one. The color is picked by the app from a fixed palette.

A deck gets a label in one of two ways:

- **A group turned into a label.** A **group** is a parent deck whose sub-decks share a theme (`courant::01-AI`). Turned into a label, its row leaves the app's tree and its sub-decks take its place among the decks of its tree parent, in their existing order, each carrying the label. The label reads the group's name, number prefix hidden (`AI`), and follows a rename of the group in Anki. A deck created in Anki under the group carries it too.
- **A label set on a deck.** It only colors the deck: its row, its tree parent and its place do not change.

Both are reached from the row's right-click menu:

| Item | Offered on | Effect |
|---|---|---|
| « Étiquette ▸ » an existing label, « Nouvelle étiquette… » (asks a name), « Retirer l'étiquette » | a deck no label covers, or carrying a label set on it | sets that label on the deck; « Retirer l'étiquette » takes off the one set on it |
| « Transformer en étiquette » | a deck with sub-decks, no label covering it, other than the [study deck](./priority.md#budget-filtering) and the [mobile deck](./mobile.md#phone-app) | turns the group into a label |

A deck covered by a label set above it, or by a group's label, shows that label greyed in the menu and takes no other. A label, or « Transformer en étiquette », that would overlap another label is refused, and the menu names a deck carrying it (« python porte l'étiquette AI »). Setting a label on a deck removes the same label set on decks under it.

**Turning a group into a label.** A group can hold two things of its own, filed under its full name and reached only from its row: cards that sit directly in it, and a corpus. Both get a new home first:

- **Its own cards** move, in Anki, to a sub-deck named `misc` (`courant::04-maths::misc`), created with the group's preset and shown first among the label's decks, or to the group's existing `misc` sub-deck. Each card keeps its note, reviews and flag. This is a write to Anki, synced as after any write the app makes. A filtered deck searching the group still finds the cards, since its search covers the sub-decks.
- **Its corpus** is copied onto each of its sub-decks (the new `misc` included), after each sub-deck's own sources, ids kept so every [anchor](./sources.md#anchors) stays valid, and removed from the group ([sources.md § Inheritance](./sources.md#inheritance)). A deck created under the group later gets none.

One `confirm()` dialog states every effect (the number of cards moved and the sub-deck they go to, the sources copied) and the group turns only once confirmed; a group with neither turns at once. In Anki every deck keeps its full name, and so does everything keyed by it.

**Changes made in Anki.** The app brings its labels in line with the collection each time it lists the decks:

- A group deleted in Anki takes its label with it, also from the decks it was set on; a deck deleted takes the label set on it.
- A deck put under a group turned into a label takes the label; one taken out of it loses it.
- When a change puts one label under another, a group's label wins over a label set on a deck, and between two of the same kind the higher one wins; the other is removed. A group whose label is removed shows its row again, its sub-decks under it.
- A label with no group that no deck carries any more is removed.

### Where the app stores it

The app deck order and the labels live in `deck_order.json` at the project root (override with `ANKI_DECK_ORDER`), gitignored, every deck named by its deck id: `order` maps a tree parent's id (`0` for the top of the tree) to the ids of its decks in order, holding only the tree parents that were reordered; `labels` maps a label id to its color and either the group's deck id or its own name; `deck_labels` maps a deck id to the label set on it.

### Creating a deck

Two entry points, same outcome:

1. **Add-deck button** — a row at the top of the deck list (below the header), labelled "+ Add a deck". Clicking it opens an inline input where the user types the full deck name. An autocomplete dropdown suggests existing deck paths as the user types, so they can pick a parent prefix (e.g. selecting `Médecine::Cardio` and then typing `::Arythmies`).
2. **Context menu** — right-clicking a deck row opens a context menu with "Nouveau sous-paquet" (opens the inline input pre-filled with the right-clicked deck's name followed by `::`), the [label](#labels) items, and "Supprimer le paquet" (deletes the deck and its cards after a `confirm()` prompt).

Pressing `Entrée` in the input submits; `Échap` cancels and closes the input. The input is dismissed after a successful creation or on cancel.

**Validation.** Before creating, the frontend checks that the name is non-empty and does not match an existing deck name (case-sensitive, against the loaded deck list). On conflict the input shows an inline error ("Ce paquet existe déjà") and stays open.

**Creation.** `POST /api/decks` with `{ "name": "<full deck name>" }`. The server creates the deck, and any intermediate deck it needs. Returns the created deck name. On success the deck tree refreshes and the new deck is selected; it takes its place in the [app deck order](#tree-parent-and-the-app-deck-order) like any deck the order does not hold yet.

## Queue (column 2)

The **queue** is the list of notes for the selected deck and all its sub-decks. Each note carries a `deck` field saying where it actually lives: the deck of its first card, which is the deck its row shows. Flagged notes come first, then unflagged; within each part, notes follow their deck's place in the [app's tree](#tree-parent-and-the-app-deck-order) read top to bottom (a deck's own notes before its sub-decks'), then note id ascending (creation order). The queue shows flagged notes only by default; a toggle ("show all") shows the whole deck, unflagged notes at 55 % opacity. When the queue is empty the column shows "Nothing to review here."

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
- **Images.** An image whose alt carries text is shown as that text: Anki's rendered LaTeX images carry the LaTeX source as alt, which MathJax then renders. An image without useful alt (a pasted picture) is shown as the picture itself, fetched from Anki's media through `GET /api/media/{filename}`; only the `src` file name is taken from the note, so the output tag is one the app writes. An image with neither useful alt nor a media file name in `src` becomes `[image]`, and so does a picture inside a cloze hint.
- **All other tags stripped.**
- **Cloze deletions marked up.** Each cloze deletion is displayed with its number as a label and, when present, its hint. The number is needed because most notes carry several clozes and the flag is per card.
- **Context header extracted.** A [context header](./notes.md#context-header) at the start of the field is extracted and styled separately from the body.
- **MathJax rendered.** `\(…\)`, `\[…\]`, `[$]…[/$]`, `[$$]…[/$$]` delimiters pass through the escape step and are rendered by MathJax 3, loaded in the page. After each DOM update the app calls `MathJax.typesetPromise()`.

## API

All routes are under `/api`. Each request is one action of the [holding policy](./00-overview.md#anki). Errors: Anki refused the operation → 502, unknown note → 404, collection held by Anki desktop → 503 with `Close Anki desktop first`. All error bodies are `{ "detail": "<message>" }`.

| Route | Purpose |
|---|---|
| `GET /api/decks` | `{ decks, labels, priority_count }` — every row of the app's tree in order (deck id, full name, shown leaf, tree depth, tree parent id, label, flagged counts, source kinds, whether « Transformer en étiquette » is offered or why it is refused), the labels, and the [priority](./priority.md) note count |
| `POST /api/decks` | Create a new deck |
| `DELETE /api/decks` | Delete a deck (cards move to Default) |
| `POST /api/decks/move` | `{ deck_id, before? , after? }` — [move](#moving-a-deck) a deck before or after a deck of its tree parent; 409 when that deck is under another tree parent |
| `PUT /api/decks/label` | `{ deck_id, label_id? , name? }` — set an existing label, or a new one by name, on a deck; `{ deck_id }` alone takes off the label set on it; 409 when refused |
| `GET /api/decks/group-label?deck_id=` | What turning a group into a label would do: `{ refused, own_cards, misc, misc_exists, sources }` |
| `POST /api/decks/group-label` | `{ deck_id }` — turn a group into a label; 409 when refused |
| `GET /api/notes/priority` | Flagged notes matching the [priority](./priority.md) criteria (cross-deck) |
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
| `GET /api/media/{filename}` | One file from Anki's media folder, with its content type; 404 when missing or when the name has a path separator |
| `GET /api/anki/sync` | `{ ok, message }` — the [sync status](./00-overview.md#ankiweb-sync) of the app's last AnkiWeb sync; never opens the collection |

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
