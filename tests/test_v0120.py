import copy
import json
import time

import pytest
from test_logic import add_history, edit, mock_review
from test_v080 import enter, prepare_position, response

from quantpilot.v2 import ai_client, journal
from quantpilot.v2.logic import LogicEngine, ReviewSettings
from quantpilot.v2.models import Account, Instrument
from quantpilot.v2.rules import Parameters, effective, migrate_legacy
from quantpilot.v2.signals import evaluate, indicators


def frames():
    rows = [[n * 3600000, 100 + n, 103 + n, 99 + n, 101 + n, 100] for n in range(100)]
    return {f: rows for f in ("1h", "4h", "1d", "1w")}


def eval_plan(quote, params):
    return evaluate(
        Instrument(id="btc", label="BTC", symbol="BTC/USDT:USDT").model_dump(),
        frames(),
        quote,
        [],
        "balanced",
        time.time(),
        1,
        params,
    )


def test_disabled_stop_never_falls_back(quote):
    result = eval_plan(quote, {"atr_stop_enabled": False})
    assert result["plan"] is None and not result["valid"]
    assert any("保护止损" in b for b in result["blockers"])
    assert eval_plan(quote, {"atr_stop_enabled": False, "fixed_stop_enabled": True})["plan"][
        "stop"
    ] == pytest.approx(quote["ask"] * 0.98)


def test_disabled_volume_removes_gate_and_weight(quote):
    result = eval_plan(quote, {"volume_enabled": False, "volume_multiplier": 5})
    assert not any("成交量" in b for b in result["blockers"])
    assert not any("成交量" in b["name"] for b in result["breakdown"])


def test_ema_disabled_does_not_use_price_fallback():
    p = {"ema_enabled": False, "macd_enabled": False, "rsi_enabled": False}
    s = indicators(frames()["1h"], p)
    assert s["trend"] == "flat" and s["ema20"] is None and s["rsi"] is None


def test_optional_condition_really_removed(quote):
    strict = eval_plan(quote, {"volume_multiplier": 5})
    off = eval_plan(
        quote, {"volume_multiplier": 5, "overrides": {"volume_multiplier": False, "weight_etf": False}}
    )
    assert any("成交量" in b for b in strict["blockers"])
    assert not any("成交量" in b for b in off["blockers"])
    assert not any(b["name"] == "已披露资金流" for b in off["breakdown"])


def test_disabled_hour_does_not_require_hour_data(quote):
    f = frames()
    f.pop("1h")
    result = evaluate(
        Instrument(id="btc", label="BTC", symbol="BTC/USDT:USDT").model_dump(),
        f,
        quote,
        [],
        "balanced",
        time.time(),
        1,
        {"frame_1h_enabled": False},
    )
    assert "1h" not in result["indicators"] and not any("1h" in b for b in result["blockers"])


def test_migration_preserves_old_effect_only_once(workspace):
    legacy = {"atr_stop": 4, "overrides": {"atr_stop": False}, "atr_stop_enabled": False}
    converted = migrate_legacy(legacy)
    assert converted["atr_stop"] == 2 and converted["fixed_stop_enabled"]
    assert converted["semantics_version"] == 2
    converted["overrides"] = {"fixed_stop_pct": False}
    assert not effective(migrate_legacy(converted))["fixed_stop_enabled"]
    with workspace.store.transaction() as db:
        d = workspace.store.read(db, "v2_logic")
        d["current"]["parameters"] = legacy
        workspace.store.write(db, "v2_logic", d)
    LogicEngine(workspace)
    assert "升级前" in workspace.logic.state()["migration_note"]
    assert workspace.store.get("v2_logic_pre_v012")["current"]["parameters"] == legacy


