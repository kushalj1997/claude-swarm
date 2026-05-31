"""Auto-switch governor: max the cheap plans, fall back to the API, recover.

The operator's rate-resilience policy, encoded as a small state machine over
the :class:`~claude_swarm.usage.UsageSnapshot`:

    run on cheap subscription plans by default; when those hit their usage
    limit, switch to the metered API swarm to stay autonomous; switch back
    when a cheap plan frees up.

States::

                ┌───────────────┐  cheap-plan headroom > enter_threshold
       START ──▶│  CHEAP_PLANS  │◀──────────────────────────────┐
                └──────┬────────┘                                │
                       │ all cheap lanes 429 / headroom ≤ exit   │ a cheap lane
                       ▼                                          │  recovers
                ┌───────────────┐                                │
                │  API_SWARM    │────────────────────────────────┘
                └──────┬────────┘
                       │ API spend ≥ daily budget cap
                       ▼
                ┌───────────────┐
                │  THROTTLED    │  batch-only + cheapest model until a lane frees
                └───────────────┘

Two properties keep it from thrashing (architecture §10 risk "rate-mode flap"):

* **Hysteresis** — the headroom level that *enters* CHEAP_PLANS
  (``enter_cheap_fraction``) is strictly above the level that *exits* it
  (``exit_cheap_fraction``); a lane hovering at one number can't oscillate.
* **Minimum dwell** — once a transition fires, the governor will not change
  mode again for ``min_dwell_s`` seconds.

The governor performs no I/O of its own beyond persisting its mode flag; it
reads usage via an injected :class:`~claude_swarm.usage.UsageTracker` and is
the single source of truth for "which lane should the dispatcher prefer right
now". The meta-supervisor owns one instance and broadcasts transitions.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from ._paths import state_dir
from .usage import Lane, UsageSnapshot, UsageTracker

log = logging.getLogger(__name__)


class Mode(str, Enum):
    """Which dispatch lane class the governor currently prefers."""

    CHEAP_PLANS = "cheap_plans"
    API_SWARM = "api_swarm"
    THROTTLED = "throttled"


@dataclass(frozen=True)
class GovernorConfig:
    """Tunables for :class:`Governor`.

    ``enter_cheap_fraction`` must be strictly greater than
    ``exit_cheap_fraction`` (validated) so the two thresholds form a hysteresis
    band rather than a single flappable line.
    """

    #: Re-enter CHEAP_PLANS only when a subscription lane is above this
    #: headroom fraction. Higher than the exit threshold (hysteresis).
    enter_cheap_fraction: float = 0.15
    #: Leave CHEAP_PLANS for the API once every subscription lane is at/below
    #: this headroom fraction (or throttled).
    exit_cheap_fraction: float = 0.05
    #: Daily metered-API budget cap in USD. Crossing it drops API_SWARM ->
    #: THROTTLED (charter §10 cost discipline).
    api_daily_budget_usd: float = 25.0
    #: Minimum seconds a mode must hold before another transition can fire.
    min_dwell_s: float = 120.0

    def __post_init__(self) -> None:
        if not (0.0 <= self.exit_cheap_fraction < self.enter_cheap_fraction <= 1.0):
            raise ValueError(
                "require 0 <= exit_cheap_fraction < enter_cheap_fraction <= 1 "
                f"(got exit={self.exit_cheap_fraction}, enter={self.enter_cheap_fraction})"
            )
        if self.api_daily_budget_usd < 0:
            raise ValueError("api_daily_budget_usd must be non-negative")
        if self.min_dwell_s < 0:
            raise ValueError("min_dwell_s must be non-negative")


@dataclass
class _GovernorState:
    """Persisted governor state (mode + when it last changed + API spend).

    ``mode_since`` is ``None`` until the first decision stamps it. A ``None``
    value means "never transitioned" and is treated as dwell-elapsed, so a
    freshly constructed governor is free to act on its very first tick instead
    of being frozen for ``min_dwell_s`` against an arbitrary wall-clock origin.
    """

    mode: Mode = Mode.CHEAP_PLANS
    mode_since: float | None = None
    #: Rolling metered-API spend, reset each UTC day boundary.
    api_spend_usd: float = 0.0
    api_spend_day: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "mode_since": self.mode_since,
            "api_spend_usd": round(self.api_spend_usd, 6),
            "api_spend_day": self.api_spend_day,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> _GovernorState:
        raw_since = d.get("mode_since")
        return cls(
            mode=Mode(d.get("mode", Mode.CHEAP_PLANS.value)),
            mode_since=None if raw_since is None else float(raw_since),
            api_spend_usd=float(d.get("api_spend_usd") or 0.0),
            api_spend_day=str(d.get("api_spend_day") or ""),
        )


@dataclass
class GovernorDecision:
    """The governor's answer for one tick: which lane to dispatch through."""

    mode: Mode
    lane: Lane
    reason: str
    changed: bool
    mode_since: float | None
    api_spend_usd: float
    api_daily_budget_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "lane": self.lane.value,
            "reason": self.reason,
            "changed": self.changed,
            "mode_since": self.mode_since,
            "api_spend_usd": round(self.api_spend_usd, 6),
            "api_daily_budget_usd": self.api_daily_budget_usd,
        }


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")


