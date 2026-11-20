# Changelog

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
