"""Rate-limit resilience for the never-sleep supervisor loop.

A perpetual supervisor must never be killed by a provider rate limit. This
module is the thin, dependency-light wrapper every LLM call in the runtime
goes through so a ``429`` / ``overloaded_error`` / ``pause_turn`` backs off
and (optionally) rotates to another key or lane *instead of* tearing the
loop down.

The three primitives, each independently testable:

* :class:`TransientError` — the classification of a retryable failure, with
  the provider's own ``Retry-After`` / ``anthropic-ratelimit-*-reset`` hint
  parsed out so we wait exactly as long as the server told us to.
* :class:`BackoffPolicy` — exponential backoff with full jitter. It is
  **cache-aware**: it never produces a sleep inside the prompt-cache danger
  window around the 5-minute TTL (a bare ``sleep(300)`` wastes a full cache
  generation — global charter §10), nudging such sleeps to a safe value.
* :class:`KeyRotator` — an optional ordered ring of provider *lanes* (API
  keys / subscription plans). Keys are read **at call time** from a supplied
  resolver and are never logged or stored on the rotator; only an opaque
  lane *label* is ever surfaced (no-secrets-in-logs charter).

:func:`resilient_call` ties them together: it invokes a callable, and on a
transient error it sleeps per the server hint or the backoff policy, rotates
the lane when the same lane keeps failing, and re-raises only once the
attempt budget is exhausted. It is abort-aware via an injected predicate so
a stop request short-circuits the wait.

No network, no provider SDK, stdlib only — the caller supplies the actual
call. That keeps this module trivially testable and provider-agnostic.
"""
from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

#: HTTP statuses we treat as retryable transient throttling/overload.
#: 429 = rate limited; 529 = Anthropic "overloaded".
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 529})

#: ``stop_reason`` / error ``type`` values that mean "back off and resume",
#: not "the request was malformed". ``pause_turn`` is the server-tool turn
#: cap which is *resumed*, not failed.
RETRYABLE_REASONS: frozenset[str] = frozenset(
    {"overloaded_error", "rate_limit_error", "pause_turn", "api_error"}
)

#: The prompt-cache TTL we protect (Anthropic default ephemeral cache = 5 min).
#: A sleep that lands inside ``[TTL - GUARD, TTL + GUARD]`` is nudged below the
#: window so the next call still hits a warm cache.
CACHE_TTL_S: float = 300.0
CACHE_GUARD_S: float = 30.0
#: When a computed sleep falls in the cache danger window we clamp it here —
#: comfortably under the TTL so the cache survives the wait.
CACHE_SAFE_SLEEP_S: float = 270.0


@dataclass(frozen=True)
class TransientError(Exception):
    """A retryable failure surfaced from a provider call.

    ``retry_after_s`` is the server's own hint when present (from a
    ``Retry-After`` header or an ``anthropic-ratelimit-*-reset`` header); the
    backoff policy honours it in preference to its own schedule. ``status``
    and ``reason`` are kept for logging + the rotation decision.
    """

    message: str = "transient provider error"
    status: int | None = None
    reason: str | None = None
    retry_after_s: float | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        bits = [self.message]
        if self.status is not None:
            bits.append(f"status={self.status}")
        if self.reason is not None:
            bits.append(f"reason={self.reason}")
        if self.retry_after_s is not None:
            bits.append(f"retry_after={self.retry_after_s:.1f}s")
        return " ".join(bits)


def _parse_retry_after(value: str) -> float | None:
    """Parse a ``Retry-After`` header value (seconds form only).

    The HTTP spec also permits an HTTP-date; providers in practice send an
    integer/float number of seconds, which is what we support. A date or an
    unparseable value yields ``None`` (we fall back to the backoff policy).
    """
    value = value.strip()
    try:
        secs = float(value)
    except ValueError:
        return None
    return max(0.0, secs)


def retry_after_from_headers(headers: Mapping[str, str] | None) -> float | None:
    """Extract a wait hint (seconds) from rate-limit response headers.

    Recognises, in priority order:
      1. ``retry-after`` — seconds to wait (RFC 9110).
      2. ``anthropic-ratelimit-*-reset`` — seconds until the bucket resets.

    Header lookup is case-insensitive. Returns ``None`` when no usable hint
    is present so the caller falls back to exponential backoff.
    """
    if not headers:
        return None
    lower = {k.lower(): v for k, v in headers.items()}
    ra = lower.get("retry-after")
    if ra is not None:
        parsed = _parse_retry_after(ra)
        if parsed is not None:
            return parsed
    # Anthropic emits per-bucket reset headers, e.g.
    # ``anthropic-ratelimit-tokens-reset: 12``. Take the soonest reset.
    resets: list[float] = []
    for key, val in lower.items():
        if re.fullmatch(r"anthropic-ratelimit-[a-z-]+-reset", key):
            parsed = _parse_retry_after(val)
            if parsed is not None:
                resets.append(parsed)
    if resets:
        return min(resets)
    return None


