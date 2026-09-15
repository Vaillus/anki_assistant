---
name: spec-sync
description: >-
  Pre-PR check: map the branch's changes to affected specs, report
  code-vs-spec drift, and rewrite drifted specs with /write-spec-file.
---

# /spec-sync

Run before opening a pull request. Three passes: identify, check, fix.

## 1 — Identify affected specs

1. `git diff main...HEAD --name-only` → list of changed files.
2. Read `specs/00-overview.md` § Module map. For each changed code file, find
   the spec it maps to. A changed spec file is always included. Deduplicate.
3. Print the list — each affected spec and the code files that triggered it.
   Flag any changed code file that has **no** spec in the module map ("unspecced").

If no specs are affected, say so and stop.

## 2 — Drift check

For each affected spec:

1. Read the spec in full.
2. Read **every** code file the module map assigns to this spec — not just the
   changed ones; drift can exist in unchanged code.
3. Compare and report every disagreement as a row:

| Spec says | Code does | Likely right |
|---|---|---|
| … | … | spec / code / unclear |

What counts as drift:
- A rule the spec states that the code violates.
- A behaviour the code implements that the spec doesn't mention.
- A term or definition that differs.
- An API route, tool, or command listed in the spec that is absent, renamed,
  or has a different contract in the code.

What is **not** drift: implementation details the spec intentionally omits.
Specs describe behaviour, not how the code achieves it — missing internal
helpers, data structures, or private functions are not drift.

If no drift for a spec, say "no drift."

## 3 — Rewrite drifted specs

For each spec where drift was found:

1. Present the drift table and ask: **"Rewrite this spec to match the code?"**
   If the user says no, or if the likely-right column says "spec" for every
   row, skip — the code should be fixed instead.
2. If yes, invoke `/write-spec-file` on that spec's concept so it is rewritten
   at the spec's style and abstraction level, incorporating the new code
   behaviour.
3. Show the diff of what changed in the spec file.

## 4 — Summary

Print one table:

| Spec | Drift found | Action taken |
|---|---|---|
| … | yes / no | rewritten / skipped — reason / no drift |

Flag anything that needs a human decision ("spec says X, code says Y, both
look intentional — which is right?").
