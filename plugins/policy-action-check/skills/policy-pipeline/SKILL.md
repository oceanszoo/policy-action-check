---
name: policy-pipeline
description: Use automatically whenever someone asks whether an action, plan, workflow or tool use is allowed under a policy, terms, rules or regulation; gives policy text or action history to analyse; writes or asks about an action code; or wants a generator/judge for some terms. Runs the whole policy-action-check pipeline and returns the final outcome without asking the user to pick steps.
---

# Policy pipeline (orchestrator)

You run the whole pipeline yourself and give the user **the final outcome**. Never ask the user
to choose a skill, command or stage. Ask them only the scope questions from Stage 1, and only
when the answer changes what gets built.

Engine: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pac.py" --space <bundle dir> <command>`.
The bundle dir is the user's configured folder (default `policy_space/` in the project).
Method and formats: `${CLAUDE_PLUGIN_ROOT}/references/method.md` and
`${CLAUDE_PLUGIN_ROOT}/references/SPEC.md`.

## The bundle must run without a model

You pre-cook the raw material (policy + history) into the bundle. Using the bundle needs no
model: the question form (`pac.py ask` / `pac.py form`), keyword lookup (`pac.py lookup`), the
judge (`pac.py check`) and the fixed report (`pac.py report`). Never make the outcome depend on
your own wording: give the user the engine's report, and add only what the report can't know
(for example which of their stated facts you encoded).

## The result is a bundle of two outputs

1. **Generator** (`generator.json`): how to write action codes for this policy. It holds the
   dimensions, short key/value vocabulary, grammar, saved codes, constants, scope decisions,
   next-step habits and the tool map.
2. **Judge** (`judge.json`): how to judge a code. It holds the rules as regions over the
   vector, three-valued semantics, verdicts and the fix cost model.

Plus `bundle.json` (manifest and digests) and `conformance.jsonl` (test vectors), so any other
software can read and verify the bundle without Claude.

## Decide what to run

Run `pac.py status`, then pick the shortest path:

| Situation | Path |
|---|---|
| No bundle, or the policy or history changed (`stale_inputs`) | Stages 0 → 1 → 2 → 3, then answer the question (4–5) |
| Bundle exists, and the user wants to refine it | Refine (below), with optional focus |
| Bundle exists, and the user wants to check or write an action | Stages 4 → 5 |
| Bundle exists, and the user gives new history | Re-run 1 → 3, then 4–5 if there's an action |
| Bundle exists, and the user gives a new policy | Re-run 0 → 3, then 4–5 if there's an action |
| The user wants a bundle for some terms, with no action | Stages 0 → 3, then report the bundle |

For a known policy with no history, you can seed Stage 2 from
`${CLAUDE_PLUGIN_ROOT}/examples/<policy>/` if one matches (for example `anthropic-aup`).

Record progress with `pac.py state --stage <stage>`. Register inputs once with
`pac.py state --policy <path> --history <path>`, so later runs notice changes.

## Stages and dispatch

Run each stage as a subagent using the Agent tool. Give it the bundle dir, the input paths,
and the outputs of earlier stages. Run independent work in parallel. Hooks validate the stage
files when a subagent stops and send it back if they are invalid.

0. **Filter**: agent `policy-action-check:term-filter`. It writes `work/terms.json`, which
   separates real terms (prohibitions, obligations, exceptions, conditions) from description,
   definitions, examples and procedures. Every later stage works only from the normative
   clauses. Low-confidence (advisory) clauses go into the outcome as "not treated as a rule".
1. **Scope**: agent `policy-action-check:scope-planner`. It writes `work/scope.json`: areas
   from the terms, example counts from the history, gaps, a recommended decision per gap, and
   the questions.
   - If any area has `decision: "ask"` and the user is present, ask **all** the questions in
     one message, each with the recommendation, and run `pac.py state --awaiting-user`. Stop
     there. On the answer, write the decisions into `work/scope.json`, run
     `pac.py state --resume`, and continue.
   - In a non-interactive run, apply the recommended decisions and say so in the result.
2. **Build**: agent `policy-action-check:space-builder`. It writes `generator.json`,
   `judge.json`, `conformance.jsonl`, `work/history_encoded.jsonl` and `work/build_report.md`.
