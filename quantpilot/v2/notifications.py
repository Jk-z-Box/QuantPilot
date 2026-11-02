"""Transactional notifications. Delivery never blocks market/position protection."""

from __future__ import annotations

import json
import threading
import time
from typing import Literal

import httpx
from pydantic import Field, SecretStr

from .models import Model

CATEGORIES = {
    "open": "开单",
    "take_profit": "止盈",
    "stop_loss": "止损",
    "add": "加仓",
    "reduce": "减仓 / 平仓",
    "opportunity": "有效机会",
    "ai": "AI 介入干预",
}


class Channels(Model):
    open: bool = True
    take_profit: bool = True
    stop_loss: bool = True
    add: bool = True
    reduce: bool = True
    opportunity: bool = True
    ai: bool = True


class NotificationSettings(Model):
    telegram_enabled: bool = False
    sound_enabled: bool = False
    sound_tone: Literal["chime", "bell", "pulse", "alert"] = "chime"
    sound_volume: int = Field(default=75, ge=0, le=100, strict=True)
    telegram: Channels = Field(default_factory=Channels)
    sound: Channels = Field(default_factory=Channels)


class TelegramKey(Model):
    token: SecretStr = Field(min_length=20, max_length=256)
    chat_id: str = Field(pattern=r"^-?[0-9]{1,20}$")


def emit(db, identity, category, message):
    if category not in CATEGORIES:
        return
    settings = db.execute("SELECT value FROM kv WHERE key='v2_notifications_settings'").fetchone()
    if not settings:
        return
    settings = json.loads(settings[0])
    context = db.execute("SELECT value FROM kv WHERE key='v2_notifications_context'").fetchone()[0]
    state = "pending" if settings["telegram_enabled"] and settings["telegram"][category] else "disabled"
    db.execute(
        "INSERT OR IGNORE INTO notification_events(identity,category,message,created,delivery,context) VALUES (?,?,?,?,?,?)",
        (identity, category, message[:3500], time.time(), state, context),
    )
    db.execute(
        "DELETE FROM notification_events WHERE seq < (SELECT COALESCE(MAX(seq),0)-2000 FROM notification_events)"
    )


def trade_event(store, db, identity, aid, action, detail):
    reason = detail.get("reason", "")
    category = (
        action
        if action in ("open", "add")
        else "take_profit"
        if reason == "take_profit"
        else "stop_loss"
        if reason in ("stop_loss", "liquidation_distance")
        else "reduce"
    )
    watch = store.read(db, "v2_watchlist")
    symbol = next(
        (i["symbol"] for i in watch if i["id"] == detail.get("instrument_id")), detail.get("symbol", "")
    )
    account = store.read(db, "v2_accounts").get(aid, {})
    kind = (
        "模拟"
        if account.get("kind") == "paper"
        else "交易所测试"
        if account.get("kind") == "demo"
        else "实盘"
    )
    message = f"QuantPilot · {CATEGORIES[category]}\n{kind}账户：{account.get('name', aid)}\n{symbol}\n方向：{detail.get('side', '—')}\n数量：{detail.get('qty', '—')}\n成交价：{detail.get('price', '—')}"
    if reason:
        message += "\n原因：" + reason
    emit(db, "trade:" + identity, category, message)


