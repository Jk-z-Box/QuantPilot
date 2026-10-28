from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

EXCHANGES = ("binance", "okx", "gate")
FRAMES = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}
PROFILES = {
    "conservative": {
        "label": "保守",
        "risk": 0.003,
        "exposure": 0.30,
        "ttl": 120,
        "min_rr": 2,
        "volume": 1.2,
        "adds": 1,
    },
    "balanced": {
        "label": "平衡",
        "risk": 0.006,
        "exposure": 0.60,
        "ttl": 180,
        "min_rr": 1.8,
        "volume": 1,
        "adds": 2,
    },
    "aggressive": {
        "label": "激进",
        "risk": 0.01,
        "exposure": 0.90,
        "ttl": 240,
        "min_rr": 1.6,
        "volume": 0.8,
        "adds": 3,
    },
}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Instrument(Model):
    id: str = Field(default="", max_length=64, pattern=r"^[a-zA-Z0-9_-]*$")
    label: str = Field(min_length=1, max_length=40)
    exchange: Literal["binance", "okx", "gate"] = "binance"
    symbol: str = Field(pattern=r"^[A-Z0-9]{1,20}/USDT:USDT$")
    enabled: bool = True
    group: Literal["crypto", "equity"] = "crypto"


def defaults():
    return [
        Instrument(
            id=s.lower(),
            label=s,
            symbol=s + "/USDT:USDT",
            group="equity" if s in ("AVGO", "SOXL") else "crypto",
        ).model_dump()
        for s in ("BTC", "ETH", "SOL", "AVGO", "SOXL")
    ]


class Preferences(Model):
    profile: Literal["conservative", "balanced", "aggressive"] = "balanced"
    min_score: int = Field(default=80, ge=1, le=100, strict=True)
    ai_provider: Literal["api_key", "chatgpt", "codex"] = "api_key"
    ai_enabled: bool = False
    ai_model: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_.:-]*$")
    ai_interval: int = Field(default=300, ge=60, le=3600)
    ai_intervene: bool = False
    ai_data_enabled: bool = False
    ai_data_interval: int = Field(default=1800, ge=900, le=86400)
    halted: bool = False


POLICY_KEYS = ("ai_provider", "profile", "min_score", "ai_enabled", "ai_model", "ai_intervene", "ai_data_enabled")


def signal_policy(preferences):
    return {key: preferences[key] for key in POLICY_KEYS}


def policy_matches(opportunity, preferences):
    # Older/test signals still have the independent profile and threshold guards.
    return "policy" not in opportunity or opportunity["policy"] == signal_policy(preferences)


class Sizing(Model):
    instrument_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    mode: Literal["fixed", "maximum"] = "maximum"
    quantity: float | None = Field(default=None, gt=0, le=1000000000)
    leverage: int = Field(default=1, ge=1, le=1000)
    add_count: int | None = Field(default=None, ge=0, le=5)
    add_ratios: list[float] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def check_adds(self):
        if any(not 0 < x <= 1 for x in self.add_ratios):
            raise ValueError("加仓比例需大于 0 且不超过 1")
        if self.add_ratios and (self.add_count is None or len(self.add_ratios) != self.add_count):
            raise ValueError("加仓次数需与比例数量相同")
        return self


class Account(Model):
    id: str = Field(default="", max_length=64, pattern=r"^[a-zA-Z0-9_-]*$")
    name: str = Field(min_length=1, max_length=50)
    kind: Literal["paper", "demo", "live"] = "paper"
    exchange: Literal["binance", "okx", "gate"] = "binance"
    capital: float | None = Field(default=None, ge=100, le=100000000)
    auto: bool = False
    allowed: list[str] = Field(default_factory=list, max_length=30)
    sizing: list[Sizing] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid_sizing(self):
        if self.kind == "paper":
            self.capital = 10000 if self.capital is None else self.capital
        else:
            self.capital = None
        ids = [s.instrument_id for s in self.sizing]
        if len(ids) != len(set(ids)) or len(self.allowed) != len(set(self.allowed)):
            raise ValueError("交易对不可重复")
        if self.kind != "paper" and not set(ids) <= set(self.allowed):
            raise ValueError("仓位设置只允许已授权交易对")
        if self.auto and (not self.sizing or any(s.quantity is None for s in self.sizing)):
            raise ValueError("请先设置交易对的币数量，再启用自动交易")
        if self.auto and self.kind != "paper" and set(ids) != set(self.allowed):
            raise ValueError("每个授权交易对都需要设置仓位数量")
        return self


class AccountSecrets(Model):
    api_key: SecretStr = Field(min_length=1, max_length=512)
    secret: SecretStr = Field(min_length=1, max_length=512)
    passphrase: SecretStr = Field(default=SecretStr(""), max_length=512)


class Reset(Model):
    capital: float = Field(ge=100, le=100000000)
    confirmation: Literal["RESET"]


class KeyInput(Model):
    api_key: SecretStr = Field(min_length=1, max_length=512)


class Evidence(Model):
    """Published observations, not a request for an LLM to invent data."""

    instrument_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    category: Literal["etf", "options", "liquidation", "macro"]
    source: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=500)
    published_at: float = Field(gt=0)
    expires_at: float = Field(gt=0)
    value: float = 0
    bias: Literal["bullish", "bearish", "neutral", "risk"] = "neutral"
    note: str = Field(default="", max_length=1000)
    event_at: float | None = None

    @model_validator(mode="after")
    def check_source(self):
        if not re.match(r"^https://[^\s/@]+(?:/|$)", self.url) or self.expires_at <= self.published_at:
            raise ValueError("需要 HTTPS 来源和正确时间范围")
        return self
