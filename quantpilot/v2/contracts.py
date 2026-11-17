"""Native settlement arithmetic for linear USDT and inverse coin perpetuals.

`qty` in stored positions is base amount at entry. For inverse contracts the
immutable face value is qty * entry; current base exposure is face / mark.
All money returned here is denominated in the settlement currency.
"""

from ..market import decimal as D


def settlement(symbol):
    return symbol.rsplit(":", 1)[-1] if ":" in symbol else "USDT"


def inverse(value):
    if isinstance(value.get("inverse"), bool):
        return bool(value["inverse"])
    return settlement(value.get("symbol", value.get("logic_symbol", ""))) != "USDT"


def client_name(exchange, settle="USDT"):
    return ("binanceusdm" if settle == "USDT" else "binancecoinm") if exchange == "binance" else exchange


def client_options(settle="USDT"):
    return {
        "defaultType": "swap",
        "defaultSubType": "linear" if settle == "USDT" else "inverse",
        "defaultSettle": settle,
    }


def validate_market(m):
    linear = m.get("linear") and m.get("settle") == "USDT"
    coin = m.get("inverse") and m.get("quote") == "USD" and m.get("settle") == m.get("base")
    if not m.get("swap") or m.get("active") is False or not (linear or coin):
        raise ValueError("仅支持已上市的 USDT 线性永续或以标的币结算的 USD 反向永续")
    if D(m.get("contractSize") or 0) <= 0:
        raise ValueError("缺少有效合约面值")
    return m


def native_notional(p, price):
    return D(p["qty"]) * (D(p["entry"]) / D(price) if inverse(p) else D(price))


def pnl(p, price, qty=None):
    q = D(p["qty"] if qty is None else qty)
    entry, price = D(p["entry"]), D(price)
    sign = 1 if p["side"] == "long" else -1
    return q * (1 - entry / price if inverse(p) else price - entry) * sign


def base_amount(m, contracts, price):
    value = D(contracts) * D(m["contractSize"])
    return value / D(price) if inverse(m) else value


def contract_amount(m, base, price):
    return D(base) * (D(price) if inverse(m) else 1) / D(m["contractSize"])


def entry_position(qty, price, side, is_inverse=False):
    return {"qty": str(qty), "entry": str(price), "side": side, "inverse": is_inverse}


def risk_reward(symbol, entry, stop, targets, side):
    """Estimated native-settlement return/risk for one entry-time base coin."""
    coin = settlement(symbol) != "USDT"
    p = entry_position(1, entry, side, coin)
    loss = -pnl(p, stop)
    reward = sum(pnl(p, t["price"]) * D(t["ratio"]) for t in targets if t.get("enabled", True))
    unit = D(1) if coin else D(entry)
    # Charge exit fees at each projected exit value (inverse exposure changes with price).
    stop_cost = unit * D(".0007") + native_notional(p, stop) * D(".0007")
    reward_cost = unit * D(".0007") + sum(
        native_notional(p, t["price"]) * D(t["ratio"]) * D(".0007") for t in targets if t.get("enabled", True)
    )
    return float((reward - reward_cost) / (loss + stop_cost)) if loss + stop_cost > 0 else 0
