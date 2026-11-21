# Changelog

## 0.8.0 - a standalone tool

- Python package `policy-action-check` with one `pac` command (also `python -m policy_action_check`).
  The engine moved to `src/policy_action_check/engine.py`; the plugin keeps a synced copy.
- `pac build` and `pac refine`: the build pipeline with any model provider (OpenRouter, OpenAI,
  Anthropic, Ollama, any OpenAI-compatible server), using the plugin's agent prompts and the
  engine's checks in place of hooks. Standard library only.
- `pac mcp`: an MCP server (stdio) exposing check, report, lookup, the question form and more.
- npm package `policy-action-check` (`packages/pac-js`): the JavaScript judge plus a Node loader
  and TypeScript types, tested output-identical to the Python engine.
- The example bundle built from a real policy is gone; tests use an invented sample bundle.
  The reader no longer has a "Try the example" button.

## 0.7.0 - first public release

- Claude Code plugin `policy-action-check` 0.7.0 and Policy Action Reader, bundle format 1.6.
- Example bundle built from Anthropic's public Usage Policy (summaries plus three short quotes).

## Bundle format history

See [SPEC.md](plugins/policy-action-check/references/SPEC.md) for details.

- **1.6**: typed clause references and a glossary; reference lint, context lookup; embedded source terms and exact citations in results and guides.
- **1.5**: deterministic refinement (gaps, patches) and a generated `BUNDLE.md` guide in every bundle.
- **1.4**: case map (`categories`, nested folders), generator page, `a & b` and `a > b` cases, every possible result when facts are skipped.
- **1.3**: bundle self-exam (`exam`, `outcome_map`, `not_encoded`).
- **1.2**: question form, keyword lookup, records, fixed report layout, next-step lookup, notes. No model needed at use time.
- **1.1**: multi-valued dimensions, size limits, `facts` / `requirements` / `unknown_facts` in judge output.
- **1.0**: first format: generator, judge, conformance vectors.