class Notifications:
    def __init__(self, workspace, transport=None):
        self.workspace, self.store, self.vault = workspace, workspace.store, workspace.vault
        self.transport = transport
        self.lock = threading.Lock()
        with self.store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS notification_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT UNIQUE NOT NULL, category TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL, delivery TEXT NOT NULL, context TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0)"
            )
            db.execute(
                "INSERT OR IGNORE INTO kv VALUES ('v2_notifications_settings',?)",
                (json.dumps(NotificationSettings().model_dump()),),
            )
            db.execute("INSERT OR IGNORE INTO kv VALUES ('v2_notifications_context','0')")
            db.execute("INSERT OR IGNORE INTO kv VALUES ('v2_notifications_status','{}')")
            # Never replay an ambiguous send after a process crash.
            db.execute("UPDATE notification_events SET delivery='unknown' WHERE delivery='sending'")

    def state(self, after=None):
        settings = NotificationSettings.model_validate(
            self.store.get("v2_notifications_settings")
        ).model_dump()
        with self.store.connect() as db:
            latest = db.execute("SELECT COALESCE(MAX(seq),0) FROM notification_events").fetchone()[0]
            rows = (
                []
                if after is None
                else [
                    dict(r)
                    for r in db.execute(
                        "SELECT seq,category,message,created,delivery FROM notification_events WHERE seq>? ORDER BY seq LIMIT 100",
                        (after,),
                    )
                ]
            )
            failed = db.execute(
                "SELECT count(*) FROM notification_events WHERE delivery IN ('failed','unknown')"
            ).fetchone()[0]
        key = self.vault.get("v2:telegram")
        return {
            "settings": settings,
            "configured": bool(key),
            "chat_id": key["chat_id"] if key else "",
            "cursor": rows[-1]["seq"] if rows else latest,
            "events": rows,
            "failed_count": failed,
            "status": self.store.get("v2_notifications_status"),
        }

    def settings(self, body):
        settings = NotificationSettings.model_validate(body).model_dump()
        with self.lock, self.store.transaction() as db:
            if settings["telegram_enabled"] and not self.vault.get("v2:telegram"):
                raise ValueError("请先配置 Telegram Bot Token 和 Chat ID")
            self.store.write(db, "v2_notifications_settings", settings)
            context = self.store.read(db, "v2_notifications_context") + 1
            self.store.write(db, "v2_notifications_context", context)
            db.execute("UPDATE notification_events SET delivery='disabled' WHERE delivery='pending'")
            self.store.audit(db, "notification_settings", settings)
        return self.state()

    def key(self, body):
        token = body.token.get_secret_value().strip()
        import re

        if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}", token):
            raise ValueError("Bot Token 格式不正确")
        with self.lock:
            self.vault.put("v2:telegram", {"token": token, "chat_id": body.chat_id})
            self._invalidate()
        return self.state()

    def _invalidate(self):
        with self.store.transaction() as db:
            settings = self.store.read(db, "v2_notifications_settings")
            settings["telegram_enabled"] = False
            self.store.write(db, "v2_notifications_settings", settings)
            self.store.write(
                db, "v2_notifications_context", self.store.read(db, "v2_notifications_context") + 1
            )
            db.execute("UPDATE notification_events SET delivery='disabled' WHERE delivery='pending'")

    def delete_key(self):
        with self.lock:
            with self.store.transaction() as db:
                db.execute("DELETE FROM credentials WHERE name=?", ("v2:telegram",))
                self.store.audit(db, "telegram_deleted", {})
            self._invalidate()
        return self.state()

    def send(self, key, message):
        # Fixed destination; raw HTTP exceptions can contain the token and are never logged.
        with httpx.Client(timeout=8, follow_redirects=False, trust_env=False, transport=self.transport) as c:
            response = c.post(
                "https://api.telegram.org/bot" + key["token"] + "/sendMessage",
                json={
                    "chat_id": key["chat_id"],
                    "text": message,
                    "link_preview_options": {"is_disabled": True},
                },
            )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 429:
            return "retry", min(3600, max(5, int(body.get("parameters", {}).get("retry_after", 30))))
        return ("sent", 0) if response.is_success and body.get("ok") is True else ("failed", 0)

    def test(self):
        with self.lock:
            key = self.vault.get("v2:telegram")
            if not key:
                raise ValueError("请先保存 Telegram 配置")
            status = self.store.get("v2_notifications_status")
            if time.time() - status.get("test_at", 0) < 30:
                raise ValueError("测试消息至少间隔 30 秒")
            with self.store.transaction() as db:
                self.store.write(db, "v2_notifications_status", {**status, "test_at": time.time()})
            try:
                result, _ = self.send(key, "QuantPilot 提醒测试：连接成功。此消息不包含账户资金或密钥。")
            except Exception:
                raise ValueError("发送结果未确认；请检查 Telegram，避免立即重复发送") from None
            if result != "sent":
                raise ValueError("Telegram 未接受消息；请检查 Chat ID、Bot 权限及限频状态")
            return {"sent": True}

    def deliver_once(self):
        with self.lock:
            key = self.vault.get("v2:telegram")
            settings = self.store.get("v2_notifications_settings")
            if not key or not settings["telegram_enabled"]:
                return
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE notification_events SET delivery='expired' WHERE delivery='pending' AND created<?",
                    (time.time() - 3600,),
                )
                row = db.execute(
                    "SELECT * FROM notification_events WHERE delivery='pending' AND next_attempt<=? ORDER BY seq LIMIT 1",
                    (time.time(),),
                ).fetchone()
                if not row:
                    return
                row = dict(row)
                context = db.execute("SELECT value FROM kv WHERE key='v2_notifications_context'").fetchone()[
                    0
                ]
                if row["context"] != context or not settings["telegram"][row["category"]]:
                    db.execute(
                        "UPDATE notification_events SET delivery='disabled' WHERE seq=?", (row["seq"],)
                    )
                    return
                db.execute(
                    "UPDATE notification_events SET delivery='sending',attempts=attempts+1 WHERE seq=?",
                    (row["seq"],),
                )
            delay = 0
            try:
                state, delay = self.send(key, row["message"])
            except httpx.ConnectError:
                state, delay = "retry", 30
            except Exception:
                state = "unknown"
            if state == "retry":
                state = "pending" if row["attempts"] < 3 else "failed"
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE notification_events SET delivery=?,next_attempt=? WHERE seq=?",
                    (state, time.time() + delay, row["seq"]),
                )
                self.store.write(
                    db, "v2_notifications_status", {"delivery": state, "checked_at": time.time()}
                )
                db.execute(
                    "DELETE FROM notification_events WHERE seq < (SELECT COALESCE(MAX(seq),0)-2000 FROM notification_events)"
                )

    def start(self):
        def loop():
            while not self.workspace.stop_event.wait(1):
                try:
                    self.deliver_once()
                except Exception:
                    self.store.record("notification_delivery_error", {"reason": "delivery_failed"})

        threading.Thread(target=loop, name="notifications", daemon=True).start()


