# HANDOFF — paper trial LIVE + Birdeye websocket pipeline (session 2026-07-23)

Previous handoff preserved verbatim as `HANDOFF-base-entry-2026-07-22.md` (base-entry
filter adoption; its Birdeye-Lite budget constraints are now VOID — see §1). The MEXC
spec remains in `HANDOFF-mexc-2026-07-21.md` (still zero code written). Campaign
verdicts + this session's decisions live in Claude's memory files under
`C:\Users\kensm\.claude\projects\C--Users-kensm-memecoin-strat-v1-AI-memes-strat\memory\`
(`memebot-paper-trial` is the newest; also `memebot-montecarlo`,
`memebot-base-entry-filter`, `memebot-bounce-campaign`, `memebot-round4-verdict`,
`memebot-target-rules`, `memebot-two-trees`).

NOTE: a second Claude session worked this repo in parallel today (blacklist tab:
`blcards.py`, manual-blocklist routes in `dashboard/server.py`, `backtest.py`
blocklist-path exports, TradingView-style position drawings in `tradecards.py`).
Its features are live and verified working; treat them as done work, not drift.

## 1. Goal

**End goal:** a Solana memecoin strategy with real forward edge; backtests only rank
candidates — **paper trading is the arbiter** (locked, do not relitigate).

**Immediate sub-goal at session end:** the paper trial IS RUNNING (started 2026-07-23
~11:40 UTC, 1.0 SOL account, **zero trades yet** — ~100+ rejections, which is the
filter working). Throughput levers were pulled at 15:25 UTC; the next session's job is
to WATCH: confirm candidate flow rose, catch the first sorties, and keep both
processes alive. Do not touch strategy parameters while the trial accumulates.

**Constraints/decisions locked this session (one-line reasoning each):**

- **Paper trial parameters = backtest variant E exactly** (exits stop 40 / bank 30%
  @1.35x / trail 30 / hard 20x / 480m; entry age ≥30m; 0.25 SOL × 3 slots; 1.0 SOL
  start via `paper_starting_balance_sol` in `config.asym.json`). Reasoning: the trial
  tests the validated config, nothing else.
- **Birdeye Premium purchased 2026-07-23** (20M CU/mo, 50 RPS but 1,000 req/min
  sustained, $9.9/1M overage, websocket 500 conns). The old "no Birdeye spend until
  Aug 20" rule is DEAD. `bdusage.PLAN` updated; billing anchor ASSUMED day 23 —
  **verify against the account Packages page** (open question §5).
- **`bot/` do-not-touch rule retired** — Kenny explicitly ordered websocket wiring
  into the bot. All additions are config-driven: `websocket.enabled:false` in
  `config.asym.json` restores the zero-Birdeye bot byte-identically.
- **Data architecture (Kenny-approved):** Birdeye websocket = discovery + 1m candles;
  Birdeye REST = qualification stats; GeckoTerminal = fallback qualification +
  backstop discovery only. Reasoning: GT's shared ~30 req/min was 429ing and losing
  candidates; Premium gives ~30x headroom.
- **The backtest's setup gate runs LIVE**: `backtest.entry_setup_ok()` (the exact
  validated function) + $8k cumulative-volume floor, on websocket/backfilled candles.
  No candle coverage from listing → no entry (backtest population always had full
  history; entering blind would be a different strategy).
- **Fills stay Jupiter-quoted** (real route impact, 500 bps tolerance, 0.00015 SOL/side
  paper fee). No priority-fee/Jito-bribe modeling — documented optimism; MC edge
  margin (36% extra cost tolerance) is the headroom argument.
- **Throughput levers pulled, gates untouched:** WS promotions 3→10/cycle
  (`max_promotions_per_cycle`), watchlist listing floor $2,000→$500. Off-limits
  levers documented in the 2026-07-23 conversation: no gate loosening, no setup-filter
  retuning (researcher freedom spent), no shorter max_hold, no lower $8k floor.
- **Monte Carlo verdict (montecarlo.py):** path/sequencing risk negligible under the
  backtest distribution (0 losing paths in 50k); ALL residual risk is distributional;
  Sharpe/trade 0.37 vs Sortino/trade 7.76 (asymmetry by design); EV +56.9%/trade.
  Risk analysis only — not evidence of live edge.
- **Dashboard runs with `--config config.asym.json`** so MAGI/UNITS/SORTIE panels track
  the paper bot directly. **server.py route changes require a server restart** (code
  loads once); regenerated HTML pages (journal, montecarlo) never do.

## 2. Current state of the code

**Running processes (detached via Start-Process — they survive session close, NOT
reboot/sleep):**
- Paper bot: restarted 2026-07-23 ~20:57 local (machine slept ~17:20–20:43 local —
  trial was dark 3.4h; count EFFECTIVE runtime, not wall clock). Log
  `memebot-asym.log`, DB `memebot-asym.sqlite` (0 positions, 0 fills). Startup lines:
  "ws state restored: N watchlist, M candle mints" (restart continuity, see §3) then
  "BIRDEYE WS ON ... (max 95 price subs)" then "ws connected".
- Dashboard: `http://localhost:8700`, serving `/` `/trades` `/montecarlo` `/blacklist`
  + `/api/state` `/api/bdusage` `/api/blacklist`. Find PIDs:
  `netstat -ano | Select-String ":8700.*LISTENING"` and
  `Get-CimInstance Win32_Process -Filter "Name like 'python%'" | ? { $_.CommandLine -match 'run\.py' }`.
