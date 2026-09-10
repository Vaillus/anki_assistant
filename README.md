# anki-assistant

Revoir efficacement les cartes Anki flaguées : une interface web locale qui montre, deck par deck,
les notes à retravailler, les sources du deck (notes Obsidian, PDF) à côté, et un chat Claude qui
propose des modifications applicables en un clic. Tout est écrit directement dans Anki via
l'add-on [AnkiConnect](https://foosoft.net/projects/anki-connect/).

Le comportement est décrit dans [`specs/`](specs/00-overview.md) (source de vérité).

Prérequis : Anki ouvert avec AnkiConnect installé (port 8765 par défaut).

## Installation

```bash
uv sync
cp .env.example .env   # puis renseigner ANTHROPIC_API_KEY pour le chat
```

## Interface web

```bash
uv run anki-web        # http://localhost:5070
```

Trois colonnes : les decks avec leur nombre de notes flaguées, la file du deck sélectionné, et un
panneau Source / Chat. La note sélectionnée expose les décisions : Garder, Modifier, Splitter,
Créer, Déplacer, Supprimer, Passer. Détails dans [`specs/review.md`](specs/review.md).

## CLI


```bash
uv run anki decks                         # liste des decks
uv run anki cards "Mon Deck" -n 10        # cartes d'un deck
uv run anki cards "Mon Deck" -q is:due    # + filtre Anki
uv run anki flagged "Mon Deck"            # cartes flaguées du deck
uv run anki flagged "Mon Deck" --flag 1   # seulement les rouges
uv run anki search 'tag:vocab is:new'     # requête Anki brute
uv run anki card 1234567890123            # détail d'une carte
uv run anki note 1234567890123            # détail d'une note
uv run anki edit 1234567890123 Back="Nouvelle réponse"
uv run anki flag 0 1234567890123          # retire le flag
uv run anki tag 1234567890123 -t revoir
```

`--json` sur n'importe quelle commande donne une sortie machine.

## Python

```python
from anki_assistant import AnkiClient

anki = AnkiClient()
for card in anki.flagged_cards("Mon Deck"):
    print(card.flag_name, card.plain_fields())

anki.update_note_fields(card.note_id, {"Back": "corrigé"})
anki.clear_flag([card.card_id])
anki.invoke("anyAnkiConnectAction", param=1)  # passe-plat générique
```

## Sources des decks

Chaque deck peut être associé à une source : un PDF, ou une note du vault Obsidian.
Le mapping vit dans `sources.json` (surchargeable avec la variable `ANKI_SOURCES`).

```bash
uv run anki source set "courant::00-Thèse" "Allocation sur des angles disjoints"
uv run anki source set "courant::01-AI::little book of deep learning" ~/Docs/lbdl.pdf
uv run anki source get "courant::01-AI::little book of deep learning::4"  # hérité du parent
uv run anki source list
uv run anki source missing        # decks sans source + cibles introuvables
uv run anki source open "courant::00-Thèse"
```

Le type est déduit de la cible : un chemin en `.pdf` donne un PDF, tout le reste est traité
comme une note du vault. `--kind` force le choix.

Un sous-deck sans entrée propre hérite de la source du deck parent le plus proche : mapper
`courant::01-AI::little book of deep learning` couvre donc ses sous-decks `::1` à `::6`.

Les commandes `cards` et `flagged` affichent la source du deck en tête de sortie.

```python
from anki_assistant import SourceStore

store = SourceStore()
source = store.get("courant::00-Thèse")
print(source.kind, source.target, source.uri(store.vault))
```

## Todos

`todo.md` mirrors the TickTick project **anki assistant**. Each line carries its task id in
a trailing HTML comment, which is what pairs the two sides:

```markdown
- [ ] review the specs <!--tt:6a9d847a3302cf9468487e2d-->
```

```bash
uv run python scripts/sync_todos.py             # push local edits, then pull
uv run python scripts/sync_todos.py --dry-run   # report what would change
uv run python scripts/sync_todos.py --pull-only # let TickTick win, write nothing back
```

A sync pushes first, then pulls:

| Local edit                       | Effect in TickTick             |
| -------------------------------- | ------------------------------ |
| Check a box                       | Task completed, line moves to `## Done` |
| Add a `- [ ]` line with no id     | Task created, id written back  |
| Change a line's text              | Task renamed                   |
| Delete a line                     | Nothing — the pull restores it |

Deleting is deliberately one-way: remove the task in TickTick instead. If a title changed on
both sides since the last sync, the local text wins.

Credentials are read from the [`tt` CLI](https://github.com/Vaillus/ticktick-cli) config at
`~/.config/tt/.env`, so there is no second set of secrets here. Run `tt auth` when the script
reports an expired token.
