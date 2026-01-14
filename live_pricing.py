import asyncio
import aiohttp
import base64
import struct
import os
import json
import requests
from flask import Blueprint, jsonify, request
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from dotenv import load_dotenv

live_pricing_bp = Blueprint('live_pricing_bp', __name__)

# Load env vars from .env, then prefer RPC_API like the Node.js script
load_dotenv()
# RPC_URL = os.getenv("RPC_API", "http://127.0.0.1:8899")
RPC_URL = os.getenv("RPC_API", "https://api.mainnet-beta.solana.com")

DEX_PROGRAMS = {
    "Raydium": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "PumpSwap": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "Meteora": "M2mx93ekt1fmXSVkTrUL9xVFHkmME8HTUi5Cyc5aF7K",
}

# Default target token (can be overridden via query param "mint")
TARGET_TOKEN = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"

POOL_CACHE = {}
POOLS_DIR = os.path.join(os.path.dirname(__file__), "static", "pools")
POOL_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "static", "pool_registry.json")
REFRESH_INTERVAL = 60  # seconds


# ---------- Helper: Base58 encoding ----------
def base58_encode(b):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = int.from_bytes(b, 'big')
    encode = ''
    while num > 0:
        num, rem = divmod(num, 58)
        encode = alphabet[rem] + encode
    pad = 0
    for byte in b:
        if byte == 0:
            pad += 1
        else:
            break
    return '1' * pad + encode


# ---------- Parse pool data ----------
def parse_pool_data(data_base64):
    """Best-effort parser. Actual layouts differ by DEX; these offsets are placeholders.
    Returns (mint0, mint1, raw_bytes) or (None, None, raw_bytes) on failure.
    """
    raw_data = base64.b64decode(data_base64)
    try:
        token_mint_0 = base58_encode(raw_data[64:96])
        token_mint_1 = base58_encode(raw_data[96:128])
    except Exception:
        token_mint_0, token_mint_1 = None, None
    return token_mint_0, token_mint_1, raw_data


# ---------- Async pool fetcher ----------
async def get_dex_pools(session, program_id, dex_name):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getProgramAccounts",
        "params": [program_id, {"encoding": "base64"}],
    }
    async with session.post(RPC_URL, json=payload) as resp:
        try:
            data = await resp.json()
        except Exception:
            text = await resp.text()
            return dex_name, {"error": f"bad_response", "status": resp.status, "body": text}
        pools = data.get("result", [])
        print(f"[PoolFetch] {dex_name}: {len(pools)} pools fetched (status={resp.status})")
        return dex_name, pools


# ---------- Background refresher ----------
async def refresh_all_pools():
    async with aiohttp.ClientSession() as session:
        while True:
            print("\n[PoolRefresh] Refreshing DEX pools...")
            tasks = [get_dex_pools(session, pid, name) for name, pid in DEX_PROGRAMS.items()]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            updated = False
            for result in results:
                if isinstance(result, tuple):
                    dex_name, pools = result
                    if isinstance(pools, dict) and pools.get("error"):
                        print(f"[PoolRefresh] {dex_name} error: {pools}")
                    else:
                        _write_dex_pools_file(dex_name, pools)
                        updated = True

            print(f"[PoolRefresh] Disk cache updated.\n")
            await asyncio.sleep(REFRESH_INTERVAL)


# ---------- Run background task ----------
def start_background_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(refresh_all_pools())
    loop.run_forever()


# Start background pool sync thread (guard against Flask debug reloader)
import threading

if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.environ.get("FLASK_RUN_FROM_CLI"):
    threading.Thread(target=start_background_loop, daemon=True).start()


