# Policy Action Check

Turn a policy or terms of use, plus your own action history, into a small **bundle of two files**
that anyone can use to check an action, with **no AI model at use time**:

- `generator.json` says how to write an action as a short code, for example
  `act=biz pur=cyber auth=own`.
- `judge.json` says which terms a code violates, what you must do next, and the smallest fix.

The same code always gets the same verdict. Unknown facts are reported as unknown, never as "no".

This repository has two parts:

| part | what it is |
|---|---|
| [`plugins/policy-action-check/`](plugins/policy-action-check/) | A Claude Code plugin that reads a policy and your history once and builds the bundle. It also ships the reference engine `scripts/pac.py` (Python standard library only). |
| [`apps/pac-reader/`](apps/pac-reader/) | **Policy Action Reader**, a single-file web app that opens a bundle, lets you build cases on a case map, and shows the report. It runs entirely in the browser and uploads nothing. |

The bundle format is specified in
[`plugins/policy-action-check/references/SPEC.md`](plugins/policy-action-check/references/SPEC.md)
(version 1.6), so other software can read bundles too.

## Quick start

**Try the reader.** Download [`apps/pac-reader/dist/pac-reader.html`](apps/pac-reader/dist/pac-reader.html),
open it in a browser and press **Try the example**. The example bundle is built from Anthropic's
public Usage Policy; see [its README](plugins/policy-action-check/examples/anthropic-aup/README.md).

**Build your own bundle (Claude Code).**

```
/plugin marketplace add oceanszoo/policy-action-check
/plugin install policy-action-check@oceanszoo-plugins
```

Then ask, for example: "Build a policy bundle from `terms.md` and `history.csv`."

**Judge a code from the command line.**

```sh
python3 plugins/policy-action-check/scripts/pac.py --space plugins/policy-action-check/examples/anthropic-aup validate
```

## Layout

```
.claude-plugin/marketplace.json   Claude Code marketplace entry
plugins/policy-action-check/      plugin: agents, hooks, skill, engine (scripts/pac.py), spec, example bundle, tests
apps/pac-reader/                  web reader: pac.js engine (same behaviour as pac.py), page, build, tests
tests/                            cross-checks between the reader and the Python engine
```

## Development

```sh
python -m pip install -e '.[dev]'
ruff check .
pytest                      # needs Node.js 18+ for the reader tests
node apps/pac-reader/build.mjs   # rebuild dist/pac-reader.html after changing the reader
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

Bundles are aids for reasoning about policies, not legal advice. Example bundles summarise public
policies for demonstration; the official policy text always controls. This project is not
affiliated with or endorsed by any policy owner whose policy appears in an example.

## License

[MIT](LICENSE)
