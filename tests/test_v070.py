import copy
import json
import time

import httpx
import pytest
from pydantic import ValidationError

from quantpilot.v2 import ai_client
from quantpilot.v2.action_review import Decision
from quantpilot.v2.history import equity_history, snapshot_history, validate_reference
from quantpilot.v2.models import Instrument
from quantpilot.v2.notifications import NotificationSettings, TelegramKey, emit
from quantpilot.v2.public_feeds import farside_etf
from quantpilot.v2.rules import Parameters, effective
from quantpilot.v2.signals import evaluate


def enable_actions(w, monkeypatch, account, decision="approve"):
    w.vault.put("v2:ai:openai", {"apiKey": "test-only"})
    w.preferences({**w.store.get("v2_preferences"), "ai_model": "test-model", "ai_action_enabled": False})
    account.update(w.save_account({**account, "ai_action_enabled": True}))
    # Market sharing is integration-tested in test_v0100; these tests cover account guards.
    monkeypatch.setattr(
        w.action_review.market, "inspect", lambda op, q, p: {"state": "approved", "opportunity": op}
    )
    callbacks = []
    monkeypatch.setattr(w.action_review.pool, "submit", lambda fn, *args: callbacks.append((fn, args)))
    response = Decision(
        decision=decision,
        reason="已检查点位及风险",
        entry_low=None,
        entry_high=None,
        stop=None,
        target_1=None,
        target_2=None,
        exit_ratio=None,
    ).model_dump()
    monkeypatch.setattr(
        ai_client,
        "response",
        lambda *a, **kw: {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(response)}]}
            ],
        },
    )
    return callbacks, response


def run_job(callbacks):
    fn, args = callbacks.pop(0)
    fn(*args)


def paper_enter(w, a, o, q):
    return w.paper.enter(a, o, {"btc": q}, {i["id"]: i for i in w.store.get("v2_watchlist")}, "balanced")


def bars(frame="1w", count=80):
    span = {"1w": 604800, "1d": 86400, "4h": 14400, "1h": 3600}[frame]
    end = int(time.time()) // span * span - span
    return [
        [
            (end - (count - 1 - i) * span) * 1000,
            100 + i * 0.1,
            102 + i * 0.1,
            99 + i * 0.1,
            101 + i * 0.1,
            100 + i,
        ]
        for i in range(count)
    ]


def test_notifications_key_auth_and_categories(logged, client, workspace):
    assert logged.get("/api/v2/notifications").status_code == 200
    n = workspace.notifications
    with pytest.raises(ValueError):
        n.settings({**NotificationSettings().model_dump(), "telegram_enabled": True})
    assert (
        logged.put(
            "/api/v2/notifications/telegram", json={"token": "not-a-valid-token-but-long", "chat_id": "123"}
        ).status_code
        == 409
    )
    token = "123456789:" + ("x" * 30)
    assert (
        logged.put("/api/v2/notifications/telegram", json={"token": token, "chat_id": "-123"}).status_code
        == 200
    )
    assert token not in logged.get("/api/v2/state").text
    settings = NotificationSettings(telegram_enabled=True, sound_enabled=True).model_dump()
    settings["telegram"]["opportunity"] = False
    n.settings(settings)
    with workspace.store.transaction() as db:
        emit(db, "one", "open", "example")
        emit(db, "one", "open", "duplicate")
        emit(db, "two", "opportunity", "example opportunity")
    assert len(n.state(0)["events"]) == 2
    assert n.state(0)["events"][1]["delivery"] == "disabled"
    assert logged.request("DELETE", "/api/v2/notifications/telegram", json={}).status_code == 200
    assert not n.state()["settings"]["telegram_enabled"]
    assert workspace.vault.get("v2:telegram") is None


def setup_telegram(w, transport):
    n = w.notifications
    n.transport = transport
    n.key(TelegramKey(token="123456789:" + ("x" * 30), chat_id="123"))
    n.settings(NotificationSettings(telegram_enabled=True).model_dump())
    with w.store.transaction() as db:
        emit(db, "x", "open", "成交测试")
    return n


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(200, json={"ok": True}), "sent"),
        (httpx.Response(403, json={"ok": False}), "failed"),
        (httpx.Response(429, json={"parameters": {"retry_after": 90}}), "pending"),
    ],
)
def test_telegram_delivery_and_retry(workspace, response, expected):
    requests = []

    def handle(r):
        requests.append(r)
        return response

    n = setup_telegram(workspace, httpx.MockTransport(handle))
    n.deliver_once()
    assert n.state(0)["events"][0]["delivery"] == expected
    n.deliver_once()
    assert len(requests) == 1
    assert json.loads(requests[0].content)["text"] == "成交测试"


