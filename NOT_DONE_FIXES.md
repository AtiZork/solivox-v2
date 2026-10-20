# Solivox v2 — Open Items (from teammate's action plan, verified against code 2026-10-20)

Source: teammate's `fix_action_plan.md`. Each item below was independently re-verified against
the current codebase before listing (not just copied from the plan). Status and any correction
to the original plan is noted under "Verified".

Working order: top to bottom within each section. Items flagged **NEEDS YOUR CALL** touch a
decision you already made this session in the opposite direction — not starting those until
you confirm, to avoid re-litigating settled choices.

======================================================================
## SECTION 1 — P0 (live risk, small effort)
======================================================================

### 1.1 Delete the dead legacy buy bot files — ✅ DONE
Deleted `autosnipe_logic.py` and `autosnipe_buy_script.py`. Removed the dead
`_start_all_bots_disabled`/`_stop_all_bots_disabled` functions and their helpers from
`autosnipe.py`, the now-unused `threading`/`asyncio`/`current_app` imports and
`listener_thread`/`auto_buy_thread` globals, and the dangling import/comments in `main.py`.
Added `tests/test_dead_code_removed.py` (AST-based, not grep, so it won't false-positive on a
comment mentioning the old name) to fail the suite if either module is ever imported again.
Full app import verified (both `.venv` and the real `so-venv`), 335/335 tests passing.

### 1.3 DB-level unique constraint for buy dedup (upgrade) — ✅ DONE
Checked the live table for pre-existing duplicates before doing anything, as the original risk
note said to. Found exactly one: user 4 had bought token `CaaK...pump` twice on 2026-10-14
(before the B10 dedup check existed) - one of the two (trade 153) was already sold, the other
(trade 142) was still open and unsold. Asked you how to handle it rather than deciding myself;
you chose to scope the new constraint to **open positions only** (not forever) - trade 153
no longer conflicts once closed-out rows are excluded, so no data had to be touched. This is a
deliberate behavior change from B10's original "forever" dedup, approved by you specifically for
this fix: a user can now rebuy a mint after fully closing out of it.

`models.py` (`Trade.__table_args__`): declared a partial unique index,
`ix_trade_open_autosnipe_user_mint`, on `(user_id, token_address)` where `auto_snipe = true AND
executed = false`. A fresh DB gets it from `db.create_all()`; `main.py`'s
`_ensure_open_autosnipe_dedup_index()` adds it to the already-existing live table the same way
`_ensure_sniper_enable_columns()` already adds missing columns - re-checks for duplicates each
time and skips (loudly, not crashing) rather than ever assuming it's safe. Verified live: 0
duplicates under the new open-only scope, and the index now exists on the production table with
exactly the intended `WHERE` clause (confirmed via `pg_indexes`).

`autosnipe_buy_new_token.py`:
- B10's query check now also filters `executed=False`, matching the index's scope exactly.
- The pre-send Trade commit (1.4a) now catches `IntegrityError` and treats it as
  `duplicate_skipped` - defense in depth for a race the query check + `WALLET_BUY_LOCK` should
  already prevent, in case either ever stops covering every path that reaches here.
- New `mark_autosnipe_position_closed(user_id, token_address)`: clears the in-memory
  `BOUGHT_TOKENS` fast-path entry. Needed because that set is otherwise permanent for the
  process's lifetime - without clearing it, a closed position's mint would still be blocked by
  the in-memory cache until a restart, even though the DB would now correctly allow a rebuy.

`autosnipe_sell_script.py`: calls `mark_autosnipe_position_closed` right after the commit that
sets `executed=True` actually succeeds (both in `_sell_if_due`'s normal full-exit path and in
`reconcile_stuck_sell_pending`), only for `auto_snipe=True` trades.

Added `tests/test_buy_dedup_index.py` (index declaration + DDL scope), and new tests in
`test_buy_execution.py` (open trade blocks a rebuy, closed trade allows one, `IntegrityError` ->
duplicate) and `test_sell_early_and_cooldown.py` (full exit frees the mint). 354/354 passing
(up from 346), clean import under the real `so-venv`.

