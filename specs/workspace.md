# Workspace

> The overlay that opens on a note: the note and the changes being prepared for it on the left, a conversation with Claude on the right, one « Valider » that writes everything at once. Replaces the decision dialogs of the queue and the inline proposal cards of the chat.

General terms (*note*, *card*, *reason*, *source*, *anchor*…) are defined in [00-overview.md § Vocabulary](./00-overview.md#vocabulary). What Claude sees and the tools it has: [chat.md](./chat.md).

## Purpose

Resolving a flagged note usually means *working* on it: reading the reason, asking what is wrong, trying a rewrite, cutting it in two, retouching a fragment, sometimes finding the other notes of the deck with the same defect. The workspace is one surface for all of that. Every change is a **draft** the user can read, edit, compare with the original and discard, and nothing reaches Anki until a single validation. Validation writes all of it or none of it: half a split written would lose content.

## Vocabulary

- **Workspace** — the overlay. Opened on one note, holds cards and one conversation, closed by validation or by discarding.
- **Root** — the note the workspace was opened on. Its deck is the **current deck** (for the prompt and for `search_notes`); its tags and anchors are the defaults of a created note.
- **Card** — one slot of the workspace. A card is either an **existing note** (it has a `note_id`) or a **draft note** (a split fragment or a created note, not in Anki yet). Every card has a **workspace id** (`w1`, `w2`…), unique within the workspace, which is how Claude names it.
- **Version** — the field values of a card at one point. An existing note's card starts at **v0**, the values in Anki, read-only. Each proposal by Claude adds a version; the user's hand edits modify the shown version in place. A draft note has no v0: its first version is Claude's.
- **Changed** — an existing note's card whose shown version is not v0, that is marked deleted or moved, or whose flag (below) differs from what Anki holds. A draft note is always changed.
- **Active** — a card the next message is about. Every card is in Claude's context; the active ones are the target.
- **Deleted** — a state of an existing note's card: the note is removed from Anki at validation. Reversible until then.
- **Flag** — of a card: whether the note is flagged *after* validation, shown as a ⚑ toggle in the card head. **Off** means resolved: the flag is cleared at validation. **On** (« à revoir ») means the note stays — or becomes — flagged, and its `Back Extra` is set to the **comment** typed on the card, so that it shows up in the queue later with that comment as its reason; a draft with the flag on is created already flagged. The **root opens with the flag off**: opening a workspace is resolving the note, and the user turns the flag back on to keep it in the queue. A note brought in by Claude opens with its flag as Anki holds it; a draft opens off. The flag combines with an edit (a partial fix, still flagged) and with a move; a deleted card has none. In the plan and the API a card whose flag is on is **deferred** (`defer`).
- **Kept** — an untouched card (v0, tags unchanged), flagged in Anki, whose flag is off: resolved at validation without being edited. Not a gesture of its own — it is what the root is when the user validates without touching anything, the counterpart of « Garder » in the queue.
- **Moved** — a card carrying a destination deck; applied at validation.
- **Fragment** — a draft note created by a split, linked to its **parent** card (the note that was split).

## Opening and closing

The workspace opens on a note of the queue (column 2, [review.md](./review.md)): click on the note, or `Entrée` with the note selected. The page behind is dimmed by a dark backdrop and stops reacting to keys. Only one workspace at a time.

Opening builds the root card from the note as the queue holds it (raw fields as v0, flagged cards, reason, tags, note type, anchors fetched from `GET /api/sources/anchors`), and starts an **empty conversation**. The conversation belongs to the workspace and dies with it ([chat.md § Conversation lifetime](./chat.md#conversation-lifetime)).

Closing:

- **× (top right) or `Esc`** discards everything: cards, versions, conversation. When the user changed something by hand — a version, a state, a flag toggled, a comment — a confirmation says how many cards will lose their changes; the implicit resolution of the root does not count. Nothing is written to Anki either way.
- **« Valider »** writes the changes (see [Validation](#validation)) and then closes.

After closing the queue and the deck counts are re-fetched. After a validation the next flagged note is selected, not opened (as after « Garder », [review.md § Decisions](./review.md#decisions)); after a discard the selection stays on the root, which is still there.

Nothing is persisted: a page reload drops an open workspace. Past workspaces are not kept (see [Out of scope](#out-of-scope)).

## Cards

### Layout

The workspace is a two-part overlay: cards on the left (about 60 % of the width, scrolling on its own), the conversation on the right. The header holds the root's short id, the toggle « vider Back Extra » ([Validation](#validation)), the « Valider » button and the ×.

Cards are listed root first, then in order of arrival. A fragment is shown right after its parent, indented, with a visible link to it (a bracket on the left edge). Order never changes once a card is in.

### Card head

One line: the activation toggle, the identity (« #5262 » for an existing note, « brouillon » for a draft, plus the note type, the deck when it differs from the current deck, and the tags), the **⚑ flag toggle** (outlined when off; filled, reading « ⚑ à revoir », when on), the state badges (« supprimée », « gardée », « → deck »), then the version controls when the card has more than one version: « ← v2 / 3 → ». Actions at the right: « invalider » (drop the shown version), « supprimer » / « restaurer », « déplacer… » (a deck picker, same list as `list_decks`).

The flag toggle is the only control of the flag: clicking it turns the flag on or off and changes nothing else. It is hidden on a deleted card. The clozes that carried the flag in Anki are not shown in the head any more — the card is drawn as the note will be, resolved — but stay in the reason callout's label (« raison du flag · c2 ») and keep driving the question state of v0.

The **activation toggle** is the head itself: clicking the head (outside a control) toggles the card between active and inactive. An inactive card is drawn at 55 % opacity. Every card is active when it enters the workspace.

### Body

The reason callout (as in the queue) sits at the bottom of an existing note's card, under the fields, in every version — it is about the flag, not part of the card. It always shows the reason as v0 held it, with the flagged clozes in its label.

`Back Extra` is never shown among the fields and is never editable by hand (as in the queue): the callout is the only place it appears, and the only things that write it are the « vider Back Extra » toggle and the comment of a card whose flag is on.

When the **flag is on** the callout becomes the **comment**: a `<textarea>` labelled « raison du flag · sera écrite », prefilled with the plain text of `Back Extra` as v0 holds it (as the shown version holds it, for a draft), so that the user completes or rewrites the existing reason rather than losing it. What the textarea holds at validation is written to `Back Extra` (plain text; line breaks become `<br>`, markup is escaped) — only if the user changed it, so an untouched `Back Extra` is not rewritten. Typing does not redraw. When an existing note's type has no `Back Extra` (v0 has no such field), the callout says so (« pas de champ Back Extra : le flag sera posé sans commentaire ») and nothing is written but the flag. A draft always gets the textarea — a proposal may simply have left the field out — and the server checks the note type at validation.

A draft whose flag is off shows its `Back Extra`, when the proposal filled it, in the same read-only callout labelled « Back Extra »: the field is hidden from the editable fields like everywhere else, and this is the one place it can be read.

Fields are rendered with the display renderer ([review.md § Rendering](./review.md#rendering)). **v0 is shown in question state**: the flagged clozes hidden, « Révéler » to show them, exactly as in the queue ([review.md § Question state](./review.md#question-state)). Every other version is shown in full: a rewrite may renumber clozes, and the hidden state is for understanding the flag, not for proofreading the fix.

Under a version proposed by Claude, its `rationale` in one muted line.

A **deleted** card is struck through and its fields are not editable. A **moved** card shows the destination in the badge; the move is a modifier, an existing note's card can be edited and moved.

### Editing

Click on a field → that field becomes a `<textarea>` holding the **raw value** (HTML and cloze markers), focused, with no preview; blur (click elsewhere, `Tab`) → the field is rendered again. The other fields of the card stay rendered meanwhile. Field names are not editable; fields cannot be added or removed by hand. A `propose_edit` with a `model` different from the card's current type replaces the entire field set with the target type's fields ([chat.md § Proposal tools](./chat.md#proposal-tools)); the card head shows the effective type with a ⇄ indicator when it differs from v0.

Editing the shown version modifies it in place, except when the shown version is **v0**: v0 is Anki's and never changes, so the first keystroke copies v0 into a new version (marked « éditée ») that becomes the shown one. Any version other than v0 is editable, Claude's included; a hand-edited version of Claude's keeps its rationale and gains the « éditée » mark.

### Versions

A card's versions are a list; the **shown version** is the one the arrows point at, and the one that counts for validation and for Claude ([chat.md § Context](./chat.md#context)). A new version (a proposal by Claude) is appended and becomes the shown one, wherever the arrows were.

**« invalider »** drops the shown version; the previous one is shown (or the next, when a draft's first version was dropped). On an existing note's card it is offered on every version but v0. On a draft note dropping the last version removes the card — a fragment removed this way is simply gone, the parent is not touched.

A dropped version is gone from the workspace. The conversation still holds the text Claude wrote with it, and the client tells Claude in the history that the version was rejected (« [version rejetée : w3 v2] », [chat.md § API](./chat.md#api)).

### Split

Claude's `propose_split` on a card yields: a new version on that card holding the first fragment's fields (the note keeps its scheduling history, as `review.split` does today) and one **fragment card** per other fragment, linked to it. When the proposal says the original is not kept (`original: null`), the card is marked deleted instead of gaining a version, and every fragment is a fragment card. The user can achieve the same by marking the parent deleted by hand. Fragments inherit the parent's note type (unless the proposal names another), tags and anchors.

Fragments are ordinary draft cards afterwards: Claude can target one (`w4`) for a retouch, the user can edit or drop it.

### How notes enter

1. **The root**, on opening.
2. **`add_notes`** ([chat.md § Read tools](./chat.md#read-tools)): Claude asks for notes to be shown — typically after a `search_notes`. The server checks the cap, returns the notes' text to Claude (same as `get_notes`) and sends the client an `added` event; the client fetches the notes (`POST /api/notes/lookup`) and appends one card each, v0 = the note.
3. **A proposal on a note that is not in the workspace** (`propose_edit`, `propose_split`, `propose_move` whose `target` is a note id absent from the cards): the client fetches the note, adds its card, then applies the proposal to it. Claude may do this directly after a search when the user asked for the fix, not for a look.
4. **`propose_create`**: a new draft card, no parent, tags and anchors from the root unless the proposal gives `source_ids`.

Notes already in the workspace are never added twice (a note id maps to one card). The workspace holds at most **50 cards**: the server refuses `add_notes` and absent-target proposals that would exceed it with a tool error asking Claude to narrow down, and the client refuses likewise if it ever receives one.

Notes cannot be added by hand in v1: ask Claude (« ajoute #5262 », « ajoute les notes sur KKT »).

## Conversation

The right pane is the chat of [chat.md](./chat.md), unchanged in its mechanics (input, chips for attaching sources, streaming, reading summaries), with two differences in what is rendered:

- A proposal of kind `edit`, `split`, `create` or `move` does not render as a card in the log. It lands on the workspace (a version, a fragment, a new card, a badge) and the log shows one muted pointer line: « → carte w3 » (or « → 3 cartes » for a split).
- `create_source` and `edit_source` proposals stay in the log with their « Appliquer », as today: they write into the vault, not into Anki, and are applied on click, not at validation. The × does not undo them.

## Validation

### The button

« Valider » carries the count of what it will do: « Valider · 2 modifiées · 3 créées · 1 supprimée · 1 gardée · 1 à revoir · 1 déplacée » (zero counts omitted). A card counts under every action it carries: an edited card with the flag on is both « modifiée » and « à revoir », a draft with the flag on both « créée » and « à revoir », a moved card also « déplacée ». Disabled when the plan is empty and during the write. On a freshly opened workspace the plan already holds the root as kept: « Valider · 1 gardée » is the workspace's « Garder ». When the plan deletes at least one note, a confirmation lists them; nothing else asks for confirmation — the workspace itself is the review step.

### What is written

The **plan** is built from the cards. Per existing note's card, in this order: deleted → `delete`; edited (shown version ≠ v0, or tags changed) → `edit`, with the `defer` modifier when the flag is on; otherwise, flag on → `defer` when the note is not flagged in Anki, the comment was changed or the card is moved; flag off → `keep` when the note is flagged in Anki or the card is moved. Any other card is not in the plan: a note brought in for a look, flag as Anki holds it, is left alone. `comment` is sent only when the user changed it.

| Card | Anki writes | Flag |
|---|---|---|
| Draft note (fragment, created) | `addNote` in its deck (parent's deck for a fragment, current deck otherwise), model, tags; anchors written to `sources.json`. Deferred: `Back Extra` is the comment in the same `addNote`, whether or not the proposal gave the field, as long as the note type has it (`modelFieldNames`) | — (new notes are unflagged), unless deferred: red on every card |
| Existing, edited | `updateNote` with the shown version's fields (and tags if changed); when the plan carries a different `model`, `updateNoteModel` instead (swaps the note type, writes fields and tags in one call — c1's history is kept, c2+ become orphan cards removed by Check Database) | cleared |
| Existing, kept | none | cleared |
| Existing, deferred (alone or with an edit) | `updateNote` setting `Back Extra` to the comment (in the edit's `updateNote` when there is one; skipped when the note type has no such field) | **kept**; a red flag is set on every card of a note that carried none |
| Existing, moved (with either of the above) | `changeDeck` on all cards; anchors re-checked against the destination corpus ([sources.md § Anchors](./sources.md#anchors)) | cleared |
| Existing, deleted | `deleteNotes` | — |

**« vider Back Extra »** (header toggle, on by default): every *edited* note that had a reason gets `Back Extra` set to empty in the same `updateNote`. Kept notes are not touched, for the same reason « Garder » in the queue clears nothing: the note was fine. Deferred notes are exempt: their `Back Extra` is the comment, whatever the toggle says.

### Order, snapshot, rollback

AnkiConnect has no transactions, so all-or-nothing is emulated. `workspace.apply` proceeds:

1. **Validate** the plan's shape (unknown action, a create without fields, a note listed twice…) before touching Anki.
2. **Snapshot** every existing note of the plan in one read: raw fields, tags, deck, model, card ids, flagged card ids.
3. **Create** the draft notes (`review.create`, then anchors). A fragment inherits the parent's anchors; a created note gets the plan's `source_ids`. A deferred draft is flagged (red, every card) right after its creation; a rollback deletes it like any created note.
4. **Edit** (`review.edit` with `unflag=False`), `Back Extra` emptied when the toggle says so, or set to the comment when the card is deferred. A deferred card that is not edited gets its comment written by its own `updateNote` here.
5. **Move** (`changeDeck`, anchors re-checked).
6. **Unflag** every edited, kept or moved note that is not deferred. **Flag** (red, every card) each deferred note that carried no flag.
7. **Delete**, one `deleteNotes` call for all of them. Their anchors are removed from `sources.json` best-effort (a failure there is reported, not fatal: « nettoyer » catches orphans).

Deletion comes last so that a failure anywhere before it has lost no content. On the first failure the write stops and a **rollback** is attempted: created notes deleted, edited notes restored from the snapshot (fields, tags, flags put back colour by colour, flags set by a deferral removed), moved notes moved back. The response reports what failed and whether the rollback completed; the client keeps the workspace open, marks the cards concerned and shows the report. Draft cards that were created and not rolled back gain their `note_id` so a second « Valider » does not create them twice.

### Undo

The server keeps the snapshot of the **last successful validation** in memory (one, overwritten by the next validation, gone when the server restarts). The queue header shows « Annuler la dernière validation » while one is available (`GET /api/workspace/undo` says, at load and after each validation). Clicking it restores the snapshot: created notes deleted, edited notes' fields and tags put back (a deferred note's comment included), moves reverted, flags put back on the cards that carried one and removed from the cards a deferral flagged. The notes come back in the queue.

Undo is **unavailable** when the validation deleted notes — a deleted note cannot be recreated with its history — and the button is not shown. It is **refused** (« modifiée depuis, annulation impossible ») when one of the notes no longer holds the values the validation wrote, which means it was edited since, in the app or in Anki; nothing is written then.

No finer undo: a version, a card or a proposal is not a unit of writing any more. Discarding before validation is free; after validation this single step is the way back.

## Keyboard

While the workspace is open the queue's keys are off. `Esc` closes (with the confirmation when there are changes) — unless a field has the focus (a card's textarea, the message box, the deck picker): then `Esc` only gives the focus back, and a second `Esc` closes. `⌘/Ctrl+Entrée` sends the message. `Tab` moves between fields as in any form. No other shortcut in v1.

## API

All under `/api`. Errors follow [review.md § API](./review.md#api) (502 AnkiConnect, 503 unreachable, 404 unknown note).

| Method & path | Body | Returns |
|---|---|---|
| `POST /api/notes/lookup` | `{ note_ids: [int] }` | `Note[]` (same shape as `GET /api/notes/{id}`; unknown ids dropped, order kept) |
| `POST /api/workspace/apply` | `ApplyPlan` (below) | `ApplyReport` (below); `200` whether or not every write succeeded — `ok` says |
| `POST /api/workspace/undo` | — | `ApplyReport`; `409` when there is nothing to undo, when the last validation deleted notes, or when a note was modified since |
| `GET /api/workspace/undo` | — | `{ available: bool }` — whether the button should be shown |

```json
{ "deck": "courant::00-Thèse", "clear_reason": true,
  "cards": [
    { "wid": "w1", "action": "edit",   "note_id": 1732375559262, "fields": { "Text": "<raw>", "Back Extra": "" }, "tags": ["phd"], "move_to": null },
    { "wid": "w2", "action": "create", "parent_wid": "w1", "deck": "courant::00-Thèse", "model": "Cloze", "fields": { "Text": "<raw>", "Back Extra": "" }, "tags": ["phd"], "source_ids": ["r9wt4n"] },
    { "wid": "w3", "action": "keep",   "note_id": 1732375559999, "move_to": "courant::01-AI" },
    { "wid": "w4", "action": "delete", "note_id": 1732375560001 },
    { "wid": "w5", "action": "defer",  "note_id": 1732375560002, "comment": "Trop vague, à recouper avec le cours §3", "move_to": null },
    { "wid": "w6", "action": "edit",   "note_id": 1732375560003, "fields": { "Text": "<raw>", "Back Extra": "<raw>" }, "defer": true, "comment": "Reformulée, mais il manque l'exemple" } ] }
```

`fields` on an `edit` are the **complete** raw values of the shown version (every field), so the server never merges. `tags` on `edit` is omitted when unchanged. `defer` (on `edit` and `create`, default false) and the action `defer` mark the note as deferred; `comment` is the plain text to write to `Back Extra` and is only accepted with a deferral (`null` leaves the field as the plan otherwise has it). The server, not the client, turns the comment into field HTML. `create` carries `parent_wid` only for fragments (the server does not use it — the client already resolved deck, tags and anchors — it is there for the report).

```json
{ "ok": true, "created": { "w2": 1757400000001 }, "resolved": [1732375559262, 1732375559999],
  "deferred": [1732375560002, 1732375560003], "deleted": [1732375560001], "moved": [1732375559999],
  "errors": [], "rolled_back": false, "undo_available": false }
```

`errors` is empty on success; on failure it names each failed step (« création de w2 : … », « annulation : … »). `rolled_back` is true when every rollback step succeeded. `undo_available` is true after a successful validation that deleted nothing.

## Module `workspace.py`

Pure functions over `AnkiClient` and `SourceStore`, no FastAPI imports, reusing `review.create`, `review.edit`, `review.move`, `review.delete`:

```python
@dataclass class CardPlan: wid, action, note_id, fields, tags, model, deck, source_ids, move_to, parent_wid, defer, comment
@dataclass class ApplyPlan: deck, clear_reason, cards
@dataclass class NoteSnap: note_id, fields, tags, deck, model, card_ids, flags   # flags: card id -> flag, restored colour by colour
@dataclass class Snapshot: notes: dict[int, NoteSnap]; created; deleted; moved; anchors_before; written; model_changed; unflagged; flagged
@dataclass class ApplyReport: ok, created, resolved, deferred, deleted, moved, errors, rolled_back, undo_available

def validate(plan) -> list[str]
def take_snapshot(client, plan) -> Snapshot
def apply(client, store, plan) -> tuple[ApplyReport, Snapshot]
def undo(client, store, snapshot) -> ApplyReport        # raises Modified / NothingToUndo
```

`anchors_before` records a moved note's anchors, since a move can drop the ones absent from the destination corpus and moving back must restore them. `routes_workspace.py` holds the last snapshot on `app.state.last_validation`, clears it after an undo, and maps the exceptions to 409.

## Frontend

`static/workspace.js` renders the overlay (`wsOverlay()`, appended by `draw()` when `S.ws` is set) and handles its events; `S.ws` in `state.js` is the whole workspace state — cards with their versions and shown index, states, the conversation, the attached sources, the report of a failed validation. Field editing is the one place where typing does not redraw: the textarea writes into the shown version on `input`, the redraw happens on blur.

## Out of scope

History of past workspaces; adding a note by hand while the workspace is open; undo after a validation that deleted notes; more than one workspace at a time; per-version or per-card undo after validation; keyboard navigation between cards.
