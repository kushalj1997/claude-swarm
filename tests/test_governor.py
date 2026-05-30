"""Governor tests — auto-switch state machine, hysteresis, dwell, budget cap."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_swarm.governor import Governor, GovernorConfig, Mode
from claude_swarm.usage import Lane, UsageTracker

#: Cap every subscription lane so an undeclared lane (which legitimately
#: counts as "has headroom" until a 429) doesn't keep the governor in CHEAP.
ALL_SUB_CAPS: dict[Lane, int] = {
    Lane.CLAUDE_CODE_MAX: 1000,
    Lane.CURSOR: 1000,
    Lane.CODEX: 1000,
}


def _make(
    tmp_path: Path,
    *,
    caps: dict[Lane, int] | None = None,
    window_s: int = 1000,
    config: GovernorConfig | None = None,
) -> tuple[UsageTracker, Governor]:
    tr = UsageTracker(path=tmp_path / "usage.json", plan_caps=caps, window_s=window_s)
    gov = Governor(
        tracker=tr,
        config=config or GovernorConfig(min_dwell_s=0.0),
        path=tmp_path / "governor.json",
    )
    return tr, gov


def _exhaust_all_subs(tr: UsageTracker, *, now: float, tokens: int = 999) -> None:
    """Consume ``tokens`` on every capped subscription lane (drives toward API)."""
    for lane in (Lane.CLAUDE_CODE_MAX, Lane.CURSOR, Lane.CODEX):
        tr.record_usage(lane, tokens=tokens, now=now)


# ----- config validation ---------------------------------------------


def test_config_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValueError):
        GovernorConfig(enter_cheap_fraction=0.05, exit_cheap_fraction=0.15)
    with pytest.raises(ValueError):
        GovernorConfig(enter_cheap_fraction=0.1, exit_cheap_fraction=0.1)  # not strict


def test_config_rejects_negative_budget_and_dwell() -> None:
    with pytest.raises(ValueError):
        GovernorConfig(api_daily_budget_usd=-1.0)
    with pytest.raises(ValueError):
        GovernorConfig(min_dwell_s=-1.0)


# ----- default state + happy path ------------------------------------


def test_starts_in_cheap_plans(tmp_path: Path) -> None:
    _, gov = _make(tmp_path, caps={Lane.CURSOR: 1000})
    d = gov.decide(now=1000.0)
    assert d.mode is Mode.CHEAP_PLANS
    assert d.lane.is_subscription


def test_cheap_mode_routes_to_best_subscription_lane(tmp_path: Path) -> None:
    tr, gov = _make(tmp_path, caps={Lane.CLAUDE_CODE_MAX: 1000, Lane.CURSOR: 1000})
    tr.record_usage(Lane.CLAUDE_CODE_MAX, tokens=950, now=1000.0)  # 5% left
    tr.record_usage(Lane.CURSOR, tokens=100, now=1000.0)           # 90% left
    d = gov.decide(now=1000.0)
    assert d.mode is Mode.CHEAP_PLANS
    assert d.lane is Lane.CURSOR


# ----- cheap -> api on exhaustion ------------------------------------


def test_switches_to_api_when_subscriptions_exhausted(tmp_path: Path) -> None:
    tr, gov = _make(
        tmp_path,
        caps={Lane.CLAUDE_CODE_MAX: 1000, Lane.CURSOR: 1000, Lane.CODEX: 1000},
    )
    for lane in (Lane.CLAUDE_CODE_MAX, Lane.CURSOR, Lane.CODEX):
        tr.record_usage(lane, tokens=990, now=1000.0)  # 1% left, below exit 5%
    d = gov.decide(now=1000.0)
    assert d.mode is Mode.API_SWARM
    assert d.lane is Lane.API
    assert d.changed


def test_switches_to_api_when_all_subscriptions_throttled(tmp_path: Path) -> None:
    tr, gov = _make(tmp_path, caps={Lane.CURSOR: 1000})
    for lane in (Lane.CLAUDE_CODE_MAX, Lane.CURSOR, Lane.CODEX):
        tr.record_rate_limit(lane, retry_after_s=300.0, now=1000.0)
    d = gov.decide(now=1000.0)
    assert d.mode is Mode.API_SWARM


# ----- api -> cheap recovery (with hysteresis) -----------------------


def test_recovers_to_cheap_when_lane_frees(tmp_path: Path) -> None:
    tr, gov = _make(tmp_path, caps={Lane.CURSOR: 1000})
    # Drive into API mode: throttle every sub lane.
    for lane in (Lane.CLAUDE_CODE_MAX, Lane.CURSOR, Lane.CODEX):
        tr.record_rate_limit(lane, retry_after_s=10.0, now=1000.0)
    assert gov.decide(now=1000.0).mode is Mode.API_SWARM
    # After the cooldown, CURSOR has a full 1000-token cap (> enter 15%).
    d = gov.decide(now=1011.0)
    assert d.mode is Mode.CHEAP_PLANS
    assert d.lane is Lane.CURSOR


def test_hysteresis_band_prevents_flap(tmp_path: Path) -> None:
    # The band is exit=5% .. enter=15%. A lane sitting at 10% headroom is
    # inside the band: it must neither leave CHEAP (10% > 5% exit) nor, once
    # in API, bounce back to CHEAP (10% < 15% enter). That's the anti-flap.
    cfg = GovernorConfig(enter_cheap_fraction=0.15, exit_cheap_fraction=0.05, min_dwell_s=0.0)

    # (a) Starting in CHEAP with every sub lane at 10% headroom -> stays CHEAP.
    tr_a = UsageTracker(path=tmp_path / "a.json", plan_caps=ALL_SUB_CAPS, window_s=1000)
    _exhaust_all_subs(tr_a, now=1000.0, tokens=900)  # 10% left on each
    gov_a = Governor(tracker=tr_a, config=cfg, path=tmp_path / "gov_a.json")
    assert gov_a.decide(now=1000.0).mode is Mode.CHEAP_PLANS

    # (b) Now genuinely exhaust every lane to fall into API mode...
    _exhaust_all_subs(tr_a, now=1000.0, tokens=90)  # ~1% left on each, below exit
    assert gov_a.decide(now=1000.0).mode is Mode.API_SWARM
    # ...and recovering only back into the band (10%) keeps us in API
    # (10% < 15% enter) — no flap.
    tr_b = UsageTracker(path=tmp_path / "b.json", plan_caps=ALL_SUB_CAPS, window_s=1000)
    _exhaust_all_subs(tr_b, now=2000.0, tokens=900)  # 10% left on each
    gov_b = Governor(tracker=tr_b, config=cfg, path=tmp_path / "gov_a.json")  # same gov state file
    assert gov_b.mode is Mode.API_SWARM  # loaded the API mode from disk
    assert gov_b.decide(now=2000.0).mode is Mode.API_SWARM


# ----- minimum dwell --------------------------------------------------


def test_min_dwell_holds_mode(tmp_path: Path) -> None:
    cfg = GovernorConfig(min_dwell_s=100.0)
    tr, gov = _make(tmp_path, caps=ALL_SUB_CAPS, config=cfg)
    # First decide stamps mode_since at t=1000.
    gov.decide(now=1000.0)
    # Now exhaust every sub lane, but only 50s later — within the 100s dwell.
    _exhaust_all_subs(tr, now=1050.0)
    d = gov.decide(now=1050.0)
    assert d.mode is Mode.CHEAP_PLANS  # held despite exhaustion
    assert not d.changed
    # Past the dwell window the transition is allowed.
    d2 = gov.decide(now=1101.0)
    assert d2.mode is Mode.API_SWARM
    assert d2.changed


# ----- budget cap -> throttled (bypasses dwell) ----------------------


def test_budget_cap_forces_throttled_bypassing_dwell(tmp_path: Path) -> None:
    cfg = GovernorConfig(api_daily_budget_usd=5.0, min_dwell_s=10_000.0)
    _, gov = _make(tmp_path, caps={Lane.CURSOR: 1000}, config=cfg)
    gov.decide(now=1000.0)  # CHEAP, fresh mode_since
    gov.record_api_spend(6.0, now=1000.0)  # over the $5 cap
    d = gov.decide(now=1000.5)  # within dwell, but budget bypasses it
    assert d.mode is Mode.THROTTLED  # the safety stop fired despite dwell
    assert "budget" in d.reason


def test_throttled_recovers_to_cheap_after_budget_rolls_over(tmp_path: Path) -> None:
    cfg = GovernorConfig(api_daily_budget_usd=5.0, min_dwell_s=0.0)
    _, gov = _make(tmp_path, caps={Lane.CURSOR: 1000}, config=cfg)
    day1 = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC).timestamp()
    gov.record_api_spend(6.0, now=day1)
    assert gov.decide(now=day1).mode is Mode.THROTTLED
    # Next UTC day: spend rolls to 0, subscription headroom is full -> cheap.
    day2 = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC).timestamp()
    d = gov.decide(now=day2)
    assert d.mode is Mode.CHEAP_PLANS
    assert d.api_spend_usd == 0.0


def test_throttled_prefers_free_subscription_lane_over_capped_api(tmp_path: Path) -> None:
    # Over the API budget, but a subscription lane still has headroom: the
    # THROTTLED governor must route to that free lane, not the capped API.
    cfg = GovernorConfig(api_daily_budget_usd=5.0, min_dwell_s=0.0)
    _, gov = _make(tmp_path, caps={Lane.CURSOR: 1000}, config=cfg)
    gov.record_api_spend(6.0, now=1000.0)
    d = gov.decide(now=1000.0)
    assert d.mode is Mode.THROTTLED
    assert d.lane is Lane.CURSOR  # free lane beats the budget-capped API


def test_throttled_routes_to_api_when_no_free_lane(tmp_path: Path) -> None:
    cfg = GovernorConfig(api_daily_budget_usd=5.0, min_dwell_s=0.0)
    tr, gov = _make(tmp_path, caps=ALL_SUB_CAPS, config=cfg)
    for lane in (Lane.CLAUDE_CODE_MAX, Lane.CURSOR, Lane.CODEX):
        tr.record_rate_limit(lane, retry_after_s=3600.0, now=1000.0)
    gov.record_api_spend(6.0, now=1000.0)
    d = gov.decide(now=1000.0)
    assert d.mode is Mode.THROTTLED
    assert d.lane is Lane.API  # nothing free -> the (restricted) API


def test_day_rollover_persists_on_read_tick(tmp_path: Path) -> None:
    # A decide() that crosses the UTC day boundary must durably zero the
    # spend, even with no mode change — a restarted process must not re-load
    # yesterday's spend.
    cfg = GovernorConfig(api_daily_budget_usd=100.0, min_dwell_s=0.0)
    _, gov = _make(tmp_path, caps={Lane.CURSOR: 1000}, config=cfg)
    day1 = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC).timestamp()
    gov.record_api_spend(40.0, now=day1)
    day2 = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC).timestamp()
    gov.decide(now=day2)  # read-mostly tick that crosses midnight
    # Fresh instance over the same file sees the rolled-over (zero) spend.
    tr2 = UsageTracker(path=tmp_path / "usage.json", window_s=1000)
    gov2 = Governor(tracker=tr2, config=cfg, path=tmp_path / "governor.json")
    assert gov2.decide(now=day2).api_spend_usd == 0.0


def test_record_api_spend_rejects_negative(tmp_path: Path) -> None:
    _, gov = _make(tmp_path)
    with pytest.raises(ValueError):
        gov.record_api_spend(-1.0)


# ----- persistence ----------------------------------------------------


def test_mode_persists_across_instances(tmp_path: Path) -> None:
    tr, gov = _make(tmp_path, caps=ALL_SUB_CAPS)
    _exhaust_all_subs(tr, now=1000.0)
    assert gov.decide(now=1000.0).mode is Mode.API_SWARM
    # Fresh governor over the same files resumes in API mode.
    tr2 = UsageTracker(path=tmp_path / "usage.json", window_s=1000)
    gov2 = Governor(
        tracker=tr2,
        config=GovernorConfig(min_dwell_s=0.0),
        path=tmp_path / "governor.json",
    )
    assert gov2.mode is Mode.API_SWARM
