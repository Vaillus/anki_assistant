"""Web page fetching and HTML-to-text extraction.

Fetches a web page, reduces it to readable plain text (block structure kept as
line breaks, headings as ``#`` marks, site chrome stripped), and caches the
result for the process lifetime (a failed fetch is retried after
``WEB_RETRY_SECONDS``).
"""

from __future__ import annotations

import re
import time
from html.parser import HTMLParser

import httpx

#: Seconds allowed for fetching a web source.
WEB_TIMEOUT = 15.0
#: A failed fetch is remembered this long before it is tried again, so that an offline session
#: does not wait for a timeout on every corpus load.
WEB_RETRY_SECONDS = 60.0
#: Some sites refuse the default `python-httpx/x.y` agent with a 403; a browser-like one is fine.
WEB_USER_AGENT = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) anki-assistant"

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
