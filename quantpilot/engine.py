from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from decimal import Decimal, DefaultContext, getcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .market import MarketError, decimal

ZERO = Decimal("0")
DefaultContext.prec = 50
getcontext().prec = 50


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    exchange: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: Decimal = Field(gt=0, le=100000000, max_digits=28, decimal_places=12)
    limit_price: Decimal = Field(gt=0, le=1000000000, max_digits=28, decimal_places=12)
    expires_at: float
    stop_price: Decimal | None = Field(default=None, gt=0, le=1000000000)
    take_price: Decimal | None = Field(default=None, gt=0, le=1000000000)
    mode: Literal["paper", "live"] = "paper"

    @field_validator("exchange", "symbol")
    @classmethod
    def bounded(cls, value):
        if len(value) > 32:
            raise ValueError("invalid_market")
        return value


class Engine:
    def __init__(self, settings, store, market):
        self.settings, self.store, self.market = settings, store, market

    def _identity(self, request):
        return hashlib.sha256(request.model_dump_json().encode()).hexdigest()

    def _prior(self, db, request, fingerprint):
        row = db.execute("SELECT fingerprint,result FROM orders WHERE id=?", (request.id,)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                self.store.audit(db, "idempotency_conflict", {"id": request.id})
                return {"id": request.id, "status": "rejected", "code": "idempotency_conflict"}
            return json.loads(row["result"])
        return None

    def _save(self, db, request, fingerprint, result):
        db.execute("INSERT INTO orders VALUES (?,?,?,?)", (request.id, fingerprint, json.dumps(result), time.time()))
        self.store.audit(db, "paper_order", {"id": request.id, "status": result["status"],
                                           "code": result.get("code", "filled"), "source": result.get("source", "manual")})
        return result

    def _reject(self, request, fingerprint, code):
        with self.store.transaction() as db:
            return self._prior(db, request, fingerprint) or self._save(db, request, fingerprint, {
                "id": request.id, "status": "rejected", "code": code,
            })

    def marks(self, positions, extra=None):
        marks = dict(extra or {})
        for key in positions:
            if key not in marks:
                exchange, symbol = key.split(":", 1)
                marks[key] = self.market.quote(exchange, symbol)
        return marks

    def valuation(self, account, marks, now):
        exposure = ZERO
        for key, position in account["positions"].items():
            if key not in marks:
                raise MarketError("portfolio_changed_retry")
            marks[key].validate(self.settings, now)
            exposure += decimal(position["quantity"]) * marks[key].bid
        return decimal(account["cash"]) + exposure, exposure

    @staticmethod
    def roll_day(account, equity, now):
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        if account["day"] != day:
            account["day"], account["day_equity"] = day, str(equity)

    def snapshot(self):
        account = self.store.get("account")
        try:
            marks = self.marks(account["positions"])
            with self.store.transaction() as db:
                account = self.store.read(db, "account")
                equity, exposure = self.valuation(account, marks, time.time())
                self.roll_day(account, equity, time.time())
                self.store.write(db, "account", account)
            return {**account, "equity": str(equity), "exposure": str(exposure),
                    "day_pnl": str(equity - decimal(account["day_equity"])), "valuation_valid": True,
                    "marks": {key: quote.public() for key, quote in marks.items()}}
        except MarketError as exc:
            return {**account, "equity": None, "exposure": None, "day_pnl": None,
                    "valuation_valid": False, "valuation_error": str(exc), "marks": {}}

    def execute(self, request: OrderRequest, *, source="manual", strategy_revision=None):
        fingerprint = self._identity(request)
        with self.store.connect() as db:
            prior = self._prior(db, request, fingerprint)
        if prior:
            return prior
        if request.mode != "paper" or self.settings.live_enabled:
            return self._reject(request, fingerprint, "live_trading_locked")
        try:
            self.market.check(request.exchange, request.symbol)
            quote = self.market.quote(request.exchange, request.symbol)
            now = time.time()
            quote.validate(self.settings, now)
            slip = decimal(self.settings.slippage_bps) / 10000
            price = quote.ask * (1 + slip) if request.side == "buy" else quote.bid * (1 - slip)
            self.market.validate_amount(request.exchange, request.symbol, request.quantity, price)
            key = request.exchange + ":" + request.symbol
            marks = self.marks(self.store.get("account")["positions"], {key: quote}) if request.side == "buy" else {key: quote}
        except MarketError as exc:
            return self._reject(request, fingerprint, str(exc))
        with self.store.transaction() as db:
            prior = self._prior(db, request, fingerprint)
            if prior:
                return prior
            try:
                now = time.time()
                quote.validate(self.settings, now)
                if not now < request.expires_at <= now + 60:
                    raise MarketError("order_expired_or_invalid_ttl")
                if strategy_revision is not None:
                    strategy = self.store.read(db, "strategy")
                    if not strategy["enabled"] or strategy.get("revision") != strategy_revision:
                        raise MarketError("strategy_stopped_or_changed")
                if request.side == "buy" and self.store.read(db, "kill"):
                    raise MarketError("kill_switch_active")
                if (request.side == "buy" and price > request.limit_price) or (
                    request.side == "sell" and price < request.limit_price
                ):
                    raise MarketError("ioc_limit_not_marketable")
                notional = request.quantity * price
                fee = notional * decimal(self.settings.fee_bps) / 10000
                if notional > decimal(self.settings.max_order) and source != "protective_exit":
                    raise MarketError("max_order_exceeded")
                account = self.store.read(db, "account")
                cash = decimal(account["cash"])
                current = account["positions"].get(key)
                quantity = decimal(current["quantity"]) if current else ZERO
                cost = decimal(current["cost"]) if current else ZERO
                if request.side == "buy":
                    equity, exposure = self.valuation(account, marks, now)
                    self.roll_day(account, equity, now)
                    if decimal(account["day_equity"]) - equity >= decimal(self.settings.daily_loss_limit):
                        raise MarketError("daily_loss_limit")
                    if not request.stop_price or not request.take_price or not request.stop_price < price < request.take_price:
                        raise MarketError("invalid_protection_prices")
                    if current and request.stop_price < decimal(current["stop_price"]):
                        raise MarketError("cannot_loosen_stop")
                    stop_proceeds = (quantity + request.quantity) * request.stop_price * (1 - decimal(self.settings.fee_bps) / 10000)
                    if cost + notional + fee - stop_proceeds > decimal(self.settings.max_trade_loss):
                        raise MarketError("trade_loss_limit")
                    if cash < notional + fee:
                        raise MarketError("insufficient_cash")
                    if exposure + notional > decimal(self.settings.max_exposure):
                        raise MarketError("max_exposure_exceeded")
                    if quantity * quote.bid + notional > decimal(self.settings.max_position):
                        raise MarketError("max_position_exceeded")
                    # Projected fill costs count toward the same daily loss budget.
                    projected_equity = equity - notional - fee + request.quantity * quote.bid
                    if decimal(account["day_equity"]) - projected_equity >= decimal(self.settings.daily_loss_limit):
                        raise MarketError("daily_loss_limit")
                    account["cash"] = str(cash - notional - fee)
                    account["positions"][key] = {"quantity": str(quantity + request.quantity), "cost": str(cost + notional + fee),
                        "stop_price": str(request.stop_price), "take_price": str(request.take_price)}
                else:
                    if request.quantity > quantity:
                        raise MarketError("insufficient_position")
                    removed_cost = cost * request.quantity / quantity
                    account["cash"] = str(cash + notional - fee)
                    account["realized_pnl"] = str(decimal(account["realized_pnl"]) + notional - fee - removed_cost)
                    if request.quantity == quantity:
                        del account["positions"][key]
                    else:
                        current["quantity"], current["cost"] = str(quantity - request.quantity), str(cost - removed_cost)
                account["fees"] = str(decimal(account["fees"]) + fee)
                self.store.write(db, "account", account)
                result = {"id": request.id, "status": "filled", "mode": "paper", "source": source,
                          "exchange": request.exchange, "symbol": request.symbol, "side": request.side,
                          "quantity": str(request.quantity), "price": str(price), "fee": str(fee),
                          "timestamp": now, "demo": quote.demo}
            except MarketError as exc:
                result = {"id": request.id, "status": "rejected", "code": str(exc), "source": source}
            return self._save(db, request, fingerprint, result)

    def protect(self):
        results = []
        for key, position in self.store.get("account")["positions"].items():
            exchange, symbol = key.split(":", 1)
            try:
                quote = self.market.quote(exchange, symbol)
                quote.validate(self.settings, time.time())
            except MarketError as exc:
                # Isolate markets so one outage cannot starve protection for other positions.
                self.store.record("protection_unavailable", {"market": key, "code": str(exc)})
                continue
            if quote.bid <= decimal(position["stop_price"]) or quote.bid >= decimal(position["take_price"]):
                identifier = hashlib.sha256((key + json.dumps(position, sort_keys=True) + str(int(time.time() / 15))).encode()).hexdigest()
                request = OrderRequest(id="protect_" + identifier, exchange=exchange, symbol=symbol, side="sell",
                                       quantity=position["quantity"], limit_price=(quote.bid * Decimal("0.98")).quantize(Decimal("0.00000001")),
                                       expires_at=time.time() + 30)
                results.append(self.execute(request, source="protective_exit"))
        return results
