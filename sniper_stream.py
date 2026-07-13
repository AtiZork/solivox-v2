"""
Phase 1: Pump.fun sniper data ingestion via WebSocket (logsSubscribe).

Replaces HTTP polling in detect_new_tokens_single_pass and get_token_specific_transactions
with a real-time stream. RPC get_transaction is only used event-driven when a Create/Buy
log arrives (not on a timer).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey
from solders.signature import Signature
from websockets import connect

from settings import (
    PUMP_FUN_PROGRAM_ID_STR,
    SNIPER_HTTP_FALLBACK,
    SNIPER_USE_WEBSOCKET,
    get_solana_ws_urls,
    solana_client,
)

logger = logging.getLogger(__name__)

PUMP_FUN_PROGRAM_ID = Pubkey.from_string(PUMP_FUN_PROGRAM_ID_STR)
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Reconnect backoff
_WS_RECONNECT_DELAY_SEC = 3
_WS_MAX_RECONNECT_DELAY_SEC = 60

# Public RPC is heavily rate-limited; throttle sniper get_transaction calls.
_RPC_MIN_INTERVAL_SEC = 0.25
_RPC_LOCK = threading.Lock()
_LAST_RPC_AT = 0.0

# Async loop bridge: subscribe to per-mint buy streams after Create.
_ws_loop: Optional[asyncio.AbstractEventLoop] = None
_mint_subscribe_queue: Optional[asyncio.Queue] = None

# Base58 pump.fun mint addresses end with "pump"
_PUMP_MINT_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}pump")


@dataclass
class BuyEvent:
    signature: str
    sol_amount: float
    usd_value: float
    timestamp: float


@dataclass
class MintTracker:
    mint: str
    launch_time: float
    create_signature: str
    buys: list[BuyEvent] = field(default_factory=list)


class SniperStreamState:
    """Thread-safe in-memory state updated by the WebSocket listener."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.processed_mints: set[str] = set()
        self.seen_signatures: set[str] = set()
        self.mint_registry: dict[str, MintTracker] = {}
        self.connected: bool = False
        self.last_message_at: Optional[float] = None
        self.messages_received: int = 0
        self.creates_detected: int = 0
        self.buys_detected: int = 0

    def mark_signature_seen(self, signature: str) -> bool:
        with self._lock:
            if signature in self.seen_signatures:
                return False
            self.seen_signatures.add(signature)
            if len(self.seen_signatures) > 50_000:
                # Prevent unbounded growth
                self.seen_signatures = set(list(self.seen_signatures)[-25_000:])
            return True

    def register_launch(self, mint: str, create_signature: str) -> bool:
        with self._lock:
            if mint in self.processed_mints or mint in self.mint_registry:
                return False
            self.mint_registry[mint] = MintTracker(
                mint=mint,
                launch_time=time.time(),
                create_signature=create_signature,
            )
            self.creates_detected += 1
            return True

    def mark_processed(self, mint: str) -> None:
        with self._lock:
            self.processed_mints.add(mint)

    def is_processed(self, mint: str) -> bool:
        with self._lock:
            return mint in self.processed_mints

    def has_tracked_mints(self) -> bool:
        with self._lock:
            return bool(self.mint_registry)

    def get_tracked_mint_set(self) -> set[str]:
        with self._lock:
            return set(self.mint_registry.keys())

    def add_buy(self, mint: str, buy: BuyEvent) -> None:
        with self._lock:
            tracker = self.mint_registry.get(mint)
            if tracker is None:
                return
            tracker.buys.append(buy)
            self.buys_detected += 1

    def get_buy_stats(self, mint: str, min_usd_value: float) -> dict:
        with self._lock:
            tracker = self.mint_registry.get(mint)
            if tracker is None:
                return {
                    "total_buys": 0,
                    "qualifying_count": 0,
                    "launch_time": None,
                    "qualifying_buys": [],
                }
            qualifying = [b for b in tracker.buys if b.usd_value >= min_usd_value]
            return {
                "total_buys": len(tracker.buys),
                "qualifying_count": len(qualifying),
                "launch_time": tracker.launch_time,
                "qualifying_buys": qualifying,
            }

    def note_message(self) -> None:
        with self._lock:
            self.messages_received += 1
            self.last_message_at = time.time()

    def status(self) -> dict:
        with self._lock:
            return {
                "connected": self.connected,
                "messages_received": self.messages_received,
                "creates_detected": self.creates_detected,
                "buys_detected": self.buys_detected,
                "tracked_mints": len(self.mint_registry),
                "processed_mints": len(self.processed_mints),
                "last_message_at": self.last_message_at,
            }