def test_telegram_uncertain_result_is_not_replayed(workspace):
    calls = []

    def fail(request):
        calls.append(1)
        raise httpx.ReadTimeout("hidden")

    n = setup_telegram(workspace, httpx.MockTransport(fail))
    n.deliver_once()
    n.deliver_once()
    assert len(calls) == 1 and n.state(0)["events"][0]["delivery"] == "unknown"


def test_paper_events_only_after_committed_fill(workspace, account, opportunity, quote):
    paper_enter(workspace, account, opportunity, quote)
    paper_enter(workspace, account, opportunity, quote)
    events = workspace.notifications.state(0)["events"]
    assert [e["category"] for e in events] == ["open"]
    workspace.paper.close(account["id"], "btc", quote, reason="stop_loss")
    assert [e["category"] for e in workspace.notifications.state(0)["events"]] == ["open", "stop_loss"]


def test_action_review_blocks_until_approved(workspace, account, opportunity, quote, monkeypatch):
    callbacks, _ = enable_actions(workspace, monkeypatch, account)
    with pytest.raises(ValueError, match="等待"):
        paper_enter(workspace, account, opportunity, quote)
    assert not workspace.store.get("v2_ledgers")[account["id"]]["positions"]
    run_job(callbacks)
    assert paper_enter(workspace, account, opportunity, quote)["status"] == "filled"
    assert any(e["category"] == "ai" for e in workspace.notifications.state(0)["events"])


def test_ai_rejection_cannot_open(workspace, account, opportunity, quote, monkeypatch):
    callbacks, _ = enable_actions(workspace, monkeypatch, account, "reject")
    gate = workspace.action_review
    assert gate.inspect(account["id"], "btc", "open", quote, opportunity)["state"] == "pending"
    run_job(callbacks)
    with pytest.raises(ValueError, match="暂不执行"):
        paper_enter(workspace, account, opportunity, quote)
    assert not workspace.store.get("v2_ledgers")[account["id"]]["positions"]


def test_ai_timeout_blocks_entry_and_allows_protection(workspace, account, opportunity, quote, monkeypatch):
    paper_enter(workspace, account, opportunity, quote)
    callbacks, _ = enable_actions(workspace, monkeypatch, account)
    gate = workspace.action_review
    # Isolate async deadline behavior from sizing eligibility (tested separately).
    monkeypatch.setattr("quantpilot.v2.action_review.account_context", lambda *a: {})
    for action in ("open", "add", "stop_loss", "take_profit", "liquidation_distance"):
        gate.last_requests.clear()  # Each case independently tests timeout, not the shared cadence.
        assert gate.inspect(account["id"], "btc", action, quote, opportunity)["state"] == "pending"
        for job in gate.jobs.values():
            job["deadline"] = time.time() - 1
        if action in ("open", "add"):
            with pytest.raises(ValueError, match="不可用"):
                gate.inspect(account["id"], "btc", action, quote, opportunity)
        else:
            assert gate.inspect(account["id"], "btc", action, quote, opportunity)["state"] == "fallback"
    # Late completion never overwrites the timed-out decision.
    while callbacks:
        run_job(callbacks)
    assert all(j["state"] == "error" for j in gate.jobs.values())


def test_disconnected_ai_stop_loss_still_fills(workspace, account, opportunity, quote, monkeypatch):
    paper_enter(workspace, account, opportunity, quote)
    enable_actions(workspace, monkeypatch, account)
    monkeypatch.setattr(ai_client, "ready", lambda *a: False)
    q = {**quote, "mid": 95.0, "bid": 94.99, "ask": 95.01}
    workspace.paper.protect(account, {"btc": q}, {})
    assert not workspace.store.get("v2_ledgers")[account["id"]]["positions"]
    assert workspace.notifications.state(0)["events"][-1]["category"] == "stop_loss"


