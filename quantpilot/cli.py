from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .config import (
    EXCHANGES,
    Settings,
    atomic_json,
    config_path,
    hash_password,
    load_settings,
    update_settings,
)
from .store import Store, Vault


def initialize(args):
    path = config_path()
    if path.exists():
        raise ValueError("Configuration exists; use qp password to change the password")
    password = getpass.getpass("管理员密码（至少 12 位）: ")
    if password != getpass.getpass("再次输入密码: "):
        raise ValueError("Passwords do not match")
    settings = Settings(username=args.username, password_hash=hash_password(password), port=args.port,
        public_origin=args.origin or f"http://127.0.0.1:{args.port}", host=args.host,
        market_mode="demo" if args.demo else "public", tls_cert=args.tls_cert, tls_key=args.tls_key)
    atomic_json(path, settings.model_dump())
    Vault(Store(path.parent, settings.initial_cash))
    print("初始化完成。真实交易已锁定。运行 qp serve 启动本地控制台。")


def backup(path: Path, destination: Path):
    if destination.exists():
        raise ValueError("Backup path already exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory() as temporary:
        copy = Path(temporary) / "quantpilot.db"
        with sqlite3.connect(path.parent / "quantpilot.db") as src, sqlite3.connect(copy) as dst:
            src.backup(dst)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output, tarfile.open(fileobj=output, mode="w:gz") as archive:
            archive.add(copy, arcname="quantpilot.db")
            archive.add(path, arcname="config.json")
            archive.add(path.parent / "master.key", arcname="master.key")
    print(f"备份已保存：{destination}。包含加密主密钥，请离线保管。")


def cloudflare_rule(domain: str, port: int):
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
        raise ValueError("Invalid domain")
    if not 1024 <= port <= 65535:
        raise ValueError("Port must be 1024–65535")
    return {"ref": "quantpilot_origin_port", "description": "QuantPilot HTTPS origin port",
        "expression": f'(http.host eq "{domain}")', "action": "route", "enabled": True,
        "action_parameters": {"origin": {"port": port}}}


def service(action):
    if action not in ("start", "stop", "restart", "status", "logs"):
        raise ValueError("Unknown service action")
    if not shutil.which("systemctl"):
        raise ValueError("Service management requires Linux with systemd; use qp serve locally")
    command = ["journalctl", "-u", "quantpilot", "-n", "100", "--no-pager"] if action == "logs" else ["systemctl", action, "quantpilot"]
    return subprocess.run(command, check=False).returncode  # noqa: S603


def menu():
    print("\nQuantPilot 管理菜单 · 模拟盘\n1 状态  2 启动  3 停止  4 重启  5 日志\n6 暂停开仓  7 恢复开仓  8 修改密码  9 备份  0 退出")
    choice = input("选择: ").strip()
    actions = {"1": ["status"], "2": ["start"], "3": ["stop"], "4": ["restart"], "5": ["logs"],
               "6": ["kill", "on"], "7": ["kill", "off"], "8": ["password"], "9": ["backup"]}
    if choice in actions:
        return main(actions[choice])
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="QuantPilot: paper-first trading console")
    sub = parser.add_subparsers(dest="command")
    init = sub.add_parser("init", help="Initialize configuration with an interactive password")
    init.add_argument("--username", default="admin")
    init.add_argument("--port", type=int, default=8080)
    init.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")  # noqa: S104
    init.add_argument("--origin")
    init.add_argument("--tls-cert")
    init.add_argument("--tls-key")
    init.add_argument("--demo", action="store_true")
    for name in ("serve", "status", "start", "stop", "restart", "logs", "password", "doctor", "menu"):
        sub.add_parser(name)
    kill = sub.add_parser("kill")
    kill.add_argument("state", choices=["on", "off"])
    credential = sub.add_parser("credential")
    credential.add_argument("provider", choices=[*EXCHANGES, "openai"])
    credential.add_argument("--delete", action="store_true")
    cf = sub.add_parser("cloudflare", help="Print a single rule; does not modify existing Cloudflare rules")
    cf.add_argument("--domain", required=True)
    cf.add_argument("--port", type=int, required=True)
    back = sub.add_parser("backup")
    back.add_argument("--output", type=Path)
    chatgpt = sub.add_parser("chatgpt-login", help="在运行浏览器的电脑上完成 OpenAI 官方登录，再连接自托管服务器")
    chatgpt.add_argument("--server", required=True)
    chatgpt.add_argument("--local-data", type=Path)
    chatgpt.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "chatgpt-login":
            from .chatgpt_login import login

            return login(args)
        if args.command == "init":
            initialize(args)
            return 0
        if args.command == "cloudflare":
            print(json.dumps(cloudflare_rule(args.domain, args.port), indent=2))
            return 0
        if args.command in (None, "menu"):
            return menu()
        if args.command in ("start", "stop", "restart", "logs"):
            return service(args.command)
        settings = load_settings()
        path = config_path()
        store = Store(path.parent, settings.initial_cash)
        if args.command == "serve":
            import uvicorn

            uvicorn.run("quantpilot.app:create_app", factory=True, host=settings.host, port=settings.port,
                        workers=1, proxy_headers=False, access_log=False, log_level="warning",
                        ssl_certfile=settings.tls_cert, ssl_keyfile=settings.tls_key)
        elif args.command == "status":
            with store.connect() as db:
                v2 = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT key,value FROM kv WHERE key IN ('v2_preferences','v2_accounts')")}
            print(json.dumps({"mode": "paper", "live_locked": True, "market_mode": settings.market_mode,
                "origin": settings.public_origin, "kill_switch": store.get("kill"),
                "strategy": store.get("strategy"),
                "workspace": v2.get("v2_preferences"),
                "accounts": [{"id": a["id"], "name": a["name"], "kind": a["kind"], "auto": a["auto"]} for a in (v2.get("v2_accounts") or {}).values()],
                "live_execution_switch": os.getenv("QP_LIVE_EXECUTION") == "1"}, indent=2))
            if shutil.which("systemctl"):
                return service("status")
        elif args.command == "kill":
            store.set_kill(args.state == "on")
            with store.transaction() as db:
                row = db.execute("SELECT value FROM kv WHERE key='v2_preferences'").fetchone()
                if row:
                    prefs = json.loads(row[0])
                    prefs["halted"] = args.state == "on"
                    store.write(db, "v2_preferences", prefs)
                    if args.state == "on":
                        accounts = store.read(db, "v2_accounts")
                        for account in accounts.values():
                            account["auto"] = False
                        store.write(db, "v2_accounts", accounts)
                    store.audit(db, "v2_cli_halt", {"halted": prefs["halted"]})
            print("开仓暂停。减仓和保护退出仍可执行。" if args.state == "on" else "开仓限制已解除，策略仍需手动启用。")
        elif args.command == "password":
            password = getpass.getpass("新密码（至少 12 位）: ")
            if password != getpass.getpass("再次输入密码: "):
                raise ValueError("Passwords do not match")
            update_settings(path, {"password_hash": hash_password(password)})
            with store.transaction() as db:
                db.execute("DELETE FROM sessions")
                store.audit(db, "password_changed", {})
            print("密码已更新并立即生效，旧会话已撤销。")
        elif args.command == "credential":
            vault = Vault(store)
            if args.delete:
                vault.delete(args.provider)
            else:
                credentials = {"apiKey": getpass.getpass("API Key: ")}
                if not credentials["apiKey"]:
                    raise ValueError("Key cannot be empty")
                if args.provider != "openai":
                    credentials["secret"] = getpass.getpass("API Secret（只读权限）: ")
                    if not credentials["secret"]:
                        raise ValueError("Secret cannot be empty")
                    passphrase = getpass.getpass("Passphrase（不需要则留空）: ")
                    if passphrase:
                        credentials["password"] = passphrase
                vault.put(args.provider, credentials)
            print("凭据已更新，不会回显。")
        elif args.command == "backup":
            destination = args.output or path.parent / "backups" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".tar.gz")
            backup(path, destination.resolve())
        elif args.command == "doctor":
            import ccxt

            Vault(store)
            with store.connect() as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            print(json.dumps({"python": sys.version.split()[0], "ccxt": ccxt.__version__,
                              "database": integrity, "live_locked": True,
                              "tls": bool(settings.tls_cert), "secure_cookie": settings.secure_cookie}, indent=2))
            return 0 if integrity == "ok" else 1
    except (ValueError, OSError):
        # ValidationError can include secret input; avoid printing exception text.
        print("操作失败：请检查配置、输入格式、文件权限或运行 qp --help。敏感信息未输出。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