stream_state = SniperStreamState()

_listener_thread: Optional[threading.Thread] = None
_listener_running = False
_on_new_token: Optional[Callable[[str], None]] = None
_on_connected: Optional[Callable[[], None]] = None
_connected_callback_fired = False
_sol_usd_price: float = 0.0
_sol_price_updated_at: float = 0.0


def _get_sol_usd_price() -> float:
    global _sol_usd_price, _sol_price_updated_at
    now = time.time()
    if _sol_usd_price > 0 and (now - _sol_price_updated_at) < 60:
        return _sol_usd_price
    try:
        from utils import get_token_symbol_and_price

        data = get_token_symbol_and_price(WSOL_MINT)
        if data and data.get("usdPrice"):
            _sol_usd_price = float(data["usdPrice"])
            _sol_price_updated_at = now
    except Exception as exc:
        logger.warning("Failed to refresh SOL USD price: %s", exc)
    return _sol_usd_price or 150.0


def _logs_contain(logs: list, needle: str) -> bool:
    return any(needle in line for line in logs)


def _as_signature(signature: str | Signature) -> Signature:
    """WebSocket log notifications return signature strings; solana-py expects Signature."""
    if isinstance(signature, Signature):
        return signature
    return Signature.from_string(str(signature))


def _rpc_throttle() -> None:
    """Serialize and pace RPC calls to avoid 429 on public mainnet."""
    global _LAST_RPC_AT
    with _RPC_LOCK:
        elapsed = time.time() - _LAST_RPC_AT
        if elapsed < _RPC_MIN_INTERVAL_SEC:
            time.sleep(_RPC_MIN_INTERVAL_SEC - elapsed)
        _LAST_RPC_AT = time.time()


def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "too many request" in msg


def _extract_pump_mints_from_logs(logs: list) -> list[str]:
    """Try to find pump.fun mint addresses in log lines (no RPC)."""
    seen: set[str] = set()
    result: list[str] = []
    for line in logs:
        for match in _PUMP_MINT_RE.finditer(line):
            mint = match.group(0)
            if mint not in seen:
                seen.add(mint)
                result.append(mint)
    return result


def _get_transaction_with_retry(signature: str, *, max_attempts: int = 5):
    """Fetch transaction with pacing, retries on 429, backoff when tx not indexed yet."""
    delays = (0.0, 0.3, 0.6, 1.0, 2.0)
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(delays[min(attempt, len(delays) - 1)])
        try:
            _rpc_throttle()
            resp = solana_client.get_transaction(
                _as_signature(signature),
                encoding="jsonParsed",
                commitment=Confirmed,
                max_supported_transaction_version=0,
            )
            if resp.value is not None:
                return resp.value
        except Exception as exc:
            if _is_rate_limit_error(exc):
                logger.debug("RPC rate limited on %s (attempt %d)", signature[:16], attempt + 1)
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.debug(
                "get_transaction attempt %d for %s: %s",
                attempt + 1,
                signature[:16],
                exc,
            )
    return None


def _mint_from_tx_value(tx) -> Optional[str]:
    if tx is None:
        return None
    account_keys = tx.transaction.transaction.message.account_keys
    for key in account_keys:
        addr = str(key)
        if addr.endswith("pump"):
            return addr
    meta = tx.transaction.meta
    if meta and meta.post_token_balances:
        for bal in meta.post_token_balances:
            mint = str(bal.mint)
            if mint.endswith("pump"):
                return mint
    return None


def _extract_mint_from_create(signature: str, logs: list) -> Optional[str]:
    """Resolve mint from WS logs first, then RPC with retries."""
    log_mints = _extract_pump_mints_from_logs(logs)
    if log_mints:
        return log_mints[0]
    tx = _get_transaction_with_retry(signature)
    return _mint_from_tx_value(tx)


def _extract_mint_from_create_tx(signature: str) -> Optional[str]:
    """Legacy wrapper — prefer _extract_mint_from_create(signature, logs)."""
    tx = _get_transaction_with_retry(signature)
    return _mint_from_tx_value(tx)


