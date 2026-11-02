"""Verified account valuation and explicit, approximate pre-order liquidation data."""

import copy
import time

from .accounts import size_order, valuation


def account_context(w, account, raw, key, quote, opportunity, action):
    now = time.time()
    instruments = {i["id"]: i for i in w.store.get("v2_watchlist")}
    quotes = {**copy.deepcopy(w.quotes), key: quote}
    if account["kind"] == "paper":
        book = copy.deepcopy(raw)
        checked = now
        source = "local_paper_ledger_and_fresh_quotes"
    else:
        checked = raw.get("checked_at", 0)
        if raw.get("error") or not -5 <= now - checked <= 30:
            raise ValueError("账户资金快照缺失或已过期，暂停新增风险")
        if raw.get("equity") is None or raw.get("available") is None or "positions" not in raw:
            raise ValueError("交易所权益、可用资金或持仓数据不完整")
        book = w.exchange.risk_book(account, raw, instruments)
        source = "exchange_account_snapshot"
    snap = valuation(book, quotes, now)
    if not snap["valuation_valid"]:
        raise ValueError("持仓报价不完整或已过期，无法完成账户风险评估")
    data = {
        "account_balance": {
            k: snap[k] for k in ("cash", "equity", "available", "margin", "unrealized", "exposure")
        },
        "balance_source": source,
        "balance_checked_at": checked,
        "portfolio_positions": snap["positions"],
        "portfolio_complete": True,
        "portfolio_empty": not snap["positions"],
        "position": next((p for p in snap["positions"] if p["instrument_id"] == key), None),
        "liquidation_model_note": "模拟及拟开仓强平为 USDT 线性合约估算，维持保证金率 0.5%、平仓费率 0.05%；全仓估算固定其他仓位价格。不是交易所风险档位认证。null 且 no_positive_price 表示正价格范围内无估算强平线，不表示未计算。已有交易所仓位使用交易所实际强平价。",
    }
    if account["kind"] != "paper":
        data["exchange_reported_balance"] = {k: raw[k] for k in ("equity", "available")}
        data["exchange_positions"] = raw["positions"]
    if action in ("open", "add"):
        prefs = w.store.get("v2_preferences")
        order = size_order(
            account,
            book,
            opportunity,
            quotes,
            instruments,
            prefs["profile"],
            now,
            action == "add",
            prefs["min_score"],
        )
        data["proposed_order"] = {
            k: (str(v) if k in ("qty", "notional", "price", "stop") else v) for k, v in order.items()
        }
    return data
