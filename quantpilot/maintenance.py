"""Fixed-repository update requests. The web process never executes a shell."""

from __future__ import annotations

import json
import re
import secrets
import time
import tomllib
from pathlib import Path

import httpx

from .config import atomic_json

VERSION = "0.6.0"
REPOSITORY = "Jk-z-Box/QuantPilot"
BRANCH = "feat/mission-control-v2"
STATUS = Path("/var/lib/quantpilot-maintenance/status.json")


def latest(transport=None):
    try:
        with httpx.Client(timeout=15, follow_redirects=False, transport=transport) as client:
            result = client.get(
                f"https://api.github.com/repos/{REPOSITORY}/commits/{BRANCH}",
                headers={"Accept": "application/vnd.github+json"},
            )
            result.raise_for_status()
            commit = result.json()["sha"]
            if not re.fullmatch(r"[a-f0-9]{40}", commit):
                raise ValueError("invalid commit")
            result = client.get(f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/pyproject.toml")
            result.raise_for_status()
            if len(result.content) > 65536:
                raise ValueError("oversized metadata")
            version = tomllib.loads(result.text)["project"]["version"]
            if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise ValueError("invalid version")
        return {
            "version": version,
            "commit": commit,
            "repository": REPOSITORY,
            "branch": BRANCH,
            "url": f"https://github.com/{REPOSITORY}/commit/{commit}",
            "checked_at": time.time(),
            "newer": tuple(map(int, version.split("."))) > tuple(map(int, VERSION.split("."))),
        }
    except Exception:
        raise ValueError("无法读取 GitHub 更新，请稍后重试；当前版本继续运行") from None


def status():
    try:
        data = json.loads(STATUS.read_text())
        return {k: data[k] for k in ("state", "message", "at", "commit", "id") if k in data}
    except (OSError, ValueError):
        return {"state": "unavailable", "message": "未安装 Linux 网页更新服务，可使用部署命令更新"}


def can_update(store, db):
    accounts = store.read(db, "v2_accounts")
    if any(a["auto"] for a in accounts.values()) or store.read(db, "strategy")["enabled"]:
        raise ValueError("请先暂停所有账户的自动交易，再更新系统")
    if store.read(db, "account")["positions"]:
        raise ValueError("旧版模拟账本仍有持仓，暂不能更新")
    if any(b["positions"] for b in store.read(db, "v2_ledgers").values()) or any(
        store.read(db, "v2_managed").values()
    ):
        raise ValueError("有持仓时不能重启更新，请先完成平仓并核对")
    if any(s.get("positions") for s in store.read(db, "v2_external").values()):
        raise ValueError("交易所仍有仓位，请先核对账户")
    if db.execute("SELECT 1 FROM v2_orders WHERE state IN ('sending','unknown') LIMIT 1").fetchone():
        raise ValueError("存在待核对订单，暂不能更新")


def queue_update(store, commit):
    if status()["state"] == "unavailable":
        raise ValueError("本机没有可用的网页更新服务")
    checked = latest()
    if not checked["newer"] or checked["commit"] != commit:
        raise ValueError("更新版本已变化或无需升级，请重新检查更新")
    path = store.directory / "update-request.json"
    with store.transaction() as db:
        can_update(store, db)
        if path.exists() or status()["state"] == "running":
            raise ValueError("已有更新请求正在处理")
        request = {"id": secrets.token_hex(12), "commit": commit, "created_at": time.time()}
        atomic_json(path, request)
        store.audit(db, "update_requested", request)
    return {"state": "queued", "message": "已排队，约一分钟内开始；更新会备份并重启服务", **request}
