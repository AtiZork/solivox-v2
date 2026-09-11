"""
Shyft live Solana token pricing.

Uses:
  - Shyft REST  GET /sol/v1/token/get_info  → token metadata
  - Shyft HTTP RPC (getAccountInfo)         → Pump.fun bonding-curve + Pyth SOL/USD
  - Shyft WebSocket accountSubscribe        → live bonding-curve updates

Project-wide helper:
  from shyft_pricing import get_token_price
  price_data = get_token_price(token_mint)

Does not alter existing live_pricing / price_stream business logic.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from solders.pubkey import Pubkey
from websockets.asyncio.client import connect as ws_connect

from settings import (
    PUMP_FUN_PROGRAM_ID_STR,
    SHYFT_API_BASE,
    SHYFT_API_KEY,
    SHYFT_NETWORK,
    SHYFT_RPC_URL,
    SHYFT_WS_URL,
)

logger = logging.getLogger(__name__)

PUMP_FUN_PROGRAM_ID = Pubkey.from_string(PUMP_FUN_PROGRAM_ID_STR)
TOKEN_METADATA_PROGRAM_ID = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")
PYTH_SOL_PRICE_FEED = Pubkey.from_string("7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE")
WSOL_MINT = "So11111111111111111111111111111111111111112"

_SECRET_QUERY_KEYS = {"api_key", "api-key", "x-api-key", "token", "access_token"}
_WS_RECONNECT_DELAY_SEC = 2.0
_WS_MAX_RECONNECT_DELAY_SEC = 60.0


def _redact_url(url: str) -> str:
    """Strip secrets from URLs before logging."""
    try:
        parts = urlsplit(url)
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in _SECRET_QUERY_KEYS:
                query.append((key, "***"))
            else:
                query.append((key, value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except Exception:
        return "<redacted>"


@dataclass
class TokenPriceSnapshot:
    mint: str
    name: Optional[str] = None
    symbol: Optional[str] = None
    decimals: Optional[int] = None
    image: Optional[str] = None
    description: Optional[str] = None
    current_supply: Optional[float] = None
    price_in_sol: Optional[float] = None
    usd_price: Optional[float] = None
    sol_price_usd: Optional[float] = None
    source: Optional[str] = None
    bonding_curve: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    timestamp_unix: float = field(default_factory=time.time)
    raw_token_info: Optional[dict] = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Aliases used by trade.py / older callers
        data["usdPrice"] = data.get("usd_price")
        data["tokenAddress"] = data.get("mint")
        return data


class ShyftPricingError(Exception):
    """Raised when Shyft pricing cannot be resolved."""


class ShyftPricingService:
    """Reusable Shyft-backed token metadata + live price service."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        rpc_url: Optional[str] = None,
        ws_url: Optional[str] = None,
        api_base: Optional[str] = None,
        network: Optional[str] = None,
        session: Optional[requests.Session] = None,
        request_timeout_sec: float = 20.0,
    ) -> None:
        self.api_key = api_key or SHYFT_API_KEY
        self.rpc_url = rpc_url or SHYFT_RPC_URL
        self.ws_url = ws_url or SHYFT_WS_URL
        self.api_base = (api_base or SHYFT_API_BASE).rstrip("/")
        self.network = network or SHYFT_NETWORK
        self.session = session or requests.Session()
        self.request_timeout_sec = request_timeout_sec
        self._stop_events: dict[str, threading.Event] = {}
        self._stream_threads: dict[str, threading.Thread] = {}

        if not self.api_key:
            raise ShyftPricingError("SHYFT_API_KEY is not configured")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _rest_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }

    def _rpc(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = self.session.post(
                self.rpc_url,
                json=payload,
                timeout=self.request_timeout_sec,
            )
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException as exc:
            logger.error("Shyft RPC %s failed against %s: %s", method, _redact_url(self.rpc_url), exc)
            raise ShyftPricingError(f"Shyft RPC {method} failed: {exc}") from exc

        if "error" in body:
            logger.error("Shyft RPC %s error: %s", method, body["error"])
            raise ShyftPricingError(f"Shyft RPC {method} error: {body['error']}")
        return body.get("result")

    @staticmethod
    def _decode_account_data(data_field: Any) -> Optional[bytes]:
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

    def _get_account_bytes(self, pubkey: str | Pubkey) -> Optional[bytes]:
        key = str(pubkey)
        result = self._rpc("getAccountInfo", [key, {"encoding": "base64", "commitment": "confirmed"}])
        if not result or not result.get("value"):
            return None
        return self._decode_account_data(result["value"].get("data"))

    # ------------------------------------------------------------------
    # Token details (Shyft REST, with on-chain Metaplex fallback)
    # ------------------------------------------------------------------
    @staticmethod
    def _metadata_pda(mint: str) -> Pubkey:
        mint_pk = Pubkey.from_string(mint)
        pda, _ = Pubkey.find_program_address(
            [b"metadata", bytes(TOKEN_METADATA_PROGRAM_ID), bytes(mint_pk)],
            TOKEN_METADATA_PROGRAM_ID,
        )
        return pda

    @staticmethod
    def _parse_metaplex_metadata(raw: bytes) -> dict[str, Optional[str]]:
        """Parse Metaplex Token Metadata name/symbol/uri from account bytes."""
        empty = {"name": None, "symbol": None, "metadata_uri": None}
        if not raw or len(raw) < 1 + 32 + 32 + 4:
            return empty
        try:
            off = 1 + 32 + 32  # key + update_authority + mint
            name_len = int.from_bytes(raw[off : off + 4], "little", signed=False)
            off += 4
            if name_len < 0 or name_len > 200 or len(raw) < off + name_len:
                return empty
            name = raw[off : off + name_len].decode("utf-8", errors="ignore").strip("\x00 ").strip()
            off += name_len

            if len(raw) < off + 4:
                return {"name": name or None, "symbol": None, "metadata_uri": None}
            sym_len = int.from_bytes(raw[off : off + 4], "little", signed=False)
            off += 4
            if sym_len < 0 or sym_len > 50 or len(raw) < off + sym_len:
                return {"name": name or None, "symbol": None, "metadata_uri": None}
            symbol = raw[off : off + sym_len].decode("utf-8", errors="ignore").strip("\x00 ").strip()
            off += sym_len

            metadata_uri = None
            if len(raw) >= off + 4:
                uri_len = int.from_bytes(raw[off : off + 4], "little", signed=False)
                off += 4
                if 0 < uri_len <= 500 and len(raw) >= off + uri_len:
                    metadata_uri = (
                        raw[off : off + uri_len].decode("utf-8", errors="ignore").strip("\x00 ").strip()
                        or None
                    )
            return {
                "name": name or None,
                "symbol": symbol or None,
                "metadata_uri": metadata_uri,
            }
        except Exception:
            return empty

    @staticmethod
    def _parse_spl_mint(raw: bytes) -> dict[str, Any]:
        """Parse SPL mint account: supply + decimals."""
        if not raw or len(raw) < 45:
            return {"decimals": None, "current_supply": None, "mint_authority": None}
        try:
            # COption pubkey (4 + 32) + supply u64 + decimals u8
            has_auth = int.from_bytes(raw[0:4], "little") == 1
            auth = None
            if has_auth:
                # leave as None string-wise unless needed; keep bytes unused
                auth = None
            supply_raw = int.from_bytes(raw[36:44], "little", signed=False)
            decimals = raw[44]
            supply = float(supply_raw) / (10 ** decimals) if decimals is not None else float(supply_raw)
            return {
                "decimals": int(decimals),
                "current_supply": supply,
                "mint_authority": auth,
            }
        except Exception:
            return {"decimals": None, "current_supply": None, "mint_authority": None}

    def _fetch_metadata_json(self, uri: Optional[str]) -> dict[str, Any]:
        if not uri:
            return {}
        url = uri.strip()
        if url.startswith("ipfs://"):
            url = "https://ipfs.io/ipfs/" + url[len("ipfs://") :]
        try:
            resp = self.session.get(url, timeout=min(self.request_timeout_sec, 10.0))
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.debug("Metadata URI fetch failed for %s: %s", uri, exc)
        return {}

    @staticmethod
    def _parse_token2022_metadata(raw: bytes) -> dict[str, Optional[str]]:
        """Parse Token-2022 TokenMetadata TLV extension (type=19) from mint account bytes."""
        empty = {"name": None, "symbol": None, "metadata_uri": None}
        if not raw or len(raw) < 86:
            return empty
        # Scan for TokenMetadata TLV (type 19). AccountType/padding before extensions varies.
        for i in range(82, len(raw) - 4):
            ext_type = int.from_bytes(raw[i : i + 2], "little", signed=False)
            ext_len = int.from_bytes(raw[i + 2 : i + 4], "little", signed=False)
            if ext_type != 19 or ext_len < 68 or i + 4 + ext_len > len(raw):
                continue
            data = raw[i + 4 : i + 4 + ext_len]
            # update_authority(32) + mint(32) + borsh name/symbol/uri
            try:
                off = 64
                out: dict[str, Optional[str]] = {
                    "name": None,
                    "symbol": None,
                    "metadata_uri": None,
                }
                for key in ("name", "symbol", "metadata_uri"):
                    if off + 4 > len(data):
                        break
                    n = int.from_bytes(data[off : off + 4], "little", signed=False)
                    off += 4
                    if n < 0 or n > 512 or off + n > len(data):
                        return empty
                    value = data[off : off + n].decode("utf-8", errors="ignore").strip("\x00 ").strip()
                    off += n
                    out[key] = value or None
                if out.get("name") or out.get("symbol"):
                    return out
            except Exception:
                continue
        return empty

    def get_onchain_token_details(self, mint: str) -> dict[str, Any]:
        """Resolve name/symbol/decimals/supply via Shyft RPC (Metaplex / Token-2022 + SPL mint)."""
        mint = (mint or "").strip()
        if not mint:
            raise ShyftPricingError("mint address is required")

        mint_raw = self._get_account_bytes(mint)
        mint_info = self._parse_spl_mint(mint_raw or b"")

        meta_raw = self._get_account_bytes(self._metadata_pda(mint))
        parsed = self._parse_metaplex_metadata(meta_raw or b"")
        if not parsed.get("name") and not parsed.get("symbol"):
            parsed = self._parse_token2022_metadata(mint_raw or b"")

        image = None
        description = None
        uri = parsed.get("metadata_uri")
        meta_json = self._fetch_metadata_json(uri)
        if meta_json:
            image = meta_json.get("image") or meta_json.get("image_uri")
            description = meta_json.get("description")
            if not parsed.get("name") and meta_json.get("name"):
                parsed["name"] = meta_json.get("name")
            if not parsed.get("symbol") and meta_json.get("symbol"):
                parsed["symbol"] = meta_json.get("symbol")

        if not parsed.get("name") and not parsed.get("symbol") and mint_info.get("decimals") is None:
            raise ShyftPricingError("on-chain token metadata not found")

        return {
            "mint": mint,
            "name": parsed.get("name"),
            "symbol": parsed.get("symbol"),
            "decimals": mint_info.get("decimals"),
            "image": image,
            "description": description,
            "current_supply": mint_info.get("current_supply"),
            "metadata_uri": uri,
            "mint_authority": mint_info.get("mint_authority"),
            "freeze_authority": None,
            "raw": {
                "source": "shyft_rpc_onchain",
                "metadata": parsed,
                "mint": mint_info,
                "offchain": meta_json or None,
            },
        }

    def get_token_details(self, mint: str) -> dict[str, Any]:
        """Prefer Shyft REST get_info; fall back to on-chain Metaplex via Shyft RPC."""
        mint = (mint or "").strip()
        if not mint:
            raise ShyftPricingError("mint address is required")

        url = f"{self.api_base}/sol/v1/token/get_info"
        params = {"network": self.network, "token_address": mint}
        try:
            resp = self.session.get(
                url,
                params=params,
                headers=self._rest_headers(),
                timeout=self.request_timeout_sec,
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("success"):
                result = body.get("result") or {}
                details = {
                    "mint": mint,
                    "name": result.get("name"),
                    "symbol": result.get("symbol"),
                    "decimals": result.get("decimals"),
                    "image": result.get("image"),
                    "description": result.get("description"),
                    "current_supply": result.get("current_supply"),
                    "metadata_uri": result.get("metadata_uri"),
                    "mint_authority": result.get("mint_authority"),
                    "freeze_authority": result.get("freeze_authority"),
                    "raw": result,
                }
                # If REST succeeds but name/symbol empty, enrich from chain
                if details.get("name") and details.get("symbol"):
                    return details
                try:
                    onchain = self.get_onchain_token_details(mint)
                    for key in ("name", "symbol", "decimals", "image", "description", "current_supply", "metadata_uri"):
                        if not details.get(key) and onchain.get(key) is not None:
                            details[key] = onchain.get(key)
                    details["raw"] = {"rest": result, "onchain": onchain.get("raw")}
                except ShyftPricingError:
                    pass
                return details
            logger.warning(
                "Shyft token get_info unsuccessful for mint=%s: %s",
                mint,
                body.get("message") or body.get("error") or body,
            )
        except requests.RequestException as exc:
            logger.warning("Shyft token get_info failed for mint=%s: %s", mint, exc)

        return self.get_onchain_token_details(mint)

    # ------------------------------------------------------------------
    # Pricing via Shyft RPC
    # ------------------------------------------------------------------
    @staticmethod
    def bonding_curve_pda(mint: str) -> Pubkey:
        mint_pk = Pubkey.from_string(mint)
        pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)], PUMP_FUN_PROGRAM_ID)
        return pda

    @staticmethod
    def _price_sol_from_bonding_raw(raw: bytes) -> Optional[float]:
        for vt_off, vs_off in ((8, 16), (48, 56)):
            if len(raw) < vs_off + 8:
                continue
            vt = int.from_bytes(raw[vt_off : vt_off + 8], "little", signed=False)
            vs = int.from_bytes(raw[vs_off : vs_off + 8], "little", signed=False)
            if vt > 0 and vs > 0:
                return float(vs) / (float(vt) * 1000.0)
        return None

    def get_sol_usd(self) -> Optional[float]:
        raw = self._get_account_bytes(PYTH_SOL_PRICE_FEED)
        if not raw or len(raw) < 93:
            return None
        price_data = struct.unpack_from("<q", raw, 73)[0]
        exponent = struct.unpack_from("<i", raw, 89)[0]
        return float(price_data) * (10.0 ** float(exponent))

    def get_bonding_curve_price(self, mint: str) -> dict[str, Any]:
        mint = (mint or "").strip()
        if not mint:
            raise ShyftPricingError("mint address is required")

        curve = self.bonding_curve_pda(mint)
        raw = self._get_account_bytes(curve)
        if not raw:
            raise ShyftPricingError("bonding curve account not found (token may be graduated or non-pump)")

        price_sol = self._price_sol_from_bonding_raw(raw)
        if price_sol is None:
            raise ShyftPricingError("unable to parse bonding-curve reserves")

        sol_usd = self.get_sol_usd()
        usd = (price_sol * sol_usd) if sol_usd is not None else None
        return {
            "mint": mint,
            "bonding_curve": str(curve),
            "price_in_sol": price_sol,
            "usd_price": usd,
            "sol_price_usd": sol_usd,
            "source": "shyft_rpc_pumpfun_bonding_curve",
        }

    def get_latest_price(self, mint: str, include_token_details: bool = True) -> TokenPriceSnapshot:
        """Fetch latest price (+ optional Shyft token details) for a mint."""
        mint = (mint or "").strip()
        if not mint:
            raise ShyftPricingError("mint address is required")

        details: dict[str, Any] = {}
        if include_token_details:
            try:
                details = self.get_token_details(mint)
            except ShyftPricingError as exc:
                logger.warning("Token details unavailable for %s: %s", mint, exc)

        try:
            price = self.get_bonding_curve_price(mint)
        except ShyftPricingError:
            # Native SOL / WSOL: quote via Pyth only
            if mint in (WSOL_MINT, "11111111111111111111111111111111"):
                sol_usd = self.get_sol_usd()
                if sol_usd is None:
                    raise
                price = {
                    "mint": mint,
                    "bonding_curve": None,
                    "price_in_sol": 1.0,
                    "usd_price": sol_usd,
                    "sol_price_usd": sol_usd,
                    "source": "shyft_rpc_pyth_sol",
                }
            else:
                raise

        now = time.time()
        return TokenPriceSnapshot(
            mint=mint,
            name=details.get("name"),
            symbol=details.get("symbol"),
            decimals=details.get("decimals"),
            image=details.get("image"),
            description=details.get("description"),
            current_supply=details.get("current_supply"),
            price_in_sol=price.get("price_in_sol"),
            usd_price=price.get("usd_price"),
            sol_price_usd=price.get("sol_price_usd"),
            source=price.get("source"),
            bonding_curve=price.get("bonding_curve"),
            timestamp=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            timestamp_unix=now,
            raw_token_info=details.get("raw"),
        )

    # ------------------------------------------------------------------
    # Live WebSocket streaming (Shyft accountSubscribe)
    # ------------------------------------------------------------------
    def stop_stream(self, mint: str) -> None:
        mint = (mint or "").strip()
        ev = self._stop_events.get(mint)
        if ev:
            ev.set()

    def stop_all_streams(self) -> None:
        for mint in list(self._stop_events):
            self.stop_stream(mint)

    def stream_price(
        self,
        mint: str,
        on_update: Callable[[TokenPriceSnapshot], None],
        *,
        include_token_details: bool = True,
        max_updates: Optional[int] = None,
        run_in_thread: bool = True,
    ) -> Optional[threading.Thread]:
        """
        Subscribe to Pump.fun bonding-curve account changes over Shyft WS.

        Emits an initial snapshot immediately, then live updates on accountNotify.
        """
        mint = (mint or "").strip()
        if not mint:
            raise ShyftPricingError("mint address is required")

        stop_event = threading.Event()
        self._stop_events[mint] = stop_event

        def _runner() -> None:
            try:
                asyncio.run(
                    self._stream_price_async(
                        mint,
                        on_update,
                        stop_event,
                        include_token_details=include_token_details,
                        max_updates=max_updates,
                    )
                )
            except Exception as exc:
                logger.exception("Shyft price stream crashed for mint=%s: %s", mint, exc)

        if run_in_thread:
            thread = threading.Thread(target=_runner, name=f"shyft-price-{mint[:8]}", daemon=True)
            self._stream_threads[mint] = thread
            thread.start()
            return thread

        _runner()
        return None

    async def _stream_price_async(
        self,
        mint: str,
        on_update: Callable[[TokenPriceSnapshot], None],
        stop_event: threading.Event,
        *,
        include_token_details: bool = True,
        max_updates: Optional[int] = None,
    ) -> None:
        details: dict[str, Any] = {}
        if include_token_details:
            try:
                details = await asyncio.to_thread(self.get_token_details, mint)
            except ShyftPricingError as exc:
                logger.warning("Stream token details unavailable for %s: %s", mint, exc)

        curve = self.bonding_curve_pda(mint)
        updates = 0
        delay = _WS_RECONNECT_DELAY_SEC

        # Initial snapshot via RPC
        try:
            snap = await asyncio.to_thread(self.get_latest_price, mint, include_token_details)
            on_update(snap)
            updates += 1
            if max_updates is not None and updates >= max_updates:
                return
        except ShyftPricingError as exc:
            logger.warning("Initial Shyft price snapshot failed for %s: %s", mint, exc)

        while not stop_event.is_set():
            try:
                logger.info(
                    "Connecting Shyft WS %s for mint=%s curve=%s",
                    _redact_url(self.ws_url),
                    mint,
                    curve,
                )
                async with ws_connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
                    sub_req = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "accountSubscribe",
                        "params": [
                            str(curve),
                            {"encoding": "base64", "commitment": "confirmed"},
                        ],
                    }
                    await ws.send(json.dumps(sub_req))
                    delay = _WS_RECONNECT_DELAY_SEC

                    while not stop_event.is_set():
                        try:
                            raw_msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue

                        try:
                            msg = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            logger.debug("Ignoring non-JSON WS frame")
                            continue

                        if "error" in msg:
                            logger.error("Shyft WS error for mint=%s: %s", mint, msg["error"])
                            break

                        # Subscription confirmation
                        if msg.get("id") == 1 and "result" in msg:
                            logger.info("Shyft accountSubscribe ok mint=%s sub=%s", mint, msg["result"])
                            continue

                        params = msg.get("params") or {}
                        result = params.get("result") or {}
                        value = result.get("value") or {}
                        data_field = value.get("data")
                        raw = self._decode_account_data(data_field)
                        if not raw:
                            continue

                        price_sol = self._price_sol_from_bonding_raw(raw)
                        if price_sol is None:
                            continue

                        sol_usd = await asyncio.to_thread(self.get_sol_usd)
                        usd = (price_sol * sol_usd) if sol_usd is not None else None
                        now = time.time()
                        snap = TokenPriceSnapshot(
                            mint=mint,
                            name=details.get("name"),
                            symbol=details.get("symbol"),
                            decimals=details.get("decimals"),
                            image=details.get("image"),
                            description=details.get("description"),
                            current_supply=details.get("current_supply"),
                            price_in_sol=price_sol,
                            usd_price=usd,
                            sol_price_usd=sol_usd,
                            source="shyft_ws_accountSubscribe",
                            bonding_curve=str(curve),
                            timestamp=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                            timestamp_unix=now,
                            raw_token_info=details.get("raw"),
                        )
                        on_update(snap)
                        updates += 1
                        if max_updates is not None and updates >= max_updates:
                            stop_event.set()
                            break

            except Exception as exc:
                if stop_event.is_set():
                    break
                logger.warning(
                    "Shyft WS disconnected (%s); reconnecting in %.1fs",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _WS_MAX_RECONNECT_DELAY_SEC)

        logger.info("Shyft price stream stopped for mint=%s", mint)


