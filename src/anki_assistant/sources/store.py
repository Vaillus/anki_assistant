"""A deck's corpus: the documents it was made from. Spec: specs/sources.md.

The mapping lives in a JSON file (default: `sources.json` next to the project, overridable with
the ANKI_SOURCES env var):

    {
      "vault": {"name": "Vault", "path": "/Users/me/Documents/Vault"},
      "sources": {
        "k7q2vd": {"kind": "obsidian", "target": "Allocation sur des angles disjoints"},
        "m3x8pa": {"kind": "pdf", "target": "~/stone_search.pdf", "pages": "12-19",
                   "note": "chap. 2"},
        "w5hc2e": {"kind": "web", "target": "https://fleuret.org/francois/lbdl.html"}
      },
      "decks": {
        "courant::00-Thèse": ["k7q2vd", "m3x8pa"],
        "courant::01-AI::little book of deep learning": ["w5hc2e"],
        "courant::01-AI::ML for CO": ["w5hc2e"]
      },
      "anchors": {
        "1739276778640": ["k7q2vd"]
      }
    }

`sources` holds each source once, under its id; a deck's value in `decks` is the ordered ids of
its own sources: its **corpus**. Several decks can list one source, which is then **shared**:
edited from one deck, it changes for all; it goes once no deck lists it. A deck's effective
corpus is its own sources followed by each ancestor's (nearest first), so `courant::01-AI::little
book of deep learning` sees its own sources then those of `courant::01-AI`, then `courant`. A
source is looked up by deck and id (`find`), through that effective corpus.

Older files are read too: a deck value holding whole entries (before sharing), or a single
object (0.1). An id met again on another deck lists the source first read (a differing copy is
logged as a warning); an entry without `id` gets one on load. The next save writes the format above.

`anchors` maps an Anki note id (JSON key, so a string) to the ids of the sources the note was
made from. Nothing is written into Anki: only this file knows a note is anchored.

A `web` source is a pointer, not a snapshot: nothing of the page is stored, its text is fetched
when needed (`_fetch_web_cached`) and reduced to plain text by `html_to_text`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import urllib.parse
from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from .web import _fetch_web_cached

DEFAULT_VAULT = Path("~/Documents/Vault").expanduser()
Kind = Literal["pdf", "obsidian", "web"]
KINDS: tuple[Kind, ...] = ("pdf", "obsidian", "web")

DEFAULT_MAX_CHARS = 60_000
ZOTERO_DB = Path("~/Zotero/zotero.sqlite").expanduser()

log = logging.getLogger(__name__)

_PAGES_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # lowercase base32


def new_source_id() -> str:
    """A fresh source id: 6 lowercase base32 characters, path-safe, never reused once stored."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))


def default_store_path() -> Path:
    env = os.environ.get("ANKI_SOURCES")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[3] / "sources.json"


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


_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*\n?", re.DOTALL)


def _strip_front_matter(text: str) -> str:
    return _FRONT_MATTER_RE.sub("", text, count=1)


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
    extraction: str = ""


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

    def pdf_n_pages(self) -> int | None:
        path = self.pdf_path()
        if path is None or not path.exists():
            return None
        from anki_assistant.pdf_cache import get_pdf_toc

        n_pages, _ = get_pdf_toc(path)
        return n_pages

    def pdf_toc(self) -> list[tuple[int, str]]:
        path = self.pdf_path()
        if path is None or not path.exists():
            return []
        from anki_assistant.pdf_cache import get_pdf_toc

        _, toc = get_pdf_toc(path)
        return [(e.page, e.heading) for e in toc]

    def note_path(self, vault: Vault) -> Path | None:
        """Filesystem path of the Obsidian note, whether or not it exists."""
        if self.kind != "obsidian":
            return None
        rel = self.target if self.target.endswith(".md") else f"{self.target}.md"
        return vault.path / rel

    def uri(self, vault: Vault) -> str:
        """A URI the frontend can open: /api/sources/<id>/file for a PDF (served by the app,
        since browsers block file:// from http://), obsidian:// for a note, the URL for a page."""
        if self.kind == "web":
            return self.target
        if self.kind == "pdf":
            return f"/api/sources/{urllib.parse.quote(self.id, safe='')}/file"
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
        if self.pages:
            from anki_assistant.pdf_cache import get_pdf_text

            full_text, n_pages, _, extraction = get_pdf_text(path, self.pages)
            truncated = len(full_text) > max_chars
            return SourceText(
                text=full_text[:max_chars],
                truncated=truncated,
                n_pages=n_pages,
                extraction=extraction,
            )
        toc_entries = self.pdf_toc()
        n_pages = self.pdf_n_pages()
        if toc_entries:
            lines = [f"- p.{p} {h}" for p, h in toc_entries]
            outline = "Document structure:\n" + "\n".join(lines)
        else:
            outline = f"PDF of {n_pages} pages, no structure detected."
        return SourceText(
            text=outline,
            truncated=False,
            n_pages=n_pages,
            warning="Use read_source with the pages parameter to read specific pages.",
        )


