"""Single schema for executable strategy controls and the editable web form."""

from pydantic import Field, create_model, model_validator

from .models import Model

# default, lower, upper, group, label. Disabling a numeric override restores its baseline;
# indicator/action switches independently remove that rule from evaluation.
NUMERIC = {
    "ema_fast": (20, 2, 100, "指标", "EMA 快线周期"),
    "ema_slow": (50, 3, 300, "指标", "EMA 慢线周期"),
    "rsi_period": (14, 2, 60, "指标", "RSI 周期"),
    "macd_fast": (12, 2, 50, "指标", "MACD 快线"),
    "macd_slow": (26, 3, 100, "指标", "MACD 慢线"),
    "macd_signal": (9, 2, 50, "指标", "MACD 信号周期"),
    "atr_period": (14, 2, 60, "指标", "ATR 周期"),
    "volume_lookback": (20, 2, 200, "指标", "成交量均值周期"),
    "volatility_lookback": (30, 2, 200, "指标", "波动率周期"),
    "rsi_long_min": (43.0, 0.0, 100.0, "指标", "做多 RSI 下限"),
    "rsi_long_max": (70.0, 0.0, 100.0, "指标", "做多 RSI 上限"),
    "rsi_short_min": (30.0, 0.0, 100.0, "指标", "做空 RSI 下限"),
    "rsi_short_max": (57.0, 0.0, 100.0, "指标", "做空 RSI 上限"),
    "volume_multiplier": (1.0, 0.1, 5.0, "指标", "成交量门槛乘数"),
    "funding_limit": (0.0003, 0.0, 0.05, "指标", "资金费率上限（小数）"),
    "atr_score_min": (0.001, 0.0, 0.5, "指标", "ATR 比率评分下限"),
    "atr_score_max": (0.06, 0.001, 0.5, "指标", "ATR 比率评分上限"),
    "max_atr_pct": (0.10, 0.005, 0.5, "指标", "最大 ATR 比率"),
    "weight_trend": (24.0, 0.0, 100.0, "权重与周期", "趋势权重"),
    "weight_rsi": (9.0, 0.0, 100.0, "权重与周期", "RSI 权重"),
    "weight_macd": (9.0, 0.0, 100.0, "权重与周期", "MACD 权重"),
    "weight_volume": (12.0, 0.0, 100.0, "权重与周期", "成交量权重"),
    "weight_volatility": (6.0, 0.0, 100.0, "权重与周期", "波动率权重"),
    "weight_funding": (6.0, 0.0, 100.0, "权重与周期", "资金费率权重"),
    "weight_oi": (4.0, 0.0, 100.0, "权重与周期", "持仓量权重"),
    "weight_options": (8.0, 0.0, 100.0, "权重与周期", "期权权重"),
    "weight_etf": (6.0, 0.0, 100.0, "权重与周期", "资金流权重"),
    "weight_liquidation": (8.0, 0.0, 100.0, "权重与周期", "清算权重"),
    "weight_macro": (8.0, 0.0, 100.0, "权重与周期", "宏观权重"),
    "frame_1w_weight": (1.0, 0.0, 10.0, "权重与周期", "周线趋势权重"),
    "frame_1d_weight": (1.0, 0.0, 10.0, "权重与周期", "日线趋势权重"),
    "frame_4h_weight": (1.0, 0.0, 10.0, "权重与周期", "4H 趋势权重"),
    "frame_1h_weight": (1.0, 0.0, 10.0, "权重与周期", "1H 趋势权重"),
    "atr_stop": (2.0, 0.2, 10.0, "开仓与退出", "ATR 止损倍数"),
    "fixed_stop_pct": (0.02, 0.003, 0.20, "开仓与退出", "固定止损比例"),
    "stop_floor_pct": (0.003, 0.003, 0.10, "开仓与退出", "最小止损比例"),
    "target_1": (1.5, 0.2, 20.0, "开仓与退出", "止盈 1 风险倍数"),
    "target_2": (3.0, 0.3, 30.0, "开仓与退出", "止盈 2 风险倍数"),
    "target_1_ratio": (0.5, 0.01, 0.99, "开仓与退出", "止盈 1 平仓比例"),
    "entry_band_r": (0.12, 0.01, 2.0, "开仓与退出", "入场区间 / 风险距离"),
    "min_rr_override": (0.0, 0.0, 10.0, "开仓与退出", "最低收益风险比（0 跟随风险偏好）"),
    "ttl_override": (0, 0, 900, "开仓与退出", "信号有效秒数（0 跟随风险偏好）"),
    "add_trigger_r": (0.75, 0.0, 5.0, "加减仓与关联风险", "加仓触发 / 风险距离"),
    "reduce_trigger_r": (0.5, 0.0, 5.0, "加减仓与关联风险", "减仓观察 / 风险距离"),
    "add_default_ratio": (0.25, 0.01, 1.0, "加减仓与关联风险", "默认每次加仓比例"),
    "add_score_bonus": (5, 0, 30, "加减仓与关联风险", "加仓额外分数"),
    "risk_reduce_ratio": (0.5, 0.05, 1.0, "加减仓与关联风险", "风险减仓比例"),
    "group_exposure_fraction": (0.75, 0.1, 1.0, "加减仓与关联风险", "同类资产敞口比例上限"),
    "portfolio_risk_multiple": (3.0, 1.0, 3.0, "加减仓与关联风险", "组合止损预算 / 单笔预算"),
}
FLAGS = {
    "ema_enabled": ("指标", "EMA 趋势"),
    "rsi_enabled": ("指标", "RSI 动量"),
    "macd_enabled": ("指标", "MACD 动量"),
    "volume_enabled": ("指标", "成交量评分"),
    "volume_confirm_enabled": ("指标", "成交量必须确认"),
    "volatility_enabled": ("指标", "波动率评分"),
    "funding_enabled": ("指标", "资金费率"),
    "oi_enabled": ("指标", "合约持仓量"),
    "options_enabled": ("指标", "期权 / 交割"),
    "etf_enabled": ("指标", "ETF 已披露资金流"),
    "liquidation_enabled": ("指标", "清算指标"),
    "macro_enabled": ("指标", "宏观 / 新闻"),
    "frame_1w_enabled": ("权重与周期", "周线"),
    "frame_1d_enabled": ("权重与周期", "日线"),
    "frame_4h_enabled": ("权重与周期", "4H"),
    "frame_1h_enabled": ("权重与周期", "1H"),
    "alignment_4h_1h_enabled": ("权重与周期", "4H 与 1H 必须同向"),
    "conservative_alignment_enabled": ("权重与周期", "保守模式要求全部周期同向"),
    "entry_enabled": ("开仓与退出", "自动策略开仓"),
    "atr_stop_enabled": ("开仓与退出", "使用 ATR 止损（关闭改用固定比例）"),
    "target_1_enabled": ("开仓与退出", "第一档止盈"),
    "target_2_enabled": ("开仓与退出", "第二档止盈"),
    "breakeven_enabled": ("开仓与退出", "首次止盈后移至保本"),
    "trend_exit_enabled": ("开仓与退出", "趋势反转退出"),
    "event_exit_enabled": ("开仓与退出", "事件风险退出"),
    "add_enabled": ("加减仓与关联风险", "允许策略加仓"),
    "profitable_add_only": ("加减仓与关联风险", "仅盈利加仓"),
    "invalid_signal_reduce_enabled": ("加减仓与关联风险", "失效信号减仓"),
    "price_reduce_enabled": ("加减仓与关联风险", "到减仓观察价自动减仓"),
}


