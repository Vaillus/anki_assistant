"""A deck's corpus: the documents it was made from. Spec: specs/sources.md.

The mapping lives in a JSON file (default: `sources.json` next to the project, overridable with
the ANKI_SOURCES env var):

    {
      "vault": {"name": "Vault", "path": "/Users/me/Documents/Vault"},
      "decks": {
        "courant::00-Thèse": [
          {"id": "k7q2vd", "kind": "obsidian", "target": "Allocation sur des angles disjoints"},
          {"id": "m3x8pa", "kind": "pdf", "target": "~/stone_search.pdf", "pages": "12-19",
           "note": "chap. 2"}
        ],
        "courant::01-AI::little book of deep learning": [
          {"id": "r9wt4n", "kind": "pdf", "target": "~/lbdl.pdf"},
          {"id": "w5hc2e", "kind": "web", "target": "https://fleuret.org/francois/lbdl.html"}
        ]
      },
      "anchors": {
        "1739276778640": ["k7q2vd"]
      }
    }

A deck's value is an ordered list of sources: its **corpus**. A deck without a corpus of its own
inherits its nearest parent deck's corpus, so mapping `courant::01-AI::little book of deep
learning` also covers its `::1` .. `::6` sub-decks. For backward compatibility with the 0.1
format, a deck value that is a single object (not a list) is read as a one-element list and
rewritten as a list on next save; an entry without `id` gets one on load.

`anchors` maps an Anki note id (JSON key, so a string) to the ids of the sources the note was
made from. Nothing is written into Anki: only this file knows a note is anchored.

A `web` source is a pointer, not a snapshot: nothing of the page is stored, its text is fetched
when needed (`_fetch_web_cached`) and reduced to plain text by `html_to_text`.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.parse
from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

import httpx
import pypdf

DEFAULT_VAULT = Path("~/Documents/Vault").expanduser()
Kind = Literal["pdf", "obsidian", "web"]
KINDS: tuple[Kind, ...] = ("pdf", "obsidian", "web")

DEFAULT_MAX_CHARS = 60_000

#: Seconds allowed for fetching a web source.
WEB_TIMEOUT = 15.0
#: A failed fetch is remembered this long before it is tried again, so that an offline session
#: does not wait for a timeout on every corpus load.
WEB_RETRY_SECONDS = 60.0
#: Some sites refuse the default `python-httpx/x.y` agent with a 403; a browser-like one is fine.
WEB_USER_AGENT = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) anki-assistant"

_PAGES_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # lowercase base32


def new_source_id() -> str:
    """A fresh source id: 6 lowercase base32 characters, path-safe, never reused once stored."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))


# In-memory cache of PDF text extraction, keyed by (path, mtime, pages). PDFs are slow to parse
# and the same corpus is requested on every note under review.
_PDF_TEXT_CACHE: dict[tuple[str, float, str], tuple[str, int]] = {}


def default_store_path() -> Path:
    env = os.environ.get("ANKI_SOURCES")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "sources.json"


def is_url(target: str) -> bool:
    """True for an absolute http(s) URL without whitespace."""
    return bool(_URL_RE.match(target.strip()))


def detect_kind(target: str) -> Kind:
    """An http(s) URL is a web page, a .pdf target a PDF; anything else an Obsidian note."""
    if is_url(target):
        return "web"
    return "pdf" if target.strip().lower().endswith(".pdf") else "obsidian"


def is_valid_pages(pages: str) -> bool:
    """True for '', '12-19', '7', '3-5,9'; false for anything else."""
    return not pages or bool(_PAGES_RE.match(pages))


def _page_numbers(pages: str, n_pages: int) -> list[int]:
    """Expand '3-5,9' into [3, 4, 5, 9], deduplicated, clamped to 1..n_pages, order preserved."""
    seen: dict[int, None] = {}
    for part in pages.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(part)
        for p in range(start, end + 1):
            if 1 <= p <= n_pages:
                seen.setdefault(p, None)
    return list(seen)


