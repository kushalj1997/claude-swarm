"""conductors — API / SDK conductor implementations + the build_conductor factory.

This subpackage is a sibling to ``claude_swarm/conductor.py``; it does NOT
move or modify the existing reference conductors
(:class:`~claude_swarm.conductor.SubprocessConductor`,
:class:`~claude_swarm.conductor.ClaudeCLIConductor`).

Imports are safe when neither ``anthropic`` nor ``claude_agent_sdk`` is
installed — the optional deps are imported lazily inside each conductor's
``dispatch()`` method, not at module load time.
"""
from __future__ import annotations

from .api import ApiConductor
from .factory import build_conductor
from .sdk import SDKConductor

__all__ = [
    "ApiConductor",
    "SDKConductor",
    "build_conductor",
]
