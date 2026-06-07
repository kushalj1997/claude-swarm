"""build_conductor() — single selection seam for all four conductor backends.

Called from both the ``run`` and ``perpetual`` CLI commands so the two
if/elif blocks stay in lockstep.

**DEFAULT is ``"api"`` (Anthropic Messages API).**

Rationale: starting June 15 2026 the ``claude --print`` / Agent-SDK CLI path
is no longer free under Max plans — it draws from a metered monthly credit and
then bills at standard API rates (Anthropic policy change, 2026-05-30).  The
``"api"`` conductor uses the ``ANTHROPIC_API_KEY`` env var directly, avoiding
all per-CLI-invocation overhead and metering surprises.

``"claude"`` / ``"stub"`` remain available; ``"claude"`` now emits a
``DeprecationWarning`` reminding callers of the billing change.
"""
from __future__ import annotations

import logging
import warnings

from ..conductor import ClaudeCLIConductor
from ..supervisor import Conductor, StubConductor
from .api import ApiConductor
from .env import MissingAnthropicApiKeyError, require_anthropic_api_key
from .sdk import SDKConductor

log = logging.getLogger(__name__)

#: The canonical production conductor. ``"api"`` since 2026-06-07 (June-15
#: CLI billing deadline). ``"stub"`` remains the CI/test default when callers
#: explicitly pass it; ``"claude"`` is available but deprecated.
DEFAULT_CONDUCTOR: str = "api"

_CLI_DEPRECATION_MSG = (
    "conductor='claude' shells out to `claude --print`. "
    "Starting June 15 2026 this path draws from a metered monthly credit "
    "(not the interactive Max quota). Switch to conductor='api' "
    "(ANTHROPIC_API_KEY) to avoid unexpected billing. "
    "Pass CLAUDE_SWARM_ALLOW_CLI_CONDUCTOR=1 to suppress this warning."
)


def build_conductor(
    name: str,
    *,
    model_override: str | None,
    demo_delay_s: float = 0.0,
) -> Conductor:
    """Return the :class:`~claude_swarm.supervisor.Conductor` for *name*.

    Parameters
    ----------
    name:
        One of ``"stub"``, ``"claude"``, ``"api"``, ``"sdk"``.
        Default in production use is :data:`DEFAULT_CONDUCTOR` (``"api"``).
    model_override:
        Forwarded to the concrete conductor.  ``None`` means each head's
        ``default_model`` is used.
    demo_delay_s:
        Only meaningful for ``"stub"``; ignored by the other backends.

    Raises
    ------
    ValueError
        For an unrecognised *name* (shouldn't happen when called from a
        ``click.Choice``-validated CLI option, but guards programmatic use).
    """
    if name == "stub":
        return StubConductor(demo_delay_s=demo_delay_s)
    if name == "claude":
        import os

        if not os.environ.get("CLAUDE_SWARM_ALLOW_CLI_CONDUCTOR"):
            warnings.warn(_CLI_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
        return ClaudeCLIConductor(model_override=model_override)
    if name == "api":
        require_anthropic_api_key()
        return ApiConductor(model_override=model_override)
    if name == "sdk":
        return SDKConductor(model_override=model_override)
    raise ValueError(
        f"Unknown conductor name {name!r}; expected one of: stub, claude, api, sdk."
    )


__all__ = [
    "DEFAULT_CONDUCTOR",
    "MissingAnthropicApiKeyError",
    "build_conductor",
    "require_anthropic_api_key",
]
