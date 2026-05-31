"""Unit tests for build_conductor() — the selection seam."""
from __future__ import annotations

import pytest

from claude_swarm.conductor import ClaudeCLIConductor
from claude_swarm.conductors import build_conductor
from claude_swarm.conductors.api import ApiConductor
from claude_swarm.conductors.sdk import SDKConductor
from claude_swarm.supervisor import StubConductor


class TestBuildConductorRouting:
    def test_stub_returns_stub_conductor(self) -> None:
        cond = build_conductor("stub", model_override=None, demo_delay_s=0.0)
        assert isinstance(cond, StubConductor)

    def test_stub_demo_delay_forwarded(self) -> None:
        cond = build_conductor("stub", model_override=None, demo_delay_s=2.5)
        assert isinstance(cond, StubConductor)
        assert cond.demo_delay_s == pytest.approx(2.5)

    def test_claude_returns_claude_cli_conductor(self) -> None:
        cond = build_conductor("claude", model_override=None)
        assert isinstance(cond, ClaudeCLIConductor)

    def test_claude_model_override_forwarded(self) -> None:
        cond = build_conductor("claude", model_override="claude-haiku-4-5")
        assert isinstance(cond, ClaudeCLIConductor)
        assert cond.model_override == "claude-haiku-4-5"

    def test_claude_none_model_override(self) -> None:
        """model_override=None → head's default_model is used at dispatch time."""
        cond = build_conductor("claude", model_override=None)
        assert isinstance(cond, ClaudeCLIConductor)
        assert cond.model_override is None

    def test_api_returns_api_conductor(self) -> None:
        cond = build_conductor("api", model_override=None)
        assert isinstance(cond, ApiConductor)

    def test_api_model_override_forwarded(self) -> None:
        cond = build_conductor("api", model_override="claude-sonnet-4-6")
        assert isinstance(cond, ApiConductor)
        assert cond.model_override == "claude-sonnet-4-6"

    def test_sdk_returns_sdk_conductor(self) -> None:
        cond = build_conductor("sdk", model_override=None)
        assert isinstance(cond, SDKConductor)

    def test_sdk_model_override_forwarded(self) -> None:
        cond = build_conductor("sdk", model_override="claude-opus-4-7")
        assert isinstance(cond, SDKConductor)
        assert cond.model_override == "claude-opus-4-7"

    def test_unknown_name_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown conductor name"):
            build_conductor("nonexistent", model_override=None)


class TestBuildConductorNoRealClientConstruction:
    """Confirm that instantiating api/sdk conductors does NOT import or call
    the optional deps (anthropic, claude_agent_sdk) — lazy imports stay lazy."""

    def test_api_conductor_importable_without_anthropic(self) -> None:
        """ApiConductor is a dataclass; __init__ must not touch anthropic."""
        import sys
        # Temporarily remove anthropic from sys.modules to simulate absence.
        removed = sys.modules.pop("anthropic", None)
        try:
            cond = ApiConductor(model_override="haiku")
            assert cond.model_override == "haiku"
        finally:
            if removed is not None:
                sys.modules["anthropic"] = removed

    def test_sdk_conductor_importable_without_sdk(self) -> None:
        """SDKConductor is a dataclass; __init__ must not touch claude_agent_sdk."""
        import sys
        removed = sys.modules.pop("claude_agent_sdk", None)
        try:
            cond = SDKConductor(model_override="sonnet")
            assert cond.model_override == "sonnet"
        finally:
            if removed is not None:
                sys.modules["claude_agent_sdk"] = removed
