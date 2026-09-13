# Notes

> Defines the objects the app works on — note, card, note type, field — and the conventions it preserves when it reads or writes a field.

## Note, card, note type

An Anki **note** is the editable object: an ordered set of **fields** (name → raw HTML value), a list of **tags**, and a **note type**. A note is not a card.

A **card** is what Anki schedules and shows to the learner. Cards are generated from a note by its note type's templates — they are never edited directly. A single note can produce several cards (a Cloze note with three cloze deletions yields three cards).

A **note type** (Anki calls it "model") decides which fields a note has, how many cards it produces, and what those cards look like (front template, back template, CSS). The set of fields is fixed by the note type — every note of the same type has exactly the same field names in the same order.

## The review unit is the note

The app reviews notes, not cards. A note is **flagged** when any of its cards carries a flag; resolving a note clears the flag on all its cards. Flag colours carry no meaning — any flag means "to review."

## Fields are raw Anki HTML

A field value is stored as an HTML fragment written by Anki's editor: `<br>` for line breaks, `<div>`/`<p>` wrappers, `<img>` tags for rendered LaTeX (whose `alt` carries the source). Everything that reads or writes a field — the display renderer ([review.md § Rendering](./review.md#rendering)), the edit and split decisions, the chat proposals — works on that raw value. Rewriting a field means producing the same syntax, with the markers intact and the HTML valid.

## Note types the app handles

The app is **note-type-agnostic**: no code compares model names. A note's type is an opaque string carried through from AnkiConnect, and the field list comes from Anki at runtime. In practice, the collection uses two types:

| Note type | Fields | Cards produced |
|---|---|---|
| **Cloze** | `Text`, `Back Extra` | One card per cloze deletion in `Text`. Card ordinal (`ord`) 0 hides `c1`, ord 1 hides `c2`, etc. |
| **Basic** | `Front`, `Back` | One card: `Front` on the question side, `Back` on the answer side. |

Other types work — the app iterates whatever fields the type declares — but the review queue, the workspace and the chat prompt are designed around these two.

## Reason (Back Extra)

`Back Extra` is the only field name the code knows by name. When a flagged note has a `Back Extra` field, its plain-text value is the **reason** — the text displayed prominently as "why this was flagged" and included in Claude's context. If the note type has no `Back Extra` field, or the note is not flagged, the reason is empty.

`Back Extra` is never shown among the editable fields (queue or workspace) and is never editable by hand. A checkbox at validation offers to clear it ("vider Back Extra"). See [workspace.md § Validation](./workspace.md#validation).

## Cloze markers

A Cloze note's `Text` field contains one or more **cloze deletions**: `{{c1::answer}}` or `{{c1::answer::hint}}`. The number after `c` is the **cloze number**; each distinct number produces one card.

Rules for producing cloze fields (stated to Claude as facts in the system prompt):

- Numbers are contiguous starting from `c1`.
- Braces are balanced — every `{{` has its `}}`.
- A Cloze note has at least one cloze deletion.
- The answer is non-greedy: `{{c1::a::b}}` is answer `a`, hint `b`, not answer `a::b`.

The renderer (`render.py`) parses cloze markers with a regex (`{{c(\d+)::(.*?)(?:::(.*?))?}}`), escapes the surrounding text first, then wraps each cloze in `<span class="cloze" data-n="N" data-hint="…">answer</span>`. The hint, when present, lives in a `data-hint` attribute so the UI can show `[hint]` in question state.

## Context header

Many notes start with a **context header**: a short topic label in a `<div class="context">…</div>` as the first line of the field, so that a cloze read in isolation is not ambiguous. Example:

```html
<div class="context">Stone's Model - FAB and KKT Conditions:</div>There are {{c1::three}} conditions…
```

A first line that is the grammatical start of the sentence is not a header — it is the `<div class="context">` wrapper that makes the difference, and the note type's CSS styles it.

The renderer extracts the header before stripping HTML, then re-wraps it as `<span class="context">…</span>` ahead of the body. Plain-text conversion (`strip_html`) does not preserve the header's visual distinction — it drops the `<div>` and the header runs into the body text.