Not done as part of this fix: `trade_kind='AUTOSNIPE'` from the original plan text turned out to
be redundant with `auto_snipe=True` (the two are always set together) and the plan's own "+
open/active status" phrasing didn't match what B10 actually checked (no `executed` filter at
all, i.e. "forever") - scoped to match your approved open-positions-only decision instead of the
plan's original (and, on inspection, already-inaccurate) wording.

### 1.4a — B5: orphaned position if DB write fails after send — ✅ DONE
`autosnipe_buy_new_token.py`: the `Trade(...)` row is now created and committed right after
the quote response comes back — before the swap transaction is even fetched, let alone signed
or sent — instead of after `send_transaction` succeeds. Every field it needs
(`estimated_tokens`, `token_name`/`token_symbol`, config-derived fields) was already available
at that point; nothing was gated on the send.

Two outcomes, handled differently on purpose:
- **Quote/swap step fails** (nothing was ever sent — no ambiguity): the pending Trade row is
  deleted via a new `_delete_unsent_pending_trade()` helper, so a plain API failure doesn't
  leave a phantom DB row behind.
- **`send_transaction` itself raises** (`RPCException`): the row is deliberately *kept*. The
  existing comment on `BOUGHT_TOKENS.add(buy_key)` already establishes that a raised send error
  doesn't prove the transaction never landed — so deleting the row here would risk erasing the
  only record of a buy that actually went through. A Trade row whose tokens never arrive is
  already handled gracefully elsewhere (the sell cycle logs a zero-balance warning and skips
  it), which is a far safer failure mode than real money spent with zero DB record.

Only the `TradeHistory` row (which needs the real tx signature) still waits until after a
successful send — that part is unchanged.

Not done as part of this fix (scoped out, per the plan's own "add a periodic sweep" note being
a separate, larger follow-up): a background job comparing wallet token-account balances against
open Trade rows to catch any phantom/missed positions on an ongoing basis.

Updated `tests/test_buy_execution.py`: the old
`test_preflight_rejection_is_reported_not_crashed` asserted "no Trade row for a rejected buy",
which was the *old* behavior this fix intentionally reverses — updated it to assert the Trade
row is written and kept, and that no TradeHistory row is created. Added two new tests:
`test_trade_row_is_written_before_the_swap_is_even_built` and
`test_swap_setup_failure_deletes_the_just_written_pending_trade`. Verified both the `.venv` test
suite (337/337, up from 335) and a clean import under the real `so-venv`.

### 1.4b — C9: remaining double-sell risk on DB failure after sell send — ✅ DONE
The earlier atomic-commit fix closes "orphaned TradeHistory row while trade bookkeeping stays
stale" (one commit succeeds, a second fails) by merging both writes into one commit. It doesn't
close the deeper case: that *single* commit can still fail as a whole, after a real send
succeeded - then nothing at all is recorded (no TradeHistory row, stale `purchased_token_amount`),
so a retry has no way to know a send already happened and could fire the same tier again for
tokens already sold.

`models.py` (`Trade`) / `main.py`: added two columns, `sell_pending` (bool) and
`sell_pending_since` (timestamp), with the matching `ALTER TABLE` entries for existing Postgres
rows.

`autosnipe_sell_script.py` (`_sell_if_due`):
- Right before `send_transaction`, sets `sell_pending=True` + `sell_pending_since=now` and
  commits that **on its own**, before anything is sent. If even this can't be committed, the
  attempt is abandoned (`reason=sell_pending_marker_failed`, outcome `failed`) rather than
  sending money the DB is in no shape to track.
- Send itself raising: clears the marker immediately (nothing was sent, safe to retry normally).
- Send succeeds and the existing bookkeeping commit also succeeds: clears the marker as part of
  that same atomic commit - no change to the happy path's commit count.
- Send succeeds but the bookkeeping commit fails: the marker (already durably committed before
  the send) stays `True`. `_sell_if_due` now checks this first and returns `"cooling"` for any
  trade with `sell_pending=True`, so it's never re-evaluated for a fresh decision while stuck.
