"""`pac`: one command for the engine, the builder and the MCP server.

  pac [--space DIR] check|report|ask|form|lookup|validate|exam|gaps|...   no model (engine)
  pac [--space DIR] build|refine|providers                                build time, uses a model
  pac [--space DIR] mcp                                                   MCP server over stdio
"""

from __future__ import annotations

import sys

from policy_action_check import __version__, engine

BUILDER = {"build", "refine", "providers"}
EXTRA_HELP = """
build-time commands (use a language model; see `pac build --help`):
  build        policy (+ history) -> bundle, with any provider (OpenRouter, OpenAI, Anthropic, Ollama, ...)
  refine       close self-exam and case-map gaps in an existing bundle
  providers    list the supported providers and their settings
adapters:
  mcp          serve the bundle's judge to MCP clients over stdio
Using a bundle never needs a model."""


def _command(argv: list[str]) -> str | None:
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--space":
            skip = True
        elif not arg.startswith("-"):
            return arg
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in (["--version"], ["version"]):
        print(f"pac (policy-action-check) {__version__}")
        return 0
    cmd = _command(argv)
    if cmd in BUILDER:
        from policy_action_check.builder import main as builder_main
        return builder_main(argv)
    if cmd == "mcp":
        from policy_action_check.mcp_server import main as mcp_main
        return mcp_main(argv)
    if cmd is None:
        try:
            engine.main(argv or ["--help"])
        except SystemExit as exc:
            if not argv or "-h" in argv or "--help" in argv:
                print(EXTRA_HELP)
            return int(exc.code or 0)
    return engine.main(argv)


if __name__ == "__main__":
    sys.exit(main())