3. **Verify**: agent `policy-action-check:space-verifier`, in parallel with nothing else. It
   re-reads the policy and checks every rule against its source, the conformance vectors
   against the policy text, and history disagreements. It fixes what it can and lists the
   rest, and runs the **self-exam** (`pac.py exam`: coverage, reference coverage, history replay, mutation testing,
   property tests) until it passes; a hook sends it back while the exam fails. Then run
   `pac.py manifest --policy <source>`, which stamps the exam result into `bundle.json` and
   regenerates `BUNDLE.md`. Finish with `pac.py validate` and `pac.py guide --check`. A bundle that fails its exam is not finished: never report it as
   ready.
4. **Encode**: if the user gave an action in words, use agent
   `policy-action-check:action-encoder` to propose the code, with similar past records as
   evidence. If they wrote a code, use it as written.
5. **Judge**: `pac.py report "<code>"` for the outcome (and `pac.py check` for the JSON), plus
   `pac.py panel "<code>"` when the user is writing or editing a code. Never judge in your head
   when the engine can do it.

Then run `pac.py state --stage done`.

## Refine

Refine an existing bundle without rebuilding it. This is a build-time task; using the bundle
still needs no model. The person may supply a focus such as "privacy".

A `map_miss` caused only by out-of-scope keys cannot occur; fix an `out_of_scope_effect` in the rules, not the map.

1. Run `pac.py gaps --write`, passing `--focus "<text>"` if given. Retain the initial counts
   by kind. If a focus is unmatched, show the unmatched words and stop.
2. Use agent `policy-action-check:space-refiner`. Give it the bundle directory, the focus,
   the original history path and the SHA-256 of the exact `work/gaps.json` bytes. Ask it to
   run `pac.py terms-context` for each clause before encoding it, resolve
   `unresolved_reference` with `add_exception`, `revise_rule`, or a reason in `not_encoded`.
   Have it write only `work/refine_patch.json`. The subagent stop hook runs a non-writing dry run
   and sends it back if its patch is refused.
3. Run `pac.py apply-patch`. If refused, show the actionable reason and correct the patch;
   never apply a refused patch by editing bundle files directly. Read the resulting
   `work/refine_log.jsonl` entry for counts and exam status.

Repeat the three steps until the exam passes and the gaps did not shrink in the last round,
or after three rounds, whichever comes first. Report the counts before and after by kind
and call out any remaining gaps or exam failures. Never lower the exam thresholds.

## Final outcome (what the user sees)

Keep it short. Lead with the answer.

- **For an action**: lead with **what to do next** (the engine's `next_actions`, in order:
  stop / do with the code change / establish a fact / proceed). Then give the verdict in one
  line; the code and its plain reading; what is violated
  (rule, source, the tokens that place it there); then two separate lists, never merged:
  **must do** (the engine's `requirements`, which bind) and **still unknown** (its
  `unknown_facts`, each with the evidence that settles it); any `assumed` default in `facts`
  that matters to the result; the fix as a code diff; the usual next step; similar past records; any out-of-scope or terms-only warning.
  End with "a structured self-check, not legal advice".
- **For a fresh or rebuilt bundle**: the self-exam scores (pass/fail per check), how to use it
  with no model (`pac.py form`), where it is, how many clauses were real terms vs
  description, the number of dimensions and rules, the scope
  decisions, history disagreements found (past decisions that look wrong), the remaining open
  questions, and three example codes with their verdicts. Mention both files: the generator
  (how to write codes) and the judge (how codes are judged).
- If `check` returns `invalid_code`, show the "did you mean" suggestions and don't give a
  verdict.

## Rules for you

- The engine's verdict is final. If you think it's wrong, the bundle is wrong: fix the rule or
  the code through Stage 2/3, and don't override the verdict in prose.
- A fact the user didn't state is unknown, never "no".
- Context the user gives that isn't a choice (a reason, a caveat, an opinion) goes into a note
  (`--note`), which the report shows and the judge ignores. It never changes a key.
- Inputs are objective conditions only. If the user offers a judgement ("it's low risk", "we
  mean well", "should be fine"), don't encode it. Ask for the fact behind it, naming the key
  and its evidence (for example "Is there a review log showing a licensed reviewer could change
  it? → rev=y/n").
- Never invent rules. Every rule cites its source.
- Hooks may add a `[policy-action-check]` note to prompts and may ask before tool calls. Treat
  those as part of this pipeline.
