import copy
import json
import time

import pytest
from pydantic import ValidationError

from quantpilot.v2 import ai_client
from quantpilot.v2.action_review import Decision
from quantpilot.v2.models import Preferences
from quantpilot.v2.notifications import NotificationSettings, opportunity_message
from quantpilot.v2.position_review import position_guard


def ai(workspace, monkeypatch, account, enabled=True):
    workspace.vault.put("v2:ai:openai", {"apiKey": "test-only"})
    workspace.preferences(
        {**workspace.store.get("v2_preferences"), "ai_model": "test-model", "ai_action_enabled": False}
    )
    account.update(workspace.save_account({**account, "ai_action_enabled": enabled}))
    # Market sharing has separate integration tests; preserve account-stage cadence assertions.
    monkeypatch.setattr(
        workspace.action_review.market, "inspect", lambda op, q, p: {"state": "approved", "opportunity": op}
    )
    callbacks = []
    monkeypatch.setattr(
        workspace.action_review.pool, "submit", lambda fn, *args: callbacks.append((fn, args))
    )
    decision = Decision(
        decision="approve",
        reason="checked",
        entry_low=None,
        entry_high=None,
        stop=None,
        target_1=None,
        target_2=None,
        exit_ratio=None,
    ).model_dump()
    monkeypatch.setattr(ai_client, "response", lambda *a, **kw: response(decision))
    return callbacks


def response(value):
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}],
    }


def enter(w, account, op, quote):
    return w.paper.enter(
        account, op, {"btc": quote}, {i["id"]: i for i in w.store.get("v2_watchlist")}, "balanced"
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("evaluation_interval", 9),
        ("evaluation_interval", 3601),
        ("evaluation_interval", 30.5),
        ("ai_reaudit_threshold_pct", 0),
        ("ai_reaudit_threshold_pct", 10.1),
    ],
)
def test_review_settings_bounds(key, value):
    with pytest.raises(ValidationError):
        Preferences(**{key: value})


def test_evaluation_uses_shared_interval(workspace, monkeypatch):
    workspace.preferences({**workspace.store.get("v2_preferences"), "evaluation_interval": 90})
    observed = []
    monkeypatch.setattr(workspace, "scan", lambda item, analyze, prefs: observed.append(analyze))
    workspace.last_evaluation = time.time() - 35
    workspace.tick()
    assert observed and not any(observed)
    observed.clear()
    workspace.last_evaluation = time.time() - 95
    workspace.tick()
    assert observed and all(observed)


def test_disabled_pair_does_not_request_ai(workspace, account, opportunity, quote, monkeypatch):
    callbacks = ai(workspace, monkeypatch, account)
    with pytest.raises(ValueError, match="未在此账户启用"):
        workspace.action_review.inspect(
            account["id"], "eth", "open", quote, {**opportunity, "instrument_id": "eth"}
        )
    assert not callbacks
    assert workspace.action_review.inspect(account["id"], "eth", "stop_loss", quote)["state"] == "approved"


def test_pair_filter_before_broker(workspace, account, opportunity, quote, monkeypatch):
    ai(workspace, monkeypatch, account)
    workspace.opportunities = {"eth": {**opportunity, "instrument_id": "eth", "min_score": 80}}
    workspace.quotes = {"eth": quote}
    monkeypatch.setattr(workspace.paper, "enter", lambda *args: pytest.fail("disabled pair reached broker"))
    workspace.execute()


def test_shared_cadence_prevents_plan_change_spam(workspace, account, opportunity, quote, monkeypatch):
    jobs = ai(workspace, monkeypatch, account)
    g = workspace.action_review
    assert g.inspect(account["id"], "btc", "open", quote, opportunity)["state"] == "pending"
    changed = copy.deepcopy(opportunity)
    changed["plan"]["entry"] = 100.01
    for _ in range(5):
        assert g.inspect(account["id"], "btc", "open", quote, changed)["state"] == "pending"
    assert len(jobs) == 1
    # Stops retain protection even when the next scheduled review is not due.
    assert g.inspect(account["id"], "btc", "stop_loss", quote)["state"] == "fallback"
    g.last_requests[(account["id"], "btc")] -= 31
    g.inspect(account["id"], "btc", "open", quote, changed)
    assert len(jobs) == 2


def test_custom_threshold_and_result_lifetime(workspace, account, opportunity, quote, monkeypatch):
    jobs = ai(workspace, monkeypatch, account)
    workspace.preferences(
        {**workspace.store.get("v2_preferences"), "evaluation_interval": 90, "ai_reaudit_threshold_pct": 1}
    )
    g = workspace.action_review
    g.inspect(account["id"], "btc", "open", quote, opportunity)
    fn, args = jobs.pop()
    fn(*args)
    job = next(iter(g.jobs.values()))
    job["completed_at"] -= 40  # Custom 90 seconds, not the previous hardcoded 30.
    assert (
        g.inspect(account["id"], "btc", "open", {**quote, "mid": 100.5}, opportunity)["state"] == "approved"
    )
    assert g.inspect(account["id"], "btc", "open", {**quote, "mid": 101.1}, opportunity)["state"] == "pending"
    assert not jobs


