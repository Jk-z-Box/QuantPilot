from __future__ import annotations

import hashlib
import threading
import time

from .engine import OrderRequest
from .market import TIMEFRAMES, MarketError, decimal
from .strategy import analyze


class Runner:
    def __init__(self, engine):
        self.engine = engine
        self.store, self.market = engine.store, engine.market
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = None

    def tick(self):
        self.engine.protect()
        config = self.store.get("strategy")
        if not config["enabled"] or self.store.get("kill"):
            return
        exchange, symbol, timeframe = config["exchange"], config["symbol"], config["timeframe"]
        signal = analyze(self.market.candles(exchange, symbol, timeframe), TIMEFRAMES[timeframe])
        if signal["action"] == "hold" or signal["valid_until"] < time.time():
            return
        quote = self.market.quote(exchange, symbol)
        quote.validate(self.engine.settings, time.time())
        key = exchange + ":" + symbol
        position = self.store.get("account")["positions"].get(key)
        if signal["action"] == "buy":
            # One position per strategy market; pyramiding is deliberately disabled.
            if position:
                return
            quantity = self.market.budget_amount(exchange, symbol, decimal(config["budget"]), quote.ask * decimal("1.001"))
        else:
            if not position:
                return
            quantity = decimal(position["quantity"])
        # Persisted order IDs deduplicate a completed candle even across process restarts/config revisions.
        identity = f"{exchange}:{symbol}:{timeframe}:{signal['candle_timestamp']}:{signal['action']}"
        order_id = "strategy_" + hashlib.sha256(identity.encode()).hexdigest()
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone():
                return
        request = OrderRequest(id=order_id,
            exchange=exchange, symbol=symbol, side=signal["action"], quantity=quantity,
            limit_price=(quote.ask * decimal("1.005") if signal["action"] == "buy" else quote.bid * decimal("0.995")).quantize(decimal("0.00000001")),
            expires_at=time.time() + 30,
            stop_price=quote.mid * (1 - decimal(config["stop_pct"])),
            take_price=quote.mid * (1 + decimal(config["take_pct"])))
        self.engine.execute(request, source="strategy", strategy_revision=config.get("revision"))

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as exc:
                # Never log an exchange exception: it may contain signed headers.
                code = str(exc) if isinstance(exc, MarketError) else "runner_failed"
                if code != self.last_error:
                    self.store.record("runner_error", {"code": code})
                    self.last_error = code
            self.stop_event.wait(15)

    def start(self):
        # Automatic strategy entries require explicit reactivation after restart.
        with self.store.transaction() as db:
            config = self.store.read(db, "strategy")
            config["enabled"] = False
            self.store.write(db, "strategy", config)
            self.store.audit(db, "startup", {"mode": "paper", "strategy_enabled": False})
        self.thread = threading.Thread(target=self.loop, name="paper-runner", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=45)
