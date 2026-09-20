"""Generate the Solivox Requirements & Architecture Word document from codebase review."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt


def add_heading(doc: Document, text: str, level: int = 1):
    return doc.add_heading(text, level=level)


def add_para(doc: Document, text: str, bold: bool = False):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = bold
    return p


def add_bullets(doc: Document, items: list[str]):
    for item in items:
        doc.add_paragraph(item, style="List Bullet")


def add_numbered(doc: Document, items: list[str]):
    for item in items:
        doc.add_paragraph(item, style="List Number")


def add_table(doc: Document, headers: list[str], rows: list[list[str]]):
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = "Table Grid"
    for i, h in enumerate(headers):
        table.rows[0].cells[i].text = h
    for r_i, row in enumerate(rows):
        for c_i, val in enumerate(row):
            table.rows[r_i + 1].cells[c_i].text = val
    doc.add_paragraph()
    return table


def build() -> Document:
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Inches(0.9)
    section.bottom_margin = Inches(0.9)
    section.left_margin = Inches(1.0)
    section.right_margin = Inches(1.0)

    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    # ---------------------------------------------------------------------
    # Title
    # ---------------------------------------------------------------------
    title = doc.add_heading("Solivox Project Documentation", 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub.add_run(
        f"Requirements, Architecture, Implementation Status & Workflows\n"
        f"Updated: {date.today().isoformat()}\n"
        f"Single source of truth based on codebase review + prior requirements document"
    )

    add_para(
        doc,
        "This document explains what Solivox is, what was originally required, what exists "
        "in the code today, what is new/current, and how the major trading flows work. "
        "Where the older documentation and the code disagree, the discrepancy is called out explicitly.",
    )

    # ---------------------------------------------------------------------
    # 1. Project Overview
    # ---------------------------------------------------------------------
    add_heading(doc, "1. Project Overview", 1)

    add_heading(doc, "1.1 What Solivox Does", 2)
    add_para(
        doc,
        "Solivox is a Solana-focused automated trading application. It provides a web dashboard "
        "where users manage wallets, create Long trades, configure Auto Sniper (Pump.fun) bots, "
        "monitor open positions with live prices, and automatically buy/sell tokens according "
        "to configurable rules.",
    )
    add_bullets(
        doc,
        [
            "Buy and sell Solana tokens through Jupiter swap APIs.",
            "Monitor positions with live USD pricing (Shyft / on-chain bonding curves).",
            "Automatically snipe newly created Pump.fun tokens based on transaction-count rules.",
            "Automatically sell Long and AutoSnipe positions based on profit/drop rules.",
            "Manage multiple wallets and JWT-authenticated users.",
        ],
    )

    add_heading(doc, "1.2 Overall Architecture", 2)
    add_para(
        doc,
        "Solivox is a Flask + Flask-SocketIO application backed by PostgreSQL (SQLAlchemy). "
        "Trading execution uses Jupiter for quotes/swaps and Solana RPC/WebSocket for chain I/O. "
        "Background APScheduler jobs and WebSocket listeners run alongside the HTTP server.",
    )
    add_bullets(
        doc,
        [
            "Entry point: main.py (port 8000).",
            "Database: PostgreSQL via SQLAlchemy models in models.py.",
            "Auth: JWT (flask-jwt-extended) for API access.",
            "Realtime UI: Socket.IO trade subscription (trades_ws.py) + live price stream.",
            "Config/env: settings.py, config.py, .env (RPC URLs, Shyft, sniper/price flags).",
        ],
    )

    add_heading(doc, "1.3 Major Components", 2)
    add_table(
        doc,
        ["Component", "Primary modules", "Role"],
        [
            ["Web UI / Dashboard", "templates/, static/js/", "Trade cards, AutoSnipe config, wallets, monitor"],
            ["Long Trade API", "trade.py, LongTrade.js", "Create/list/update Long buys; pending price triggers"],
            ["Long Auto-Sell", "long_sell_trade.py", "Scheduler that sells (and conditional-buys) Long trades"],
            ["Auto Sniper Config", "autosnipe.py, autosnipe.html", "CRUD for AutoSnipeConfig"],
            ["Sniper Ingestion", "sniper_stream.py, autosnipe_buy_new_token.py", "Detect Pump.fun creates; evaluate buy; execute Jupiter buy"],
            ["Sniper Auto-Sell", "autosnipe_sell_script.py, autosnipe_sell_logic.py", "Sell Autosnipe trades on drop/profit rules"],
            ["Pricing", "shyft_pricing.py, price_stream.py, init_scheduler.py", "USD price for decisions + dashboard"],
            ["Wallets", "wallet_managment.py", "Create/attach/fund wallets; keys on disk"],
            ["History / Logs", "trade_history.py, TradeHistory, TradeLog", "Executed fills and operational logs"],
            ["Charts (peripheral)", "charts.py", "DEXScreener/CoinGecko chart helpers (not core trading)"],
        ],
    )

    add_heading(doc, "1.4 How Components Work Together", 2)
    add_numbered(
        doc,
        [
            "User authenticates and selects/creates a wallet.",
            "For Long trades: user submits buy parameters → trade.py creates a Trade → Jupiter executes if buy_now, else pending → long_sell_trade monitors and sells.",
            "For Sniper: user saves AutoSnipeConfig → sniper_stream detects Pump.fun token creates → should_buy_token checks txn thresholds → Jupiter buy creates Trade(auto_snipe=True) → autosnipe_sell_script evaluates sell rules periodically.",
            "Dashboard receives live trade/price updates over Socket.IO and TokenPrice rows; user can manually buy more or sell percentages.",
            "Every successful swap writes a TradeHistory row; important bot events may write TradeLog rows.",
        ],
    )

    # ---------------------------------------------------------------------
    # 2. Initial Requirements
    # ---------------------------------------------------------------------
    add_heading(doc, "2. Initial Requirements (Original Scope)", 1)
    add_para(
        doc,
        "This section documents functionality that was part of the original Solivox requirements "
        "(from the prior requirements document and residual code/UI). Implementation status is "
        "stated honestly based on the current repository.",
    )

    add_heading(doc, "2.1 Wallet Management", 2)
    add_para(doc, "Original requirement: users can create wallets, attach existing wallets, and fund/cover wallets in the system.")
    add_para(doc, "Implementation status: Implemented and active.", bold=True)
    add_bullets(
        doc,
        [
            "Module: wallet_managment.py (blueprint wallet_bp).",
            "APIs include: POST /create_wallet, GET /get_wallets, POST /attach_wallet, POST /recover_wallet, POST /fund_wallet, POST /check_balance.",
            "Model: Wallet (user_id, public_key, title, private_key path, balance).",
            "Private keys are stored as filesystem paths under SECURE_DIRECTORY (not as raw key blobs in the DB).",
            "Frontend: my-wallets.html / wallet.js and wallet controls on the main UI.",
        ],
    )

    add_heading(doc, "2.2 Long Trades", 2)
    add_para(doc, "Original requirement: user buys a token for a trade; the trade can later be sold based on set parameters.")
    add_para(doc, "Implementation status: Implemented and active (core product path).", bold=True)
    add_bullets(
        doc,
        [
            "Create: POST /trade (trade.py) from LongTrade.js.",
            "Modes: buy_now (immediate Jupiter swap) or buy_token_if_price (pending until price triggers).",
            "Monitoring/sell: long_auto_sell_schedular in long_sell_trade.py filters Trade(executed=False, auto_snipe=False).",
            "Pricing for decisions: shyft_pricing.get_token_price.",
            "Execution: Jupiter quote + swap (WSOL ↔ token), then TradeHistory BUY/SELL rows.",
            "Manual overrides: PUT /sell_token/<id>/, PUT /buy_token/<id>/, auto-sell toggle endpoint.",
            "Key Long sell fields on Trade: sell_100_at_30_percent_drop, sell_100_after_100_percent_profit_drop, sell_at_200/300/500/1000/2000/10000_percent_profit, long_* gas/slippage, buy_if_price_up/down.",
        ],
    )
    add_para(
        doc,
        "Code note: long_auto_sell_schedular docstring mentions “Radium token migration”, but the "
        "filter and execution path are Long (non-autosnipe) Jupiter sells — not a Raydium-specific executor.",
    )

    add_heading(doc, "2.3 Sniper (AutoSnipe) — Original Concept", 2)
    add_para(
        doc,
        "Original requirement: user adds a sniper configuration; sniper scans/detects new tokens; "
        "matching tokens are bought automatically; sell parameters sell automatically; results appear on the home dashboard.",
    )
    add_para(doc, "Implementation status: Implemented and active (evolved into current AutoSnipe).", bold=True)
    add_bullets(
        doc,
        [
            "Config model: AutoSnipeConfig; APIs under /api/autosnipe* (autosnipe.py).",
            "Detection: Pump.fun program logs via WebSocket (sniper_stream.py); HTTP mint polling fallback exists.",
            "Buy gate: should_buy_token in autosnipe_buy_new_token.py (txn count ≥ USD threshold within launch_delay).",
            "Buy execution: Jupiter WSOL→token; Trade with auto_snipe=True, trade_kind=AUTOSNIPE.",
            "Sell: autosnipe_sell_script scheduler every ~5s using evaluate_autosnipe_sell_amount.",
            "UI: /auto-snipe page (autosnipe.html + autosnipe_manager.js); resulting trades listed on home via /get_trades.",
        ],
    )

    add_heading(doc, "2.4 Dashboard Monitoring (Original)", 2)
    add_para(doc, "Original requirements included continuous price updates, P/L, market cap, add-buy, percent sells, and transaction history from the dashboard.")
    add_para(doc, "Implementation status: Largely implemented on dashboard / base monitor UI.", bold=True)
    add_bullets(
        doc,
        [
            "Live updates: Socket.IO subscribe_trades (trades_ws.py) + TokenPrice stream (price_stream.py).",
            "Profit/Payout helpers: utils.calculate_profit_and_payout.",
            "Market cap: utils.get_estimated_market_cap (supply × USD price); shown in dashboard-logic.js.",
            "Manual percent sells (10/25/50/80/100) and exact amount sells via dashboard JS → sell APIs.",
            "Per-trade history API: GET /trade_history/<trade_id> (trade_history.py).",
        ],
    )

    add_heading(doc, "2.5 Local Solana Node Dependency (Original Ops Requirement)", 2)
    add_para(
        doc,
        "Original requirement: use a local Solana node for sniper detection and Long/Sniper buys/sells; "
        "avoid relying on public mainnet RPC for trading speed.",
    )
    add_para(doc, "Implementation status: Supported via configuration (environment-dependent).", bold=True)
    add_bullets(
        doc,
        [
            "settings.py: USE_LOCAL_NODE (default true) → http://127.0.0.1:8899 and ws://127.0.0.1:8900.",
            "Overrides: SOLANA_RPC_URL / SOLANA_WS_URL (and fallbacks).",
            "Sniper WebSocket and swap send_transaction use these endpoints.",
            "Actual speed/correctness depends on the operator running a healthy local validator/RPC.",
        ],
    )

    # ---- A. RAD ----
    add_heading(doc, "2.6 A. RAD / Radium Token Functionality (Initial Requirement)", 2)
    add_para(
        doc,
        "Historical intent (from UI remnants, Trade/PresignedTrade fields, and older naming): "
        "RAD (Radium/Raydium) trades would be configured similarly to Long trades, automatically bought "
        "per RAD configuration, then sold under RAD sell conditions.",
    )
    add_para(doc, "Implementation status in current codebase: Incomplete / not operational as a dedicated RAD trading path.", bold=True)

    add_heading(doc, "2.6.1 What Exists in Code", 3)
    add_bullets(
        doc,
        [
            "UI remnant: #radModal “RAD TRADE” in templates/base.html (entry button mostly commented out).",
            "Config form JS: static/js/js/tradeConfig.js posts named configs to POST /configurations with JSON config_data (drop/profit/rebuy/gas/slippage style fields).",
            "Model TradeConfiguration(name, config_data) intended to store named RAD configs.",
            "Trade and PresignedTrade columns: rad_slippage, rad_sell_slippage, rad_sell_gas_fee, plus older RAD-style sell tier fields (sell_20_at_200_percent_profit, rebuy_*, etc.).",
            "Raydium-related pricing helpers exist in live_pricing.py (/raydium_price, pool refresh) but are not a full RAD buy→sell bot.",
            "Standalone stub raydium_token_price.py (Coinvera placeholder) is unused by the app.",
        ],
    )

    add_heading(doc, "2.6.2 What Is Missing (Discrepancy vs Requirement)", 3)
    add_bullets(
        doc,
        [
            "No Flask route implements /configurations (frontend calls will fail).",
            "No dedicated Radium/Raydium swap executor that buys by RAD config and sells by RAD sell rules.",
            "Active automated trading paths are Long (Jupiter) and AutoSnipe (Jupiter), not Raydium AMM trading.",
            "Therefore the documented “complete RAD buy → monitor → sell flow” is not present as a working end-to-end feature today.",
        ],
    )

    add_heading(doc, "2.6.3 Intended / Residual Configuration Parameters (from UI/models)", 3)
    add_table(
        doc,
        ["Parameter (residual)", "Where seen", "Intended meaning"],
        [
            ["name + config_data", "TradeConfiguration / tradeConfig.js", "Named RAD configuration blob"],
            ["sell / rebuy / window fields", "tradeConfig.js defaults", "Sell tiers, rebuy after sell, timing windows"],
            ["buy_gas_fee / sell_gas_fee / slippage", "tradeConfig.js / Trade.rad_*", "Execution fees and slippage for RAD path"],
            ["rad_slippage / rad_sell_slippage / rad_sell_gas_fee", "Trade, PresignedTrade", "RAD-specific execution parameters"],
        ],
    )

    # ---- B. Pre-Signed ----
    add_heading(doc, "2.7 B. Pre-Signed Trade Functionality (Initial Requirement)", 2)
    add_para(
        doc,
        "Requirement intent: before a token launches, prepare/sign a transaction, store it, and execute "
        "immediately when the token goes live so the buy happens as soon as possible after launch.",
    )
    add_para(doc, "Implementation status: Data model only — create/store/execute workflow is not implemented.", bold=True)

    add_heading(doc, "2.7.1 PresignedTrade Model (models.py)", 3)
    add_bullets(
        doc,
        [
            "token_address, to_pubkey, amount, initial_price",
            "config_id → TradeConfiguration",
            "estimated_amount",
            "signed_transaction (Base64-encoded transaction text)",
            "live_time (UNIX timestamp intended as execution time)",
            "executed flag",
            "RAD-like sell parameters mirrored from older RAD design",
            "created_at / updated_at",
        ],
    )

    add_heading(doc, "2.7.2 Expected Flow vs Actual Code", 3)
    add_table(
        doc,
        ["Step", "Expected behavior", "Actual codebase"],
        [
            ["Create", "Build & sign Jupiter/Raydium TX before launch", "No service creates PresignedTrade rows"],
            ["Store", "Persist Base64 signed_transaction + live_time", "Schema exists; no writers found"],
            ["Trigger", "At launch / live_time, submit stored TX", "No scheduler/worker reads PresignedTrade"],
            ["Result", "Create Trade + TradeHistory", "Not implemented"],
            ["UI", "Manage presigned trades", "Nav label “Presigned trade” → transactions.html template shell only"],
        ],
    )
    add_para(
        doc,
        "Note: Other modules create temporary signed Jupiter transactions and send them immediately. "
        "Those are not the stored pre-launch PresignedTrade feature.",
    )

    # ---- C. Other chain ----
    add_heading(doc, "2.8 C. Other Chain Token Fetching (Including BOPS)", 2)
    add_para(
        doc,
        "Requirement intent: fetch/discover tokens from other chains/sources (example cited: BOPS).",
    )
    add_para(doc, "Implementation status: No BOPS integration found. Only peripheral multi-chain chart fetching exists.", bold=True)
    add_bullets(
        doc,
        [
            "Repository search finds no BOPS/bops module, API, or configuration.",
            "charts.py supports DEXScreener pair charts for chains such as ethereum, bsc, solana (query params).",
            "CoinGecko helper endpoints exist for Solana token price history in charts.py.",
            "Active sniper discovery is Solana Pump.fun only (sniper_stream.py).",
            "Legacy Moralis/Solscan listing helpers exist in older autosnipe_buy_script.py but are not started from main.py.",
        ],
    )
    add_para(
        doc,
        "Conclusion: multi-chain chart viewing is available as a side feature; automated trading/discovery "
        "for non-Solana tokens (including BOPS) is not part of the active application path.",
    )

    # ---- D. Transaction history ----
    add_heading(doc, "2.9 D. Transaction History Management", 2)
    add_para(doc, "Original requirement: manage transaction history for trades.")
    add_para(doc, "Implementation status: Implemented for executed fills + operational logs (with a few UI/API mismatches).", bold=True)

    add_heading(doc, "2.9.1 TradeHistory (executed swaps)", 3)
    add_bullets(
        doc,
        [
            "Fields: trade_id (FK to Trade), token_address, trade_type (BUY/SELL), trade_kind (LONG/AUTOSNIPE), amount, execution_price, tx_id, timestamp.",
            "Writers: trade.py, long_sell_trade.py, autosnipe_buy_new_token.py, autosnipe_sell_script.py, menual_sell_token.py.",
            "API: GET /trade_history/<trade_id> via trade_history.py.",
            "Relationship: one Trade can have many TradeHistory rows (buy and subsequent sells).",
        ],
    )

    add_heading(doc, "2.9.2 TradeLog (application/bot logs)", 3)
    add_bullets(
        doc,
        [
            "Fields: timestamp, level, message.",
            "Filled by DBLogHandler in sniper/sell scripts.",
            "Rendered on /logs page from main.py using TradeLog query.",
        ],
    )

    add_heading(doc, "2.9.3 Known discrepancies", 3)
    add_bullets(
        doc,
        [
            "Some frontend remnants call /api/trade_history or /get_logs, which are not registered routes.",
            "trade_kind casing can vary (AUTOSNIPE vs AutoSnipe) across writers — treat as string labels, not enums enforced in DB.",
        ],
    )

    # ---------------------------------------------------------------------
    # 3. Current / New Requirements
    # ---------------------------------------------------------------------
    add_heading(doc, "3. Current / New Requirements", 1)
    add_para(
        doc,
        "These items are identified as newer requirements in the prior requirements document and/or "
        "recently implemented features in the codebase. They extend or refine Sniper/Long/Dashboard behavior.",
    )

    add_heading(doc, "3.1 Sniper Sell Enable/Disable Toggles", 2)
    add_para(doc, "Requirement: six (paired) enable/disable controls on sniper sell conditions so individual drop rules can be turned off without deleting percentages.")
    add_para(doc, "Why: allow operators to disable specific auto-sell triggers while keeping others active.")
    add_para(doc, "Expected / implemented behavior:")
    add_bullets(
        doc,
        [
            "Fields: drop_cutoff_enabled, drop_after_100_enabled, drop_after_400_enabled (default True).",
            "Stored on AutoSnipeConfig and copied onto Trade at sniper buy time.",
            "Evaluated in autosnipe_sell_logic.sniper_flag_enabled / evaluate_autosnipe_sell_amount.",
            "UI toggles in autosnipe.html / autosnipe_manager.js and trade edit UI in base.html / Trading.js.",
            "Startup schema ensure in main.py adds missing columns on Postgres.",
        ],
    )
    add_para(doc, "Works alongside: existing drop_cutoff / drop_after_100 / drop_after_400 numeric thresholds.")

    add_heading(doc, "3.2 Sell at 100% Profit (sell_at_100)", 2)
    add_para(doc, "Requirement: add a profit-target sell field at 100% profit, matching the pattern of sell_at_200 / sell_at_400.")
    add_para(doc, "Expected / implemented behavior:")
    add_bullets(
        doc,
        [
            "Field sell_at_100 (default 10) on AutoSnipeConfig and Trade.",
            "At price multiplier ≥ 2.0 (100% profit), sell configured percent of position if higher tiers do not apply and trailing drop-sells do not fire.",
            "Configured in Auto Sniper modal and trade edit UI; copied on sniper buy.",
            "Logic: autosnipe_sell_logic.py; tests in tests/test_sniper_sell_enable.py.",
        ],
    )
    add_para(doc, "Extends: existing sell_at_200…sell_at_10000 profit ladder.")

    add_heading(doc, "3.3 Half-Transaction Extended Scan", 2)
    add_para(
        doc,
        "Requirement: if the normal scan window does not reach the full transaction threshold but reaches "
        "at least 50% of it, optionally continue scanning for an additional configured number of seconds.",
    )
    add_para(doc, "Example (configurable): threshold 10 txs ≥ $80, normal window 10s; if 5–9 qualify at 10s and feature enabled with +30s, keep scanning until start + 10 + 30 = 40s total.")
    add_para(doc, "Expected / implemented behavior:")
    add_bullets(
        doc,
        [
            "half_txns_scan_enabled (default False) — preserves old behavior when off.",
            "half_txns_scan_duration — additional seconds added on top of launch_delay (not “remaining until absolute N”).",
            "half_threshold = min_txns / 2 (dynamic; e.g. 80 → 40, 5 → 2.5).",
            "Helpers: autosnipe_buy_logic.py; applied in should_buy_token (WS and HTTP paths).",
            "UI: Auto Sniper configuration modal.",
            "Tests: tests/test_autosnipe_half_txns_scan.py.",
        ],
    )
    add_para(doc, "Extends: original min_txns + launch_delay buy gate. Does not restart the whole sniper process.")

    add_heading(doc, "3.4 Candlestick Charts on Dashboard", 2)
    add_para(doc, "Prior document note: candlestick charts (1m / 15m / 30m) on the dashboard are not included in current scope.")
    add_para(doc, "Status: Out of scope / not a current deliverable for the sniper board work.", bold=True)
    add_para(doc, "Peripheral chart code exists in charts.py (DEXScreener/CoinGecko) but is separate from the main dashboard trade monitor.")

    add_heading(doc, "3.5 Long Trade Field Updates", 2)
    add_para(doc, "Requirement: Long trade sell fields continue to drive Long auto-sell behavior and can be updated from UI.")
    add_para(doc, "Status: Active. Long sell parameters are editable via trade update APIs/UI; long_sell_trade.py consumes them.")

    add_heading(doc, "3.6 Rebuy After Resell in Sniper", 2)
    add_para(doc, "Prior document lists “Rebuy after resell in sniper bot” as a new requirement.")
    add_para(doc, "Implementation status: Not implemented in the active AutoSnipe sell path.", bold=True)
    add_bullets(
        doc,
        [
            "rebuy_* fields exist on Trade / PresignedTrade / RAD tradeConfig defaults.",
            "autosnipe_sell_logic.py and autosnipe_sell_script.py do not implement sniper rebuy-after-sell.",
            "Treat as pending/new requirement relative to current sniper code.",
        ],
    )

    add_heading(doc, "3.7 Shyft Live Pricing (Current Technical Direction)", 2)
    add_para(doc, "Current trading/monitor pricing path uses Shyft + on-chain bonding curve logic (shyft_pricing.py), with WebSocket accountSubscribe for dashboard prices and HTTP fallback scheduler.")
    add_para(doc, "Replaces/extends older Jupiter metadata price helper usage for decisioning; Jupiter remains the swap venue.")

    # ---------------------------------------------------------------------
    # 4. Initial vs Current
    # ---------------------------------------------------------------------
    add_heading(doc, "4. Initial vs Current Functionality", 1)

    add_table(
        doc,
        ["Area", "Originally required", "Current status"],
        [
            ["Wallets", "Create/attach/fund", "Active"],
            ["Long Trade buy/sell", "Yes", "Active (Jupiter)"],
            ["Sniper config + auto buy/sell", "Yes", "Active (Pump.fun + Jupiter); enhanced with new fields"],
            ["Dashboard live P/L / mcap / manual sell", "Yes", "Active"],
            ["Local node for speed", "Yes", "Configurable (USE_LOCAL_NODE)"],
            ["RAD / Radium dedicated bot", "Yes (historical)", "Incomplete; UI/model remnants only"],
            ["Pre-signed pre-launch trades", "Yes (historical)", "Model only; no executor"],
            ["Other-chain / BOPS fetch", "Cited historically", "No BOPS; charts only for other chains"],
            ["Transaction history", "Yes", "Active via TradeHistory (+ TradeLog)"],
            ["Half-txn extended scan", "New", "Implemented"],
            ["Sniper sell enable toggles", "New", "Implemented"],
            ["sell_at_100", "New", "Implemented"],
            ["Sniper rebuy after resell", "New (listed)", "Not implemented"],
            ["Dashboard candlesticks", "New but out of scope", "Not in scope"],
        ],
    )

    add_heading(doc, "4.1 Still Active", 2)
    add_bullets(
        doc,
        [
            "Long trades and Long auto-sell scheduler",
            "AutoSnipe config, Pump.fun detection, buy gate, Jupiter buy, sniper auto-sell",
            "Wallet management, JWT auth",
            "Dashboard monitoring (prices, P/L, market cap, manual sells)",
            "TradeHistory / TradeLog",
            "Local-node capable RPC/WS configuration",
        ],
    )

    add_heading(doc, "4.2 Changed / Replaced", 2)
    add_bullets(
        doc,
        [
            "Pricing decision path moved toward Shyft/on-chain rather than Jupiter price helpers.",
            "Sniper ingestion prefers WebSocket Pump.fun logs (sniper_stream) over older Moralis polling (commented out).",
            "Sniper sell rules gained enable flags, sell_at_100, and half-txn extended scan.",
            "Older autosnipe_logic /start_all path is not auto-started from main.py (legacy parallel architecture).",
        ],
    )

    add_heading(doc, "4.3 No Longer / Not in Current Working Scope", 2)
    add_bullets(
        doc,
        [
            "Operational RAD Raydium auto-trade bot (not executable end-to-end).",
            "Operational PresignedTrade pre-launch pipeline.",
            "BOPS / automated other-chain sniping.",
            "Dashboard candlestick charting as a scoped deliverable.",
        ],
    )

    # ---------------------------------------------------------------------
    # 5. Technical Implementation
    # ---------------------------------------------------------------------
    add_heading(doc, "5. Technical Implementation", 1)

    add_heading(doc, "5.1 Backend Architecture", 2)
    add_bullets(
        doc,
        [
            "Flask app in main.py with CORS, JWT, SQLAlchemy, SocketIO.",
            "Blueprints registered for trades, wallets, autosnipe, history, charts, pricing, auth, etc.",
            "On startup (reloader child): create_scheduler (prices), auto_snipe_auto_sell_schedular, long_auto_sell_schedular, start_sniper_ingestion.",
            "db.create_all() plus light ALTER helper for newer sniper columns.",
        ],
    )

    add_heading(doc, "5.2 Important Modules / Files", 2)
    add_table(
        doc,
        ["File", "Purpose"],
        [
            ["main.py", "App entry, routes for pages, blueprint registration, job startup"],
            ["models.py", "ORM models"],
            ["settings.py", "RPC/WS, feature flags, API keys"],
            ["trade.py", "Long trade create/list/update/manual buy"],
            ["long_sell_trade.py", "Long auto buy-trigger/sell scheduler"],
            ["autosnipe.py", "AutoSnipeConfig API"],
            ["autosnipe_buy_new_token.py", "Buy gate + Jupiter sniper buy + ingestion glue"],
            ["autosnipe_buy_logic.py", "Half-txn scan helpers"],
            ["sniper_stream.py", "Pump.fun WS create/buy counters"],
            ["autosnipe_sell_script.py", "Sniper sell scheduler"],
            ["autosnipe_sell_logic.py", "Sniper sell decision pure logic"],
            ["shyft_pricing.py", "get_token_price"],
            ["price_stream.py / init_scheduler.py", "Live TokenPrice updates"],
            ["wallet_managment.py", "Wallet APIs"],
            ["trades_ws.py", "Socket.IO trade push"],
            ["menual_sell_token.py", "Manual sells"],
        ],
    )

    add_heading(doc, "5.3 APIs (High Level)", 2)
    add_bullets(
        doc,
        [
            "Auth: user_register.py (register/login JWT).",
            "Wallets: /create_wallet, /get_wallets, /attach_wallet, …",
            "Long: POST /trade, GET /get_trades, PUT /trade/<id>/, manual buy/sell endpoints.",
            "AutoSnipe: GET/POST /api/autosnipe, /api/autosnipe/list, PUT/DELETE /api/autosnipe/<id>.",
            "History: GET /trade_history/<trade_id>.",
            "Live pricing helpers: live_pricing_bp endpoints; TokenPrice APIs in token_price.py.",
        ],
    )

    add_heading(doc, "5.4 Background Jobs / Workers", 2)
    add_table(
        doc,
        ["Job", "Interval / mode", "Module"],
        [
            ["Sniper WS ingestion", "Continuous", "sniper_stream / start_sniper_ingestion"],
            ["Sniper HTTP detect fallback", "When WS unavailable", "autosnipe_buy_new_token detect loop"],
            ["Sniper auto-sell", "~5 seconds", "autosnipe_sell_script"],
            ["Long auto-sell/buy triggers", "Tight loop in scheduler job", "long_sell_trade"],
            ["Price WS + HTTP fallback", "Continuous / 5–30s", "price_stream / init_scheduler"],
        ],
    )

    add_heading(doc, "5.5 Token Detection / Scanning", 2)
    add_bullets(
        doc,
        [
            "Primary: logsSubscribe to Pump.fun program; parse Create; track per-mint buys.",
            "Buy condition scan duration: AutoSnipeConfig.launch_delay seconds.",
            "Qualifying txn: USD value ≥ buy_txns_over_80_usd; need min_txns such txs.",
            "Optional half-txn extension when enabled.",
            "Legacy Moralis new-token polling exists but is not started by main.py.",
        ],
    )

    add_heading(doc, "5.6 Buy and Sell Flows (Technical)", 2)
    add_para(doc, "Common swap pattern: Jupiter /swap/v1/quote → /swap → VersionedTransaction → sign with wallet seed file → solana_client.send_transaction → TradeHistory.")
    add_para(doc, "Sniper buy also guards against duplicates via in-memory BOUGHT_TOKENS and wallet lock.")

    add_heading(doc, "5.7 Pricing Flow", 2)
    add_bullets(
        doc,
        [
            "Decision helper: shyft_pricing.get_token_price(mint) → usdPrice, name, symbol.",
            "Dashboard: price_stream accountSubscribe on bonding curves → TokenPrice rows.",
            "Fallback HTTP polling of open trades when WS pricing disabled/unavailable.",
            "Market cap estimate: circulating/adjusted supply × USD price.",
        ],
    )

    add_heading(doc, "5.8 Wallet Management", 2)
    add_para(doc, "See §2.1. Keys on disk under SECURE_DIRECTORY; public keys in DB; balances fetched live from RPC.")

    add_heading(doc, "5.9 Configuration Management", 2)
    add_bullets(
        doc,
        [
            "Long parameters live on each Trade row (and can be updated).",
            "Sniper parameters live on AutoSnipeConfig and are copied onto Trade at buy.",
            "Named TradeConfiguration JSON was intended for RAD named presets (API incomplete).",
            "Env feature flags in settings.py control local node, sniper WS, price WS.",
        ],
    )

    add_heading(doc, "5.10 External Services", 2)
    add_table(
        doc,
        ["Service", "Use"],
        [
            ["Solana RPC/WS (local or mainnet)", "Detection subscriptions, send_transaction, balances"],
            ["Jupiter API", "Swap quotes and swap transactions"],
            ["Shyft", "Token metadata/pricing related calls"],
            ["Pyth (via pricing stack)", "SOL/USD reference where used"],
            ["DEXScreener / CoinGecko", "Optional charts"],
            ["Moralis / Solscan", "Legacy discovery helpers (not primary path)"],
        ],
    )

    add_heading(doc, "5.11 Frontend", 2)
    add_bullets(
        doc,
        [
            "base.html: home/pending trades, Long modal, residual RAD modal, trade edit.",
            "autosnipe.html: multi AutoSniper cards/config.",
            "dashboard.html: live monitor.",
            "my-wallets.html, logs.html, page-login/register.",
            "Key JS: Trading.js, LongTrade.js, autosnipe_manager.js, dashboard-logic.js, wallet.js.",
        ],
    )

    # ---------------------------------------------------------------------
    # 6. End-to-End Workflows
    # ---------------------------------------------------------------------
    add_heading(doc, "6. End-to-End Workflows", 1)

    add_heading(doc, "6.1 RAD Token Flow (Documented Intent vs Reality)", 2)
    add_numbered(
        doc,
        [
            "Intended: user creates RAD configuration (similar to Long) via RAD modal → POST /configurations.",
            "Intended: system buys Radium tokens per config and monitors/sells with RAD sell rules.",
            "Actual: modal/JS and DB fields exist; /configurations API and RAD executor are missing → flow stops at configuration submit.",
        ],
    )

    add_heading(doc, "6.2 Long Trade Flow", 2)
    add_numbered(
        doc,
        [
            "User selects wallet, token mint, amount, sell parameters, and buy_now or price-trigger mode.",
            "POST /trade creates Trade (trade_kind LONG, auto_snipe False).",
            "If buy_now: Jupiter buy executes; TradeHistory BUY written; purchased_token_amount set.",
            "If pending: scheduler watches buy_if_price_up/down and may buy later.",
            "long_auto_sell_schedular evaluates drop/profit rules using Shyft price.",
            "On match: Jupiter sell token→WSOL; TradeHistory SELL; may mark executed when fully done.",
            "Dashboard can also trigger manual percent/amount sells.",
        ],
    )

    add_heading(doc, "6.3 Pre-Signed Trade Flow (Intended)", 2)
    add_numbered(
        doc,
        [
            "Intended: before launch, build and sign TX; store PresignedTrade(signed_transaction, live_time, config_id).",
            "Intended: at launch/live_time, broadcast stored TX; create Trade + history.",
            "Actual: only the table/model exists — no create/execute worker.",
        ],
    )

    add_heading(doc, "6.4 Token Detection / Scanning Flow (Sniper)", 2)
    add_numbered(
        doc,
        [
            "start_sniper_ingestion starts WS listener (if SNIPER_USE_WEBSOCKET).",
            "Pump.fun Create log detected → mint extracted → autosnipe_buy_new_token(mint, active configs).",
            "should_buy_token polls WS counters or HTTP txns for launch_delay seconds.",
            "If qualifying ≥ min_txns → buy True.",
            "Else if half scan enabled and qualifying ≥ min_txns/2 → continue for additional half_txns_scan_duration seconds.",
            "Else skip.",
        ],
    )

    add_heading(doc, "6.5 Sniper Buy Flow", 2)
    add_numbered(
        doc,
        [
            "Buy conditions met → buy_token (wallet lock).",
            "Skip if already in BOUGHT_TOKENS.",
            "Load wallet key file; Jupiter quote WSOL→mint; sign/send.",
            "Create Trade(auto_snipe=True) copying sniper sell/enable fields from config.",
            "Write TradeHistory BUY; mark token bought.",
        ],
    )

    add_heading(doc, "6.6 Sniper Sell Flow", 2)
    add_numbered(
        doc,
        [
            "Every ~5s, load open autosnipe trades.",
            "Fetch current USD price via get_token_price.",
            "evaluate_autosnipe_sell_amount applies drop cutoff, profit ceiling, trailing drops, then profit-target partials (incl. sell_at_100).",
            "If amount > 0: Jupiter sell; write TradeHistory; update remaining purchased amount / executed as applicable.",
        ],
    )

    add_heading(doc, "6.7 Transaction History Flow", 2)
    add_numbered(
        doc,
        [
            "Any successful swap path constructs TradeHistory with trade_id, type, kind, amount, price, tx_id.",
            "Dashboard/monitor requests GET /trade_history/<trade_id> to display fills.",
            "Operational messages may also appear in TradeLog on /logs.",
        ],
    )

    add_heading(doc, "6.8 Wallet Flow", 2)
    add_numbered(
        doc,
        [
            "Create: generate keypair, write key file, insert Wallet row.",
            "Attach/recover: import existing key material into SECURE_DIRECTORY + DB.",
            "Fund/check: RPC airdrop (dev-style) / get_balance.",
            "Trading modules resolve Wallet by public_key / user_id and read key path for signing.",
        ],
    )

    add_heading(doc, "6.9 Current New Functionality Flow (Half-Txn + Enable Flags + sell_at_100)", 2)
    add_numbered(
        doc,
        [
            "Operator configures AutoSnipe including half_txns_scan_* , enable toggles, sell_at_100.",
            "Save via /api/autosnipe.",
            "On new mint, buy gate may extend scan when half-threshold met.",
            "On buy, Trade stores enable flags and sell_at_100.",
            "Sell scheduler respects toggles and 100% profit partial sell tier.",
        ],
    )

    # ---------------------------------------------------------------------
    # 7. Configuration
    # ---------------------------------------------------------------------
    add_heading(doc, "7. Configuration Reference", 1)

    add_heading(doc, "7.1 AutoSnipeConfig (Active Sniper)", 2)
    add_table(
        doc,
        ["Field", "Default", "Effect"],
        [
            ["buy_txns_over_80_usd", "80", "Minimum USD size for a txn to count as qualifying"],
            ["min_txns", "5", "Number of qualifying txns required to buy"],
            ["launch_delay", "5", "Normal scan window in seconds"],
            ["half_txns_scan_enabled", "False", "If true, allow extended scan when ≥50% of min_txns"],
            ["half_txns_scan_duration", "30", "Additional seconds added after launch_delay when extending"],
            ["buy_amount", "1.0", "SOL to spend on sniper buy"],
            ["slippage", "100", "Buy slippage % (also influences autosnipe sell slippage copy)"],
            ["priority_fee", "0.01", "Priority fee SOL for buy"],
            ["drop_cutoff", "30", "Sell all if price drops this % from buy"],
            ["drop_cutoff_enabled", "True", "Toggle for drop_cutoff"],
            ["drop_until_profit", "99", "Stop further sells once profit% reaches this ceiling"],
            ["drop_after_100", "50", "After ≥100% profit, sell all if drop from peak ≥ this %"],
            ["drop_after_100_enabled", "True", "Toggle for drop_after_100"],
            ["drop_after_400", "30", "After ≥400% profit, sell all if drop from peak ≥ this %"],
            ["drop_after_400_enabled", "True", "Toggle for drop_after_400"],
            ["sell_at_100 … sell_at_10000", "10", "Partial sell % at corresponding profit multipliers"],
            ["active", "True", "Whether this config participates in sniping"],
        ],
    )

    add_heading(doc, "7.2 Long Trade Key Fields", 2)
    add_table(
        doc,
        ["Field", "Role"],
        [
            ["buy_now / buy_token_if_price", "Immediate buy vs wait for price triggers"],
            ["buy_if_price_up / buy_if_price_down", "Pending buy triggers"],
            ["sell_100_at_30_percent_drop", "Full sell on drop from entry"],
            ["sell_100_after_100_percent_profit_drop", "Full sell on peak drop after 100% profit"],
            ["sell_at_*_percent_profit", "Partial sells at profit tiers"],
            ["long_slippage / long_sell_slippage / gas fees", "Execution parameters"],
            ["auto_sell", "UI/API toggle related to allowing auto sell behavior"],
        ],
    )

    add_heading(doc, "7.3 Environment / Runtime Settings", 2)
    add_table(
        doc,
        ["Setting", "Purpose"],
        [
            ["USE_LOCAL_NODE", "Prefer localhost RPC/WS"],
            ["SOLANA_RPC_URL / SOLANA_WS_URL", "Explicit endpoints"],
            ["SNIPER_USE_WEBSOCKET / SNIPER_HTTP_FALLBACK", "Sniper ingestion mode"],
            ["PRICE_USE_WEBSOCKET / PRICE_HTTP_FALLBACK", "Dashboard pricing mode"],
            ["SECURE_DIRECTORY", "Wallet key file directory"],
            ["SHYFT_API_KEY", "Shyft authentication"],
            ["API_KEY", "Optional Jupiter paid API"],
        ],
    )

    # ---------------------------------------------------------------------
    # 8. Database
    # ---------------------------------------------------------------------
    add_heading(doc, "8. Database Models", 1)
    add_table(
        doc,
        ["Model / Table", "Purpose", "Notes"],
        [
            ["User", "Authentication accounts", "password_hash, optional email"],
            ["Wallet", "User wallets", "private_key stores file path"],
            ["AutoSnipeConfig", "Sniper configurations", "Buy/scan/sell/enable fields"],
            ["TradeConfiguration", "Named JSON configs", "Intended for RAD presets; API incomplete"],
            ["Trade", "Open/managed positions", "LONG + AUTOSNIPE + residual RAD columns"],
            ["TokenPrice", "Price samples per trade", "Dashboard/live pricing"],
            ["PresignedTrade", "Pre-signed TX storage", "Unused by runtime"],
            ["TradeHistory", "Executed BUY/SELL fills", "Links to Trade"],
            ["TradeLog", "Operational log lines", "Shown on /logs"],
        ],
    )

    # ---------------------------------------------------------------------
    # 9. Current Project Status
    # ---------------------------------------------------------------------
    add_heading(doc, "9. Current Project Status", 1)

    add_heading(doc, "9.1 Completed / Existing (Active)", 2)
    add_bullets(
        doc,
        [
            "User auth + wallet management",
            "Long trade create/monitor/auto-sell/manual sell",
            "AutoSnipe configuration CRUD",
            "Pump.fun WS sniper detection + Jupiter buy",
            "Sniper auto-sell with enable flags, sell_at_100, half-txn extended scan",
            "Shyft-oriented pricing + live TokenPrice updates",
            "Dashboard monitor features (P/L, market cap, percent sells)",
            "TradeHistory persistence and per-trade history API",
            "Local-node capable deployment settings",
        ],
    )

    add_heading(doc, "9.2 New / Current Functionality (Recently Added)", 2)
    add_bullets(
        doc,
        [
            "half_txns_scan_enabled / half_txns_scan_duration",
            "drop_*_enabled toggles",
            "sell_at_100 profit target",
            "Shyft pricing integration as primary decision price source",
        ],
    )

    add_heading(doc, "9.3 Known Gaps / Pending / Incomplete", 2)
    add_bullets(
        doc,
        [
            "RAD end-to-end Raydium trading bot (UI/model only; missing /configurations and executor).",
            "PresignedTrade create/store/execute pipeline (model only).",
            "BOPS / automated other-chain token fetching (not present).",
            "Sniper “rebuy after resell” (listed as new requirement; not in sell logic).",
            "Dashboard candlestick charts (explicitly out of scope in requirements notes).",
            "Some frontend routes still reference missing endpoints (/configurations, /api/trade_history, /get_logs).",
            "Legacy parallel sniper modules (autosnipe_logic, Moralis autosnipe_buy_script) not started by default — risk of confusion.",
            "Long sell elif ordering historically may prevent some higher profit tiers from firing as labeled — verify when changing Long sell logic.",
        ],
    )

    add_heading(doc, "9.4 Code vs Documentation Discrepancies (Summary)", 2)
    add_bullets(
        doc,
        [
            "RAD is documented historically as a working mode; code shows incomplete remnants.",
            "Presigned trades are required historically; only ORM exists.",
            "BOPS called out historically; no code references found.",
            "long_sell_trade docstring says Radium; behavior is Long/Jupiter.",
            "Nav “Presigned trade” does not manage PresignedTrade records.",
        ],
    )

    # ---------------------------------------------------------------------
    # Appendix
    # ---------------------------------------------------------------------
    add_heading(doc, "Appendix A — Suggested Reading Order for New Developers", 1)
    add_numbered(
        doc,
        [
            "Read this document sections 1, 4, and 9 for orientation.",
            "Skim models.py for data shapes.",
            "Trace Long path: LongTrade.js → trade.py → long_sell_trade.py.",
            "Trace Sniper path: autosnipe_manager.js → autosnipe.py → sniper_stream.py → autosnipe_buy_new_token.py → autosnipe_sell_logic.py.",
            "Trace pricing: shyft_pricing.py + price_stream.py.",
            "Run unit tests: tests/test_sniper_sell_enable.py, tests/test_autosnipe_half_txns_scan.py.",
        ],
    )

    add_heading(doc, "Appendix B — Document Sources", 1)
    add_bullets(
        doc,
        [
            "Prior file: Slivox_Requirements_Document (1).docx",
            "Codebase review of solivox1 (Flask app, models, sniper/long modules, frontend, tests)",
            "Where prior docs and code conflict, this document prefers verified code behavior and labels historical intent separately.",
        ],
    )

    return doc


def main():
    doc = build()
    out_dir = Path(r"c:\Users\Mudassar\Documents\Musaddaq Data\solivox1\docs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out1 = out_dir / "Solivox_Project_Documentation.docx"
    out2 = Path(r"C:\Users\Mudassar\Downloads\Solivox_Project_Documentation.docx")
    doc.save(out1)
    doc.save(out2)
    print(f"Wrote: {out1}")
    print(f"Wrote: {out2}")


if __name__ == "__main__":
    main()