def test_executed_signal_does_not_reaudit(workspace, account, opportunity, quote, monkeypatch):
    enter(workspace, account, opportunity, quote)
    jobs = ai(workspace, monkeypatch, account)
    assert enter(workspace, account, opportunity, quote)["status"] == "duplicate"
    assert not jobs


@pytest.mark.parametrize("tone", ["chime", "bell", "pulse", "alert"])
def test_sound_settings_persist(logged, tone):
    body = NotificationSettings(sound_enabled=True, sound_tone=tone, sound_volume=95).model_dump()
    r = logged.put("/api/v2/notifications", json=body)
    assert r.status_code == 200
    assert logged.get("/api/v2/notifications").json()["settings"] == body


@pytest.mark.parametrize(
    "change", [{"sound_volume": 101}, {"sound_volume": -1}, {"sound_volume": 10.5}, {"sound_tone": "unknown"}]
)
def test_sound_settings_reject_invalid(change):
    with pytest.raises(ValidationError):
        NotificationSettings(**change)


def test_telegram_opportunity_has_actual_levels(opportunity):
    message = opportunity_message(opportunity)
    for value in ("99", "101", "96", "106", "112", "103", "98", "50%", "非成交"):
        assert value in message
    assert "开仓区间" in message and "减仓观察" in message and "止盈 2" in message


def prepare_position(workspace, account, opportunity, quote, monkeypatch):
    enter(workspace, account, opportunity, quote)
    account = workspace.save_account(
        {**account, "position_review_enabled": True, "position_review_interval": 60}
    )
    ai(workspace, monkeypatch, account, enabled=False)
    workspace.quotes = {"btc": quote}
    workspace.opportunities = {"btc": opportunity}
    proposal = {
        "instrument_id": "btc",
        "stop": 97,
        "target_1": 105,
        "target_2": 110,
        "add_at": 103,
        "reduce_at": 98,
        "reduce_ratio": 0.25,
        "reason": "reviewed",
    }
    monkeypatch.setattr(ai_client, "response", lambda *a, **kw: response({"plans": [proposal]}))
    return account, proposal


def review_now(workspace, account):
    r = workspace.position_reviews
    r.run(
        account,
        r.held(account),
        workspace.store.get("v2_preferences"),
        time.time(),
        workspace.logic.current()["id"],
    )
    return r


def test_position_review_applies_protection_and_reduction(
    workspace, account, opportunity, quote, monkeypatch
):
    account, _ = prepare_position(workspace, account, opportunity, quote, monkeypatch)
    r = review_now(workspace, account)
    assert r.state(account)["status"] == "ready"
    held = r.held(account)["btc"]
    assert r.points(account, "btc", held)["add_at"] == 103
    workspace.paper.protect(
        account, workspace.quotes, r.opportunities(account, workspace.opportunities, workspace.quotes)
    )
    held = r.held(account)["btc"]
    assert float(held["stop"]) == 97 and held["targets"][0]["price"] == 105
    q = {**quote, "mid": 97.9, "bid": 97.89, "ask": 97.91}
    workspace.paper.protect(
        account, {"btc": q}, r.opportunities(account, workspace.opportunities, {"btc": q})
    )
    assert float(r.held(account)["btc"]["qty"]) == pytest.approx(float(held["qty"]) * 0.75)
    assert r.points(account, "btc", r.held(account)["btc"])["add_at"] is None


@pytest.mark.parametrize("change", [{"stop": 95}, {"target_1": 120}, {"reduce_at": 90}, {"add_at": 99}])
def test_position_review_rejects_unsafe_proposals(
    workspace, account, opportunity, quote, monkeypatch, change
):
    account, proposal = prepare_position(workspace, account, opportunity, quote, monkeypatch)
    proposal.update(change)
    before = copy.deepcopy(workspace.store.get("v2_ledgers"))
    r = review_now(workspace, account)
    assert r.state(account)["status"] == "error"
    assert workspace.store.get("v2_ledgers") == before
    assert r.points(account, "btc", r.held(account)["btc"])["add_at"] is None


def test_position_review_discards_changed_account(workspace, account, opportunity, quote, monkeypatch):
    account, proposal = prepare_position(workspace, account, opportunity, quote, monkeypatch)

    def changing(*args, **kwargs):
        workspace.save_account({**account, "position_review_enabled": False})
        return response({"plans": [proposal]})

    monkeypatch.setattr(ai_client, "response", changing)
    r = review_now(workspace, account)
    assert r.state(account)["status"] == "error"
    assert float(r.held(account)["btc"]["stop"]) == 96


