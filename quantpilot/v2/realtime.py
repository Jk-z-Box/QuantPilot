"""Authenticated read-only browser streaming, with bounded latest-state coalescing."""

from __future__ import annotations

import asyncio
import copy
import json
import secrets
import time
from contextlib import suppress
from urllib.parse import urlsplit

import anyio
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from .models import FRAMES


def install(app, workspace, session):
    epoch = secrets.token_hex(12)

    def subscription(value):
        key, frame = value.get("instrument_id", ""), value.get("frame", "1h")
        if not isinstance(key, str) or len(key) > 64 or frame not in FRAMES:
            raise ValueError("invalid_subscription")
        if key and key not in {i["id"] for i in workspace.store.get("v2_watchlist")}:
            raise ValueError("unknown_instrument")
        return {"instrument_id": key, "frame": frame}

    async def packets(connection, selected):
        queue = workspace.funds.subscribe()
        previous = {"quotes": {}, "opportunities": {}, "market_status": {}, "candles": {}}
        sequence, last_auth, last_sent, first = 0, 0, 0, True
        watch, watch_at = {}, 0
        try:
            while True:
                now = time.time()
                if now - last_auth >= 5:
                    await asyncio.to_thread(session, connection)
                    last_auth = now
                # Display subscriptions change rarely; market values still update every cycle.
                if now - watch_at >= 1 or (
                    selected["instrument_id"] and selected["instrument_id"] not in watch
                ):
                    watch = {i["id"]: i for i in workspace.store.get("v2_watchlist")}
                    watch_at = now
                with workspace.lock:
                    current = {
                        "quotes": copy.deepcopy(workspace.quotes),
                        "opportunities": copy.deepcopy(workspace.opportunities),
                    }
                for key, op in current["opportunities"].items():
                    q = current["quotes"].get(key, {})
                    if (
                        op.get("expires_at", 0) <= now
                        or q.get("error")
                        or not -5 <= now - q.get("timestamp", 0) <= 15
                    ):
                        op["valid"] = False
                        if "行情或机会已过期" not in op.get("blockers", []):
                            op.setdefault("blockers", []).append("行情或机会已过期")
                current["market_status"] = {i["id"]: workspace.streams.status(i) for i in watch.values()}
                key, frame = selected["instrument_id"], selected["frame"]
                item = watch.get(key)
                bars = workspace.streams.merge_candles(item, frame, []) if item else []
                current["candles"] = {"instrument_id": key, "frame": frame, "rows": bars}
                changed = {}
                for name, value in current.items():
                    if first or value != previous[name]:
                        if name in ("quotes", "opportunities", "market_status") and not first:
                            changed[name] = {k: v for k, v in value.items() if previous[name].get(k) != v}
                            removed = sorted(set(previous[name]) - set(value))
                            if removed:
                                changed[name + "_removed"] = removed
                        else:
                            changed[name] = value
                        previous[name] = value
                while not queue.empty():
                    changed["accounts"] = queue.get_nowait()["accounts"]
                if changed or now - last_sent >= 5:
                    sequence += 1
                    yield {
                        "type": "snapshot" if first else "delta" if changed else "heartbeat",
                        "epoch": epoch,
                        "sequence": sequence,
                        "published_at": time.time(),
                        **changed,
                    }
                    first, last_sent = False, now
                await asyncio.sleep(0.05)
        finally:
            workspace.funds.unsubscribe(queue)

    @app.websocket("/api/v2/realtime")
    async def realtime(socket: WebSocket):
        origin = urlsplit(socket.headers.get("origin", ""))
        if (
            origin.scheme not in ("https", "http")
            or origin.netloc != socket.headers.get("host")
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
        ):
            await socket.close(code=1008)
            return
        try:
            session(socket)
        except HTTPException:
            await socket.close(code=1008)
            return
        await socket.accept()
        selected = {"instrument_id": "", "frame": "1h"}

        async def send():
            async for packet in packets(socket, selected):
                # Slow consumers are disconnected rather than buffering an unbounded backlog.
                await asyncio.wait_for(socket.send_json(packet), 5)

        async def receive():
            last_command = 0
            while True:
                text = await asyncio.wait_for(socket.receive_text(), 45)
                if len(text) > 512:
                    raise ValueError("oversized_command")
                msg = json.loads(text)
                if not isinstance(msg, dict):
                    raise ValueError("invalid_command")
                if msg.get("type") == "ping":
                    continue
                if msg.get("type") != "subscribe":
                    raise ValueError("invalid_command")
                # Fast control changes are coalesced; never disconnect a normal UI interaction.
                pause = 0.1 - (time.monotonic() - last_command)
                if pause > 0:
                    await asyncio.sleep(pause)
                session(socket)
                selected.update(subscription(msg))
                last_command = time.monotonic()

        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            pass
        except (WebSocketDisconnect, RuntimeError, TimeoutError):
            pass
        except HTTPException:
            with suppress(RuntimeError):
                await socket.close(code=1008)
        except (ValueError, TypeError):
            with suppress(RuntimeError):
                await socket.close(code=1008)
        finally:
            with anyio.CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                with suppress(RuntimeError, WebSocketDisconnect):
                    await socket.close()

    @app.get("/api/v2/realtime-events")
    async def realtime_events(request: Request, instrument_id: str = "", frame: str = "1h"):
        session(request)
        try:
            selected = subscription({"instrument_id": instrument_id, "frame": frame})
        except ValueError:
            raise HTTPException(422, "订阅参数无效") from None

        async def events():
            try:
                async for packet in packets(request, selected):
                    if await request.is_disconnected():
                        return
                    yield "event: realtime\ndata: " + json.dumps(packet) + "\n\n"
            except HTTPException:
                yield "event: expired\ndata: {}\n\n"
            except ValueError:
                yield "event: reconnect\ndata: {}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store, no-transform"},
        )
