"""The YAML shown in the docs and example files must be accepted by the real config schema."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from twinspark.schemas.config import ControllerConfig

ROOT = Path(__file__).resolve().parent.parent
BASE = {
    "node": {"node_id": "A", "role": "controller"},
    "nodes": {"A": {"agent_url": "http://127.0.0.1:9443"},
              "B": {"agent_url": "http://192.168.100.2:9443", "qsfp_ip": "192.168.100.2"}},
}


def _yaml_blocks(path: Path) -> list[dict]:
    text = path.read_text()
    return [yaml.safe_load(m) for m in re.findall(r"```yaml\n(.*?)```", text, re.S)]


def test_remote_management_doc_examples_validate():
    blocks = _yaml_blocks(ROOT / "docs" / "remote-management.md")
    assert len(blocks) >= 2                                   # wake and plug examples exist
    for block in blocks:
        cfg = {**BASE, "nodes": {**BASE["nodes"], "B": {**BASE["nodes"]["B"], **block["nodes"]["B"]}}}
        parsed = ControllerConfig.model_validate(cfg)
        assert parsed.nodes["B"].wake or parsed.nodes["B"].plug


def test_controller_example_remote_block_validates_when_uncommented():
    """Switch the commented-out remote-management lines on and load the whole example."""
    out: list[str] = []
    in_block = False
    for ln in (ROOT / "deploy" / "controller.example.yaml").read_text().splitlines():
        if "optional remote management" in ln:
            in_block = True
            out.append(ln)
            continue
        if in_block and ln.lstrip().startswith("#"):
            ln = re.sub(r"^(\s*)# ?", r"\1", ln, count=1)
        elif in_block:
            in_block = False
        out.append(ln)
    parsed = ControllerConfig.model_validate(yaml.safe_load("\n".join(out)))
    b = parsed.nodes["B"]
    assert b.wake and b.wake.mac == "aa:bb:cc:dd:ee:ff" and b.wake.broadcast == "192.168.100.255"
    assert b.plug and b.plug.on and "${secret:plug_token}" in b.plug.on.url and b.plug.settle_s == 10
    assert b.terminal_port == 9444


def test_unquoted_on_off_keys_and_typos():
    """`on:` / `off:` are booleans in YAML 1.1; they must still configure the plug. Typos must be errors."""
    import pytest
    from pydantic import ValidationError

    from twinspark.schemas.config import PlugSettings, WakeSettings

    plug = PlugSettings.model_validate(yaml.safe_load(
        "on: {url: 'http://10.0.0.5/on'}\noff: {url: 'http://10.0.0.5/off'}\nsettle_s: 5\n"))
    assert plug.on and plug.on.url.endswith("/on") and plug.off and plug.off.url.endswith("/off")
    assert PlugSettings.model_validate({"cycle": {"url": "http://10.0.0.5/c"}}).cycle
    with pytest.raises(ValidationError):
        WakeSettings.model_validate({"mac": "aa:bb:cc:dd:ee:ff", "mac_address": "x"})
    with pytest.raises(ValidationError):
        PlugSettings.model_validate({"turn_on": {"url": "http://10.0.0.5/on"}})
    with pytest.raises(ValidationError):
        PlugSettings.model_validate({"on": {"url": "http://10.0.0.5/on", "verb": "GET"}})
