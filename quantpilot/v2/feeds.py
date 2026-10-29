from __future__ import annotations

import calendar
import datetime as dt
import re
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import httpx

from .public_feeds import ETF_URL, btc_etf, company_news

FED = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"


def fomc_events(html):
    result = []
    sections = re.split(r"(\d{4}) FOMC Meetings", html)
    for i in range(1, len(sections), 2):
        year, block = int(sections[i]), sections[i + 1]
        pairs = re.findall(
            r"fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)",
            block,
            re.S,
        )
        for month, days in pairs:
            if month.strip() not in calendar.month_name:
                continue
            nums = re.findall(r"\d+", days)
            if not nums:
                continue
            when = dt.datetime(
                year,
                list(calendar.month_name).index(month.strip()),
                int(nums[-1]),
                14,
                tzinfo=ZoneInfo("America/New_York"),
            ).timestamp()
            result.append({"title": "FOMC 利率决议（常规 14:00 ET 时间窗口）", "event_at": when, "url": FED})
    return sorted(result, key=lambda e: e["event_at"])


def option_summary(instruments, books, now):
    by_name = {b["instrument_name"]: b for b in books}
    rows = []
    for m in instruments:
        b = by_name.get(m["instrument_name"])
        if b and m.get("expiration_timestamp", 0) / 1000 > now and float(b.get("open_interest", 0)) > 0:
            rows.append(
                {
                    "strike": float(m["strike"]),
                    "type": m["option_type"],
                    "oi": float(b["open_interest"]),
                    "expiry": m["expiration_timestamp"] / 1000,
                    "size": float(m.get("contract_size", 1)),
                }
            )
    if not rows:
        raise ValueError("期权数据缺失")
    expiry = min(r["expiry"] for r in rows)
    nearest = [r for r in rows if r["expiry"] == expiry]
    strikes = sorted({r["strike"] for r in nearest})

    def payout(price):
        return sum(
            max(0, price - r["strike"] if r["type"] == "call" else r["strike"] - price) * r["oi"] * r["size"]
            for r in nearest
        )

    pain = min(strikes, key=payout)
    return {
        "expiry": expiry,
        "max_pain": pain,
        "call_oi": sum(r["oi"] for r in nearest if r["type"] == "call"),
        "put_oi": sum(r["oi"] for r in nearest if r["type"] == "put"),
        "contracts": len(nearest),
        "scope": "Deribit 最近到期；按当前持仓估算，并非价格预测",
    }


