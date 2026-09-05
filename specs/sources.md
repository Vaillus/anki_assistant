# Sources

> A deck's corpus: the documents it was made from, how they are stored, resolved, displayed, and turned into text for Claude.

## Purpose

When reviewing a flagged note the user wants the original material within reach, and Claude needs it in context to verify or rewrite. A deck therefore points to a **corpus**: an ordered list of sources.

## Data model

`sources.json` at the project root (override with `ANKI_SOURCES`):

```json
{
  "vault": { "name": "Vault", "path": "/Users/hugovaillaud/Documents/Vault" },
  "decks": {
    "courant::00-Thèse": [
      { "kind": "obsidian", "target": "Allocation sur des angles disjoints" },
      { "kind": "pdf", "target": "~/Documents/these/stone_search.pdf", "pages": "12-19", "note": "chap. 2" }
    ],
    "courant::01-AI::little book of deep learning": [
      { "kind": "pdf", "target": "~/Documents/the-little-book-of-deep-learning.pdf" }
    ]
  }
}
```

- `kind` — `"obsidian"` or `"pdf"`.
- `target` — note name relative to the vault root, with or without `.md` (obsidian), or a filesystem path, `~` allowed (pdf).
- `pages` — optional, pdf only. `"12-19"`, `"7"`, `"3-5,9"`. 1-based, inclusive. Absent = whole document.
- `note` — optional free text shown next to the source.
- **Backward compatibility:** a deck value that is a single object (the 0.1 format) is read as a one-element list and rewritten as a list on next save.

### Inheritance

`SourceStore.corpus(deck) -> list[Source]`: the deck's own list if present, else the nearest ancestor's (`a::b::c` → `a::b` → `a`), else `[]`. `Source.deck` records which deck the entry was written on so the UI can say « héritée de courant::01-AI ».

### Kinds

| kind | exists? | `uri()` | text |
|---|---|---|---|
| obsidian | `<vault>/<target>.md` is a file | `obsidian://open?vault=<name>&file=<target>` (percent-encoded, `%20` for spaces, never `+`) | file content |
| pdf | path is a file | `file://` URI | `pypdf` text of the selected pages, pages joined with `\n\n--- page N ---\n\n` |

## Text extraction

`Source.text(vault, max_chars=60_000) -> SourceText`:

```python
@dataclass
class SourceText:
    text: str  # extracted, possibly truncated
    truncated: bool
    n_pages: int | None  # pdf only
    warning: str  # e.g. "PDF entier (312 pages) sans plage de pages : seules les 60 000 premiers caractères sont passés."
```

- Obsidian: read the file; strip YAML front matter (`---` block at the top); keep the Markdown as is.
- PDF: open with `pypdf`, extract the pages in `pages` (or all). Cache the extraction in memory keyed by `(path, mtime, pages)` — PDFs are slow to parse and the same corpus is requested on every note.
- Truncate to `max_chars` with `truncated=True`. A whole PDF without `pages` gets the warning above so the user learns to set a range.

## UI — Source tab (column 3)

- Header per source: kind chip (`obsidian` violet, `pdf` red), target, `pages`/`note` in muted text, « héritée de … » when inherited, ⚠ when the file is missing, an « ouvrir ↗ » link to `uri`.
- Below: the extracted text in a scrollable monospace block (first 4 000 chars, « afficher plus » expands to the full `text`).
- Footer: « + ajouter une source » → a small form: kind (auto-detected from the target: ends with `.pdf` → pdf, else obsidian), target with autocompletion over vault notes (`/api/vault/notes?q=`), pages, note. Saving writes to `sources.json` on the **selected deck** (not the ancestor), so adding a source to a sub-deck stops inheritance for that sub-deck — the form says so when the current corpus is inherited, with a « copier les sources héritées d'abord » checkbox (on by default).
- Each source row has « retirer ». On an inherited corpus, « retirer » writes the remaining entries onto the selected deck (same rule as adding: the corpus is materialised and inheritance stops). Removing the last own entry deletes the deck entry and inheritance resumes.

## API

| Method & path | Body | Returns |
|---|---|---|
| `GET /api/sources` | — | the whole `decks` mapping, lists only |
| `GET /api/sources/corpus?deck=` | — | `{ deck, inherited_from: str \| null, sources: SourceView[] }` |
| `PUT /api/sources?deck=` | `SourceEntry[]` (validated; empty list = delete entry) | `{ deck, sources }` |
| `GET /api/vault/notes?q=` | — | `string[]`, up to 50 note names containing `q` case-insensitively, `.md` stripped, sorted |

`SourceView`:

```json
{ "kind": "pdf", "target": "~/…/lbdl.pdf", "pages": "41-52", "note": "", "on_deck": "courant::01-AI::little book of deep learning",
  "exists": true, "uri": "file:///…", "text": "…", "truncated": false, "n_pages": 12, "warning": "" }
```

The deck goes in the query string, not the path: deck names contain `::` and spaces.

`text` in `corpus` is the full extracted text (already capped by `max_chars`); the frontend does its own 4 000-char fold. `chat.py` calls `SourceStore.corpus(deck)` + `Source.text()` directly, not the HTTP API.

## Module `sources.py`

Keeps the existing `Vault`, `Source`, `SourceStore` names. Changes from 0.1: `Source.pages`, `Source.text()`, list-valued deck entries with legacy single-object read, `SourceStore.corpus()` replacing single-valued `get()` (keep `get()` returning the first source of the corpus for the CLI), `SourceStore.set_corpus(deck, entries)`, and the `vault_notes(q)` helper. The CLI `anki source …` keeps working on the first entry.
