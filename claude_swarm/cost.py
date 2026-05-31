"""Per-call token + USD pricing primitives for the swarm.

A dependency-light port of the deep-ai cost module (deep-ai
dm/deep_manager/swarm/cost.py:23-123). This module supplies ONLY the
``$/token`` producer — tracking, ledger writes, and budget gating live in
``usage.py`` and ``governor.py`` respectively.

Usage::

    from claude_swarm.cost import price_call, resolve_model, MODEL_PRICING

    usd = price_call(
        "claude-haiku-4-5",
        input_tokens=800,
        output_tokens=200,
    )

Pricing snapshot: 2026-05. Update when Anthropic publishes new rates.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1 M tokens for each billing dimension."""

    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


# ---------------------------------------------------------------------------
# Pricing table — Anthropic rates as of 2026-05.
# ---------------------------------------------------------------------------
MODEL_PRICING: dict[str, ModelPrice] = {
    # Opus 4.8 (1 M context). Current frontier.
    # Pricing mirrors Opus 4.7 until Anthropic publishes a separate sheet.
    "claude-opus-4-8": ModelPrice(
        input=5.0, cache_write_5m=6.25, cache_write_1h=10.0, cache_read=0.5, output=15.0
    ),
    # Opus 4.7 (1 M context). Premium reasoning.
    "claude-opus-4-7": ModelPrice(
        input=5.0, cache_write_5m=6.25, cache_write_1h=10.0, cache_read=0.5, output=15.0
    ),
    # Sonnet 4.6. Default worker.
    "claude-sonnet-4-6": ModelPrice(
        input=3.0, cache_write_5m=3.75, cache_write_1h=6.0, cache_read=0.3, output=15.0
    ),
    # Haiku 4.5. Router / classifier. Canonical date-stamped id.
    "claude-haiku-4-5-20251001": ModelPrice(
        input=0.8, cache_write_5m=1.0, cache_write_1h=1.6, cache_read=0.08, output=4.0
    ),
    # Haiku 4.5. Bare id — the claude-swarm CLI default (main.py:394).
    # Aliases to the same price row so price_call("claude-haiku-4-5", ...)
    # returns non-zero; without this entry cost accounting silently shows $0.
    "claude-haiku-4-5": ModelPrice(
        input=0.8, cache_write_5m=1.0, cache_write_1h=1.6, cache_read=0.08, output=4.0
    ),
}

# ---------------------------------------------------------------------------
# Friendly aliases (resolved before pricing lookup).
# ---------------------------------------------------------------------------
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    # Both bare and date-stamped haiku aliases resolve to the bare id so that
    # the pricing row added above is hit by both spellings.
    "haiku": "claude-haiku-4-5",
    "haiku-20251001": "claude-haiku-4-5-20251001",
}


def resolve_model(name_or_alias: str) -> str:
    """Return the canonical model id for *name_or_alias*, or the input unchanged."""
    return MODEL_ALIASES.get(name_or_alias, name_or_alias)


def price_call(
    model: str,
    *,
    input_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    output_tokens: int = 0,
) -> float:
    """USD cost of one ``messages.create`` response.

    Returns ``0.0`` for unknown models so callers never raise on a new id;
    they just see zero cost (visible in accounting dashboards as a gap, which
    is the correct signal to add a pricing row).
    """
    p = MODEL_PRICING.get(resolve_model(model))
    if p is None:
        return 0.0
    return (
        (input_tokens * p.input)
        + (cache_read_tokens * p.cache_read)
        + (cache_write_5m_tokens * p.cache_write_5m)
        + (cache_write_1h_tokens * p.cache_write_1h)
        + (output_tokens * p.output)
    ) / 1_000_000.0


__all__ = [
    "MODEL_ALIASES",
    "MODEL_PRICING",
    "ModelPrice",
    "price_call",
    "resolve_model",
]