class Feeds:
    def __init__(self, vault, store, transport=None):
        self.vault, self.store, self.transport = vault, store, transport
        self.cache, self.lock = {}, threading.RLock()

    def get(self, url, params=None, headers=None, text=False):
        with httpx.Client(timeout=8, transport=self.transport, follow_redirects=False) as client:
            r = client.get(url, params=params, headers=headers)
            r.raise_for_status()
            if len(r.content) > 5000000:
                raise ValueError("数据源响应过大")
            return r.text if text else r.json()

    def cached(self, key, ttl, fn):
        now = time.time()
        with self.lock:
            if key in self.cache and now - self.cache[key][0] < ttl:
                return self.cache[key][1]
            try:
                value = fn()
            except Exception:
                value = None
            self.cache[key] = (now, value)
            return value

    def macro(self):
        def fetch():
            now = time.time()
            events = fomc_events(self.get(FED, text=True))
            if not events:
                raise ValueError("FOMC 日历结构变化")
            events = [e for e in events if e["event_at"] > now - 7200][:5]
            root = ET.fromstring(self.get("https://www.federalreserve.gov/feeds/press_all.xml", text=True))  # noqa: S314
            news = []
            for node in root.findall("./channel/item")[:12]:
                pub = node.findtext("pubDate")
                if pub:
                    news.append(
                        {
                            "title": node.findtext("title", "")[:300],
                            "url": node.findtext("link", ""),
                            "published_at": parsedate_to_datetime(pub).timestamp(),
                        }
                    )
            return {"events": events, "news": news, "checked_at": now}

        return self.cached("macro", 300, fetch)

    def options(self, asset):
        if asset not in ("BTC", "ETH", "SOL"):
            return None

        def fetch():
            url = "https://www.deribit.com/api/v2/public/"
            ins = self.get(
                url + "get_instruments", {"currency": asset, "kind": "option", "expired": "false"}
            )["result"]
            books = self.get(url + "get_book_summary_by_currency", {"currency": asset, "kind": "option"})[
                "result"
            ]
            return {**option_summary(ins, books, time.time()), "checked_at": time.time()}

        return self.cached(("options", asset), 300, fetch)

    def evidence(self, item):
        now = time.time()
        out = []
        asset = item["symbol"].split("/")[0]
        macro = self.macro()
        if macro:
            next_event = macro["events"][0]["event_at"] if macro["events"] else None
            urgent = any(
                now - n["published_at"] < 21600 and re.search(r"emergency|unscheduled", n["title"], re.I)
                for n in macro["news"]
            )
            out.append(
                {
                    "category": "macro",
                    "status": "fresh",
                    "source": "Federal Reserve",
                    "url": FED,
                    "published_at": macro["checked_at"],
                    "expires_at": macro["checked_at"] + 600,
                    "event_at": next_event,
                    "bias": "risk" if urgent else "neutral",
                    "note": "官方日历与公告；不代表已覆盖全市场突发新闻或公司财报",
                    "details": macro,
                }
            )
        option = self.options(asset)
        if option:
            out.append(
                {
                    "category": "options",
                    "status": "fresh",
                    "source": "Deribit",
                    "url": "https://www.deribit.com",
                    "published_at": option["checked_at"],
                    "expires_at": option["checked_at"] + 600,
                    "event_at": option["expiry"],
                    "bias": "neutral",
                    "note": option["scope"],
                    "details": option,
                }
            )
        with self.store.connect() as db:
            imported = self.store.read(db, "v2_evidence")
        for row in imported:
            if row["instrument_id"] == item["id"]:
                out.append(
                    {
                        **row,
                        "status": "fresh" if row["published_at"] <= now < row["expires_at"] else "stale",
                        "source": row["source"] + "（人工核验导入）",
                    }
                )
        liquidation = self.liquidation(asset) or self.public_liquidation(asset)
        if liquidation:
            out.append(liquidation)
        if item["group"] == "equity":
            company = self.cached(
                ("public_company", asset), 600, lambda: company_news(self.get, asset, time.time())
            )
            if company:
                out.append(company)
            configured_company = self.company(asset)
            if configured_company:
                out.append(configured_company)
        public_etf = (
            self.cached("public_btc_etf", 900, lambda: btc_etf(self.get(ETF_URL), time.time()))
            if asset == "BTC"
            else None
        )
        if public_etf:
            out.append(public_etf)
        cg = self.coinglass(asset) if not public_etf or public_etf["status"] != "fresh" else None
        if cg:
            out.extend(cg)
        for category in ("etf", "options", "liquidation", "macro"):
            if not any(x["category"] == category for x in out):
                out.append(
                    {
                        "category": category,
                        "status": "missing",
                        "source": "未接入 / 来源暂不可用",
                        "note": "缺失数据不计入有效证据，不以推测替代披露数据",
                        "bias": "neutral",
                    }
                )
        return out

    def coinglass(self, asset):
        key = self.vault.get("v2:feed:coinglass")
        if not key:
            return None

        def fetch():
            headers = {"CG-API-KEY": key["apiKey"]}
            now, output = time.time(), []
            coin = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}.get(asset)
            if coin:
                url = "https://open-api-v4.coinglass.com/api/etf/" + coin + "/flow-history"
                body = self.get(url, headers=headers)
                rows = body.get("data", []) if body.get("code") == "0" else []
                if rows:
                    row = max(rows, key=lambda r: r.get("timestamp", 0))
                    stamp = float(row["timestamp"]) / 1000
                    flow = float(row["flow_usd"])
                    output.append(
                        {
                            "category": "etf",
                            "source": "CoinGlass 已披露 ETF 汇总",
                            "url": "https://www.coinglass.com/bitcoin-etf",
                            "published_at": stamp,
                            "expires_at": stamp + 4 * 86400,
                            "status": "fresh" if 0 <= now - stamp < 4 * 86400 else "stale",
                            "bias": "bullish" if flow > 0 else "bearish" if flow < 0 else "neutral",
                            "value": flow,
                            "note": "已披露交易日净流量 USD；非盘中实时资金流",
                        }
                    )
            return output

        return self.cached(("cg", asset), 900, fetch)

    def liquidation(self, asset):
        key = self.vault.get("v2:feed:coinglass")
        if not key:
            return None

        def fetch():
            headers = {"CG-API-KEY": key["apiKey"]}
            base = "https://open-api-v4.coinglass.com"
            history = self.get(
                base + "/api/futures/liquidation/aggregated-history",
                {"symbol": asset, "exchange_list": "Binance,OKX,Gate", "interval": "1h", "limit": 24},
                headers,
            )
            data = history.get("data", []) if history.get("code") == "0" else []
            if not data:
                raise ValueError("清算量不可用")
            stamp = max(float(x["time"]) for x in data)
            if stamp > 1e12:
                stamp /= 1000
            longs = sum(float(x["aggregated_long_liquidation_usd"]) for x in data)
            shorts = sum(float(x["aggregated_short_liquidation_usd"]) for x in data)
            heat = None
            try:
                raw = self.get(
                    base + "/api/futures/liquidation/aggregated-heatmap/model1",
                    {"symbol": asset, "range": "24h"},
                    headers,
                )
                if raw.get("code") == "0":
                    h = raw["data"]
                    # Bound UI payload. Heatmap is modelled liquidation exposure, not actual fills.
                    heat = {
                        "prices": h["y_axis"],
                        "cells": h["liquidation_leverage_data"][-2000:],
                        "timestamps": [c[0] for c in h["price_candlesticks"]],
                    }
            except Exception:
                heat = None
            return {
                "category": "liquidation",
                "source": "CoinGlass",
                "url": "https://www.coinglass.com/liquidation-data",
                "published_at": stamp,
                "expires_at": stamp + 7200,
                "status": "fresh" if 0 <= time.time() - stamp < 7200 else "stale",
                "bias": "neutral",
                "note": "实际清算量按最近 24 根小时数据汇总；热力图为模型估算，非实际清算。"
                + ("热力图权限不可用。" if heat is None else ""),
                "details": {"long_liquidation_usd": longs, "short_liquidation_usd": shorts, "heatmap": heat},
            }

        return self.cached(("liquidation", asset), 300, fetch)

    def public_liquidation(self, asset):
        def fetch():
            url = "https://api.gateio.ws/api/v4/futures/usdt/contract_stats"
            rows = self.get(url, {"contract": asset + "_USDT", "interval": "1h", "limit": 24})
            if not isinstance(rows, list) or len(rows) < 2:
                raise ValueError("公开清算数据不足")
            rows = sorted(rows, key=lambda r: float(r["time"]))
            stamp = float(rows[-1]["time"])
            if any(float(b["time"]) - float(a["time"]) != 3600 for a, b in zip(rows, rows[1:], strict=False)):
                raise ValueError("清算统计时段不连续")
            return {
                "category": "liquidation",
                "status": "fresh" if 0 <= time.time() - stamp < 7200 else "stale",
                "source": "Gate 公开清算统计（单市场）",
                "url": "https://www.gate.com/docs/developers/apiv4/en/futures/#futures-statistics",
                "published_at": stamp,
                "expires_at": stamp + 7200,
                "bias": "neutral",
                "note": f"Gate {asset}/USDT 最近 {len(rows)} 根小时统计；仅为该交易所已发生清算，不代表币安或全市场。热力图需另外配置。",
                "details": {
                    "long_liquidation_usd": sum(
                        float(r.get("long_liq_usd_new", r["long_liq_usd"])) for r in rows
                    ),
                    "short_liquidation_usd": sum(
                        float(r.get("short_liq_usd_new", r["short_liq_usd"])) for r in rows
                    ),
                    "heatmap": None,
                },
            }

        return self.cached(("gate_liquidation", asset), 300, fetch)

    def company(self, asset):
        key = self.vault.get("v2:feed:finnhub")
        if not key:
            return None

        def fetch():
            now = time.time()
            today = dt.datetime.now(dt.UTC).date()
            headers = {"X-Finnhub-Token": key["apiKey"]}
            root = "https://finnhub.io/api/v1"
            news = self.get(
                root + "/company-news",
                {"symbol": asset, "from": str(today - dt.timedelta(days=2)), "to": str(today)},
                headers,
            )
            earnings = self.get(
                root + "/calendar/earnings",
                {"symbol": asset, "from": str(today), "to": str(today + dt.timedelta(days=14))},
                headers,
            )
            events = []
            for e in earnings.get("earningsCalendar", []):
                # The vendor often supplies only before/after-market timing; use a full-day risk window.
                date = dt.date.fromisoformat(e["date"])
                start = dt.datetime.combine(date, dt.time.min, ZoneInfo("America/New_York")).timestamp()
                events.append(
                    {
                        "title": asset + " 财报（日期级窗口，具体时间待确认）",
                        "event_at": start,
                        "url": "https://finnhub.io",
                    }
                )
            recent = [
                {"title": n["headline"][:300], "published_at": n["datetime"], "url": n.get("url", "")}
                for n in news[:10]
            ]
            risk = any(-86400 < e["event_at"] - now < 86400 for e in events) or any(
                now - n["published_at"] < 21600
                and re.search(r"halt|fraud|bankrupt|emergency", n["title"], re.I)
                for n in recent
            )
            return {
                "category": "macro",
                "source": "Finnhub 公司披露与新闻",
                "url": "https://finnhub.io",
                "published_at": now,
                "expires_at": now + 600,
                "status": "fresh",
                "bias": "risk" if risk else "neutral",
                "note": "新闻标题筛查不能保证覆盖所有突发事件；财报日期前后 24 小时阻止新增仓位。",
                "details": {"news": recent, "events": events},
            }

        return self.cached(("company", asset), 300, fetch)
