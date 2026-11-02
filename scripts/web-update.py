#!/usr/bin/env python3
"""Root timer: consume one bounded request, verify trusted HEAD, run pinned installer."""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import stat
import subprocess
import time
from pathlib import Path

import httpx

from quantpilot.config import atomic_json
from quantpilot.maintenance import REPOSITORY, can_update, latest
from quantpilot.store import Store

ROOT = Path("/var/lib/quantpilot-maintenance")
DATA = Path("/var/lib/quantpilot")


def report(state, message, request=None):
    target = ROOT / "status.json"
    atomic_json(
        target,
        {
            "state": state,
            "message": message,
            "at": time.time(),
            **{k: v for k, v in (request or {}).items() if k in ("id", "commit")},
        },
    )
    os.chmod(target, 0o644)


def read_request(path, owner):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner
            or info.st_size > 1024
            or info.st_nlink != 1
        ):
            raise ValueError("invalid request file")
        body = json.loads(stream.read(1025))
    if (
        set(body) != {"id", "commit", "created_at"}
        or not re.fullmatch(r"[a-f0-9]{24}", body["id"])
        or not re.fullmatch(r"[a-f0-9]{40}", body["commit"])
        or not 0 <= time.time() - body["created_at"] < 900
    ):
        raise ValueError("invalid request")
    return body


def main():
    if os.geteuid() != 0:
        raise SystemExit("Root service only")
    ROOT.mkdir(mode=0o755, exist_ok=True)
    with (ROOT / "worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        path = DATA / "update-request.json"
        if not path.exists():
            if not (ROOT / "status.json").exists():
                report("ready", "网页更新服务就绪")
            return
        request = None
        try:
            request = read_request(path, pwd.getpwnam("quantpilot").pw_uid)
            path.unlink()
            checked = latest()
            if not checked["newer"] or checked["commit"] != request["commit"]:
                raise ValueError("update changed")
            # Treat the writable trading directory as untrusted: database work
            # runs with the trading user's privileges, not root file access.
            account = pwd.getpwnam("quantpilot")
            os.setegid(account.pw_gid)
            os.seteuid(account.pw_uid)
            try:
                store = Store(DATA)
                with store.transaction() as db:
                    can_update(store, db)
                    prefs = store.read(db, "v2_preferences")
                    prefs["halted"] = True
                    store.write(db, "v2_preferences", prefs)
                    store.write(db, "kill", True)
            finally:
                os.seteuid(0)
                os.setegid(0)
            report("running", "正在备份并安装固定版本，请勿重复操作", request)
            with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                result = client.get(
                    f"https://raw.githubusercontent.com/{REPOSITORY}/{request['commit']}/install.sh"
                )
                result.raise_for_status()
                if len(result.content) > 65536:
                    raise ValueError("oversized installer")
            installer = ROOT / "install.sh"
            installer.write_bytes(result.content)
            os.chmod(installer, 0o600)
            with (ROOT / "install.log").open("w") as log:
                os.chmod(log.name, 0o600)
                result = subprocess.run(  # noqa: S603 — fixed installer and validated trusted commit
                    [
                        "/bin/bash",
                        str(installer),
                        "--ref",
                        request["commit"],
                        "--upgrade",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=1800,
                    check=False,
                    cwd=ROOT,
                    env={
                        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                        "HOME": "/root",
                    },
                )
            if result.returncode:
                raise ValueError("installer failed")
            report("completed", "更新完成；请刷新网页。自动开仓保持暂停，核对后再启用", request)
        except Exception:
            path.unlink(missing_ok=True)
            report(
                "failed",
                "更新未完成，未自动恢复开仓；请检查 sudo journalctl -u quantpilot-update 与受保护的安装日志",
                request,
            )


if __name__ == "__main__":
    main()
