"""Environment preflights for conductor backends."""
from __future__ import annotations

import os
from collections.abc import Mapping


class MissingAnthropicApiKeyError(RuntimeError):
    """Raised when an API-backed conductor cannot run without credentials."""


def require_anthropic_api_key(*, environ: Mapping[str, str] | None = None) -> None:
    """Fail fast unless the API key is present and non-blank.

    The key value is intentionally not returned so callers cannot accidentally
    log, persist, or pass it through surfaces that only need readiness proof.
    """

    env = os.environ if environ is None else environ
    value = env.get("ANTHROPIC_API_KEY")
    if value is None:
        raise MissingAnthropicApiKeyError(
            "ANTHROPIC_API_KEY is not set; conductor='api' requires a key in the "
            "process environment before any task is claimed."
        )
    if not value.strip():
        raise MissingAnthropicApiKeyError(
            "ANTHROPIC_API_KEY is blank; conductor='api' requires a non-empty key "
            "in the process environment before any task is claimed."
        )


__all__ = ["MissingAnthropicApiKeyError", "require_anthropic_api_key"]