def test_journal_entry_costs_close_and_snapshot_immutability(workspace, account, opportunity, quote):
    op = {
        **opportunity,
        "logic_version": workspace.logic.current()["id"],
        "parameters": Parameters().model_dump(),
        "demo": False,
        "indicators": {"1h": {"trend": "long"}},
    }
    workspace.signal_frames[op["id"]] = frames()
    enter(workspace, account, op, quote)
    with workspace.store.connect() as db:
        row = db.execute("SELECT * FROM v2_trade_journal").fetchone()
        tid = row["id"]
        data = json.loads(row["detail"])
    assert data["entry_quote"]["mid"] == 100 and data["bars"]["1h"]
    assert data["risk_and_sizing"]["risk"]["account_equity"]
    quote["mid"] = 101
    workspace.quotes = {"btc": quote}
    journal.sample(workspace)
    workspace.paper.close(account["id"], "btc", quote, reason="manual")
    with workspace.store.connect() as db:
        row = db.execute("SELECT * FROM v2_trade_journal WHERE id=?", (tid,)).fetchone()
        data = json.loads(row["detail"])
        assert data["entry_quote"]["mid"] == 100 and data["mark_samples"] == 1
        assert row["closed"] and data["outcome"]["fee"] and data["outcome"]["trade_net_pnl"]
    evidence = journal.review_evidence(workspace.store, 0, time.time())
    assert evidence["groups"][0]["trades"] == 1
    assert evidence["equity_curves"][0]["samples"] == 1
    assert evidence["groups"][0]["entry_regime"] == "trend_long"
    assert workspace.logic.state()["evidence_summary"]["recorded_complete_trades"] == 1
    assert "bars" not in evidence["closed_trade_evidence"][0]
    replay = journal.compare_candidate(workspace.store, {"entry_enabled": False}, 0, time.time())
    assert replay["samples"] == 1 and replay["profit_backtest"] is False
    assert replay["comparisons"][0]["after"]["valid"] is False


def setup_recheck(workspace, account, opportunity, quote, monkeypatch):
    op = {
        **opportunity,
        "logic_version": workspace.logic.current()["id"],
        "parameters": workspace.logic.current()["parameters"],
        "demo": False,
    }
    account, proposal = prepare_position(workspace, account, op, quote, monkeypatch)
    account = workspace.save_account({**account, "strategy_recheck_enabled": True})
    # Avoid asynchronous execution; tests explicitly run the queued work.
    jobs = []
    monkeypatch.setattr(workspace.position_reviews.pool, "submit", lambda f, *a: jobs.append((f, a)))
    return account, proposal, jobs


def test_update_immediately_queues_and_adopts_with_entry_attribution(
    workspace, account, opportunity, quote, monkeypatch
):
    account, proposal, jobs = setup_recheck(workspace, account, opportunity, quote, monkeypatch)
    old = workspace.logic.current()["id"]
    new = edit(workspace.logic, ema_fast=22)
    assert workspace.logic.current()["id"] == new["id"] and len(jobs) == 1
    assert workspace.position_reviews.held(account)["btc"]["logic_version"] == old
    fn, args = jobs[0]
    fn(*args)
    held = workspace.position_reviews.held(account)["btc"]
    assert held["logic_version"] == new["id"] and held["entry_logic_version"] == old
    assert float(held["stop"]) == 96  # proposed protection is not yet executed
    assert workspace.store.get("v2_strategy_rechecks")[account["id"]]["state"] == "reviewed"
    workspace.paper.protect(
        account, workspace.quotes, workspace.position_reviews.opportunities(account, {}, workspace.quotes)
    )
    assert float(workspace.position_reviews.held(account)["btc"]["stop"]) == 97
    workspace.paper.close(account["id"], "btc", quote, reason="manual")
    with workspace.store.connect() as db:
        t = db.execute("SELECT * FROM logic_trades").fetchone()
        assert t["version"] == old
        d = json.loads(db.execute("SELECT detail FROM v2_trade_journal").fetchone()[0])
        assert d["versions"] == [old, new["id"]]


def test_recheck_off_keeps_original_position_version(workspace, account, opportunity, quote, monkeypatch):
    account, _, jobs = setup_recheck(workspace, account, opportunity, quote, monkeypatch)
    account = workspace.save_account({**account, "strategy_recheck_enabled": False})
    old = workspace.logic.current()["id"]
    edit(workspace.logic, ema_fast=22)
    assert not workspace.store.get("v2_strategy_rechecks").get(account["id"])
    assert workspace.position_reviews.held(account)["btc"]["logic_version"] == old


