"""Unit tests for claude_swarm.cost — pricing table + price_call."""
from __future__ import annotations

import pytest

from claude_swarm.cost import MODEL_PRICING, price_call, resolve_model


class TestResolveModel:
    def test_alias_haiku(self) -> None:
        result = resolve_model("haiku")
        # Alias resolves to the bare id (which has its own pricing row).
        assert result == "claude-haiku-4-5"

    def test_alias_sonnet(self) -> None:
        assert resolve_model("sonnet") == "claude-sonnet-4-6"

    def test_alias_opus(self) -> None:
        assert resolve_model("opus") == "claude-opus-4-7"

    def test_unknown_passthrough(self) -> None:
        assert resolve_model("some-future-model-9999") == "some-future-model-9999"

    def test_exact_id_passthrough(self) -> None:
        assert resolve_model("claude-sonnet-4-6") == "claude-sonnet-4-6"


class TestPriceCall:
    def test_known_model_output(self) -> None:
        """claude-sonnet-4-6 output = $15 / 1M tokens."""
        usd = price_call("claude-sonnet-4-6", output_tokens=1_000_000)
        assert usd == pytest.approx(15.0, rel=1e-6)

    def test_known_model_input(self) -> None:
        """claude-sonnet-4-6 input = $3 / 1M tokens."""
        usd = price_call("claude-sonnet-4-6", input_tokens=1_000_000)
        assert usd == pytest.approx(3.0, rel=1e-6)

    def test_known_model_mixed(self) -> None:
        """Mixed token types accumulate correctly."""
        usd = price_call(
            "claude-sonnet-4-6",
            input_tokens=100_000,
            output_tokens=50_000,
            cache_read_tokens=200_000,
        )
        expected = (100_000 * 3.0 + 50_000 * 15.0 + 200_000 * 0.3) / 1_000_000
        assert usd == pytest.approx(expected, rel=1e-6)

    def test_unknown_model_returns_zero(self) -> None:
        """Unknown model ids return 0.0 — visible gap, not a silent wrong number."""
        assert price_call("model-that-does-not-exist-999") == 0.0

    def test_all_zero_tokens(self) -> None:
        assert price_call("claude-sonnet-4-6") == 0.0

    # ------------------------------------------------------------------
    # Regression: CLI default id must NOT return $0
    # ------------------------------------------------------------------
    def test_cli_default_haiku_bare_id_has_nonzero_price(self) -> None:
        """Regression guard: price_call('claude-haiku-4-5', ...) must return > 0.

        The claude-swarm CLI defaults to 'claude-haiku-4-5' (main.py:394).
        Without an explicit pricing row OR alias for this bare id, every demo
        run shows $0 cost — a silent accounting bug.  This test fails if that
        row/alias is missing.
        """
        usd = price_call("claude-haiku-4-5", output_tokens=1_000_000)
        assert usd > 0.0, (
            "price_call('claude-haiku-4-5', output_tokens=1_000_000) returned $0.00 — "
            "the CLI-default model id has no pricing row or alias in MODEL_PRICING."
        )

    def test_haiku_bare_id_matches_expected_rate(self) -> None:
        """Haiku output rate = $4 / 1M — same as the date-stamped id."""
        usd = price_call("claude-haiku-4-5", output_tokens=1_000_000)
        assert usd == pytest.approx(4.0, rel=1e-6)

    def test_haiku_via_alias(self) -> None:
        """'haiku' alias must also price correctly (resolves to claude-haiku-4-5)."""
        usd = price_call("haiku", output_tokens=1_000_000)
        assert usd == pytest.approx(4.0, rel=1e-6)

    def test_datestamped_haiku_id(self) -> None:
        """The date-stamped id must also have a pricing row."""
        assert "claude-haiku-4-5-20251001" in MODEL_PRICING
        usd = price_call("claude-haiku-4-5-20251001", output_tokens=1_000_000)
        assert usd == pytest.approx(4.0, rel=1e-6)

    def test_cache_write_dimensions(self) -> None:
        """Both cache-write tiers are billed."""
        # Sonnet: cache_write_5m=$3.75/M, cache_write_1h=$6.0/M
        usd_5m = price_call("claude-sonnet-4-6", cache_write_5m_tokens=1_000_000)
        usd_1h = price_call("claude-sonnet-4-6", cache_write_1h_tokens=1_000_000)
        assert usd_5m == pytest.approx(3.75, rel=1e-6)
        assert usd_1h == pytest.approx(6.0, rel=1e-6)