class SourceStore:
    """Read/write the sources, the deck -> corpus mapping and the note -> sources anchors.

    A source is stored once, under its id (`sources`); a deck's own corpus is the ordered ids
    it lists (`lists`), so several decks can list one **shared** source (specs/sources.md).
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_store_path()
        self.vault = Vault()
        #: source id -> the source, stored once whichever decks list it
        self.sources: dict[str, Source] = {}
        #: deck name -> the ids of its own sources, in order
        self.lists: dict[str, list[str]] = {}
        #: note id (as a string, it is a JSON key) -> source ids, in the order the user added them
        self.anchor_map: dict[str, list[str]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.vault = Vault.from_dict(raw.get("vault", {}))
        sources: dict[str, Source] = {}
        for sid, entry in raw.get("sources", {}).items():
            sources[str(sid)] = Source.from_dict("", {**entry, "id": sid})
        lists: dict[str, list[str]] = {}
        for deck, value in raw.get("decks", {}).items():
            # Before sharing, a deck held whole entries; 0.1 held a single object.
            items = value if isinstance(value, list) else [value]
            ids: list[str] = []
            for item in items:
                if isinstance(item, dict):
                    src = Source.from_dict(deck, item)
                    first = sources.setdefault(src.id, src)
                    if replace(first, deck=deck) != src:
                        log.warning(
                            "source %s differs on %s from %s; the first read is kept",
                            src.id,
                            deck,
                            first.deck or "sources",
                        )
                    sid = src.id
                else:
                    sid = str(item)
                    if sid not in sources:
                        log.warning("%s: unknown source id %s dropped", deck, sid)
                        continue
                if sid not in ids:
                    ids.append(sid)
            if ids:
                lists[deck] = ids
        self.sources = sources
        self.lists = lists
        self.anchor_map = {
            str(note_id): [str(sid) for sid in ids]
            for note_id, ids in raw.get("anchors", {}).items()
            if ids
        }

    def save(self) -> None:
        payload = {
            "vault": self.vault.to_dict(),
            "sources": {
                sid: {k: v for k, v in src.to_dict().items() if k != "id"}
                for sid, src in sorted(self.sources.items())
            },
            "decks": {deck: list(ids) for deck, ids in sorted(self.lists.items())},
            "anchors": {
                note_id: list(ids) for note_id, ids in sorted(self.anchor_map.items()) if ids
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # -------------------------------------------------------------- accessors

    def _listed(self, deck: str) -> list[Source]:
        """The deck's own sources, each as listed on `deck` (`Source.deck`)."""
        return [replace(self.sources[sid], deck=deck) for sid in self.lists.get(deck, [])]

    @property
    def corpora(self) -> dict[str, list[Source]]:
        """Every deck's own corpus, as listed. A view: write through `set_corpus`."""
        return {deck: self._listed(deck) for deck in self.lists}

    @corpora.setter
    def corpora(self, value: dict[str, list[Source]]) -> None:
        """Replace every corpus at once; an id met again lists the source first given."""
        self.sources, self.lists = {}, {}
        for deck, entries in value.items():
            ids: list[str] = []
            for src in entries:
                if not src.id:
                    src = replace(src, id=new_source_id())
                self.sources.setdefault(src.id, replace(src, deck=deck))
                if src.id not in ids:
                    ids.append(src.id)
            if ids:
                self.lists[deck] = ids

    def corpus(self, deck: str) -> list[Source]:
        """The deck's own sources, then each ancestor's (nearest first)."""
        result: list[Source] = []
        seen_ids: set[str] = set()
        parts = deck.split("::")
        for cut in range(len(parts), 0, -1):
            ancestor = "::".join(parts[:cut])
            for src in self._listed(ancestor):
                if src.id not in seen_ids:
                    result.append(src)
                    seen_ids.add(src.id)
        return result

    def find(self, deck: str, source_id: str) -> Source | None:
        """The source `source_id` as the deck reaches it, through its effective corpus; None when
        that corpus does not list it, even if another deck does (specs/sources.md)."""
        return next((src for src in self.corpus(deck) if src.id == source_id), None)

    def get(self, deck: str, inherit: bool = True) -> Source | None:
        """First source of the deck's corpus (inheritance included unless `inherit=False`).

        Kept for the CLI, which only ever shows/edits one source per deck.
        """
        corpus = self.corpus(deck) if inherit else self._listed(deck)
        return corpus[0] if corpus else None

    def set_corpus(self, deck: str, entries: list[Source]) -> tuple[list[Source], int]:
        """Replace the deck's own corpus with `entries`. An empty list deletes the entry.

        An entry without an id is a new source and gets one; an entry with a known id lists
        that shared source and rewrites its fields, for every deck that lists it. A source no
        deck lists any more goes, and so do the anchors to it; the count of dropped anchors is
        returned with the new corpus.
        """
        ids: list[str] = []
        for source in entries:
            sid = source.id or new_source_id()
            self.sources[sid] = replace(source, id=sid)
            if sid not in ids:
                ids.append(sid)
        if ids:
            self.lists[deck] = ids
        else:
            self.lists.pop(deck, None)
        removed = self._forget_unlisted()
        self.save()
        return self._listed(deck), removed

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
        (stored,), _ = self.set_corpus(deck, [source])
        return stored

    def unset(self, deck: str) -> bool:
        """Remove the corpus written on this exact deck (not an ancestor's)."""
        removed = deck in self.lists
        if removed:
            self.set_corpus(deck, [])
        return removed

    def share(self, deck: str, with_decks: Iterable[str]) -> None:
        """List the deck's own sources on each of `with_decks`, after that deck's own, then take
        them off `deck`: shared, never copied, ids kept (a group turned into a label)."""
        group_ids = self.lists.pop(deck, [])
        if not group_ids:
            return
        for other in with_decks:
            own = self.lists.get(other, [])
            self.lists[other] = own + [sid for sid in group_ids if sid not in own]
        self._forget_unlisted()
        self.save()

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

    # `AbstractSet`, not `set`: inside this class `set` is the method above.
    def _known_ids(self) -> AbstractSet[str]:
        return set(self.sources)

    def _forget_unlisted(self) -> int:
        """Drop the sources no deck lists any more, then the anchors to them. Returns how many
        anchors went."""
        listed = {sid for ids in self.lists.values() for sid in ids}
        for sid in list(self.sources):
            if sid not in listed:
                del self.sources[sid]
        return self._drop_anchors_to_unknown_sources()

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

        An inherited corpus is listed on `deck` first — shared, not copied, so anchors survive —
        which is the rule of the Source tab's form; this is what `POST /api/sources` and
        `create_note` share. The entry is validated as on load (kind, url, pages); a missing id
        gets one. Returns the stored entry, listed on `deck`.
        """
        entry = Source.from_dict(deck, source.to_dict())
        own = self._listed(deck) if deck in self.lists else self.corpus(deck)
        self.set_corpus(deck, [*own, entry])
        return replace(self.sources[entry.id], deck=deck)

    # ---------------------------------------------------------- vault writes

    def create_note(
        self, deck: str, name: str, content: str, source_id: str | None = None
    ) -> Source:
        """Write `<vault>/<name>.md` and add it to the deck's own corpus.

        Refuses if the file exists (FileExistsError). `name` is vault-relative, `/` allowed
        (parent directories are created), `.md` optional. `source_id` lets a caller who
        announced the id beforehand (the chat's create-source proposal) keep it.
        """
        clean = name.strip().strip("/")
        if not clean:
            raise ValueError("empty note name")
        target = clean[:-3] if clean.endswith(".md") else clean
        path = self.vault.path / f"{target}.md"
        if path.exists():
            raise FileExistsError(f"{target}.md already exists in the vault")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self.add_source(
            deck, Source(deck=deck, kind="obsidian", target=target, id=source_id or "")
        )

    def replace_in_note(self, deck: str, source_id: str, old: str, new: str) -> None:
        """Replace `old` with `new` in an obsidian source's file; `old` must occur exactly once.

        KeyError for an id the deck's corpus does not list; ValueError for a pdf or web source,
        or when `old` occurs 0 or 2+ times (the message says which). The bounded, reviewable
        replacement is the whole safety story of writing into the vault — and what makes undoing
        it a swap of `old` and `new`.
        """
        source = self.find(deck, source_id)
        if source is None:
            raise KeyError(f"unknown source: {source_id}")
        if source.kind != "obsidian":
            raise ValueError("only an Obsidian note can be edited")
        if not old:
            raise ValueError("empty passage to replace")
        path = source.note_path(self.vault)
        if path is None or not path.exists():
            raise ValueError("file not found")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise ValueError("passage not found")
        if count > 1:
            raise ValueError(f"ambiguous passage ({count} occurrences)")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
