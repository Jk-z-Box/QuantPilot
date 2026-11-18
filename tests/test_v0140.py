import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from test_v2 import universe

from quantpilot.retired import retire_console
from quantpilot.store import Store
from quantpilot.v2.models import account_revision
from quantpilot.v2.position_automation import loss_add_ready, trail_state, update_trailing
from quantpilot.v2.sentiment import observation


def book(w, a):
    return w.store.get("v2_ledgers")[a["id"]]


def test_account_paused_save_and_stale_form_conflict(logged, workspace):
    account = workspace.store.get("v2_accounts")["paper-default"]
    revision = account_revision(account)
    body = {
        **account,
        "name": "Paused settings persist",
        "expected_revision": revision,
        "auto": False,
        "position_review_enabled": True,
        "strategy_recheck_enabled": True,
    }
    response = logged.post("/api/v2/accounts", json=body)
    assert response.status_code == 200
    assert logged.post("/api/v2/accounts", json={**body, "name": "stale"}).status_code == 409
    assert workspace.store.get("v2_accounts")[account["id"]]["name"] == body["name"]
    assert Store(workspace.store.directory).get("v2_accounts")[account["id"]]["strategy_recheck_enabled"]
    current = next(a for a in logged.get("/api/v2/state").json()["accounts"] if a["id"] == account["id"])
    assert current["revision"] != revision
    assert logged.post("/api/v2/accounts", json=account).status_code == 409


def test_concurrent_saves_cannot_overwrite_silently(workspace):
    a = workspace.store.get("v2_accounts")["paper-default"]
    revision = account_revision(a)

    def save(i):
        try:
            workspace.save_account({**a, "name": f"edit-{i}"}, revision)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(save, range(2))) == 1


def test_display_order_does_not_reorder_trading_inputs(workspace, logged):
    before = workspace.store.get("v2_watchlist")
    order = [i["id"] for i in reversed(before)]
    assert logged.put("/api/v2/display-order", json={"ids": order}).status_code == 200
    assert workspace.store.get("v2_watchlist") == before
    assert logged.get("/api/v2/state").json()["display_order"] == order
    assert logged.put("/api/v2/display-order", json={"ids": order[:-1]}).status_code == 409


def test_retirement_only_removes_v1_data(workspace):
    before = workspace.store.get("v2_accounts")
    workspace.vault.put("v2:account:paper-default", {"secret": "current"})
    workspace.vault.put("okx", {"secret": "old"})
    with workspace.store.transaction() as db:
        workspace.store.write(db, "account", {"positions": {"legacy": "old"}})
        workspace.store.write(db, "strategy", {"enabled": True})
        db.execute("CREATE TABLE orders(id TEXT)")
        db.execute("INSERT INTO orders VALUES ('old')")
        workspace.store.write(db, "v2_unchanged_probe", {"value": 7})
    retire_console(workspace.store)
    retire_console(workspace.store)
    assert workspace.store.get("v2_accounts") == before
    assert workspace.store.get("v2_unchanged_probe") == {"value": 7}
    assert workspace.vault.get("okx") is None
    assert workspace.vault.get("v2:account:paper-default")["secret"] == "current"
    with workspace.store.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='orders'").fetchone()
        assert not db.execute("SELECT 1 FROM kv WHERE key IN ('account','strategy')").fetchone()
    Store(workspace.store.directory)
    with workspace.store.connect() as db:
        assert not db.execute("SELECT 1 FROM kv WHERE key='account'").fetchone()


@pytest.mark.parametrize("bad", [float("nan"), -1, 101, float("inf")])
def test_sentiment_invalid_values_rejected(bad):
    with pytest.raises(ValueError):
        observation({"data": [{"value": bad, "timestamp": 1000}]}, 1100)


def test_sentiment_uses_publication_time_not_fetch_time():
    x = observation({"data": [{"value": 24, "timestamp": 1000}, {"value": 30, "timestamp": 900}]}, 140000)
    assert x["status"] == "stale" and x["published_at"] == 1000
    assert x["details"]["history"][1]["value"] == 30