def _read_pdf_cached(path: Path, pages: str) -> tuple[str, int]:
    """(text, n_pages) for the selected pages of `path`, cached on (path, mtime, pages)."""
    mtime = path.stat().st_mtime
    key = (str(path), mtime, pages)
    cached = _PDF_TEXT_CACHE.get(key)
    if cached is not None:
        return cached
    reader = pypdf.PdfReader(str(path))
    n_pages = len(reader.pages)
    page_numbers = _page_numbers(pages, n_pages) if pages else list(range(1, n_pages + 1))
    blocks = [
        f"--- page {p} ---\n\n{reader.pages[p - 1].extract_text() or ''}" for p in page_numbers
    ]
    result = ("\n\n".join(blocks), n_pages)
    _PDF_TEXT_CACHE[key] = result
    return result


_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*\n?", re.DOTALL)


def _strip_front_matter(text: str) -> str:
    return _FRONT_MATTER_RE.sub("", text, count=1)


# ------------------------------------------------------------------- web pages

#: Elements whose content is not page text: code, styling, and the site's chrome.
_SKIPPED_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "head", "nav", "footer", "aside"}
)
#: When a page marks its content with one of these, only that content is kept.
_MAIN_TAGS = frozenset({"main", "article"})
#: Elements that start a new line (block-level, roughly).
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "header",
        "hr",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "thead",
        "tr",
        "ul",
    }
)
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class _TextExtractor(HTMLParser):
    """Reduce an HTML document to readable plain text: block structure kept as line breaks,
    headings as `#` marks, list items as `- `, everything else folded. Site chrome
    (`nav`, `footer`, `aside`) is dropped, and when the page wraps its content in `<main>` or
    `<article>` only that part is kept."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._chunks: list[str] = []
        #: The chunks that fell inside a `<main>` / `<article>`; preferred when non-empty.
        self._main_chunks: list[str] = []
        self._skip_depth = 0
        self._main_depth = 0
        self._in_title = False
        self._in_pre = 0

    def _emit(self, chunk: str) -> None:
        self._chunks.append(chunk)
        if self._main_depth:
            self._main_chunks.append(chunk)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _MAIN_TAGS:
            self._main_depth += 1
        if tag in _HEADING_TAGS:
            self._emit("\n\n" + "#" * _HEADING_TAGS[tag] + " ")
        elif tag == "li":
            self._emit("\n- ")
        elif tag in ("td", "th"):
            self._emit("\t")
        elif tag in _BLOCK_TAGS:
            self._emit("\n")
            if tag == "pre":
                self._in_pre += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in _SKIPPED_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in _MAIN_TAGS and self._main_depth:
            self._main_depth -= 1
        # A closing `li` adds nothing: the next item or the list's end breaks the line.
        if tag in _HEADING_TAGS or tag in _BLOCK_TAGS:
            self._emit("\n")
            if tag == "pre" and self._in_pre:
                self._in_pre -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        self._emit(data if self._in_pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        chunks = self._main_chunks if "".join(self._main_chunks).strip() else self._chunks
        raw = "".join(chunks)
        lines = [line.strip() for line in raw.split("\n")]
        folded = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        title = " ".join(self.title.split())
        return f"{title}\n\n{folded}" if title else folded


def html_to_text(html: str) -> str:
    """Plain text of an HTML page, the `<title>` as its first line (specs/sources.md)."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def _fetch_web(url: str) -> tuple[str, str]:
    """`(text, warning)` of a page, uncached. Never raises: a failure is the warning."""
    try:
        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=WEB_TIMEOUT,
            headers={"User-Agent": WEB_USER_AGENT},
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return "", f"page inaccessible : HTTP {exc.response.status_code}"
    except httpx.HTTPError as exc:
        return "", f"page inaccessible : {exc.__class__.__name__}: {exc}"
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type in ("text/html", "application/xhtml+xml", ""):
        return html_to_text(response.text), ""
    if content_type.startswith("text/"):
        return response.text, ""
    return "", f"contenu non textuel ({content_type})"