def classify_error(exc: BaseException) -> TransientError | None:
    """Map an arbitrary exception to a :class:`TransientError`, or ``None``.

    Heuristic + duck-typed so it works with the Anthropic SDK's exceptions,
    plain ``httpx`` errors, or a hand-rolled error object — without importing
    any of them. We look for, in order:

      * an explicit :class:`TransientError` (passthrough);
      * a ``status_code`` / ``status`` attribute in :data:`RETRYABLE_STATUS`;
      * a ``response.headers`` mapping to pull the retry hint from;
      * a ``type`` / ``reason`` attribute in :data:`RETRYABLE_REASONS`;
      * the substring ``overloaded`` / ``rate limit`` / ``429`` / ``529`` in
        the message as a last resort.

    Anything else returns ``None`` — the caller re-raises it untouched (a
    real bug must not be silently retried forever).
    """
    if isinstance(exc, TransientError):
        return exc

    status = _first_int_attr(exc, ("status_code", "status"))
    headers = _headers_of(exc)
    retry_after = retry_after_from_headers(headers)
    reason = _first_str_attr(exc, ("type", "reason", "code"))

    if status in RETRYABLE_STATUS:
        return TransientError(
            message="retryable HTTP status",
            status=status,
            reason=reason,
            retry_after_s=retry_after,
        )
    if reason in RETRYABLE_REASONS:
        return TransientError(
            message="retryable provider reason",
            status=status,
            reason=reason,
            retry_after_s=retry_after,
        )
    text = str(exc).lower()
    if any(tok in text for tok in ("overloaded", "rate limit", "rate_limit", " 429", " 529")):
        return TransientError(
            message="retryable per message text",
            status=status,
            reason=reason,
            retry_after_s=retry_after,
        )
    return None


def _first_int_attr(obj: Any, names: Sequence[str]) -> int | None:
    for name in names:
        val = getattr(obj, name, None)
        if isinstance(val, bool):  # bool is an int subclass; never a status
            continue
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            try:
                return int(val)
            except ValueError:
                continue
    return None


def _first_str_attr(obj: Any, names: Sequence[str]) -> str | None:
    for name in names:
        val = getattr(obj, name, None)
        if isinstance(val, str) and val:
            return val
    return None


def _headers_of(obj: Any) -> Mapping[str, str] | None:
    headers = getattr(obj, "headers", None)
    if isinstance(headers, Mapping):
        return headers
    response = getattr(obj, "response", None)
    if response is not None:
        rh = getattr(response, "headers", None)
        if isinstance(rh, Mapping):
            return rh
    return None


def cache_safe_sleep(seconds: float) -> float:
    """Clamp ``seconds`` out of the prompt-cache danger window.

    A sleep inside ``[CACHE_TTL_S - CACHE_GUARD_S, CACHE_TTL_S + CACHE_GUARD_S]``
    is the worst case: long enough to look like a real wait, but it expires
    the warm cache for a full generation. We nudge such sleeps down to
    :data:`CACHE_SAFE_SLEEP_S` (just under the TTL). Sleeps well below or well
    above the window are returned unchanged — a deliberate multi-minute
    backoff is fine; only the *exactly-around-300s* case is the trap.
    """
    if seconds <= 0:
        return 0.0
    low = CACHE_TTL_S - CACHE_GUARD_S
    high = CACHE_TTL_S + CACHE_GUARD_S
    if low <= seconds <= high:
        return CACHE_SAFE_SLEEP_S
    return seconds


@dataclass
class BackoffPolicy:
    """Exponential backoff with full jitter, cache-aware.

    ``delay(attempt)`` returns the seconds to wait before retry ``attempt``
    (0-indexed: attempt 0 is the wait before the *second* try). The base
    schedule is ``base * factor**attempt`` capped at ``max_delay``, then full
    jitter (uniform in ``[0, schedule]``) is applied so a fleet of loops does
    not synchronise their retries, and finally :func:`cache_safe_sleep`
    nudges the result out of the cache danger window.
    """

    base_s: float = 1.0
    factor: float = 2.0
    max_delay_s: float = 60.0
    jitter: bool = True
    #: Injected for deterministic tests; defaults to the module RNG.
    rng: Callable[[float, float], float] = field(default=random.uniform, repr=False)

    def delay(self, attempt: int, *, retry_after_s: float | None = None) -> float:
        """Seconds to wait before the next attempt.

        When the server supplied a ``retry_after_s`` hint we honour it
        (clamped cache-safe) rather than our own schedule — the server knows
        its own reset window better than we do.
        """
        if retry_after_s is not None and retry_after_s > 0:
            return cache_safe_sleep(retry_after_s)
        attempt = max(0, attempt)
        raw = self.base_s * (self.factor ** attempt)
        raw = min(raw, self.max_delay_s)
        if self.jitter and raw > 0:
            raw = self.rng(0.0, raw)
        return cache_safe_sleep(raw)


