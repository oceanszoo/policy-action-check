# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Ground rules

- **The engine is the judge.** Verdicts come only from the engine
  (`src/policy_action_check/engine.py`, or its JavaScript port `packages/pac-js/pac.js`) running on
  the bundle files. If a verdict is wrong, fix the bundle, not the prose around it.
- **Edit the sources, not the copies.** The plugin's `scripts/pac.py`, `agents/*.md` and
  `references/*.md`, and the reader's `pac.js`, are copies. Edit `src/policy_action_check/` or
  `packages/pac-js/` and run `python tools/sync.py`; CI fails when a copy differs.
- **Using a bundle never needs a model.** Only `pac build` and `pac refine` call a provider, and
  everything they produce must pass the engine's checks.
- **Spec first.** A change to the bundle format updates `references/SPEC.md`, the version number
  (minor if compatible, major if not), the engine, the reader and the tests, all together.
- **No dependencies.** The Python package uses the standard library only (providers included), and
  `pac.js` has no dependencies, so a bundle can be judged anywhere.
- **The two engines agree.** `pac.js` must give exactly the same outputs as the Python engine;
  `apps/pac-reader/tests/parity.mjs` checks it.
- **Every rule cites its source** and has at least one conformance vector. Exceptions need vectors
  on both sides.
- **Unknown is never "no".** Requirement dimensions must not have defaults.
- **Only embed policy text you may distribute.** Prefer clearly labelled summaries.

## Test fixtures

The repository ships no real policy. Tests use the synthetic bundle in `tests/fixtures/sample-bundle/`
(an invented "Sample Service Terms"). If you change the bundle format, update the fixture, rerun
`pac manifest` on it, and keep its conformance vectors written from its own terms text.

## Before you open a pull request

```sh
ruff check .
pytest
python tools/sync.py --check             # copies must match their sources
node apps/pac-reader/build.mjs --check   # dist/ must be current; rebuild and commit it if not
(cd packages/pac-js && npm test)
```

Builder changes can be tested offline: `pac build --provider replay --replay replies.json` answers
from canned replies (see `tests/test_builder.py`). Never commit API keys.

If you change the plugin, also run `claude plugin validate plugins/policy-action-check` when the
Claude Code CLI is available.

Bundle files and the reader are stored with LF line endings (see `.gitattributes`); keep them
byte-identical, because bundles carry digests.
