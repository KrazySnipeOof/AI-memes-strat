# HANDOFF — MEXC leveraged long/short mode (session 2026-07-21)

This file replaces the 2026-07-19 handoff (sweep/backtest campaign). That campaign's verdicts
are preserved in Claude's memory files (`memebot-round4-verdict`, `memebot-bounce-campaign`,
`memebot-target-rules` under `C:\Users\kensm\.claude\projects\C--Users-kensm-memecoin-strat-v1-AI-memes-strat\memory\`).
Nothing from the old handoff is still in-flight in THIS repo.

## 1. Goal

Kenny's request (verbatim): **"now add a mode that does this but for the exchange MEXC where
I can short and long memecoins with leverage."** I.e., replicate the existing Solana memecoin
bot's behavior (discover → filter → momentum entry → stop/TP-ladder/trailing/time exits →
paper-first trading with SQLite ledger) but on **MEXC USDT-margined perpetual futures**, with
**both long and short entries and configurable leverage**.

**Immediate sub-goal at session end:** all design decisions were made and the MEXC API was
verified reachable/current (see §4), but **zero code files have been created yet**. The next
session starts by creating the `mexcbot/` package per the spec in §5.

**Constraints and decisions locked this session (do not relitigate):**

- **Parallel package `mexcbot/`, NOT modifications to `bot/`** — `bot/` is the working Solana
  live-bot; the prior handoff marked it do-not-touch and nothing about futures trading needs
  to live inside it. New CLI entry `run_mexc.py`, new config `config.mexc.json`.
- **Paper mode first, and paper is the arbiter** — locked project rule from the earlier
  campaign (all backtest configs were negative-expectancy under honest fills; only forward
  paper results count). The MEXC mode ships as a paper trader against live MEXC market data.
  Live order placement is Phase 2, only after paper validates.
- **Separate DB/log** (`memebot-mexc.sqlite`, `memebot-mexc.log`) — same convention as
  `config.asym.json` (`memebot-asym.sqlite`) so results never mix between configs.
- **Exit rules operate on ROE-on-margin multiple** (equity multiple of margin, direction- and
  leverage-folded), NOT raw price multiple. Reason: the existing exit state machine
  (`bot/strategy.py:exit_action` — stop / TP ladder / trailing / time, keyed on
  `(banked + current_value) / spent`) then ports almost verbatim, and one set of exit params
  means the same thing for longs and shorts at any leverage.
- **Paper broker MUST model: taker fees, slippage, funding payments, and liquidation.**
  Leverage without liquidation/funding modeling would silently overstate results — that is
  exactly the "cheating" Kenny forbade in the earlier campaign. Honest-fills discipline carries
  over.
- **Isolated margin only** (per-position), default leverage low (3x proposed — open question
  for Kenny, §5).
- **No browser-token/ToS-violating API workarounds.** Obsolete anyway — see §4.

## 2. Current state of the code

**Nothing built yet for MEXC.** This session was: read the whole existing bot, design the MEXC
mode, verify MEXC API status via web search. The only file written this session is this
HANDOFF.md.

**What exists and works (verified by reading, unchanged this session):**

- Solana bot package `bot\` (scanner/safety/strategy/jupiter/execution/portfolio/main) with
  `run.py` CLI. Paper mode default. Architecture summary in `README.md` (accurate).
- `config.json` (main paper config), `config.asym.json` (asym-runner variant, separate DB).
- NERV dashboard: `dashboard\server.py` (380 lines) + `dashboard\index.html` (578 lines) —
  reads the Solana SQLite ledger + Jupiter quotes. **Not MEXC-aware; integration deliberately
  deferred** (follow-up, not part of the first MEXC deliverable).
- Key architecture facts for mirroring (all verified by reading this session):
  - `bot\main.py` — `Bot.cycle()` = `manage_positions()` then `try_enter()`; per-position
    management computes `multiple = (sol_received + sellable_value) / sol_spent`, tracks
    `peak_multiple`/`tp_stage` in DB, asks `strategy.exit_action(...)` for
    stop/TP/trailing/time decision, executes via broker, records partial vs full exits.
  - `bot\strategy.py:exit_action(multiple, peak, stage, age_min, cfg)` — the state machine to
    replicate: hard TP → TP ladder (stage-indexed, fraction-of-remaining) → stop (stage 0
    only) → trailing (after first TP, off peak) → time stop.
  - `bot\portfolio.py` — SQLite schema: `positions` / `fills` / `candidates` tables, `mode`
    column separates paper/live rows, `record_candidates()` accumulates an unbiased cohort for
    future backtests. Mirror this shape.
  - `bot\execution.py` — PaperBroker fills on live quotes + flat fee; LiveBroker gated by
    `MEMEBOT_I_UNDERSTAND_THE_RISKS=yes` + wallet pinning. Mirror the gating pattern.
  - `bot\util.py` — `make_session()` (requests + retry), `iso_now/parse_iso/utc_now/fnum`,
    `load_dotenv`. REUSE these directly (`from bot.util import ...` works since `run_mexc.py`
    will insert repo root into `sys.path` exactly like `run.py` does).

**Git state:** branch `main`, last commit `b3f8516` ("Initial commit: Solana memecoin
paper/live bot with NERV dashboard."). Pre-existing uncommitted changes NOT from this session
(leave them alone): modified `.gitignore`, `backtest.py`, `config.asym.json`,
`dashboard/index.html`, `dashboard/server.py`, `sweep.py`; untracked `bd*.py`, `sweep2-4*.py`,
`loop8_eval.py`, `loop9_eval.py`, `tradecards.py`, `blocklist.py`, `canvases/`, `web/`,
`.bd_cache/`, `.bd_cache_ext/`. This session added only `HANDOFF.md` (this rewrite).

**Two working trees (from memory, still true):** this git repo
(`C:\Users\kensm\memecoin-strat-v1\AI-memes-strat`) is canonical and runs the live dashboard;
`C:\Users\kensm\solana-memebot` is a separate copy holding the big `.ohlcv_cache`. All MEXC
work happens in the git repo.

**Environment:** Windows 11, PowerShell 5.1, Python on PATH, `requests` installed and working.
No new installs needed for the MEXC paper mode (public market data needs no key, no auth lib —
stdlib `hmac`/`hashlib` suffice for the future signed client). GeckoTerminal/Jupiter usage
unchanged.

**Commands (existing, all verified in prior sessions):**

- Solana paper bot: `python run.py --config config.asym.json` (`--once`, `--report`)
- Dashboard: `python dashboard/server.py` → http://localhost:8700
- Quick MEXC API smoke test (do this first next session):
  `python -c "import requests; r=requests.get('https://contract.mexc.com/api/v1/contract/ticker', timeout=15); d=r.json()['data']; print(len(d), d[0]['symbol'])"`
  Expect several hundred contracts.

## 3. Files being actively edited

- `C:\Users\kensm\memecoin-strat-v1\AI-memes-strat\HANDOFF.md` — **complete** (this file;
  overwrote the stale 2026-07-19 handoff per the /handoff skill).

No other files were created or modified this session. The files to CREATE next session are
specced in §5.

**Do NOT touch:** everything under `bot\` (working Solana bot), `config.json`,
`config.asym.json`, `memebot.sqlite` / `memebot-asym.sqlite`, `dashboard\` (in active use;
MEXC dashboard support is a later, separate step), and the pre-existing uncommitted research
scripts listed in §2.

## 4. Failed attempts — do not repeat

Nothing was coded, so no code-level dead ends. Two knowledge findings that must not be
re-derived wrongly:

- **OBSOLETE ASSUMPTION: "MEXC futures order API is locked/under maintenance."** That was true
  for years (order endpoints returned maintenance errors for ordinary keys since ~2022), but a
  web search this session (2026-07-21) confirmed **MEXC launched API futures trading on
  March 31, 2026** for all futures pairs — order placement via official API now works for
  regular users, **requires completed KYC** and futures-order permission enabled on the API
  key. Sources: MEXC announcement "Introducing API Futures Trading on Mar 31, 2026"
  (https://www.mexc.com/announcements/article/introducing-api-futures-trading-on-mar-31-2026-17827791534551),
  futures API docs (https://www.mexc.com/api-docs/futures/integration-guide). Consequence: do
  NOT build the live path around browser-token bypass repos (e.g. github.com/vecful/mexc-futures-api)
  — ToS-violating and now unnecessary. Build the official signed client (HMAC-SHA256,
  `ApiKey`/`Request-Time`/`Signature` headers) but keep it gated behind paper validation.
- **Verify against current docs, not memory:** the futures docs moved to
  https://www.mexc.com/api-docs/futures/ (old mirror: mexcdevelop.github.io/apidocs/contract_v1_en/).
  When implementing, confirm exact endpoint paths/field names from the live docs; the paths in
  §5 are from the old contract v1 API and were reachable as of this session but field names
  (`riseFallRate`, `fundingRate`, etc.) should be checked against a real response before coding
  parsers.

## 5. Next steps

Start with step 0 (2 minutes), then build in order. Design details below are the locked spec.

0. **Smoke-test the MEXC public API** (command in §2). Also fetch one contract detail
   (`GET https://contract.mexc.com/api/v1/contract/detail`) and one kline
   (`GET https://contract.mexc.com/api/v1/contract/kline/BTC_USDT?interval=Min5`) and eyeball
   the real field names before writing parsers.