def test_periodic_switch_and_interval_limit_requests(workspace, account, opportunity, quote, monkeypatch):
    account, _ = prepare_position(workspace, account, opportunity, quote, monkeypatch)
    r, tasks = workspace.position_reviews, []
    monkeypatch.setattr(r.pool, "submit", lambda fn, *args: tasks.append((fn, args)))
    r.scheduled()
    r.scheduled()
    assert len(tasks) == 1
    fn, args = tasks.pop()
    fn(*args)
    r.scheduled()
    assert not tasks
    workspace.save_account({**account, "position_review_enabled": False})
    r.scheduled()
    assert not tasks


def test_periodic_failure_preserves_stop(workspace, account, opportunity, quote, monkeypatch):
    account, _ = prepare_position(workspace, account, opportunity, quote, monkeypatch)
    monkeypatch.setattr(ai_client, "response", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    r = review_now(workspace, account)
    assert r.state(account)["status"] == "error"
    q = {**quote, "mid": 95, "bid": 94.99, "ask": 95.01}
    workspace.paper.protect(account, {"btc": q}, {})
    assert not r.held(account)


def test_points_null_when_unplanned_and_take_profit_stage(workspace, account, opportunity, quote):
    enter(workspace, account, opportunity, quote)
    p = workspace.position_reviews.held(account)["btc"]
    p["tp_done"] = [0]
    points = workspace.position_reviews.points(account, "btc", p)
    assert points["targets"][0]["stage"] == 2
    assert points["reduce_at"] is None
    stopped = {**account, "auto": False}
    assert workspace.position_reviews.points(stopped, "btc", p)["add_at"] is None


def test_account_review_controls_api(logged, account):
    r = logged.post(
        "/api/v2/accounts", json={**account, "position_review_enabled": True, "position_review_interval": 180}
    )
    assert r.status_code == 200
    a = next(a for a in logged.get("/api/v2/state").json()["accounts"] if a["id"] == account["id"])
    assert a["position_review_enabled"] and a["position_review_interval"] == 180


def test_expired_position_plan_stops_new_actions(workspace, account, opportunity, quote, monkeypatch):
    account, _ = prepare_position(workspace, account, opportunity, quote, monkeypatch)
    r = review_now(workspace, account)
    with workspace.store.transaction() as db:
        records = workspace.store.read(db, "v2_position_reviews")
        records[account["id"]]["plans"]["btc"]["expires_at"] = time.time() - 1
        workspace.store.write(db, "v2_position_reviews", records)
    points = r.points(account, "btc", r.held(account)["btc"])
    assert points["add_at"] is None and points["reduce_at"] is None
    assert float(points["stop"]) == 96 and len(points["targets"]) == 2


def test_external_partial_fill_invalidates_position_plan(workspace, account, opportunity, quote):
    enter(workspace, account, opportunity, quote)
    r = workspace.position_reviews
    managed = r.held(account)["btc"]
    managed["symbol"] = "BTC/USDT:USDT"
    external_account = {**account, "kind": "demo", "position_review_enabled": True}
    actual = {"symbol": managed["symbol"], "contracts": 100, "contractSize": 0.001, "side": "long"}
    with workspace.store.transaction() as db:
        workspace.store.write(db, "v2_managed", {account["id"]: {"btc": managed}})
        workspace.store.write(db, "v2_external", {account["id"]: {"positions": [actual]}})
    held = r.held(external_account)["btc"]
    assert held["external_qty"] == pytest.approx(0.1)
    with workspace.store.transaction() as db:
        workspace.store.write(
            db,
            "v2_position_reviews",
            {
                account["id"]: {
                    "account": external_account,
                    "preferences": workspace.store.read(db, "v2_preferences"),
                    "version": workspace.logic.current()["id"],
                    "plans": {"btc": {"expires_at": time.time() + 60, "guard": position_guard(held)}},
                }
            },
        )
    assert r.plan(external_account, "btc", held)
    with workspace.store.transaction() as db:
        workspace.store.write(
            db, "v2_external", {account["id"]: {"positions": [{**actual, "contracts": 50}]}}
        )
    assert r.plan(external_account, "btc", r.held(external_account)["btc"]) is None


@pytest.mark.parametrize("change", [{"mid": 102}, {"timestamp": 1}])
def test_quote_change_during_position_review_discards_result(
    workspace, account, opportunity, quote, monkeypatch, change
):
    account, proposal = prepare_position(workspace, account, opportunity, quote, monkeypatch)

    def changing(*args, **kwargs):
        workspace.quotes["btc"] = {**quote, **change}
        return response({"plans": [proposal]})

    monkeypatch.setattr(ai_client, "response", changing)
    r = review_now(workspace, account)
    assert r.state(account)["status"] == "error"
    assert float(r.held(account)["btc"]["stop"]) == 96