def test_ai_ticket_rejected_after_balance_changes(workspace, account, opportunity, quote, monkeypatch):
    callbacks, _ = enable_actions(workspace, monkeypatch, account)
    gate = workspace.action_review
    gate.inspect(account["id"], "btc", "open", quote, opportunity)
    run_job(callbacks)
    approved = gate.inspect(account["id"], "btc", "open", quote, opportunity)["opportunity"]
    with workspace.store.transaction() as db:
        book = workspace.store.read(db, "v2_ledgers")
        book[account["id"]]["cash"] = "9900"
        workspace.store.write(db, "v2_ledgers", book)
    with workspace.store.transaction() as db:
        with pytest.raises(ValueError, match="配置已变化"):
            gate.commit_check(db, account["id"], approved)


def test_ai_adjusted_points_are_validated(workspace, account, opportunity, quote, monkeypatch):
    callbacks, result = enable_actions(workspace, monkeypatch, account, "adjust")
    result.update(stop=97.0, target_1=106.0, target_2=111.0)
    gate = workspace.action_review
    gate.inspect(account["id"], "btc", "open", quote, opportunity)
    run_job(callbacks)
    answer = gate.inspect(account["id"], "btc", "open", quote, opportunity)
    assert answer["opportunity"]["plan"]["stop"] == 97.0
    result["stop"] = 101.0
    with pytest.raises(ValueError, match="无效"):
        gate.adjust(copy.deepcopy(opportunity), result, quote)


def test_reference_bars_never_splice_native(workspace):
    item = {**workspace.store.get("v2_watchlist")[0], "history_enabled": True}
    source = bars()
    native = source[-5:]
    with workspace.store.transaction() as db:
        workspace.store.write(
            db,
            "v2_history",
            {
                item["id"]: {
                    "context": [item["exchange"], item["symbol"]],
                    "attempted_at": time.time(),
                    "frames": {
                        "1w": {
                            "rows": source,
                            "kind": "perpetual_reference",
                            "source": "OKX",
                            "last_bar_at": source[-1][0] / 1000,
                        }
                    },
                }
            },
        )
    frames, notes = workspace.history.supplement(item, {"1w": native, "1h": bars("1h")})
    assert frames["1w"] == source and frames["1w"] is not source
    assert notes["1w"]["status"] == "reference"
    assert workspace.history.supplement({**item, "history_enabled": False}, {"1w": native})[0]["1w"] == native


@pytest.mark.parametrize("mode", ["stale", "bad_price", "nan", "duplicate", "gap"])
def test_history_rejects_unverified_data(mode):
    data = bars()
    if mode == "stale":
        data = [[(r[0] - 20 * 86400 * 1000), *r[1:]] for r in data]
    if mode == "bad_price":
        data[2][2] = 1
    if mode == "nan":
        data[2][4] = float("nan")
    if mode == "duplicate":
        data[2][0] = data[1][0]
    if mode == "gap":
        data.pop(3)
    with pytest.raises(ValueError):
        validate_reference(data, "1w", time.time())


def test_history_interval_and_failure_cache(workspace, monkeypatch):
    item = {**workspace.store.get("v2_watchlist")[0], "history_enabled": True}
    callbacks = []
    monkeypatch.setattr(workspace.history.pool, "submit", lambda *a: callbacks.append(a))
    workspace.market.demo = False
    workspace.history.supplement(item, {"1w": []})
    workspace.history.supplement(item, {"1w": []})
    assert len(callbacks) == 1
    workspace.history.running.clear()
    workspace.history.supplement(item, {"1w": []})
    assert len(callbacks) == 1
    assert workspace.history.state()[item["id"]]["attempted_at"] > 0


