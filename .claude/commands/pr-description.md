---
name: write-pr-description
description: >-
  Write a PR description for this single-maintainer repo. PRs are
  squash-merged, so the body becomes the commit message. Three sections:
  Why, What changed, Decisions (optional). No reviewer ceremony.
---

# Write PR description

## Context

This repo has one maintainer. PRs are squash-merged: the title becomes the
commit subject, the body becomes the commit body. The reader is future-me
doing `git blame` or `git log`.

## Title

Conventional Commits — `type(scope): description`, lowercase, no period,
~72 chars. Unchanged from the global skill.

## Body

```markdown
## Why

One to three sentences. What was wrong or missing before this branch.

## What changed

What the branch does in product terms — not a file list, not a diff
summary. A few bullets or a short paragraph.

## Decisions (optional)

Only when a choice was non-obvious. Name the alternative and why you
picked this one. Skip the heading entirely when there's nothing to say.
```

## What doesn't belong

- File-by-file changes list — the diff is right there.
- "How to test" — CI is the gate; if the test matters it's in the suite.
- "Impact" — either it's "why" restated or it's speculation.
- "Before / After" as its own section — fold it into why + what changed.
- "Notes for reviewers" — no reviewer.
- AI attribution footer — per CLAUDE.md.

## Quality bar

- [ ] "Why" makes sense without reading the diff.
- [ ] "What changed" describes product behaviour, not files.
- [ ] No section is left empty or filled with a placeholder.
