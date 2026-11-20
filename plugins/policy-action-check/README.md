# Policy Action Check

Turn any policy or terms, plus your own action history, into a **bundle of two files**:

- **Generator** (`generator.json`): how to write an action as a short code, for example
  `claim-auto: act=biz hr=ins scope=ind fav=part rev=n disc=y`.
- **Judge** (`judge.json`): how to judge a code. It says which terms the code violates, what is
  required next, and the smallest fix (`rev=n -> rev=y`).

Every code is the readable surface of a vector: one coordinate per key, and a missing key is
unknown. Terms are regions in that space, so judging is exact. The same code always gets the
same verdict.

**The plugin's whole job** is to turn the policy's terms into constraints in the space (`judge.json`) and to produce the generator's base data: dimensions, choices and the question form (`generator.json`). **Using the bundle needs no model.** Claude reads the policy and your history
once and pre-cooks them into the two files. After that, a question form or keyword lookup writes
the code, and a plain engine judges it and prints a fixed report. Any software can do the same
from the files ([format spec](references/SPEC.md), version 1.6).

People click choices, and only the choices build the vector. They can add a note to any choice
or to the whole action; notes are shown in the report and never affect the verdict.

An action that does several things at once lists them all on one key (`pur=data+track`). Each
result lists **what you must do** (binding requirements) apart from **what is still unknown**
(facts nobody stated), and shows where every fact came from.

Every 1.5 or newer bundle includes a generated `BUNDLE.md` with the case map, features, rules, examples
and self-exam badge. Open it directly or use the **Guide** link in the offline reader.
`pac.py guide` regenerates it; `pac.py guide --check` checks whether it is current.

Version 1.6 tracks definitions and links between policy clauses. `pac.py terms-lint`
checks the links, and `pac.py terms-context <clause-id>` shows what a clause depends on.
Before running the final exam and manifest, `pac.py embed-terms` copies the source text
from the build into the judge. Reports and the guide can then quote the terms without
reopening the policy or calling a model. Only embed text you are allowed to distribute.

To grow an existing bundle, ask to refine it, optionally with a focus such as "privacy".
`pac.py gaps --write` finds gaps. The build-time refiner proposes a patch, and
`pac.py apply-patch --dry-run` validates it without writing. `pac.py apply-patch` applies the
patch, reruns the exam and records the round. Gap detection, patch application and guide
generation are deterministic and use neither a model nor the network.

## Install (Claude Code)

```
/plugin marketplace add oceanszoo/policy-action-check
/plugin install policy-action-check@oceanszoo-plugins
```

## Use

Just ask. You don't pick skills or commands.

- "Build a policy bundle from `terms.md` and `history.csv`."
- "Can our claims bot auto-deny claims under our service terms?"
- "`act=biz pur=std hr=emp scope=ind fav=part rev=n`: is this ok?"

A prompt hook spots policy questions and starts the pipeline. When it finishes, you get the
outcome, or the bundle. It asks you something only when your history doesn't cover part of the
terms, and then only which areas are in scope.

```
policy ─▶ filter (real terms only) ─┐
history ────────────────────────────┴▶ scope ─▶ build ─▶ verify ─▶ bundle (generator + judge)
action (words or code) ───▶ encode ─▶ judge (exact engine) ─▶ outcome
```

| Part | What it does |
|---|---|
| `skills/policy-pipeline` | orchestrator: picks the stages, dispatches subagents, writes the outcome |
| `agents/term-filter` | separates real terms from description, definitions, examples and procedures |
| `agents/scope-planner` | compares terms with history, finds gaps, drafts scope questions |
| `agents/space-builder` | builds the generator, the judge and the conformance tests |
| `agents/space-verifier` | second reader: re-checks every rule against the policy text |
| `agents/action-encoder` | plain words to a code, with similar past records as evidence |
| `scripts/pac.py` | exact engine, standard-library Python: parse, judge, fix, live panel, hooks |
| `hooks/hooks.json` | intent detection, stage validation, unfinished-pipeline guard, optional tool-call guard |

### Settings

| Setting | Values | Default |
|---|---|---|
| Guard tool calls (`guard_mode`) | `off`, `warn` (ask you), `enforce` (deny violations) | `warn` |
| Bundle folder (`space_dir`) | a folder name | `policy_space` |
| Judge as of date (`policy_date`) | `YYYY-MM-DD` | today |

The tool-call guard does something only when the bundle's generator has a `tool_map`, which
maps tool calls (such as an MCP tool that sends claim decisions) to codes.

## Self-exam

Before a bundle is finished, `pac.py exam` tests it the way open-source software is tested, with
no model: coverage of terms and rules (like coverage.py), replay of your past records (golden
tests), mutation testing of every rule (like mutmut or Stryker), and property tests over
generated actions (like Hypothesis). `pac.py manifest` stamps the result into `bundle.json`, so
your own software can refuse a bundle that failed.

```sh
python3 scripts/pac.py --space policy_space exam
```

## Use a bundle without Claude (no model)

```sh
python3 scripts/pac.py --space policy_space form          # answer questions, get the report
python3 scripts/pac.py --space policy_space ask "pur=std hr=ins"   # next question as JSON
python3 scripts/pac.py --space policy_space lookup "chatbot that denies insurance claims"
python3 scripts/pac.py --space policy_space categories            # the case map (a tree of categories)
python3 scripts/pac.py --space policy_space fit "pur=cyber auth=own"   # which category a code fits
python3 scripts/pac.py --space policy_space report "a: @support-bot disc=y & b: pur=cyber > c: @robot-shared"   # cases at the same time (&) and step by step (>)
python3 scripts/pac.py --space policy_space report "act=biz pur=std hr=ins scope=ind fav=part"
python3 scripts/pac.py --space policy_space check "@support-bot"
python3 scripts/pac.py --space policy_space panel "act=biz pur=std hr=ins scope=ind rev=n"
python3 scripts/pac.py --space policy_space validate   # schema + conformance tests
```

## Develop

```sh
python3 -m pytest plugins/policy-action-check/tests
claude plugin validate plugins/policy-action-check
```

Tests use the synthetic bundle in `tests/fixtures/sample-bundle/` at the repository root (its codes,
such as `@support-bot`, are the ones in the examples above). See [CONTRIBUTING.md](CONTRIBUTING.md).

This is a structured self-check, not legal advice.
