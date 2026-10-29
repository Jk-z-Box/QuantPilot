"""Public observations with source timestamps. No access-control circumvention."""

from __future__ import annotations

import datetime as dt
import math
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ETF_URL = "https://www.tftc.io/bitcoin-etf-flows/latest.json"
ETF_PAGE = "https://www.tftc.io/bitcoin-etf-flows"
NEWS_URL = "https://news.google.com/rss/search"


def btc_etf(body, now):
    if body.get("units") != "USD":
        raise ValueError("ETF 单位无效")
    session = body["latestSession"]
    date = dt.date.fromisoformat(session["date"])
    stamp = dt.datetime.combine(date, dt.time(16), ZoneInfo("America/New_York")).timestamp()
    flow = float(session["netFlowUsd"])
    funds = session["perEtfUsd"]
    values = [float(x) for x in funds.values()]
    if (
        len(values) < 10
        or not all(math.isfinite(x) for x in [flow, *values])
        or abs(sum(values) - flow) > max(100, len(values) * 50000)
        or stamp > now
    ):
        raise ValueError("ETF 披露不完整或汇总不一致")
    return {
        "category": "etf",
        "source": "The Commoner Bitcoin ETF Tracker / TFTC（公开汇总）",
        "url": ETF_PAGE,
        "published_at": stamp,
        "expires_at": stamp + 4 * 86400,
        "status": "fresh" if now - stamp < 4 * 86400 else "stale",
        "bias": "bullish" if flow > 0 else "bearish" if flow < 0 else "neutral",
        "value": flow,
        "note": f"{date} 交易日 BTC ETF 已披露汇总，非盘中资金流；时间按该交易日美东收盘标记。来源超过 4 天不计分。",
        "details": {
            "session": str(date),
            "net_flow_usd": flow,
            "funds": funds,
            "license": "CC BY 4.0",
            "methodology": ETF_PAGE + "/methodology",
            "attribution": "The Commoner Bitcoin ETF Tracker by TFTC",
        },
    }


def news_items(xml, now):
    root = ET.fromstring(xml)  # noqa: S314
    news = []
    for item in root.findall("./channel/item")[:20]:
        title, link, date = item.findtext("title", ""), item.findtext("link", ""), item.findtext("pubDate")
        if not date or not link.startswith("https://"):
            continue
        stamp = parsedate_to_datetime(date).timestamp()
        if -5 <= now - stamp <= 7 * 86400:
            news.append({"title": title[:300], "url": link, "published_at": stamp})
    return news


def company_news(get, asset, now):
    query = {"AVGO": "Broadcom (earnings OR results OR acquisition)", "SOXL": "SOXL semiconductor"}.get(
        asset, f'"{asset}" stock earnings'
    )
    params = {"q": query + " when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    news = []
    source = "Google News 公开新闻索引"
    url = NEWS_URL + "?" + urlencode(params)
    if asset == "AVGO":
        try:
            news = news_items(get("https://investors.broadcom.com/rss/news-releases.xml", text=True), now)
            if news:
                source, url = "Broadcom 官方公告", "https://investors.broadcom.com/rss-feeds"
        except Exception:
            news = []
    if not news:
        news = news_items(get(NEWS_URL, params=params, text=True), now)
    if not news:
        raise ValueError("没有近期公开新闻")
    latest = max(n["published_at"] for n in news)
    risk = any(
        now - n["published_at"] < 21600
        and re.search(r"trading halt|fraud|bankrupt|emergency", n["title"], re.I)
        for n in news
    )
    return {
        "category": "macro",
        "source": source,
        "url": url,
        "published_at": latest,
        "expires_at": latest + 2 * 86400,
        "status": "fresh" if now - latest < 2 * 86400 else "stale",
        "bias": "risk" if risk else "neutral",
        "note": "免费标题与原文链接；新闻索引不是已核实事实，不提供完整财报日历，不据此推算财报时间。",
        "details": {"news": news[:10], "events": [], "checked_at": now},
    }
