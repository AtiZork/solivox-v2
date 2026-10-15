"""
Yellowstone (Geyser) gRPC live Solana token pricing.

Used exclusively by the Sniper sell flow (autosnipe_sell_script.py) to fetch
the latest Pump.fun bonding-curve price right before a sell decision/execution.
The Sniper buy flow and all other pricing consumers keep using shyft_pricing.

Reuses existing project logic instead of duplicating it:
  - Bonding-curve PDA derivation + virtual-reserve parsing:
    shyft_pricing.ShyftPricingService (static helpers).
  - SOL/USD conversion: the shared ShyftPricingService's Pyth-based
    get_sol_usd() (same source live_pricing/price_stream/shyft_pricing use).

Project-wide helper:
  from yellowstone_pricing import get_yellowstone_price
  price_data = get_yellowstone_price(token_mint)
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import grpc
from solders.pubkey import Pubkey

from settings import (
    GEYSER_GRPC_TOKEN,
    GEYSER_GRPC_URL,
    GEYSER_PRICE_CACHE_MAX_AGE_SEC,
    GEYSER_PRICE_TIMEOUT_SEC,
    GEYSER_STREAM_SYNC_SEC,
    GEYSER_USE_TLS,
    USE_GEYSER,
)
from shyft_pricing import ShyftPricingService, get_shyft_pricing_service, get_token_price
from yellowstone_grpc.generated import geyser_pb2, geyser_pb2_grpc

logger = logging.getLogger(__name__)


class YellowstonePricingError(Exception):
    """Raised when a Yellowstone price cannot be resolved for the sell flow."""


def is_yellowstone_enabled() -> bool:
    """USE_GEYSER=false or missing endpoint => Sniper sell stays on Shyft pricing."""
    return bool(USE_GEYSER and GEYSER_GRPC_URL)


def _build_channel() -> grpc.Channel:
    endpoint = GEYSER_GRPC_URL.replace("http://", "").replace("https://", "")
    if GEYSER_USE_TLS:
        return grpc.secure_channel(
            endpoint,
            grpc.ssl_channel_credentials(),
            compression=grpc.Compression.Gzip,
        )
    return grpc.insecure_channel(endpoint, compression=grpc.Compression.Gzip)


def _call_metadata() -> tuple:
    if GEYSER_GRPC_TOKEN:
        return (("x-token", GEYSER_GRPC_TOKEN),)
    return ()


def _subscribe_request(bonding_curve_pubkey: str) -> geyser_pb2.SubscribeRequest:
    req = geyser_pb2.SubscribeRequest()
    req.accounts["sell_price"].account.append(bonding_curve_pubkey)
    req.commitment = geyser_pb2.CommitmentLevel.PROCESSED
    return req


def _request_iterator(request: geyser_pb2.SubscribeRequest):
    yield request


def _fetch_bonding_curve_account(
    bonding_curve_pubkey: str, timeout_sec: float
) -> bytes:
    """Open a short-lived Yellowstone subscription and return the first account payload."""
    channel = _build_channel()
    try:
        stub = geyser_pb2_grpc.GeyserStub(channel)
        call = stub.Subscribe(
            _request_iterator(_subscribe_request(bonding_curve_pubkey)),
            timeout=timeout_sec,
            metadata=_call_metadata(),
        )
        try:
            for update in call:
                if update.HasField("account"):
                    data = bytes(update.account.account.data)
                    if data:
                        return data
            raise YellowstonePricingError(
                f"Yellowstone stream ended without an account update for {bonding_curve_pubkey}"
            )
        finally:
            call.cancel()
    except grpc.RpcError as exc:
        code = exc.code() if hasattr(exc, "code") else None
        raise YellowstonePricingError(
            f"Yellowstone gRPC error ({code}) for {bonding_curve_pubkey}: {exc.details() if hasattr(exc, 'details') else exc}"
        ) from exc
    finally:
        channel.close()


def _pumpswap_fallback(mint: str) -> dict[str, Any]:
    """
    Price a migrated token from its PumpSwap pool (live RPC read of the two
    vault balances — never frozen like a completed bonding curve, and more
    appropriate than a gRPC subscribe-and-wait for a point-in-time read).
    """
    try:
        price = get_shyft_pricing_service().get_pumpswap_pool_price(mint)
    except Exception as exc:
        raise YellowstonePricingError(
            f"token {mint} has migrated off the Pump.fun bonding curve and its "
            f"PumpSwap price could not be resolved: {exc}"
        ) from exc

    now = time.time()
    result = dict(price)
    result.setdefault("timestamp", datetime.fromtimestamp(now, tz=timezone.utc).isoformat())
    result.setdefault("timestamp_unix", now)
    result["usdPrice"] = result.get("usd_price")
    result["tokenAddress"] = result.get("mint")
    return result


def get_yellowstone_price(token_mint: str) -> dict[str, Any]:
    """
    Fetch the latest live price for a mint via Yellowstone gRPC.

    While the token is still on its Pump.fun bonding curve, this subscribes
    to the curve account and returns the live-pushed price. Once the token
    has migrated (graduated) to PumpSwap, the bonding curve is permanently
    frozen, so this instead reads the live price from the PumpSwap pool.

    Returns a dict shaped like shyft_pricing.get_token_price()'s output
    (mint, price_in_sol, usd_price/usdPrice, sol_price_usd, source, timestamp, ...)
    so callers can swap the price source without changing their price-consumption
    code.

    Raises:
        YellowstonePricingError: disabled, misconfigured, unresolved mint,
            unavailable/timeout, or an invalid/zero price (bonding-curve or
            PumpSwap).
    """
    if not is_yellowstone_enabled():
        raise YellowstonePricingError(
            "Yellowstone is disabled (USE_GEYSER=false or GEYSER_GRPC_URL unset)"
        )

    mint = (token_mint or "").strip()
    if not mint:
        raise YellowstonePricingError("mint address is required")

    try:
        bonding_curve = ShyftPricingService.bonding_curve_pda(mint)
    except Exception as exc:
        raise YellowstonePricingError(f"Could not resolve mint {mint}: {exc}") from exc

    # Cheap pre-check: a migrated bonding curve will never deliver another
    # account update, so don't waste the subscribe timeout waiting for one.
    if get_shyft_pricing_service().is_bonding_curve_migrated(mint):
        return _pumpswap_fallback(mint)

    raw = _fetch_bonding_curve_account(str(bonding_curve), GEYSER_PRICE_TIMEOUT_SEC)

    if ShyftPricingService._bonding_curve_is_complete(raw):
        # Migrated during our subscription wait — fall back instead of
        # returning the now-frozen final bonding-curve price.
        return _pumpswap_fallback(mint)

    price_sol = ShyftPricingService._price_sol_from_bonding_raw(raw)
    if not price_sol or price_sol <= 0:
        raise YellowstonePricingError(
            f"Yellowstone returned an invalid/zero price for mint {mint}"
        )

    try:
        sol_usd = get_shyft_pricing_service().get_sol_usd()
    except Exception as exc:
        raise YellowstonePricingError(f"SOL/USD conversion failed: {exc}") from exc

    if not sol_usd or sol_usd <= 0:
        raise YellowstonePricingError("SOL/USD conversion returned an invalid/zero price")

    usd_price = price_sol * sol_usd
    now = time.time()
    result = {
        "mint": mint,
        "bonding_curve": str(bonding_curve),
        "price_in_sol": price_sol,
        "usd_price": usd_price,
        "sol_price_usd": sol_usd,
        "source": "yellowstone_grpc_accountSubscribe",
        "timestamp": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "timestamp_unix": now,
    }
    # Alias used by existing callers (mirrors shyft_pricing.TokenPriceSnapshot.to_dict()).
    result["usdPrice"] = result["usd_price"]
    result["tokenAddress"] = result["mint"]
    return result


# --- Persistent subscription + in-memory price cache ------------------------
#
# get_yellowstone_price() above opens a brand-new gRPC channel and waits for
# the *next* account write every single call. Yellowstone's startup/snapshot
# replay (the is_startup=true account updates in the SubscribeUpdateAccount
# proto) only happens once, at the plugin's own boot, for whatever filters
# are registered at that moment - a filter a client adds afterward never
# gets a retroactive snapshot, only future writes. So a quiet token (no
# trade landing inside the subscribe window) can never receive anything on
# that path and always burns the full GEYSER_PRICE_TIMEOUT_SEC.
#
# YellowstonePriceStream instead keeps one persistent subscription open for
# every currently-open position. The moment a mint is first tracked it's
# seeded with one direct RPC read (so there's a correct price immediately,
# without waiting on the stream at all); from then on, live pushes keep the
# cached entry current for free. A price lookup is then just a dict read.

_RECONNECT_DELAY_SEC = 3
_MAX_RECONNECT_DELAY_SEC = 60


def _pubkey_bytes(pubkey_str: str) -> bytes:
    return bytes(Pubkey.from_string(pubkey_str))


def _queue_iterator(q: "queue.Queue"):
    while True:
        item = q.get()
        if item is None:
            return
        yield item


class YellowstonePriceStream:
    """Persistent Yellowstone subscription covering every currently-open
    Sniper position's bonding curve, backed by an in-memory mint -> latest
    price cache. See module docstring above for why this exists."""

    def __init__(self, get_open_mints: Callable[[], set], sync_interval: float = 5.0) -> None:
        self._get_open_mints = get_open_mints
        self._sync_interval = sync_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._sync_thread: Optional[threading.Thread] = None
        self.connected = False

        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._cache_at: dict[str, float] = {}
        self._tracked: dict[str, str] = {}  # mint -> bonding curve pubkey (base58)
        self._pubkey_to_mint: dict[bytes, str] = {}
        self._mint_locks: dict[str, threading.Lock] = {}

        self._current_queue: Optional["queue.Queue"] = None
        self._current_call = None

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="yellowstone-price-stream", daemon=True)
        self._sync_thread = threading.Thread(
            target=self._sync_loop, name="yellowstone-price-sync", daemon=True
        )
        self._thread.start()
        self._sync_thread.start()
        logger.info("[YellowstoneStream] started")

    def stop(self) -> None:
        self._running = False
        call = self._current_call
        if call is not None:
            try:
                call.cancel()
            except Exception:
                pass
        q = self._current_queue
        if q is not None:
            try:
                q.put(None)
            except Exception:
                pass

    # -- public price API -------------------------------------------------

    def get_cached_price(self, mint: str) -> Optional[dict]:
        with self._lock:
            cached = self._cache.get(mint)
            cached_at = self._cache_at.get(mint)
        if cached is None or cached_at is None:
            return None
        if (time.monotonic() - cached_at) > GEYSER_PRICE_CACHE_MAX_AGE_SEC:
            return None  # safety net only; forces a fresh seed read
        return cached

    def ensure_tracked(self, mint: str) -> dict:
        """Seed + register a mint that isn't cached yet. Safe to call from
        multiple sell-price worker threads concurrently for the same mint
        (coalesces onto one RPC read) or different mints (run in parallel)."""
        return self._seed_and_track(mint)

    # -- internals --------------------------------------------------------

    def _mint_lock(self, mint: str) -> threading.Lock:
        with self._lock:
            lock = self._mint_locks.get(mint)
            if lock is None:
                lock = threading.Lock()
                self._mint_locks[mint] = lock
            return lock

    def _seed_and_track(self, mint: str) -> dict:
        lock = self._mint_lock(mint)
        with lock:
            with self._lock:
                cached = self._cache.get(mint)
            if cached is not None:
                return cached

            result = get_token_price(mint, include_token_details=False)
            if result.get("source") == "shyft_rpc_pumpswap_pool":
                # Migrated: never belongs on the gRPC bonding-curve filter.
                # The is_bonding_curve_migrated() pre-check in
                # get_yellowstone_price_cached() handles it from here on.
                return result

            bonding_curve = str(ShyftPricingService.bonding_curve_pda(mint))
            with self._lock:
                self._cache[mint] = result
                self._cache_at[mint] = time.monotonic()
                self._tracked[mint] = bonding_curve
                self._pubkey_to_mint[_pubkey_bytes(bonding_curve)] = mint
            self._push_current_filter()
            return result

    def _sync_mints(self) -> None:
        try:
            wanted = set(self._get_open_mints())
        except Exception as exc:
            logger.warning("[YellowstoneStream] could not load open trades: %s", exc)
            return

        with self._lock:
            currently = set(self._tracked)

        for mint in wanted - currently:
            try:
                self._seed_and_track(mint)
            except Exception as exc:
                logger.warning("[YellowstoneStream] could not seed %s: %s", mint, exc)

        to_remove = currently - wanted
        if to_remove:
            with self._lock:
                for mint in to_remove:
                    pubkey = self._tracked.pop(mint, None)
                    self._cache.pop(mint, None)
                    self._cache_at.pop(mint, None)
                    self._mint_locks.pop(mint, None)
                    if pubkey is not None:
                        self._pubkey_to_mint.pop(_pubkey_bytes(pubkey), None)
            self._push_current_filter()

    def _sync_loop(self) -> None:
        while self._running:
            time.sleep(self._sync_interval)
            if not self._running:
                break
            try:
                self._sync_mints()
            except Exception as exc:
                logger.warning("[YellowstoneStream] sync failed: %s", exc)

    def _full_subscribe_request(self) -> geyser_pb2.SubscribeRequest:
        with self._lock:
            pubkeys = list(self._tracked.values())
        return self._subscribe_request_for(pubkeys)

    def _push_current_filter(self) -> None:
        q = self._current_queue
        if q is None:
            return  # not connected yet; the connect path sends the current set
        with self._lock:
            pubkeys = list(self._tracked.values())
        try:
            q.put(self._subscribe_request_for(pubkeys))
        except Exception:
            pass

    @staticmethod
    def _subscribe_request_for(pubkeys) -> geyser_pb2.SubscribeRequest:
        req = geyser_pb2.SubscribeRequest()
        for pk in pubkeys:
            req.accounts["sell_price"].account.append(pk)
        req.commitment = geyser_pb2.CommitmentLevel.PROCESSED
        return req

    def _reseed_all_tracked(self) -> None:
        """Called right after (re)connect: a live push can only ever be
        missed during a disconnect gap, so re-read every tracked mint
        directly rather than trusting whatever's cached."""
        with self._lock:
            mints = list(self._tracked)
        dropped = False
        for mint in mints:
            try:
                result = get_token_price(mint, include_token_details=False)
            except Exception as exc:
                logger.warning("[YellowstoneStream] reseed failed for %s: %s", mint, exc)
                continue
            if result.get("source") == "shyft_rpc_pumpswap_pool":
                with self._lock:
                    pubkey = self._tracked.pop(mint, None)
                    self._cache.pop(mint, None)
                    self._cache_at.pop(mint, None)
                    self._mint_locks.pop(mint, None)
                    if pubkey is not None:
                        self._pubkey_to_mint.pop(_pubkey_bytes(pubkey), None)
                dropped = True
                continue
            with self._lock:
                self._cache[mint] = result
                self._cache_at[mint] = time.monotonic()
        if dropped:
            self._push_current_filter()

    def _handle_update(self, update) -> None:
        if not update.HasField("account"):
            return
        pubkey = bytes(update.account.account.pubkey)
        with self._lock:
            mint = self._pubkey_to_mint.get(pubkey)
        if mint is None:
            return  # push for a mint we've since stopped tracking

        data = bytes(update.account.account.data)
        if not data:
            return

        if ShyftPricingService._bonding_curve_is_complete(data):
            with self._lock:
                self._tracked.pop(mint, None)
                self._cache.pop(mint, None)
                self._cache_at.pop(mint, None)
                self._mint_locks.pop(mint, None)
                self._pubkey_to_mint.pop(pubkey, None)
            self._push_current_filter()
            return

        price_sol = ShyftPricingService._price_sol_from_bonding_raw(data)
        if not price_sol or price_sol <= 0:
            return  # don't overwrite a good cached price with junk

        try:
            sol_usd = get_shyft_pricing_service().get_sol_usd()
        except Exception:
            sol_usd = None
        if not sol_usd or sol_usd <= 0:
            return

        with self._lock:
            bonding_curve = self._tracked.get(mint, "")
        usd_price = price_sol * sol_usd
        now_wall = time.time()
        result = {
            "mint": mint,
            "bonding_curve": bonding_curve,
            "price_in_sol": price_sol,
            "usd_price": usd_price,
            "sol_price_usd": sol_usd,
            "source": "yellowstone_grpc_accountSubscribe",
            "timestamp": datetime.fromtimestamp(now_wall, tz=timezone.utc).isoformat(),
            "timestamp_unix": now_wall,
        }
        result["usdPrice"] = result["usd_price"]
        result["tokenAddress"] = result["mint"]

        with self._lock:
            self._cache[mint] = result
            self._cache_at[mint] = time.monotonic()

    def _run(self) -> None:
        delay = _RECONNECT_DELAY_SEC
        while self._running:
            channel = None
            try:
                channel = _build_channel()
                stub = geyser_pb2_grpc.GeyserStub(channel)
                q: "queue.Queue" = queue.Queue()
                q.put(self._full_subscribe_request())
                self._current_queue = q
                call = stub.Subscribe(_queue_iterator(q), metadata=_call_metadata())
                self._current_call = call
                self.connected = True
                delay = _RECONNECT_DELAY_SEC
                logger.info("[YellowstoneStream] connected")
                self._reseed_all_tracked()
                for update in call:
                    if not self._running:
                        break
                    self._handle_update(update)
            except grpc.RpcError as exc:
                code = exc.code() if hasattr(exc, "code") else None
                logger.error(
                    "[YellowstoneStream] gRPC error (%s): %s — retrying in %ss", code, exc, delay
                )
            except Exception as exc:
                logger.error("[YellowstoneStream] stream error: %s — retrying in %ss", exc, delay)
            finally:
                self.connected = False
                q = self._current_queue
                if q is not None:
                    try:
                        q.put(None)
                    except Exception:
                        pass
                self._current_queue = None
                self._current_call = None
                if channel is not None:
                    try:
                        channel.close()
                    except Exception:
                        pass
            if self._running:
                time.sleep(delay)
                delay = min(delay * 2, _MAX_RECONNECT_DELAY_SEC)
        logger.info("[YellowstoneStream] stopped")


