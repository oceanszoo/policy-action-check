"""The pip package: one engine, copies in sync, and a working `pac` command."""

import subprocess
import sys
from pathlib import Path

from policy_action_check import __version__, cli, engine

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "fixtures" / "sample-bundle"


def test_adapter_copies_match_their_sources():
    res = subprocess.run([sys.executable, "tools/sync.py", "--check"], cwd=ROOT, capture_output=True, text=True,
                         check=False)
    assert res.returncode == 0, res.stderr


def test_versions_agree():
    import json
    import tomllib
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    plugin = json.loads((ROOT / "plugins/policy-action-check/.claude-plugin/plugin.json").read_text())
    npm = json.loads((ROOT / "packages/pac-js/package.json").read_text())
    assert pyproject["project"]["version"] == __version__ == plugin["version"] == npm["version"]


def test_cli_dispatches_engine_commands(capsys):
    assert cli.main(["--space", str(SAMPLE), "report", "@support-bot disc=n"]) == 0
    assert "Not allowed as coded." in capsys.readouterr().out
    assert cli.main(["--version"]) == 0 and __version__ in capsys.readouterr().out


def test_help_lists_build_time_commands(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "build" in out and "mcp" in out and "never needs a model" in out


def test_module_entry_point():
    res = subprocess.run([sys.executable, "-m", "policy_action_check", "--space", str(SAMPLE), "validate"],
                         capture_output=True, text=True, check=False, cwd=ROOT)
    assert res.returncode == 0, res.stdout + res.stderr


def test_engine_has_no_dependencies():
    import ast
    tree = ast.parse((ROOT / "src/policy_action_check/engine.py").read_text())
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= set(sys.stdlib_module_names) | {"__future__"}
    assert engine.SpaceError
