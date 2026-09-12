# Sources

> A deck's corpus: the documents it was made from, how they are stored, resolved, displayed, turned into text for Claude, **anchored** to individual notes, and written back to the vault.

## Purpose

When reviewing a flagged note the user wants the original material within reach, and Claude needs it in context to verify or rewrite. A deck therefore points to a **corpus**: an ordered list of sources — Obsidian notes, PDFs, and **web pages**, since the material a deck was made from is as often a Wikipedia article or a set of online lecture notes as a file on disk.

The corpus is not closed: a source is added from the Source tab's form, or proposed by Claude in the conversation once it has found a page worth keeping ([chat.md § Proposal tools](./chat.md#proposal-tools)).

A corpus can be large (ten notes for a big deck). A single note usually comes from one or two of them. **Anchors** record which ones, so that the Source tab opens on the right documents and the chat can load those alone instead of the whole corpus (see [chat.md](./chat.md#context)).

## Data model

`sources.json` at the project root (override with `ANKI_SOURCES`):

```json
{
  "vault": { "name": "Vault", "path": "/Users/hugovaillaud/Documents/Vault" },
  "decks": {
    "courant::00-Thèse": [
      { "id": "k7q2vd", "kind": "obsidian", "target": "Allocation sur des angles disjoints" },
      { "id": "m3x8pa", "kind": "pdf", "target": "~/Documents/these/stone_search.pdf", "pages": "12-19", "note": "chap. 2" }
    ],
    "courant::04-maths::dérivés": [
      { "id": "r9wt4n", "kind": "obsidian", "target": "maths/différentiabilité" },
      { "id": "w5hc2e", "kind": "web", "target": "https://en.wikipedia.org/wiki/Differentiable_function" }
    ]
  },
  "anchors": {
    "1739276778640": ["r9wt4n"],
    "1735382601644": ["r9wt4n", "k7q2vd"]
  }
}
```

### Source entry

- `id` — 6 lowercase base32 characters, generated when the entry is created, never changed. **The id is the identity of a source**: anchors point to it, and renaming a vault note or moving a PDF (editing `target`) keeps the anchors intact.
- `kind` — `"obsidian"`, `"pdf"` or `"web"`. Auto-detected from the target when not given: an `http://` or `https://` URL is `web`, a target ending in `.pdf` is `pdf`, anything else is `obsidian`.
- `target` — note name relative to the vault root, with or without `.md` (obsidian), a filesystem path, `~` allowed (pdf), or an absolute `http(s)` URL (web — anything else is refused).
- `pages` — optional, pdf only (refused on another kind). `"12-19"`, `"7"`, `"3-5,9"`. 1-based, inclusive. Absent = whole document.
- `note` — optional free text shown next to the source.
- **Backward compatibility:** a deck value that is a single object (the 0.1 format) is read as a one-element list; an entry without `id` gets one on load. Both are rewritten on next save.

### Anchors

`anchors` maps an Anki **note id** (as a string, JSON keys) to a **list of source ids**, in the order the user added them. Any number of sources per note, any number of notes per source, no duplicates within a list. An empty list is removed on save. Nothing is written into Anki: the note does not know it is anchored, only the repo does.

Each anchor is qualified separately. It is **valid** when its source is in the effective corpus of the note's deck (own or inherited), **dangling** when the source id exists but is not in that corpus (the note was moved, or the deck's corpus was rewritten). A note's whole entry is **orphan** when the note no longer exists in Anki. `SourceStore` cannot tell orphan from valid on its own — that needs Anki — so it exposes the raw mapping and lets `review.py` / the routes qualify it.

Lifecycle rules (enforced by `workspace.apply` at validation, see [workspace.md](./workspace.md#validation), and by the review routes when called directly):

- **Note deleted** → its anchors become orphan. Not removed eagerly (the delete path should not fail on a `sources.json` write); cleaned by « nettoyer » below.
- **Source removed from a corpus** → that source id is removed from every note's list. The UI warns first: « 3 notes sont ancrées à cette source ».
- **Note moved to another deck** → each anchor is kept if its source is in the destination's effective corpus, removed otherwise.
- **Split** → the fragments (kept original and new notes) inherit the original's anchors.
- **Create** (sibling) → the new note gets the anchors chosen in the dialog, default: the original's.

### Inheritance

`SourceStore.corpus(deck) -> list[Source]`: the deck's own list if present, else the nearest ancestor's (`a::b::c` → `a::b` → `a`), else `[]`. `Source.deck` records which deck the entry was written on so the UI can say « héritée de courant::01-AI ».

### Kinds

| kind | exists? | `uri()` | text |
|---|---|---|---|
| obsidian | `<vault>/<target>.md` is a file | `obsidian://open?vault=<name>&file=<target>` (percent-encoded, `%20` for spaces, never `+`) | file content |
| pdf | path is a file | `file://` URI | `pypdf` text of the selected pages, pages joined with `\n\n--- page N ---\n\n` |
| web | always true — a URL is not checked without a request; a page that cannot be fetched reports it through `SourceText.warning` instead | the URL itself | the page fetched by the server and reduced to text (below) |

A web source is a **pointer, not a snapshot**: nothing of the page is stored, the text is fetched when it is needed and cached in memory. A page whose content the user wants to keep as it is today is a `propose_create_source` (a note in the vault), not a web source.

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
- Web: `GET` the URL with `httpx` (redirects followed, `WEB_TIMEOUT` = 15 s, a browser-like `User-Agent` since some sites refuse the default one). An HTML body goes through `html_to_text`: `script`, `style`, `noscript`, `svg`, the head and the site chrome (`nav`, `footer`, `aside`) are dropped; when the page wraps its content in `<main>` or `<article>`, only that part is kept; block elements become line breaks, headings keep their level as `#` marks, list items get a `- `, whitespace is folded. The page `<title>` is the first line. No readability heuristics beyond that: a page that hides its content behind scripts comes back thin, and the Source tab shows what Claude would get. A `text/plain` body is kept as is; any other content type gives an empty text and the warning « contenu non textuel (`<content-type>`) ». A failed request (network error, HTTP status ≥ 400) gives an empty text and the warning « page inaccessible : … ». The text of a fetched page is cached in memory by URL for the life of the server; a failure is cached for `WEB_RETRY_SECONDS` = 60 s so that an offline session does not wait for a timeout on every corpus load.
- Truncate to `max_chars` with `truncated=True`. A whole PDF without `pages` gets the warning above so the user learns to set a range.

## Writing to the vault

The app writes into the vault in exactly two ways, both **obsidian** kind only, both triggered by a user click (a form, or « Appliquer » on a chat proposal — never by Claude alone):

- **Create a note.** `SourceStore.create_note(deck, name, content) -> Source`: writes `<vault>/<name>.md` with `content` as is (the caller may include front matter), **refuses if the file already exists** (409), creates the parent directories if `name` has a `/`, then appends an obsidian entry to the deck's own corpus (materialising an inherited corpus first, same rule as adding a source from the form) and returns it. No default folder: `name` is whatever the user typed or accepted in the proposal.
- **Edit a note.** `SourceStore.replace_in_note(source_id, old, new) -> None`: reads the file, requires `old` to occur **exactly once** (0 → 409 « passage introuvable », 2+ → 409 « passage ambigu »), writes the file back with the single replacement. No whole-file rewrite: a source edit is always a bounded, reviewable replacement, and it is what makes the chat's undo of a source edit trivial (swap `old` and `new`).

The vault is the user's own notes; these two constraints are the whole safety story and are not to be relaxed for convenience.

## UI — Source tab (column 3)

- Header per source: kind chip (`obsidian` violet, `pdf` red, `web` blue), target, `pages`/`note` in muted text, « héritée de … » when inherited, ⚠ when the file is missing, an « ouvrir ↗ » link to `uri` (a new tab for a web source), and « ancrée à cette note » in accent colour on each source the selected note is anchored to. **Anchored sources are listed first, in anchor order, and expanded; the others are collapsed** to their header.
- Below: the extracted text in a scrollable monospace block (first 4 000 chars, « afficher plus » expands to the full `text`).
- Anchor control: on the selected note in column 2, one chip per anchor « ⚓ différentiabilité » (× removes it) and a « ⚓ ancrer… » chip opening a menu of the effective corpus sources not yet anchored. A dangling anchor shows as « ⚓ source hors corpus ».
- Footer: « + ajouter une source » → a small form: target (a vault note with autocompletion over `/api/vault/notes?q=`, a PDF path, or a URL), kind (auto-detected from the target as in [Source entry](#source-entry), overridable), pages (pdf only, the field is hidden otherwise), note. Saving writes to `sources.json` on the **selected deck** (not the ancestor), so adding a source to a sub-deck stops inheritance for that sub-deck — the form says so when the current corpus is inherited, with a « copier les sources héritées d'abord » checkbox (on by default).
- The conversation is the other way in: Claude's `propose_add_source` ([chat.md § Proposal tools](./chat.md#proposal-tools)) is an inline card with « Appliquer » that calls `POST /api/sources`; the source lands on the selected deck under the same materialisation rule, inherited entries always copied first.
- Each source row has « retirer ». On an inherited corpus, « retirer » writes the remaining entries onto the selected deck (same rule as adding: the corpus is materialised and inheritance stops). Removing the last own entry deletes the deck entry and inheritance resumes. When the source has anchors, the confirm says how many and that they will be removed.
- Footer, right: « n notes ancrées disparues · nettoyer » when `GET /api/sources/anchors/orphans` returns a non-empty list for the collection; hidden otherwise. Clicking calls `prune` and refreshes.

## API

| Method & path | Body | Returns |
|---|---|---|
| `GET /api/sources` | — | the whole `decks` mapping, lists only |
| `GET /api/sources/corpus?deck=&note_id=` | — | `{ deck, inherited_from: str \| null, anchored: [source_id], sources: SourceView[] }` — `anchored` lists the note's anchors in order (`[]` without `note_id`) |
| `PUT /api/sources?deck=` | `SourceEntry[]` (validated; empty list = delete entry; entries without `id` get one; anchors to ids no longer present are removed) | `{ deck, sources, removed_anchors: int }` |
| `POST /api/sources?deck=` | `{ target, kind?, pages?, note?, anchor_note_ids?, id? }` — one entry **appended** to the deck's own corpus (an inherited corpus is materialised first, ids kept); `kind` defaults to the auto-detection; `anchor_note_ids` and `id` as on `POST /api/sources/notes` | `{ deck, source: SourceView }`; 400 on an invalid target (a web target that is not an `http(s)` URL, pages on a non-pdf) |
| `GET /api/vault/notes?q=` | — | `string[]`, up to 50 note names containing `q` case-insensitively, `.md` stripped, sorted |
| `GET /api/sources/anchors?note_id=` | — | `{ note_id, anchors: [{ source_id, status: "valid" \| "dangling" }] }` |
| `PUT /api/sources/anchors?note_id=` | `{ source_ids: [str] }` (full list, order kept; `[]` clears) | same as GET; 404 if a source id is unknown |
| `GET /api/sources/anchors/orphans` | — | `{ note_ids: [...] }` — anchored note ids that Anki no longer knows (`notesInfo` returns empty) |
| `POST /api/sources/anchors/prune` | — | `{ removed: int }` |
| `POST /api/sources/notes` | `{ deck, name, content, anchor_note_ids?, id? }` | `{ deck, source: SourceView }`; 409 if the file exists. `anchor_note_ids` appends the new source to those notes' anchors in the same call; `id` lets the chat keep the id it announced to Claude ([chat.md § Proposal tools](./chat.md#proposal-tools)) |
| `PATCH /api/sources/{source_id}/text` | `{ old, new }` | `{ source: SourceView }` (text re-extracted); 409 on 0 or 2+ matches; 400 on a pdf source |

`SourceView`:

```json
{ "id": "m3x8pa", "kind": "pdf", "target": "~/…/lbdl.pdf", "pages": "41-52", "note": "", "on_deck": "courant::01-AI::little book of deep learning",
  "exists": true, "uri": "file:///…", "text": "…", "truncated": false, "n_pages": 12, "warning": "", "anchored_count": 3 }
```

The deck goes in the query string, not the path: deck names contain `::` and spaces. Source ids are path-safe, so they go in the path.

`text` in `corpus` is the full extracted text (already capped by `max_chars`); the frontend does its own 4 000-char fold. `chat.py` calls `SourceStore.corpus(deck)` + `Source.text()` directly, not the HTTP API. For a web source `exists` is always true and `uri` is the URL; a page that could not be fetched has an empty `text` and the reason in `warning`.

## Module `sources.py`

Keeps the existing `Vault`, `Source`, `SourceStore` names. Added for anchors and writing:

```python
Source.id: str                                            # generated by new_source_id() when absent
SourceStore.anchors(note_id) -> list[str]                  # raw mapping lookup, [] when none
SourceStore.set_anchors(note_id, source_ids) -> None       # KeyError if an id is unknown anywhere in decks
SourceStore.add_anchor(note_id, source_id) -> None         # no-op if already present
SourceStore.anchors_to(source_id) -> list[int]
SourceStore.anchored_note_ids() -> list[int]
SourceStore.remove_anchors(note_ids) -> int
SourceStore.by_id(source_id) -> Source | None
SourceStore.add_source(deck, source) -> Source              # append to the own corpus, materialising first
SourceStore.create_note(deck, name, content) -> Source     # FileExistsError if present
SourceStore.replace_in_note(source_id, old, new) -> None   # ValueError: 0 or 2+ matches, or not obsidian
detect_kind(target) -> Kind                                # "web" | "pdf" | "obsidian"
html_to_text(html) -> str                                  # the reduction described in Text extraction
```

`set_corpus(deck, entries)` assigns ids to new entries and drops anchors whose source id disappears from that deck's list (returns the count through the route). `add_source` is what `POST /api/sources` and `create_note` share: materialise an inherited corpus on the deck (ids kept, so anchors survive), append, save. `Source.from_dict` refuses a web target that is not an `http(s)` URL and `pages` on a non-pdf entry. The CLI `anki source …` keeps working on the first entry and gains `anki source anchor NOTE_ID SOURCE_ID…` (sets the full list); `anki source set` accepts a URL and `--kind web`.
