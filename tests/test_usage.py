"""Usage-tracker tests — headroom, windowing, 429 throttling, header ingest."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_swarm.usage import (
    DEFAULT_REASSESS_AFTER_S,
    Lane,
    LaneState,
    UsageTracker,
)


def _tracker(tmp_path: Path, **kw: object) -> UsageTracker:
    return UsageTracker(path=tmp_path / "usage.json", **kw)  # type: ignore[arg-type]


# ----- Lane -----------------------------------------------------------


def test_lane_subscription_classification() -> None:
    assert Lane.CLAUDE_CODE_MAX.is_subscription
    assert Lane.CURSOR.is_subscription
    assert Lane.CODEX.is_subscription
    assert not Lane.API.is_subscription


# ----- recording + windowing -----------------------------------------


def test_record_usage_accrues_within_window(tmp_path: Path) -> None:
    tr = _tracker(tmp_path, plan_caps={Lane.CLAUDE_CODE_MAX: 1000}, window_s=100)
    tr.record_usage(Lane.CLAUDE_CODE_MAX, tokens=300, now=1000.0)
    tr.record_usage(Lane.CLAUDE_CODE_MAX, tokens=200, now=1050.0)
    lv = tr.lane_view(Lane.CLAUDE_CODE_MAX, now=1051.0)
    assert lv.tokens_used == 500
    assert lv.requests_made == 2
    assert lv.headroom_tokens == 500
    assert lv.headroom_fraction == pytest.approx(0.5)


def test_events_outside_window_drop_out(tmp_path: Path) -> None:
    tr = _tracker(tmp_path, plan_caps={Lane.CURSOR: 1000}, window_s=100)
    tr.record_usage(Lane.CURSOR, tokens=400, now=1000.0)
    # 200s later the first event is well outside the 100s window.
    tr.record_usage(Lane.CURSOR, tokens=100, now=1200.0)
    lv = tr.lane_view(Lane.CURSOR, now=1200.0)
    assert lv.tokens_used == 100  # only the in-window event counts
    assert lv.headroom_tokens == 900


def test_prune_keeps_file_bounded(tmp_path: Path) -> None:
    import json

    tr = _tracker(tmp_path, window_s=10)
    for i in range(50):
        tr.record_usage(Lane.API, tokens=1, now=1000.0 + i)
    # Only events within the last 10s should remain persisted (file bounded).
    persisted = json.loads((tmp_path / "usage.json").read_text())
    api_row = next(row for row in persisted["lanes"] if row["lane"] == "api")
    st = LaneState.from_dict(api_row)
    assert len(st.events) <= 11


def test_record_usage_rejects_negative(tmp_path: Path) -> None:
    tr = _tracker(tmp_path)
    with pytest.raises(ValueError):
        tr.record_usage(Lane.API, tokens=-1)
    with pytest.raises(ValueError):
        tr.record_usage(Lane.API, tokens=1, requests=-1)


# ----- headroom semantics --------------------------------------------


def test_unknown_cap_reports_none_headroom(tmp_path: Path) -> None:
    tr = _tracker(tmp_path)  # no caps declared
    lv = tr.lane_view(Lane.CODEX, now=1000.0)
    assert lv.token_cap == 0
    assert lv.headroom_tokens is None
    assert lv.headroom_fraction is None
    assert not lv.throttled


def test_headroom_never_negative(tmp_path: Path) -> None:
    tr = _tracker(tmp_path, plan_caps={Lane.CLAUDE_CODE_MAX: 100}, window_s=1000)
    tr.record_usage(Lane.CLAUDE_CODE_MAX, tokens=250, now=1000.0)  # overshoot
    lv = tr.lane_view(Lane.CLAUDE_CODE_MAX, now=1000.0)
    assert lv.headroom_tokens == 0
    assert lv.headroom_fraction == 0.0


# ----- 429 throttling -------------------------------------------------


def test_rate_limit_collapses_headroom_until_reset(tmp_path: Path) -> None:
    tr = _tracker(tmp_path, plan_caps={Lane.CURSOR: 1000}, window_s=1000)
    tr.record_rate_limit(Lane.CURSOR, retry_after_s=60.0, now=2000.0)
    # Inside the cooldown: throttled, zero headroom.
    lv = tr.lane_view(Lane.CURSOR, now=2030.0)
    assert lv.throttled
    assert lv.headroom_tokens == 0
    assert lv.headroom_fraction == 0.0
    assert lv.seconds_to_reset == pytest.approx(30.0)
    # After the cooldown elapses: recovered, headroom restored.
    lv2 = tr.lane_view(Lane.CURSOR, now=2061.0)
    assert not lv2.throttled
    assert lv2.headroom_tokens == 1000


def test_rate_limit_without_retry_after_uses_default(tmp_path: Path) -> None:
    tr = _tracker(tmp_path)
    tr.record_rate_limit(Lane.CLAUDE_CODE_MAX, now=5000.0)
    lv = tr.lane_view(Lane.CLAUDE_CODE_MAX, now=5000.0)
    assert lv.throttled
    assert lv.reset_at == pytest.approx(5000.0 + DEFAULT_REASSESS_AFTER_S)


def test_clear_throttle(tmp_path: Path) -> None:
    tr = _tracker(tmp_path, plan_caps={Lane.CURSOR: 500})
    tr.record_rate_limit(Lane.CURSOR, retry_after_s=999.0, now=1000.0)
    assert tr.lane_view(Lane.CURSOR, now=1000.0).throttled
    tr.clear_throttle(Lane.CURSOR)
    assert not tr.lane_view(Lane.CURSOR, now=1000.0).throttled


# ----- Anthropic header ingest (API lane) ----------------------------


def test_apply_headers_sets_cap_and_remaining(tmp_path: Path) -> None:
    tr = _tracker(tmp_path)
    tr.apply_anthropic_headers(
        Lane.API,
        {
            "anthropic-ratelimit-tokens-limit": "100000",
            "anthropic-ratelimit-tokens-remaining": "25000",
        },
        now=1000.0,
    )
    lv = tr.lane_view(Lane.API, now=1000.0)
    assert lv.token_cap == 100000
    assert lv.headroom_tokens == 25000  # reported wins over cap-minus-used
    assert lv.headroom_fraction == pytest.approx(0.25)


def test_apply_headers_reset_as_duration(tmp_path: Path) -> None:
    tr = _tracker(tmp_path)
    tr.apply_anthropic_headers(
        Lane.API,
        {
            "anthropic-ratelimit-tokens-remaining": "0",
            "anthropic-ratelimit-tokens-reset": "30s",
        },
        now=1000.0,
    )
    lv = tr.lane_view(Lane.API, now=1000.0)
    assert lv.throttled
    assert lv.reset_at == pytest.approx(1030.0)


def test_apply_headers_reset_as_rfc3339(tmp_path: Path) -> None:
    tr = _tracker(tmp_path)
    reset_dt = datetime(2026, 5, 29, 21, 0, 0, tzinfo=UTC)
    now = reset_dt.timestamp() - 45.0
    tr.apply_anthropic_headers(
        Lane.API,
        {
            "anthropic-ratelimit-tokens-remaining": "0",
            "anthropic-ratelimit-tokens-reset": "2026-05-29T21:00:00Z",
        },
        now=now,
    )
    lv = tr.lane_view(Lane.API, now=now)
    assert lv.throttled
    assert lv.seconds_to_reset == pytest.approx(45.0)


def test_header_remaining_zero_recovers_after_reset(tmp_path: Path) -> None:
    # Regression: a header-driven throttle (remaining=0 + reset) must not pin
    # headroom to zero once the reset window elapses — it falls back to the
    # cap-based estimate so the recovered lane is routable again.
    tr = _tracker(tmp_path)
    tr.apply_anthropic_headers(
        Lane.API,
        {
            "anthropic-ratelimit-tokens-limit": "100000",
            "anthropic-ratelimit-tokens-remaining": "0",
            "anthropic-ratelimit-tokens-reset": "30s",
        },
        now=1000.0,
    )
    # During the cooldown: throttled, zero headroom.
    during = tr.lane_view(Lane.API, now=1010.0)
    assert during.throttled
    assert during.headroom_tokens == 0
    # After the reset: not throttled, and headroom recovers to the cap
    # (no usage was recorded against the API lane).
    after = tr.lane_view(Lane.API, now=1031.0)
    assert not after.throttled
    assert after.headroom_tokens == 100000


def test_apply_headers_retry_after_throttles(tmp_path: Path) -> None:
    tr = _tracker(tmp_path)
    tr.apply_anthropic_headers(Lane.API, {"Retry-After": "120"}, now=1000.0)
    lv = tr.lane_view(Lane.API, now=1000.0)
    assert lv.throttled
    assert lv.reset_at == pytest.approx(1120.0)


def test_apply_headers_tolerates_garbage(tmp_path: Path) -> None:
    tr = _tracker(tmp_path)
    # Malformed values must not raise; lane stays in its prior (clean) state.
    tr.apply_anthropic_headers(
        Lane.API,
        {
            "anthropic-ratelimit-tokens-limit": "not-a-number",
            "anthropic-ratelimit-tokens-reset": "garbage",
        },
        now=1000.0,
    )
    lv = tr.lane_view(Lane.API, now=1000.0)
    assert lv.token_cap == 0
    assert not lv.throttled


# ----- persistence ----------------------------------------------------


def test_state_round_trips_across_instances(tmp_path: Path) -> None:
    p = tmp_path / "usage.json"
    tr = UsageTracker(path=p, plan_caps={Lane.CURSOR: 1000}, window_s=1000)
    tr.record_usage(Lane.CURSOR, tokens=400, now=1000.0)
    tr.record_rate_limit(Lane.API, retry_after_s=50.0, now=1000.0)
    # A fresh instance reads back the same numbers.
    tr2 = UsageTracker(path=p, window_s=1000)
    assert tr2.lane_view(Lane.CURSOR, now=1000.0).tokens_used == 400
    assert tr2.lane_view(Lane.CURSOR, now=1000.0).token_cap == 1000
    assert tr2.lane_view(Lane.API, now=1000.0).throttled


def test_corrupt_state_file_starts_fresh(tmp_path: Path) -> None:
    p = tmp_path / "usage.json"
    p.write_text("{not json", encoding="utf-8")
    tr = UsageTracker(path=p)
    # No crash; every lane present and clean.
    snap = tr.snapshot(now=1000.0)
    assert {lv.lane for lv in snap.lanes} == set(Lane)


# ----- snapshot helpers ----------------------------------------------


def test_best_subscription_lane_prefers_more_headroom(tmp_path: Path) -> None:
    tr = _tracker(
        tmp_path,
        plan_caps={Lane.CLAUDE_CODE_MAX: 1000, Lane.CURSOR: 1000},
        window_s=1000,
    )
    tr.record_usage(Lane.CLAUDE_CODE_MAX, tokens=900, now=1000.0)  # 10% left
    tr.record_usage(Lane.CURSOR, tokens=100, now=1000.0)           # 90% left
    snap = tr.snapshot(now=1000.0)
    best = snap.best_subscription_lane()
    assert best is not None
    assert best.lane is Lane.CURSOR


def test_best_subscription_lane_skips_throttled(tmp_path: Path) -> None:
    tr = _tracker(tmp_path, plan_caps={Lane.CURSOR: 1000}, window_s=1000)
    tr.record_rate_limit(Lane.CLAUDE_CODE_MAX, retry_after_s=999.0, now=1000.0)
    tr.record_rate_limit(Lane.CODEX, retry_after_s=999.0, now=1000.0)
    snap = tr.snapshot(now=1000.0)
    best = snap.best_subscription_lane()
    assert best is not None
    assert best.lane is Lane.CURSOR  # the only un-throttled sub lane with a cap


def test_best_subscription_lane_none_when_all_throttled(tmp_path: Path) -> None:
    tr = _tracker(tmp_path)
    for lane in (Lane.CLAUDE_CODE_MAX, Lane.CURSOR, Lane.CODEX):
        tr.record_rate_limit(lane, retry_after_s=999.0, now=1000.0)
    snap = tr.snapshot(now=1000.0)
    assert snap.best_subscription_lane() is None


def test_any_subscription_headroom_counts_unknown_cap(tmp_path: Path) -> None:
    # No caps declared anywhere => unknown headroom counts as "has headroom".
    tr = _tracker(tmp_path)
    snap = tr.snapshot(now=1000.0)
    assert snap.any_subscription_headroom(min_fraction=0.05)


def test_any_subscription_headroom_false_when_exhausted(tmp_path: Path) -> None:
    tr = _tracker(
        tmp_path,
        plan_caps={Lane.CLAUDE_CODE_MAX: 1000, Lane.CURSOR: 1000, Lane.CODEX: 1000},
        window_s=1000,
    )
    for lane in (Lane.CLAUDE_CODE_MAX, Lane.CURSOR, Lane.CODEX):
        tr.record_usage(lane, tokens=990, now=1000.0)  # 1% left, below 5%
    snap = tr.snapshot(now=1000.0)
    assert not snap.any_subscription_headroom(min_fraction=0.05)


def test_best_lane_prefers_unknown_over_known_empty(tmp_path: Path) -> None:
    # Regression: an undeclared (unknown-headroom) lane must outrank a lane we
    # know is fully exhausted — routing to a provably-empty lane wastes a turn.
    tr = _tracker(tmp_path, plan_caps={Lane.CURSOR: 1000}, window_s=1000)
    tr.record_usage(Lane.CURSOR, tokens=1000, now=1000.0)  # CURSOR: known 0% headroom
    # CODEX has no cap -> unknown headroom; CLAUDE_CODE_MAX also unknown.
    snap = tr.snapshot(now=1000.0)
    best = snap.best_subscription_lane()
    assert best is not None
    assert best.lane is not Lane.CURSOR  # never the provably-empty lane


def test_snapshot_lane_lookup(tmp_path: Path) -> None:
    tr = _tracker(tmp_path)
    snap = tr.snapshot(now=1000.0)
    assert snap.lane(Lane.API) is not None
    assert snap.lane(Lane.API).lane is Lane.API  # type: ignore[union-attr]
