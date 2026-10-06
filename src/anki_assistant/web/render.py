"""Anki field HTML -> safe display HTML. Spec: specs/review.md#rendering.

Anki stores field values as HTML fragments written by its own editor: `<br>` for line breaks,
`<div>`/`<p>` wrappers, and `<img>` tags — rendered LaTeX (whose `alt` carries the source) or
pasted pictures (a bare media file name in `src`, no alt). The review UI wants none of that
markup, but does want cloze deletions marked up so they can be highlighted, and pictures shown.
So: flatten to text, escape it, then re-introduce exactly the tags we control
(`<span class="cloze">`, `<br>`, and `<img>` pointing at `/api/media/`).
"""

from __future__ import annotations

import html
import re
from urllib.parse import quote

__all__ = ["render_field"]

_BR = re.compile(r"<br\s*/?>", re.I)
_BLOCK_END = re.compile(r"</\s*(?:p|div|li)\s*>", re.I)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_ALT = re.compile(r"\balt\s*=\s*(\"([^\"]*)\"|'([^']*)'|([^\s\">]+))", re.I)
_SRC = re.compile(r"\bsrc\s*=\s*(\"([^\"]*)\"|'([^']*)'|([^\s\">]+))", re.I)
# A picture is held out of the flatten/escape steps as this marker, then swapped for an `<img>`
# the module writes. NUL never survives in a field (it is stripped on input), so the marker
# cannot be forged by a note.
_PIC = re.compile("\x00(\\d+)\x00")
_TAG = re.compile(r"<[^>]+>")
# {{cN::answer}} or {{cN::answer::hint}}. The answer is non-greedy so nested braces in a hint
# do not swallow the closing delimiter; DOTALL because an answer may span a line break.
_CLOZE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)
_CONTEXT = re.compile(r'^\s*<div\s+class\s*=\s*"context"\s*>(.*?)</div\s*>', re.I | re.S)


def _attr(pattern: re.Pattern[str], tag: str) -> str:
    """The (unescaped) value of one attribute of `tag`, or "" when absent."""
    m = pattern.search(tag)
    if not m:
        return ""
    return html.unescape(next((g for g in m.groups()[1:] if g is not None), ""))


def _media_name(src: str) -> str | None:
    """`src` when it is a bare file name in Anki's media folder, else None."""
    src = src.strip()
    if not src or "/" in src or "\\" in src or src in {".", ".."}:
        return None
    return src


def _picture_tag(name: str) -> str:
    """The only `<img>` the transform emits: the media route, nothing else from the note."""
    return f'<img src="/api/media/{html.escape(quote(name, safe=""))}" alt="" class="field-img">'


def _img_to_text(match: re.Match[str], pictures: list[str]) -> str:
    """An `<img>` becomes its alt text (LaTeX source, for Anki's rendered maths), a picture
    marker (a pasted picture: media file name in `src`, recorded in `pictures`), or `[image]`."""
    tag = match.group(0)
    alt = _ALT.search(tag)
    if alt:
        text = next((g for g in alt.groups()[1:] if g is not None), "")
        if text.strip():
            return text
    name = _media_name(_attr(_SRC, tag))
    if name is None:
        return "[image]"
    pictures.append(name)
    return f"\x00{len(pictures) - 1}\x00"


def _cloze_to_span(match: re.Match[str]) -> str:
    """`{{cN::answer::hint}}` -> a span carrying the cloze number and, if any, the hint.

    The hint lives in an attribute so the UI can show `[hint]` when it hides the answer
    (question state). The text was already escaped, quotes included, so it is attribute-safe.
    """
    number, answer, hint = match.group(1), match.group(2), match.group(3)
    if hint:
        hint = _PIC.sub("[image]", hint)  # a picture cannot sit inside an attribute
    hint_attr = f' data-hint="{hint}"' if hint else ""
    return f'<span class="cloze" data-n="{number}"{hint_attr}>{answer}</span>'


def render_field(raw: str) -> str:
    """Turn one raw Anki field value into display HTML.

    Escaped first, so nothing from the note can inject markup; the only tags in the output are
    `<br>`, `<span class="cloze" data-n="N" data-hint="…">`, and
    `<img src="/api/media/<name>" alt="" class="field-img">`.
    """
    if not raw:
        return ""
    raw = raw.replace("\x00", "")  # reserved for picture markers
    # Extract context header before stripping tags so we can re-wrap it.
    ctx = ""
    rest = raw
    m = _CONTEXT.match(raw)
    if m:
        ctx = m.group(1)
        rest = raw[m.end() :]
    text = _BR.sub("\n", rest)
    text = _BLOCK_END.sub("\n", text)
    pictures: list[str] = []
    text = _IMG.sub(lambda m: _img_to_text(m, pictures), text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = html.escape(text.strip())
    text = _CLOZE.sub(_cloze_to_span, text)
    text = _PIC.sub(lambda m: _picture_tag(pictures[int(m.group(1))]), text)
    out = text.replace("\n", "<br>")
    if ctx:
        ctx_text = html.escape(html.unescape(_TAG.sub("", ctx)).strip())
        out = f'<span class="context">{ctx_text}</span><br>{out}'
    return out
