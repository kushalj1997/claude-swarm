"""Launchd source template tests.

These tests validate the committed template only. They do not install, load, or
start any launchd job.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

TEMPLATE = Path("docs/launchd/com.kushal.claude-swarm-perpetual.plist.template")


def test_launchd_template_is_parseable_keepalive_plist() -> None:
    data = plistlib.loads(TEMPLATE.read_bytes())

    assert data["Label"] == "com.kushal.claude-swarm-perpetual"
    assert data["KeepAlive"] is True
    assert data["RunAtLoad"] is True
    assert "ProgramArguments" in data
    assert data["ProgramArguments"][0] == "__CLAUDE_SWARM_BIN__"
    assert "perpetual" in data["ProgramArguments"]


def test_launchd_template_does_not_embed_anthropic_api_key() -> None:
    data = plistlib.loads(TEMPLATE.read_bytes())
    env = data.get("EnvironmentVariables", {})

    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in TEMPLATE.read_text(encoding="utf-8")
