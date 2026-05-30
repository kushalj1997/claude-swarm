"""Usage-limits tracking across provider lanes.

The swarm runs against several billing lanes that each have their own usage
ceiling:

* **subscription plans** (Claude Code on a Max plan, Cursor, Codex) — billed
  flat, ~free at the margin, but capped by a rolling usage window that, when
  exhausted, returns 429 / "limit reached" until the window resets.
* the **metered API** (raw ``POST /v1/messages``) — pay-as-you-go, never
  hard-stalled because we own the backoff, but it costs real USD and exposes
  precise ``anthropic-ratelimit-*`` response headers.

This module tracks *headroom* — how much of each lane is left before its
limit — from whatever signal a lane actually emits:

* For the API lane, the response carries explicit headers
  (``anthropic-ratelimit-tokens-remaining``, ``...-reset``) we record verbatim.
* For subscription lanes we have no header, so headroom is *inferred* from a
  declared plan cap minus observed consumption in the rolling window, and a
  429 collapses headroom to zero until the observed reset elapses.

The governor (:mod:`claude_swarm.governor`) reads a :class:`UsageSnapshot`
to decide which lane to dispatch to. This module owns only the bookkeeping;
it makes no routing decisions and performs no network I/O.

Design mirrors the rest of the package: stdlib only, atomic JSON persistence
(``tmp + os.replace`` like :mod:`claude_swarm.messaging`), and dataclasses
with explicit ``to_dict`` for the CLI surface.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from ._paths import state_dir

log = logging.getLogger(__name__)

#: Default rolling window over which a lane's consumption accrues toward its
#: cap, in seconds. Subscription plans commonly meter on a multi-hour window;
#: 5h (18000s) is a conservative default the operator can override per lane.
DEFAULT_WINDOW_S: int = 18_000

#: When a lane reports a 429 but no explicit reset, assume the limit clears
#: after this many seconds (one full default window). Pessimistic on purpose:
#: better to under-use a throttled lane than to hammer it into a longer ban.
DEFAULT_REASSESS_AFTER_S: int = DEFAULT_WINDOW_S


class Lane(str, Enum):
    """A distinct billing/throttling lane the swarm can dispatch through."""

    CLAUDE_CODE_MAX = "claude-code-max"
    CURSOR = "cursor"
    CODEX = "codex"
    API = "api"

    @property
    def is_subscription(self) -> bool:
        """True for flat-billed plan lanes (everything except the metered API)."""
        return self is not Lane.API


@dataclass
class _Event:
    """One recorded unit of consumption against a lane, oldest-first on disk."""

    ts: float
    tokens: int
    requests: int

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "tokens": self.tokens, "requests": self.requests}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> _Event:
        return cls(
            ts=float(d.get("ts") or 0.0),
            tokens=int(d.get("tokens") or 0),
            requests=int(d.get("requests") or 0),
        )


@dataclass
class LaneState:
    """Mutable per-lane bookkeeping persisted to disk.

    ``token_cap`` is the lane's usage ceiling per :attr:`window_s` window
    (declared for subscription plans; mirrored from the API's reported limit
    for the API lane). ``token_cap <= 0`` means "unknown / uncapped" — headroom
    is then reported as ``None`` rather than a fabricated number.
    """

    lane: Lane
    token_cap: int = 0
    window_s: int = DEFAULT_WINDOW_S
    events: list[_Event] = field(default_factory=list)
    last_429_ts: float | None = None
    #: Wall-clock time at which a throttled lane is expected to recover. Set
    #: from an observed ``retry-after`` / ``...-reset`` header, else inferred.
    reset_at: float | None = None
    #: Verbatim headroom from a provider header (API lane). When present it
    #: overrides the inferred estimate. ``None`` => infer from cap - used.
    reported_tokens_remaining: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane.value,
            "token_cap": self.token_cap,
            "window_s": self.window_s,
            "events": [e.to_dict() for e in self.events],
            "last_429_ts": self.last_429_ts,
            "reset_at": self.reset_at,
            "reported_tokens_remaining": self.reported_tokens_remaining,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LaneState:
        return cls(
            lane=Lane(d["lane"]),
            token_cap=int(d.get("token_cap") or 0),
            window_s=int(d.get("window_s") or DEFAULT_WINDOW_S),
            events=[_Event.from_dict(e) for e in d.get("events", []) if isinstance(e, dict)],
            last_429_ts=_opt_float(d.get("last_429_ts")),
            reset_at=_opt_float(d.get("reset_at")),
            reported_tokens_remaining=_opt_int(d.get("reported_tokens_remaining")),
        )

    # ----- derived, window-relative views ----------------------------

    def _live_events(self, now: float) -> list[_Event]:
        cutoff = now - max(1, self.window_s)
        return [e for e in self.events if e.ts > cutoff and e.ts <= now]

    def tokens_used(self, now: float) -> int:
        return sum(e.tokens for e in self._live_events(now))

    def requests_made(self, now: float) -> int:
        return sum(e.requests for e in self._live_events(now))

    def is_throttled(self, now: float) -> bool:
        """True if the lane is in a 429 cooldown that hasn't yet elapsed."""
        if self.reset_at is None:
            return False
        return now < self.reset_at

    def headroom_tokens(self, now: float) -> int | None:
        """Estimated tokens remaining before this lane hits its limit.

        Resolution order:
            1. throttled (in 429 cooldown) -> 0
            2. a provider-reported remaining count -> that, verbatim
            3. a known cap -> ``max(0, cap - used_in_window)``
            4. unknown cap -> ``None`` (do not fabricate a number)
        """
        if self.is_throttled(now):
            return 0
        # A provider-reported remaining count is only authoritative while it
        # is fresh. Once the throttle window we recorded alongside it has
        # elapsed (``reset_at`` in the past), a stale ``remaining == 0`` would
        # otherwise pin headroom to zero forever; treat it as expired and fall
        # through to the cap-based estimate.
        reported = self.reported_tokens_remaining
        expired = self.reset_at is not None and now >= self.reset_at
        if reported is not None and not expired:
            return max(0, reported)
        if self.token_cap > 0:
            return max(0, self.token_cap - self.tokens_used(now))
        return None

    def headroom_fraction(self, now: float) -> float | None:
        """Headroom as a fraction of the cap in ``[0.0, 1.0]`` (``None`` if unknown).

        For a provider-reported remaining count without a known cap we cannot
        compute a fraction, so this returns ``None`` even though
        :meth:`headroom_tokens` may be a concrete number.
        """
        if self.is_throttled(now):
            return 0.0
        if self.token_cap <= 0:
            return None
        remaining = self.headroom_tokens(now)
        if remaining is None:
            return None
        return max(0.0, min(1.0, remaining / self.token_cap))


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _opt_int(v: Any) -> int | None:
    return None if v is None else int(v)


