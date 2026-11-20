# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Ground rules

- **The engine is the judge.** Verdicts come only from `plugins/policy-action-check/scripts/pac.py`
  running on the bundle files. If a verdict is wrong, fix the bundle, not the prose around it.
- **Spec first.** A change to the bundle format updates `references/SPEC.md`, the version number
  (minor if compatible, major if not), the engine, the reader and the tests, all together.
- **Standard library only** for `pac.py`, and no dependencies for `apps/pac-reader/pac.js`, so a
  bundle can be judged anywhere.
- **The two engines agree.** `pac.js` must give exactly the same outputs as `pac.py`;
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
node apps/pac-reader/build.mjs --check   # dist/ must be current; rebuild and commit it if not
```

If you change the plugin, also run `claude plugin validate plugins/policy-action-check` when the
Claude Code CLI is available.

Bundle files and the reader are stored with LF line endings (see `.gitattributes`); keep them
byte-identical, because bundles carry digests.
