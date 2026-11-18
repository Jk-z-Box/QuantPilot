from __future__ import annotations

import math
import threading
import time

import ccxt

from .contracts import client_name, client_options, settlement, validate_market
from .models import EXCHANGES, FRAMES


class FuturesMarket:
    def __init__(self, demo=False, factory=None):
        self.demo = demo
        self.factory = factory or (lambda name, config: getattr(ccxt, name)(config))
        self.clients = {}
        self.locks = {name: threading.RLock() for name in EXCHANGES}
        self.cache = {}
        self.streams = None
        self.catalog_checked = {}

    def client(self, exchange, settle="USDT"):
        if exchange not in EXCHANGES:
            raise ValueError("不支持的交易所")
        key = (exchange, settle == "USDT")
        if key not in self.clients:
            self.clients[key] = self.factory(
                client_name(exchange, settle),
                {
                    "enableRateLimit": True,
                    "timeout": 8000,
                    "options": client_options(settle, exchange),
                },
            )
        return self.clients[key]

    def instrument(self, client, symbol):
        client.load_markets()
        market = client.market(symbol)
        return validate_market(market)

    def markets(self, exchange, query="", kind="linear", settle="", offset=0, limit=50):
        with self.locks[exchange]:
            client = self.client(exchange, "USDT" if kind == "linear" else (settle or "BTC"))
            catalog_key = (exchange, kind)
            checked = self.catalog_checked.get(catalog_key)
            markets = (
                client.load_markets(reload=True)
                if checked and time.time() - checked > 600
                else client.load_markets()
            )
            self.catalog_checked[catalog_key] = time.time()
            found = []
            for m in markets.values():
                try:
                    validate_market(m)
                except ValueError:
                    continue
                if bool(m.get("inverse")) != (kind == "inverse") or (settle and m["settle"] != settle):
                    continue
                if query.upper().replace(" ", "") not in m["symbol"]:
                    continue
                found.append(
                    {
                        "symbol": m["symbol"],
                        "base": m["base"],
                        "settle": m["settle"],
                        "inverse": bool(m.get("inverse")),
                        "contract_size": m["contractSize"],
                        "min_contracts": m.get("limits", {}).get("amount", {}).get("min"),
                        "amount_step": m.get("precision", {}).get("amount"),
                    }
                )
            found.sort(key=lambda m: m["symbol"])
            return {
                "items": found[offset : offset + limit],
                "total": len(found),
                "offset": offset,
                "limit": limit,
                "exchange": exchange,
                "kind": kind,
            }

    def quote(self, item):
        if self.streams and not self.demo:
            streamed = self.streams.quote(item)
            if streamed:
                cached = self.cache.get((item["exchange"], item["symbol"], "derivatives"))
                return {**(cached[1] if cached else {}), **streamed}
        now = time.time()
        if self.demo:
            p = {"BTC": 68000, "ETH": 2600, "SOL": 160, "AVGO": 180, "SOXL": 40}.get(item["label"], 100)
            p *= 1 + math.sin(now / 80) * 0.002
            return {
                "mid": p,
                "bid": p * 0.9999,
                "ask": p * 1.0001,
                "timestamp": now,
                "received_at": now,
                "funding_rate": 0.0001,
                "open_interest": 100000,
                "mark": p,
                "source": "synthetic_demo",
            }
        name, symbol = item["exchange"], item["symbol"]
        with self.locks[name]:
            client = self.client(name, settlement(symbol))
            m = self.instrument(client, symbol)
            if name == "binance":
                book = (
                    client.dapiPublicGetTickerBookTicker
                    if m.get("inverse")
                    else client.fapiPublicGetTickerBookTicker
                )({"symbol": m["id"]})
                if isinstance(book, list):
                    book = book[0]
                bid, ask, stamp = float(book["bidPrice"]), float(book["askPrice"]), float(book["time"]) / 1000
            else:
                book = client.fetch_order_book(symbol, 5 if name == "okx" else 20)
                bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
                stamp = (book.get("timestamp") or 0) / 1000
            cached = (now, self.derivatives(item))
        if not all(math.isfinite(v) and v > 0 for v in (bid, ask)) or ask < bid:
            raise ValueError("交易所报价无效")
        return {
            "mid": (bid + ask) / 2,
            "bid": bid,
            "ask": ask,
            "timestamp": stamp,
            "received_at": time.time(),
            "source": "exchange",
            "inverse": bool(m.get("inverse")),
            "settle": m["settle"],
            "contract_size": m["contractSize"],
            "contract_step": (
                10 ** -int(m["precision"]["amount"])
                if client.precisionMode == 2
                else m["precision"]["amount"]
            ),
            "min_contracts": m.get("limits", {}).get("amount", {}).get("min"),
            **cached[1],
        }

    def candles(self, item, frame="1h", closed=False):
        if frame not in FRAMES:
            raise ValueError("周期不支持")
        now, span = time.time(), FRAMES[frame]
        key = (item["exchange"], item["symbol"], frame, closed)
        cached = self.cache.get(key)
        streamed = self.streams and not self.demo and self.streams.quote(item)
        if cached and now - cached[0] < (300 if streamed else 30 if closed else 4):
            rows = self.streams.merge_candles(item, frame, cached[1]) if streamed else cached[1]
            if all(rows[i][0] - rows[i - 1][0] == span * 1000 for i in range(1, len(rows))):
                return [r for r in rows if not closed or r[0] / 1000 + span <= now - 2]
        if self.demo:
            base = self.quote(item)["mid"]
            end = int(now) // span * span
            rows = []
            for i in range(180):
                c = base * (0.92 + i * 0.00045 + math.sin(i / 4) * 0.001)
                rows.append(
                    [
                        (end - (179 - i) * span) * 1000,
                        c * 0.999,
                        c * 1.003,
                        c * 0.997,
                        c,
                        100 + i + 20 * math.cos(i),
                    ]
                )
        else:
            with self.locks[item["exchange"]]:
                client = self.client(item["exchange"], settlement(item["symbol"]))
                self.instrument(client, item["symbol"])
                rows = client.fetch_ohlcv(item["symbol"], frame, limit=420)
        if self.streams and not self.demo:
            rows = self.streams.merge_candles(item, frame, rows)
        valid = []
        for row in rows:
            values = [float(v) for v in row[:6]]
            if len(values) != 6 or not all(math.isfinite(x) for x in values):
                raise ValueError("K 线数值无效")
            t, op, high, low, close, vol = values
            if (
                min(op, high, low, close) <= 0
                or vol < 0
                or not low <= min(op, close) <= max(op, close) <= high
            ):
                raise ValueError("K 线价格无效")
            if valid and t - valid[-1][0] != span * 1000:
                raise ValueError("K 线缺失或重复")
            if t / 1000 > now + 5:
                raise ValueError("K 线时间来自未来")
            if not closed or t / 1000 + span <= now - 2:
                valid.append(values)
        if not valid or now - (valid[-1][0] / 1000 + span) > span + 30:
            raise ValueError("K 线已过期")
        self.cache[key] = (now, valid)
        return valid

    def derivatives(self, item):
        if self.demo:
            return {}
        name, symbol = item["exchange"], item["symbol"]
        now = time.time()
        key = (name, symbol, "derivatives")
        cached = self.cache.get(key)
        if cached and now - cached[0] < 60:
            return cached[1]
        with self.locks[name]:
            client = self.client(name, settlement(symbol))
            self.instrument(client, symbol)
            funding, oi, mark = None, None, None
            try:
                f = client.fetch_funding_rate(symbol)
                funding, mark = f.get("fundingRate"), f.get("markPrice")
            except Exception:
                funding, mark = None, None
            try:
                o = (
                    client.fetch_open_interest_history(symbol, "5m", limit=2)[-1]
                    if name == "gate"
                    else client.fetch_open_interest(symbol)
                )
                if name == "gate" and not 0 <= time.time() - (o.get("timestamp") or 0) / 1000 <= 600:
                    raise ValueError("持仓量数据过期")
                oi = o.get("openInterestAmount")
            except Exception:
                oi = None
            result = {"funding_rate": funding, "open_interest": oi, "mark": mark, "context_at": now}
            self.cache[key] = (now, result)
            return result
