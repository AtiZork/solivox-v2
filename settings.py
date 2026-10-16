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
SNIPER_RPC_MIN_INTERVAL_SEC = max(0.0, float(os.getenv("SNIPER_RPC_MIN_INTERVAL_SEC", "0.25")))

# Bounded worker pools for sniper event processing (replaces one-OS-thread-
# per-event, which grows without limit under real load). Event pool handles
# quick RPC lookups (mint/buy resolution); decision pool handles the
# longer-lived buy-condition-check + buy-execution callback per new token.
SNIPER_EVENT_POOL_SIZE = int(os.getenv("SNIPER_EVENT_POOL_SIZE", "50"))
SNIPER_DECISION_POOL_SIZE = int(os.getenv("SNIPER_DECISION_POOL_SIZE", "20"))
# Concurrent price fetches per sniper sell cycle. Sells themselves stay
# sequential; raise with care, Shyft RPC/gRPC rate limits apply.
SNIPER_SELL_PRICE_WORKERS = int(os.getenv("SNIPER_SELL_PRICE_WORKERS", "10"))
# How often the backup sell-decision cycle re-evaluates every open trade.
# This is only the safety net: the primary path is the instant per-mint
# trigger (SNIPER_SELL_STREAM), which reacts the moment a token trades,
# not on this interval. Lowering this adds a price-fetch RPC round trip per
# open trade, per tick, through Shyft/Yellowstone — watch for 429s from
# Shyft's rate limit if you push this below ~1s with many open positions.
SNIPER_SELL_CYCLE_INTERVAL_SEC = float(os.getenv("SNIPER_SELL_CYCLE_INTERVAL_SEC", "1"))
# Instant sells: re-check an open trade the moment its token trades (node
# WebSocket logsSubscribe per open mint). The periodic cycle stays as a safety net.
SNIPER_SELL_STREAM = os.getenv("SNIPER_SELL_STREAM", "true").lower() == "true"
SNIPER_SELL_STREAM_SYNC_SEC = float(os.getenv("SNIPER_SELL_STREAM_SYNC_SEC", "5"))
SNIPER_SELL_EVENT_WORKERS = int(os.getenv("SNIPER_SELL_EVENT_WORKERS", "8"))
# After a sell attempt that can't succeed yet, wait before retrying the same
# trade: wallet holds 0 tokens -> EMPTY, any other failure -> RETRY.
SNIPER_SELL_EMPTY_COOLDOWN_SEC = float(os.getenv("SNIPER_SELL_EMPTY_COOLDOWN_SEC", "60"))
SNIPER_SELL_RETRY_COOLDOWN_SEC = float(os.getenv("SNIPER_SELL_RETRY_COOLDOWN_SEC", "2"))
# A Jupiter quote/swap 429 (rate limited) backs off this much longer than a
# normal failure before retrying. Retrying a 429 on the same short cooldown as
# any other error just keeps hammering a service that already asked to slow
# down - observed causing a large backlog of simultaneously-due sells to keep
# re-triggering 429s instead of draining.
SNIPER_SELL_RATE_LIMIT_COOLDOWN_SEC = float(os.getenv("SNIPER_SELL_RATE_LIMIT_COOLDOWN_SEC", "20"))
# Momentum-exit rule: a position whose token has had no detected trade for
# this long is sold in full, regardless of price - once a token stops
# trading, holding longer offers no new signal either way for a sniper
# strategy. Master switch + the threshold itself.
SNIPER_SELL_INACTIVITY_ENABLED = os.getenv("SNIPER_SELL_INACTIVITY_ENABLED", "true").lower() == "true"
SNIPER_SELL_INACTIVITY_SEC = float(os.getenv("SNIPER_SELL_INACTIVITY_SEC", "5"))
# How often the inactivity sweep compares "now" against each open mint's last
# detected trade. This is pure in-memory timestamp comparison (no RPC calls),
# so it can safely run far more often than the full price-fetch cycle - that
# is what lets it detect the threshold promptly instead of waiting on however
# long a full cycle pass over every open trade currently takes.
SNIPER_SELL_INACTIVITY_SWEEP_SEC = float(os.getenv("SNIPER_SELL_INACTIVITY_SWEEP_SEC", "0.25"))
# A trade whose wallet balance first reads 0 is retried on the short cooldown
# for this long (its buy may simply not have landed yet) before backing off
# for SNIPER_SELL_EMPTY_COOLDOWN_SEC.
SNIPER_SELL_EMPTY_GRACE_SEC = float(os.getenv("SNIPER_SELL_EMPTY_GRACE_SEC", "15"))

