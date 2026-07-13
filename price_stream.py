"""
Live dashboard pricing via WebSocket accountSubscribe.

Watches open Trade rows, subscribes to each Pump.fun bonding-curve account,
and on reserve changes stores USD price into TokenPrice (replaces slow HTTP
interval polling in init_scheduler).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import threading
import time
from typing import Optional

from solders.pubkey import Pubkey
from websockets import connect

from settings import (
    PUMP_FUN_PROGRAM_ID_STR,
    PRICE_HTTP_FALLBACK,
    PRICE_USE_WEBSOCKET,
    get_solana_ws_urls,
    solana_client,
)

logger = logging.getLogger(__name__)

PUMP_FUN_PROGRAM_ID = Pubkey.from_string(PUMP_FUN_PROGRAM_ID_STR)
PYTH_SOL_PRICE_FEED = Pubkey.from_string("7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE")

_WS_RECONNECT_DELAY_SEC = 3
_WS_MAX_RECONNECT_DELAY_SEC = 60
_TRADE_REFRESH_SEC = 20
_SOL_PRICE_REFRESH_SEC = 30
_MIN_STORE_INTERVAL_SEC = 5  # avoid flooding TokenPrice on every micro-update
_MAX_OPEN_TRADES = 50

_listener_thread: Optional[threading.Thread] = None
_listener_running = False
_flask_app = None
_connected = False
_last_message_at: Optional[float] = None
_prices_stored = 0
_tracked_count = 0


def _bonding_curve_pda(mint: str) -> Pubkey:
    mint_pk = Pubkey.from_string(mint)
    pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)], PUMP_FUN_PROGRAM_ID)
    return pda


def _decode_account_data(data_field) -> Optional[bytes]:
    if data_field is None:
        return None
    if isinstance(data_field, list) and data_field:
        if isinstance(data_field[0], str):
            try:
                return base64.b64decode(data_field[0])
            except Exception:
                return None
        if isinstance(data_field[0], int):
            return bytes(data_field)
    if isinstance(data_field, str):
        try:
            return base64.b64decode(data_field)
        except Exception:
            return None
    if isinstance(data_field, (bytes, bytearray)):
        return bytes(data_field)
    return None


def _price_sol_from_bonding_raw(raw: bytes) -> Optional[float]:
    """Parse Pump.fun bonding-curve virtual reserves → price in SOL."""
    for vt_off, vs_off in ((8, 16), (48, 56)):
        if len(raw) < vs_off + 8:
            continue
        vt = int.from_bytes(raw[vt_off : vt_off + 8], "little", signed=False)
        vs = int.from_bytes(raw[vs_off : vs_off + 8], "little", signed=False)
        if vt > 0 and vs > 0:
            return float(vs) / (float(vt) * 1000.0)
    return None


def _fetch_sol_usd() -> Optional[float]:
    try:
        resp = solana_client.get_account_info(PYTH_SOL_PRICE_FEED, encoding="base64")
        value = resp.value
        if value is None or value.data is None:
            return None
        raw = _decode_account_data(value.data)
        if not raw or len(raw) < 93:
            return None
        price_data = struct.unpack_from("<q", raw, 73)[0]
        exponent = struct.unpack_from("<i", raw, 89)[0]
        return float(price_data) * (10.0 ** float(exponent))
    except Exception as exc:
        logger.debug("SOL/USD fetch failed: %s", exc)
        return None


def _load_open_trades() -> list[dict]:
    """Return open trades to price: [{trade_id, mint, token_name, symbol}, ...]."""
    if _flask_app is None:
        return []
    from models import Trade

    with _flask_app.app_context():
        trades = (
            Trade.query.filter_by(executed=False)
            .order_by(Trade.created_at.desc())
            .limit(_MAX_OPEN_TRADES)
            .all()
        )
        return [
            {
                "trade_id": t.id,
                "mint": t.token_address,
                "token_name": t.token_name,
                "symbol": t.token_symbol,
            }
            for t in trades
            if t.token_address
        ]


def _store_price(trade_id: int, mint: str, token_name, symbol, usd_price: float) -> None:
    if _flask_app is None or usd_price is None or usd_price <= 0:
        return
    from models import db, TokenPrice

    global _prices_stored
    try:
        with _flask_app.app_context():
            tp = TokenPrice(
                trade_id=trade_id,
                token_address=mint,
                token_name=token_name,
                symbol=symbol,
                price=usd_price,
            )
            db.session.add(tp)
            db.session.commit()
            _prices_stored += 1
            logger.info(
                "[Price WS] Stored %.10f USD for trade=%s mint=%s...",
                usd_price,
                trade_id,
                mint[:8],
            )
    except Exception as exc:
        logger.error("[Price WS] DB store failed trade=%s: %s", trade_id, exc)
        try:
            with _flask_app.app_context():
                from models import db as _db

                _db.session.rollback()
        except Exception:
            pass


def _add_trade_to_meta(meta: dict, trade_meta: dict) -> None:
    trades = meta.setdefault("trades", [])
    if not any(t.get("trade_id") == trade_meta["trade_id"] for t in trades):
        trades.append(trade_meta)


async def _price_ws_loop() -> None:
    global _connected, _last_message_at, _tracked_count, _listener_running
    reconnect_delay = _WS_RECONNECT_DELAY_SEC
    ws_urls = get_solana_ws_urls()
    url_index = 0

    while _listener_running:
        ws_url = ws_urls[url_index % len(ws_urls)]
        sol_usd: Optional[float] = None
        last_sol_refresh = 0.0
        last_trade_refresh = 0.0
        sub_to_meta: dict[int, dict] = {}
        mint_to_sub: dict[str, int] = {}
        pending_subs: dict[str, dict] = {}
        last_store_at: dict[int, float] = {}

        try:
            logger.info("[Price WS] Connecting to %s", ws_url)
            async with connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                _connected = True
                reconnect_delay = _WS_RECONNECT_DELAY_SEC
                url_index = 0
                logger.info("[Price WS] Connected via %s", ws_url)

                async def request_subscribe(mint: str, trade_meta: dict) -> None:
                    if mint in mint_to_sub:
                        sub_id = mint_to_sub[mint]
                        if sub_id in sub_to_meta:
                            _add_trade_to_meta(sub_to_meta[sub_id], trade_meta)
                        return
                    # Already waiting for ACK for this mint
                    for pending in pending_subs.values():
                        if pending["mint"] == mint:
                            _add_trade_to_meta(pending, trade_meta)
                            return
                    try:
                        pda = str(_bonding_curve_pda(mint))
                    except Exception as exc:
                        logger.debug("[Price WS] Bad mint %s: %s", mint[:8], exc)
                        return
                    req_id = f"price-{mint[:16]}"
                    pending_subs[req_id] = {
                        "mint": mint,
                        "pda": pda,
                        "trades": [trade_meta],
                    }
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": req_id,
                                "method": "accountSubscribe",
                                "params": [
                                    pda,
                                    {"encoding": "base64", "commitment": "processed"},
                                ],
                            }
                        )
                    )

                for trade_meta in _load_open_trades():
                    await request_subscribe(trade_meta["mint"], trade_meta)
                last_trade_refresh = time.time()
                _tracked_count = len(mint_to_sub) + len(pending_subs)

                async for raw in ws:
                    if not _listener_running:
                        break
                    now = time.time()

                    if sol_usd is None or (now - last_sol_refresh) >= _SOL_PRICE_REFRESH_SEC:
                        refreshed = await asyncio.to_thread(_fetch_sol_usd)
                        if refreshed:
                            sol_usd = refreshed
                        last_sol_refresh = now

                    if (now - last_trade_refresh) >= _TRADE_REFRESH_SEC:
                        open_trades = await asyncio.to_thread(_load_open_trades)
                        open_mints = {t["mint"] for t in open_trades}
                        for trade_meta in open_trades:
                            await request_subscribe(trade_meta["mint"], trade_meta)
                        for mint in list(mint_to_sub.keys()):
                            if mint not in open_mints:
                                mint_to_sub.pop(mint, None)
                        last_trade_refresh = now
                        _tracked_count = len(mint_to_sub)

                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if "id" in data and "result" in data and data["id"] in pending_subs:
                        meta = pending_subs.pop(data["id"])
                        sub_id = data["result"]
                        if sub_id is None:
                            continue
                        sub_to_meta[sub_id] = meta
                        mint_to_sub[meta["mint"]] = sub_id
                        _tracked_count = len(mint_to_sub)
                        logger.info(
                            "[Price WS] Watching bonding curve for %s... (sub=%s, trades=%d)",
                            meta["mint"][:8],
                            sub_id,
                            len(meta.get("trades", [])),
                        )
                        continue

                    if "params" not in data:
                        continue

                    result = data["params"].get("result", {})
                    subscription = data["params"].get("subscription")
                    if subscription not in sub_to_meta:
                        continue

                    _last_message_at = now
                    value = result.get("value", {})
                    raw_bytes = _decode_account_data(value.get("data"))
                    if not raw_bytes:
                        continue

                    price_sol = _price_sol_from_bonding_raw(raw_bytes)
                    if price_sol is None or sol_usd is None:
                        continue
                    usd_price = price_sol * sol_usd

                    for trade_meta in sub_to_meta[subscription].get("trades", []):
                        trade_id = trade_meta["trade_id"]
                        if (now - last_store_at.get(trade_id, 0)) < _MIN_STORE_INTERVAL_SEC:
                            continue
                        last_store_at[trade_id] = now
                        await asyncio.to_thread(
                            _store_price,
                            trade_id,
                            trade_meta["mint"],
                            trade_meta.get("token_name"),
                            trade_meta.get("symbol"),
                            usd_price,
                        )

        except asyncio.CancelledError:
            break
        except Exception as exc:
            _connected = False
            logger.error(
                "[Price WS] Error on %s: %s — retry in %ss",
                ws_url,
                exc,
                reconnect_delay,
            )
            url_index += 1
            await asyncio.sleep(reconnect_delay)
            if url_index >= len(ws_urls):
                reconnect_delay = min(reconnect_delay * 2, _WS_MAX_RECONNECT_DELAY_SEC)
                url_index = 0

    _connected = False
    logger.info("[Price WS] Listener stopped.")


def _run_loop() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_price_ws_loop())
    finally:
        loop.close()


def start_price_stream(app) -> bool:
    """Start background WebSocket price listener. Call once from create_scheduler."""
    global _listener_thread, _listener_running, _flask_app
    if not PRICE_USE_WEBSOCKET:
        logger.info("[Price WS] Disabled (PRICE_USE_WEBSOCKET=false).")
        return False
    if _listener_running and _listener_thread and _listener_thread.is_alive():
        return True

    _flask_app = app
    _listener_running = True
    _listener_thread = threading.Thread(
        target=_run_loop,
        daemon=True,
        name="price-ws-listener",
    )
    _listener_thread.start()
    logger.info(
        "[Price WS] Listener thread started (endpoints: %s).",
        get_solana_ws_urls(),
    )
    return True


def stop_price_stream() -> None:
    global _listener_running
    _listener_running = False


def is_price_ws_active() -> bool:
    return _connected and _listener_running


def price_stream_status() -> dict:
    return {
        "connected": _connected,
        "running": _listener_running,
        "tracked_mints": _tracked_count,
        "prices_stored": _prices_stored,
        "last_message_at": _last_message_at,
        "use_websocket": PRICE_USE_WEBSOCKET,
        "http_fallback": PRICE_HTTP_FALLBACK,
    }
