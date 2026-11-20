# Method

## The idea

**The plugin pre-cooks; the bundle runs alone.** A model reads the policy and the history once,
at build time. What comes out (generator + judge) is used with no model at all: a question form
or keyword lookup writes the code, and the engine judges it and fills a fixed report. People
build the vector only by clicking choices; they may add notes, which travel with the result for
people to read and are never judged. Anything a
model would have to decide at use time is precomputed into the bundle or left out.

An action is a point in a small feature space that **you build**, and every dimension in it has a
name and a meaning. Policy terms are regions in that space. Where an action sits tells you
which terms it violates, which requirements it has triggered, and the smallest move that makes
it compliant.

This looks like an embedding space, but it isn't one. Text similarity can't decide compliance:
"monitor staff with consent" and "monitor staff without consent" read almost the same, but only
one of them is allowed. Similarity is used only to propose codes from words and to find
similar past records.

**An action code is the readable surface of a vector.** Each key is one coordinate. A missing
key is an unset coordinate, so a code stands for a region of possible vectors, not a single
point. Rules are regions. Checking is three-valued over unset coordinates. A fix is a move to
the nearest vector outside every violation region, written as a code diff. Similar past records
are nearby vectors.

**Inputs are objective conditions. The output is what to do next.** Every dimension is a fact
that evidence can settle: what the action does, what exists, who agreed, what was recorded.
Opinions, intentions and risk ratings are refused. The engine rejects them, and the encoder
turns them into a question about the underlying fact. The judge leads with `next_actions`:
stop, do (with the code change), establish a missing fact, or proceed.

## Pipeline

```
policy ─▶ 0 filter ─▶ real terms ─┐
history ───────────────────────────┴▶ 1 scope ─▶ 2 build ─▶ 3 verify ─▶ bundle (generator + judge)
                                        │ asks the user only about gaps
action (words or code) ─────────────────▶ 4 encode ─▶ 5 judge (engine) ─▶ outcome
```

## 0. Term filter

Policies mix real terms with description. Split the text into clauses, one rule-like statement
each, and classify them:
- **Normative**, which may become rules: `prohibition`, `obligation`, `permission_exception`
  (an exception or permission that modifies another clause) and `condition_scope` (when or to
  whom something applies).
- **Descriptive**, which never becomes a rule: `definition`, which feeds the code vocabulary;
  `description`, the background; `example`, which helps pick values; and `procedure`, such as
  enforcement or contact details.

Advisory wording ("should", "we encourage") is low-confidence and is not a hard rule unless the
policy says it is enforced. Everything later works only from the normative clauses, and each
rule records the clause ids it came from.

## 1. Scope plan

Terms usually cover far more ground than the history does. Before building:

1. List the policy's areas, from the normative clauses only. Each area is one **rule family**
   (a prohibition group, an obligation domain, a special product type) and lists the clause ids
   it covers. Also note the actors, products, regions and dates the policy distinguishes.
2. Sort the history into those areas and count the examples per area. Also count records that
   fit no area: a high share can mean a missing or wrong policy.
3. An area with fewer than about 5 examples is a **gap**. For each one, recommend:
   - `in_scope_examples`: the user adds records;
   - `terms_only`: rules are built, but suggestions there are marked low-confidence;
   - `out_of_scope`: no rules, and actions there are reported as outside the scope, not as
     compliant.
4. Write one specific question per gap family, about that family's own facts ("Do you use
   Claude to control robots or vehicles?", not "Is this area in scope?"). Ask the user all of
   them in one message, each with a recommendation. Facts fixed
   for them (such as "always an API business") become `constants`. In a non-interactive run,
   apply the recommendations and say so.

## 2. Build

1. **Terms to rules.** Make each rule atomic:
   - *prohibition*: a region, with exceptions that take an action out of it;
   - *obligation*: `when … require …`.
   Give each a stable id, its source, and `effective_from`. Never invent a rule.
   Run `pac.py terms-context <clause-id>` for each clause first. Put `modifies` backlinks
   in a prohibition's `exceptions`, or narrow `when` or `require`. Put `incorporates`
   targets in `when`. Take dimension values and meanings from `uses_definition` targets.
   Express which rule wins an `overrides` or `subject_to` relationship in that rule's
   `when` or `exceptions`. Cite every clause used this way in `source_terms`.