def test_equity_adapter_verifies_symbol_and_preserves_source():
    data = [[r[0] + 14 * 3600 * 1000, *r[1:]] for r in bars("1d")]
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": "AVGO", "currency": "USD", "exchangeName": "NMS"},
                    "timestamp": [int(r[0] / 1000) for r in data],
                    "indicators": {
                        "quote": [
                            {
                                k: [r[i] for r in data]
                                for k, i in [
                                    ("open", 1),
                                    ("high", 2),
                                    ("low", 3),
                                    ("close", 4),
                                    ("volume", 5),
                                ]
                            }
                        ]
                    },
                }
            ]
        }
    }
    ref = equity_history("AVGO", "1d", httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assert ref["kind"] == "underlying_reference" and ref["currency"] == "USD"
    body["chart"]["result"][0]["meta"]["symbol"] = "OTHER"
    with pytest.raises(ValueError):
        equity_history("AVGO", "1d", httpx.MockTransport(lambda r: httpx.Response(200, json=body)))


def test_parameter_switches_change_behavior_and_disabled_override_restores_default(quote):
    item = Instrument(id="btc", label="BTC", symbol="BTC/USDT:USDT").model_dump()
    frames = {f: bars(f) for f in ("1w", "1d", "4h", "1h")}
    now = time.time()
    q = {**quote, "mid": 109.0, "ask": 109.01, "bid": 108.99}
    baseline = evaluate(item, frames, q, [], "balanced", now, min_score=1)
    disabled = evaluate(
        item,
        frames,
        q,
        [],
        "balanced",
        now,
        min_score=1,
        parameters={"entry_enabled": False, "rsi_enabled": False, "frame_1w_enabled": False},
    )
    assert any("关闭" in s for s in disabled["blockers"])
    assert not any("RSI" in b["name"] for b in disabled["breakdown"])
    assert "1w" not in disabled["indicators"]
    assert sum(b["max"] for b in disabled["breakdown"]) < sum(b["max"] for b in baseline["breakdown"])
    assert effective({"atr_stop": 4, "overrides": {"atr_stop": False}})["atr_stop"] == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"overrides": {"unknown": False}},
        {"macd_fast": 30, "macd_slow": 20},
        {"rsi_long_min": 80, "rsi_long_max": 40},
        {
            "frame_1w_enabled": False,
            "frame_1d_enabled": False,
            "frame_4h_enabled": False,
            "frame_1h_enabled": False,
        },
    ],
)
def test_full_rules_validation(bad):
    with pytest.raises(ValidationError):
        Parameters.model_validate(bad)


def test_disabled_tp_and_breakeven_respected(workspace, account, opportunity, quote):
    opportunity["parameters"] = {
        "target_1_enabled": False,
        "breakeven_enabled": False,
        "trend_exit_enabled": False,
        "invalid_signal_reduce_enabled": False,
    }
    opportunity["plan"]["targets"][0]["enabled"] = False
    paper_enter(workspace, account, opportunity, quote)
    q = {**quote, "mid": 107.0, "bid": 106.99, "ask": 107.01}
    workspace.paper.protect(account, {"btc": q}, {})
    pos = workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]
    assert pos["tp_done"] == [] and float(pos["stop"]) == 96.0


def test_farside_uses_actual_session_and_marks_partial():
    html = "<table><tr><td>30 Oct 2026</td><td>20.1</td><td>(10.0)</td><td>10.1</td></tr></table>"
    r = farside_etf(html, "ETH", 1793519999)
    assert r["value"] == 10100000 and r["bias"] == "bullish"
    r = farside_etf(html.replace("20.1", "-"), "ETH", 1793519999)
    assert r["bias"] == "neutral" and r["details"]["partial"]


def test_gate_new_liquidation_fields_without_old_keys(workspace):
    from quantpilot.v2.feeds import Feeds

    now = int(time.time()) // 3600 * 3600
    f = Feeds(
        workspace.vault,
        workspace.store,
        httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json=[
                    {"time": now - 3600, "long_liq_usd_new": "1.1", "short_liq_usd_new": "2.2"},
                    {"time": now, "long_liq_usd_new": "3.3", "short_liq_usd_new": "4.4"},
                ],
            )
        ),
    )
    row = f.public_liquidation("BTC")
    assert row["status"] == "fresh" and row["details"]["long_liquidation_usd"] == pytest.approx(4.4)


def test_snapshot_parser_never_uses_model_numbers():
    import datetime as dt

    date = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=2)
    html = "<table><tr><th>Date</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Adj Close</th><th>Volume</th></tr>"
    for i in range(70):
        day = date - dt.timedelta(days=i)
        html += f"<tr><td>{day.strftime('%b %d, %Y')}</td><td>100</td><td>102</td><td>99</td><td>101</td><td>101</td><td>1,200</td></tr>"
    data = snapshot_history(html + "</table>", "1d", time.time())
    assert len(data) == 70 and data[-1][5] == 1200
    with pytest.raises(ValueError):
        snapshot_history("model says 60 candles exist", "1d", time.time())