# ---------- Live token price endpoint ----------
@live_pricing_bp.route('/live_token_price', methods=['GET'])
def live_token_price():
    try:
        target_mint = request.args.get("mint", TARGET_TOKEN)
        if not target_mint:
            return jsonify({"status": "failed", "message": "Missing mint parameter"}), 400

        # 0) Try local registry (accurate vault-based pricing if entry exists)
        reg = _load_pool_registry()
        if reg:
            matches = [e for e in reg if e.get("base_mint") == target_mint or e.get("quote_mint") == target_mint]
            results = []
            for e in matches:
                pr = _price_from_registered_entry(e)
                if pr is not None:
                    results.append({"dex": e.get("dex"), **pr})
            if results:
                # pick best by higher quote reserve
                best = sorted(results, key=lambda x: x.get("quote_reserve", 0), reverse=True)[0]
                return jsonify({"matches": results, "best": best, "source": "registry"})

        matched = []
        for dex_name in DEX_PROGRAMS.keys():
            for pool in _iter_dex_pools_from_disk(dex_name):
                try:
                    data_base64 = pool["account"]["data"][0]
                except Exception:
                    continue

                token_mint_0, token_mint_1, raw_data = parse_pool_data(data_base64)

                if token_mint_0 is None or token_mint_1 is None:
                    continue

                if target_mint in (token_mint_0, token_mint_1):
                    # Placeholder reserve decoding; real layouts differ per DEX
                    try:
                        reserve_A = struct.unpack_from("<Q", raw_data, 128)[0]
                        reserve_B = struct.unpack_from("<Q", raw_data, 136)[0]
                    except Exception:
                        continue

                    # Assumed decimals for example only
                    price = (reserve_B / 1e6) / (reserve_A / 1e9) if reserve_A else 0
                    matched.append({
                        "dex": dex_name,
                        "price": price,
                        "base": token_mint_0,
                        "quote": token_mint_1
                    })
        if matched:
            # Choose best by highest quoted reserve as a naive liquidity proxy
            best = sorted(matched, key=lambda x: x.get("price", 0), reverse=True)[0]
            return jsonify({"matches": matched, "best": best, "source": "disk_scan_placeholder"})

        return jsonify(
            {"status": "failed", "message": "Token not found in any cached DEX pools", "mint": target_mint}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@live_pricing_bp.route('/refresh_pools', methods=['POST'])
def manual_refresh_pools():
    """Trigger an immediate background refresh tick."""
    try:
        threading.Thread(target=lambda: asyncio.run(refresh_once()), daemon=True).start()
        return jsonify({"status": "ok", "message": "Refresh triggered"})
    except Exception as e:
        return jsonify({"status": "failed", "error": str(e)}), 500


async def refresh_once():
    async with aiohttp.ClientSession() as session:
        tasks = [get_dex_pools(session, pid, name) for name, pid in DEX_PROGRAMS.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        updated = False
        for result in results:
            if isinstance(result, tuple):
                dex_name, pools = result
                if not (isinstance(pools, dict) and pools.get("error")):
                    _write_dex_pools_file(dex_name, pools)
                    updated = True


def _write_dex_pools_file(dex_name: str, pools: list):
    """Write pools to per-DEX NDJSON file without keeping them in memory."""
    try:
        os.makedirs(POOLS_DIR, exist_ok=True)
        tmp_path = os.path.join(POOLS_DIR, f"{dex_name}.ndjson.tmp")
        final_path = os.path.join(POOLS_DIR, f"{dex_name}.ndjson")
        with open(tmp_path, "w") as f:
            for p in pools:
                f.write(json.dumps(p) + "\n")
        os.replace(tmp_path, final_path)
        print(f"[PoolCache] {dex_name}: wrote {len(pools)} pools to {final_path}")
    except Exception as e:
        print(f"[PoolCache] {dex_name} write failed: {e}")


def _iter_dex_pools_from_disk(dex_name: str):
    """Yield pools from per-DEX NDJSON file line-by-line to avoid RAM usage."""
    path = os.path.join(POOLS_DIR, f"{dex_name}.ndjson")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception as e:
        print(f"[PoolCache] {dex_name} read failed: {e}")


# No RAM warm-load; pools are streamed from disk when needed.


# ---------- Local RPC helpers ----------
def _rpc_call(method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = requests.post(RPC_URL, json=payload, timeout=6)
        if r.ok:
            j = r.json()
            return j.get("result")
    except Exception as e:
        print(f"[RPC] {method} error: {e}")
    return None


# ---------- Pump.fun bonding curve helpers ----------
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
PYTH_SOL_PRICE_FEED = Pubkey.from_string("7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE")
TOKEN_METADATA_PROGRAM_ID = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")


def _pumpfun_bonding_curve_pda(token_mint_str: str) -> Pubkey:
    token_mint = Pubkey.from_string(token_mint_str)
    pda, _bump = Pubkey.find_program_address([b"bonding-curve", bytes(token_mint)], PUMP_FUN_PROGRAM_ID)
    return pda


def _get_account_info_base64(pubkey: Pubkey):
    """Use solana-py Client to fetch account info and return base64-encoded data.

    Handles multiple solana-py return shapes across versions.
    """
    client = Client(RPC_URL)
    resp = client.get_account_info(pubkey, encoding="base64")
    value = resp.value
    if value is None or value.data is None:
        return None
    data_field = getattr(value, "data", None)
    # Common case: list/tuple [base64_string, "base64"] or bytes list
    if isinstance(data_field, (list, tuple)):
        if data_field and isinstance(data_field[0], str):
            return data_field[0]
        if data_field and isinstance(data_field[0], (bytes, bytearray)):
            return base64.b64encode(bytes(data_field[0])).decode("ascii")
        if data_field and isinstance(data_field[0], list) and all(isinstance(x, int) for x in data_field[0]):
            return base64.b64encode(bytes(data_field[0])).decode("ascii")
    # Dict-like {"data": [..]}
    if isinstance(data_field, dict):
        inner = data_field.get("data")
        if isinstance(inner, (list, tuple)) and inner and isinstance(inner[0], str):
            return inner[0]
        if isinstance(inner, (bytes, bytearray)):
            return base64.b64encode(bytes(inner)).decode("ascii")
    # Direct bytes
    if isinstance(data_field, (bytes, bytearray)):
        return base64.b64encode(bytes(data_field)).decode("ascii")
    # Last resort: try to coerce
    try:
        return base64.b64encode(bytes(data_field)).decode("ascii")
    except Exception:
        return None


def _get_token_metadata_name_symbol(mint_str: str):
    """Fetch token name and symbol from Metaplex Metadata PDA, no third-party APIs.

    Layout (v1):
      - key: u8
      - update_authority: Pubkey (32)
      - mint: Pubkey (32)
      - name: string (u32 len + bytes)
      - symbol: string (u32 len + bytes)
      - uri: string (u32 len + bytes)
    We parse just name and symbol.
    """
    try:
        mint = Pubkey.from_string(mint_str)
        meta_pda, _ = Pubkey.find_program_address(
            [b"metadata", bytes(TOKEN_METADATA_PROGRAM_ID), bytes(mint)], TOKEN_METADATA_PROGRAM_ID
        )
        raw = _get_account_data_bytes(meta_pda)
        if not raw or len(raw) < 1 + 32 + 32 + 4:
            return None, None
        off = 0
        off += 1  # key
        off += 32  # update_authority
        off += 32  # mint
        # name
        if len(raw) < off + 4:
            return None, None
        name_len = int.from_bytes(raw[off:off + 4], "little", signed=False)
        off += 4
        if name_len < 0 or len(raw) < off + name_len:
            return None, None
        name = raw[off:off + name_len].decode("utf-8", errors="ignore").strip("\x00 ")
        off += name_len
        # symbol
        if len(raw) < off + 4:
            return name or None, None
        sym_len = int.from_bytes(raw[off:off + 4], "little", signed=False)
        off += 4
        if sym_len < 0 or len(raw) < off + sym_len:
            return name or None, None
        symbol = raw[off:off + sym_len].decode("utf-8", errors="ignore").strip("\x00 ")
        return (name or None), (symbol or None)
    except Exception:
        return None, None


def _parse_pumpfun_reserves_from_account_b64(data_in):
    """Accept base64 string, bytes, or list-of-ints and parse reserves."""
    raw = None
    if isinstance(data_in, (bytes, bytearray)):
        raw = bytes(data_in)
    elif isinstance(data_in, str):
        raw = base64.b64decode(data_in)
    elif isinstance(data_in, (list, tuple)):
        # Could be [base64str, "base64"] or list-of-ints
        if data_in and isinstance(data_in[0], str):
            raw = base64.b64decode(data_in[0])
        elif all(isinstance(x, int) for x in data_in):
            raw = bytes(data_in)
    if raw is None:
        try:
            raw = bytes(data_in)
        except Exception:
            return None, None
    # Offsets adjusted: u64 at 48 and 56 (little-endian) based on observed layout
    if len(raw) < 64:
        return None, None
    virtual_token_reserves = int.from_bytes(raw[48:56], "little", signed=False)
    virtual_sol_reserves = int.from_bytes(raw[56:64], "little", signed=False)
    return virtual_token_reserves, virtual_sol_reserves


def _get_account_data_bytes(pubkey: Pubkey):
    data_b64 = _get_account_info_base64(pubkey)
    if not data_b64:
        return None
    try:
        return base64.b64decode(data_b64)
    except Exception:
        return None


def _get_sol_price_usd_from_pyth() -> float | None:
    """Fetch SOL/USD price from Pyth price feed account by parsing raw bytes.

    Mirrors the JS logic:
      priceData = readBigInt64LE(73)
      exponent  = readInt32LE(89)
      realPrice = Number(priceData) * (10 ** exponent)
    """
    try:
        raw = _get_account_data_bytes(PYTH_SOL_PRICE_FEED)
        if not raw or len(raw) < 93:
            return None
        price_data = struct.unpack_from("<q", raw, 73)[0]
        exponent = struct.unpack_from("<i", raw, 89)[0]
        # exponent is typically negative
        real_price = float(price_data) * (10.0 ** float(exponent))
        return real_price
    except Exception:
        return None


def _read_u64_le(raw: bytes, offset: int) -> int:
    if raw is None or len(raw) < offset + 8:
        return 0
    return int.from_bytes(raw[offset:offset + 8], "little", signed=False)


def _get_associated_token_address(mint: Pubkey, owner: Pubkey) -> Pubkey:
    # ATA PDA seeds: [owner, TOKEN_PROGRAM_ID, mint] under Associated Token Program
    ata, _ = Pubkey.find_program_address([
        bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)
    ], ASSOCIATED_TOKEN_PROGRAM_ID)
    return ata


def _parse_pumpfun_reserves_from_bytes_heuristic(raw: bytes):
    """Try several offset pairs to find non-zero reserves in case layout changed.

    Returns (vt, vs, (vt_off, vs_off)) or (None, None, None) if not found.
    """
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 24:
        return None, None, None
    candidates = [
        (8, 16),  # original assumption
        (16, 24),
        (24, 32),
        (32, 40),
        (40, 48),
        (48, 56),
        (56, 64),
    ]
    # Try both orders (a->vt,b->vs) and swapped
    scored = []
    for a, b in candidates:
        if len(raw) < max(a, b) + 8:
            continue
        v1 = int.from_bytes(raw[a:a + 8], "little", signed=False)
        v2 = int.from_bytes(raw[b:b + 8], "little", signed=False)
        for (vt, vs, offs, swapped) in [
            (v1, v2, (a, b), False),
            (v2, v1, (b, a), True),
        ]:
            if vt <= 0 or vs <= 0:
                continue
            if vt >= (1 << 62) or vs >= (1 << 62):
                continue
            # Try both scale hypotheses for Pump.fun: 1000 and 1
            for scale in (1000.0, 1.0):
                price = float(vs) / (float(vt) * scale)
                # Keep only reasonable prices (avoid infinities and nonsense)
                if price > 0 and price < 1e6:
                    # Prefer prices in a typical range for SOL pairs
                    score = 0
                    if 1e-12 <= price <= 100:  # broad but sane
                        score += 2
                    if not swapped:
                        score += 1
                    scored.append((score, price, vt, vs, offs, scale))
    if not scored:
        return None, None, None
    scored.sort(key=lambda x: x[0], reverse=True)
    _score, _price, vt, vs, offs, scale = scored[0]
    # Return vt, vs with an annotation via a global so caller can retrieve scale
    return vt, vs, (offs[0], offs[1], scale)


def _coerce_account_data_to_bytes(data_in):
    """Return raw bytes from various account data representations."""
    if isinstance(data_in, (bytes, bytearray)):
        return bytes(data_in)
    if isinstance(data_in, str):
        try:
            return base64.b64decode(data_in)
        except Exception:
            return None
    if isinstance(data_in, (list, tuple)):
        if data_in and isinstance(data_in[0], str):
            try:
                return base64.b64decode(data_in[0])
            except Exception:
                return None
        if all(isinstance(x, int) for x in data_in):
            try:
                return bytes(data_in)
            except Exception:
                return None
    try:
        return bytes(data_in)
    except Exception:
        return None


@live_pricing_bp.route('/pump_price', methods=['POST'])
def pump_price():
    """Compute token price in SOL using Pump.fun bonding-curve, falling back to PumpSwap pool.

    Body JSON/form: { "mint": <token mint> }
    """
    payload = request.get_json(silent=True) or {}
    mint = payload.get("mint") or request.form.get("mint") or request.args.get("mint")
    mint = (mint or "").strip()
    if not mint:
        return jsonify({"status": "failed", "message": "Missing 'mint'"}), 400

    # 1) Try Pump.fun bonding-curve reserves at 8/16 like the JS reference
    try:
        mint_pk = Pubkey.from_string(mint)
    except Exception as e:
        return jsonify({"status": "failed", "message": f"Invalid mint: {e}"}), 400

    # Fetch token metadata (name, symbol) via Metaplex on-chain
    token_name, token_symbol = _get_token_metadata_name_symbol(mint)

    bonding_pda = _pumpfun_bonding_curve_pda(mint)
    raw = _get_account_data_bytes(bonding_pda)
    if raw:
        vt = _read_u64_le(raw, 8)
        vs = _read_u64_le(raw, 16)
        if vt > 0:
            price = float(vs) / (float(vt) * 1000.0)
            sol_price_usd = _get_sol_price_usd_from_pyth()
            resp = {
                "source": "pumpfun",
                "mint": mint,
                "token_name": token_name,
                "token_symbol": token_symbol,
                "bondingcurve": str(bonding_pda),
                "virtual_token_reserves": int(vt),
                "virtual_sol_reserves": int(vs),
                "price_in_sol": price,
                "rpc_url": RPC_URL,
            }
            if sol_price_usd is not None:
                resp["sol_price_usd"] = sol_price_usd
                resp["price_in_usd"] = price * sol_price_usd
            return jsonify(resp)

    # 2) Fallback to Pump AMM (PumpSwap) vault price
    # pool-authority PDA: ["pool-authority", mint] under Pump.fun program
    pool_auth_pda, _ = Pubkey.find_program_address([b"pool-authority", bytes(mint_pk)], PUMP_FUN_PROGRAM_ID)
    # pool PDA: ["pool", indexLE16(0), pool_auth_pda, mint, wsol]
    index_le16 = (0).to_bytes(2, byteorder="little", signed=False)
    pool_pda, _ = Pubkey.find_program_address(
        [b"pool", index_le16, bytes(pool_auth_pda), bytes(mint_pk), bytes(WSOL_MINT)],
        PUMP_AMM_PROGRAM_ID
    )
    base_vault = _get_associated_token_address(mint_pk, pool_pda)
    quote_vault = _get_associated_token_address(WSOL_MINT, pool_pda)

    base_amt, base_dec = _get_token_account_amount_and_decimals(str(base_vault))
    quote_amt, quote_dec = _get_token_account_amount_and_decimals(str(quote_vault))
    if base_amt is None or quote_amt is None or base_dec is None or quote_dec is None or base_amt == 0:
        return jsonify({
            "status": "failed",
            "message": "Unable to fetch pool vault balances",
            "pool": str(pool_pda),
            "base_vault": str(base_vault),
            "quote_vault": str(quote_vault),
        }), 502

    base_reserve = base_amt / (10 ** base_dec) if base_dec else 0.0
    quote_reserve = quote_amt / (10 ** quote_dec) if quote_dec else 0.0
    if base_reserve <= 0:
        return jsonify({"status": "failed", "message": "Base reserve is zero"}), 422
    price = quote_reserve / base_reserve
    sol_price_usd = _get_sol_price_usd_from_pyth()
    resp = {
        "source": "pumpswap",
        "mint": mint,
        "token_name": token_name,
        "token_symbol": token_symbol,
        "pool": str(pool_pda),
        "base_vault": str(base_vault),
        "quote_vault": str(quote_vault),
        "base_reserve": base_reserve,
        "quote_reserve": quote_reserve,
        "price_in_sol": price,
        "rpc_url": RPC_URL,
    }
    if sol_price_usd is not None:
        resp["sol_price_usd"] = sol_price_usd
        resp["price_in_usd"] = price * sol_price_usd
    return jsonify(resp)


def _get_token_account_amount_and_decimals(token_account: str):
    res = _rpc_call("getTokenAccountBalance", [token_account])
    if not res or not isinstance(res, dict) or "value" not in res:
        return None, None
    try:
        amount = int(res["value"]["amount"])  # raw amount (integer string)
        decimals = int(res["value"]["decimals"])  # token mint decimals
        return amount, decimals
    except Exception:
        return None, None


def _load_pool_registry():
    try:
        if os.path.exists(POOL_REGISTRY_PATH):
            with open(POOL_REGISTRY_PATH, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        print(f"[Registry] load failed: {e}")
    return []


def _save_pool_registry(entries: list):
    try:
        os.makedirs(os.path.dirname(POOL_REGISTRY_PATH), exist_ok=True)
        with open(POOL_REGISTRY_PATH, "w") as f:
            json.dump(entries, f)
        return True
    except Exception as e:
        print(f"[Registry] save failed: {e}")
        return False


def _price_from_registered_entry(entry: dict):
    try:
        base_vault = entry.get("base_vault")
        quote_vault = entry.get("quote_vault")
        base_mint = entry.get("base_mint")
        quote_mint = entry.get("quote_mint")
        if not base_vault or not quote_vault:
            return None
        base_amt, base_dec = _get_token_account_amount_and_decimals(base_vault)
        quote_amt, quote_dec = _get_token_account_amount_and_decimals(quote_vault)
        if base_amt is None or quote_amt is None or base_dec is None or quote_dec is None:
            return None
        base_reserve = base_amt / (10 ** base_dec) if base_dec else 0
        quote_reserve = quote_amt / (10 ** quote_dec) if quote_dec else 0
        if base_reserve <= 0:
            return None
        price = quote_reserve / base_reserve
        return {
            "price": price,
            "base": base_mint,
            "quote": quote_mint,
            "base_reserve": base_reserve,
            "quote_reserve": quote_reserve,
            "base_decimals": base_dec,
            "quote_decimals": quote_dec,
        }
    except Exception:
        return None
    try:
        amount = int(res["value"]["amount"])  # raw amount
        decimals = int(res["value"]["decimals"])  # decimals for this token mint
        return amount, decimals
    except Exception:
        return None, None


@live_pricing_bp.route('/price_from_vaults', methods=['GET'])
def price_from_vaults():
    """Compute price from two SPL token vault accounts using ONLY local RPC.

    Query params:
      - base_vault: SPL token account pubkey for base token reserve
      - quote_vault: SPL token account pubkey for quote token reserve

    Returns price = quote_reserve / base_reserve (quote per 1 base).
    """
    try:
        base_vault = request.args.get("base_vault")
        quote_vault = request.args.get("quote_vault")
        if not base_vault or not quote_vault:
            return jsonify({"status": "failed", "message": "Missing base_vault or quote_vault"}), 400

        base_amt, base_dec = _get_token_account_amount_and_decimals(base_vault)
        quote_amt, quote_dec = _get_token_account_amount_and_decimals(quote_vault)
        if base_amt is None or quote_amt is None or base_dec is None or quote_dec is None:
            return jsonify({"status": "failed", "message": "Unable to fetch vault balances"}), 502

        base_reserve = base_amt / (10 ** base_dec) if base_dec else 0
        quote_reserve = quote_amt / (10 ** quote_dec) if quote_dec else 0
        if base_reserve <= 0:
            return jsonify({"status": "failed", "message": "Base reserve is zero"}), 422

        price = quote_reserve / base_reserve
        sol_price_usd = _get_sol_price_usd_from_pyth()
        resp = {
            "price": price,
            "base_vault": base_vault,
            "quote_vault": quote_vault,
            "base_reserve": base_reserve,
            "quote_reserve": quote_reserve,
            "base_decimals": base_dec,
            "quote_decimals": quote_dec,
        }
        if sol_price_usd is not None:
            resp["sol_price_usd"] = sol_price_usd
            resp["price_in_usd"] = price * sol_price_usd
        return jsonify(resp)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@live_pricing_bp.route('/register_pool', methods=['POST'])
def register_pool():
    """Register or update a pool entry in the local registry.

    Body JSON fields:
      - dex: e.g. "Raydium", "PumpSwap"
      - base_mint, quote_mint
      - base_vault, quote_vault
    """
    try:
        payload = request.get_json(force=True)
        required = ["dex", "base_mint", "quote_mint", "base_vault", "quote_vault"]
        if not all(k in payload and payload[k] for k in required):
            return jsonify({"status": "failed", "message": f"Missing fields, required: {', '.join(required)}"}), 400
        entries = _load_pool_registry() or []
        # upsert by (dex, base_mint, quote_mint)
        key = (payload["dex"], payload["base_mint"], payload["quote_mint"])
        updated = False
        for i, e in enumerate(entries):
            if (e.get("dex"), e.get("base_mint"), e.get("quote_mint")) == key:
                entries[i] = payload
                updated = True
                break
        if not updated:
            entries.append(payload)
        if _save_pool_registry(entries):
            return jsonify({"status": "ok", "updated": updated, "count": len(entries)})
        return jsonify({"status": "failed", "message": "Unable to save registry"}), 500
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


def parse_raydium_amm_v4_pool(data_base64):
    """
    Parse Raydium AMM V4 pool account data.

    Raydium AMM V4 layout (partial):
    - status: u64 @ 0
    - nonce: u64 @ 8
    - orderNum: u64 @ 16
    - depth: u64 @ 24
    - coin_decimals: u64 @ 32
    - pc_decimals: u64 @ 40
    - state: u64 @ 48
    - reset_flag: u64 @ 56
    - min_size: u64 @ 64
    - vol_max_cut_ratio: u64 @ 72
    - amount_wave_ratio: u64 @ 80
    - coin_lot_size: u64 @ 88
    - pc_lot_size: u64 @ 96
    - min_price_multiplier: u64 @ 104
    - max_price_multiplier: u64 @ 112
    - sys_decimal_value: u64 @ 120
    - fees (multiple fields): @ 128-200+
    - pool_coin_token_account: Pubkey @ 208 (32 bytes)
    - pool_pc_token_account: Pubkey @ 240 (32 bytes)
    - coin_mint: Pubkey @ 272 (32 bytes)
    - pc_mint: Pubkey @ 304 (32 bytes)
    - lp_mint: Pubkey @ 336 (32 bytes)
    - amm_open_orders: Pubkey @ 368 (32 bytes)
    - serum_market: Pubkey @ 400 (32 bytes)
    - serum_program_id: Pubkey @ 432 (32 bytes)
    - amm_target_orders: Pubkey @ 464 (32 bytes)
    - pool_withdraw_queue: Pubkey @ 496 (32 bytes)
    - pool_temp_lp_token_account: Pubkey @ 528 (32 bytes)
    - amm_owner: Pubkey @ 560 (32 bytes)
    - pnl_owner: Pubkey @ 592 (32 bytes)
    """
    try:
        raw_data = base64.b64decode(data_base64)

        if len(raw_data) < 624:
            return None

        # Extract key fields
        coin_decimals = struct.unpack_from("<Q", raw_data, 32)[0]
        pc_decimals = struct.unpack_from("<Q", raw_data, 40)[0]

        # Token accounts (vaults)
        pool_coin_vault = base58_encode(raw_data[208:240])
        pool_pc_vault = base58_encode(raw_data[240:272])

        # Mints
        coin_mint = base58_encode(raw_data[272:304])
        pc_mint = base58_encode(raw_data[304:336])

        return {
            "coin_mint": coin_mint,
            "pc_mint": pc_mint,
            "coin_vault": pool_coin_vault,
            "pc_vault": pool_pc_vault,
            "coin_decimals": coin_decimals,
            "pc_decimals": pc_decimals,
        }
    except Exception as e:
        print(f"[Raydium] Parse error: {e}")
        return None


@live_pricing_bp.route('/raydium_price', methods=['GET'])
def raydium_price():
    """
    Fetch accurate price from Raydium pools for a given token.

    Query params:
      - mint: Token mint address
      - quote_mint: (optional) Specific quote token to filter by (e.g., WSOL, USDC)
    """
    try:
        target_mint = request.args.get("mint")
        quote_filter = request.args.get("quote_mint")

        if not target_mint:
            return jsonify({"status": "failed", "message": "Missing 'mint' parameter"}), 400

        # Check if we have Raydium pools cached
        raydium_pools = list(_iter_dex_pools_from_disk("Raydium"))

        if not raydium_pools:
            return jsonify({
                "status": "failed",
                "message": "No Raydium pools cached. Try calling /refresh_pools first."
            }), 404

        matching_pools = []

        for pool in raydium_pools:
            try:
                data_base64 = pool["account"]["data"][0]
                pool_info = parse_raydium_amm_v4_pool(data_base64)

                if not pool_info:
                    continue

                # Check if target mint is in this pool
                is_coin = pool_info["coin_mint"] == target_mint
                is_pc = pool_info["pc_mint"] == target_mint

                if not (is_coin or is_pc):
                    continue

                # Apply quote filter if specified
                if quote_filter:
                    quote_mint = pool_info["pc_mint"] if is_coin else pool_info["coin_mint"]
                    if quote_mint != quote_filter:
                        continue

                # Fetch vault balances
                coin_amt, _ = _get_token_account_amount_and_decimals(pool_info["coin_vault"])
                pc_amt, _ = _get_token_account_amount_and_decimals(pool_info["pc_vault"])

                if coin_amt is None or pc_amt is None or coin_amt == 0:
                    continue

                # Calculate reserves
                coin_reserve = coin_amt / (10 ** pool_info["coin_decimals"])
                pc_reserve = pc_amt / (10 ** pool_info["pc_decimals"])

                # Calculate price (quote per base)
                if is_coin:
                    # Target is coin side, price = pc/coin
                    price = pc_reserve / coin_reserve if coin_reserve > 0 else 0
                    base_mint = pool_info["coin_mint"]
                    quote_mint = pool_info["pc_mint"]
                else:
                    # Target is pc side, price = coin/pc
                    price = coin_reserve / pc_reserve if pc_reserve > 0 else 0
                    base_mint = pool_info["pc_mint"]
                    quote_mint = pool_info["coin_mint"]

                matching_pools.append({
                    "pool_address": pool["pubkey"],
                    "base_mint": base_mint,
                    "quote_mint": quote_mint,
                    "price": price,
                    "coin_reserve": coin_reserve,
                    "pc_reserve": pc_reserve,
                    "liquidity": coin_reserve * pc_reserve,  # Simple liquidity metric
                })

            except Exception as e:
                print(f"[Raydium] Error processing pool: {e}")
                continue

        if not matching_pools:
            return jsonify({
                "status": "failed",
                "message": f"No Raydium pools found for mint {target_mint}",
                "searched_pools": len(raydium_pools)
            }), 404

        # Sort by liquidity (highest first)
        matching_pools.sort(key=lambda x: x["liquidity"], reverse=True)

        # Get SOL price for USD conversion
        sol_price_usd = _get_sol_price_usd_from_pyth()

        best_pool = matching_pools[0]
        response = {
            "status": "success",
            "source": "raydium",
            "mint": target_mint,
            "best_pool": best_pool,
            "all_pools": matching_pools,
            "pool_count": len(matching_pools),
        }

        # Add USD price if quote is WSOL and we have SOL price
        if best_pool["quote_mint"] == str(WSOL_MINT) and sol_price_usd:
            response["price_in_usd"] = best_pool["price"] * sol_price_usd
            response["sol_price_usd"] = sol_price_usd

        return jsonify(response)

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500