def _extract_mint_and_sol_from_buy_tx(signature: str) -> tuple[Optional[str], float]:
    """Event-driven RPC: resolve mint and SOL spent for a Pump.fun buy."""
    tx = _get_transaction_with_retry(signature, max_attempts=3)
    if tx is None or tx.transaction.meta is None:
        return None, 0.0

    mint = _mint_from_tx_value(tx)
    meta = tx.transaction.meta
    sol_spent = 0.0
    if meta.pre_balances and meta.post_balances and len(meta.pre_balances) > 0:
        lamport_delta = meta.pre_balances[0] - meta.post_balances[0]
        if lamport_delta > 0:
            sol_spent = lamport_delta / 1_000_000_000
    return mint, sol_spent


def _process_create(signature: str, logs: list) -> None:
    mint = _extract_mint_from_create(signature, logs)
    if not mint:
        logger.warning("[WS] Create detected but mint not resolved: %s", signature[:16])
        return

    if not stream_state.register_launch(mint, signature):
        return

    logger.info("[WS] New Pump.fun token: %s (sig %s...)", mint, signature[:16])
    _request_mint_subscription(mint)
    if _on_new_token:
        threading.Thread(
            target=_run_new_token_callback,
            args=(mint,),
            daemon=True,
            name=f"sniper-token-{mint[:8]}",
        ).start()


def _handle_create(signature: str, logs: list) -> None:
    if not _logs_contain(logs, "Instruction: Create"):
        return
    if not stream_state.mark_signature_seen(signature):
        return
    # Resolve mint via RPC in background so the WS loop stays responsive.
    threading.Thread(
        target=_process_create,
        args=(signature, list(logs)),
        daemon=True,
        name=f"sniper-create-{signature[:8]}",
    ).start()


def _run_new_token_callback(mint: str) -> None:
    try:
        _on_new_token(mint)
    except Exception as exc:
        logger.error("on_new_token callback error for %s: %s", mint, exc, exc_info=True)


def _request_mint_subscription(mint: str) -> None:
    """Schedule a per-mint logsSubscribe on the WS loop (buys for this token only)."""
    if _ws_loop is None or _mint_subscribe_queue is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_mint_subscribe_queue.put(mint), _ws_loop)
    except Exception as exc:
        logger.warning("Failed to schedule mint subscription for %s: %s", mint[:16], exc)


def _process_mint_buy(mint: str, signature: str, logs: list) -> None:
    if not _logs_contain(logs, "Instruction: Buy"):
        return
    if not stream_state.mark_signature_seen(signature):
        return

    _, sol_amount = _extract_mint_and_sol_from_buy_tx(signature)
    if sol_amount <= 0:
        return

    sol_usd = _get_sol_usd_price()
    usd_value = sol_amount * sol_usd
    buy = BuyEvent(
        signature=signature,
        sol_amount=sol_amount,
        usd_value=usd_value,
        timestamp=time.time(),
    )
    stream_state.add_buy(mint, buy)
    stats = stream_state.get_buy_stats(mint, min_usd_value=0)
    logger.info(
        "[WS] Buy on %s: %.4f SOL (~$%.2f) | total buys=%d (sig %s...)",
        mint[:8],
        sol_amount,
        usd_value,
        stats["total_buys"],
        signature[:16],
    )


def _handle_mint_buy(mint: str, signature: str, logs: list) -> None:
    threading.Thread(
        target=_process_mint_buy,
        args=(mint, signature, list(logs)),
        daemon=True,
        name=f"sniper-buy-{mint[:8]}",
    ).start()