@pytest.mark.parametrize("failure", ["ai", "invalid_stop", "unknown_order"])
def test_failed_adoption_preserves_old_protection(
    workspace, account, opportunity, quote, monkeypatch, failure
):
    account, proposal, jobs = setup_recheck(workspace, account, opportunity, quote, monkeypatch)
    old = copy.deepcopy(workspace.position_reviews.held(account)["btc"])
    if failure == "ai":
        monkeypatch.setattr(
            ai_client, "response", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("unavailable"))
        )
    elif failure == "invalid_stop":
        proposal["stop"] = 90
    else:
        with workspace.store.transaction() as db:
            db.execute(
                "INSERT INTO v2_orders VALUES (?,?,?,?,?,?)",
                ("pending-review", account["id"], "open", "unknown", "{}", time.time()),
            )
    edit(workspace.logic, ema_fast=22)
    fn, args = jobs[0]
    fn(*args)
    assert workspace.position_reviews.held(account)["btc"] == old
    assert workspace.position_reviews.state(account)["status"] == "error"
    assert workspace.store.get("v2_strategy_rechecks")[account["id"]]["state"] == "pending"


def test_strategy_close_is_a_proposal_until_broker_executes(
    workspace, account, opportunity, quote, monkeypatch
):
    account, proposal, jobs = setup_recheck(workspace, account, opportunity, quote, monkeypatch)
    proposal["action"] = "close"
    edit(workspace.logic, ema_fast=22)
    fn, args = jobs[0]
    fn(*args)
    assert "btc" in workspace.position_reviews.held(account)
    ops = workspace.position_reviews.opportunities(account, {}, workspace.quotes)
    assert ops["btc"]["ai"]["action"] == "close"
    workspace.paper.protect(account, workspace.quotes, ops)
    assert not workspace.position_reviews.held(account)


def test_strategy_changed_during_ai_discards_response(workspace, account, opportunity, quote, monkeypatch):
    account, proposal, jobs = setup_recheck(workspace, account, opportunity, quote, monkeypatch)
    original = workspace.position_reviews.held(account)
    edit(workspace.logic, ema_fast=22)

    def mutate(*args, **kwargs):
        edit(workspace.logic, ema_fast=23)
        return response({"plans": [proposal]})

    monkeypatch.setattr(ai_client, "response", mutate)
    fn, args = jobs[0]
    fn(*args)
    assert workspace.position_reviews.held(account) == original
    assert (
        workspace.store.get("v2_strategy_rechecks")[account["id"]]["version"]
        == workspace.logic.current()["id"]
    )


def test_invalid_optimizer_proposal_has_explicit_result(workspace, monkeypatch):
    add_history(workspace)
    workspace.logic.settings({**ReviewSettings().model_dump(), "auto_optimize": True})
    old = workspace.logic.current()
    mock_review(monkeypatch, workspace, 50, {**old["parameters"], "ema_fast": 100, "ema_slow": 50})
    r = workspace.logic.review()
    assert r["action"] == "invalid_proposal" and workspace.logic.current() == old


def test_account_switch_requires_position_review():
    a = Account(strategy_recheck_enabled=True, name="test")
    assert not a.strategy_recheck_enabled


def test_review_includes_effective_rules_and_labels_absent_evidence(workspace, monkeypatch):
    add_history(workspace)
    workspace.logic.settings({**ReviewSettings().model_dump(), "auto_optimize": True})
    calls = mock_review(monkeypatch, workspace, 50)
    r = workspace.logic.review()
    payload = json.loads(calls[0]["input"])
    assert r["action"] == "evidence_missing"
    assert payload["effective_parameters"]["switches"]["ema_enabled"]
    assert payload["trade_evidence"]["limitations"]
    assert "test-key" not in calls[0]["input"]


def test_evidence_endpoint_requires_login(client):
    assert client.get("/api/v2/logic/evidence").status_code == 401


def test_evidence_endpoint_and_legacy_recovery(logged, workspace, account, opportunity, quote):
    op = {**opportunity, "logic_version": workspace.logic.current()["id"], "demo": False}
    enter(workspace, account, op, quote)
    workspace.paper.close(account["id"], "btc", quote, reason="manual")
    with workspace.store.transaction() as db:
        db.execute("DELETE FROM v2_trade_journal")
        db.execute("DELETE FROM v2_trade_events")
    journal.backfill_legacy(workspace.store)
    journal.backfill_legacy(workspace.store)
    response = logged.get("/api/v2/logic/evidence")
    assert response.status_code == 200
    data = response.json()
    assert len(data["closed_trade_evidence"]) == 1
    d = data["closed_trade_evidence"][0]
    assert d["schema"] == 0 and d["entry_regime"] == "unknown"
    assert d["missing"] and d["outcome"]["trade_net_pnl"] is not None
    assert len(d["events"]) == 2
    assert "password" not in response.text and "apiKey" not in response.text
