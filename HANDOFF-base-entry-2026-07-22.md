# HANDOFF — base-entry filter adoption + dashboard/usage overhaul (session 2026-07-22)

This file replaces the 2026-07-21 MEXC handoff, which is preserved verbatim as
`HANDOFF-mexc-2026-07-21.md` (MEXC mode: fully designed, ZERO code written — see that file
before starting any MEXC work). Campaign verdicts live in Claude's memory files under
`C:\Users\kensm\.claude\projects\C--Users-kensm-memecoin-strat-v1-AI-memes-strat\memory\`
(`memebot-base-entry-filter`, `memebot-bounce-campaign`, `memebot-round4-verdict`,
`memebot-target-rules`, `memebot-two-trees`).

## 1. Goal

**End goal:** a Solana memecoin strategy with real forward edge; backtests only rank
candidates — **paper trading is the arbiter** (locked rule, do not relitigate).

**Immediate sub-goal at session end:** Kenny asked "how can I have it live backtesting?
Does it require birdeye websocket?" Answer delivered: no websocket needed (and not on his
Lite plan — it's Business-tier); recommended (a) start the existing paper bot (zero Birdeye
CU) and/or (b) a scheduled rolling-backtest loop on GeckoTerminal until the Birdeye cycle
resets. **Kenny has NOT yet chosen** — that decision is step 1 of Next steps.

**Constraints/decisions locked this session:**

- **Base-entry setup filter adopted** (`DEFAULT_SETUP` in `backtest.py`): 10m range ≤25% of
  entry · ≤1 nuke (body ≤0.70) · entry ≥60% of credible peak (vol≥$500, cl≥0.5h, peak capped
  2×cl) · 15m trend 0.90–1.25x. Time-based windows (works on 1m and 5m candles). Reasoning:
  Kenny showed 4 winning trade cards (2026-07-20: goldlon, ITHACA, LABS, FABLE) and asked to
  only take such trades; at 1m resolution only FABLE was a true tight base (cards resample
  ~90min → visually flat), so the *pattern* was quantified instead and validated:
  1028 trades 68%/1.234x → 171 trades 81%/1.544x, sha1 halves tr 1.752x/82% va 1.361x/80%.
  **Do NOT retune thresholds on this sample** (one degree of researcher freedom spent).
- **`simulate()` keeps `setup=None` default** so sweeps + pre-registered loop8/loop9 evals
  are byte-identical; filter is opt-in and ON in `backtest.py` CLI and `tradecards.py`
  (escape hatch `--no-setup-filter` in both).
- **Live mirror in `config.asym.json` entry block**: `min_chg_m5_pct` −10, `max_chg_m5_pct`
  25 (was +1/+60 momentum-chase). Reasoning: base entry, not spike-chasing; `bot/` code
  untouched (config-driven).
- **Birdeye usage ledger is billing-cycle-synced** (`bdusage.py`): cycle anchored day 20
  (Kenny's account Packages page), Lite plan 2.5M CU/cycle, 15 RPS, overage $15/1M CU.
  **OHLCV = ~110 CU/call — calibrated from the account cap actually tripping** (first
  estimate 60 was ~2x low). `snapshot()` reprices from request counts so recalibrations
  apply retroactively. `reports/bd_usage_baseline.json` = pre-ledger reconstruction
  (13,900 req / 1,475,000 CU); edit that file to recalibrate against Birdeye's Metrics page.
- **NO Birdeye API spend until Aug 20 2026** — cycle at ~99.4% (2.49M/2.5M). Any further
  request bills overage. GeckoTerminal/DexScreener/Jupiter are the free alternatives.
- **`reports/bd_nodata.json` ledger**: tokens with no usable candles are never re-fetched
  (the 30d index resume burned 7,031 requests to learn it holds ~1 recoverable token).
- **blocklist family rule ignores `"?"` symbols by design** (else 800+ unknown-symbol
  salvaged tokens could be mass-purged by one flagged member).

## 2. Current state of the code

**Verified working (all by direct run + curl this session):**

- **Backtest telemetry (adopted variant E)**: 220 trades · 84% WR · 1.57x avg ·
  +56.9%/trade on the grown+cleaned sample (`reports/backtest_summary.json`, regenerated).
  Caveat: majority of exits are `data_end` (12h candle windows) — inherent, disclosed in UI.
- **Sample**: `reports/bd_tokens.json` = 13,460 tokens (newest listing 2026-07-22 09:09 UTC);
  `.bd_cache` = 1m candles keyed by mint (~6.4k usable); `reports/bd_blocklist.json` = 4,550
  (693 wash-ramps + 3,857 serial redeploys, 243 factory symbols);
  `reports/bd_nodata.json` = 7,026 known-dead mints.
- **NERV dashboard** (`http://localhost:8700`): persistent sticky top nav on both pages
  (HOME / TRADE JOURNAL tabs + live Birdeye meter polling `/api/bdusage` every 60s);
  PROTOTYPE section is a 2×2 grid: ENTRY DOCTRINE (all entry gates, served from config —
  never hardcoded) · EXIT DOCTRINE · BACKTEST TELEMETRY (incl. ENTRY FILTER row) · PAPER
  TRIAL; SORTIE LOG newest-close-first with real symbols, caption from journal's strategy
  field ("asym-runner v2 + base-entry filter"). Meter reads 23,243 req · ~2.49M/2.5M CU
  (99.4%) · Jul 20 – Aug 20.