@dataclass
class LaneView:
    """A read-only, point-in-time projection of one lane for the CLI/governor."""

    lane: Lane
    is_subscription: bool
    token_cap: int
    window_s: int
    tokens_used: int
    requests_made: int
    headroom_tokens: int | None
    headroom_fraction: float | None
    throttled: bool
    last_429_ts: float | None
    reset_at: float | None
    seconds_to_reset: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane.value,
            "is_subscription": self.is_subscription,
            "token_cap": self.token_cap,
            "window_s": self.window_s,
            "tokens_used": self.tokens_used,
            "requests_made": self.requests_made,
            "headroom_tokens": self.headroom_tokens,
            "headroom_fraction": (
                None if self.headroom_fraction is None else round(self.headroom_fraction, 4)
            ),
            "throttled": self.throttled,
            "last_429_ts": self.last_429_ts,
            "reset_at": self.reset_at,
            "seconds_to_reset": (
                None if self.seconds_to_reset is None else round(self.seconds_to_reset, 1)
            ),
        }


@dataclass
class UsageSnapshot:
    """Immutable rollup across every lane at a single instant."""

    ts: float
    lanes: list[LaneView]

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "lanes": [lv.to_dict() for lv in self.lanes]}

    def lane(self, lane: Lane) -> LaneView | None:
        for lv in self.lanes:
            if lv.lane is lane:
                return lv
        return None

    def best_subscription_lane(self) -> LaneView | None:
        """Return the un-throttled subscription lane with the most headroom.

        Lanes with unknown headroom (no cap declared) are treated as having
        *some* headroom and rank below any lane with a concrete positive
        fraction, but above throttled lanes. ``None`` if every subscription
        lane is throttled.
        """
        candidates = [
            lv for lv in self.lanes if lv.is_subscription and not lv.throttled
        ]
        if not candidates:
            return None

        def _key(lv: LaneView) -> tuple[int, float]:
            # Ordering, best first:
            #   tier 2: a known *positive* fraction (rank by the fraction)
            #   tier 1: unknown headroom (no cap declared) — not proven empty
            #   tier 0: a known *zero* fraction (provably exhausted)
            # so an undeclared lane outranks a lane we know is empty.
            frac = lv.headroom_fraction
            if frac is None:
                return (1, 0.0)
            if frac <= 0.0:
                return (0, 0.0)
            return (2, frac)

        return max(candidates, key=_key)

    def any_subscription_headroom(self, *, min_fraction: float) -> bool:
        """True if at least one subscription lane is above ``min_fraction``.

        A lane with unknown headroom (no declared cap) counts as having
        headroom — we cannot prove it's exhausted, and a 429 would flip it
        to throttled, which *is* observable.
        """
        for lv in self.lanes:
            if not lv.is_subscription or lv.throttled:
                continue
            if lv.headroom_fraction is None or lv.headroom_fraction > min_fraction:
                return True
        return False


