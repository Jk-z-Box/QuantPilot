"""Exchange contract metadata. Never guess leverage from a symbol name."""

from __future__ import annotations

import math
import time


def contract_limits(client, exchange, symbol, notional=None, quantity=None):
    client.load_markets()
    market = client.market(symbol)
    if (
        not market.get("swap")
        or not market.get("linear")
        or market.get("settle") != "USDT"
        or market.get("active") is False
    ):
        raise ValueError("该交易所没有可用的 USDT 线性永续合约")
    size = float(market.get("contractSize") or 1)
    maximum = market.get("limits", {}).get("leverage", {}).get("max")
    tiers = []
    if maximum is None or notional is not None:
        try:
            if client.has.get("fetchMarketLeverageTiers"):
                tiers = client.fetch_market_leverage_tiers(symbol)
            elif client.has.get("fetchLeverageTiers"):
                tiers = client.fetch_leverage_tiers([symbol]).get(symbol, [])
        except Exception:
            if notional is not None:
                raise ValueError("无法核验当前仓位档位的杠杆上限，暂不下单") from None
        if tiers:
            # OKX's minSz/maxSz are contract counts, despite CCXT's Notional field names.
            exposure = quantity / size if exchange == "okx" and quantity is not None else notional
            valid = [
                t
                for t in tiers
                if exposure is None or float(t["minNotional"]) <= exposure <= float(t["maxNotional"])
            ]
            if not valid:
                raise ValueError("仓位超出已核验的交易所风险档位")
            maximum = (
                min(float(t["maxLeverage"]) for t in valid)
                if exposure is not None
                else max(float(t["maxLeverage"]) for t in valid)
            )
        elif notional is not None:
            raise ValueError("交易所未返回仓位杠杆档位，暂不下单")
    verified = maximum is not None and math.isfinite(float(maximum)) and float(maximum) >= 1
    precision = market.get("precision", {}).get("amount")
    step = None
    if precision is not None:
        step = (10 ** -int(precision) if client.precisionMode == 2 else float(precision)) * size
    minimum = market.get("limits", {}).get("amount", {}).get("min")
    return {
        "symbol": symbol,
        "exchange": exchange,
        "max_leverage": int(float(maximum)) if verified else 1,
        "verified": verified,
        "contract_size": size,
        "quantity_step": step,
        "min_quantity": float(minimum) * size if minimum is not None else None,
        "checked_at": time.time(),
        "note": "交易所公布上限；实际下单还会检查仓位档位及风险额度"
        if verified
        else "暂无法核验上限，仅允许 1 倍；币安通常需要已保存的账户 API 读取档位",
    }
