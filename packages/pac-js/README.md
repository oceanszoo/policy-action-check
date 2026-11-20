# policy-action-check (JavaScript)

A no-model judge for [policy-action-check](https://github.com/oceanszoo/policy-action-check)
bundles, in plain JavaScript with no dependencies. It runs in the browser and in Node 18+.

Give it a bundle (`generator.json` + `judge.json`) and an action code, and it returns the same
verdict, fixes and report as the Python engine. The two are checked against each other on every
change, output for output.

```sh
npm install policy-action-check
```

## Node

```js
import { checkFlow, renderReport } from "policy-action-check";
import { loadBundle } from "policy-action-check/node";

const { space, verification } = await loadBundle("policy_space");
if (!verification?.digests_ok) console.warn("bundle files do not match bundle.json");

const result = checkFlow("act=biz pur=std chat=y disc=n", space);
console.log(result.verdict);              // "violates"
console.log(renderReport(result, space)); // the bundle's fixed plain-text report
```

## Browser

```js
import { Space, checkFlow, nextQuestion, lookup } from "policy-action-check";

const space = Space.fromTexts(generatorText, judgeText, recordsText /* optional */);
checkFlow("@support-bot disc=n", space);   // judge a code
nextQuestion("act=biz", space);            // the question form: what to ask next
lookup("chatbot that denies claims", space); // everyday words -> code tokens
```

## What is in it

| Export | Does |
|---|---|
| `Space`, `Space.fromTexts` | load a bundle (format 1.x) |
| `parseCode`, `formatCode`, `explainCode` | read, normalise and explain a code |
| `check`, `checkFlow` | judge one case, or cases joined with `&` (same time) and `>` (step by step) |
| `renderReport` | the fixed report a person reads |
| `nextQuestion`, `lookup`, `categories`, `fitDetail` | write a code without a model: questions, phrases, the case map |
| `minimalFixes` | the smallest change that makes a code allowed |
| `verifyManifest`, `sha256Hex` | check files against the digests in `bundle.json` |
| `loadBundle` (from `policy-action-check/node`) | read a bundle folder from disk and verify it |

Results follow the bundle specification
([SPEC.md](https://github.com/oceanszoo/policy-action-check/blob/main/src/policy_action_check/prompts/SPEC.md)).
Building a bundle is done with the Python package (`pip install policy-action-check`, then
`pac build`); judging with one never needs a model.

A structured self-check, not legal advice. MIT licensed.