1. **`mexcbot/api.py`** — thin client over `https://contract.mexc.com`:
   public: `/api/v1/contract/detail` (universe + contractSize + leverage limits),
   `/api/v1/contract/ticker` (lastPrice, 24h riseFallRate, volume24, fundingRate),
   `/api/v1/contract/kline/{symbol}?interval=Min5` (compute 5m/1h momentum),
   `/api/v1/contract/depth/{symbol}` (spread for paper fills),
   `/api/v1/contract/funding_rate/{symbol}`. Private (Phase 2, stub now): HMAC-SHA256 signed
   `POST /api/v1/private/order/submit`, `GET /api/v1/private/account/assets`.
2. **`mexcbot/config.py`** — `MexcConfig` mirroring `bot/config.py` style. New knobs:
   `margin_usdt` (per-trade margin), `leverage` (default 3), `taker_fee_rate` (0.0002),
   `slippage_bps` (5), `maintenance_margin_rate` (0.005, configurable),
   `universe` block (`exclude_symbols` majors denylist: BTC/ETH/SOL/BNB/XRP/ADA/LTC/etc.,
   `include_symbols` whitelist, `min_volume24_usdt`, `max_universe`),
   `entry` block with **`long` and `short` sub-blocks, independently enable-able** (short =
   mirrored momentum: negative-but-not-capitulated 5m/1h change), direction-aware funding
   filter (`max_adverse_funding_rate`, reject long if funding > +x, short if < -x),
   `exits` (same keys as Solana config — ROE-multiple semantics), `risk`
   (`daily_loss_limit_usdt`). Defaults: `db_path` `memebot-mexc.sqlite`, `log_path`
   `memebot-mexc.log`, mode paper.
