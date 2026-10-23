"""
Persistent, program-wide Yellowstone (Geyser) gRPC cache for sell-side pricing.

Problem this replaces: yellowstone_pricing.py used to open a brand-new,
short-lived gRPC subscription *per price check*. Yellowstone only pushes an
account update on change, never on subscribe, so a quiet token (nobody else
trading it in that window) got nothing and the call sat until
GEYSER_PRICE_TIMEOUT_SEC expired before falling back to Shyft RPC. A
just-migrated token had an analogous gap finding its new PumpSwap pool via
getProgramAccounts, which isn't indexed/queryable the instant the pool is
created.

Fix: subscribe once, at startup, to every account owned by the pump.fun
bonding-curve program and every PumpSwap pool account (owner + datasize
filters only - no unverified discriminator bytes), and keep the latest state
of each in memory. By the time a sell needs a price, it's usually already
here.

Scope (confirmed): sell-side only - the buy flow never used Yellowstone
pricing and isn't touched by this. For PumpSwap, only pool *discovery* is
cached (the pool account's own address/metadata) - the two live vault
balances that actually determine price stay a cheap live RPC read in
shyft_pricing.get_pumpswap_pool_price, since that was never the slow part.

Public API:
  start_yellowstone_cache() / stop_yellowstone_cache()
  get_cached_curve_raw(pda_str) -> bytes | None
  set_cached_curve_raw(pda_str, raw)   # used by yellowstone_pricing's
                                        # RPC-seed-on-miss fallback
  get_cached_pool_metadata(mint) -> dict | None
  is_cache_connected() -> bool
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import grpc
from solders.pubkey import Pubkey

from settings import (
    GEYSER_GRPC_TOKEN,
    GEYSER_GRPC_URL,
    GEYSER_USE_TLS,
    PUMP_FUN_PROGRAM_ID_STR,
    USE_GEYSER,
)
from shyft_pricing import (
    PUMPSWAP_PROGRAM_ID,
    _PUMPSWAP_BASE_MINT_OFFSET,
    _PUMPSWAP_BASE_VAULT_OFFSET,
    _PUMPSWAP_POOL_DATA_SIZE,
    _PUMPSWAP_QUOTE_MINT_OFFSET,
    _PUMPSWAP_QUOTE_VAULT_OFFSET,
    _PUMPSWAP_VIRTUAL_QUOTE_RESERVES_OFFSET,
)
from yellowstone_grpc.generated import geyser_pb2, geyser_pb2_grpc

logger = logging.getLogger(__name__)

_RECONNECT_DELAY_SEC = 3.0
_MAX_RECONNECT_DELAY_SEC = 60.0

# mint -> {pool_pubkey, base_mint, quote_mint, base_vault, quote_vault,
# virtual_quote_reserves} - matches get_pumpswap_pool_price's
# cached_pool_metadata shape exactly, so it can be passed straight through.
_pool_cache_by_mint: dict[str, dict[str, Any]] = {}
_pool_cache_lock = threading.Lock()

# bonding-curve PDA (string) -> raw account bytes, keyed exactly as callers
# already have it (they derive the PDA from the mint themselves via
# ShyftPricingService.bonding_curve_pda - no reverse index needed here).
_curve_cache: dict[str, bytes] = {}
_curve_cache_lock = threading.Lock()

_connected = False
_stop_event = threading.Event()
_cache_thread: Optional[threading.Thread] = None
_started = False

# Observability counters, read-only outside this module (GIL-atomic
# increments are fine for simple counters, same convention as
# price_stream.py's status globals - not trading-critical data).
_curve_updates_seen = 0
_pool_updates_seen = 0


def _channel() -> grpc.Channel:
    endpoint = GEYSER_GRPC_URL.replace("http://", "").replace("https://", "")
    if GEYSER_USE_TLS:
        return grpc.secure_channel(
            endpoint, grpc.ssl_channel_credentials(), compression=grpc.Compression.Gzip
        )
    return grpc.insecure_channel(endpoint, compression=grpc.Compression.Gzip)


def _call_metadata() -> tuple:
    if GEYSER_GRPC_TOKEN:
        return (("x-token", GEYSER_GRPC_TOKEN),)
    return ()


def _subscribe_request() -> geyser_pb2.SubscribeRequest:
    """
    Two named filters, routed on the response side via SubscribeUpdate.filters
    (the server tells us which named filter(s) matched) rather than
    re-deriving owner/datasize ourselves. owner + datasize only - no
    discriminator bytes trusted from outside this codebase; both values reused
    are already relied on by live production code (PUMP_FUN_PROGRAM_ID_STR,
    _PUMPSWAP_POOL_DATA_SIZE).
    """
    req = geyser_pb2.SubscribeRequest()
    req.commitment = geyser_pb2.CommitmentLevel.PROCESSED

    curves = req.accounts["pump_curves"]
    curves.owner.append(PUMP_FUN_PROGRAM_ID_STR)

    pools = req.accounts["pumpswap_pools"]
    pools.owner.append(str(PUMPSWAP_PROGRAM_ID))
    pool_filter = pools.filters.add()
    pool_filter.datasize = _PUMPSWAP_POOL_DATA_SIZE

    return req


def _request_iterator(request: geyser_pb2.SubscribeRequest, stop_event: threading.Event):
    """
    Yields the initial request then blocks (never returns) until stop_event
    fires. The bidi Subscribe call's request side must stay open - letting
    this generator return right after the first yield can make the stream
    behave unexpectedly once the client signals "done sending."
    """
    yield request
    while not stop_event.wait(timeout=1.0):
        pass


def _store_pool_update(pool_pubkey: str, data: bytes) -> None:
    if len(data) < _PUMPSWAP_VIRTUAL_QUOTE_RESERVES_OFFSET + 16:
        return
    try:
        base_mint = str(Pubkey.from_bytes(data[_PUMPSWAP_BASE_MINT_OFFSET : _PUMPSWAP_BASE_MINT_OFFSET + 32]))
        quote_mint = str(Pubkey.from_bytes(data[_PUMPSWAP_QUOTE_MINT_OFFSET : _PUMPSWAP_QUOTE_MINT_OFFSET + 32]))
        base_vault = str(Pubkey.from_bytes(data[_PUMPSWAP_BASE_VAULT_OFFSET : _PUMPSWAP_BASE_VAULT_OFFSET + 32]))
        quote_vault = str(Pubkey.from_bytes(data[_PUMPSWAP_QUOTE_VAULT_OFFSET : _PUMPSWAP_QUOTE_VAULT_OFFSET + 32]))
        virtual_quote_reserves = int.from_bytes(
            data[_PUMPSWAP_VIRTUAL_QUOTE_RESERVES_OFFSET : _PUMPSWAP_VIRTUAL_QUOTE_RESERVES_OFFSET + 16],
            "little",
            signed=True,
        )
    except Exception as exc:
        logger.warning("[YellowstoneCache] malformed PumpSwap pool update for %s: %s", pool_pubkey, exc)
        return
    global _pool_updates_seen
    _pool_updates_seen += 1
    with _pool_cache_lock:
        _pool_cache_by_mint[base_mint] = {
            "pool_pubkey": pool_pubkey,
            "base_mint": base_mint,
            "quote_mint": quote_mint,
            "base_vault": base_vault,
            "quote_vault": quote_vault,
            "virtual_quote_reserves": virtual_quote_reserves,
        }


def _handle_update(update: geyser_pb2.SubscribeUpdate) -> None:
    global _curve_updates_seen
    if not update.HasField("account"):
        return
    acc = update.account.account
    pubkey_str = str(Pubkey.from_bytes(bytes(acc.pubkey)))
    data = bytes(acc.data)
    filters = set(update.filters)
    if "pump_curves" in filters:
        _curve_updates_seen += 1
        with _curve_cache_lock:
            _curve_cache[pubkey_str] = data
    elif "pumpswap_pools" in filters:
        _store_pool_update(pubkey_str, data)


def _run_cache_thread() -> None:
    global _connected
    reconnect_delay = _RECONNECT_DELAY_SEC
    while not _stop_event.is_set():
        channel = None
        try:
            channel = _channel()
            stub = geyser_pb2_grpc.GeyserStub(channel)
            call = stub.Subscribe(
                _request_iterator(_subscribe_request(), _stop_event),
                metadata=_call_metadata(),
            )
            logger.info("[YellowstoneCache] connecting to %s", GEYSER_GRPC_URL)
            for update in call:
                if _stop_event.is_set():
                    break
                _handle_update(update)
                if not _connected:
                    _connected = True
                    reconnect_delay = _RECONNECT_DELAY_SEC
                    logger.info(
                        "[YellowstoneCache] connected - subscribed to bonding-curve + PumpSwap-pool accounts"
                    )
        except Exception as exc:
            logger.warning(
                "[YellowstoneCache] stream error: %s - reconnecting in %ss", exc, reconnect_delay
            )
        finally:
            _connected = False
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
        if _stop_event.is_set():
            break
        _stop_event.wait(timeout=reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, _MAX_RECONNECT_DELAY_SEC)
    logger.info("[YellowstoneCache] stopped.")


def start_yellowstone_cache() -> bool:
    """Idempotent - no-ops if Yellowstone is disabled or already running."""
    global _cache_thread, _started
    if not (USE_GEYSER and GEYSER_GRPC_URL):
        return False
    if _started and _cache_thread is not None and _cache_thread.is_alive():
        return True
    _started = True
    _stop_event.clear()
    _cache_thread = threading.Thread(target=_run_cache_thread, name="yellowstone-cache", daemon=True)
    _cache_thread.start()
    return True


def stop_yellowstone_cache() -> None:
    global _started
    _stop_event.set()
    _started = False


def get_cached_curve_raw(pda_str: str) -> Optional[bytes]:
    with _curve_cache_lock:
        return _curve_cache.get(pda_str)


def set_cached_curve_raw(pda_str: str, raw: bytes) -> None:
    """Used by yellowstone_pricing's RPC-seed-on-miss fallback: seed a value
    once via RPC, then let the live stream overwrite it on the next trade."""
    with _curve_cache_lock:
        _curve_cache[pda_str] = raw


def get_cached_pool_metadata(mint: str) -> Optional[dict[str, Any]]:
    with _pool_cache_lock:
        entry = _pool_cache_by_mint.get(mint)
        return dict(entry) if entry is not None else None


def is_cache_connected() -> bool:
    return _connected


def cache_status() -> dict[str, Any]:
    """Diagnostics snapshot - curve/pool entry counts, not trade-decision data."""
    with _curve_cache_lock:
        curves_cached = len(_curve_cache)
    with _pool_cache_lock:
        pools_cached = len(_pool_cache_by_mint)
    return {
        "connected": _connected,
        "curves_cached": curves_cached,
        "pools_cached": pools_cached,
        "curve_updates_seen": _curve_updates_seen,
        "pool_updates_seen": _pool_updates_seen,
    }
