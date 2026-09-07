# Writing and fixing flashcards with an LLM — recommendations

> Research note, 2026-09-06. Input for the chat system prompt ([`specs/chat.md`](../../specs/chat.md), `STANDING_INSTRUCTIONS` in `chat.py`). Not a spec: nothing here is implemented until it is moved into a spec.

## Question

How should Claude be instructed so that the cards it proposes (edits, splits, new notes) are good spaced-repetition prompts, and how should it diagnose a flagged card? What do the people who have studied this recommend?

## Sources

Primary sources, in order of weight for our case:

1. Andy Matuschak, *How to write good prompts* (2020) — <https://andymatuschak.org/prompts/>. The reference on prompt quality; the essay Dwarkesh Patel pastes into Claude.
2. Ozzie Kirkby & Andy Matuschak, *Memory Machines: evaluating LLM-generated flashcards* (2026) — <https://memory-machines.com/report>. 1 500 labelled cards, 16 models, the only systematic evaluation of LLM card quality.
3. Matuschak's working notes on LLM generation — <https://notes.andymatuschak.org/Using_machine_learning_to_generate_good_spaced_repetition_prompts_from_explanatory_text>, <https://notes.andymatuschak.org/zJRDpsHzhrx87XxabV5jqXg>, <https://notes.andymatuschak.org/zGkLPdiEs7Qohkesq7TNiBe>.
4. Soren Bjornstad, *Rules for designing precise Anki cards* — <https://controlaltbackspace.org/precise/>.
5. Piotr Wozniak, *Twenty rules of formulating knowledge* (1999) — the original source of "one atomic fact per card", cited by all of the above.
6. Practitioner reports: John Whiles, *I'm making Claude write Anki cards for me* — <https://johnwhiles.com/posts/claude-anki>; Nate Meyvis on Kirkby & Matuschak — <https://www.natemeyvis.com/kirkby-and-matuschak-on-making-flashcards-with-llms/>.

## What a good card is (the rubric)

Matuschak, Bjornstad and Wozniak agree on the same properties. Matuschak's names, with Bjornstad's operational tests:

| Property | Test |
|---|---|
| **Focused** | One idea, one retrieval. Several short notes beat one long one. |
| **Precise / single answer** | The question excludes every other correct answer. Otherwise you memorise "what the card is asking", not the knowledge. |
| **Consistent** | The same answer comes out every review. Fuzzy prompts cause retrieval-induced forgetting of the neighbours you did not recall. |
| **Tractable** | Answerable about 90 % of the time. Cards that keep failing get split or deleted, not rewritten harder. |
| **Effortful** | Not inferable from the question's shape, a give-away cue, or a yes/no guess. |
| **Context-free** | Understandable months later without the source. Bjornstad's topic prefix is our `<div class="context">` header. |

Named anti-patterns (the diagnostic vocabulary for a flagged card):

- **Binary** question (yes/no, either/or) → rephrase as an open question about a consequence or an example.
- **Enumeration** ("name all the…") / **open list** with fuzzy boundary → one cloze per element, order kept stable; or "why does X belong" prompts.
- **Multiple valid answers** → add the constraint that excludes the others.
- **Orphan fact** with no "why" → add or link a significance prompt, or delete.
- **Wordy / long question** → gets pattern-matched by shape. One sentence, no qualifier that is not needed to exclude another answer.
- **Give-away cue** in the question → trivial retrieval.
- **Surface prompt** on conceptual material: recalls the wording ("what properties define echelon form?") instead of the meaning. Matuschak's lenses for concepts: classify an example ("is this in echelon form, why?"), apply ("put this system in echelon form"), purpose ("what does echelon form tell you about the solution set?"), contrast with a neighbouring concept, parts/whole, cause/effect.

## What is known about LLMs doing this

From Memory Machines (2026) and Matuschak's notes:

- **Targeting is solved, construction is not.** Models reliably find what a passage wants you to remember. They fail at writing a prompt that survives months of review. The best model (GPT-5.2) produced unusable cards 36 % of the time; Claude Sonnet 5 ranked second at 58 % usable.
- **The dangerous failure is the plausible card, not the hallucination.** Their tier T1: cards that read fine but are ambiguous, underspecified or too abstract. They "quietly degrade the system". Wordiness was undetectable by every model tested (F1 ≤ 0.37); lack of context was detectable (F1 ≈ 0.85).
- **Grounded examples beat principles.** Showing the judge two or three rated cards *from the same source* raised precision from 56 % to 78 % and cut false positives from 52 % to 17 %. Generic few-shot examples and rubrics helped only marginally. Fine-tuning on 1 500 samples "bought efficiency, not capability".
- **Separate target selection from composition.** Matuschak's most reliable recipe: point at the exact phrase to reinforce, give the principles, add a hint about the angle, supply generous surrounding context. Works "usually on the first try" for declarative knowledge, poorly for conceptual material.
- **Human culling is the workflow that works.** Meyvis and Whiles both report the propose-then-cull loop with Claude as efficient. Whiles's prompt asks for "single atomic concept" cards with "clear success and failure conditions", after the learner has understood the material.
- **Writing the card is part of learning.** Pan et al. (2022, via Keiffenheim) measure d = 0.45 in favour of self-written cards. Our setting is on the safe side (the card exists, Hugo decides), which argues against ever letting Claude apply without a click.

## Recommendations for the chat prompt

In order of expected payoff. Items 1 and 2 are the ones the evidence supports most strongly.

1. **Ground with Hugo's own good cards.** Put three to five well-formed, unflagged notes of the same deck in the system prompt as style exemplars (hand-picked by tag, or the most-reviewed unflagged notes). Single strongest lever in the benchmark; the deck index already exists, so it is cheap.
2. **Diagnose before fixing.** Require the `rationale` of every proposal to start with the flaw, from a fixed vocabulary: `lacks-context`, `multiple-answers`, `shallow`, `wordy`, `narrow`, `binary`, `enumeration`, `orphan-fact`, `two-ideas`, `wrong-fact`. A named flaw forces the matching fix; `two-ideas` maps to `propose_split`.
3. **Two-step composition.** Before writing a field, state in one line the exact thing Hugo must retrieve and why it matters; then write the card. Keep the original card's target unless the flag reason says the target is wrong.
4. **Conceptual lenses.** When the note is a concept rather than a fact, offer prompts from the lenses (classify, apply, purpose, contrast) instead of definitional recall, and say which lens was used.
5. **Hard constraints on form**, since models cannot self-detect them: one sentence per question; no qualifier that does not exclude another answer; no yes/no; no "list all"; every card readable without the source.
6. **Quote the corpus.** Ask Claude to cite the supporting passage in the rationale; an audit pass against the source catches most invented facts.
7. **Offer two variants for a rewrite** when the flaw is construction (wordy, multiple answers). Cheap for Claude, and culling among candidates is the step humans are good at.

Already in the standing instructions and confirmed by the sources: one note = one idea, prefer several short notes, never invent beyond the corpus, address the flag reason first, context header outside the sentence, Claude never writes to Anki itself.

## Open questions

- How to pick the exemplar notes automatically (tag, review count, manual "golden" flag?).
- Whether the flaw vocabulary should be a tool-schema enum (machine-checkable) or free text in the rationale.
- Whether to expose Matuschak's lenses as an explicit choice to Hugo ("propose a classification card") rather than leaving it to Claude.
