"""Unit tests for build_conductor() — the selection seam."""
from __future__ import annotations

import warnings

import pytest

from claude_swarm.conductor import ClaudeCLIConductor
from claude_swarm.conductors import DEFAULT_CONDUCTOR, build_conductor
from claude_swarm.conductors.api import ApiConductor
from claude_swarm.conductors.sdk import SDKConductor
from claude_swarm.supervisor import StubConductor


class TestDefaultConductorIsAPIBased:
    """A: DEFAULT_CONDUCTOR must resolve to the Anthropic Messages API backend.

    Rationale: `claude --print` is metered starting June 15 2026.
    The production default must never silently route through the CLI.
    """

    def test_default_conductor_constant_is_api(self) -> None:
        """DEFAULT_CONDUCTOR == 'api' — literal value check."""
        assert DEFAULT_CONDUCTOR == "api", (
            f"DEFAULT_CONDUCTOR changed to {DEFAULT_CONDUCTOR!r}. "
            "Only change this after a documented policy decision; "
            "the June-15-2026 billing deadline makes 'claude' unsafe as default."
        )

    def test_build_conductor_default_returns_api_conductor(self) -> None:
        """build_conductor(DEFAULT_CONDUCTOR, ...) returns ApiConductor."""
        cond = build_conductor(DEFAULT_CONDUCTOR, model_override=None)
        assert isinstance(cond, ApiConductor), (
            f"build_conductor(DEFAULT_CONDUCTOR) returned {type(cond).__name__}, "
            "expected ApiConductor. The default must use the Anthropic Messages API."
        )

    def test_build_conductor_default_is_not_cli_conductor(self) -> None:
        """The default must never return ClaudeCLIConductor (metered CLI)."""
        cond = build_conductor(DEFAULT_CONDUCTOR, model_override=None)
        assert not isinstance(cond, ClaudeCLIConductor), (
            "Default conductor resolved to ClaudeCLIConductor — this will incur "
            "unexpected billing from June 15 2026."
        )

    def test_claude_conductor_emits_deprecation_warning(self) -> None:
        """Requesting conductor='claude' explicitly must emit a DeprecationWarning."""
        import os
        # Ensure the suppression env var is not set for this test.
        old = os.environ.pop("CLAUDE_SWARM_ALLOW_CLI_CONDUCTOR", None)
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                build_conductor("claude", model_override=None)
            dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            assert dep_warnings, (
                "conductor='claude' must emit a DeprecationWarning about the June-15 billing change."
            )
            assert "june 15" in str(dep_warnings[0].message).lower() or "metered" in str(dep_warnings[0].message).lower()
        finally:
            if old is not None:
                os.environ["CLAUDE_SWARM_ALLOW_CLI_CONDUCTOR"] = old

    def test_claude_conductor_warning_suppressed_by_env(self) -> None:
        """CLAUDE_SWARM_ALLOW_CLI_CONDUCTOR=1 suppresses the deprecation."""
        import os
        old = os.environ.get("CLAUDE_SWARM_ALLOW_CLI_CONDUCTOR")
        os.environ["CLAUDE_SWARM_ALLOW_CLI_CONDUCTOR"] = "1"
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                build_conductor("claude", model_override=None)
            dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            assert not dep_warnings, "Warning should be suppressed when env var is set."
        finally:
            if old is None:
                del os.environ["CLAUDE_SWARM_ALLOW_CLI_CONDUCTOR"]
            else:
                os.environ["CLAUDE_SWARM_ALLOW_CLI_CONDUCTOR"] = old


class TestBuildConductorRouting:
    def test_stub_returns_stub_conductor(self) -> None:
        cond = build_conductor("stub", model_override=None, demo_delay_s=0.0)
        assert isinstance(cond, StubConductor)

    def test_stub_demo_delay_forwarded(self) -> None:
        cond = build_conductor("stub", model_override=None, demo_delay_s=2.5)
        assert isinstance(cond, StubConductor)
        assert cond.demo_delay_s == pytest.approx(2.5)

    def test_claude_returns_claude_cli_conductor(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cond = build_conductor("claude", model_override=None)
        assert isinstance(cond, ClaudeCLIConductor)

    def test_claude_model_override_forwarded(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cond = build_conductor("claude", model_override="claude-haiku-4-5")
        assert isinstance(cond, ClaudeCLIConductor)
        assert cond.model_override == "claude-haiku-4-5"

    def test_claude_none_model_override(self) -> None:
        """model_override=None → head's default_model is used at dispatch time."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
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
