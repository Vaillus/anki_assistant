# anki-assistant

Efficiently review flagged Anki cards: a local web interface that shows, deck by deck,
the notes to rework, the deck's sources (Obsidian notes, PDFs, web pages) side by side, and a Claude chat that
proposes one-click edits. The app opens Anki's collection file itself, through the `anki`
Python package (the code Anki desktop runs), and syncs it with AnkiWeb after its writes.

Behaviour is described in [`specs/`](specs/00-overview.md) (source of truth).

Anki desktop must be **closed** while the app works: Anki opens its collection file with an
exclusive lock, so the app and Anki desktop take turns. While Anki desktop is open, the app
answers « Close Anki desktop first » and writes nothing. The `anki` package is pinned to Anki
desktop's version (`26.9.2`); bump both together.

## Installation

```bash
uv sync
cp .env.example .env   # then fill in ANTHROPIC_API_KEY for the chat
uv run anki login      # once: keeps the AnkiWeb key in .env, so the app syncs after its writes
```

| Variable | Default | Meaning |
|---|---|---|
| `ANKI_COLLECTION` | the only profile's | Path to `collection.anki2` (e.g. `~/Library/Application Support/Anki2/User 1/collection.anki2`). |
| `ANKIWEB_HKEY` | unset | The AnkiWeb key `anki login` writes. Unset: no AnkiWeb sync from the app; Anki desktop sends its changes at its next sync. |
| `ANKIWEB_ENDPOINT` | unset | The AnkiWeb server `anki login` writes when AnkiWeb names one. |

## Web interface

```bash
uv run anki-web        # http://localhost:5070
```

Three columns: decks with their flagged-note count, the selected deck's queue, and a
Source / Chat panel. The selected note exposes the decisions: Keep, Edit, Split,
Create, Move, Delete, Skip. Details in [`specs/review.md`](specs/review.md).

## Phone app

The phone app (`/m`) reviews one deck on the iPhone, offline, and replays the answers into
Anki at the next sync. Details in [`specs/mobile.md`](specs/mobile.md).

`uv run anki-web` also serves the phone app alone on a second port, `5071` (the **phone
port**), both bound to `127.0.0.1`. [Tailscale](https://tailscale.com) exposes the phone
port over HTTPS on your tailnet (offline caching on the phone requires HTTPS):

```bash
tailscale serve --bg 5071    # https://<mac-name>.<tailnet>.ts.net -> localhost:5071
tailscale serve status       # check it
tailscale serve reset        # stop exposing it
```

> **Never `tailscale serve` port 5070.** The main app has no login and writes to Anki; your
> tailnet may be shared. Only the phone port checks who is asking.

Then open `https://<mac-name>.<tailnet>.ts.net/m` on the phone and add it to the home screen.
On the Mac, `http://localhost:5070/m` shows the same page, without the check.

Settings, in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `MOBILE_OWNER_LOGIN` | unset | Your Tailscale login (e.g. `you@example.com`). Every request to the phone port is refused (`403`) unless its `Tailscale-User-Login` is this login; unset refuses them all. |
| `MOBILE_PORT` | `5071` | The phone port. |
| `MOBILE_DECK` | `courant` | The deck the phone reviews (sub-decks included). |
| `ANKI_ROLLOVER_HOUR` | `4` | Anki's "next day starts at" hour, to count Anki days. |

The phone port serves only `/m`, the page's static files and `/api/mobile/`; anything else
is `404`. Answers given on the phone are appended to `review_log.jsonl` (gitignored).

## Desktop launcher

[`scripts/app/install.sh`](scripts/app/) builds **Anki Assistant.app** in `~/Applications`:
a real app bundle that starts the server and renders the
UI in its own window — no tab strip, no address bar, its own Dock icon and running dot.
See [`scripts/app/README.md`](scripts/app/README.md) for details.

```bash
scripts/app/install.sh              # build it (pass a directory to install elsewhere)
scripts/app/launch.sh               # servers up, then show the window
scripts/app/launch.sh stop          # stop the server
```

## CLI

```bash
uv run anki decks                         # list decks
uv run anki cards "My Deck" -n 10         # cards from a deck
uv run anki cards "My Deck" -q is:due     # + Anki filter
uv run anki flagged "My Deck"             # flagged cards in the deck
uv run anki flagged "My Deck" --flag 1    # red flags only
uv run anki search 'tag:vocab is:new'     # raw Anki query
uv run anki card 1234567890123            # card details
uv run anki note 1234567890123            # note details
uv run anki edit 1234567890123 Back="New answer"
uv run anki flag 0 1234567890123          # clear the flag
uv run anki tag 1234567890123 -t review
```

`--json` on any command gives machine-readable output. `--collection PATH` opens another
`collection.anki2` than `ANKI_COLLECTION`'s; the command keeps the AnkiWeb login from `.env`.

Like the web app, the CLI needs Anki desktop closed. A command that wrote (`edit`, `flag`,
`tag`) waits up to 10 seconds for its AnkiWeb sync before exiting, and says on stderr when
the sync failed or was still running: its changes then go with the next sync.

```bash
uv run anki login                         # once: AnkiWeb key into .env (never the password)
```

## Python

```python
from anki_assistant import AnkiClient

anki = AnkiClient()  # the only profile's collection; Anki desktop closed
for card in anki.flagged_cards("My Deck"):
    print(card.flag_name, card.plain_fields())

anki.update_note_fields(card.note_id, {"Back": "corrected"})
anki.clear_flag([card.card_id])
```

## Deck sources

Each deck can be linked to sources: a PDF, an Obsidian vault note, or a web page
(a URL, re-fetched by the server when its text is requested — nothing is copied).
The mapping lives in `sources.json` (overridable with the `ANKI_SOURCES` env var).

```bash
uv run anki source set "courant::00-Thèse" "Allocation sur des angles disjoints"
uv run anki source set "courant::01-AI::little book of deep learning" ~/Docs/lbdl.pdf
uv run anki source set "courant::04-maths::dérivés" https://en.wikipedia.org/wiki/Derivative
uv run anki source get "courant::01-AI::little book of deep learning::4"  # inherited from parent
uv run anki source list
uv run anki source missing        # decks without a source + unreachable targets
uv run anki source open "courant::00-Thèse"
```

The type is inferred from the target: an `http(s)` URL yields a web page, a `.pdf` path
a PDF, everything else is treated as a vault note. `--kind` forces the choice.

A sub-deck with no entry of its own inherits the source from its closest parent deck:
mapping `courant::01-AI::little book of deep learning` therefore covers its sub-decks `::1` through `::6`.

The `cards` and `flagged` commands display the deck's source at the top of the output.

```python
from anki_assistant import SourceStore

store = SourceStore()
source = store.get("courant::00-Thèse")
print(source.kind, source.target, source.uri(store.vault))
```
