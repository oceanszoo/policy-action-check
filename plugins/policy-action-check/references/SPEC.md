# Policy action bundle format, version 1.6

A bundle is a folder of plain JSON files and a generated Markdown guide. Any software can read it to **generate** action codes
for a policy and **judge** them, without Claude. This document is the contract. Version 1.x
changes are backwards compatible; a reader must reject a different major version.

**A bundle needs no model to use.** A model may help to *build* or *refine* a bundle from policy text and
history, but generating a code (a question form, or keyword lookup) and judging it (verdicts,
must-do, unknowns, fixes, next steps, the report) are fully specified here and run from the files
alone. Two readers given the same bundle and the same answers produce the same output.

```
form: people click choices (+ optional notes)
  │  choices only ──▶ vector G (the code): every objective condition
  │  notes ─────────────────────────────────────────────┐ carried along, shown, never judged
  ▼                                                     │
G placed in the space; each term is a region            │
  ▼                                                     ▼
where G sits ──▶ suggestion: next_actions, verdict, must-do, unknowns, fix ──▶ report (+ notes)
```

**Changes in 1.6**: typed clause references and a glossary; deterministic reference lint,
context lookup and self-exam; bundled source terms and exact citations in results and guides.
Bundles from 1.5 and earlier remain readable without embedded texts.

**Changes in 1.5**: deterministic refinement and guide generation. The build-time work files
`work/gaps.json`, `work/refine_patch.json`, and `work/refine_log.jsonl` are not part of the
portable bundle; readers ignore them. The manifest may record refinement rounds in
`refined`. A generated `BUNDLE.md` ships with the bundle and has a digest in the manifest.
Map fit may ignore out-of-scope keys only if the policy judgment stays identical.

**Changes in 1.4**: generator `categories` (required from 1.4), a tree as deep as the material needs, and
dimension `under` for folders nested inside other folders, with the generator page that starts
from them ("Generator page") and the self-exam check `map_fit`; cases that happen at the same time, written `a & b`, next
to cases that happen step by step, `a > b`; flow results carry `stages`; skipped facts give
every possible result (`possible`, "Possible results").

**Changes in 1.3**: the self-exam ("Self-exam") and its fields: `bundle.json` `exam`,
generator `outcome_map` and `exam`, judge `not_encoded`, record `disagreement`.

**Changes in 1.2** (a 1.0 or 1.1 bundle is still valid; 1.2 bundles must pass the 1.2 checks):
- every dimension has a `question`; the question form is specified exactly ("Question form");
- generator `lexicon` (typed words → candidate tokens by plain phrase matching) and
  `subjective_terms` (the words a reader refuses);
- `records.jsonl` (past records as codes) joins the bundle, for similar records;
- judge rules may carry a `title`, and `judge.json` may carry `texts`; the report layout is fixed
  ("Report");
- next-step suggestions are a lookup in `transitions`, specified exactly;
- **notes**: free text a person adds to a choice or to the whole action. They are carried to
  the output and the report, and never affect anything else (see "Notes").

**Changes in 1.1**:
- multi-valued dimensions (`"multi": true`, written `pur=cyber+fraud`);
- generator `limits` (a size budget for the space);
- judge output adds `facts` (where each value came from), `requirements` (binding obligations)
  and `unknown_facts` (unstated facts that decide a rule), kept apart;
- `work/scope.json` areas carry `source_terms` and their own `question`.

```
<bundle>/
  bundle.json         manifest
  generator.json      how to write action codes        (format "pac-generator")
  judge.json          how to judge an action code      (format "pac-judge")
  conformance.jsonl   test vectors every implementation must pass
  records.jsonl       past records as codes, optional (1.2)
  BUNDLE.md           generated guide, required from 1.5
  work/               pipeline working files (not part of the contract)
```

## bundle.json

```json
{"format": "pac-bundle", "version": "1.2", "name": "my-terms",
 "policy": "https://example.com/terms",
 "files": {"generator.json": "<sha256>", "judge.json": "<sha256>", "conformance.jsonl": "<sha256>"},
 "created": "2026-11-18T05:40:00+00:00", "spec": "<url of this document>"}
```

Readers should verify the digests.
Refreshing a legacy manifest does not promote its generator or judge to 1.6. New builds
declare version 1.6 in the generator and judge before writing the manifest.

The exam badge may include `checks` (ordered `id`, `score`, `threshold`, `passed`, and
`skipped` fields), so the guide can render every check without running the exam.

Optional `refined` is a list of `{date, round, focus, ops}` entries in application order;
`ops` maps each applied operation name to its count.

## BUNDLE.md (1.5)

