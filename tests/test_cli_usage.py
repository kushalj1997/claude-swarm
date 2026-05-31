"""CLI tests for the ``usage`` subcommand group."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from claude_swarm.cli.main import main


def _home(tmp_path: Path, monkeypatch) -> CliRunner:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CLAUDE_SWARM_HOME", str(tmp_path / "swarm"))
    runner = CliRunner()
    runner.invoke(main, ["init"])
    return runner


def test_usage_show_lists_all_lanes(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = _home(tmp_path, monkeypatch)
    r = runner.invoke(main, ["usage", "show"])
    assert r.exit_code == 0, r.output
    snap = json.loads(r.output)
    lanes = {lv["lane"] for lv in snap["lanes"]}
    assert lanes == {"claude-code-max", "cursor", "codex", "api"}


def test_usage_set_cap_then_record_then_decide(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = _home(tmp_path, monkeypatch)
    # Declare a cap so headroom is computable.
    r = runner.invoke(main, ["usage", "set-cap", "--lane", "cursor", "--tokens", "1000"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["token_cap"] == 1000
    # Record usage against it.
    r = runner.invoke(main, ["usage", "record", "--lane", "cursor", "--tokens", "200"])
    assert r.exit_code == 0, r.output
    lv = json.loads(r.output)
    assert lv["tokens_used"] == 200
    assert lv["headroom_tokens"] == 800
    # Governor decides — plenty of headroom, so CHEAP_PLANS.
    r = runner.invoke(main, ["usage", "decide"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["mode"] == "cheap_plans"
    assert d["lane"] == "cursor"


def test_usage_limit_throttles_lane(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = _home(tmp_path, monkeypatch)
    r = runner.invoke(main, ["usage", "limit", "--lane", "claude-code-max", "--retry-after-s", "60"])
    assert r.exit_code == 0, r.output
    lv = json.loads(r.output)
    assert lv["throttled"] is True
    assert lv["seconds_to_reset"] is not None


def test_usage_decide_throttled_on_budget(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = _home(tmp_path, monkeypatch)
    # Throttle every subscription lane so "both exhausted" holds and the
    # throttled governor has no free lane to fall back to -> routes to api.
    for lane in ("claude-code-max", "cursor", "codex"):
        runner.invoke(main, ["usage", "limit", "--lane", lane, "--retry-after-s", "3600"])
    r = runner.invoke(
        main,
        ["usage", "decide", "--budget-usd", "1.0", "--add-api-spend", "2.0"],
    )
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["mode"] == "throttled"
    assert d["lane"] == "api"


def test_usage_decide_min_dwell_override_allows_immediate_switch(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    runner = _home(tmp_path, monkeypatch)
    # Exhaust every subscription lane.
    for lane in ("claude-code-max", "cursor", "codex"):
        runner.invoke(main, ["usage", "set-cap", "--lane", lane, "--tokens", "1000"])
        runner.invoke(main, ["usage", "record", "--lane", lane, "--tokens", "990"])
    # With dwell=0 the very first decision is free to flip to the API lane.
    r = runner.invoke(main, ["usage", "decide", "--min-dwell-s", "0"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["mode"] == "api_swarm"
    assert d["lane"] == "api"


def test_usage_decide_throttled_prefers_free_subscription_lane(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    runner = _home(tmp_path, monkeypatch)
    # Over budget, but cursor still has headroom: route there (free) not api.
    runner.invoke(main, ["usage", "set-cap", "--lane", "cursor", "--tokens", "1000"])
    r = runner.invoke(
        main,
        ["usage", "decide", "--budget-usd", "1.0", "--add-api-spend", "2.0"],
    )
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["mode"] == "throttled"
    assert d["lane"] == "cursor"
