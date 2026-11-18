"""Account-owned capital budgets. No signal-profile or strategy fallbacks."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# label, unit, minimum, maximum. Percent values are percentages, not fractions.
LIMITS = {
    "margin_pct": ("总保证金占权益上限", "%", 0.01, 100),
    "exposure_multiple": ("总名义敞口 / 权益", "倍", 0.01, 1000),
    "trade_loss_pct": ("每次开仓或加仓止损预算", "%", 0.001, 100),
    "portfolio_loss_pct": ("组合止损预算", "%", 0.001, 100),
    "daily_loss_pct": ("当日权益亏损上限（UTC）", "%", 0.01, 100),
    "group_multiple": ("同类资产名义敞口 / 权益", "倍", 0.01, 1000),
    "stop_liq_pct": ("止损至强平最小缓冲 / 价格", "%", 0, 100),
    "mark_liq_pct": ("现价至强平最小缓冲 / 价格", "%", 0, 100),
}
DEFAULTS = {
    "conservative": (50, 0.30, 0.30, 0.90, 1.5, 0.225, 3, 5),
    "balanced": (70, 0.60, 0.60, 1.80, 3, 0.45, 3, 5),
    "aggressive": (90, 0.90, 1, 3, 5, 0.675, 3, 5),
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


class Limit(Strict):
    enabled: bool = True
    value: float


def preset(level="balanced"):
    return {k: {"enabled": True, "value": v} for k, v in zip(LIMITS, DEFAULTS[level], strict=True)}


class RiskPolicy(Strict):
    limits: dict[str, Limit] = Field(default_factory=lambda: preset())
    default_add_count: int = Field(default=2, ge=0, le=5, strict=True)

    @model_validator(mode="after")
    def valid_limits(self):
        if set(self.limits) != set(LIMITS):
            raise ValueError("账户限额项目不完整或含未知项目")
        for k, v in self.limits.items():
            if not LIMITS[k][2] <= v.value <= LIMITS[k][3]:
                raise ValueError(f"{LIMITS[k][0]}超出允许范围")
        return self


class Allocation(Strict):
    # Explicit per-pair allocation, especially for lossless pre-0.15 migration.
    available_margin_pct: float = Field(default=95, gt=0, le=100)
    trade_loss_pct: float | None = Field(default=None, ge=0.001, le=100)
    exposure_multiple: float | None = Field(default=None, ge=0.01, le=1000)
    group_multiple: float | None = Field(default=None, ge=0.01, le=1000)


class Authority(Strict):
    allowed: bool = False
    minimum: float
    maximum: float
    allow_disable: bool = False


class RiskAI(Strict):
    enabled: bool = False
    level: Literal["conservative", "balanced", "aggressive"] = "balanced"
    min_interval: int = Field(default=60, ge=30, le=3600, strict=True)
    validity_seconds: int = Field(default=1800, ge=60, le=86400, strict=True)
    equity_change_pct: float = Field(default=2, ge=0.1, le=50)
    authority: dict[str, Authority] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_authority(self):
        if set(self.authority) - set(LIMITS):
            raise ValueError("未知 AI 额度授权项目")
        for k, a in self.authority.items():
            if not LIMITS[k][2] <= a.minimum <= a.maximum <= LIMITS[k][3]:
                raise ValueError(f"{LIMITS[k][0]}的 AI 授权范围无效")
        if self.enabled and not any(a.allowed for a in self.authority.values()):
            raise ValueError("请先勾选 AI 可调整的额度并设置授权范围")
        return self


def policy(account):
    return RiskPolicy.model_validate(account.get("risk_policy", {})).model_dump()


def value(account, key, sizing=None):
    limit = policy(account)["limits"][key]
    result = Decimal(str(limit["value"])) if limit["enabled"] else None
    allocation = (sizing or {}).get("allocation", {})
    override = allocation.get(key)
    if override is not None:
        override = Decimal(str(override))
        result = min(result, override) if result is not None else override
    return result


def fraction(account, key, sizing=None):
    v = value(account, key, sizing)
    return v / 100 if v is not None else None


def migrate(account, profile, parameters):
    """Copy previously effective limits once, without upgrading risk authorization."""
    if "risk_policy" in account:
        return account
    a = copy.deepcopy(account)
    risk, exposure, adds = {
        "conservative": (0.3, 0.3, 1),
        "balanced": (0.6, 0.6, 2),
        "aggressive": (1, 0.9, 3),
    }[profile]
    active = parameters.get("overrides", {})
    group = parameters.get("group_exposure_fraction", 0.75)
    multiple = (
        parameters.get("portfolio_risk_multiple", 3) if active.get("portfolio_risk_multiple", True) else 3
    )
    limits = preset(profile)
    limits["margin_pct"]["enabled"] = False  # Old system used free-margin allocation per order.
    limits["trade_loss_pct"]["value"] = risk
    limits["portfolio_loss_pct"]["value"] = risk * multiple
    limits["group_multiple"] = {
        "enabled": active.get("group_exposure_fraction", True),
        "value": exposure * group,
    }
    limits["mark_liq_pct"]["value"] = 3 if a["kind"] == "paper" else 5
    a["risk_policy"] = {"limits": limits, "default_add_count": adds}
    for s in a.get("sizing", []):
        allocation = Allocation().model_dump()
        if s["mode"] == "system":
            margin, factor = {"conservative": (20, 0.5), "balanced": (40, 0.75), "aggressive": (70, 1)}[
                s.get("system_level", "balanced")
            ]
            allocation.update(
                available_margin_pct=margin, trade_loss_pct=risk * factor, exposure_multiple=exposure * factor
            )
            if limits["group_multiple"]["enabled"]:
                allocation["group_multiple"] = exposure * group * factor
        s["allocation"] = allocation
    return a


def bounded_changes(account, proposed):
    """AI can only edit explicitly delegated fields, never its own authority."""
    before = policy(account)
    after = RiskPolicy.model_validate(proposed).model_dump()
    authority = RiskAI.model_validate(account.get("risk_ai", {})).authority
    if after["default_add_count"] != before["default_add_count"]:
        raise ValueError("AI 额度管理不能修改加仓次数")
    changes = {}
    for k, old in before["limits"].items():
        new = after["limits"][k]
        if new == old:
            continue
        scope = authority.get(k)
        if not scope or not scope.allowed:
            raise ValueError(f"AI 未获授权修改{LIMITS[k][0]}")
        if not scope.minimum <= new["value"] <= scope.maximum:
            raise ValueError(f"AI 提议超出{LIMITS[k][0]}授权范围")
        if old["enabled"] != new["enabled"] and not scope.allow_disable:
            raise ValueError(f"AI 未获授权切换{LIMITS[k][0]}")
        changes[k] = {"before": old, "after": new}
    return after, changes


def protection_fraction(account, position):
    """A capital-policy edit cannot silently rewrite an existing exit trigger."""
    return Decimal(str(position.get("liquidation_buffer_pct", 3 if account["kind"] == "paper" else 5))) / 100