3. **`mexcbot/scanner.py`** — universe = USDT perps from detail+ticker, minus denylist, plus
   whitelist, volume floor, ranked by |24h change|·volume; `Candidate` carries symbol, price,
   funding rate, volume, 24h change; 5m/1h change filled from klines only for the top
   `max_deep_checks_per_cycle` candidates (rate-limit hygiene, mirrors the Solana bot's
   cheap-filter→deep-check split). Record all candidates to DB (unbiased cohort, like
   `record_candidates`).
4. **`mexcbot/strategy.py`** — `entry_signal(c, cfg) -> (side|None, why)`; `exit_action(...)`
   copied in shape from `bot/strategy.py` (it already works on a generic multiple).
5. **`mexcbot/portfolio.py`** — SQLite (USDT REAL columns, not lamport ints): `positions`
   (`side` TEXT long/short, `leverage`, `margin_usdt`, `qty`, `entry_price`, `banked_usdt`,
   `funding_usdt`, `fees_usdt`, `tp_stage`, `peak_multiple`, `liq_price`, timestamps, mode,
   exit_reason), `fills`, `candidates`. Same method surface as `bot/portfolio.py`.
6. **`mexcbot/execution.py`** — `PaperFuturesBroker` with the locked math:
   - Fill at last price ± `slippage_bps` (adverse), entry/exit fee = notional × taker_fee_rate.
   - `dir = +1 long / -1 short`. Unrealized PnL = `dir * (P - entry) * qty`.
   - **multiple = (banked + equity_now) / total_cost**, equity_now = remaining margin +
     uPnL + accrued funding − estimated exit fee; total_cost = initial margin + entry fee.
     (Same shape as Solana `(sol_received + value)/sol_spent`.)
   - Partial close fraction f: realize f of (margin + uPnL) − fees; qty, margin ×= (1−f).
   - **Liquidation (isolated linear):** `liq_price = entry * (1 - dir*(1/leverage - mmr))`;
     if current price crosses it → force-close at liq price, full remaining margin lost,
     exit_reason `liquidated`. Note gap risk: we only see current price each cycle.
   - **Funding:** at each 8h mark crossed (00:00/08:00/16:00 UTC), accrue
     `-dir * funding_rate * notional` using the current rate (document: approximation, real
     settlements use the rate fixed at settlement).
   - Startup validation: warn/refuse if `stop_loss_pct/100 >= 1 - mmr*leverage` (stop must
     trigger before liquidation, else the stop is fiction).
   - `LiveFuturesBroker`: signed client stub, refuses to start without
     `MEMEBOT_I_UNDERSTAND_THE_RISKS=yes` + `MEXC_API_KEY`/`MEXC_API_SECRET` in `.env`, and
     logs the KYC requirement. Not wired to real orders until paper validates.
7. **`mexcbot/main.py` + `run_mexc.py` + `config.mexc.json`** — cycle orchestration and
   report, both mirroring `bot/main.py` / `run.py` closely (same log line styles so the
   existing log-parsing habits/dashboards can adapt later).
8. **Verify:** `python run_mexc.py --once` (paper) → expect discovery counts, hold/enter log
   lines, rows in `memebot-mexc.sqlite`; then `python run_mexc.py --report`. Then a short
   continuous run.
9. **Later / optional:** NERV dashboard MEXC support; kline-history backtester for the
   long/short strategy (apply the locked anti-cheat rules from memory if built).

**Open questions only Kenny can answer** (proceed with stated defaults meanwhile):
- Default leverage (proposed 3x isolated) and per-trade margin (proposed 50 USDT paper)?
- Shorts AND longs both enabled by default? (Proposed: yes, both.)
- Does he have a KYC'd MEXC account + API key yet? (Only matters for Phase 2 live; paper
  needs nothing.)