@model_validator(mode="after")
def ordered(self):
    p = self.model_dump()
    e = {k: (p[k] if p["overrides"].get(k, True) else v[0]) for k, v in NUMERIC.items()}
    if e["ema_fast"] >= e["ema_slow"] or e["macd_fast"] >= e["macd_slow"]:
        raise ValueError("快线周期必须小于慢线")
    if e["target_2"] <= e["target_1"]:
        raise ValueError("第二档止盈须高于第一档")
    if (
        e["rsi_long_min"] > e["rsi_long_max"]
        or e["rsi_short_min"] > e["rsi_short_max"]
        or e["atr_score_min"] > e["atr_score_max"]
    ):
        raise ValueError("指标上下限顺序错误")
    if not any(p["frame_" + f + "_enabled"] for f in ("1w", "1d", "4h", "1h")):
        raise ValueError("至少开启一个趋势周期")
    if set(p["overrides"]) - set(NUMERIC):
        raise ValueError("未知参数开关")
    return self


Parameters = create_model(
    "Parameters",
    __base__=Model,
    __validators__={"ordered": ordered},
    **{
        k: (
            type(v[0]),
            Field(
                default=v[0],
                ge=v[1],
                le=v[2],
                title=v[4],
                json_schema_extra={"group": v[3]},
                strict=isinstance(v[0], int),
            ),
        )
        for k, v in NUMERIC.items()
    },
    **{
        k: (bool, Field(default=k != "price_reduce_enabled", title=v[1], json_schema_extra={"group": v[0]}))
        for k, v in FLAGS.items()
    },
    overrides=(
        dict[str, bool],
        Field(default_factory=dict, description="关闭数值覆盖时恢复基准值；指标和动作开关独立控制是否参与。"),
    ),
)


def effective(parameters=None):
    p = Parameters.model_validate(parameters or {}).model_dump()
    for k, v in NUMERIC.items():
        if not p["overrides"].get(k, True):
            p[k] = v[0]
    return p