def configure_loss(w, a):
    a = copy.deepcopy(a)
    a["sizing"][0].update(
        mode="fixed",
        add_count=1,
        add_ratios=[0.25],
        loss_add={"enabled": True, "trigger_pct": 1, "max_loss_pct": 3, "cooldown": 30},
    )
    return w.save_account(a)


def test_loss_add_eligibility_bounds_and_same_signal(workspace, account, opportunity, quote):
    a = configure_loss(workspace, account)
    now = time.time()
    p = {"side": "long", "entry": 100, "opened_at": now - 60, "tp_done": []}
    q = {**quote, "bid": 98.49, "ask": 98.51, "mid": 98.5}
    assert loss_add_ready(a, "btc", p, q, opportunity, now)
    assert not loss_add_ready(a, "btc", p, {**q, "timestamp": now - 60}, opportunity, now)
    assert not loss_add_ready(a, "btc", p, q, {**opportunity, "direction": "short"}, now)
    assert not loss_add_ready(a, "btc", {**p, "last_add_at": now}, q, opportunity, now)
    assert not loss_add_ready(a, "btc", p, {**q, "ask": 95}, opportunity, now)


def test_loss_add_uses_shared_count_cooldown_and_preserves_stop(workspace, account, opportunity, quote):
    a = configure_loss(workspace, account)
    workspace.paper.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    with workspace.store.transaction() as db:
        books = workspace.store.read(db, "v2_ledgers")
        p = books[a["id"]]["positions"]["btc"]
        p["opened_at"] -= 60
        workspace.store.write(db, "v2_ledgers", books)
    q = {**quote, "bid": 98.49, "ask": 98.51, "mid": 98.5}
    op = {**opportunity, "id": "loss-add-1"}
    before = book(workspace, a)["positions"]["btc"]
    result = workspace.paper.enter(a, op, {"btc": q}, universe(workspace), "balanced", True)
    assert result["status"] == "filled"
    after = book(workspace, a)["positions"]["btc"]
    assert after["adds"] == 1 and after["stop"] == before["stop"]
    assert Decimal(after["qty"]) == Decimal(before["qty"]) * Decimal("1.25")
    with pytest.raises(ValueError):
        workspace.paper.enter(a, {**op, "id": "another"}, {"btc": q}, universe(workspace), "balanced", True)


@pytest.mark.parametrize("side,prices", [("long", [100, 103, 105, 104]), ("short", [100, 97, 95, 96])])
def test_trailing_only_tightens_and_triggers_retrace(side, prices):
    p = {"entry": 100, "side": side}
    config = {"enabled": True, "activation_pct": 1, "distance_pct": 0.5}
    stops = []
    for price in prices:
        state, hit = trail_state(p, {"bid": price, "ask": price}, config)
        p["trailing"] = state
        if state["stop"] is not None:
            stops.append(state["stop"])
    assert hit and stops == sorted(stops, reverse=side == "short")
    assert trail_state(p, {"bid": prices[-1], "ask": prices[-1]}, {**config, "enabled": False})[1] is False


def test_trailing_partial_once_and_restart_retains_high(workspace, account, opportunity, quote):
    a = copy.deepcopy(account)
    a["sizing"][0]["trailing_take_profit"].update(
        enabled=True, activation_pct=1, distance_pct=0.5, exit_ratio=0.5
    )
    a = workspace.save_account(a)
    workspace.paper.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    before = book(workspace, a)["positions"]["btc"]["qty"]
    up = {**quote, "bid": 104, "ask": 104.01, "mid": 104}
    down = {**quote, "bid": 103, "ask": 103.01, "mid": 103}
    workspace.paper.protect(a, {"btc": up}, {"btc": opportunity})
    reopened = Store(workspace.store.directory)
    assert reopened.get("v2_ledgers")[a["id"]]["positions"]["btc"]["trailing"]["peak"] == 104
    workspace.paper.protect(a, {"btc": down}, {"btc": opportunity})
    assert Decimal(book(workspace, a)["positions"]["btc"]["qty"]) == Decimal(before) / 2
    workspace.paper.protect(a, {"btc": down}, {"btc": opportunity})
    assert Decimal(book(workspace, a)["positions"]["btc"]["qty"]) == Decimal(before) / 2
    assert book(workspace, a)["positions"]["btc"]["trailing"]["done"]
    with workspace.store.connect() as db:
        rows = db.execute("SELECT result FROM v2_orders WHERE action='close'").fetchall()
        assert any(json.loads(x[0])["reason"] == "trailing_take_profit" for x in rows)