- New `reconcile_stuck_sell_pending(app)`, scheduled on the same interval as
  `reconcile_pending_sells`: for trades stuck with `sell_pending=True` for longer than that
  interval, re-reads the wallet's **real on-chain token balance** (there's no sold-amount figure
  left to replay - the commit that would have recorded it is exactly what failed) and syncs
  `purchased_token_amount` to match truth, marking `executed` if it's now ~0, then clears the
  marker. Runs under the same per-trade lock as a live sell attempt so the two can't race.

Updated `tests/test_sell_early_and_cooldown.py` and `tests/test_sniper_take_profit.py`: both had
an existing test that set `db.session.commit.side_effect` to fail on *every* call from the start
of the cycle, which the old code tolerated right up until after the send. With this fix, the
same "DB can't commit anything" setup now fails at the earlier, safer point (before the send is
ever attempted) - updated their expected outcome/reason accordingly, and added
`test_persistent_db_failure_prevents_any_send` plus a rewritten
`test_db_failure_after_send_leaves_sell_pending_and_blocks_a_resell` (first commit succeeds, the
second fails) to cover both the new and original scenarios explicitly. Also updated
`test_sell_reconciliation.py`'s scheduler-wiring test for the new job, and added a new
`reconcile_stuck_sell_pending` test section (8 tests) there. As elsewhere in this session, the
mocked harness can't model real SQLAlchemy rollback reverting a `SimpleNamespace`'s attributes,
so tests that exercise a failed commit assert the failure is caught/logged/rolled-back rather
than asserting on post-rollback field values.

Not done as part of this fix: `reconcile_stuck_sell_pending` trusts the real wallet balance
outright rather than cross-checking it against an on-chain transaction history the way
`reconcile_pending_sells` does with a signature - acceptable here since there's no signature to
check in the first place (that's the whole problem), but worth knowing if a different source of
balance drift is ever suspected.

Verified both the `.venv` test suite (346/346, up from 337) and a clean import under the real
`so-venv`.

### 1.5 Remove hardcoded DB credential fallback — ✅ DONE
`config.py` now raises at import time if `DATABASE_URL` is unset, same pattern as
`JWT_SECRET_KEY`. Also fixed a real bug found while doing this: `config.py` relied on another
module (`settings.py`) happening to call `load_dotenv()` first via `main.py`'s current import
order — fragile. `config.py` now calls `load_dotenv()` itself, so it works standalone
regardless of import order. Verified both via standalone import and the full app. 335/335
tests passing.

### 1.6 — C10: verify the mint's own WS subscription is alive before treating silence as inactivity — ✅ DONE
`autosnipe_sell_script.py` (`check_inactivity`): before letting elapsed silence count toward a
mint's inactivity threshold, now checks `mint in _sell_stream.subscribed` - the same dict
`sell_stream.py`'s `SellStream` already keeps (`mint -> subscription id`), just never consulted
here before. If the mint isn't in it (a just-bought mint `SellStream` hasn't synced in yet -
`sync_interval` is 5s, not instant - or a subscribe request that silently failed, per
`sell_stream.py`'s own "subscribe failed for %s" log), its activity timer is reset instead of
accumulating elapsed time toward a force-sell. A warning is logged only if the mint *was*
previously tracked (a subscription that existed and then vanished) - a brand new mint hitting
this on its first tick is normal and silent, same as the existing "first time seen" seeding path
right below it.

**Known limitation, called out rather than hidden:** this only catches drops the *client* can
see (never subscribed, or a subscribe that errored). A server-side silent drop - the RPC node
stops delivering for a subscription id without ever telling the client - would still show the
mint as present in `.subscribed`, since nothing told the client otherwise. Catching that would
need an independent check (e.g. querying recent signatures for the mint) on every sweep tick,
which conflicts with this sweep's own "cheap, frequent, no RPC calls" design - not attempted
here. This closes the concrete, observable gap (the one the live bug report in the plan actually
described), not every theoretical one.

Updated `tests/test_sell_inactivity.py`'s `_connected_stream()` helper to populate `.subscribed`
(previously unset - a bare `MagicMock()`, which this new check would have read as "never
subscribed" for every existing test); it now defaults to an always-contains stand-in so none of
the existing timing tests needed touching, and added 5 new tests for the subscription-liveness
behavior itself (not-subscribed blocks a fire, timer reset, warn-only-if-previously-tracked,
resubscribe fires normally). 358/358 passing (up from 354), clean import under the real
`so-venv`.

### 1.7 Route-auth enumeration test — ✅ DONE
`tests/test_route_auth.py`: the `app` fixture now also registers `monitor_bp`, `charts_bp`,
`user_register_bp`, `autosnipe_trade_bp` (`autosnipe_buy_new_token.py`), and `live_pricing_bp` -
all the main.py blueprints that import cleanly in this constrained test environment.
`wallet_managment.py`'s `wallet_bp` is still the one left out (imports `bip_utils`, which
doesn't build here, per the file's existing docstring) - its routes have no coverage from this
test, same gap the file already had before this fix for every other test in it.

New `test_every_non_public_route_requires_auth`: walks every `(endpoint, method)` pair Flask
actually registers, skips an explicit `PUBLIC_ROUTES` allowlist (with a reason next to each
entry), and asserts every other one 401s with no `Authorization` header. A sanity floor
(`checked > 15`) fails loud if the blueprint list above ever silently shrinks.

**Two real findings from actually building the allowlist** (not fixed here - flagging, not
silently changing behavior per how this whole pass has worked):
- `live_pricing_bp`'s `/refresh_pools` and `/register_pool` have no auth at all and mutate
  *shared* state (trigger a background pool refresh; write arbitrary attacker-supplied entries
  into the on-disk pool registry other pricing logic reads and trusts). Not a per-user data
  leak like Phase 1's fixes - there's no "owner" concept for a shared pool registry - but a
  poisoned registry entry could feed wrong price data into every user's trading decisions.
  Worth a look under Section 2 hardening; `@jwt_required()` alone wouldn't really fix it (any
  logged-in user could still poison it), so it needs its own decision, not a copy of the B10-style
  fix.