async def _pump_fun_ws_loop() -> None:
    global _listener_running, _connected_callback_fired, _ws_loop, _mint_subscribe_queue
    reconnect_delay = _WS_RECONNECT_DELAY_SEC
    pump_id = str(PUMP_FUN_PROGRAM_ID)
    ws_urls = get_solana_ws_urls()
    url_index = 0

    while _listener_running:
        ws_url = ws_urls[url_index % len(ws_urls)]
        mint_sub_ids: dict[int, str] = {}
        pending_sub_requests: dict[str, str] = {}
        mint_subscribe_queue: asyncio.Queue = asyncio.Queue()
        _mint_subscribe_queue = mint_subscribe_queue
        _ws_loop = asyncio.get_running_loop()

        async def drain_mint_subscribe_queue(ws) -> None:
            while True:
                try:
                    mint = mint_subscribe_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                req_id = f"sub-{mint[:12]}"
                if req_id in pending_sub_requests:
                    continue
                pending_sub_requests[req_id] = mint
                subscribe_mint = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [mint]},
                        {"commitment": "processed"},
                    ],
                }
                await ws.send(json.dumps(subscribe_mint))
                logger.debug("[WS] Requesting buy stream for %s", mint[:16])

        try:
            logger.info("[WS] Connecting to %s", ws_url)
            async with connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                subscribe_request = {
                    "jsonrpc": "2.0",
                    "id": "pump-program",
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [pump_id]},
                        {"commitment": "processed"},
                    ],
                }
                await ws.send(json.dumps(subscribe_request))
                response = json.loads(await ws.recv())
                sub_id = response.get("result")
                if not sub_id:
                    raise RuntimeError(f"logsSubscribe failed: {response}")

                stream_state.connected = True
                reconnect_delay = _WS_RECONNECT_DELAY_SEC
                url_index = 0
                logger.info("[WS] Subscribed to Pump.fun logs via %s (id=%s)", ws_url, sub_id)

                if _on_connected and not _connected_callback_fired:
                    _connected_callback_fired = True
                    try:
                        _on_connected()
                    except Exception as exc:
                        logger.error("on_connected callback error: %s", exc)

                async for raw in ws:
                    if not _listener_running:
                        break
                    await drain_mint_subscribe_queue(ws)
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # logsSubscribe JSON-RPC response (per-mint buy stream)
                    if "id" in data and "result" in data:
                        req_id = data.get("id")
                        if isinstance(req_id, str) and req_id in pending_sub_requests:
                            mint = pending_sub_requests.pop(req_id)
                            mint_sub = data.get("result")
                            if mint_sub:
                                mint_sub_ids[mint_sub] = mint
                                logger.info(
                                    "[WS] Buy stream active for %s (sub=%s)",
                                    mint[:16],
                                    mint_sub,
                                )
                        continue

                    if "params" not in data:
                        continue

                    result = data["params"].get("result", {})
                    subscription = data["params"].get("subscription")
                    value = result.get("value", {})
                    signature = value.get("signature")
                    logs = value.get("logs", [])
                    if not signature or not logs:
                        continue

                    stream_state.note_message()
                    if subscription in mint_sub_ids:
                        _handle_mint_buy(mint_sub_ids[subscription], signature, logs)
                    else:
                        _handle_create(signature, logs)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            stream_state.connected = False
            logger.error(
                "[WS] Listener error on %s: %s — trying next endpoint in %ss",
                ws_url,
                exc,
                reconnect_delay,
            )
            url_index += 1
            await asyncio.sleep(reconnect_delay)
            if url_index >= len(ws_urls):
                reconnect_delay = min(reconnect_delay * 2, _WS_MAX_RECONNECT_DELAY_SEC)
                url_index = 0
        finally:
            stream_state.connected = False
            _ws_loop = None
            _mint_subscribe_queue = None

    logger.info("[WS] Pump.fun listener stopped.")


def _run_listener_thread() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_pump_fun_ws_loop())
    finally:
        loop.close()


def start_sniper_stream(
    on_new_token: Callable[[str], None],
    on_connected: Optional[Callable[[], None]] = None,
) -> bool:
    """
    Start the WebSocket listener in a background daemon thread.
    Returns True if the thread was started.
    """
    global _listener_thread, _listener_running, _on_new_token, _on_connected, _connected_callback_fired

    if not SNIPER_USE_WEBSOCKET:
        logger.info("[WS] SNIPER_USE_WEBSOCKET=false — skipping WebSocket listener.")
        return False

    if _listener_thread and _listener_thread.is_alive():
        logger.info("[WS] Listener already running.")
        return True

    _on_new_token = on_new_token
    _on_connected = on_connected
    _connected_callback_fired = False
    _listener_running = True
    _listener_thread = threading.Thread(target=_run_listener_thread, name="sniper-ws", daemon=True)
    _listener_thread.start()
    logger.info("[WS] Sniper WebSocket listener thread started (endpoints: %s).", get_solana_ws_urls())
    return True


def stop_sniper_stream() -> None:
    global _listener_running
    _listener_running = False
    stream_state.connected = False
    logger.info("[WS] Stop signal sent to sniper WebSocket listener.")


def wait_for_connection(timeout_sec: float = 15.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if stream_state.connected:
            return True
        time.sleep(0.25)
    return stream_state.connected


def get_mint_buy_stats(mint: str, min_usd_value: float) -> dict:
    return stream_state.get_buy_stats(mint, min_usd_value)


def is_ws_active() -> bool:
    return SNIPER_USE_WEBSOCKET and _listener_thread is not None and _listener_thread.is_alive()


def should_use_http_fallback() -> bool:
    return SNIPER_HTTP_FALLBACK and not SNIPER_USE_WEBSOCKET
