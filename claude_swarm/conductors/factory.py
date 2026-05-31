"""build_conductor() — single selection seam for all four conductor backends.

Called from both the ``run`` and ``perpetual`` CLI commands so the two
if/elif blocks stay in lockstep.  Existing ``stub``/``claude`` return values
are byte-identical to the blocks they replace.
"""
from __future__ import annotations

from ..conductor import ClaudeCLIConductor
from ..supervisor import Conductor, StubConductor
from .api import ApiConductor
from .sdk import SDKConductor


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
        return ClaudeCLIConductor(model_override=model_override)
    if name == "api":
        return ApiConductor(model_override=model_override)
    if name == "sdk":
        return SDKConductor(model_override=model_override)
    raise ValueError(
        f"Unknown conductor name {name!r}; expected one of: stub, claude, api, sdk."
    )


__all__ = ["build_conductor"]