- **Trade journal** (`/trades` = `reports/trade_cards.html`): 220 trades, 80 cards, filter
  documented in header; `reports/trade_journal.json` feeds the sortie log (rows sorted by
  `end_ts` desc — the panel displays close time).

**Broken/incomplete:**

- **130 tokens still have symbol `"?"`** — DexScreener has delisted them; unrecoverable
  free. Cosmetic only.
- **~1,175 fresh-run no-data mints are NOT in `bd_nodata.json`** — bdfresh was killed
  before its end-of-run save (in-memory set lost). A future bdfresh re-run would re-pay for
  them (~110 CU each). Fix idea in Next steps 4.
- **Paper bot NOT running** (PAPER TRIAL shows STANDBY; MAGI log empty).
- **Dashboard runs as a background task of THIS Claude session** — it dies when the session
  closes. Restart: `python dashboard/server.py` (serves on 127.0.0.1:8700).

**Git:** branch `main`, last commit `b3f8516` (initial). ALL session work is uncommitted:
modified `.gitignore`, `HANDOFF.md`, `backtest.py`, `config.asym.json`,
`dashboard/index.html`, `dashboard/server.py`, `sweep.py`; untracked `bdusage.py` (new),
`bdfresh.py` (new), `HANDOFF-mexc-2026-07-21.md` (new), `bdfetch.py`, `bd*.py`, `sweep2-4*`,
`loop8/9_eval.py`, `tradecards.py`, `blocklist.py`, `canvases/`, `web/`, caches. Nothing
committed this session (Kenny never asked).

**Commands (all run from repo root `C:\Users\kensm\memecoin-strat-v1\AI-memes-strat`):**

- Dashboard: `python dashboard/server.py` → http://localhost:8700
- Paper bot (asym): `python run.py --config config.asym.json` (`--once`, `--report`)
- Backtest telemetry: `python backtest.py --tokens-json reports/bd_tokens.json --cache-dir .bd_cache --entry-age 30 --min-entry-vol 8000 --no-report`
- Trade cards/journal: `python tradecards.py`
- Blocklist refresh: `python blocklist.py` (after any new candle fetch)
- Fresh listings fetch: `python bdfresh.py` — **DO NOT RUN before Aug 20** (overage)
- Env: Windows 11, PowerShell 5.1 + Git Bash, Python 3.11 (`requests` installed),
  `BIRDEYE_API_KEY` in `.env`. No new installs this session.
- Windows gotchas: kill processes by PID from `netstat -ano | grep :8700` (taskkill by
  image name misses these python processes); console is cp1252 — printing emoji token
  symbols in verification scripts crashes (`UnicodeEncodeError: 'charmap' codec`).

## 3. Files being actively edited

All **complete** (no mid-edit files):

- `backtest.py` — added `DEFAULT_SETUP`, `entry_setup_ok(pre, entry_ts, entry, setup)`,
  `simulate(..., setup=None)` gate after the volume check, `--no-setup-filter` CLI flag,
  setup echoed in `summary["sample"]["setup_filter"]`.
- `tradecards.py` — setup filter on by default; sticky nav + Birdeye meter (embedded
  snapshot + live poll); journal rows sorted by close desc; strategy label
  "asym-runner v2 + base-entry filter".
- `config.asym.json` — entry block only: `min_chg_m5_pct` −10.0, `max_chg_m5_pct` 25.0.
- `dashboard/server.py` — `/api/bdusage` endpoint; `asym_state()` now serves
  `entry.{signal,funnel,safety}`; `build_state()` serves `backtest_strategy` from journal.
- `dashboard/index.html` — `.topnav` (sticky, both-page nav + meter, polls `/api/bdusage`);
  `#asym` container class `grid3`→`cols` (2×2); ENTRY DOCTRINE panel (built from served
  values incl. `setup_filter`); sortie caption uses `st.backtest_strategy`.
- `bdusage.py` (NEW) — billing-cycle usage ledger; see §1 locked decisions.
- `bdfresh.py` (NEW) — forward sample growth from new_listing feed (last ~3 days), dedupes
  vs index/cache/nodata, merges into index at END of run (see §4 for the kill hazard).