#: url -> (fetched_at, text, warning). A success lives for the process; a failure is retried
#: after WEB_RETRY_SECONDS.
_WEB_TEXT_CACHE: dict[str, tuple[float, str, str]] = {}


def _fetch_web_cached(url: str) -> tuple[str, str]:
    cached = _WEB_TEXT_CACHE.get(url)
    now = time.monotonic()
    if cached is not None:
        fetched_at, text, warning = cached
        if not warning or now - fetched_at < WEB_RETRY_SECONDS:
            return text, warning
    text, warning = _fetch_web(url)
    _WEB_TEXT_CACHE[url] = (now, text, warning)
    return text, warning


@dataclass
class Vault:
    name: str = "Vault"
    path: Path = DEFAULT_VAULT

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Vault:
        path = Path(raw.get("path", str(DEFAULT_VAULT))).expanduser()
        return cls(name=raw.get("name") or path.name, path=path)

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "path": str(self.path)}


@dataclass
class SourceText:
    text: str
    truncated: bool
    n_pages: int | None
    warning: str = ""


@dataclass
class Source:
    """A source of a deck's corpus. `deck` is the deck the entry was written on.

    `id` is the identity of the source: anchors point to it, so editing `target` (a renamed
    vault note, a moved PDF) keeps them intact. Generated on creation, never changed.
    """

    deck: str
    kind: Kind
    target: str
    pages: str = ""
    note: str = ""
    id: str = ""

    @classmethod
    def from_dict(cls, deck: str, raw: dict[str, Any]) -> Source:
        target = str(raw.get("target") or "").strip()
        if not target:
            raise ValueError(f"{deck}: target must not be blank")
        kind = raw.get("kind") or detect_kind(target)
        if kind not in KINDS:
            raise ValueError(f"{deck}: kind must be one of {', '.join(KINDS)}, got {kind!r}")
        if kind == "web" and not is_url(target):
            raise ValueError(f"{deck}: a web target must be an http(s) URL, got {target!r}")
        pages = raw.get("pages") or ""
        if not is_valid_pages(pages):
            raise ValueError(f"{deck}: invalid pages format {pages!r}")
        if pages and kind != "pdf":
            raise ValueError(f"{deck}: pages only apply to a pdf source")
        return cls(
            deck=deck,
            kind=kind,
            target=target,
            pages=pages,
            note=raw.get("note", ""),
            id=str(raw.get("id") or "") or new_source_id(),
        )

    def to_dict(self) -> dict[str, str]:
        out: dict[str, str] = {"id": self.id, "kind": self.kind, "target": self.target}
        if self.pages:
            out["pages"] = self.pages
        if self.note:
            out["note"] = self.note
        return out

    # ------------------------------------------------------------ resolution

    def pdf_path(self) -> Path | None:
        return Path(self.target).expanduser() if self.kind == "pdf" else None

    def note_path(self, vault: Vault) -> Path | None:
        """Filesystem path of the Obsidian note, whether or not it exists."""
        if self.kind != "obsidian":
            return None
        rel = self.target if self.target.endswith(".md") else f"{self.target}.md"
        return vault.path / rel

    def uri(self, vault: Vault) -> str:
        """A URI macOS can open: file:// for a PDF, obsidian:// for a note, the URL for a page."""
        if self.kind == "web":
            return self.target
        if self.kind == "pdf":
            return Path(self.target).expanduser().resolve().as_uri()
        file_arg = self.target[:-3] if self.target.endswith(".md") else self.target
        # Percent-encoding, not form encoding: Obsidian reads a space as %20, never as "+".
        vault_q = urllib.parse.quote(vault.name, safe="")
        file_q = urllib.parse.quote(file_arg, safe="/")
        return f"obsidian://open?vault={vault_q}&file={file_q}"

    def exists(self, vault: Vault) -> bool:
        """Whether the target is there. A URL is not checked (that would be a request on every
        deck listing); a page that cannot be fetched says so through `text().warning`."""
        if self.kind == "web":
            return True
        path = self.pdf_path() if self.kind == "pdf" else self.note_path(vault)
        return bool(path and path.exists())

    def describe(self, vault: Vault) -> str:
        mark = "" if self.exists(vault) else "  [MISSING]"
        pages_suffix = f"  pages {self.pages}" if self.pages else ""
        return f"{self.kind}: {self.target}{pages_suffix}{mark}"

    # ------------------------------------------------------------- text

    def text(self, vault: Vault, max_chars: int = DEFAULT_MAX_CHARS) -> SourceText:
        """Extracted text of this source, truncated to `max_chars`.

        Obsidian: file content with the leading YAML front matter stripped. PDF: `pypdf` text of
        the pages in `self.pages` (or the whole document), pages joined with
        "\\n\\n--- page N ---\\n\\n"; extraction is cached in memory on (path, mtime, pages).
        Web: the page fetched and reduced to text (`html_to_text`), cached by URL; a page that
        cannot be fetched gives an empty text and the reason in `warning`.
        """
        if self.kind == "web":
            body, warning = _fetch_web_cached(self.target)
            truncated = len(body) > max_chars
            return SourceText(
                text=body[:max_chars], truncated=truncated, n_pages=None, warning=warning
            )
        if self.kind == "obsidian":
            path = self.note_path(vault)
            if path is None or not path.exists():
                return SourceText(text="", truncated=False, n_pages=None, warning="")
            raw = path.read_text(encoding="utf-8")
            body = _strip_front_matter(raw)
            truncated = len(body) > max_chars
            return SourceText(text=body[:max_chars], truncated=truncated, n_pages=None, warning="")

        path = self.pdf_path()
        if path is None or not path.exists():
            return SourceText(text="", truncated=False, n_pages=None, warning="")
        full_text, n_pages = _read_pdf_cached(path, self.pages)
        warning = ""
        if not self.pages:
            chars_str = f"{max_chars:,}".replace(",", " ")
            warning = (
                f"PDF entier ({n_pages} pages) sans plage de pages : "
                f"seules les {chars_str} premiers caractères sont passés."
            )
        truncated = len(full_text) > max_chars
        return SourceText(
            text=full_text[:max_chars], truncated=truncated, n_pages=n_pages, warning=warning
        )