def opportunity_message(op):
    p = op.get("plan") or {}

    def point(v):
        return f"{float(v):,.8f}".rstrip("0").rstrip(".") if v is not None else "未计划"

    targets = p.get("targets", [])
    lines = [
        "QuantPilot · 有效机会（非成交）",
        f"{op['exchange'].upper()} · {op['symbol']}",
        f"方向：{'做多' if op['direction'] == 'long' else '做空'} · {op['score']} / 100",
        f"开仓区间：{point(p.get('entry_low'))} — {point(p.get('entry_high'))}",
        f"止损：{point(p.get('stop'))}",
    ]
    for index, t in enumerate(targets, 1):
        lines.append(
            f"止盈 {index}："
            + (f"{point(t['price'])} · 平仓 {t['ratio'] * 100:g}%" if t.get("enabled", True) else "未计划")
        )
    params = op.get("parameters", {})
    lines += [
        "加仓观察：" + point(p.get("add_at") if params.get("add_enabled", True) else None),
        "减仓观察："
        + point(p.get("reduce_at"))
        + ("" if params.get("price_reduce_enabled", False) else "（自动按价减仓未启用）"),
        f"收益 / 风险：{p.get('rr', '—')} : 1",
        "有效至：" + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(op["expires_at"])),
        "观察点位不代表已下单；实际执行须通过账户授权、AI 开关和风控。",
    ]
    return "\n".join(lines)
