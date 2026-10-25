import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantpilot.engine import Engine, OrderRequest
from quantpilot.market import MarketError
from quantpilot.runner import Runner
from quantpilot.store import Store


def test_round_trip_with_fees_and_no_short(engine, order):
    buy = engine.execute(order)
    assert buy["status"] == "filled"
    cash = Decimal(engine.store.get("account")["cash"])
    assert cash == Decimal("10000") - Decimal(buy["price"]) - Decimal(buy["fee"])
    sell = order.model_copy(update={"id": "sell-order-0001", "side": "sell", "limit_price": Decimal("99")})
    result = engine.execute(sell)
    assert result["status"] == "filled"
    account = engine.store.get("account")
    assert not account["positions"]
    assert Decimal(account["realized_pnl"]) < 0
    assert engine.execute(sell.model_copy(update={"id": "sell-again-0002"}))["code"] == "insufficient_position"


def test_partial_sale_preserves_proportional_cost_basis(engine, order):
    engine.execute(order)
    cost = Decimal(engine.store.get("account")["positions"]["okx:BTC/USDT"]["cost"])
    sell = order.model_copy(update={"id": "partial-sale", "side": "sell", "quantity": Decimal("0.4"), "limit_price": Decimal("99")})
    result = engine.execute(sell)
    assert result["status"] == "filled"
    account = engine.store.get("account")
    position = account["positions"]["okx:BTC/USDT"]
    assert Decimal(position["quantity"]) == Decimal("0.6")
    assert Decimal(position["cost"]) == cost * Decimal("0.6")
    expected_pnl = Decimal(result["price"]) * Decimal("0.4") - Decimal(result["fee"]) - cost * Decimal("0.4")
    assert Decimal(account["realized_pnl"]) == expected_pnl


def test_fill_costs_cannot_cross_daily_loss_cap(engine, order):
    engine.settings.daily_loss_limit = "0.01"
    assert engine.execute(order)["code"] == "daily_loss_limit"
    assert engine.store.get("account")["cash"] == "10000"


def test_idempotency_persists_after_reconnect(engine, order):
    result = engine.execute(order)
    cash = engine.store.get("account")["cash"]
    restarted = Engine(engine.settings, Store(engine.store.directory), engine.market)
    assert restarted.execute(order) == result
    assert restarted.store.get("account")["cash"] == cash
    assert restarted.execute(order.model_copy(update={"quantity": Decimal("2")}))["code"] == "idempotency_conflict"


def test_simultaneous_duplicate_only_once(engine, order):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: engine.execute(order), range(12)))
    assert all(result == results[0] for result in results)
    assert engine.store.get("account")["positions"]["okx:BTC/USDT"]["quantity"] == "1"
    assert len(engine.store.recent("orders")) == 1


def test_concurrent_exposure_serialized(engine, order):
    engine.settings.max_exposure = "500"
    requests = [order.model_copy(update={"id": f"concurrent-{i}", "quantity": Decimal("3")}) for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(engine.execute, requests))
    assert sum(row["status"] == "filled" for row in results) == 1


@pytest.mark.parametrize(("change", "code"), [
    ({"mode": "live"}, "live_trading_locked"),
    ({"quantity": Decimal("10")}, "max_order_exceeded"),
    ({"expires_at": 1}, "order_expired_or_invalid_ttl"),
    ({"expires_at": time.time() + 10000}, "order_expired_or_invalid_ttl"),
    ({"limit_price": Decimal("90")}, "ioc_limit_not_marketable"),
    ({"stop_price": None}, "invalid_protection_prices"),
    ({"take_price": Decimal("90")}, "invalid_protection_prices"),
    ({"exchange": "unknown"}, "market_not_allowlisted"),
    ({"symbol": "BTC/USDT:USDT"}, "market_not_allowlisted"),
])
def test_order_guardrails(engine, order, change, code):
    assert engine.execute(order.model_copy(update=change))["code"] == code
    assert engine.store.get("account")["cash"] == "10000"
    assert len(engine.store.recent("audit")) == 1