@dataclass
class KeyRotator:
    """An ordered ring of provider *lanes* (keys / plans).

    Each lane is an opaque label (e.g. ``"max-plan-a"``, ``"metered-api"``).
    The actual secret is resolved lazily at call time by ``resolve`` and is
    never stored on this object — only the label ring is. That keeps secrets
    out of process memory longer than necessary and out of any log line.

    ``advance()`` rotates to the next lane and returns its label;
    ``current()`` returns the active label; ``credential()`` resolves the
    active lane's secret on demand. With a single lane, ``advance`` is a
    no-op (rotation cannot help a one-lane setup — backoff still applies).
    """

    lanes: tuple[str, ...]
    #: Maps a lane label to its secret. MUST read the secret fresh (env /
    #: keychain / file); the rotator never caches the returned value.
    resolve: Callable[[str], str | None] = field(default=lambda _label: None, repr=False)
    _idx: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.lanes:
            raise ValueError("KeyRotator requires at least one lane label")
        # Defensive: labels must not look like secrets themselves.
        for label in self.lanes:
            if not label or len(label) > 64:
                raise ValueError(f"invalid lane label: {label!r}")

    def current(self) -> str:
        return self.lanes[self._idx]

    def advance(self) -> str:
        """Rotate to the next lane; returns the new active label."""
        if len(self.lanes) > 1:
            self._idx = (self._idx + 1) % len(self.lanes)
        return self.current()

    def credential(self) -> str | None:
        """Resolve the active lane's secret at call time (never cached)."""
        return self.resolve(self.current())

    @property
    def num_lanes(self) -> int:
        return len(self.lanes)


@dataclass
class ResilientCallStats:
    """What :func:`resilient_call` did — for metrics + the status surface."""

    attempts: int = 0
    total_sleep_s: float = 0.0
    rotations: int = 0
    last_lane: str | None = None
    succeeded: bool = False


def resilient_call(
    fn: Callable[..., T],
    *,
    backoff: BackoffPolicy | None = None,
    rotator: KeyRotator | None = None,
    max_attempts: int = 6,
    rotate_after: int = 2,
    sleep: Callable[[float], None] = time.sleep,
    should_abort: Callable[[], bool] | None = None,
    stats: ResilientCallStats | None = None,
    **kwargs: Any,
) -> T:
    """Call ``fn(**kwargs)``, retrying transient throttling without dying.

    On a :class:`TransientError` (or any exception :func:`classify_error`
    recognises) we sleep per the server hint or the backoff schedule, and
    after ``rotate_after`` consecutive transient failures we rotate the
    rotator to the next lane and inject its credential as ``api_key`` into
    ``kwargs`` (only when a rotator is supplied). A non-transient exception
    is re-raised immediately — we never silently swallow real bugs.

    The loop is abort-aware: ``should_abort`` is checked before every sleep
    and before every attempt, so a stop request short-circuits the wait
    instead of blocking for the full backoff.

    Raises the last :class:`TransientError` once ``max_attempts`` is spent.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    backoff = backoff or BackoffPolicy()
    st = stats if stats is not None else ResilientCallStats()
    consecutive_transient = 0
    last_err: TransientError | None = None

    if rotator is not None:
        st.last_lane = rotator.current()
        cred = rotator.credential()
        if cred is not None:
            kwargs.setdefault("api_key", cred)

    for attempt in range(max_attempts):
        if should_abort is not None and should_abort():
            raise _abort_error(last_err) from None
        st.attempts = attempt + 1
        try:
            result = fn(**kwargs)
            st.succeeded = True
            return result
        except BaseException as exc:
            transient = classify_error(exc)
            if transient is None:
                raise
            last_err = transient
            consecutive_transient += 1
            log.warning(
                "transient call failure (attempt %d/%d): %s",
                attempt + 1,
                max_attempts,
                transient,
            )
            if attempt + 1 >= max_attempts:
                break
            if rotator is not None and consecutive_transient >= rotate_after:
                new_lane = rotator.advance()
                st.rotations += 1
                st.last_lane = new_lane
                consecutive_transient = 0
                cred = rotator.credential()
                if cred is not None:
                    kwargs["api_key"] = cred
                log.info("rotated provider lane -> %s", new_lane)
            wait_s = backoff.delay(attempt, retry_after_s=transient.retry_after_s)
            if should_abort is not None and should_abort():
                raise _abort_error(last_err) from None
            st.total_sleep_s += wait_s
            if wait_s > 0:
                sleep(wait_s)

    assert last_err is not None  # only reachable after a transient failure
    raise last_err


def _abort_error(last: TransientError | None) -> TransientError:
    return TransientError(
        message="resilient_call aborted before completion",
        reason="aborted",
        retry_after_s=last.retry_after_s if last else None,
    )


__all__ = [
    "CACHE_GUARD_S",
    "CACHE_SAFE_SLEEP_S",
    "CACHE_TTL_S",
    "RETRYABLE_REASONS",
    "RETRYABLE_STATUS",
    "BackoffPolicy",
    "KeyRotator",
    "ResilientCallStats",
    "TransientError",
    "cache_safe_sleep",
    "classify_error",
    "resilient_call",
    "retry_after_from_headers",
]