- `/is_wallet_attached` (`wallet_managment.py`, not covered by this test - see above) has no
  `@jwt_required()` either, but checked separately: it reads Flask's cookie-based `session`
  object for a `public_key` key that nothing in the codebase ever sets, so it always returns
  `{"wallet_attached": False}` regardless of caller - dead code, not a live leak. Left alone.

Everything else that turned up with no `@jwt_required()` (`charts_bp`'s DexScreener/CoinGecko
proxies, the two sniper/price stream-status health checks, `/live_token_price`,
`/price_from_vaults`, `/pump_price`, `/raydium_price`, `monitor.py`'s retired route, `/register`
and `/login` themselves) has no per-user ownership concept at all - global market data or a
fixed-response retired/health-check endpoint - so these are allowlisted as genuinely public,
matching Phase 1's own scope (JWT + ownership for routes that touch a specific user's rows).

359/359 passing (up from 358), clean import under the real `so-venv` for every module this test
now registers.
- **Risk:** low — test-only change (plus the two flagged findings above, neither touched).

### 1.8 Stand up CI
- **Verified:** no `.github/workflows` or equivalent exists.
- **Fix:** a basic workflow running `pytest tests/` on push/PR.
- **Risk:** low, but needs you to confirm which CI platform (GitHub Actions assumed) and
  whether secrets/env vars for it should exist yet.

### 1.2 — Parallelize per-user buy-config evaluation (B9)
- **NEEDS YOUR CALL.** You explicitly told me to skip B9 this session. Listed here only so
  it's not lost, not scheduled.

======================================================================
## SECTION 2 — P1 (platform hardening) — none of this done
======================================================================

- **2.2 Scope CORS to real origins** — **NEEDS YOUR CALL.** Conflicts with your explicit
  instruction earlier ("no need, local-only"). Teammate didn't have that context.
- **2.3 Rate limiting** — **NEEDS YOUR CALL.** Same conflict — I skipped this earlier for the
  same local-only reasoning.