def test_trailing_does_not_need_automation_and_stale_quote_cannot_trigger(
    workspace, account, opportunity, quote
):
    a = copy.deepcopy(account)
    a["sizing"][0]["trailing_take_profit"]["enabled"] = True
    a = workspace.save_account(a)
    workspace.paper.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    a = workspace.save_account({**a, "auto": False})
    update_trailing(workspace.store, a, "btc", {**quote, "bid": 104}, opportunity)
    assert not update_trailing(
        workspace.store, a, "btc", {**quote, "bid": 102, "timestamp": time.time() - 50}, opportunity
    )[0]
    assert update_trailing(workspace.store, a, "btc", {**quote, "bid": 102}, opportunity)[0]


def test_atr_missing_keeps_existing_trigger():
    p = {"side": "long", "entry": 100, "trailing": {"peak": 104, "armed": True, "stop": 102}}
    state, hit = trail_state(p, {"bid": 101, "ask": 101}, {"enabled": True, "distance_mode": "atr"}, {})
    assert state["stop"] == 102 and hit


def test_exchange_trailing_partial_does_not_dedup_later_hard_stop(workspace, monkeypatch, opportunity, quote):
    from test_v2 import demo_account

    a, broker = demo_account(workspace, monkeypatch)
    a["sizing"][0]["trailing_take_profit"].update(enabled=True, exit_ratio=0.5)
    a = workspace.save_account(a)
    client = broker.client(a)
    broker.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    initial = client.fetch_positions()[0]["contracts"]
    broker.manage(a, {"btc": {**quote, "bid": 104, "ask": 104.01, "mid": 104}}, {}, universe(workspace))
    broker.manage(a, {"btc": {**quote, "bid": 103, "ask": 103.01, "mid": 103}}, {}, universe(workspace))
    assert client.fetch_positions()[0]["contracts"] == pytest.approx(initial / 2, abs=0.0001)
    broker.manage(a, {"btc": {**quote, "bid": 103, "ask": 103.01, "mid": 103}}, {}, universe(workspace))
    assert client.fetch_positions()[0]["contracts"] == pytest.approx(initial / 2, abs=0.0001)
    broker.manage(a, {"btc": {**quote, "bid": 94, "ask": 94.01, "mid": 94}}, {}, universe(workspace))
    assert not client.fetch_positions()
    assert all(c[5]["reduceOnly"] for c in client.calls[-2:])


def test_sentiment_switches_and_market_scope(quote):
    from test_v070 import bars

    from quantpilot.v2.models import Instrument
    from quantpilot.v2.signals import evaluate

    now = time.time()
    item = Instrument(id="btc", label="BTC", symbol="BTC/USDT:USDT").model_dump()
    frames = {f: bars(f) for f in ("1w", "1d", "4h", "1h")}
    q = {**quote, "mid": 109, "ask": 109.01, "bid": 108.99}
    ev = observation({"data": [{"value": 20, "timestamp": now - 60}]}, now)

    def run(params, evidence, group="crypto"):
        return evaluate(
            {**item, "group": group}, frames, q, evidence, "balanced", now, min_score=1, parameters=params
        )

    off = run({}, [ev])
    on = run({"sentiment_enabled": True}, [ev])
    assert not any(b["name"] == "BTC 市场情绪参考" for b in off["breakdown"])
    assert next(b for b in on["breakdown"] if b["name"] == "BTC 市场情绪参考")["points"] == 5
    missing = run({"sentiment_enabled": True, "sentiment_require_fresh": True}, [])
    assert any("指数缺失" in b for b in missing["blockers"])
    assert not any(
        b["name"] == "BTC 市场情绪参考" for b in run({"sentiment_enabled": True}, [ev], "equity")["breakdown"]
    )
