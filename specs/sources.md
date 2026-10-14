# Sources

> A deck's corpus: the documents it was made from, how they are stored and resolved, what text the app extracts from them, which notes are anchored to them, and the two ways the app writes back into the vault.

## Corpus

A deck's **corpus** is an ordered list of **sources**. A source is one document the deck was made from: a **vault note** — a Markdown file in the user's Obsidian **vault** — a **PDF** on disk, optionally restricted to a page range, or a **web page** at a URL. "Note" on its own keeps its meaning from [notes.md](./notes.md): an Anki note.

Every source has an **id**: six path-safe characters, generated when the entry is created, never changed. [Anchors](#anchors) point to it, so renaming a vault note or moving a PDF keeps them intact.

A source can be **shared**: listed in the corpus of several decks and stored once. What it points to, its page range and its annotation are the same from every deck that lists it, so editing it from one deck changes it for all of them. Removing a shared source from one deck takes it off that deck's corpus only; the source itself goes once no deck lists it.

Corpora live in `sources.json` at the project root (override with `ANKI_SOURCES`). Nothing about sources is stored in Anki. The file holds the vault location, each source once under its id (`sources`), each deck's corpus as the ordered ids of its sources keyed by deck name (`decks`), and the [anchors](#anchors). A file written before sources were shared, with whole entries under each deck, is read as such: an id met again on another deck lists the source already read. When the two entries differ (target, kind, page range or annotation), the first one read is kept and a warning in the server log names the id and both decks.

A source entry has:

- `id` — the source's identity (see above).
- `kind` — `obsidian`, `pdf` or `web`. Auto-detected from the target when not given: an `http(s)` URL is `web`, a target ending in `.pdf` is `pdf`, anything else is `obsidian`.
- `target` — for a vault note, its name relative to the vault root, with or without `.md`; for a PDF, a filesystem path, `~` allowed; for a web page, an absolute `http(s)` URL.
- `pages` — PDF only (refused on another kind), optional: a page range such as `12-19`, `7` or `3-5,9`, 1-based and inclusive. Absent means the whole document.
- `note` — optional free text shown next to the source, called its **annotation** below.

The [Source tab](#source-tab) shows the corpus; the chat makes it available to Claude ([chat.md § What Claude sees](./chat.md#what-claude-sees)).

### Inheritance

A deck's **effective corpus** is its own sources followed by each ancestor's, nearest first: `a::b::c`'s own, then `a::b`'s, then `a`'s. A deck with no own sources sees only the inherited ones. Each source remembers the deck it was written on, so the UI can distinguish own from inherited.

A group turned into a [label](./review.md#labels) never holds a corpus: its sources become shared with each of its sub-decks, listed after each one's own sources, then taken off the group. No source is copied and every id stays, so every anchor stays valid. Every deck under it sees the same sources as before, and editing one of them from a sub-deck's row changes it for every deck that lists it.

### Looking a source up

A source is reached by a deck and its id: opening it, reading its text for the chat, or replacing a passage of it goes through the effective corpus of the deck in use. A deck whose effective corpus does not list the id does not reach the source, even when another deck shares it.

### Text

A vault note resolves to `<vault>/<target>.md` and opens in Obsidian through an `obsidian://` link; a PDF resolves to its path and opens in Zotero's reader through a `zotero://open-pdf` URI looked up from the Zotero SQLite database (`POST /api/sources/{id}/open`); when the lookup fails, falls back to the system default app; a web page opens in a new tab at its URL. A source whose file does not exist is **missing**: still listed, marked as such, with no text.

A web source is a **pointer, not a snapshot**: nothing of the page is stored, the text is fetched when needed. A page whose content the user wants to keep as it stands is a vault note (via `propose_create_source` in [chat.md § Source proposals](./chat.md#source-proposals)), not a web source.

Each source yields **extracted text** when read by Claude:

- A **vault note** yields its Markdown as written, without the YAML front matter block at the top.
- A **web page** is fetched and reduced to its main text content. A page that cannot be fetched yields an empty text with a warning.
- A **PDF** exposes three representations described [below](#pdf-representations).

Extracted text is capped at 60 000 characters; the source says whether it was **truncated**. The Source tab does not display extracted text — each kind opens in its native viewer or browser.

#### PDF representations

A PDF source exposes three representations of the document, from lightest to heaviest:

- **Structural index** — an ordered list of (page number, heading) entries covering the document's contents, derived from the PDF's bookmark outline or, when no bookmarks exist, from a heuristic scan of page headings. Always available without extraction cost.
- **Extracted text** — page-level Markdown for a requested set of pages, preserving tables, equations, and formatting. Produced by Docling; when Docling is not installed, pypdf is the fallback (plain text only). Cached in a **sidecar file** next to the PDF (`<name>.pdf.md`) so that subsequent reads of the same pages return instantly.
- **Raw file** — the PDF itself, opened in Zotero or the default viewer for human reading. No text is extracted.

Which representation a consumer gets depends on whether a page range is present:

- **No page range** — the source returns its structural index. The chat's corpus index includes it inline ([chat.md § What Claude sees](./chat.md#what-claude-sees)) so that Claude can identify relevant pages; the Source tab shows the page count and a warning to set a range. No text is extracted.
- **Page range given** — on the source entry's `pages` field or as a `pages` override on `read_source` ([chat.md § Read tools](./chat.md#read-tools)) — the source returns extracted text for those pages. Claude uses the structural index to choose which pages to request.

The sidecar file is created lazily on first extraction and grows incrementally: each newly requested page is extracted and appended, pages already cached are reused. The sidecar is invalidated when the PDF's modification time changes.

## Anchors

An **anchor** links an Anki note to a source of its deck's corpus that the note was made from. A note can be anchored to several sources, in the order the user added them, and a source to any number of notes. Anchors are stored in `sources.json` (note id → list of source ids); the note in Anki carries nothing.

Each anchor is qualified against the note's current deck:

- **valid** — the source is in the effective corpus of the note's deck;
- **dangling** — the source exists but is not in that corpus (the note was moved, or the corpus rewritten);
- **orphan** — the note itself no longer exists in Anki. Telling orphan from valid needs Anki, so it is computed on request and never stored.

Anchors follow the note through the writes of the workspace ([workspace.md § Validation](./workspace.md#validation)):

- **Note deleted** → its anchors become orphan. They are not removed eagerly (a delete must not fail on a `sources.json` write); « clean up » in the Source tab removes them.
- **Source removed from a corpus** → once no deck lists it any more, every anchor to it is removed. The UI warns first: « 3 notes are anchored to this source ». A shared source removed from one deck only keeps its anchors, each qualified against its note's deck as above.
- **Note moved to another deck** → each anchor is kept if its source is in the destination's effective corpus, removed otherwise.
- **Split** → the kept original and the new fragments inherit the original's anchors.
- **Create** → the new note gets the anchors chosen in the workspace, by default those of the note the workspace was opened on ([workspace.md § Opening and closing](./workspace.md#opening-and-closing)).

## Writing to the vault

The vault is the user's own notes. The app writes into it in exactly two ways, on vault notes only, and only behind a user click — a form, or « Apply » on a chat proposal ([chat.md § Proposal tools](./chat.md#proposal-tools)). Claude never writes on its own.

- **Create a vault note.** Writes `<vault>/<name>.md` with the given content as is (front matter included if the caller supplies it), creating parent folders when the name contains a `/`. Refused if the file already exists. The new file is appended to the deck's own corpus as an obsidian source. There is no default folder: the name is what the user typed or accepted.
- **Replace a passage.** Replaces one passage of a vault note with another. The passage to replace must occur **exactly once** in the file: refused as « passage not found » when it is absent, « ambiguous passage » when it is repeated. There is no whole-file rewrite. A source edit is always a bounded, reviewable replacement, and undoing it is the same replacement with the two texts swapped.

These two constraints are the whole safety story and are not to be relaxed for convenience.

## Source tab

The Source tab shows the effective corpus of the deck selected in column 1 ([review.md](./review.md)). When the corpus is inherited, a banner names the deck it comes from. Sources the selected note is anchored to come first, in anchor order, expanded; the others are collapsed to their header. A deck with no effective corpus shows « No sources for this deck. »

Each source is a row:

- **Header** — kind chip, target, page range and annotation in muted text, an « open ↗ » link, « remove ». Under it, when they apply: « inherited from … », « ⚠ file not found » for a missing source, a warning for PDFs without a page range, and « anchored to this note » in accent colour when the selected note is anchored to the source.

**Adding a source.** « + add a source » opens a form: target (a vault note with autocompletion, a PDF path with autocompletion from PDFs under the vault, or a URL), kind (auto-detected from the target, overridable), pages (PDF only, hidden otherwise); annotation. Saving adds the source to the selected deck's own list. A source can also be added from the conversation: Claude's `propose_add_source` ([chat.md § Source proposals](./chat.md#source-proposals)) is an inline card with « Apply ».

**Removing a source.** « remove » appears only on the deck's own sources, not on inherited ones. It removes the source from the deck's own list; a shared source stays listed by its other decks. Anchors to the source go only once no deck lists it ([Anchors](#anchors)); then, when notes are anchored to it, the confirmation says how many and that their anchors will be removed.

**Anchoring.** On the selected note in column 2: one chip per anchor (« ⚓ differentiability », × removes it) and « ⚓ anchor… », a menu of the effective corpus's sources the note is not yet anchored to. A dangling anchor shows as « ⚓ source outside corpus ».

**Orphans.** The tab's footer shows « n anchored notes missing · clean up » when anchored notes no longer exist in Anki; clicking removes their anchors. Hidden when there are none.

## API

All routes are under `/api`. A deck travels in the query string, never in the path (deck names contain `::` and spaces); a source id is path-safe and goes in the path. A vault write that is refused (existing file, passage absent or ambiguous) answers 409, and 400 on an invalid target; a source id the deck's effective corpus does not list is 404 ([Looking a source up](#looking-a-source-up)).

| Route | Purpose |
|---|---|
| `GET /api/sources` | Every deck's own corpus, as stored |
| `GET /api/sources/corpus?deck=&note_id=` | A deck's effective corpus with source metadata and where each is inherited from; with `note_id`, the note's anchors |
| `PUT /api/sources?deck=` | Replace the corpus written on a deck (an empty list deletes it); reports how many anchors were dropped |
| `POST /api/sources?deck=` | Append one source to a deck's own corpus (an inherited corpus is listed on the deck first, shared, not copied); auto-detects kind |
| `GET /api/vault/notes?q=` | Vault note names containing `q`, for the form's autocompletion |
| `GET /api/vault/pdfs?q=` | PDF paths under the vault whose filename contains `q`, for the form's autocompletion |
| `GET /api/sources/anchors?note_id=` | A note's anchors, each qualified valid or dangling |
| `PUT /api/sources/anchors?note_id=` | Replace a note's anchors, order kept |
| `GET /api/sources/anchors/orphans` | Anchored note ids that Anki no longer knows |
| `POST /api/sources/anchors/prune` | Remove the orphans' anchors |
| `POST /api/sources/notes` | Create a vault note and add it to a deck's corpus; may anchor notes to it and reuse an id announced beforehand ([chat.md § Proposal tools](./chat.md#proposal-tools)) |
| `PATCH /api/sources/{source_id}/text?deck=` | Replace one passage of a vault note of the deck's effective corpus |
| `POST /api/sources/{source_id}/open?deck=` | Open a PDF source of the deck's effective corpus in Zotero via `zotero://open-pdf` URI (falls back to default app) |
