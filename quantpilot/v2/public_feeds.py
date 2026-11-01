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


FARSIDE = {
    "BTC": "bitcoin-etf-flow-all-data",
    "ETH": "ethereum-etf-flow-all-data",
    "SOL": "solana-etf-flow-all-data",
}


def farside_etf(html, asset, now):
    from html.parser import HTMLParser

    class Table(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.rows, self.row, self.cell = [], [], None

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self.row = []
            if tag in ("td", "th"):
                self.cell = []

        def handle_data(self, data):
            if self.cell is not None:
                self.cell.append(data)

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self.cell is not None:
                self.row.append(" ".join("".join(self.cell).split()))
                self.cell = None
            if tag == "tr" and self.row:
                self.rows.append(self.row)

    table = Table()
    table.feed(html)
    valid = []
    for row in table.rows:
        try:
            date = dt.datetime.strptime(row[0], "%d %b %Y").date()
        except ValueError:
            continue
        if len(row) < 4 or not re.fullmatch(r"\(?-?[\d,]+(?:\.\d+)?\)?", row[-1]):
            continue
        # A total is a publisher-reported aggregate, not proof that every fund has reported.
        raw = row[-1].replace(",", "")
        total = -float(raw[1:-1]) if raw.startswith("(") else float(raw)
        stamp = dt.datetime.combine(date, dt.time(16), ZoneInfo("America/New_York")).timestamp()
        if stamp <= now and math.isfinite(total):
            valid.append((stamp, total, date, any(v in ("", "-", "—") for v in row[1:-1])))
    if not valid:
        raise ValueError("未找到带日期的 ETF 汇总")
    stamp, total, date, incomplete = max(valid)
    return {
        "category": "etf",
        "source": "Farside Investors · 公开已披露汇总",
        "url": "https://farside.co.uk/" + FARSIDE[asset] + "/",
        "published_at": stamp,
        "expires_at": stamp + 4 * 86400,
        "status": "fresh" if now - stamp < 4 * 86400 else "stale",
        "bias": "neutral"
        if incomplete
        else "bullish"
        if total > 0
        else "bearish"
        if total < 0
        else "neutral",
        "value": total * 1_000_000,
        "note": f"{date} {asset} ETF 汇总（USD）；按美东收盘标记。"
        + (
            "部分基金列为空或横线，只计中性覆盖，不视为完整全日资金流。"
            if incomplete
            else "来自公开表格的已披露值，非盘中实时数据。"
        ),
        "details": {
            "session": str(date),
            "net_flow_usd": total * 1_000_000,
            "partial": incomplete,
            "retrieved_at": now,
        },
    }
