#!/usr/bin/env python3
"""Copy the canonical sources into the adapters that ship them, or check that the copies match.

Canonical sources live in the packages; adapters carry byte-identical copies so each one works
on its own (a Claude Code plugin is installed from its folder alone, and the single-file reader
inlines its engine):

  src/policy_action_check/engine.py          -> plugins/policy-action-check/scripts/pac.py
  src/policy_action_check/prompts/agents/*.md -> plugins/policy-action-check/agents/*.md
  src/policy_action_check/prompts/SPEC.md     -> plugins/policy-action-check/references/SPEC.md
  src/policy_action_check/prompts/method.md   -> plugins/policy-action-check/references/method.md
  packages/pac-js/pac.js                      -> apps/pac-reader/pac.js
  LICENSE                                     -> packages/pac-js/LICENSE

Usage: python tools/sync.py           (write the copies)
       python tools/sync.py --check   (exit 1 if any copy differs)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "policy_action_check"
PLUGIN = ROOT / "plugins" / "policy-action-check"


def pairs() -> list[tuple[Path, Path]]:
    out = [(PKG / "engine.py", PLUGIN / "scripts" / "pac.py"),
           (PKG / "prompts" / "SPEC.md", PLUGIN / "references" / "SPEC.md"),
           (PKG / "prompts" / "method.md", PLUGIN / "references" / "method.md"),
           (ROOT / "packages" / "pac-js" / "pac.js", ROOT / "apps" / "pac-reader" / "pac.js"),
           (ROOT / "LICENSE", ROOT / "packages" / "pac-js" / "LICENSE")]
    out += [(src, PLUGIN / "agents" / src.name) for src in sorted((PKG / "prompts" / "agents").glob("*.md"))]
    return out


def stale() -> list[str]:
    return [str(dst.relative_to(ROOT)) for src, dst in pairs()
            if not dst.exists() or dst.read_bytes() != src.read_bytes()]


def main(argv: list[str]) -> int:
    if "--check" in argv:
        if bad := stale():
            print("copies differ from their sources, run: python tools/sync.py\n  " + "\n  ".join(bad),
                  file=sys.stderr)
            return 1
        print("all copies match their sources")
        return 0
    for src, dst in pairs():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            dst.write_bytes(src.read_bytes())
            print(f"wrote {dst.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
