# Policy Action Check

Turn a policy or terms of use, plus your own action history, into a small **bundle of files**
that anyone can use to check an action, with **no AI model at use time**:

- `generator.json` says how to write an action as a short code, for example
  `act=biz pur=cyber auth=own`.
- `judge.json` says which terms a code violates, what you must do next, and the smallest fix.

The same code always gets the same verdict. Unknown facts are reported as unknown, never as "no".
A language model is used once, at build time, to draft the bundle from the policy; the bundle then
passes a deterministic self-exam before it is finished.

## Pick your way in

| You want to | Use | Needs a model? |
|---|---|---|
| Judge codes, run the question form, check or refine bundles from a terminal or Python | [`pac` CLI / Python package](#python-package-and-cli) | no |
| Build a bundle from a policy with OpenRouter, OpenAI, Anthropic, Ollama or any OpenAI-compatible server | [`pac build`](#build-a-bundle-with-any-model) | yes, at build time |
| Let an MCP client (Claude Desktop, Cursor, an agent) judge actions | [`pac mcp`](#mcp-server) | no |
| Judge in a web page or Node app | [npm package](#javascript-npm) | no |
| Do it all inside Claude Code | [Claude Code plugin](#claude-code-plugin) | Claude Code |
| Open a bundle and build cases by clicking | [Policy Action Reader](#policy-action-reader) | no |

## Python package and CLI

```sh
pip install policy-action-check      # Python 3.11+, no dependencies
pac --space policy_space form        # answer questions, get the report
pac --space policy_space report "act=biz pur=std chat=y disc=n"
pac --space policy_space check "@support-bot"          # the same result as JSON
pac --space policy_space lookup "chatbot that denies claims"
pac --space policy_space validate    # schema + conformance tests
pac --space policy_space exam        # the self-exam
```

`--space` defaults to `./policy_space` (or `PAC_SPACE`). `python -m policy_action_check` works
too. In Python:

```python
from pathlib import Path
from policy_action_check import engine

space = engine.Space(Path("policy_space"))
result = engine.check_flow("act=biz pur=std chat=y disc=n", space)
print(result["verdict"], result["violated"])
print(engine.render_report(result, space))
```

## Build a bundle with any model

`pac build` runs the build pipeline (term filter, scope plan, builder, verifier) against the
provider you choose, then the self-exam. The model only drafts files; after each stage the
engine checks them (terms lint, validation, the exam) and sends any problems back for another
try. A bundle that fails its exam is reported as not ready.

```sh
export OPENROUTER_API_KEY=...        # or OPENAI_API_KEY / ANTHROPIC_API_KEY
pac --space policy_space build --policy terms.md --history history.csv --model <model id>

pac build --provider anthropic --model claude-opus-5-5 --policy terms.md
pac build --provider openai    --model <model id>      --policy https://example.com/terms
pac build --provider ollama    --model llama3.1         --policy terms.md   # local, no key
pac build --provider openai-compatible --base-url http://localhost:8000/v1 --model <id> --policy terms.md
```

| Option | Meaning |
|---|---|
| `--history` | your past actions (CSV, JSONL or text); optional, makes the bundle fit your cases |
| `--interactive` | ask the scope questions on the terminal (otherwise the recommended answers are used and listed in the output) |
| `--from verify` | resume from a stage, for example after editing files by hand |
| `--max-tokens`, `--timeout`, `--attempts` | per-call limits and tries per stage |

`pac providers` lists the presets and their environment variables (`PAC_PROVIDER`, `PAC_MODEL`,
`PAC_BASE_URL`, `PAC_API_KEY` work for all of them). Larger, stronger models do much better at
the build stage, which writes the whole bundle in one reply.

Refine an existing bundle (close self-exam and case-map gaps) the same way:

```sh
pac --space policy_space refine --focus privacy --policy terms.md --model <model id>
```

Each refine round is a patch that `pac apply-patch` validates and applies only if every check
passes; nothing is written when it is refused.

## MCP server

```sh
pac --space /path/to/policy_space mcp
```

It speaks MCP over stdio and offers `check`, `report`, `parse`, `lookup`, `next_question`,
`categories`, `fit` and `bundle_info`. For example, in a client's MCP configuration:

```json
{"mcpServers": {"policy": {"command": "pac", "args": ["--space", "/path/to/policy_space", "mcp"]}}}
```

## JavaScript (npm)

```sh
npm install policy-action-check
```

```js
import { checkFlow, renderReport } from "policy-action-check";
import { loadBundle } from "policy-action-check/node";

const { space } = await loadBundle("policy_space");
console.log(renderReport(checkFlow("@support-bot disc=n", space), space));
```

It runs in browsers and Node 18+ with no dependencies, and its outputs are tested to be identical
to the Python engine's. See [packages/pac-js](packages/pac-js/).

## Claude Code plugin

```
/plugin marketplace add oceanszoo/policy-action-check
/plugin install policy-action-check@oceanszoo-plugins
```

Then ask, for example: "Build a policy bundle from `terms.md` and `history.csv`." The plugin runs
the same pipeline with Claude Code subagents and the same engine. See
[plugins/policy-action-check](plugins/policy-action-check/).

## Policy Action Reader

Download [`apps/pac-reader/dist/pac-reader.html`](apps/pac-reader/dist/pac-reader.html), open it
in a browser and choose your bundle folder. It runs entirely in the browser and uploads nothing.

## Bundle format

The format is specified in [SPEC.md](src/policy_action_check/prompts/SPEC.md) (version 1.6), so
other software can read bundles too.

## Layout

```
src/policy_action_check/      Python package: engine (the judge), builder, providers, MCP server, prompts + spec
packages/pac-js/              npm package: pac.js, the same judge in JavaScript
plugins/policy-action-check/  Claude Code plugin: agents, hooks, skill (synced copies of the engine and prompts)
apps/pac-reader/              web reader: page, build, tests
tests/                        package tests, the synthetic sample bundle, reader cross-checks
tools/sync.py                 copies the canonical sources into the plugin and the reader
```

## Development

```sh
python -m pip install -e '.[dev]'
ruff check .
pytest                                   # needs Node.js 18+ for the reader tests
python tools/sync.py --check             # plugin and reader copies match the sources
node apps/pac-reader/build.mjs --check   # dist/pac-reader.html is current
(cd packages/pac-js && npm test)
```

Edit the engine, prompts and spec under `src/policy_action_check/` and `pac.js` under
`packages/pac-js/`, then run `python tools/sync.py`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

Bundles are aids for reasoning about policies, not legal advice; the official policy text always
controls. The repository ships no real policy: tests use an invented sample.

## License

[MIT](LICENSE)
