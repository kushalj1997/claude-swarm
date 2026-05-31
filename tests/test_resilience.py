"""Tests for the rate-limit resilience primitives."""
from __future__ import annotations

import pytest

from claude_swarm.resilience import (
    CACHE_SAFE_SLEEP_S,
    BackoffPolicy,
    KeyRotator,
    ResilientCallStats,
    TransientError,
    cache_safe_sleep,
    classify_error,
    resilient_call,
    retry_after_from_headers,
)

# ----- header parsing -------------------------------------------------


def test_retry_after_seconds_form() -> None:
    assert retry_after_from_headers({"Retry-After": "12"}) == 12.0


def test_retry_after_case_insensitive() -> None:
    assert retry_after_from_headers({"retry-after": "3.5"}) == 3.5


def test_retry_after_anthropic_reset_takes_soonest() -> None:
    headers = {
        "anthropic-ratelimit-requests-reset": "60",
        "anthropic-ratelimit-tokens-reset": "5",
    }
    assert retry_after_from_headers(headers) == 5.0


def test_retry_after_prefers_retry_after_over_reset() -> None:
    headers = {"Retry-After": "2", "anthropic-ratelimit-tokens-reset": "99"}
    assert retry_after_from_headers(headers) == 2.0


def test_retry_after_none_for_missing() -> None:
    assert retry_after_from_headers(None) is None
    assert retry_after_from_headers({}) is None
    assert retry_after_from_headers({"x-other": "1"}) is None


def test_retry_after_none_for_http_date() -> None:
    # We only support the seconds form; an HTTP-date yields None (fallback).
    assert retry_after_from_headers({"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"}) is None


# ----- error classification -------------------------------------------


def test_classify_passthrough_transient() -> None:
    err = TransientError(message="x", status=429)
    assert classify_error(err) is err


def test_classify_status_429() -> None:
    class FakeHTTP(Exception):
        status_code = 429

    t = classify_error(FakeHTTP("rate limited"))
    assert t is not None
    assert t.status == 429


def test_classify_status_529_overloaded() -> None:
    class Overloaded(Exception):
        status = 529

    t = classify_error(Overloaded("overloaded"))
    assert t is not None and t.status == 529


def test_classify_reason_pause_turn() -> None:
    class Paused(Exception):
        type = "pause_turn"

    t = classify_error(Paused("paused"))
    assert t is not None and t.reason == "pause_turn"


def test_classify_pulls_retry_after_from_response_headers() -> None:
    class Resp:
        def __init__(self) -> None:
            self.headers = {"retry-after": "7"}

    class HTTPErr(Exception):
        status_code = 429

        def __init__(self, msg: str) -> None:
            super().__init__(msg)
            self.response = Resp()

    t = classify_error(HTTPErr("boom"))
    assert t is not None and t.retry_after_s == 7.0


def test_classify_message_text_fallback() -> None:
    t = classify_error(RuntimeError("Error: server overloaded, try later"))
    assert t is not None


def test_classify_non_transient_returns_none() -> None:
    assert classify_error(ValueError("bad input")) is None
    assert classify_error(KeyError("missing")) is None


def test_classify_bool_is_not_a_status() -> None:
    # bool is an int subclass; a truthy attribute must not be read as status.
    class Weird(Exception):
        status = True

    assert classify_error(Weird("nope")) is None


# ----- cache-safe sleep -----------------------------------------------


def test_cache_safe_sleep_nudges_300() -> None:
    assert cache_safe_sleep(300.0) == CACHE_SAFE_SLEEP_S


def test_cache_safe_sleep_nudges_window_edges() -> None:
    assert cache_safe_sleep(275.0) == CACHE_SAFE_SLEEP_S
    assert cache_safe_sleep(325.0) == CACHE_SAFE_SLEEP_S


def test_cache_safe_sleep_passes_small_and_large() -> None:
    assert cache_safe_sleep(5.0) == 5.0
    assert cache_safe_sleep(1800.0) == 1800.0
    assert cache_safe_sleep(0.0) == 0.0
    assert cache_safe_sleep(-3.0) == 0.0


# ----- backoff policy -------------------------------------------------


def test_backoff_exponential_without_jitter() -> None:
    pol = BackoffPolicy(base_s=1.0, factor=2.0, jitter=False)
    assert pol.delay(0) == 1.0
    assert pol.delay(1) == 2.0
    assert pol.delay(2) == 4.0


def test_backoff_capped_at_max() -> None:
    pol = BackoffPolicy(base_s=1.0, factor=10.0, max_delay_s=5.0, jitter=False)
    assert pol.delay(5) == 5.0


def test_backoff_honours_retry_after_over_schedule() -> None:
    pol = BackoffPolicy(base_s=1.0, factor=2.0, jitter=False)
    assert pol.delay(3, retry_after_s=2.0) == 2.0


def test_backoff_full_jitter_within_bounds() -> None:
    pol = BackoffPolicy(base_s=1.0, factor=2.0, jitter=True)
    for attempt in range(5):
        for _ in range(50):
            d = pol.delay(attempt)
            assert 0.0 <= d <= min(1.0 * 2 ** attempt, pol.max_delay_s) + 1e-9


def test_backoff_deterministic_rng_injection() -> None:
    pol = BackoffPolicy(base_s=8.0, factor=2.0, jitter=True, rng=lambda lo, hi: hi)
    assert pol.delay(0) == 8.0  # rng returns the high bound -> full schedule


def test_backoff_negative_attempt_clamped() -> None:
    pol = BackoffPolicy(base_s=1.0, factor=2.0, jitter=False)
    assert pol.delay(-5) == 1.0