`pac.py guide` renders `BUNDLE.md` in the bundle root. `guide --check` exits 1 if it is absent
or stale, without rewriting it. Identical bundle data produces identical UTF-8 bytes with LF
line endings and exactly one trailing newline. The only date added to the guide is the exam
date already saved in `bundle.json`. `write_manifest` renders the guide and lists its SHA-256
in `files`; `validate` detects a stale guide or a missing guide on format 1.5 or later.
Older bundles need not have the guide. The offline reader accepts it, verifies its digest
and displays it verbatim via **Guide** in a details disclosure.

The document starts with agent-skill front matter (`name: pac-<bundle name>` and a description
of checking an action against the policy without a model). The heading is the policy name
followed by “: policy action bundle”. The summary lists the policy URL, format, self-exam badge
and stored exam date, and the counts of categories, features and rules. The remaining sections
are ordered as follows:

1. **How to use**: the offline web app; sample `report`, `categories`, `fit`, `check` and
   `lookup` CLI usage; the `key=value`, `a+b`, `&` and `>` forms.
2. **Case map**: depth-first nested list with category name, path, description, preset and
   catch-all markers.
3. **Features**: dimensions in file order, with key, name, question, values and meanings,
   default, applicability (`under`), fixability and evidence.
4. **Rules**: table of ID, title, kind, next step and source. From 1.6 it also lists term IDs,
   followed by **Terms** with each ID, section, class, excerpt or labelled summary, and links,
   in ID order.
5. **Examples**: first 40 conformance rows in file order, with code, verdict and violated rules.
6. **Saved codes**.
7. **Not encoded**: clause and reason; omitted when empty.
8. **Self-exam**: table of check, score, threshold and pass status; followed by the disclaimer
   from judge texts or `DEFAULT_TEXTS`.

## generator.json

| field | type | meaning |
|---|---|---|
| `format`, `version` | string | `"pac-generator"`, `"1.2"` |
| `policy` | object | `name`, `source`, `effective` |
| `grammar` | string | `"pac-code-1"` (below) |
| `dimensions` | array | the coordinates, in display order (below) |
| `ask_order` | array of keys | the order the question form asks in; keys not listed follow in display order |
| `categories` | array | the case categories the generator page offers first: `[{id, name, description?, preset, features}]` (1.4, see "Generator page") |
| `lexicon` | array | `[{phrase, token}]`: typed words that suggest a token (1.2, see "Keyword lookup") |
| `outcome_map` | object | outcome label in `records.jsonl` → list of verdicts that agree with it (1.3, used by the self-exam) |
| `exam` | object | optional threshold overrides for the self-exam, with a `reason` (1.3) |
| `subjective_terms` | array of words | opinion words a reader refuses as keys or values (1.2). Absent: the reference list in `pac.py` (`DEFAULT_SUBJECTIVE`) |
| `saved_codes` | object name → code | reusable codes (macros), used as `@name` |
| `constants` | object key → value | facts fixed for this user, applied under every code |
| `scope` | object | `areas`: `[{area, examples, gap, decision, match?}]`; `decision` ∈ `in_scope_examples`, `terms_only`, `out_of_scope`; `match` is a condition (see judge) selecting the area's actions; `unmatched_history_share` |
| `transitions` | object step → {next step → count} | habits learned from history. A reader suggests the next steps after `step` by count, highest first, ties by name, with share = count / total |
| `limits` | object | optional: `max_dimensions` (default 24), `max_rules` (default 60), and `reason` (required when the budget is raised). A space over budget fails validation |
| `tool_map` | object | `base`: a code; `mappings`: `[{tool: regex, input?: regex, code}]`; maps agent tool calls to codes |

**Dimensions are objective conditions only.** Each one is a fact that can be checked from
evidence: what the action does, what exists, who agreed, what was recorded. Opinions,
intentions and risk ratings ("low risk", "I think it's fine", "for good reasons") are never
dimensions or values. Each dimension says what evidence settles it.

A **dimension** is:

```json
{"key": "rev", "name": "qualified human review before delivery or use",
 "values": {"y": "yes", "n": "no"},
 "evidence": "review log: who reviewed, their qualification, and that they could change the output",
 "default": null, "fixable": true, "frequency": {"y": 41, "n": 7}}
```

- `key`: matches `[a-z][a-z0-9_]{0,11}`.
- `values`: value codes, each mapped to its meaning.
- `question` (required from 1.2): the plain question the form asks for this key.
- `default`: used when the key is unset. Only context dimensions may have one.
- `multi` (1.1, optional): the key holds a set of values, written `a+b`, for an action that does
  several things at once. Value codes use only letters, digits, `_`, `.` and `-`. A multi-valued
  dimension describes the action, so it is never `fixable`.