- Restart continuity (added ~01:00 UTC 07-24, Kenny's request): the bot resumes from
  `reports/ws_state.json` (watchlist ages, promoted flags, candle store, counters;
  60s snapshots + on disconnect/stop; hard kill loses ≤60s). Log appends across
  restarts; sqlite was always persistent. Restart = just rerun the §2 commands.

**Verified working this session (how):**
- Paper trial: `/api/state` → `asym.paper` = balance 1.0 SOL, 0 open/closed;
  PAPER TRIAL panel ACTIVE with order feed, live REJECTION GATE table (55 setup /
  34 smart-money / 12 rugcheck at last count), rejection-airlock animation (DOM-dump
  verified), SYSTEM UPDATES changelog panel (7 rows from `reports/updates.json`).
- Websocket: smoke tests (scratchpad) proved handshake + SOL price stream (46
  ticks/20s); `reports/ws_status.json` shows connected/watchlist/subs/backfills;
  promotions observed in log ("ws discovery: promoted VLAD", "... BECOON, tato").
  Post-switch promotions log `[ws-birdeye]` source tags.
- Birdeye REST qualification: field names verified against LIVE responses
  (market-data: `price/liquidity/fdv/market_cap`; trade-data/single:
  `volume_5m_usd/volume_1h_usd/buy_5m/sell_5m/price_change_5m_percent/
  price_change_1h_percent`). End-to-end test: BONK → fully-populated Candidate →
  correctly rejected by gates (172x fdv/liq, weak buy pressure).
- Monte Carlo: `python montecarlo.py` regenerates byte-matching pool (220 trades,
  83.6% WR, 1.569x = summary variant E); outputs
  `reports/montecarlo.{json,html}` + `reports/mc_trades.json`; served at /montecarlo.
- Trade cards: volume lanes + "At entry · no hindsight" tile panels (gate readings
  from pre-entry candles only); regenerated via `python tradecards.py` (220 trades).
- Blacklist tab (other session): 693 wash-ramps counted, 24 extreme cards render,
  manual flag/restore API works; blank-screen bug was a stale server process (fixed
  by restart — the recurring lesson).
- Restart pickup (verified ~00:57 UTC 07-24): kill -> restart logged
  "ws state restored: 3 watchlist, 1 candle mints (snapshot 37s old)", reconnected,
  resumed scanning; paper balance/rejection history intact (log + sqlite persist).

**Broken / incomplete / caveats:**
- **0 trades so far** — expected (strict gates, narrow pre-lever funnel). If still 0
  after ~24h at the new settings, investigate funnel yield (see §5.1).
- **bdusage meter over-counts the new cycle** by ~240K CU (legacy calendar-month-keyed
  buckets attributed via `tracked_since`). Harmless direction (conservative);
  recalibrate `reports/bd_usage_baseline.json` against Birdeye's Metrics page.
- **Billing anchor day 23 unverified** (assumed = purchase day).
- **Promotion is single-shot**: if BOTH Birdeye REST and GT fallback fail for a ripe
  token, it's marked promoted and never retried. Rare now; retry queue is a possible
  hardening (§5.3).
- **smartmoney "no top-trader data (Birdeye unavailable); rejecting"** rejects are
  data-availability losses, not signal verdicts — reliability lever not yet pulled.
- Intermittent GT 429s on `trending_pools` (bot degrades gracefully to new_pools).
- WS drops occasionally; auto-reconnect with backoff handles it (observed working).
- Neither process survives a reboot (no Task Scheduler autostart yet).

**Git:** branch `main`, last commit `b3f8516` (initial) — EVERYTHING since is
uncommitted, including two sessions' work. Kenny has never asked to commit; suggest it
(§5.4).

**Commands (repo root `C:\Users\kensm\memecoin-strat-v1\AI-memes-strat`):**
- Bot: `python run.py --config config.asym.json` (detached:
  `Start-Process -WindowStyle Hidden python -ArgumentList "run.py","--config","config.asym.json"`)
- Dashboard: `python dashboard/server.py --config config.asym.json` (same Start-Process
  pattern) → http://localhost:8700
- Monte Carlo: `python montecarlo.py` (`--refresh-trades` to rebuild the pool)
- Trade cards: `python tradecards.py` · Blacklist preview: `python blcards.py`
- Health: `Invoke-WebRequest http://127.0.0.1:8700/api/state`; `Get-Content memebot-asym.log -Tail 20`;
  `Get-Content reports/ws_status.json`
- Env: Windows 11, PowerShell 5.1 + Git Bash, Python 3.11; **`websocket-client` 1.9.0
  installed this session** (`pip install websocket-client`); `BIRDEYE_API_KEY` in `.env`.
- Windows gotchas: kill by PID from netstat (image-name taskkill misses store-python);
  cp1252 console (no emoji prints); headless-Edge screenshots need
  `--virtual-time-budget` and are timing-lottery for animations — use `--dump-dom`
  for JS-render verification; PowerShell one-liners with `\U`/quotes break — write
  scratchpad .py files instead.

## 3. Files being actively edited

All **complete** (no mid-edit files). This session's changes:

- `bot/birdeye_ws.py` (NEW) — `BirdeyeFeed`: WS thread (listing watchlist ≥$500 liq,
  complex 1m price subs for opens + newest listings ≤95, candle store, auto-reconnect,
  status file `reports/ws_status.json`), `qualify()` (Birdeye REST stats; GT fallback),
  `setup_gate()` (entry_setup_ok + $8k cum-vol; REST backfill ≤4/cycle),
  `promote_candidates()` (limit = `ws_max_promotions_per_cycle`). CRITICAL API facts:
  complex SUBSCRIBE_PRICE query is a **boolean-expression STRING** (JSON arrays are
  silently ignored); listing events **re-broadcast** (watchlist uses setdefault to keep
  `listed_ts`/`promoted`). Late additions (~01:00 UTC 07-24): `_save_state()`/
  `_load_state()` persistence to `reports/ws_state.json` (gated by
  `websocket.persist_state`, default true); `setup_gate()` re-backfills when the
  stored candle tail is >180s stale (post-restore honesty — the gate must judge
  current candles like the backtest did; also covers the pre-existing case of a
  non-subscribed token re-gated cycles after its one backfill); `ws_status.json`
  writes now atomic via os.replace (a mid-write read returned blank once).
- `bot/main.py` — feed init in `Bot.__init__`, promotions merged into `try_enter`
  candidates, setup gate after `entry_signal` (before deep checks), `note_open_positions`
  each cycle, `finally: feed.stop()`.
- `bot/config.py` — `websocket` block parsing (`ws_*` attrs incl.
  `ws_max_promotions_per_cycle`, `ws_persist_state`); raw dict kept (unknown keys safe).
- `bot/scanner.py` — added `lookup_token()` (GT single-token pool lookup; now fallback).
- `config.asym.json` — `paper_starting_balance_sol: 1.0`; `websocket` block
  (enabled, promotions 10, floor $500, subs 95, backfills 4, cum-vol 8000,
  persist_state true). Strategy values untouched.
- `bdusage.py` — PLAN → Premium (20M/50rps/$9.9, anchor 23); CU_COST +=
  market-data 15, trade-data/single 30 (estimates).
- `montecarlo.py` (NEW) — MC engine + HTML report (5 scenarios × 10k paths, risk
  section, NERV nav); reads exits from config.asym.json; pool cache
  `reports/mc_trades.json`.
- `tradecards.py` — volume lane in `svg_chart`, `entry_tiles()` at-entry panel,
  MONTE CARLO nav tab (other session added position drawings + Blacklist button).
- `dashboard/server.py` — `/montecarlo` route; `ws`/`rejects` (classifier
  `classify_reject` matches exact verdict strings; `recent` feed)/`updates` state
  fields; asym paper block: balance/open_rows/orders (fills⋈positions SQL). Other
  session: `/blacklist` + `/api/blacklist` + `blcards` import.
- `dashboard/index.html` — MONTE CARLO + BLACKLIST tabs; BIRDEYE WS row (MELCHIOR);
  PAPER TRIAL panel: PAPER BALANCE, order feed, REJECTION GATE table; rejection
  airlock (CSS keyframes, seen-set + queue); SYSTEM UPDATES panel. 07-24: SORTIE LOG
  header has LIVE PAPER / BACKTEST view tabs (localStorage-sticky; auto-prefers live
  closes before a pick) — index.html is re-read per request, no server restart needed.
- `reports/updates.json` (NEW) — dashboard changelog; APPEND a row for every
  meaningful future change.
- Scratchpad (session-temp, gone next session): ws_smoke*.py, bd_fields.py, etc.

**Do NOT touch:** pre-registered `loop8_eval.py`/`loop9_eval.py` (verdict authority),
`sweep*.py`, `blocklist.py` thresholds + its `"?"-symbol guard, `.bd_cache_ext/`,
strategy values in `config.asym.json` (`entry`/`exits` blocks + setup-filter numbers
in `backtest.DEFAULT_SETUP`) — the trial must run unmodified. Treat `blcards.py` and
the manual-blocklist server routes as the other session's finished work.

## 4. Failed attempts — do not repeat

- **WS complex price subscription as JSON array**
  (`{"queryType":"complex","query":[{...},{...}]}`): accepted silently, delivers
  NOTHING — 0 PRICE_DATA in 20 min while 21 tokens were "subscribed" (only clue:
  msgs ≈ listings + WELCOME). The same tokens via `queryType:"simple"` streamed
  fine (46 ticks/15s). Verdict: array form dead for good; use the boolean string
  `"(address = X AND chartType = 1m AND currency = usd) OR (...)"` (docs/gist).
- **GeckoTerminal as primary qualification source**: 429 storms
  (`Max retries exceeded ... /api/v2/networks/solana/trending_pools ... too many 429
  error responses`) + single-shot promotions silently discarded ripe candidates.
  Verdict: dead as primary; kept only as fallback + backstop discovery.
- **Watchlist overwrite on listing events**: Birdeye re-broadcasts listings; plain
  assignment reset the `promoted` flag → BECOON promoted twice (15:07 and 15:08).
  Verdict: fixed via `setdefault` + max(liquidity); don't reintroduce.
- **Trusting grep for trade counts**: `Select-String "ENTER|EXIT"` matched the word
  "entered" in every scan line (173 false hits). Verdict: the sqlite ledger is the
  only ground truth for trades.
- **Headless-Edge screenshots of JS/animations**: plain `--screenshot` races the
  first `/api/state` fetch (empty panels); `--virtual-time-budget` helps but is a
  timing lottery for 2.8s animations. Verdict: verify dynamic DOM with `--dump-dom`
  and grep; screenshots only for static layout.
- **Writing Birdeye parsers from memory**: (standing repo lesson, upheld) — field
  names were verified live before coding `qualify()`; e.g. volume is `volume_5m_usd`,
  NOT `v5mUSD`. Keep doing this.
- **Blacklist tab "blank white screen"**: stale server process (routes load once);
  the disk code was fine. Verdict: any server.py change → kill PID → restart.
  Repeated 5+ times today; it will bite again.
- **Dashboard started WITHOUT `--config config.asym.json`** (Kenny's manual relaunch
  after the 07-23 evening sleep/wake): it silently reads `cfg.log_path` from
  config.json — `memebot.log`, which doesn't exist — so the rejection table went
  blank, scan panel null, status STANDBY, while the asym bot ran fine (asym/ws/paper
  panels kept working; they don't depend on cfg). Verdict: ALWAYS pass the flag;
  blank rejection table + ACTIVE bot = check the dashboard's command line first.

## 5. Next steps

1. **Monitor the trial at the new settings** (the single next action): check
   `Get-Content memebot-asym.log -Tail 30` + the dashboard. Expect: more
   candidates/scan than the old ~39, `promoted ...[ws-birdeye]` lines, rejection
   counts climbing faster, and eventually the first ENTER + fill in the PAPER ORDERS
   feed. If the bot/dashboard are down (reboot), restart both (commands §2). If 0
   trades after ~24h, diagnose WHERE candidates die (rejection-gate table proportions)
   before touching anything — and remember the off-limits list.
2. **Verify Birdeye billing anchor + recalibrate the meter**: Packages page → fix
   `bdusage.PLAN["billing_anchor_day"]` if not 23; optionally align
   `reports/bd_usage_baseline.json` with the Metrics page (~240K legacy over-count).
3. **Remaining safe throughput levers** (in order of value): make smart-money's
   Birdeye call reliable (Premium headroom; "no top-trader data" rejects are lost
   trades, not verdicts); `max_deep_checks_per_cycle` 5→10; GT backstop pages 1→2-3;
   promotion retry queue for double-failures; Task Scheduler autostart for 24/7
   uptime (biggest trades/day lever of all).
4. **Suggest committing** — two sessions of work sit uncommitted on `main`
   (logical split: strategy/MC · websocket pipeline · dashboard · blacklist).
5. **Open questions only Kenny can answer**: billing anchor day (read off Packages
   page); commit now?; if slots start filling, raise `max_positions` 3→4 (risk
   decision: 4×0.25 fully deploys the 1 SOL bankroll against the 0.75 daily
   breaker); revive MEXC spec someday?

