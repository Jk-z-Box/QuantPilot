"""Transparent native-settlement paper margin model, not an exchange risk-tier replica."""

from ..market import decimal as D
from .contracts import inverse, native_notional, pnl

MAINTENANCE_RATE = D(".005")
CLOSE_FEE_RATE = D(".0005")


def mode(position):
    # All positions created before margin selection were isolated.
    return position.get("margin_mode", "isolated")


def liquidation(position, collateral):
    qty, entry = D(position["qty"]), D(position["entry"])
    sign = 1 if position["side"] == "long" else -1
    rate = MAINTENANCE_RATE + CLOSE_FEE_RATE
    if inverse(position):
        denominator = D(collateral) + sign * qty
        value = qty * entry * (sign + rate) / denominator if denominator else D(0)
    else:
        value = (qty * entry - sign * collateral) / (qty * (1 - sign * rate))
    # No positive-price threshold is different from missing/stale market data.
    return value if value > 0 else None


def estimates(book, marks):
    positions = book["positions"]
    isolated = sum(D(p["margin"]) for p in positions.values() if mode(p) == "isolated")
    cross = {k: p for k, p in positions.items() if mode(p) == "cross"}
    pnls = {k: pnl(p, marks[k]) for k, p in cross.items()}
    maintenance = {
        k: native_notional(p, marks[k]) * (MAINTENANCE_RATE + CLOSE_FEE_RATE) for k, p in cross.items()
    }
    pool = D(book["cash"]) - isolated + sum(pnls.values()) - sum(maintenance.values())
    return {
        k: liquidation(p, pool - pnls[k] + maintenance[k] if mode(p) == "cross" else D(p["margin"]))
        for k, p in positions.items()
    }
