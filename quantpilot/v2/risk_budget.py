"""Shared, settlement-currency budget calculations for UI, sizing and AI."""

from decimal import Decimal as D

from .contracts import native_notional, pnl
from .risk_policy import LIMITS, policy, value


class BudgetExceeded(ValueError):
    def __init__(self, rows, requested, currency, prefix="可用仓位额度不足"):
        self.details = {"currency": currency, "requested_notional": str(requested), "limits": rows}
        blockers = [r for r in rows if D(r["order_headroom"]) < requested]
        super().__init__(
            prefix
            + "："
            + "；".join(
                f"{r['label']}：本次需要 {requested:.8g} {currency} 名义额，允许新增 {max(D(0), D(r['order_headroom'])):.8g} {currency}（已用 {r['used']} / 限额 {r['limit']}）"
                for r in blockers
            )
            or "可用仓位额度不足"
        )


def usage(book, snap, quotes, instruments):
    groups = {}
    loss = D(0)
    for key, p in book["positions"].items():
        notional = abs(native_notional(p, quotes.get(key, {}).get("mid", p["entry"])))
        group = instruments.get(key, {}).get("group", "unknown")
        groups[group] = groups.get(group, D(0)) + notional
        # Fees are part of the budget, including when a stop is around breakeven.
        loss += max(D(0), -pnl(p, p["stop"]) + notional * D(".0014"))
    return {
        "groups": groups,
        "portfolio_loss": loss,
        "margin": D(snap["margin"]),
        "exposure": D(snap["exposure"]),
    }


def summary(account, book, snap, quotes, instruments):
    equity = D(snap["equity"])
    used = usage(book, snap, quotes, instruments)
    day_loss = max(D(0), D(book["day_equity"]) - equity)
    rows = []
    for key, limit in policy(account)["limits"].items():
        base = D(book["day_equity"]) if key == "daily_loss_pct" else equity
        amount = base * D(str(limit["value"])) / (100 if key.endswith("pct") else 1)
        spent = {
            "trade_loss_pct": D(0),
            "margin_pct": used["margin"],
            "exposure_multiple": used["exposure"],
            "portfolio_loss_pct": used["portfolio_loss"],
            "daily_loss_pct": day_loss,
            "group_multiple": max(used["groups"].values(), default=D(0)),
        }.get(key)
        rows.append(
            {
                "key": key,
                "label": LIMITS[key][0],
                **limit,
                "limit": str(amount) if spent is not None else None,
                "used": str(spent) if spent is not None else None,
                "remaining": str(amount - spent) if spent is not None and limit["enabled"] else None,
            }
        )
    return {
        "currency": account.get("settle", "USDT"),
        "equity": str(equity),
        "available": snap["available"],
        "valuation_valid": snap["valuation_valid"],
        "limits": rows,
        "group_exposure": {k: str(v) for k, v in used["groups"].items()},
    }


def capacity(account, sizing, book, snap, quotes, instruments, group, leverage, risk_fraction):
    equity, free = D(snap["equity"]), D(snap["available"])
    used = usage(book, snap, quotes, instruments)
    correlated = used["groups"].get(group, D(0))
    margin_fraction = D(str(sizing.get("allocation", {}).get("available_margin_pct", 95))) / 100
    cost = 1 / D(leverage) + D(".0005")
    rows = [
        {
            "key": "available",
            "label": "可用资金分配（含手续费）",
            "used": "0",
            "limit": str(free * margin_fraction),
            "order_headroom": str(free * margin_fraction / cost),
        }
    ]
    specs = {
        "margin_pct": (used["margin"], cost),
        "exposure_multiple": (used["exposure"], D(1)),
        "group_multiple": (correlated, D(1)),
        "trade_loss_pct": (D(0), risk_fraction),
        "portfolio_loss_pct": (used["portfolio_loss"], risk_fraction),
    }
    for key, (spent, divisor) in specs.items():
        limit = value(account, key, sizing)
        if limit is None:
            continue
        amount = equity * limit / (100 if key.endswith("pct") else 1)
        rows.append(
            {
                "key": key,
                "label": LIMITS[key][0],
                "used": str(spent),
                "limit": str(amount),
                "order_headroom": str((amount - spent) / divisor),
            }
        )
    return max(D(0), min(D(r["order_headroom"]) for r in rows)), rows, correlated, margin_fraction