class SourceStore:
    """Read/write the deck -> corpus mapping and the note -> sources anchors."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_store_path()
        self.vault = Vault()
        self.corpora: dict[str, list[Source]] = {}
        #: note id (as a string, it is a JSON key) -> source ids, in the order the user added them
        self.anchor_map: dict[str, list[str]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.vault = Vault.from_dict(raw.get("vault", {}))
        corpora: dict[str, list[Source]] = {}
        for deck, entry in raw.get("decks", {}).items():
            if isinstance(entry, list):
                corpora[deck] = [Source.from_dict(deck, item) for item in entry]
            else:
                # 0.1 format: a single object instead of a list.
                corpora[deck] = [Source.from_dict(deck, entry)]
        self.corpora = corpora
        self.anchor_map = {
            str(note_id): [str(sid) for sid in ids]
            for note_id, ids in raw.get("anchors", {}).items()
            if ids
        }

    def save(self) -> None:
        payload = {
            "vault": self.vault.to_dict(),
            "decks": {
                deck: [src.to_dict() for src in sources]
                for deck, sources in sorted(self.corpora.items())
            },
            "anchors": {
                note_id: list(ids) for note_id, ids in sorted(self.anchor_map.items()) if ids
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # -------------------------------------------------------------- accessors

    def corpus(self, deck: str) -> list[Source]:
        """The deck's own corpus if present, else the nearest parent's, else []."""
        if deck in self.corpora:
            return list(self.corpora[deck])
        parts = deck.split("::")
        for cut in range(len(parts) - 1, 0, -1):
            parent = "::".join(parts[:cut])
            if parent in self.corpora:
                return list(self.corpora[parent])
        return []

    def get(self, deck: str, inherit: bool = True) -> Source | None:
        """First source of the deck's corpus (inheritance included unless `inherit=False`).

        Kept for the CLI, which only ever shows/edits one source per deck.
        """
        corpus = self.corpus(deck) if inherit else list(self.corpora.get(deck, []))
        return corpus[0] if corpus else None

    def set_corpus(self, deck: str, entries: list[Source]) -> tuple[list[Source], int]:
        """Replace the deck's own corpus with `entries`. An empty list deletes the entry.

        Entries without an id get one. Anchors to a source id that no longer exists anywhere
        afterwards are dropped; the count of dropped anchors is returned with the new corpus.
        """
        for source in entries:
            if not source.id:
                source.id = new_source_id()
        if entries:
            self.corpora[deck] = list(entries)
        else:
            self.corpora.pop(deck, None)
        removed = self._drop_anchors_to_unknown_sources()
        self.save()
        return list(self.corpora.get(deck, [])), removed

    def set(
        self, deck: str, target: str, kind: Kind | None = None, pages: str = "", note: str = ""
    ) -> Source:
        """Replace the deck's own corpus with a single entry built from these fields.

        This does not append to the existing corpus — it is the one-source-per-deck shortcut the
        CLI uses. Use `set_corpus` to manage a multi-source corpus.
        """
        source = Source(
            deck=deck, kind=kind or detect_kind(target), target=target, pages=pages, note=note
        )
        self.set_corpus(deck, [source])
        return source

    def unset(self, deck: str) -> bool:
        """Remove the corpus written on this exact deck (not an ancestor's)."""
        removed = deck in self.corpora
        if removed:
            self.corpora.pop(deck, None)
            self.save()
        return removed

    def coverage(self, deck_names: list[str]) -> dict[str, Source | None]:
        """Every deck mapped to its effective first source (inherited included), or None."""
        return {deck: self.get(deck) for deck in deck_names}

    def unmapped(self, deck_names: list[str]) -> list[str]:
        return [deck for deck, src in self.coverage(deck_names).items() if src is None]

    def broken(self) -> list[Source]:
        """Entries (across every deck) whose target does not exist on disk."""
        return [
            src
            for sources in self.corpora.values()
            for src in sources
            if not src.exists(self.vault)
        ]

    def by_id(self, source_id: str) -> Source | None:
        """The source carrying this id, whichever deck declares it; None when unknown."""
        for sources in self.corpora.values():
            for src in sources:
                if src.id == source_id:
                    return src
        return None

    # `AbstractSet`, not `set`: inside this class `set` is the method above.
    def _known_ids(self) -> AbstractSet[str]:
        return {src.id for sources in self.corpora.values() for src in sources}

    # ---------------------------------------------------------------- anchors

    def anchors(self, note_id: int) -> list[str]:
        """Source ids the note is anchored to, in anchor order; [] when none."""
        return list(self.anchor_map.get(str(note_id), []))

    def set_anchors(self, note_id: int, source_ids: Sequence[str]) -> None:
        """Replace the note's anchors (order kept, duplicates dropped).

        KeyError on an unknown source id.
        """
        known = self._known_ids()
        cleaned: list[str] = []
        for sid in source_ids:
            if sid not in known:
                raise KeyError(f"source inconnue : {sid}")
            if sid not in cleaned:
                cleaned.append(sid)
        key = str(note_id)
        if cleaned == self.anchor_map.get(key, []):
            return
        if cleaned:
            self.anchor_map[key] = cleaned
        else:
            del self.anchor_map[key]
        self.save()

    def add_anchor(self, note_id: int, source_id: str) -> None:
        """Append one anchor; no-op if already present. KeyError on an unknown id."""
        current = self.anchors(note_id)
        if source_id in current:
            return
        self.set_anchors(note_id, [*current, source_id])

    def anchors_to(self, source_id: str) -> list[int]:
        """Ids of the notes anchored to this source."""
        return [int(note_id) for note_id, ids in self.anchor_map.items() if source_id in ids]

    def anchored_note_ids(self) -> list[int]:
        return [int(note_id) for note_id in self.anchor_map]

    def remove_anchors(self, note_ids: Iterable[int]) -> int:
        """Drop every anchor of these notes (deleted or orphan notes). Returns how many notes."""
        removed = 0
        for note_id in note_ids:
            if self.anchor_map.pop(str(note_id), None) is not None:
                removed += 1
        if removed:
            self.save()
        return removed

    def _drop_anchors_to_unknown_sources(self) -> int:
        """Remove anchors whose source id is declared on no deck any more. Returns the count."""
        known = self._known_ids()
        removed = 0
        for note_id in list(self.anchor_map):
            kept = [sid for sid in self.anchor_map[note_id] if sid in known]
            removed += len(self.anchor_map[note_id]) - len(kept)
            if kept:
                self.anchor_map[note_id] = kept
            else:
                del self.anchor_map[note_id]
        return removed

    # ------------------------------------------------------------- appending

    def add_source(self, deck: str, source: Source) -> Source:
        """Append `source` to the deck's own corpus and save.

        An inherited corpus is materialised on `deck` first — ids kept, so anchors survive —
        which is the rule of the Source tab's form; this is what `POST /api/sources` and
        `create_note` share. The entry is validated as on load (kind, url, pages); a missing id
        gets one. Returns the stored entry, written on `deck`.
        """
        entry = Source.from_dict(deck, source.to_dict())
        own = self.corpora.get(deck)
        if own is None:
            own = [replace(inherited, deck=deck) for inherited in self.corpus(deck)]
        self.set_corpus(deck, [*own, entry])
        return entry

    # ---------------------------------------------------------- vault writes

    def create_note(
        self, deck: str, name: str, content: str, source_id: str | None = None
    ) -> Source:
        """Write `<vault>/<name>.md` and add it to the deck's own corpus.

        Refuses if the file exists (FileExistsError). `name` is vault-relative, `/` allowed
        (parent directories are created), `.md` optional. The corpus is appended to through
        `add_source` (an inherited corpus is materialised first). `source_id` lets a caller who
        announced the id beforehand (the chat's create-source proposal) keep it.
        """
        clean = name.strip().strip("/")
        if not clean:
            raise ValueError("nom de note vide")
        target = clean[:-3] if clean.endswith(".md") else clean
        path = self.vault.path / f"{target}.md"
        if path.exists():
            raise FileExistsError(f"{target}.md existe déjà dans le vault")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self.add_source(
            deck, Source(deck=deck, kind="obsidian", target=target, id=source_id or "")
        )

    def replace_in_note(self, source_id: str, old: str, new: str) -> None:
        """Replace `old` with `new` in an obsidian source's file; `old` must occur exactly once.

        KeyError for an unknown id; ValueError for a pdf or web source, or when `old` occurs 0
        or 2+ times (the message says which). The bounded, reviewable replacement is the whole
        safety story of writing into the vault — and what makes undoing it a swap of `old` and
        `new`.
        """
        source = self.by_id(source_id)
        if source is None:
            raise KeyError(f"source inconnue : {source_id}")
        if source.kind != "obsidian":
            raise ValueError("seule une note Obsidian peut être modifiée")
        if not old:
            raise ValueError("passage à remplacer vide")
        path = source.note_path(self.vault)
        if path is None or not path.exists():
            raise ValueError("fichier introuvable")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise ValueError("passage introuvable")
        if count > 1:
            raise ValueError(f"passage ambigu ({count} occurrences)")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")


def vault_notes(vault: Vault, q: str = "", limit: int = 50) -> list[str]:
    """Note names (relative to the vault root, ".md" stripped) containing `q`, case-insensitive.

    Recursive, sorted, hidden directories (`.obsidian`, `.trash`, …) skipped.
    """
    root = vault.path
    if not root.exists():
        return []
    q_lower = q.lower()
    names = []
    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        name = rel.with_suffix("").as_posix()
        if q_lower in name.lower():
            names.append(name)
    names.sort(key=str.lower)
    return names[:limit]
