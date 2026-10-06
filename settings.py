import os
from dotenv import load_dotenv
from solana.rpc.api import Client

load_dotenv()

# Wallet key storage
# Dev (Windows):  set SECURE_DIRECTORY=C:\Users\...\Documents
# Client (Linux): set SECURE_DIRECTORY=/home/rwts/Documents
secure_directory = os.getenv("SECURE_DIRECTORY", "/home/user/Documents")

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
    # Client testing: stay on local node only — do not fall back to public mainnet
    _default_ws_fallback = ""
    _default_rpc_fallback = ""
else:
    _default_rpc = _MAINNET_RPC
    _default_ws = _MAINNET_WS
    _default_ws_fallback = ""  # dev: mainnet only, no localhost attempt
    _default_rpc_fallback = _MAINNET_RPC

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", _default_rpc)
SOLANA_RPC_URL_FALLBACK = os.getenv("SOLANA_RPC_URL_FALLBACK", _default_rpc_fallback)
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

# Minimum gap (seconds) between sniper get_transaction RPC calls (mint/buy
# resolution). Default is tuned for rate-limited public RPC; a dedicated
# local validator (USE_LOCAL_NODE=true) can usually handle a much lower value.
SNIPER_RPC_MIN_INTERVAL_SEC = float(os.getenv("SNIPER_RPC_MIN_INTERVAL_SEC", "0.25"))

# Bounded worker pools for sniper event processing (replaces one-OS-thread-
# per-event, which grows without limit under real load). Event pool handles
# quick RPC lookups (mint/buy resolution); decision pool handles the
# longer-lived buy-condition-check + buy-execution callback per new token.
SNIPER_EVENT_POOL_SIZE = int(os.getenv("SNIPER_EVENT_POOL_SIZE", "50"))
SNIPER_DECISION_POOL_SIZE = int(os.getenv("SNIPER_DECISION_POOL_SIZE", "20"))

# How long (seconds) to keep tracking a mint's buy stream after it launched.
# Past this, its buy-decision window has long closed, so we unsubscribe and
# stop tracking it — otherwise subscriptions (and event volume) grow forever.
SNIPER_MINT_TRACKING_TTL_SEC = float(os.getenv("SNIPER_MINT_TRACKING_TTL_SEC", "180"))

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

# ---------------------------------------------------------------------------
# Shyft (RPC + WebSocket + REST). Prefer env; defaults match prior local config.
# Override via SHYFT_API_KEY / SHYFET_API_KEY (and optional URL overrides).
# ---------------------------------------------------------------------------
SHYFT_API_KEY = os.getenv("SHYFT_API_KEY") or os.getenv(
    "SHYFET_API_KEY",
    "bOmjFPxty2EMMr6j",
)
SHYFT_RPC_URL = os.getenv(
    "SHYFT_RPC_URL",
    f"https://rpc.shyft.to?api_key={SHYFT_API_KEY}",
)
SHYFT_WS_URL = os.getenv(
    "SHYFT_WS_URL",
    f"wss://rpc.shyft.to?api_key={SHYFT_API_KEY}",
)
SHYFT_API_BASE = os.getenv("SHYFT_API_BASE", "https://api.shyft.to")
SHYFT_NETWORK = os.getenv("SHYFT_NETWORK", "mainnet-beta")

# Backward-compatible aliases (existing typo spellings)
shyfet_api_key = SHYFT_API_KEY
WEBSOCKET_KEY_SHYFET = SHYFT_WS_URL
SHYFET_RPC = SHYFT_RPC_URL
SHYFET_WS_URL = SHYFT_WS_URL

# ---------------------------------------------------------------------------
# Yellowstone / Geyser gRPC (sniper sell-flow price fetch only).
# USE_GEYSER=false keeps the sniper sell scheduler on the existing Shyft
# pricing path unchanged; set true once GEYSER_GRPC_URL/GEYSER_GRPC_TOKEN
# point at a real Yellowstone endpoint.
# ---------------------------------------------------------------------------
USE_GEYSER = os.getenv("USE_GEYSER", "false").lower() == "true"
GEYSER_GRPC_URL = os.getenv("GEYSER_GRPC_URL", "")
GEYSER_GRPC_TOKEN = os.getenv("GEYSER_GRPC_TOKEN", "")
GEYSER_USE_TLS = os.getenv("GEYSER_USE_TLS", "true").lower() == "true"
GEYSER_PRICE_TIMEOUT_SEC = float(os.getenv("GEYSER_PRICE_TIMEOUT_SEC", "8"))
