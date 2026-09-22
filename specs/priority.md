# Priority queue

> Surface the flagged cards the user will actually encounter today, before or during their Anki session.

A synthetic entry at the top of the [deck tree](./review.md#deck-tree-column-1), above all real decks. It aggregates flagged notes across every deck that match **urgency criteria** — cards the user keeps deferring during Anki reviews and that pile up instead of being resolved.

## Criteria

A flagged note is "priority" when at least one of its flagged cards matches any of:

| Criterion | Anki state | Why it's urgent |
|---|---|---|
| **Buried** | `is:buried` (queue −2 or −3) | The user saw the card and deferred it — it will come back tomorrow unchanged |
| **Due within budget** | `is:due`, within the study deck's remaining review count | A deferred card that came back AND that Anki will present today — resolve it before the review session |
| **New** | `is:new` | A new card flagged as not fit for learning; it blocks the new-card queue |

Buried and new flagged cards are always included. Due flagged cards are included only when they fall within the study deck's remaining review budget.

### Study deck and budget filtering

The **study deck** is the single root deck the user launches Anki reviews from, set via the `STUDY_DECK` env var (e.g. `courant`).

When set, the server:

1. Fetches `review_count` from `getDeckStats` for the study deck — the remaining reviews for today.
2. Finds every due review card under it (`deck:"<study_deck>" is:due -is:new -is:buried`).
3. Sorts them by `(deck_name, due)` — deck alphabetical first (matching Anki's deck-order presentation), then most overdue first within each deck.
4. Takes the first `review_count` cards — the budget window.
5. Keeps only the flagged cards that fall within that window.

Without `STUDY_DECK`, all flagged due cards are included (no budget filtering).

## Deck tree row

The priority row appears at the top of the deck tree, visually separated from real decks. It shows:

- A fixed label — **"Priority"**.
- A count — the number of distinct flagged notes matching the criteria.

The row is hidden when the count is zero (nothing urgent). It is always visible otherwise, regardless of the "show all decks" toggle.

## Queue behaviour

When the priority row is selected, the queue (column 2) loads the matching notes as a flat list. Each note carries its `deck` field as usual, and the queue renders the deck name on each note's identity line so the user knows where it lives.

Sort order: by note id ascending. The same [decisions](./review.md#decisions) apply (Keep, Skip, Open); after a decision the queue refreshes with the same priority query. The Source tab is inactive in priority mode (no corpus to show across decks).

## API

| Route | Purpose |
|---|---|
| `GET /api/decks` | Response includes `priority_count` alongside the deck list |
| `GET /api/notes/priority` | Flagged notes matching the priority criteria (cross-deck) |

Both routes respect `STUDY_DECK` for budget filtering. See [review.md § API](./review.md#api) for the shared error conventions.