2. **Rules to dimensions.** Each condition's variable becomes a dimension with a short key and
   short value codes. Phrase it as an objective condition with an `evidence` line. If a term
   hinges on a judgement ("significant risk"), find the objective facts the policy uses to
   decide it, or record the term as needing human assessment. Never make the judgement a
   value. Give every rule a `next_step`. Context dimensions may declare a `default` (for example `hr=none`).
   Requirement dimensions (review, disclosure, safety controls) never do, because unset means
   unknown. Only controls the user can add (review, disclosure, consent, authorization,
   safety controls) are `fixable`. The nature of the action is not.
   When an action can do several listed things at once (for example a purpose), make that one
   **multi-valued** dimension (`pur=cyber+fraud`), not one yes/no dimension per thing.
   **Keep the space small.** One dimension per variable the rules actually test; merge rules
   that share a region. The default budget is 24 dimensions and 60 rules. A bigger policy may
   raise it in `limits`, with a reason.
3. **History to dimensions.** Encode the records as codes. Add a dimension or value where a
   record can't be encoded, or where outcomes differ with no dimension explaining why. Record
   value frequencies.
4. **Check against history.** Judge every encoded record and compare with its recorded
   outcome. Report the disagreements: a missing rule, a rule that's too broad, or a wrong past
   decision. Never bend a rule to fit a bad past decision.
5. **Generator extras.** Add saved codes for common patterns, step transitions, constants,
   scope areas with `match` conditions, and a tool map for agent actions. For use without a
   model: a plain `question` per dimension, a `lexicon` of everyday phrases (with their word
   forms) that point to tokens, a short `title` per rule, and `records.jsonl` (past records as
   codes and outcomes, no raw text).
6. **Conformance vectors.** Write at least one per rule, covering both sides of each exception.

## 3. Verify

A second reader re-checks every rule against the policy text, checks the conformance vectors
against the text (not against the engine), and decides each history disagreement.

### Self-exam

Before a bundle ships, `pac.py exam` checks it with no model, the way ordinary software is
tested: coverage of terms, references and rules (like coverage.py), replay of past records against their
outcomes (golden tests), mutation testing of every rule (like mutmut or Stryker: a deliberately
broken rule must be caught by a test vector), and property tests over generated codes (like
Hypothesis). The result is stamped into `bundle.json` as a badge. A failing exam means the bundle
is insufficient: fix the bundle, not the thresholds.

## 4. Encode

With no model (the normal way to use a bundle): the question form (`pac.py ask`, `pac.py form`)
asks only the questions that still decide a rule, and keyword lookup (`pac.py lookup`) turns
typed words into suggested tokens. Inside Claude Code, a model may also propose a code from a
free description, as a convenience; the person confirms it either way.

From words, with a model: set only the keys the description supports, and prefer a saved code plus
overrides. If the action does several things, list them all on a multi-valued key. Keep each
fact's source: for every key, the user's own words that settle it. Show similar past records.
Never write `=n` for a fact that wasn't mentioned.

## 5. Judge

The engine (`scripts/pac.py`) is the only judge, so the same code always gets the same verdict.

| status | meaning |
|---|---|
| `violated` | the vector is inside the rule's violation region whatever the unset keys are |
| `required_next` | an obligation is triggered and its required key is unset |
| `open` | an unset key decides whether a prohibition applies |
| `satisfied` | the rule applies and is met, or an exception holds |
| `not_applicable` | the rule's region can't contain this vector |

The verdict is `violates` if anything is violated, else `compliant_if_requirements_met` if
anything is required, else `needs_info` if anything is open, else `compliant`. A code with
errors is `invalid_code`.

The judge also reports, kept apart: `facts` (every value used and where it came from: the code,
a saved code, a constant, or an assumed default), `requirements` (obligations the action has
triggered, which bind) and `unknown_facts` (unstated facts that decide an open rule).

**Fix.** The cheapest change (filling an unset key costs 0.5, changing a set key costs 1) that
leaves no violated or required rule, using fixable keys only. If only non-fixable keys
decide, there is no compliant variant.

**Live panel.** After each token: what is now violated, what is now required, what is still
open, and the next key with the value that keeps the action compliant and is most common in
history.

## Writing the outcome for a person

Lead with the verdict. Then give: the code and its plain reading, what's violated with its
source, the binding requirements and the unknown facts as two separate lists, any assumed
defaults that matter, the fix as a code diff, the usual next step, similar past
records, and any scope warning. It is a structured self-check, not legal advice.
