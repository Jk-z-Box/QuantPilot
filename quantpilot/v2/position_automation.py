"""Explicit per-pair loss additions and durable, one-shot trailing exits."""

import copy
import time

from .rules import effective


def settings(account, key, name):
    return next((s.get(name, {}) for s in account.get("sizing", []) if s["instrument_id"] == key), {})


def loss_add_ready(account, key, position, quote, opportunity, now=None):
    now = time.time() if now is None else now
    config = settings(account, key, "loss_add")
    if not config.get("enabled") or not quote or quote.get("error"):
        return False
    if not -5 <= now - quote.get("timestamp", 0) <= 15:
        return False
    op = opportunity or {}
    if not op.get("valid") or op.get("direction") != position["side"] or op.get("expires_at", 0) <= now:
        return False
    if not effective(op.get("parameters"))["add_enabled"]:
        return False
    sign = 1 if position["side"] == "long" else -1
    price = float(quote["ask"] if sign == 1 else quote["bid"])
    entry = float(position["entry"])
    loss = (entry - price) * sign / entry * 100
    return (
        config.get("trigger_pct", 1) <= loss <= config.get("max_loss_pct", 3)
        and now - position.get("last_add_at", position.get("opened_at", now)) >= config.get("cooldown", 300)
        and not position.get("tp_done")
        and not position.get("trailing", {}).get("done")
    )


def trail_state(position, quote, config, opportunity=None):
    """Price highs/lows, not leveraged ROE. A tightened trigger never moves backwards."""
    old = position.get("trailing", {})
    if not config.get("enabled") or old.get("done"):
        return old, False
    sign = 1 if position["side"] == "long" else -1
    price, entry = float(quote["bid"] if sign == 1 else quote["ask"]), float(position["entry"])
    peak = (
        max(float(old.get("peak", price)), price) if sign == 1 else min(float(old.get("peak", price)), price)
    )
    armed = old.get("armed", False) or (peak / entry - 1) * sign * 100 >= config.get("activation_pct", 1)
    stop = old.get("stop")
    if armed:
        if config.get("distance_mode", "percent") == "atr":
            op = opportunity or {}
            atr = (op.get("indicators", {}).get("1h") or {}).get("atr")
            # Keep an already committed trigger through missing/stale ATR, never invent one.
            distance = (
                float(atr) * config.get("atr_multiple", 2)
                if atr and op.get("expires_at", 0) > time.time()
                else None
            )
        else:
            distance = peak * config.get("distance_pct", 0.5) / 100
        if distance is not None:
            candidate = peak - sign * distance
            # Allow room for fees/slippage; the entry-average alone isn't net breakeven.
            if (candidate / entry - 1) * sign > 0.003:
                stop = (
                    candidate if stop is None else max(stop, candidate) if sign == 1 else min(stop, candidate)
                )
    new = {**old, "peak": peak, "armed": armed, "stop": stop}
    return new, bool(stop is not None and (price - stop) * sign <= 0)


def update_trailing(store, account, key, quote, opportunity=None):
    config = settings(account, key, "trailing_take_profit")
    if (
        not config.get("enabled")
        or not quote
        or quote.get("error")
        or not -5 <= time.time() - quote.get("timestamp", 0) <= 15
    ):
        return False, 1
    name = "v2_ledgers" if account["kind"] == "paper" else "v2_managed"
    with store.transaction() as db:
        # Account settings may have changed since the execution loop read them.
        current = store.read(db, "v2_accounts").get(account["id"])
        if current != account:
            return False, 1
        books = store.read(db, name)
        positions = books.get(account["id"], {})
        if name == "v2_ledgers":
            positions = positions.get("positions", {})
        position = positions.get(key)
        if not position:
            return False, 1
        before = copy.deepcopy(position.get("trailing", {}))
        after, triggered = trail_state(position, quote, config, opportunity)
        if before != after:
            position["trailing"] = after
            store.write(db, name, books)
            if before.get("stop") != after.get("stop"):
                from . import journal

                journal.event(
                    db,
                    account["id"],
                    key,
                    "trailing_update",
                    {"before": before, "after": after, "reason": "有利价格移动；保护线只收紧"},
                )
        return triggered, config.get("exit_ratio", 1)
