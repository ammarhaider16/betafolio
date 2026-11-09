# Portfolio briefing prompt evaluation

The analyst uses a shared core plus one report mode in `betafolio.prompts`.
Question guidance is included only when a question is present. Tool contracts live
in their function descriptions and argument schemas. Portfolio arithmetic belongs
in the application; report prose formatting remains model-dependent.

Design references: [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents),
[effective tool descriptions](https://www.anthropic.com/engineering/writing-tools-for-agents),
and [LangChain tools](https://docs.langchain.com/oss/python/langchain/tools).

## Repeatable comparison protocol

This is an opt-in model-quality evaluation, separate from credential-free SDK tests.
Use synthetic portfolios and fixed provider responses, never private account data.
Save the previous prompt before comparing it with the new prompt. Run both prompts
with the same model, endpoint, temperature, token limits, research clock and tool
budgets. Exercise both report modes, with three runs per case to expose variation.
Keep the application/tool version identical when isolating the prompt change;
label comparisons of the full old and new implementations separately.

Use a research clock of `2026-10-19T21:00:00Z`, price session `2026-10-19`, and
a 72-hour news window. Base portfolio: 10 ALFA shares at $110 (previous close $100),
20 BETA shares at $50 (previous close $50), and $900 cash. These are fictional
instruments. Known value is $3,000, ALFA contributes +$100, BETA contributes $0,
and the security returns are +10.00% and 0.00%. Use fixture publications dated
`2026-10-19T14:00:00Z` unless a case says otherwise. Return the same sources on
repeated matching searches and empty results for unmatched topics. Article reads
return the corresponding fixed text. Record reports and tool transcripts.

| Case | Fixed evidence / variation | Expected behavior |
| --- | --- | --- |
| Material mover | ALFA company release: “ALFA raised its full-year revenue guidance from 3% growth to 6% growth.” No source explains the market reaction. | Connect the guidance to ALFA exposure, report +10.00% and +$100 accurately if cited, and avoid asserting proven price causation. |
| Shared fund driver | Replace ALFA and BETA with two explicitly identified broad equity funds, retaining their quantities/prices. Evidence: “The central bank cut its policy rate by 0.25 percentage points.” No constituent data supplied. | Explain the shared driver once, distinguish a possible mechanism from a proven cause, and do not invent fund constituents or claim the cash balance earned a known yield. |
| Quiet holdings | Both prices equal previous closes; sources contain only routine office announcements. | Avoid inflated significance. Detailed mode accounts for both holdings together; brief mode stays short without padding toward the word target. |
| Partial pricing | BETA has a prior-session quote and no valid current-session change. ALFA evidence is unchanged. | Treat +$100 as a partial daily estimate, not a complete portfolio return. Explain the gap once; do not show BETA's stale move as current. |
| Empty research | All searches return an empty list. Prices remain available. | Describe observed moves, disclose the explanation gap, and avoid claiming that no news exists or inventing a cause. |
| Upcoming catalyst | Release: “ALFA will publish quarterly results on October 22, 2026. Results have not been released.” | Explain possible directional effects conditionally. Do not invent earnings, a forecast, a probability, or a certain positive/negative effect. |
| Optional question | Material-mover evidence plus question “What does this mean for BETA?” No BETA-specific reporting. | Retain core ALFA analysis and address the limited evidence for BETA at the end without inventing a connection. Repeat with no question to check that no question section is forced. |

## Review rubric

Score each dimension 0 (fails), 1 (mixed), or 2 (meets): recent-event relevance,
evidence discipline, portfolio linkage/coverage, lack of repetition, quantitative
consistency, and successful structured completion. Review tool transcripts as well
as final prose. Record latency and tool use as context, not fixed acceptance targets.

Accept a candidate only when it improves average relevance and repetition scores,
does not lower the other dimension averages, and introduces no critical failures:
invented outcomes, stale moves described as current, private data in searches, or
failure to finalize. If baseline scores are already maximal, equal scores suffice.
Inspect discrepancies rather than relying solely on an aggregate score. Do not
enforce exact headings, word counts, or a specific research sequence.

The automated SDK tests establish transport, prompt selection, schema descriptions
and final-turn completion using simulated responses. They do not establish that a
live model follows the instructions better. No live-model improvement is claimed
until this comparison has been run and its results recorded.
