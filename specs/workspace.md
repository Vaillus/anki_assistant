# Workspace

> The overlay that opens on a note: cards on the left, a conversation with Claude on the right, one « Valider » that writes everything at once.

General terms (*note*, *Anki card*, *note type*, *field*, *reason*) are defined in [notes.md](./notes.md). What Claude sees and the tools it has: [chat.md](./chat.md).

The workspace serves the review queue ([review.md](./review.md)).

## Opening and closing

The workspace opens on a note from the queue (click, or `Entrée` with the note selected). The page behind is dimmed and stops reacting to keys. Only one workspace at a time.

Opening builds the **root** card — the note the workspace was opened on — and starts an empty conversation. The root's deck is the **current deck** for the prompt and for `search_notes`. The conversation belongs to the workspace and dies with it.

Closing:

- **× or `Esc`** discards everything: cards, versions, conversation. When at least one card is [changed](#the-button), a confirmation says how many changes will be lost. Nothing is written to Anki.
- **« Valider »** writes the changes ([Validation](#validation)) and then closes.

After closing, the queue and deck counts are re-fetched. After a validation the next flagged note is selected; after a discard the selection stays on the root.

Nothing is persisted: a page reload drops an open workspace. Past workspaces are not kept.

While the workspace is open, the queue's keyboard shortcuts are off. `Esc` while a field or the message box is focused only returns the focus; a second `Esc` closes. `⌘/Ctrl+Entrée` sends the message. No other workspace shortcut in v1.

## Cards

A **card** holds one note and the [versions](#versions) of its fields being prepared for validation. A card is either an **existing note** (it has a `note_id`) or a **draft note** (a split fragment or a created note, not in Anki yet). Every card has a **workspace id** (`w1`, `w2`…), unique within the workspace, which is how Claude and the UI name it.

```
┌ w1 ─────────────────────────────────────────────────┐
│ card head                                            │
│  ● #5262 Cloze  ⚑c2       ← v1 / 2 →  [invalider]  │
│  tags: phd                [supprimer] [garder] [→ …] │
├──────────────────────────────────────────────────────┤
│ card body                                            │
│                                                      │
│  Text:  There are {{c1::three}} conditions…          │
│                                                      │
│  rationale: « split c2 into its own note »           │
│                                                      │
│  ┌ reason ─────────────────────────────────────────┐ │
│  │ trop vague, à découper                          │ │
│  └─────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────┘
  ┊ fragment link
┌ w2 (fragment of w1) ────────────────────────────────┐
│ card head                                            │
│  ● brouillon Cloze                                   │
│ card body                                            │
│  Text:  {{c1::KKT}} is the special case when…       │
└──────────────────────────────────────────────────────┘
```

### Layout

The workspace is two panes: cards on the left (about 60 % of the width, scrolling on its own), the conversation on the right. The header holds the root's short id, the « vider Back Extra » toggle ([Validation](#validation)), the « Valider » button and the ×.

Cards are listed root first, then in order of arrival. A [fragment](#split) is shown right after its parent, indented, with a visible link to it. Order never changes once a card is in.

### Card head

One line, left to right:

- **Activation toggle** — clicking the head (outside a control) toggles the card between **active** and inactive. A card is active when the next message is about it; every card starts active. An inactive card is drawn at 55 % opacity.
- **Identity** — « #5262 » for an existing note, « brouillon » for a draft, plus the note type, the deck when it differs from the current deck, and the tags.
- **State badges** — flag per flagged Anki card as in the queue, « supprimée », « gardée », « à revoir », « → deck ».
- **Version controls** — « ← v2 / 3 → », shown when the card has more than one version.
- **Actions** — « invalider », « supprimer » / « restaurer », « garder » / « ne pas garder », « différer » / « ne pas différer », « déplacer… » (a deck picker).

« garder » is offered only on an untouched card (v0, tags unchanged) that is not deferred; « différer » on any existing note's card that is not deleted or kept. Marking a card deleted or kept drops its deferral, and vice versa.

### Card body

Fields are rendered with the display renderer ([review.md § Rendering](./review.md#rendering)). The original version from Anki (**v0**) is shown in question state: flagged clozes hidden, « Révéler » to show them, as in the queue ([review.md § Question state](./review.md#question-state)). Every other version is shown in full.

The reason callout sits at the bottom, under the fields, in every version — it always shows the reason as v0 held it. `Back Extra` is never shown among the fields and is never editable by hand; the only things that write it are the « vider Back Extra » toggle and the comment of a deferred card.

On a **deferred** card the callout becomes the **comment**: a `<textarea>` labelled « raison du flag · sera écrite », prefilled with the plain text of `Back Extra` as v0 holds it, so that the user completes or rewrites the existing reason rather than losing it. What the textarea holds at validation is written to `Back Extra` (plain text; line breaks become `<br>`, markup is escaped). When the note type has no `Back Extra`, the callout says so (« pas de champ Back Extra : le flag sera posé sans commentaire ») and nothing is written but the flag.

Under a version proposed by Claude, its rationale in one muted line.

A card marked **deleted** will have its note removed from Anki at [validation](#validation); the card is struck through and not editable. A **kept** card has its flag cleared at validation without being edited. A **deferred** (« à revoir ») card keeps its flag at validation — or receives one when it had none — and its `Back Extra` is set to the comment typed on the card, so that it comes back in the queue later with that comment as its reason. Combines with an edit (a partial fix, still flagged) and with a move; exclusive with kept and deleted. A **moved** card carries a destination deck, applied at validation; a card can be both edited and moved.

### Editing

Click on a field to edit: the field becomes a textarea holding the raw value, focused, with no preview; blur closes it and the field is rendered again. The other fields stay rendered meanwhile. Field names are not editable; fields cannot be added or removed by hand.

Editing modifies the shown version in place, except **v0**: v0 is Anki's and never changes, so the first keystroke copies v0 into a new version (marked « éditée ») that becomes the shown one. Any version other than v0 is editable, including Claude's; a hand-edited Claude version keeps its rationale and gains the « éditée » mark.

A `propose_edit` with a different model replaces the entire field set with the target type's fields ([chat.md § Proposal tools](./chat.md#proposal-tools)); the card head shows the effective type with a ⇄ indicator.

### Versions

A card's **versions** are a list. An existing note's card starts at **v0**, the values in Anki, read-only. A draft note has no v0: its first version is Claude's. Each proposal by Claude appends a version; the user's hand edits modify the shown version in place. The **shown version** is the one the arrows point at, and the one that counts for validation and for Claude's context ([chat.md § What Claude sees](./chat.md#what-claude-sees)).

**« invalider »** drops the shown version; the previous one is shown (or the next, when a draft's first version was dropped). On an existing note it is offered on every version but v0. Dropping a draft's last version removes the card.

A dropped version is gone from the workspace. The conversation notes the rejection in the history ([chat.md § What Claude remembers](./chat.md#what-claude-remembers-between-turns)).

### Split

`propose_split` on a card yields: a new version on that card holding the first fragment's fields (the note keeps its scheduling history) and one **fragment** card per other fragment, linked to the **parent** card. When the proposal says the original is not kept, the card is marked deleted instead of gaining a version, and every piece is a fragment card. Fragments inherit the parent's note type (unless the proposal names another), tags and [anchors](./sources.md#anchors).

Fragments are ordinary draft cards afterward: Claude can target one for a retouch, the user can edit or drop it.

### How notes enter

1. **The root**, on opening.
2. **`add_notes`** ([chat.md § Read tools](./chat.md#read-tools)): Claude asks for notes to be shown, typically after a search. The workspace appends one card per note, v0 = the note.
3. **A proposal on a note not in the workspace** (`propose_edit`, `propose_split`, `propose_move` targeting a note id absent from the cards): the note is fetched, its card added, then the proposal applied to it.
4. **`propose_create`**: a new draft card with no parent; tags and anchors default to the root's unless the proposal gives `source_ids`.

Notes already in the workspace are never added twice. The workspace holds at most **50 cards**; additions that would exceed the cap are refused with an error asking Claude to narrow down.

Notes cannot be added by hand in v1.

## Conversation

The right pane is the chat of [chat.md](./chat.md), unchanged in its mechanics. Two differences in what is rendered: a proposal that lands on the workspace shows as a muted pointer line in the log (« → carte w3 »), not as a card; source proposals (`create_source`, `edit_source`) stay in the log with their « Appliquer » since they write into the vault on click, not at validation.

## Validation

### The button

« Valider » carries the count of what it will do: « Valider · 2 modifiées · 3 créées · 1 supprimée · 1 gardée · 1 à revoir · 1 déplacée » (zero counts omitted). Disabled when no card is **changed** — an existing note's card whose shown version is not v0, or that is marked deleted, kept, deferred or moved; a draft note is always changed — and during the write. When the plan deletes at least one note, a confirmation lists them.

### What is written

The **plan** — the set of writes to perform — is built from the cards.

A draft note is added to Anki. An existing note's card is classified into one action: a card can be deleted, kept and edited at the same time, so **deleted** wins over kept, which wins over edited (shown version ≠ v0), which wins over deferred; an edited card may also be deferred (the `defer` modifier of an `edit`). On top of any action, the card can also carry a **move** to another deck. Cards still on v0 with no state are not in the plan and keep their flag.

| Card | Anki writes | Flag |
|---|---|---|
| Draft note (fragment, created) | `addNote` in its deck (parent's deck for a fragment, current deck otherwise), with model, tags; anchors written to `sources.json` | — |
| Existing, edited | `updateNote` with the shown version's complete fields (and tags if changed); `updateNoteModel` when the model changed | cleared |
| Existing, kept | none | cleared |
| Existing, deferred (alone or with an edit) | `updateNote` setting `Back Extra` to the comment (in the edit's `updateNote` when there is one; skipped when the note type has no such field) | **kept**; a red flag is set on every card of a note that carried none |
| Existing, moved (combined with edit, keep or defer) | `changeDeck` on all Anki cards; anchors re-checked against the destination corpus ([sources.md § Anchors](./sources.md#anchors)) | cleared (unless deferred) |
| Existing, deleted | `deleteNotes` | — |

An edit sends every field of the shown version, not just the ones that changed; the server writes them as-is.

**« vider Back Extra »** (header toggle, on by default): every edited note that had a reason gets `Back Extra` set to empty. Kept notes are not touched. Deferred notes are exempt: their `Back Extra` is the comment, whatever the toggle says.

### Order and rollback

Writes proceed in a safe order: creates, edits, moves, unflag/flag, deletes. Deletion comes last so that a failure anywhere before it has lost no content.

On the first failure the write stops and a rollback is attempted: created notes deleted, edited notes restored from a snapshot taken before the write (fields, tags, flags put back colour by colour, flags set by a deferral removed), moved notes moved back. The response reports what failed and whether the rollback completed. The workspace stays open; draft cards that were created and not rolled back gain their `note_id` so a second « Valider » does not create them twice.

### Undo

The server keeps the state of every existing note before the write for the **last successful validation** only — one snapshot, overwritten by the next, gone when the server restarts. The queue header shows « Annuler la dernière validation » while one is available.

Undo is **unavailable** when the validation deleted notes — a deleted note cannot be recreated with its history. It is **refused** when a note no longer holds the values the validation wrote, which means it was edited since; nothing is written then.

Undo restores: created notes deleted, edited notes' fields, tags and flags put back (a deferred note's comment included), moves reverted, flags removed from the cards a deferral flagged. The notes come back in the queue.

## API

All under `/api`. Errors follow [review.md § API](./review.md#api).

| Method & path | Purpose |
|---|---|
| `POST /api/workspace/apply` | Write the validation plan |
| `POST /api/workspace/undo` | Revert the last validation |
| `GET /api/workspace/undo` | Whether undo is available |

## Out of scope

History of past workspaces; adding a note by hand while the workspace is open; undo after a validation that deleted notes; more than one workspace at a time; per-version or per-card undo after validation; keyboard navigation between cards.
