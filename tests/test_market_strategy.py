import time
from decimal import Decimal

import pytest

from quantpilot.market import Market, MarketError, Quote, completed_candles
from quantpilot.strategy import analyze, ema, rsi


def candles():
    span = 3600
    end = (int(time.time()) // span - 1) * span
    return [[(end - (119 - i) * span) * 1000, 100, 102, 98, 100, 100] for i in range(120)]


def test_incomplete_candle_excluded():
    rows = candles()
    current = [rows[-1][0] + 3600000, 100, 102, 98, 100, 100]
    assert len(completed_candles(rows + [current], "1h", time.time())) == 120


@pytest.mark.parametrize("mutation", ["gap", "duplicate", "nan", "negative", "stale", "short", "bad_ohlc"])
def test_bad_candles_rejected(mutation):
    rows = candles()
    if mutation == "gap":
        rows.pop(30)
    elif mutation == "duplicate":
        rows.insert(20, rows[20])
    elif mutation == "nan":
        rows[20][4] = float("nan")
    elif mutation == "negative":
        rows[20][5] = -1
    elif mutation == "stale":
        for row in rows:
            row[0] -= 86400000
    elif mutation == "short":
        rows = rows[:10]
    else:
        rows[20][2] = 1
    with pytest.raises(MarketError):
        completed_candles(rows, "1h", time.time())


def test_flat_indicators_do_not_divide_by_zero():
    assert ema([100] * 60, 20) == 100
    assert rsi([100] * 60) == 50
    assert rsi(list(range(1, 100))) == 100
    signal = analyze(candles(), 3600)
    assert signal["action"] == "hold"
    assert signal["valid_until"] > time.time()


def test_demo_explicit_and_live_data_never_falls_back(settings):
    settings.market_mode = "demo"
    market = Market(settings)
    assert market.quote("okx", "BTC/USDT").demo is True
    assert len(market.candles("okx", "BTC/USDT")) >= 60
    settings.market_mode = "public"
    market.factory = lambda *_: (_ for _ in ()).throw(RuntimeError("secret vendor error"))
    with pytest.raises(MarketError, match="exchange_unavailable"):
        market.quote("okx", "BTC/USDT")


def test_ccxt_spot_only_rate_limit_precision_and_account_isolation(settings):
    configs = []

    class Client:
        def load_markets(self):
            return None

        def market(self, symbol):
            return {"spot": True, "active": True, "limits": {"amount": {"min": .001}, "cost": {"min": 5}}}

        def amount_to_precision(self, symbol, quantity):
            return str(Decimal(quantity).quantize(Decimal("0.001"), rounding="ROUND_DOWN"))

        def fetch_order_book(self, symbol, limit):
            return {"bids": [[100, 5]], "asks": [[100.01, 5]], "timestamp": time.time() * 1000}

        def fetch_balance(self):
            return {"total": {"USDT": 100, "BTC": 0}}

    def factory(name, config):
        configs.append(config)
        return Client()

    class FakeVault:
        def get(self, name):
            return {"apiKey": "test-key", "secret": "test-secret"}

    market = Market(settings, FakeVault(), factory)
    quote = market.quote("okx", "BTC/USDT")
    quote.validate(settings, time.time())
    assert configs[0]["enableRateLimit"] and configs[0]["options"]["defaultType"] == "spot"
    assert "apiKey" not in configs[0]
    market.validate_amount("okx", "BTC/USDT", Decimal("1"), Decimal("100"))
    with pytest.raises(MarketError, match="amount_precision"):
        market.validate_amount("okx", "BTC/USDT", Decimal(".00001"), Decimal("100"))
    assert market.balance("okx") == {"USDT": "100"}
    assert configs[-1]["apiKey"] == "test-key"
    assert not hasattr(market, "create_order")


@pytest.mark.parametrize(("bid", "ask"), [(0, 1), (-1, 1), (2, 1)])
def test_invalid_orderbook(settings, bid, ask):
    quote = Quote("okx", "BTC/USDT", Decimal(bid), Decimal(ask), time.time(), time.time())
    with pytest.raises(MarketError, match="invalid_quote"):
        quote.validate(settings, time.time())
