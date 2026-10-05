"""docs/qsfp-link.md and the README must describe commands and files that really exist."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.qsfp_fakes import FakeNode
from twinspark import cli, qsfp

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "qsfp-link.md"


def _commands(path: Path) -> list[str]:
    """Every ``tsm qsfp …`` command in fenced blocks and inline code, comments and ``sudo`` removed."""
    text = path.read_text()
    snippets = re.findall(r"```(?:bash|sh)?\n(.*?)```", text, re.S) + re.findall(r"`([^`\n]+)`", text)
    found = []
    for block in snippets:
        for line in block.splitlines():
            line = re.sub(r"\s+#.*$", "", line.strip()).removeprefix("$ ").removeprefix("sudo ").strip()
            if re.match(r"tsm (--json )?qsfp\b", line):
                found.append(line)
    return found


@pytest.mark.parametrize("path", [DOC, ROOT / "README.md"], ids=lambda p: p.name)
def test_every_documented_qsfp_command_parses(path):
    cmds = _commands(path)
    assert cmds, f"{path.name} shows no tsm qsfp command"
    parser = cli.build_parser()
    for line in cmds:
        argv = line.split()[1:]
        try:
            parser.parse_args(argv)
        except SystemExit as exc:                      # argparse exits on an unknown option or choice
            raise AssertionError(f"{path.name}: `{line}` is not a valid command") from exc


def test_every_subcommand_is_documented():
    text = DOC.read_text()
    for sub in ("status", "plan", "apply", "revert", "verify", "scan"):
        assert f"tsm qsfp {sub}" in text, sub


def test_the_netplan_example_is_exactly_what_apply_writes(tmp_path, monkeypatch):
    n = FakeNode(tmp_path)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    host = n.host()
    plan = qsfp.plan_for_node(qsfp.discover(host), "A", host=host)
    (block,) = re.findall(r"```yaml\n(.*?)```", DOC.read_text(), re.S)
    assert block == plan.text


def test_the_documented_addresses_are_the_default_plan(tmp_path, monkeypatch):
    n = FakeNode(tmp_path)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    host = n.host()
    a = qsfp.plan_for_node(qsfp.discover(host), "A", host=host)
    b = qsfp.plan_for_node(qsfp.discover(host), "B", host=host)
    text = DOC.read_text()
    for e in a.entries + b.entries:
        assert f"`{e.cidr}`" in text, e.cidr