- 2.1 Fix Dockerfile (wrong CMD/port/base image, no non-root user) — no prior decision, open.
- 2.4 Stop synchronous DB logging (`DBLogHandler` commits every log line) — open.
- 2.5 Request schema validation library (Marshmallow/Pydantic) — open, larger change.
- 2.6 Key custody / envelope encryption or KMS — open, larger change, only chmod done so far.
- 2.7 Backup/restore runbook — open, documentation only.
- 2.8 Health-check endpoint + list pagination — open.

======================================================================
## SECTION 3 — New dashboards/features — ✅ DONE (3.1-3.5)
======================================================================

Built per a plan reviewed and approved up front (see the session's plan-mode exchange), skipping
Section 2's remainder and 1.2/1.8 per your explicit instruction. Full detail of the
investigation/decisions is in that plan; summary below.

**Investigated before writing any code, not assumed:**
- Traced the actual buy/sell code: `Trade.amount`/`TradeHistory.amount` (BUY) is the *configured*
  SOL amount (real, since it's what's actually swapped in); `TradeHistory.amount` (SELL) is a
  **token** amount, not SOL; `execution_price`/`initial_price` are **USD-per-token**. The sell
  quote's real on-chain SOL proceeds and the real confirmed-tx fee are **not captured anywhere**
  in this codebase today - only configured settings exist, which the plan's own B2/C4-style rule
  says not to present as "actual."
- **Decision (confirmed with you):** ship now using only data that already exists. `sol_spent`
  (SOL, exact) and all P&L (USD, exact) are real numbers; `sol_received`/`fees_paid_sol` are
  always `null` with a `"not_tracked": true` flag - never a plausible-but-wrong number. **No
  changes to the buy/sell transaction code** (1.4a/1.4b just hardened it). The gap is a new,
  separately-scoped future item below (not done, not started).
- Traced the frontend's actual auth convention (JWT in `localStorage['token']`, manual
  `Authorization` header per request, no shared fetch helper except `autosnipe_manager.js`'s
  `apiFetch`, no Jinja inheritance) and followed it exactly for the two new pages, rather than
  inventing a new pattern.

