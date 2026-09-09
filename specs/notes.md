# Notes

> What the notes in the collection look like: conventions the app must preserve when it renders or rewrites a field, and that Claude is told as facts (not as rules of conduct) when it produces fields.

## Fields are raw Anki HTML

A field is stored as HTML with cloze markers (`{{c1::…}}`, optionally `{{c1::…::hint}}`). Everything that reads or writes a field — the display renderer ([review.md](./review.md#rendering)), the edit and split decisions, the chat proposals — works on that raw value. Rewriting a field means producing the same syntax, with the markers intact and the HTML valid.

## Context header

Many notes start with a **context header**: a short topic label in a `<div class="context">…</div>` as the first line of the field, styled by the note type's CSS.

The renderer keeps the wrapper as-is. Plain-text previews should preserve the header's visual distinction (e.g. prefix or separator) so the reader can still tell header from body without the HTML.