def test_ai_tp_adjustment_moves_trigger_not_fakes_fill(workspace, account, opportunity, quote, monkeypatch):
    paper_enter(workspace, account, opportunity, quote)
    callbacks, result = enable_actions(workspace, monkeypatch, account, "adjust")
    result.update(target_1=108.0, target_2=114.0)
    q = {**quote, "mid": 107.0, "bid": 106.99, "ask": 107.01}
    assert workspace.paper.close(account["id"], "btc", q, 0.5, "take_profit", 0)["status"] == "pending"
    run_job(callbacks)
    assert workspace.paper.close(account["id"], "btc", q, 0.5, "take_profit", 0)["status"] == "adjusted"
    p = workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]
    assert p["targets"][0]["price"] == 108 and not p["tp_done"]


def test_native_fill_notifications_are_incremental(workspace):
    class Client:
        def market(self, symbol):
            return {"contractSize": 0.01}

        def fetch_order(self, oid, symbol, params):
            return {"filled": 4 if oid == "take" else 0, "average": 110, "status": "closed"}

    p = {"symbol": "BTC/USDT:USDT", "side": "long", "legs": [{"stop_id": "stop", "target_ids": ["take"]}]}
    account = workspace.store.get("v2_accounts")["paper-default"]
    b = workspace.exchange
    b.notify_native_fills(account, "btc", p, Client())
    b.notification_checks = {}
    b.notify_native_fills(account, "btc", p, Client())
    events = workspace.notifications.state(0)["events"]
    assert len(events) == 1 and events[0]["category"] == "take_profit" and "0.04" in events[0]["message"]


def test_ordinary_ai_failure_does_not_abort_other_stop_protection(
    workspace, account, opportunity, quote, monkeypatch
):
    paper_enter(workspace, account, opportunity, quote)
    enable_actions(workspace, monkeypatch, account)
    monkeypatch.setattr(ai_client, "ready", lambda *a: False)
    assert workspace.paper.close(account["id"], "btc", quote, 0.5, "risk_reduce")["status"] == "rejected"
    q = {**quote, "mid": 95.0, "bid": 94.99, "ask": 95.01}
    assert workspace.paper.close(account["id"], "btc", q, reason="stop_loss")["status"] == "filled"


def test_schema_has_all_ai_adjustable_parameters():
    from quantpilot.v2.logic import assessment_schema

    schema = assessment_schema()["$defs"]["Parameters"]
    assert set(schema["required"]) == set(Parameters.model_fields)
    assert schema["properties"]["overrides"]["additionalProperties"] is False
    assert "volume_confirm_enabled" in schema["properties"]


def test_disabled_indicator_lookback_does_not_block_history():
    from quantpilot.v2.signals import indicators, required_bars

    p = Parameters(
        macd_slow=100, macd_signal=50, macd_enabled=False, volatility_lookback=200, volatility_enabled=False
    ).model_dump()
    assert required_bars(p) == 60
    assert indicators(bars("1h"), p)["atr"] > 0
    p["macd_enabled"] = True
    assert required_bars(p) == 151
    with pytest.raises(ValueError):
        indicators(bars("1h"), p)


@pytest.mark.parametrize(
    "source",
    [
        "https://evil.example/quote/AVGO/history/",
        "https://finance.yahoo.com/quote/SOXL/history/",
        "https://finance.yahoo.com/quote/AVGO/history/?frequency=1wk",
    ],
)
def test_ai_history_rejects_wrong_source_or_frequency(workspace, monkeypatch, source):
    from quantpilot.v2.history import ai_snapshot

    monkeypatch.setattr(
        ai_client,
        "response",
        lambda *a, **kw: {
            "status": "completed",
            "output": [
                {"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": source}]}},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps({"url": source})}],
                },
            ],
        },
    )

    def no_network(request):
        raise AssertionError("Unverified URL must not be fetched")

    with pytest.raises(ValueError):
        ai_snapshot(
            {"symbol": "AVGO/USDT:USDT"},
            "1d",
            {"ai_model": "test-model"},
            workspace.vault,
            60,
            transport=httpx.MockTransport(no_network),
        )