class Governor:
    """Owns the rate-mode flag and the lane the dispatcher should prefer.

    The typical call is :meth:`decide`, run on each supervisor tick (or on a
    bus event). It reads the current usage snapshot, applies hysteresis +
    dwell, persists any transition, and returns a :class:`GovernorDecision`.
    """

    def __init__(
        self,
        *,
        tracker: UsageTracker,
        config: GovernorConfig | None = None,
        path: Path | None = None,
        home: Path | None = None,
    ) -> None:
        self.tracker = tracker
        self.config = config or GovernorConfig()
        self.path = Path(path) if path is not None else state_dir(home) / "governor.json"
        self._state = self._load()

    # ----- persistence ----------------------------------------------

    def _load(self) -> _GovernorState:
        if not self.path.exists():
            return _GovernorState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("governor state at %s unreadable; starting fresh", self.path)
            return _GovernorState()
        try:
            return _GovernorState.from_dict(data)
        except (KeyError, ValueError):
            return _GovernorState()

    def _save(self) -> None:
        _atomic_write(self.path, json.dumps(self._state.to_dict(), indent=2))

    # ----- API spend accounting --------------------------------------

    def record_api_spend(self, usd: float, *, now: float | None = None) -> None:
        """Add metered-API spend toward the daily budget.

        Spend rolls over at the UTC day boundary so the budget is a true daily
        cap, not a since-forever total.
        """
        if usd < 0:
            raise ValueError("usd must be non-negative")
        now = time.time() if now is None else now
        self._roll_day(now)
        self._state.api_spend_usd += usd
        self._save()

    def _roll_day(self, now: float) -> None:
        today = _utc_day(now)
        if self._state.api_spend_day != today:
            self._state.api_spend_day = today
            self._state.api_spend_usd = 0.0
            # Persist immediately so the rollover survives a process restart
            # even on a read-mostly tick that makes no other state change.
            self._save()

    # ----- the decision ----------------------------------------------

    @property
    def mode(self) -> Mode:
        return self._state.mode

    def _lane_for_mode(self, mode: Mode, snapshot: UsageSnapshot) -> Lane:
        if mode is Mode.CHEAP_PLANS:
            best = snapshot.best_subscription_lane()
            return best.lane if best is not None else Lane.API
        if mode is Mode.THROTTLED:
            # We're here because the metered API hit its daily budget. A free
            # subscription lane (if any survived) is strictly better than the
            # capped API, so prefer it; the THROTTLED *mode* still tells the
            # caller to restrict to batch + cheapest model regardless of lane.
            best = snapshot.best_subscription_lane()
            return best.lane if best is not None else Lane.API
        # API_SWARM dispatches through the metered API.
        return Lane.API

    def _transition(self, new_mode: Mode, now: float) -> None:
        if new_mode is not self._state.mode:
            log.info("governor mode %s -> %s", self._state.mode.value, new_mode.value)
            self._state.mode = new_mode
            self._state.mode_since = now
            self._save()

    def decide(self, *, now: float | None = None) -> GovernorDecision:
        """Evaluate usage + budget and return the lane to dispatch through.

        Honours the minimum-dwell guard: if the current mode is younger than
        ``min_dwell_s`` the mode is held (no transition) even if thresholds
        would otherwise fire — except the budget-cap -> THROTTLED guard, which
        is a hard safety stop and bypasses dwell.
        """
        now = time.time() if now is None else now
        self._roll_day(now)
        # Anchor the dwell clock on the first-ever decision so a fresh
        # governor isn't frozen against an arbitrary wall-clock origin.
        if self._state.mode_since is None:
            self._state.mode_since = now
            self._save()
        snap = self.tracker.snapshot(now=now)
        cfg = self.config

        over_budget = self._state.api_spend_usd >= cfg.api_daily_budget_usd
        has_enter_headroom = snap.any_subscription_headroom(
            min_fraction=cfg.enter_cheap_fraction
        )
        has_exit_headroom = snap.any_subscription_headroom(
            min_fraction=cfg.exit_cheap_fraction
        )

        dwell_ok = (now - self._state.mode_since) >= cfg.min_dwell_s
        current = self._state.mode
        reason = "held: within dwell" if not dwell_ok else "held: no threshold crossed"
        target = current

        # Budget cap is a hard stop — bypasses dwell.
        if over_budget:
            target = Mode.THROTTLED
            reason = (
                f"api spend ${self._state.api_spend_usd:.4f} >= "
                f"budget ${cfg.api_daily_budget_usd:.2f}"
            )
        elif dwell_ok:
            if current is Mode.CHEAP_PLANS:
                if not has_exit_headroom:
                    target = Mode.API_SWARM
                    reason = (
                        "all subscription lanes throttled or "
                        f"<= {cfg.exit_cheap_fraction:.0%} headroom"
                    )
            elif current is Mode.API_SWARM:
                if has_enter_headroom:
                    target = Mode.CHEAP_PLANS
                    reason = (
                        "a subscription lane recovered above "
                        f"{cfg.enter_cheap_fraction:.0%} headroom"
                    )
            elif current is Mode.THROTTLED:
                # Budget no longer over (checked above). Pick the better lane.
                if has_enter_headroom:
                    target = Mode.CHEAP_PLANS
                    reason = "budget recovered; subscription headroom available"
                else:
                    target = Mode.API_SWARM
                    reason = "budget recovered; no subscription headroom -> api"
        # else: within dwell, hold current mode (reason already set)

        changed = target is not current
        self._transition(target, now)

        lane = self._lane_for_mode(self._state.mode, snap)
        return GovernorDecision(
            mode=self._state.mode,
            lane=lane,
            reason=reason,
            changed=changed,
            mode_since=self._state.mode_since,
            api_spend_usd=self._state.api_spend_usd,
            api_daily_budget_usd=cfg.api_daily_budget_usd,
        )


__all__ = [
    "Governor",
    "GovernorConfig",
    "GovernorDecision",
    "Mode",
]
