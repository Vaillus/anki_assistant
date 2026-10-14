# Priority queue

> Surface flagged cards the user will encounter today — buried, new, or due within the review budget.

A synthetic entry at the top of the [deck tree](./review.md#deck-tree-column-1), above all real decks. It aggregates flagged notes across every deck that match urgency criteria — cards that pile up instead of being resolved.

## Criteria

A flagged note is priority when at least one of its flagged cards is:

| Criterion | Anki state | Why it's urgent |
|---|---|---|
| **Buried** | `is:buried` (queue −2 or −3) | Deferred — will come back tomorrow unchanged |
| **Due within budget** | `is:due`, in the study deck's study queue | Anki will present it today |
| **New** | `is:new` | Blocks the new-card queue |

Buried and new flagged cards are always included. Due flagged cards are budget-filtered (see below). Without a study deck configured, all flagged due cards are included.

## Budget filtering

The **study deck** (`STUDY_DECK` env var, e.g. `courant`) is the root deck the user reviews from. A flagged due card is **within budget** when it is in the study deck's **study queue**: the ordered cards Anki's scheduler would show today for that deck, as Anki builds it, with its limits (the effective deck limit of the deck and each sub-deck, minus what Anki counts as studied today) and its display order. Flagged due cards outside it are excluded.

## Deck tree row

Shows **"Priority"** and a count badge. Hidden when the count is zero; always visible otherwise, regardless of the "show all decks" toggle. Visually separated from real decks.

## Queue behaviour

A flat cross-deck list. Each note shows its deck name on the identity line. Same [decisions](./review.md#decisions) as a regular queue (Keep, Skip, Open); after a decision the queue refreshes. The Source tab is inactive.

## API

| Route | Purpose |
|---|---|
| `GET /api/decks` | Response includes `priority_count` alongside the deck list |
| `GET /api/notes/priority` | Priority notes (cross-deck, budget-filtered) |
