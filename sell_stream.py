"""
Instant sell trigger: tell the sell bot the moment an open trade's token trades.

Keeps one logsSubscribe (mentions=[mint], commitment=confirmed) per open
auto-snipe trade on the node's WebSocket. Every notification for a
successful transaction calls on_activity(mint). The set of open mints is
re-synced every sync_interval seconds: new buys get subscribed, closed
trades get unsubscribed. Dead tokens produce no notifications, so they cost
nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Callable, Optional

from websockets import connect

from settings import get_solana_ws_urls

logger = logging.getLogger(__name__)

_RECONNECT_DELAY_SEC = 3
_MAX_RECONNECT_DELAY_SEC = 60


class SellStream:
    def __init__(
        self,
        get_open_mints: Callable[[], set],
        on_activity: Callable[[str], None],
        sync_interval: float = 5.0,
        ws_urls: Optional[list] = None,
    ) -> None:
        self._get_open_mints = get_open_mints
        self._on_activity = on_activity
        self._sync_interval = sync_interval
        self._ws_urls = ws_urls or get_solana_ws_urls()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.subscribed: dict = {}  # mint -> subscription id
        self.notifications = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_thread, name="sell-stream", daemon=True)
        self._thread.start()
        logger.info("[SellStream] started (endpoints: %s)", self._ws_urls)

    def stop(self) -> None:
        self._running = False

    def _run_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        finally:
            loop.close()

    async def _run(self) -> None:
        url_index = 0
        delay = _RECONNECT_DELAY_SEC
        while self._running:
            url = self._ws_urls[url_index % len(self._ws_urls)]
            try:
                async with connect(url, ping_interval=20, ping_timeout=20, max_size=None) as ws:
                    self.connected = True
                    delay = _RECONNECT_DELAY_SEC
                    logger.info("[SellStream] connected to %s", url)
                    await self._session(ws)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[SellStream] connection error on %s: %s — retrying in %ss", url, exc, delay)
                url_index += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, _MAX_RECONNECT_DELAY_SEC)
            finally:
                self.connected = False
                self.subscribed.clear()
        logger.info("[SellStream] stopped")

    async def _session(self, ws) -> None:
        sub_to_mint: dict = {}
        pending: dict = {}  # request id -> mint awaiting a subscription id
        next_sync = 0.0
        req_counter = 0
        loop = asyncio.get_running_loop()

        while self._running:
            if time.monotonic() >= next_sync:
                next_sync = time.monotonic() + self._sync_interval
                try:
                    wanted = set(await loop.run_in_executor(None, self._get_open_mints))
                except Exception as exc:
                    logger.warning("[SellStream] could not load open trades: %s", exc)
                    wanted = None
                if wanted is not None:
                    requested = set(pending.values())
                    for mint in wanted - set(self.subscribed) - requested:
                        req_counter += 1
                        rid = f"sellsub-{req_counter}"
                        pending[rid] = mint
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0",
                            "id": rid,
                            "method": "logsSubscribe",
                            "params": [{"mentions": [mint]}, {"commitment": "confirmed"}],
                        }))
                    for mint in set(self.subscribed) - wanted:
                        sub_id = self.subscribed.pop(mint)
                        sub_to_mint.pop(sub_id, None)
                        req_counter += 1
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0",
                            "id": f"sellunsub-{req_counter}",
                            "method": "logsUnsubscribe",
                            "params": [sub_id],
                        }))

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue

            if "id" in data:
                mint = pending.pop(data.get("id"), None)
                if mint is not None:
                    sub_id = data.get("result")
                    if isinstance(sub_id, int):
                        self.subscribed[mint] = sub_id
                        sub_to_mint[sub_id] = mint
                    else:
                        logger.warning("[SellStream] subscribe failed for %s: %s", mint, data.get("error"))
                continue

            params = data.get("params") or {}
            mint = sub_to_mint.get(params.get("subscription"))
            if mint is None:
                continue
            value = (params.get("result") or {}).get("value") or {}
            if value.get("err") is not None:
                continue  # failed transaction: nothing changed on-chain
            self.notifications += 1
            try:
                self._on_activity(mint)
            except Exception as exc:
                logger.error("[SellStream] on_activity error for %s: %s", mint, exc)
