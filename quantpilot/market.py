from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass
from decimal import Decimal

import ccxt

from .config import EXCHANGES, Settings

TIMEFRAMES = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class MarketError(ValueError):
    pass


def decimal(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise MarketError("invalid_number") from exc
    if not result.is_finite():
        raise MarketError("invalid_number")
    return result


@dataclass(frozen=True)
class Quote:
    exchange: str
    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp: float | None
    received_at: float
    demo: bool = False

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    def validate(self, settings: Settings, now: float):
        if not all(v.is_finite() and v > 0 for v in (self.bid, self.ask)) or self.ask < self.bid:
            raise MarketError("invalid_quote")
        if self.timestamp is None or not math.isfinite(self.timestamp):
            raise MarketError("unverified_quote_timestamp")
        if not -5 <= now - self.timestamp <= settings.max_quote_age:
            raise MarketError("stale_quote")
        if not 0 <= now - self.received_at <= settings.max_quote_age:
            raise MarketError("stale_quote")
        if (self.ask - self.bid) / self.mid * 10000 > settings.max_spread_bps:
            raise MarketError("spread_too_wide")

    def public(self):
        return {"exchange": self.exchange, "symbol": self.symbol, "bid": str(self.bid), "ask": str(self.ask),
                "mid": str(self.mid), "timestamp": self.timestamp, "received_at": self.received_at,
                "source": "synthetic_demo" if self.demo else "exchange_order_book",
                "timestamp_verified": self.timestamp is not None}


def completed_candles(rows, timeframe: str, now: float):
    duration = TIMEFRAMES[timeframe]
    result = []
    for row in rows:
        if len(row) < 6:
            raise MarketError("invalid_candle")
        values = [float(v) for v in row[:6]]
        if not all(math.isfinite(v) for v in values):
            raise MarketError("invalid_candle")
        ts, op, high, low, close, volume = values
        if ts <= 0 or min(op, high, low, close) <= 0 or volume < 0 or not low <= min(op, close) <= max(op, close) <= high:
            raise MarketError("invalid_candle")
        if ts / 1000 + duration > now - 2:
            continue
        if result and ts - result[-1][0] != duration * 1000:
            raise MarketError("candle_gap_or_duplicate")
        result.append(values)
    if len(result) < 60:
        raise MarketError("insufficient_candles")
    if now - (result[-1][0] / 1000 + duration) > duration + 30:
        raise MarketError("stale_candles")
    return result


class Market:
    """Allowlisted spot market data and read-only account access. No order transport."""

    def __init__(self, settings: Settings, vault=None, factory=None):
        self.settings, self.vault = settings, vault
        self.factory = factory or (lambda name, config: getattr(ccxt, name)(config))
        self.clients = {}
        self.private_clients = {}
        self.locks = {name: threading.RLock() for name in EXCHANGES}

    def check(self, exchange, symbol=None):
        if exchange not in EXCHANGES or (symbol is not None and symbol not in self.settings.symbols):
            raise MarketError("market_not_allowlisted")

    def client(self, exchange):
        if exchange not in self.clients:
            self.clients[exchange] = self.factory(exchange, {
                "enableRateLimit": True, "timeout": 10000,
                "options": {"defaultType": "spot"},
            })
        return self.clients[exchange]

    def _instrument(self, client, symbol):
        client.load_markets()
        instrument = client.market(symbol)
        if not instrument.get("spot") or instrument.get("active") is False:
            raise MarketError("inactive_or_non_spot_market")
        return instrument

    def quote(self, exchange, symbol):
        self.check(exchange, symbol)
        if self.settings.market_mode == "demo":
            base = {"BTC": 68000, "ETH": 2600, "SOL": 160, "BNB": 600, "XRP": 0.6}.get(symbol.split("/")[0], 100)
            price = decimal(base) * decimal(1 + math.sin(time.time() / 120) * 0.002)
            now = time.time()
            return Quote(exchange, symbol, price * Decimal("0.9998"), price * Decimal("1.0002"), now, now, True)
        try:
            with self.locks[exchange]:
                client = self.client(exchange)
                self._instrument(client, symbol)
                book = client.fetch_order_book(symbol, 20)
            now = time.time()
            timestamp = float(book["timestamp"]) / 1000 if book.get("timestamp") else None
            return Quote(exchange, symbol, decimal(book["bids"][0][0]), decimal(book["asks"][0][0]), timestamp, now)
        except MarketError:
            raise
        except Exception as exc:
            raise MarketError("exchange_unavailable") from exc

    def candles(self, exchange, symbol, timeframe="1h"):
        self.check(exchange, symbol)
        if timeframe not in TIMEFRAMES:
            raise MarketError("unsupported_timeframe")
        now = time.time()
        if self.settings.market_mode == "demo":
            span = TIMEFRAMES[timeframe]
            end = (int(now) // span - 1) * span
            base = float(self.quote(exchange, symbol).mid)
            rows = []
            for i in range(120):
                close = base * (0.95 + i * 0.0004 + math.sin(i / 5) * 0.003)
                rows.append([(end - (119 - i) * span) * 1000, close * .999, close * 1.003, close * .997, close, 100 + i])
        else:
            try:
                with self.locks[exchange]:
                    client = self.client(exchange)
                    self._instrument(client, symbol)
                    rows = client.fetch_ohlcv(symbol, timeframe, limit=120)
            except MarketError:
                raise
            except Exception as exc:
                raise MarketError("exchange_unavailable") from exc
        return completed_candles(rows, timeframe, now)

    def validate_amount(self, exchange, symbol, amount: Decimal, price: Decimal):
        self.check(exchange, symbol)
        if self.settings.market_mode == "demo":
            if amount * price < Decimal("5"):
                raise MarketError("below_min_cost")
            return
        try:
            with self.locks[exchange]:
                client = self.client(exchange)
                instrument = self._instrument(client, symbol)
                precision_amount = decimal(client.amount_to_precision(symbol, str(amount)))
                if precision_amount != amount or precision_amount <= 0:
                    raise MarketError("amount_precision")
                for category, value in (("amount", amount), ("cost", amount * price)):
                    limits = instrument.get("limits", {}).get(category, {})
                    if limits.get("min") is not None and value < decimal(limits["min"]):
                        raise MarketError("below_min_" + category)
                    if limits.get("max") is not None and value > decimal(limits["max"]):
                        raise MarketError("above_max_" + category)
        except MarketError:
            raise
        except Exception as exc:
            raise MarketError("exchange_unavailable") from exc

    def budget_amount(self, exchange, symbol, budget: Decimal, price: Decimal):
        amount = budget / price
        if self.settings.market_mode == "demo":
            return amount.quantize(Decimal("0.00000001"), rounding="ROUND_DOWN")
        try:
            with self.locks[exchange]:
                client = self.client(exchange)
                self._instrument(client, symbol)
                return decimal(client.amount_to_precision(symbol, str(amount)))
        except Exception as exc:
            raise MarketError("exchange_unavailable") from exc

    def balance(self, exchange):
        self.check(exchange)
        credentials = self.vault.get(exchange) if self.vault else None
        if not credentials:
            raise MarketError("credentials_missing")
        # A separate client prevents credentials from entering public data requests.
        try:
            with self.locks[exchange]:
                fingerprint = hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).hexdigest()
                cached = self.private_clients.get(exchange)
                if not cached or cached[0] != fingerprint:
                    client = self.factory(exchange, {"enableRateLimit": True, "timeout": 10000,
                        "options": {"defaultType": "spot"}, **credentials})
                    self.private_clients[exchange] = (fingerprint, client)
                client = self.private_clients[exchange][1]
                balance = client.fetch_balance()
                return {asset: str(decimal(value)) for asset, value in balance.get("total", {}).items() if value}
        except Exception as exc:
            raise MarketError("account_read_failed") from exc
