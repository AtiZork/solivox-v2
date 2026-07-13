import os
from dotenv import load_dotenv
from solana.rpc.api import Client

load_dotenv()

# Wallet key storage
# Dev (Windows):  set SECURE_DIRECTORY=C:\Users\...\Documents
# Client (Linux): set SECURE_DIRECTORY=/home/rwts/Documents
secure_directory = os.getenv("SECURE_DIRECTORY", "/home/rwts/Documents")

# ---------------------------------------------------------------------------
# Solana RPC / WebSocket endpoints
#
# Client production (Sydney local validator) — DEFAULT for deploy:
#   USE_LOCAL_NODE=true   → http://127.0.0.1:8899 + ws://127.0.0.1:8900
#
# Development (no local validator):
#   USE_LOCAL_NODE=false  → public mainnet
#
# Or override individually via SOLANA_RPC_URL / SOLANA_WS_URL in .env
# ---------------------------------------------------------------------------
USE_LOCAL_NODE = os.getenv("USE_LOCAL_NODE", "true").lower() == "true"

_MAINNET_RPC = "https://api.mainnet-beta.solana.com"
_MAINNET_WS = "wss://api.mainnet-beta.solana.com"
_LOCAL_RPC = "http://127.0.0.1:8899"
_LOCAL_WS = "ws://127.0.0.1:8900"

if USE_LOCAL_NODE:
    _default_rpc = _LOCAL_RPC
    _default_ws = _LOCAL_WS
    _default_ws_fallback = _MAINNET_WS  # optional backup on client server
else:
    _default_rpc = _MAINNET_RPC
    _default_ws = _MAINNET_WS
    _default_ws_fallback = ""  # dev: mainnet only, no localhost attempt

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", _default_rpc)
SOLANA_RPC_URL_FALLBACK = os.getenv("SOLANA_RPC_URL_FALLBACK", _MAINNET_RPC)
SOLANA_WS_URL = os.getenv("SOLANA_WS_URL", _default_ws)
SOLANA_WS_URL_FALLBACK = os.getenv("SOLANA_WS_URL_FALLBACK", _default_ws_fallback)


def get_solana_ws_urls() -> list[str]:
    """Ordered WebSocket endpoints to try. Skips empty entries."""
    urls = []
    for url in (SOLANA_WS_URL, SOLANA_WS_URL_FALLBACK):
        if url and url not in urls:
            urls.append(url)
    return urls


solana_client = Client(SOLANA_RPC_URL)

PUMP_FUN_PROGRAM_ID_STR = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Sniper: WebSocket ingestion (Phase 1). Set false to fall back to HTTP polling scheduler.
SNIPER_USE_WEBSOCKET = os.getenv("SNIPER_USE_WEBSOCKET", "true").lower() == "true"
SNIPER_HTTP_FALLBACK = os.getenv("SNIPER_HTTP_FALLBACK", "true").lower() == "true"

# Dashboard live pricing: accountSubscribe on Pump.fun bonding curves → TokenPrice table.
PRICE_USE_WEBSOCKET = os.getenv("PRICE_USE_WEBSOCKET", "true").lower() == "true"
PRICE_HTTP_FALLBACK = os.getenv("PRICE_HTTP_FALLBACK", "true").lower() == "true"

moraliz_api_key = os.getenv(
    "MORALIS_API_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJub25jZSI6ImNmMTVhODM4LTk3NmUtNDUxNS05Njc3LTE0YTUyNWRjMTc4NCIsIm9yZ0lkIjoiNDQxMDc5IiwidXNlcklkIjoiNDUzNzk2IiwidHlwZUlkIjoiNjgwOThlNjctNjJkYS00YTNjLTk2MDctNjkzNzZiOGQyZWFkIiwidHlwZSI6IlBST0pFQ1QiLCJpYXQiOjE3NDQzNTIwMjUsImV4cCI6NDkwMDExMjAyNX0.OkLc-k1tRX-J0uZNJ30i3w8dcCGp5jHOI9VeOGnE5mc",
)
Solcan_api_key = os.getenv(
    "SOLSCAN_API_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjcmVhdGVkQXQiOjE3NDk2Mjg2OTE3MTIsImVtYWlsIjoibXVzYWRkYXFhYmJhczk2QGdtYWlsLmNvbSIsImFjdGlvbiI6InRva2VuLWFwaSIsImFwaVZlcnNpb24iOiJ2MiIsImlhdCI6MTc0OTYyODY5MX0.kKzTK1hz-c5NBw80Mb4P7hiIHbH81fUQwSVBS10L0Wo",
)
