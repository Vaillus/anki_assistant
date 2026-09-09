"""Anki field HTML -> safe display HTML. Spec: specs/review.md#rendering.

Anki stores field values as HTML fragments written by its own editor: `<br>` for line breaks,
`<div>`/`<p>` wrappers, and `<img>` tags for rendered LaTeX (whose `alt` carries the source).
The review UI wants none of that markup, but does want cloze deletions marked up so they can be
highlighted. So: flatten to text, escape it, then re-introduce exactly the two tags we control
(`<span class="cloze">` and `<br>`).
"""

from __future__ import annotations

import html
import re

__all__ = ["render_field"]

_BR = re.compile(r"<br\s*/?>", re.I)
_BLOCK_END = re.compile(r"</\s*(?:p|div|li)\s*>", re.I)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_ALT = re.compile(r"\balt\s*=\s*(\"([^\"]*)\"|'([^']*)'|([^\s\">]+))", re.I)
_TAG = re.compile(r"<[^>]+>")
# {{cN::answer}} or {{cN::answer::hint}}. The answer is non-greedy so nested braces in a hint
# do not swallow the closing delimiter; DOTALL because an answer may span a line break.
_CLOZE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)
_CONTEXT = re.compile(r'^\s*<div\s+class\s*=\s*"context"\s*>(.*?)</div\s*>', re.I | re.S)


def _img_to_text(match: re.Match[str]) -> str:
    """An `<img>` becomes its alt text (LaTeX source, for Anki's rendered maths) or `[image]`."""
    alt = _ALT.search(match.group(0))
    if alt:
        text = next((g for g in alt.groups()[1:] if g is not None), "")
        if text.strip():
            return text
    return "[image]"


def _cloze_to_span(match: re.Match[str]) -> str:
    """`{{cN::answer::hint}}` -> a span carrying the cloze number and, if any, the hint.

    The hint lives in an attribute so the UI can show `[hint]` when it hides the answer
    (question state). The text was already escaped, quotes included, so it is attribute-safe.
    """
    number, answer, hint = match.group(1), match.group(2), match.group(3)
    hint_attr = f' data-hint="{hint}"' if hint else ""
    return f'<span class="cloze" data-n="{number}"{hint_attr}>{answer}</span>'


def render_field(raw: str) -> str:
    """Turn one raw Anki field value into display HTML.

    Escaped first, so nothing from the note can inject markup; the only tags in the output are
    `<br>` and `<span class="cloze" data-n="N" data-hint="…">`.
    """
    if not raw:
        return ""
    # Extract context header before stripping tags so we can re-wrap it.
    ctx = ""
    rest = raw
    m = _CONTEXT.match(raw)
    if m:
        ctx = m.group(1)
        rest = raw[m.end() :]
    text = _BR.sub("\n", rest)
    text = _BLOCK_END.sub("\n", text)
    text = _IMG.sub(_img_to_text, text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = html.escape(text.strip())
    text = _CLOZE.sub(_cloze_to_span, text)
    out = text.replace("\n", "<br>")
    if ctx:
        ctx_text = html.escape(html.unescape(_TAG.sub("", ctx)).strip())
        out = f'<span class="context">{ctx_text}</span><br>{out}'
    return out