**1. `latency_log.py`:** `StageTimer.finish()` (every buy/sell attempt) now also feeds two new
throttled aggregators, `buy_stats`/`sell_stats` (reusing the existing `StageStats` class as-is -
previously only used for the sniper WS stream's own RPC timing), in addition to its log line.
Added `StageStats.flush_and_return()` so a caller can retrieve+reset a snapshot instead of only
logging it; `maybe_flush` is now just that function's logging caller.

**2. `dashboard_models.py`** (new file): `LatencySnapshot(path, stage, window_start, window_end,
count, avg_ms, max_ms)`. `flush_latency_snapshots(app)` persists whatever `buy_stats`/`sell_stats`
have accumulated; `start_latency_snapshot_scheduler(app)` runs it on
`LATENCY_SUMMARY_INTERVAL_SEC`, wired into `main.py`'s existing `if __name__ == "__main__":`
scheduler block (same pattern as `create_scheduler`/`auto_snipe_auto_sell_schedular`). Table
confirmed created on the live DB via the existing `db.create_all()` startup step.

**3. `trades_api.py`** (new file): `GET /api/trades` - `user_id = get_jwt_identity()` filtered
first (mirrors `trade_history.py`'s existing ownership pattern). Filters: `from/to/status/
trade_kind/wallet_id/outcome/token/sort/page/page_size` (capped at 100). status/outcome are
derived (not stored columns) but filtered **in the SQL query itself** (a subquery + a correlated
`EXISTS`, plus a window-function subquery for exit price) - caught and fixed a real bug in an
early draft where these were filtered in Python *after* `offset/limit`, which would have silently
dropped rows from a page and under-reported `total` whenever either filter was active. Realized
P&L prices each partial (tiered take-profit) sell against the tokens it actually sold, not the
whole position at one price.

**4. `dashboard_api.py`** (new file): `GET /api/dashboard/financial` (same filters, `status`
defaults to confirmed-only per the plan) returns `totals` (`sol_spent`, `earned_usd` - gross
proceeds, distinct from net P&L -, `realized_pnl_usd`, `unrealized_pnl_usd` using the most
recently known price from `TokenPrice` - already written by both pricing schedulers - no live
price call from this endpoint, `win_rate_pct`, `trade_count`, plus the always-null
`sol_received`/`fees_paid_sol`/`not_tracked`), `by_day` and `by_wallet` breakdowns (caught and
fixed a consistency bug where these ignored the `status` filter `totals` respected). `GET
/api/dashboard/latency` reads `LatencySnapshot` - no ownership filter (nothing to own, the sniper
isn't per-user), but still requires a valid JWT.

Both new query modules build their subqueries with `sqlalchemy.select()`, not
`db.session.query()` - the latter resolves `db.session` (app-context-scoped) immediately, which
crashed every import with "Working outside of application context" since it ran at module load,
before any request exists. Caught via a standalone smoke test against SQLite before writing the
real test suite.

**5. `templates/transactions.html` + `static/js/js/transactions.js`:** full rebuild - every
Kripton demo row/chat-sidebar/dead-filter-button removed. Rebuilt using `dashboard.html`'s own
proven self-contained dark-theme style (CSS variables, stat-card grid) rather than the 1358-line
third-party template shell, since that's what the live app actually looks like day to day.
Date-range presets + custom, filters (status/trade_kind/wallet/outcome/token), four stat tiles
(Spent/Earned/Realized P&L/Unrealized P&L - matches the plan's exact wording, not the net-P&L
figure twice), a spend-vs-earned chart (Chart.js), real server-side pagination.

**6. `templates/performance.html` + `static/js/js/performance.js` + `/performance` route:**
date-range control, buy/sell/stage selector, a Chart.js avg+max time series from
`/api/dashboard/latency`.

**Verified:** `python -m py_compile` on every new/edited file; full test suite **371/371
passing** (up from 358 before this section - `tests/test_dashboard_api.py` new, plus the two new
blueprints added to `tests/test_route_auth.py`'s 1.7 enumeration fixture), run twice to rule out
test-order flakiness (found and fixed one real instance: `buy_stats`/`sell_stats` are process-wide
singletons shared with every other test that drives a real buy/sell `StageTimer`, so a latency
test needs to drain them first, not assume a clean slate); clean import under the real `so-venv`
for every new/edited module including the full `main.py`; both new pages render 200 via a real
Flask test client hitting the actual routes with the actual templates. **Not done:** a browser
click-through of `/transactions`/`/performance` - starting `main.py` myself would spin up a
second live instance of the sniper/sell schedulers against the same production DB and real
wallets alongside whatever's already running, so you're doing that check yourself on your own
timing instead.

**New follow-up item (not started, not part of this section's scope):** add real SOL-received
(the sell quote's `outAmount`) and real on-chain fee (`get_transaction().meta.fee`) capture to
the buy/sell transaction code, so `sol_received`/`fees_paid_sol`/`realized_pnl_sol` can become
exact for trades going forward (historical rows would stay approximate/null regardless). Touches
the same hot paths 1.4a/1.4b just hardened - deserves its own dedicated review pass, not a
bundle-in with a reporting feature.

======================================================================
## PROPOSED WORKING ORDER (today)
======================================================================

1. 1.5 (DB credential fail-loud) — ✅ DONE
2. 1.1 (delete dead legacy files) — ✅ DONE
3. 1.4a (B5 — write Trade before send) — ✅ DONE
4. 1.4b (C9 — sell_pending marker) — ✅ DONE
5. 1.3 (DB unique constraint upgrade) — ✅ DONE
6. 1.6 (C10 — per-mint subscription health check) — ✅ DONE
7. 1.7 (route-auth enumeration test) — ✅ DONE
8. 1.8 (CI) — separate, infra-only, last item in Section 1 — still open, needs your platform call
9. Section 3 (3.1-3.5, new dashboards) — ✅ DONE

Holding 1.2, 2.2, 2.3 until you confirm. Section 2's remainder is explicitly on hold per your
instruction, not forgotten.
