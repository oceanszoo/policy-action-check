"""policy-action-check: turn policy terms (plus action history) into a bundle that judges actions
with no model at use time.

- ``policy_action_check.engine``: the exact, standard-library engine (judge, exam, gaps, ...).
- ``policy_action_check.builder``: the build-time pipeline that drafts a bundle with any LLM.
- ``policy_action_check.mcp_server``: an MCP server exposing the judge over stdio.

Command line: ``pac`` (see ``pac --help``).
"""

__version__ = "0.8.0"
