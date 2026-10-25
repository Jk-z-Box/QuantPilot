import time
from decimal import Decimal

from quantpilot.market import MarketError
from quantpilot.runner import Runner
from quantpilot.strategy import analyze


def enable(engine):
    with engine.store.transaction() as db:
        config = engine.store.read(db, "strategy")
        config.update(enabled=True, revision="test-revision")
        engine.store.write(db, "strategy", config)


def test_automatic_buy_and_sell(engine, monkeypatch):
    enable(engine)
    signal = {"action": "buy", "valid_until": time.time() + 3600, "candle_timestamp": 100}
    monkeypatch.setattr("quantpilot.runner.analyze", lambda *_: signal)
    runner = Runner(engine)
    runner.tick()
    assert len(engine.store.recent("orders")) == 1
    assert engine.store.get("account")["positions"]
    cash = engine.store.get("account")["cash"]
    runner.tick()
    assert engine.store.get("account")["cash"] == cash
    signal["action"] = "sell"
    runner.tick()
    assert not engine.store.get("account")["positions"]
    assert len(engine.store.recent("orders")) == 2


def test_rejected_candle_not_submitted_repeatedly(engine, monkeypatch):
    enable(engine)
    engine.settings.max_exposure = "1"
    signal = {"action": "buy", "valid_until": time.time() + 3600, "candle_timestamp": 100}
    monkeypatch.setattr("quantpilot.runner.analyze", lambda *_: signal)
    runner = Runner(engine)
    runner.tick()
    runner.tick()
    assert len(engine.store.recent("orders")) == 1
    assert len([r for r in engine.store.recent("audit") if r["event"] == "idempotency_conflict"]) == 0


def test_expired_signal_and_sell_without_position_do_nothing(engine, monkeypatch):
    enable(engine)
    signal = {"action": "buy", "valid_until": 1, "candle_timestamp": 100}
    monkeypatch.setattr("quantpilot.runner.analyze", lambda *_: signal)
    runner = Runner(engine)
    runner.tick()
    signal.update(action="sell", valid_until=time.time()+3600)
    runner.tick()
    assert not engine.store.recent("orders")


def test_runner_error_logged_without_exception_secrets(engine, monkeypatch):
    runner = Runner(engine)

    def tick():
        runner.stop_event.set()
        raise RuntimeError("sensitive-vendor-details")

    monkeypatch.setattr(runner, "tick", tick)
    runner.loop()
    assert runner.last_error == "runner_failed"
    assert "sensitive-vendor-details" not in str(engine.store.recent("audit"))


def test_protection_stale_quote_keeps_position(engine, order):
    engine.execute(order)
    engine.market.price = Decimal("80")
    engine.market.age = 40
    assert not engine.protect()
    assert engine.store.get("account")["positions"]
    assert engine.store.recent("audit")[0]["event"] == "protection_unavailable"


def test_signal_buy_and_sell_conditions():
    rows = [[i * 3600000, 100 + i*.1, 101 + i*.1, 99 + i*.1, 100 + i*.1 + (-1)**i*.3, 100] for i in range(120)]
    assert analyze(rows, 3600)["action"] == "buy"
    for i, row in enumerate(rows):
        row[4] = 120 - i*.1
    assert analyze(rows, 3600)["action"] == "sell"


def test_strategy_network_error_no_orders(engine, monkeypatch):
    enable(engine)
    runner = Runner(engine)

    def failed(*_):
        runner.stop_event.set()
        raise MarketError("exchange_unavailable")

    monkeypatch.setattr(engine.market, "candles", failed)
    runner.loop()
    assert not engine.store.recent("orders")
    assert runner.last_error == "exchange_unavailable"