# How often (seconds) the sell bot re-checks "sent" sells against their actual
# on-chain outcome (RPC getSignatureStatuses). A send_transaction response only
# means the node accepted the tx for broadcast, not that it landed; this job
# catches the ones that failed on-chain and gives the tokens back to the open
# position instead of silently treating a failed send as a completed sale.
SNIPER_SELL_RECONCILE_INTERVAL_SEC = float(os.getenv("SNIPER_SELL_RECONCILE_INTERVAL_SEC", "10"))
# A "sent" sell still unconfirmed after this long is treated as failed/dropped
# (expired blockhash, node never forwarded it, etc.) and reverted.
SNIPER_SELL_RECONCILE_TIMEOUT_SEC = float(os.getenv("SNIPER_SELL_RECONCILE_TIMEOUT_SEC", "90"))

# How long (seconds) a fetched SOL/USD price is reused before refetching.
# get_sol_usd() is called on every single bonding-curve/PumpSwap price read
# (sell decisions included); SOL/USD moves negligibly within a couple of
# seconds, so caching it removes a full extra Shyft RPC round trip from the
# hot sell-price path. Same technique already used for 60s in the buy flow
# (sniper_stream._get_sol_usd_price); kept shorter here since sell decisions
# are more price-sensitive.
SOL_USD_PRICE_CACHE_TTL_SEC = float(os.getenv("SOL_USD_PRICE_CACHE_TTL_SEC", "1.5"))

# Interval for "[latency-summary]" log lines (aggregated sniper RPC / queue
# timings). 0 disables the summaries; per-buy/per-sell "[latency]" lines stay.
LATENCY_SUMMARY_INTERVAL_SEC = float(os.getenv("LATENCY_SUMMARY_INTERVAL_SEC", "30"))

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
# A mint whose Yellowstone accountSubscribe just timed out/errored (it has
# gone quiet - no new trade means no pushed update, ever, until it trades
# again) skips straight to the Shyft RPC fallback for this long instead of
# re-attempting Yellowstone (and paying GEYSER_PRICE_TIMEOUT_SEC again) on
# every subsequent sell-cycle tick. Measured against live open positions:
# with ~30 open quiet trades, re-attempting Yellowstone every tick meant a
# full price pass took 20-30s and exhausted the account's gRPC connection
# limit (observed PERMISSION_DENIED "Total connection limit reached").
# Shyft's RPC read is a live point-in-time read either way, so skipping
# Yellowstone during the cooldown costs no price freshness.
SNIPER_SELL_YELLOWSTONE_COOLDOWN_SEC = float(os.getenv("SNIPER_SELL_YELLOWSTONE_COOLDOWN_SEC", "60"))

# Buy-time entry-price reconciliation (autosnipe_buy_new_token.py). Entry
# price is computed from the confirmed buy transaction's own actual SOL
# spent / tokens received - not from Geyser and not from Shyft - a few
# seconds after the buy, so this never delays buy execution. A confirmed
# transaction's own balance change is a fixed historical fact: reading it
# 5 seconds or 5 minutes later gives the same answer either way.
SNIPER_BUY_RECONCILE_INTERVAL_SEC = float(os.getenv("SNIPER_BUY_RECONCILE_INTERVAL_SEC", "5"))
SNIPER_BUY_RECONCILE_TIMEOUT_SEC = float(os.getenv("SNIPER_BUY_RECONCILE_TIMEOUT_SEC", "90"))