_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

_shared_service: Optional[ShyftPricingService] = None


def is_valid_mint(mint: str) -> bool:
    return bool(mint and _MINT_RE.match(mint.strip()))


def get_shyft_pricing_service(*, reset: bool = False) -> ShyftPricingService:
    """Return a shared ShyftPricingService (settings.py credentials)."""
    global _shared_service
    if reset or _shared_service is None:
        _shared_service = ShyftPricingService()
    return _shared_service


def get_token_price(
    token_mint: str,
    *,
    include_token_details: bool = True,
) -> dict[str, Any]:
    """
    Reusable project-wide helper: fetch current/live Shyft price for a mint.

    Example:
        from shyft_pricing import get_token_price
        price_data = get_token_price(token_mint)

    Returns a dict with fields such as:
      mint, name, symbol, decimals, price_in_sol, usd_price, sol_price_usd,
      source, bonding_curve, timestamp, timestamp_unix, ...

    Raises:
        ShyftPricingError: invalid mint or Shyft/RPC pricing failure.
    """
    mint = (token_mint or "").strip()
    if not mint:
        raise ShyftPricingError("mint address is required")

    service = get_shyft_pricing_service()
    snap = service.get_latest_price(mint, include_token_details=include_token_details)
    return snap.to_dict()


if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Fetch live Shyft token price")
    parser.add_argument("mint", help="Solana token mint address")
    parser.add_argument("--stream", action="store_true", help="Subscribe to live WS updates")
    parser.add_argument("--updates", type=int, default=3, help="Max live updates when --stream")
    args = parser.parse_args()

    try:
        print(json.dumps(get_token_price(args.mint), indent=2, default=str))
    except ShyftPricingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.stream:
        service = get_shyft_pricing_service()

        def _print(update: TokenPriceSnapshot) -> None:
            print(json.dumps({"live_update": update.to_dict()}, indent=2, default=str))

        service.stream_price(
            args.mint,
            _print,
            max_updates=args.updates,
            run_in_thread=True,
        )
        thread = service._stream_threads.get(args.mint)
        if thread:
            thread.join(timeout=120)
        service.stop_stream(args.mint)