def _atomic_write(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` via a same-dir tmp file + rename."""
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


class UsageTracker:
    """Records per-lane consumption + rate-limit signals; reports headroom.

    Persistence is a single JSON document under the swarm state dir, written
    atomically. Single-writer by design (the governor/meta-supervisor), like
    the rest of the swarm's state plane.

    All time-dependent methods accept an injectable ``now`` for deterministic
    tests; production callers omit it and get ``time.time()``.
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        home: Path | None = None,
        plan_caps: dict[Lane, int] | None = None,
        window_s: int = DEFAULT_WINDOW_S,
    ) -> None:
        self.path = Path(path) if path is not None else state_dir(home) / "usage.json"
        self.window_s = window_s
        self._lanes: dict[Lane, LaneState] = {}
        self._load()
        # Seed/refresh declared subscription caps without clobbering recorded
        # events or 429 state on an already-tracked lane.
        for lane in Lane:
            st = self._lanes.get(lane)
            if st is None:
                st = LaneState(lane=lane, window_s=window_s)
                self._lanes[lane] = st
            if plan_caps and lane in plan_caps:
                st.token_cap = int(plan_caps[lane])

    # ----- persistence ----------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("usage state at %s unreadable; starting fresh", self.path)
            return
        for row in data.get("lanes", []):
            if not isinstance(row, dict):
                continue
            try:
                st = LaneState.from_dict(row)
            except (KeyError, ValueError):
                continue
            self._lanes[st.lane] = st

    def _save(self) -> None:
        payload = {"lanes": [st.to_dict() for st in self._lanes.values()]}
        _atomic_write(self.path, json.dumps(payload, indent=2))

    def _prune(self, st: LaneState, now: float) -> None:
        """Drop events older than the window so the file can't grow unbounded.

        Keep the throttle/reset fields untouched — they outlive the window.
        """
        cutoff = now - max(1, st.window_s)
        st.events = [e for e in st.events if e.ts > cutoff and e.ts <= now]

    # ----- recording -------------------------------------------------

    def record_usage(
        self,
        lane: Lane,
        *,
        tokens: int,
        requests: int = 1,
        now: float | None = None,
    ) -> None:
        """Record successful consumption against ``lane``.

        ``tokens`` is the total billable tokens for the call(s) (input +
        output + cache, however the caller chooses to count) and ``requests``
        the number of API calls those tokens cover (default 1).
        """
        if tokens < 0 or requests < 0:
            raise ValueError("tokens and requests must be non-negative")
        now = time.time() if now is None else now
        st = self._lanes[lane]
        st.events.append(_Event(ts=now, tokens=int(tokens), requests=int(requests)))
        self._prune(st, now)
        self._save()

    def record_rate_limit(
        self,
        lane: Lane,
        *,
        retry_after_s: float | None = None,
        now: float | None = None,
    ) -> None:
        """Record a 429 / usage-limit hit on ``lane``.

        Collapses the lane's headroom to zero until ``reset_at``. ``reset_at``
        is ``now + retry_after_s`` when the provider supplied a ``Retry-After``,
        else ``now + DEFAULT_REASSESS_AFTER_S``.
        """
        now = time.time() if now is None else now
        st = self._lanes[lane]
        st.last_429_ts = now
        delay = (
            float(retry_after_s)
            if retry_after_s is not None and retry_after_s > 0
            else float(DEFAULT_REASSESS_AFTER_S)
        )
        st.reset_at = now + delay
        self._save()

    def apply_anthropic_headers(
        self,
        lane: Lane,
        headers: dict[str, str],
        *,
        now: float | None = None,
    ) -> None:
        """Ingest ``anthropic-ratelimit-*`` response headers for ``lane``.

        Recognised keys (case-insensitive, hyphen form):
            * ``anthropic-ratelimit-tokens-limit``     -> token_cap
            * ``anthropic-ratelimit-tokens-remaining`` -> reported_tokens_remaining
            * ``anthropic-ratelimit-tokens-reset``     -> reset_at (RFC3339 or +Ns)
            * ``retry-after``                          -> 429 cooldown (seconds)

        Unknown / malformed values are ignored rather than raising — header
        shapes drift across API versions and we never want telemetry parsing
        to crash a dispatch loop.
        """
        now = time.time() if now is None else now
        h = {k.lower(): v for k, v in headers.items()}
        st = self._lanes[lane]

        limit = _safe_int(h.get("anthropic-ratelimit-tokens-limit"))
        if limit is not None and limit > 0:
            st.token_cap = limit

        remaining = _safe_int(h.get("anthropic-ratelimit-tokens-remaining"))
        if remaining is not None:
            st.reported_tokens_remaining = max(0, remaining)

        reset_at = _parse_reset(h.get("anthropic-ratelimit-tokens-reset"), now=now)
        retry_after = _safe_float(h.get("retry-after"))
        if retry_after is not None and retry_after > 0:
            # An explicit Retry-After always means we're throttled now.
            st.last_429_ts = now
            st.reset_at = now + retry_after
        elif reset_at is not None and remaining is not None and remaining <= 0:
            # Remaining hit zero with a reset time — treat as a soft throttle.
            st.last_429_ts = now
            st.reset_at = reset_at

        self._save()

    def clear_throttle(self, lane: Lane, *, now: float | None = None) -> None:
        """Manually clear a lane's throttle (e.g. operator override / reset)."""
        st = self._lanes[lane]
        st.reset_at = None
        st.reported_tokens_remaining = None
        self._save()

    def set_cap(self, lane: Lane, *, tokens: int, window_s: int | None = None) -> None:
        """Declare (and persist) a lane's token cap per window.

        ``tokens <= 0`` marks the lane uncapped (headroom reported as
        ``None``). Optionally override the rolling window for this lane.
        """
        st = self._lanes[lane]
        st.token_cap = int(tokens)
        if window_s is not None:
            st.window_s = int(window_s)
        self._save()

    # ----- reporting -------------------------------------------------

    def lane_view(self, lane: Lane, *, now: float | None = None) -> LaneView:
        now = time.time() if now is None else now
        st = self._lanes[lane]
        reset_at = st.reset_at
        seconds_to_reset = None
        if reset_at is not None and reset_at > now:
            seconds_to_reset = reset_at - now
        return LaneView(
            lane=lane,
            is_subscription=lane.is_subscription,
            token_cap=st.token_cap,
            window_s=st.window_s,
            tokens_used=st.tokens_used(now),
            requests_made=st.requests_made(now),
            headroom_tokens=st.headroom_tokens(now),
            headroom_fraction=st.headroom_fraction(now),
            throttled=st.is_throttled(now),
            last_429_ts=st.last_429_ts,
            reset_at=reset_at,
            seconds_to_reset=seconds_to_reset,
        )

    def snapshot(self, *, now: float | None = None) -> UsageSnapshot:
        now = time.time() if now is None else now
        return UsageSnapshot(
            ts=now,
            lanes=[self.lane_view(lane, now=now) for lane in Lane],
        )


def _safe_int(v: str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe_float(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_reset(v: str | None, *, now: float) -> float | None:
    """Parse an ``anthropic-ratelimit-*-reset`` value into an absolute epoch.

    Accepts three shapes seen in the wild:
        * a bare number of seconds-until-reset (``"42"``) -> ``now + 42``
        * a ``"+42s"`` duration                            -> ``now + 42``
        * an RFC3339 timestamp (``"2026-05-29T21:00:00Z"``) -> its epoch
    Returns ``None`` on anything it can't parse.
    """
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    # +Ns / Ns duration forms
    dur = v[:-1] if v.endswith("s") else v
    if dur.startswith("+"):
        dur = dur[1:]
    f = _safe_float(dur)
    if f is not None:
        return now + f
    # RFC3339 absolute timestamp
    try:
        iso = v.replace("Z", "+00:00")
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


__all__ = [
    "DEFAULT_REASSESS_AFTER_S",
    "DEFAULT_WINDOW_S",
    "Lane",
    "LaneState",
    "LaneView",
    "UsageSnapshot",
    "UsageTracker",
]