# ----- key rotator ----------------------------------------------------


def test_rotator_single_lane_advance_is_noop() -> None:
    rot = KeyRotator(lanes=("only",))
    assert rot.current() == "only"
    assert rot.advance() == "only"
    assert rot.num_lanes == 1


def test_rotator_cycles_through_lanes() -> None:
    rot = KeyRotator(lanes=("a", "b", "c"))
    assert rot.current() == "a"
    assert rot.advance() == "b"
    assert rot.advance() == "c"
    assert rot.advance() == "a"  # wraps


def test_rotator_resolves_credential_lazily() -> None:
    seen: list[str] = []

    def resolve(label: str) -> str | None:
        seen.append(label)
        return f"secret-for-{label}"

    rot = KeyRotator(lanes=("a", "b"), resolve=resolve)
    assert rot.credential() == "secret-for-a"
    rot.advance()
    assert rot.credential() == "secret-for-b"
    assert seen == ["a", "b"]


def test_rotator_rejects_empty_lanes() -> None:
    with pytest.raises(ValueError):
        KeyRotator(lanes=())


def test_rotator_rejects_secret_looking_label() -> None:
    with pytest.raises(ValueError):
        KeyRotator(lanes=("x" * 100,))  # too long to be a label


# ----- resilient_call -------------------------------------------------


def test_resilient_call_returns_on_success() -> None:
    stats = ResilientCallStats()
    result = resilient_call(lambda **_: 42, sleep=lambda _s: None, stats=stats)
    assert result == 42
    assert stats.succeeded is True
    assert stats.attempts == 1


def test_resilient_call_rejects_zero_max_attempts() -> None:
    # A misconfigured caller gets a clear ValueError, not an opaque assertion.
    with pytest.raises(ValueError, match="max_attempts"):
        resilient_call(lambda **_: 1, max_attempts=0, sleep=lambda _s: None)


def test_resilient_call_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky(**_: object) -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError(message="throttled", status=429)
        return "ok"

    slept: list[float] = []
    stats = ResilientCallStats()
    out = resilient_call(
        flaky,
        backoff=BackoffPolicy(base_s=0.5, factor=2.0, jitter=False),
        sleep=slept.append,
        stats=stats,
    )
    assert out == "ok"
    assert calls["n"] == 3
    assert stats.attempts == 3
    assert slept == [0.5, 1.0]  # slept before each of the two retries


def test_resilient_call_reraises_non_transient_immediately() -> None:
    def boom(**_: object) -> None:
        raise ValueError("real bug")

    with pytest.raises(ValueError, match="real bug"):
        resilient_call(boom, sleep=lambda _s: None)


def test_resilient_call_exhausts_attempts_and_raises_transient() -> None:
    def always_429(**_: object) -> None:
        raise TransientError(message="throttled", status=429)

    stats = ResilientCallStats()
    with pytest.raises(TransientError):
        resilient_call(
            always_429,
            backoff=BackoffPolicy(base_s=0.0, jitter=False),
            max_attempts=3,
            sleep=lambda _s: None,
            stats=stats,
        )
    assert stats.attempts == 3
    assert stats.succeeded is False


def test_resilient_call_rotates_lane_after_threshold() -> None:
    rot = KeyRotator(lanes=("a", "b"), resolve=lambda label: f"key-{label}")
    seen_keys: list[str] = []

    def flaky(**kwargs: object) -> str:
        seen_keys.append(str(kwargs.get("api_key")))
        if len(seen_keys) < 3:
            raise TransientError(message="throttled", status=429)
        return "done"

    stats = ResilientCallStats()
    out = resilient_call(
        flaky,
        backoff=BackoffPolicy(base_s=0.0, jitter=False),
        rotator=rot,
        rotate_after=2,
        sleep=lambda _s: None,
        stats=stats,
    )
    assert out == "done"
    # First two attempts use lane a's key; after 2 transient failures it rotates to b.
    assert seen_keys[0] == "key-a"
    assert seen_keys[-1] == "key-b"
    assert stats.rotations == 1


def test_resilient_call_injects_credential_without_overwriting() -> None:
    rot = KeyRotator(lanes=("a",), resolve=lambda _l: "resolved-key")
    captured: dict[str, object] = {}

    def fn(**kwargs: object) -> str:
        captured.update(kwargs)
        return "ok"

    # Caller-supplied api_key wins (setdefault) — we never clobber an explicit one.
    resilient_call(fn, rotator=rot, api_key="explicit", sleep=lambda _s: None)
    assert captured["api_key"] == "explicit"


def test_resilient_call_aborts_before_sleeping() -> None:
    def always_429(**_: object) -> None:
        raise TransientError(message="throttled", status=429)

    slept: list[float] = []
    with pytest.raises(TransientError, match="aborted"):
        resilient_call(
            always_429,
            backoff=BackoffPolicy(base_s=5.0, jitter=False),
            should_abort=lambda: True,
            sleep=slept.append,
        )
    # Aborted before any sleep happened.
    assert slept == []


def test_resilient_call_honours_server_retry_after() -> None:
    calls = {"n": 0}

    def flaky(**_: object) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientError(message="throttled", status=429, retry_after_s=4.0)
        return "ok"

    slept: list[float] = []
    resilient_call(
        flaky,
        backoff=BackoffPolicy(base_s=99.0, jitter=False),  # would be huge without the hint
        sleep=slept.append,
    )
    assert slept == [4.0]  # honoured the server hint, not the 99s schedule
