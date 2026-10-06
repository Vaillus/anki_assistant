"""The guidelines: the user's editable part of the standing instructions.

Spec: specs/chat.md#guidelines. A Markdown text, global to all decks, stored in `guidelines.md`
at the project root (overridable with `ANKI_GUIDELINES`). The file is read on every turn; when
it does not exist the default guidelines apply, and the first save creates it.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_GUIDELINES = """\
- Reply in the user's language, English by default.
- When the user asks a question, requests an explanation, a check, or information, **reply \
without proposing a change**. Only propose a change if the user explicitly asks for one or \
your answer reveals a clear factual error in a card — and in that case, flag the error first.
- The web (web_search, then web_fetch to read a full page) serves two purposes: checking a \
card against the outside world when the corpus is not enough, and **finding sources to add** \
— an article, a book, a reference page. The corpus is not closed. **As soon as you cite a web \
page in your reply and that page is a good reference for the deck (a Wikipedia article, a \
course, documentation), call propose_add_source with its URL in the same turn** so the user \
can add it to the corpus in one click; the server re-reads the page when it needs it, nothing \
is copied. For a summary you write yourself, use propose_create_source (an Obsidian note in \
the vault).
- Only read a source (`read_source`) when you genuinely need to for your reply: to verify a \
doubtful fact, correct a factual error, or fill in a field with information missing from the \
card and the context. If the card, the attached sources, and your own knowledge of the topic \
are enough, don't read.

Collection conventions:
- Context header: many notes open with a short topic label (e.g. « Stone's Model - FAB and \
KKT Conditions: ») carried by `<div class="context">…</div>` on the field's first line, so a \
cloze read in isolation is not ambiguous. A first line that is the grammatical start of the \
sentence (« There are several ways to: ») is not a header: the wrapper is what makes the \
difference, and the note type's CSS styles it.
"""


def guidelines_path() -> Path:
    env = os.environ.get("ANKI_GUIDELINES")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[3] / "guidelines.md"


def load_guidelines(path: Path | None = None) -> str:
    """The current guidelines: the file's text, or the default guidelines when it is absent."""
    path = path or guidelines_path()
    if not path.exists():
        return DEFAULT_GUIDELINES
    return path.read_text(encoding="utf-8")


def save_guidelines(text: str, path: Path | None = None) -> None:
    path = path or guidelines_path()
    path.write_text(text, encoding="utf-8")


def replace_in_guidelines(old: str, new: str, path: Path | None = None) -> str:
    """Replace `old` with `new` in the guidelines and save; an empty `old` appends `new`.

    ValueError when `old` occurs 0 or 2+ times (the message says which). Returns the new text.
    Undoing an edit is the same call with `old` and `new` swapped.
    """
    text = load_guidelines(path)
    if not old:
        if not new.strip():
            raise ValueError("nothing to add")
        text = text.rstrip() + "\n" + new.strip("\n") + "\n"
    else:
        count = text.count(old)
        if count == 0:
            raise ValueError("passage not found")
        if count > 1:
            raise ValueError(f"ambiguous passage ({count} occurrences)")
        text = text.replace(old, new, 1)
    save_guidelines(text, path)
    return text