- `bdfetch.py` — `bdusage.record(path)` inside `bd_get`; `NODATA_PATH` ledger (skip
  known-dead free, save every 100 + at end).
- `reports/bd_usage_baseline.json` (NEW) — see §1.
- `HANDOFF-mexc-2026-07-21.md` (NEW) — preserved MEXC spec.

**Do NOT touch:** everything under `bot\` (working live-bot code; this session changed only
its config), `config.json`, `memebot*.sqlite`, the pre-registered `loop8_eval.py` /
`loop9_eval.py` (verdict authority requires them frozen), `sweep*.py`, `.bd_cache_ext/`
(60d/oot campaign candles), `blocklist.py` thresholds (validated), and the `"?"-symbol
guard in `blocklist.py`.

## 4. Failed attempts — do not repeat

- **Resuming the 30d index to grow the sample** (`python bdfetch.py`, 2026-07-22 morning):
  7,031 requests → **1** new token; the other ~7,026 are past Birdeye's minute-candle
  retention ("no usable data"). Verdict: dead for good — that's why `bd_nodata.json` exists
  and is seeded with all 7,026. Growth must come from RECENT listings (bdfresh) or other
  sources, never from re-fetching this index.
- **OHLCV at 60 CU/call estimate**: account cap tripped when the local meter read ~60%.
  Solved from ground truth (2.5M cap ÷ 22,355 lifetime ohlcv calls + fixed costs) →
  **~110 CU/call**. Verdict: calibrated in `bdusage.CU_COST`; snapshot reprices
  retroactively (do not trust stored `cu_est` — it's write-time priced, kept only for
  legacy).
- **bdfresh merge-at-end design**: killing the run (Birdeye limit notification) lost the
  in-memory token metadata and nodata set → 869 orphan cache files with no index entries and
  no symbols. Salvage: merged by cache mtime ≥ 2026-07-22 15:00 UTC with `created_ts` =
  first candle ts, symbol `"?"`; then DexScreener batch API (free,
  `api.dexscreener.com/latest/dex/tokens/{addr1,...}`, 30/call) backfilled 793/923 symbols.
  Verdict: viable with modification — if bdfresh is edited, make it merge + save nodata
  incrementally (every 100 tokens), not at end.
- **Symbol backfill side effect (expected, correct)**: real symbols exposed 221 more
  serial-redeploy factory tokens → blocklist 4,329→4,550 → telemetry 255/84%/1.60x →
  **220/84%/1.57x**. The purged trades (incl. a 3.42x) were factory fabrications. Verdict:
  the 220-trade numbers are the honest ones; do not chase the 255.
- **`taskkill //F //IM python.exe`** to stop servers: matched nothing (store-python image
  names differ). Kill by PID from netstat instead.
- **Index-based feature windows** (pre[-10:]) in the filter analysis vs shipped time-based
  windows: differ on gappy 1m data (e.g. ITHACA passes time-based, fails index-based).
  Verdict: time-based is the shipped, validated definition — keep it.

## 5. Next steps

1. **Ask Kenny / act on his answer to the live-backtesting question** (he was offered:
   start paper bot, build scheduled rolling loop, or both). Recommended default if he says
   "go": start the paper bot — `python run.py --config config.asym.json` — zero Birdeye CU,
   uses the new −10/+25 entry window, dashboard auto-detects it (PAPER TRIAL → ACTIVE,
   SORTIE LOG → LIVE PAPER on first close). Reasoning: paper is the locked arbiter and the
   filter now has out-of-sample support (fresh Jul 21–22 tokens ran ≈90% WR/≈1.7x before
   blocklist tightening, 84%/1.57x pooled after).
2. **If he wants the rolling loop now**: build it on GeckoTerminal (free, ~27 req/min,
   5m candles — `backtest.py:fetch_candles` already does GT) over candidates the bot
   records + newest GT pools; switch data source to Birdeye after Aug 20. Do NOT use
   Birdeye before Aug 20.
3. **Suggest committing the session's work** (one commit for strategy+filter, one for
   dashboard/usage infra, or as Kenny prefers). Everything is uncommitted on `main`.
4. **Small hardening task**: make `bdfresh.py` save incrementally (see §4) and append its
   no-data mints to `reports/bd_nodata.json` every 100 tokens.
5. **After Aug 20 (cycle reset)**: `python bdfresh.py` to resume forward growth (~4k
   listings/day ≈ 450K CU/day at 110 CU/call — budget ~5 days of full-pace fetching per
   cycle, or throttle `--max-tokens`).
6. **Open questions only Kenny can answer**: start paper bot now? tolerate Birdeye overage
   ($15/1M CU) before Aug 20 for fresher data? commit now? does he want the MEXC mode
   (preserved spec) picked back up at some point?