- `evidence`: what record or observation settles the value. Required.
- `fixable`: whether a fix may change this key. Only controls the user can add (review,
  disclosure, consent, authorization, safety controls) are fixable. The nature of the action
  (what it does, its subject, its actor) is not.

## Action code grammar `pac-code-1`

```
flow    := stage ( " > " stage )*         (step by step)
stage   := action ( " & " action )*       (at the same time, 1.4)
action  := [ name ":" ] token ( " " token )*
token   := key "=" values | key "=?" | "@" saved_code | "date=" YYYY-MM-DD
values  := value ( "+" value )*        (more than one only on a multi-valued key)
```

- Tokens apply left to right, so later tokens override earlier ones. `@name` expands that saved
  code in place.
- A key that is absent, or `key=?`, is **unset**.
- `date=` sets the judging date. Without it, readers use the configured date or today.
- The canonical form lists the set keys in dimension order, and the members of a multi-valued
  key in the order of its `values`, without repeats. Two codes with the same canonical form are
  the same vector.
- An unknown key, an invalid value or an unknown saved code is an error.
- A subjective token (a judgement, opinion or intention such as `risk=low` or `rev=fine`) is an
  error. A reader must refuse it and ask for the objective condition instead. A reader should
  suggest the nearest valid token, and must not judge a code with errors.

## judge.json

| field | type | meaning |
|---|---|---|
| `format`, `version` | string | `"pac-judge"`, `"1.2"` |
| `semantics` | string | `"kleene-3v-1"` (below) |
| `verdicts` | array | the verdict names in use |
| `fix` | object | `fill_cost`, `change_cost`, `max_changes` |
| `rules` | array | the rules (below) |
| `notes` | array of strings | guidance that isn't scored |
| `not_encoded` | array | normative clauses deliberately not turned into rules: `[{clause, reason}]` (1.3) |
| `texts` | object | optional report wording (1.2): `verdict` (verdict → sentence), `headings` (section → heading), `disclaimer` |
| `terms` | object | from 1.6, clause ID → `{section, class, source, refs, text or summary}` |