_price_stream: Optional["YellowstonePriceStream"] = None


def start_yellowstone_stream(
    get_open_mints: Callable[[], set], sync_interval: Optional[float] = None
) -> Optional["YellowstonePriceStream"]:
    """Start the persistent Yellowstone price stream. No-op (returns None)
    if Yellowstone isn't enabled/configured, so callers can call this
    unconditionally at startup."""
    global _price_stream
    if not is_yellowstone_enabled():
        return None
    if _price_stream is not None:
        return _price_stream
    stream = YellowstonePriceStream(get_open_mints, sync_interval or GEYSER_STREAM_SYNC_SEC)
    stream.start()
    _price_stream = stream
    return stream


def stop_yellowstone_stream() -> None:
    global _price_stream
    if _price_stream is not None:
        _price_stream.stop()
        _price_stream = None


def get_yellowstone_price_cached(token_mint: str) -> dict[str, Any]:
    """
    Cache-backed replacement for get_yellowstone_price() on the sell hot
    path: never blocks waiting on a live gRPC push. See the module comment
    above YellowstonePriceStream for why. Falls back to the one-shot
    get_yellowstone_price() if the persistent stream isn't running (e.g. not
    started yet), so behavior never silently degrades to "no price".
    """
    if not is_yellowstone_enabled():
        raise YellowstonePricingError(
            "Yellowstone is disabled (USE_GEYSER=false or GEYSER_GRPC_URL unset)"
        )

    mint = (token_mint or "").strip()
    if not mint:
        raise YellowstonePricingError("mint address is required")

    # Migrated curves never belong in the gRPC filter - same pre-check as
    # get_yellowstone_price().
    if get_shyft_pricing_service().is_bonding_curve_migrated(mint):
        return _pumpswap_fallback(mint)

    stream = _price_stream
    if stream is None:
        return get_yellowstone_price(mint)

    cached = stream.get_cached_price(mint)
    if cached is not None:
        return cached

    try:
        return stream.ensure_tracked(mint)
    except YellowstonePricingError:
        raise
    except Exception as exc:
        raise YellowstonePricingError(f"could not seed price for {mint}: {exc}") from exc