@pytest.mark.parametrize("field", ["quantity", "limit_price", "stop_price", "take_price", "expires_at"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_request_rejected(order, field, value):
    with pytest.raises(ValidationError):
        OrderRequest.model_validate({**order.model_dump(), field: value})


@pytest.mark.parametrize("quantity", ["0", "-1", "100000001", "0.0000000000001"])
def test_invalid_size_rejected(order, quantity):
    with pytest.raises(ValidationError):
        OrderRequest.model_validate({**order.model_dump(), "quantity": quantity})


@pytest.mark.parametrize(("attribute", "value", "code"), [
    ("age", 31, "stale_quote"), ("age", -6, "stale_quote"),
    ("spread", Decimal("2"), "spread_too_wide"),
    ("failure", True, "exchange_unavailable"),
    ("timestamp_missing", True, "unverified_quote_timestamp"),
])
def test_quote_fail_closed(engine, order, attribute, value, code):
    setattr(engine.market, attribute, value)
    assert engine.execute(order)["code"] == code


def test_kill_blocks_buys_keeps_exits(engine, order):
    assert engine.execute(order)["status"] == "filled"
    engine.store.set_kill(True)
    assert engine.execute(order.model_copy(update={"id": "blocked-order"}))["code"] == "kill_switch_active"
    sell = order.model_copy(update={"id": "reduce-order", "side": "sell", "limit_price": Decimal("99")})
    assert engine.execute(sell)["status"] == "filled"


def test_daily_loss_mark_to_market_and_exit(engine, order):
    engine.settings.daily_loss_limit = "5"
    assert engine.execute(order)["status"] == "filled"
    engine.market.price = Decimal("90")
    next_order = order.model_copy(update={"id": "next-order", "stop_price": Decimal("89")})
    assert engine.execute(next_order)["code"] == "daily_loss_limit"
    snapshot = engine.snapshot()
    assert Decimal(snapshot["day_pnl"]) < -5
    sell = order.model_copy(update={"id": "exit-loss", "side": "sell", "limit_price": Decimal("80")})
    assert engine.execute(sell)["status"] == "filled"


def test_missing_portfolio_mark_blocks_buy(engine, order):
    assert engine.execute(order)["status"] == "filled"
    engine.market.failure = True
    assert engine.snapshot()["valuation_valid"] is False
    assert engine.execute(order.model_copy(update={"id": "new-market", "symbol": "ETH/USDT"}))["status"] == "rejected"


@pytest.mark.parametrize("limit", ["max_position", "max_exposure"])
def test_position_and_total_limits(engine, order, limit):
    setattr(engine.settings, limit, "50")
    assert engine.execute(order)["code"] == limit + "_exceeded"


def test_cash_includes_fees(engine, order):
    with engine.store.transaction() as db:
        account = engine.store.read(db, "account")
        account["cash"] = "100.10"
        engine.store.write(db, "account", account)
    assert engine.execute(order)["code"] == "insufficient_cash"


def test_stop_loss_gap_executes_at_market_not_stop(engine, order):
    engine.execute(order)
    engine.store.set_kill(True)
    engine.market.price = Decimal("80")
    results = engine.protect()
    assert results[0]["status"] == "filled"
    assert Decimal(results[0]["price"]) < 80
    assert not engine.store.get("account")["positions"]


def test_protective_exit_can_exceed_entry_order_limit(engine, order):
    engine.execute(order)
    engine.market.price = Decimal("600")
    assert engine.protect()[0]["status"] == "filled"


def test_stop_cannot_be_loosened(engine, order):
    engine.execute(order)
    new_order = order.model_copy(update={"id": "loosen-stop", "stop_price": Decimal("80")})
    assert engine.execute(new_order)["code"] == "cannot_loosen_stop"


def test_projected_stop_loss_capped(engine, order):
    order = order.model_copy(update={"quantity": Decimal("4"), "stop_price": Decimal("1")})
    assert engine.execute(order)["code"] == "trade_loss_limit"


def test_stale_strategy_revision_rejected(engine, order):
    assert engine.execute(order, strategy_revision="previous")["code"] == "strategy_stopped_or_changed"


def test_restart_disables_strategy(engine):
    with engine.store.transaction() as db:
        config = engine.store.read(db, "strategy")
        config["enabled"] = True
        engine.store.write(db, "strategy", config)
    runner = Runner(engine)
    runner.start()
    runner.stop()
    assert engine.store.get("strategy")["enabled"] is False


def test_portfolio_changed_fails_closed(engine):
    with pytest.raises(MarketError, match="portfolio_changed_retry"):
        engine.valuation({"positions": {"okx:BTC/USDT": {"quantity": "1"}}, "cash": "0"}, {}, time.time())


def test_protection_failure_isolated_per_market(engine, order):
    engine.execute(order)
    engine.execute(order.model_copy(update={"id": "second-position", "symbol": "ETH/USDT"}))
    old_quote = engine.market.quote

    def quote(exchange, symbol):
        if symbol == "BTC/USDT":
            raise MarketError("exchange_unavailable")
        return old_quote(exchange, symbol)

    engine.market.quote = quote
    engine.market.price = Decimal("80")
    # Reduction must not require valuations from unrelated, unavailable markets.
    results = engine.protect()
    assert len(results) == 1
    assert results[0]["status"] == "filled"
    assert "okx:ETH/USDT" not in engine.store.get("account")["positions"]