Each term contains exactly one of `text` (an exact excerpt of the source) or `summary`
(the builder's own words). The chosen field must be a non-empty string. Validation rejects
a term with both or neither field, and rejects `text` starting with `Summary`
(case-insensitive). It emits one warning per summary-only term. Citation results retain
the chosen field; reports, the reader and the bundle guide label a summary
**Summary (not a quote)** and do not wrap it in quotation marks.

A **rule** is:

```json
{"id": "O-hr-review", "kind": "obligation",
 "source": "High-risk use case requirements: qualified human-in-the-loop",
 "effective_from": "2026-11-12",
 "when": {"all": [{"dim": "hr", "not_in": ["none"]}, {"dim": "scope", "in": ["ind"]}]},
 "require": {"dim": "rev", "in": ["y"]},
 "exceptions": []}
```

- `source_terms`: IDs of the source clauses. From 1.6 each rule needs at least one, and any
  IDs supplied on an exception must also appear in `judge.terms`. Normative clauses determine the
  rule; descriptive clauses may supply definitions, but never rules on their own.
- `title` (optional, 1.2): a short name for reports.
- `next_step`: the instruction a person follows to satisfy the rule. For a prohibition this is
  "do not proceed". Required.
- `kind`: `prohibition` or `obligation`. Obligations have `require`. Prohibitions may have
  `exceptions`, a list of conditions.
- A **condition** is one of `{"dim", "in": [...]}`, `{"dim", "not_in": [...]}`,
  `{"all": [...]}`, `{"any": [...]}` or `{"not": cond}`.
- A rule is active when `effective_from` ≤ the judging date.

`pac.py embed-terms` copies rule and exception sources from `work/terms.json`, with one level
of modifying backlinks and outgoing `modifies`, `incorporates`, `overrides`, `subject_to` and
`uses_definition` links. It writes terms in clause ID order, before `manifest`. The embedded
links retain their IDs and kinds; a null target needs an external source. The command copies
`summary` as well as `text` when present, and the resulting terms must follow the one-field
rule. From 1.6 validation requires a valid quote or summary for each cited or linked ID.
For older bundles, missing citations produce a warning rather than a validation error.

### Semantics `kleene-3v-1`

1. **Set up.** Before evaluating, the vector is
   `defaults ∪ constants ∪ code features`, where later sources win.
2. **Conditions** evaluate to true (1), unknown (½) or false (0):
   - a leaf on an unset key is unknown;
   - on a set key, `in` is true when **any** member is listed, and `not_in` is true when **no**
     member is listed (a single value is a set of one);
   - `all` is the minimum, `any` is the maximum, and `not` is 1 − x.
3. **Prohibitions.** Let `w = when` and `x = max(exceptions)`, with 0 if there are none. Then
   `v = min(w, 1 − x)`:
   - `v = 1` → `violated`;
   - `v = 0` → `satisfied` if `w = 1`, else `not_applicable`;
   - otherwise → `open`.
4. **Obligations.** Let `w = when` and `r = require`:
   - `w = 0` → `not_applicable`;
   - `w = 1` → `satisfied` / `violated` / `required_next` for r = 1 / 0 / ½;
   - `w = ½` → `satisfied` if r = 1, else `open`.
5. **Verdict**, taking the first that applies:
   1. `invalid_code` if the code has errors;
   2. `violates` if any rule is violated;
   3. `compliant_if_requirements_met` if any is required next;
   4. `needs_info` if any is open;
   5. `compliant` otherwise.
6. **Fix.** The cheapest set of key changes, at most `max_changes`, that leaves no rule violated
   or required next. Only fixable keys that appear in those rules may change. Each change
   costs `fill_cost` if the key was unset, or `change_cost` if it was set. Report every option
   at the minimum cost, up to 5, ordered by their changes in dimension order.
7. **Invalid codes.** A code with errors gets verdict `invalid_code` and nothing else: empty
   rule lists, no fix, no requirements, no unknown facts, and one `fix_code` action.
8. **Flows.** In `a > b > c` (step by step) and `a & b` (at the same time) each action is
   judged on its own: actions at the same time don't share facts, so facts that belong to one
   action go in one code. The result carries `stages`: the 0-based action indexes of each stage,
   in order, e.g. `[[0, 1], [2]]` for `a & b > c`. The flow's verdict is the first
   of `invalid_code`, `violates`, `compliant_if_requirements_met`, `needs_info`, `compliant`
   that any action has. Its `next_actions` are the actions' own, in order, each tagged with its
   `action_index` (0-based), leaving out `proceed`; if none remain, one `proceed`.
9. **Scope warnings.** For every scope area with a `match` that is true (1) under the full vector
   and `decision` `out_of_scope` or `terms_only`: `"<area>: <decision>"`, in area order.

### Judge output

The judge's input is a code made of objective conditions. Its **primary output is
`next_actions`**: an ordered list of what to do next. Statuses and verdicts are context for it.

| kind | when | action text |
|---|---|---|
| `stop` | a prohibition is violated and no fixable key can leave it | the rule's `next_step` |
| `do` | the minimal fix changes or fills a key | the `next_step` of the rules that key satisfies, plus the code change |
| `establish` | an unset key decides an open rule | "Establish <dimension>", plus its `evidence` |
| `proceed` | nothing is violated, required or open | "Proceed" |
| `fix_code` | the code has errors (including subjective tokens) | correct the code |

Order: if any `stop` applies, only the `stop` actions (rule id order). Otherwise one `do` per
change of the first minimal fix, then one `note` listing the other equally small fixes (if
any), then one `establish` per entry of `unknown_facts`, in that list's order; `proceed` only
when the list would otherwise be empty.

**Next steps.** The action's step is its name (`name:` before the tokens), else the value of a
`step` key if the bundle has one. `next_steps` lists the steps that follow it in `transitions`
as `{step, count, share}`: count highest first, ties by name, top 3, share rounded to 2
decimals.

Alongside `next_actions`, the judge reports three lists, kept apart so a reader never mistakes
one for another (1.1):

| field | each entry | meaning |
|---|---|---|
| `facts` | `key`, `value`, `from` (`code`, `@saved_code`, `constant`, `default`), `assumed` | every value the judge used, and where it came from. `assumed` is true for defaults: confirm them if they matter |
| `requirements` | `rule`, `status`, `next_step`, `keys`, `source` | obligations the action has triggered and not yet met (status `required_next` or `violated`), in the order: required_next by id, then violated by id. They bind: meeting them is not optional. A triggered obligation that is already met is not listed |
| `unknown_facts` | `key`, `name`, `evidence`, `decides` | every key that is unset in the full vector (after defaults and constants) and appears in an `open` rule; `decides` lists those rules by id. Sorted by key. Establish them; never assume them |
| `possible` | `verdict`, `violated`, `required_next`, `when`, `exact`, `count`, `of`, `examples` | 1.4: the regions the action could land in once the `unknown_facts` are known (below). Empty when there are none |

**Possible results (1.4).** A person may skip facts they can't confirm. Then the judge lists
every region the action could land in. Let `K` be the keys of `unknown_facts`, in that order,
each with its single values in value order. If the product of their value counts is at most 256,
try every combination (the last key varies fastest); otherwise try each key alone, value by value,
with the others unset. Judge each try like a code (defaults and constants apply) and group the
tries by (`verdict`, `violated`, `required_next`), in order of first appearance, then stably by
verdict order. For each group: `when` maps each key to the values that occur in its tries (value
order); `exact` is true when all combinations were tried and the group's tries are exactly every
combination of `when`; `count` is its number of tries, `of` the number of all tries; `examples`
is empty when `exact`, else the first 3 tries as canonical codes. The report line is
``If `k=a` or `k=b` and `j=c` (count of of): <verdict sentence>``, or ``If one of `code`, ...
(+n more) (count of of): ...`` when not exact, followed by ` Violates: <titles>.` and
` Must do: <titles>.` when not empty.

## Question form (1.2)

The generator as a form, so a person can write a code by answering questions. State: the answers
so far, as a code. `key=?` records "don't know": the key counts as asked and stays unset.

1. Let `F` be `constants ∪ answers` (defaults are **not** applied while asking, so context
   questions still get asked).
2. Go through the keys in `ask_order`, then the remaining keys in display order. Skip a key that
   is answered, asked, or a constant.
3. A key **decides** an active rule when setting it to each of its values (each single value,
   for a multi-valued key, plus all its values at once, so a rule needing two members together
   is found) gives the rule more than one status under `F`. A key that appears
   only in a rule's `exceptions` or `require` decides it only when the rule's `when` is true
   under `F`, or when `when` is unknown and no unasked key of `when` decides the rule (the
   condition can't become clearer): conditions are asked before exceptions and requirements.
4. The next question is the first key (in that order) that decides an active rule whose `when` is
   already true under `F`; if there is none, the first key that decides any active rule. Show its
   `question`, its values with their meanings, and its suggested answer: the dimension's
   `default`, else its most frequent value in `frequency` (ties by value order), else none. A
   multi-valued key allows several answers, written `a+b`. For "don't know", say that the
   default is assumed when there is one.
5. If no key decides a rule on its own, take the first key (same order) that appears in a rule
   whose status under `F` is `open` or `required_next` (for a key only in `exceptions` or
   `require`: when that rule's `when` is not false). This catches keys that only matter together.
6. When neither step finds a key, the form is done: judge the answers as a code.

## Generator page (1.4)

The generator as three steps, all without a model. Only the code it builds is judged.

1. **Category.** `categories` is a **case map**: a tree of categories, as deep as the material
   needs. A branch whose cases differ in more facts gets `children`; a simple kind of term stays
   flat. The person opens categories like folders and picks any node. A node's preset is its
   ancestors' presets with its own applied last; its features are its ancestors' followed by its
   own. A bundle without categories gets one catch-all node, `any`, whose features are every key
   in question order (`ask_order`, then display order).
2. **Features.** The node's features are folders, one per key, in that order; opening one shows
   its values to choose (several for a multi-valued key). A dimension with `under` (a condition
   on one other key, `{dim, in|not_in}`) is a folder **inside** that key's folder, shown once the
   condition is true under the choices so far; nesting may go several levels. Keys the node
   doesn't offer sit in an "Other features" folder. A folder left empty is skipped: the key is
   unset (its default, if any, is assumed), and the result lists the possible regions. Each case
   may have a name (`name:`) and notes.
3. **Relationship.** With more than one case, the person orders them and says, between each case
   and the one before it, whether it happens **at the same time** (`&`) or **after it** (`>`).
   The page writes the flow code and judges it as a flow.

A category node is `{id, name, description?, preset?, features, children?, catch_all?}`. `id`
matches `[a-z0-9][a-z0-9_-]{0,31}` and is unique among its siblings; a node's `path` is the ids
from the root joined by `/`. Top-level nodes need non-empty `features`; children may add none.
`preset` is a valid single-action code (may be empty) whose keys are all folders of the node
(its own or inherited, or nested under them given the preset's values). `catch_all: true` marks a
node (and its subtree) meant to hold anything, such as "Something else". Every key a rule uses
must be offered by some node, directly or nested.

**Map fit.** A code fits a node when the node is not a catch-all, every preset value agrees with
the code (for a multi-valued key, the preset's members are among the code's), and the node's
folders, with the nested ones the code opens, reach every key the code sets. `map_fit` of a code
is the path of the deepest node it fits (ties: first in depth-first order), or none.

An **out-of-scope key** is a set key whose dimension has an `under` condition that does not
evaluate to true on the code's features. Drop all such keys in each round and recheck the
remaining keys until none can be dropped. If this changes the features, judge the original and
the remaining core for the same date. Only when `verdict`, `violated`, `required_next`, and
`open` are identical does map fit use the core instead. Otherwise map fit returns none and
the case has an `out_of_scope_effect` gap. The original code and its judgment are not changed.

`fit_detail` returns `{category, dropped}`; `dropped` contains the removed keys in sorted order,
or an empty list if the drop was refused. The `fit` CLI omits `dropped` when empty. The map-fit
exam detail includes `fits_after_drop: [{code, dropped}]` for codes whose core fits after a
successful drop.

## Notes (1.2)

Choices build the vector; notes are only for people to read. A note is free text attached to a
dimension key (a note on that choice) or to `_action` (a note on the whole action). Notes are not
part of the code, so they can't change the canonical form, any rule status, the verdict, the
fix, `next_actions` or similar records, and a reader must not parse them. Because they are never
judged, notes may hold anything, including context or opinions; the subjective-input refusal
applies to choices only. The judge output echoes them as `notes` (`{key or "_action": text}`,
unknown keys and empty notes dropped), and the report shows them last, under a heading that says
they were not used in judging.

## Keyword lookup (1.2)

Typed words suggest tokens, by plain matching only. Lowercase the text and each `phrase`, and
split both into runs of letters and digits. A phrase matches where its words appear in order and
next to each other. Return each matching token once, at its first match, ordered by position
then token. These are suggestions: the person confirms them, and nothing is set by a match
alone.

## Report (1.2)

A reader renders the judge output in this fixed order, using `texts` where given and the
reference wording otherwise (`DEFAULT_TEXTS` in `pac.py`). Empty sections are left out.

1. **What to do next**: the `next_actions`, numbered, each with its code change;
2. **Verdict**: the verdict sentence; **Action**: the canonical code and its plain reading;
3. **Errors**; **Violates** (rule `title [id]`); **Must do** (`requirements`, with their
   `next_step`); **Still unknown** (`unknown_facts`, with evidence); **Possible results**
   (`possible`); **Assumed** (facts from
   defaults); **Smallest fix**; **Usually next** (transitions); **Similar past records**;
   **Scope warnings**; **Notes**;
4. the disclaimer.

From 1.6 results include `terms`, grouped by rule status: violated, required next, open, then
excepted. Rules are ordered by ID within each status. An excepted prohibition has a true
`when` and at least one true exception. Each citation records its ID, role, section and exact
text; roles are `rule`, `exception`, `definition`, `incorporated`, `overrides` and `subject_to`.
An exception citation has boolean `applies` when exactly one exception condition maps to
that clause and the condition is known. Each next action lists its rule-role IDs in `terms`.
Invalid codes have no citations. Old bundles without embedded terms use the rule's original
source label with null ID and text.

The report lists source terms under their rules, with section and ID. Exact excerpts
use `> "…"`; summaries use `> Summary (not a quote): …` without quotation marks. Exception citations
say whether they apply when known. Definitions are quoted once under **Terms used**.
Next steps end with their cited IDs. Text is neither wrapped nor truncated. The offline
reader displays policy text as plain-text blockquotes.

## records.jsonl (1.2)

One past record per line: `{"id", "code", "outcome", "step"?, "disagreement"?}`. `disagreement`
is the builder's written reason why the recorded outcome looks wrong. Codes use the bundle's own
grammar; raw text from the history is not included. Similar records are the ones with the
smallest distance: 1 for each shared key with a different value, plus 0.5 for each key set in
only one of the two, over records sharing at least one key; ties by id; the top 3.

## Self-exam (1.3)

`pac.py exam` is the reference self-exam. It is plain computation over the bundle files, written
to `work/exam.json`, and summarised in `bundle.json` `exam` by `pac.py manifest` (only when the
exam was taken on exactly the current file digests; otherwise `exam` is null).

| check | passes when (default threshold) |
|---|---|
| `validate` | schema and conformance tests pass |
| `terms_coverage` | every `high`/`medium` normative clause of `work/terms.json` outside `out_of_scope` areas is in some rule's `source_terms`, or in judge `not_encoded` `[{clause, reason}]` (1.0) |
| `references` | every cited clause's incoming `modifies`, `overrides`, `subject_to` and outgoing `incorporates`, `overrides`, `subject_to` links are included in the same rule or reasoned `not_encoded`; unresolved links likewise need a reasoned disposition (1.0) |
| `rule_coverage` | every rule is `violated` or `required_next` in some conformance vector, and every obligation or rule with exceptions is `satisfied` in some vector (1.0) |
| `replay_agreement` | records judged against `generator.json` `outcome_map` (outcome → acceptable verdicts) agree (0.9), and every disagreeing record carries a `disagreement` reason |
| `replay_decisive` | records whose verdict is not `needs_info` (0.8) |
| `mutation_score` | mutants caught by some conformance vector (0.8). Mutants per rule: flip each `when` leaf (`in` ↔ `not_in`), drop each leaf of an `all`/`any` in `when`, drop each exception, negate `require`. A mutant is caught when, for some vector, the rule's status changes between the groups violated / required_next / open / other |
| `map_fit` | (1.4) records and conformance codes (without errors, with at least one key) that fit some category below the catch-all (see "Map fit") (0.8). Lists failures when below threshold; the detail lists fits after dropping out-of-scope keys |
| `properties` | over 300 generated codes: same result twice; notes change nothing but `notes`; filling a key without a default never moves a rule out of violated, nor out of satisfied/not_applicable (met or not applying); the first minimal fix leaves nothing violated or required; the form answered from the code reaches the same verdict and rule lists as the code |

A check with no input (no `terms.json`, no records) is skipped. `generator.json` `exam` may
change thresholds, with a `reason`. **Readers should refuse a bundle whose `exam` is null or
not passed**, or at least warn.

`references` uses `judge.terms` if the work file is absent. It skips a terms file without refs
or glossary and ignores `uses_definition`, which informs vocabulary, not rule coverage.
For a particular unresolved link, `not_encoded` may name the clause and exact link phrase
in `ref` along with a reason; a clause-only disposition covers the whole clause.

`bundle.json` `exam`: `{"passed", "date", "engine_spec", "scores": {check: score}, "failed": [check]}`.

## conformance.jsonl

One test per line:

```json
{"code": "@support-bot disc=n", "date": "2026-11-18",
 "expected": {"verdict": "violates", "violated": ["O-chatbot-disclosure"], "required_next": [], "open": []}}
```

The rule id lists are sorted. An implementation passes when every line matches. The reference
implementation is `scripts/pac.py` (`pac.py validate` runs these tests).

## work/terms.json (pipeline file)

This is the output of the term filter. Only `normative` classes may become rules.

```json
{"source": "https://example.com/terms",
 "clauses": [
  {"id": "c014", "section": "Sensitive uses", "class": "obligation",
   "text": "A qualified person must review the recommendation ...",
   "signals": ["must"], "binds": "deployer", "confidence": "high"},
  {"id": "c015", "section": "Sensitive uses", "class": "permission_exception",
   "text": "Review is not required before a wholly favourable decision ...",
   "modifies": "c014", "confidence": "high"},
  {"id": "c002", "section": "Introduction", "class": "description",
   "text": "These terms explain how the service may be used ..."}]}
```

The classes are:
- normative: `prohibition`, `obligation`, `permission_exception`, `condition_scope`;
- descriptive: `definition`, `description`, `example`, `procedure`.

Normative clauses carry a `confidence`. Advisory wording ("should") is `low` unless the policy
says it is enforced.

1.6 clauses may include `refs`, a list of
`{"to": "c014", "kind": "modifies", "text": "<exact reference phrase>"}` links.
A null `to` requires `external` (a document or section name) or `reason` (an
unresolved target). The legacy clause-level `modifies` field is read as a link.
The optional top-level `glossary` holds
`{"term": "...", "defined_in": "c007", "aliases": ["..."]}` entries; the target must be a
`definition` clause.

| Kind | Meaning |
|---|---|
| `uses_definition` | Uses a term defined by another clause |
| `incorporates` | Pulls another clause's content into this one |
| `modifies` | Provides an exception or condition to another clause |
| `overrides` | Takes precedence over another clause |
| `subject_to` | Is limited by another clause |
| `none` | Waives a deterministic signal match with a reason; not a link |

`pac.py terms-lint` emits `{errors, warnings}` and exits 1 on errors; the filter validation
stage also runs it. It checks known targets, phrases (case-insensitive with collapsed
whitespace), whole-word glossary uses, unlinked normative signal phrases, cycles consisting
only of incorporation links, and valid definition targets. It warns about unused definitions,
repeated quoted or capitalized concepts missing from the glossary, and unresolved references.

`pac.py terms-context <clause-id>` shows the requested clause, transitively linked clauses
with paths, and backlinks including exceptions. It ignores `none`. Builders and refiners use
it before encoding a clause. No model is needed for either command.

## work/scope.json (pipeline file)

```json
{"areas": [{"area": "Physical actions", "examples": 0, "gap": true,
            "source_terms": ["c031", "c032"], "recommended": "out_of_scope", "decision": "ask",
            "question": "Do you use Claude to control physical equipment, such as robots or vehicles? (recommended: out of scope)"}],
 "unmatched_history_share": 0.04,
 "constants": {"act": "biz"},
 "questions": ["Do you control physical equipment with Claude? (recommended: out of scope)"]}
```

`decision: "ask"` means the user hasn't answered yet. Each gap area is one rule family: it lists
the normative clauses it covers in `source_terms`, and when it is `ask` it carries its own
specific `question`. `questions` (a flat list) is optional.


## Refine (1.5)

This is an optional build-time loop; normal bundle use does not require it. The only model step
is proposing the patch. The commands below are deterministic and need no network.

### gaps

`pac.py gaps [--focus TEXT] [--write]` runs the exam without writing exam state. It always
prints a JSON object with `bundle`, `focus` (text or null), `focus_keys`, `exam_passed`, and
`gaps`, plus `files` (the bundle digests at scan time). `--write` also saves that object to `work/gaps.json`. Each item has `kind`, `id`,
`keys` (affected dimension keys), `detail` and `evidence`. Emit one item per failure or miss,
grouped in the following order:

| Kind | Source | Evidence |
|---|---|---|
| `uncovered_clause` | `terms_coverage` | Clause ID, text, class |
| `unresolved_reference` | `references` | Rule ID, source clause, linked clause, kind and phrase; keys are the rule's dimensions |
| `untested_rule` | `rule_coverage` | Rule ID, missing status |
| `replay_disagreement` | `replay_agreement` | Record ID, code, recorded outcome, verdict |
| `undecided_record` | `replay_decisive` | Record ID, code, unknown fact keys |
| `surviving_mutant` | `mutation_score` | Rule ID, mutant label |
| `property_failure` | `properties` | Failing code, property |
| `map_miss` | Every record or conformance code with no map fit after any safe drop, even if the map check passes | Code, nearest category for the core |
| `out_of_scope_effect` | Dropping out-of-scope keys changes the judgment | Code, dropped keys, original and core results; affected keys include their `under` dimensions |

`nearest` is `{path, preset_conflicts, unreachable}` for the non-catch-all node minimizing
the sum of preset conflicts and already-set keys it cannot reach. Both conflict and
unreachable lists contain dimension keys. Ties go to depth-first order. A focus uses the
keys returned by keyword lookup and keys whose dimension name or value meaning contains a
lowercase whole word of the focus. Keep items sharing a key, plus map misses with a name on
their nearest path matching a focus word. If nothing matches, print the unmatched words and
exit 2.

### refine_patch.json and apply-patch

The refiner writes only `work/refine_patch.json`, with `focus`, `based_on` (SHA-256 of the
exact gaps file bytes), and an ordered `ops` array. Each operation has an `op`, a `reason`,
and a `gaps` list of gap IDs. Operations are additive except for explicitly guarded rule
revision.

| Operation | Additional fields and effect |
|---|---|
| `add_category` | `parent` path (empty for top level), `node` (may have children); append with a new sibling ID |
| `set_under` | `dim`, `under`; set on a dimension that has none |
| `add_value` | `dim`, `value`, `meaning`; append with frequency zero |
| `add_dimension` | `dimension` (full object with question and evidence); append within limits |
| `add_rule` | `rule` with source, source terms, next step and title; append |
| `add_exception` | `rule`, `exception`; append to the rule's exceptions |
| `add_vector` | `line` with code and expected result; append to conformance |
| `add_lexicon` | `entry`; append |
| `add_saved_code` | `name`, `code`; require a new name |
| `not_encoded` | `clause`, optional `ref` (the exact phrase for a reference-only disposition); append the disposition using the operation reason |
| `record_disagreement` | `record`; set its disagreement to the operation reason |
| `revise_rule` | `rule`, `changes` limited to when, require, exceptions, next_step, title and source_terms; `expected_changes` lists code and expected result |

`pac.py apply-patch` refuses a stale gaps digest with “gaps changed, re-run gaps”. If the current bundle
digests differ from `files` in that report, refuse with “bundle changed since gaps, re-run gaps”.
Back up the bundle
files in `work/refine_backup/<n>/`. Apply operations in order to copies. Rejudge all existing
conformance codes; a change in verdict, violated, required_next or open must exactly match a
`revise_rule.expected_changes` entry. Refuse unlisted changes and print changed lines; update
the listed expected results. Validate before committing any changes. Then write, run the exam
with writing enabled, and regenerate the manifest and guide. Append a round entry to
`bundle.json` `refined` and a `{round, date, focus, ops, gaps_before, gaps_after, exam_passed,
scores}` line in `work/refine_log.jsonl`; gaps and ops are counts by kind. Refusal leaves
bundle files unchanged. With `--dry-run` perform the digest, ordered operation, conformance
and validation checks without writes or backups; print per-file changes and whether the gaps
shrank.
