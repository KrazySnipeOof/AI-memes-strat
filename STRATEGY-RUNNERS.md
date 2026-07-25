# Running more than one strategy at once

`freshlab.py` scored every pre-registered strategy family on the fresh
out-of-time sample (`reports/strategy_lab.json`, listings 2026-07-21+, never
touched by a sweep). Three families are worth live paper trading; only one was
being traded.

| family | fresh-OOT result | config |
|---|---|---|
| BASE — base-entry gate | 0.226x @ 6.2% WR, n=64 | `config.asym.json` |
| BOUNCE — loop8 P3 | **1.135x @ 57.4% WR, n=54** | `config.bounce.json` |
| HOLDER — H1 distributed | **1.028x @ 60.7% WR, n=28** | `config.holder.json` |

BASE is the strategy that was already live, and the fresh sample says it is the
worst of the three. That is the reason to run all three side by side rather than
argue about it: paper trading is the arbiter.

## Running them

One process per config. `--workdir` is optional and only needed when the code
lives somewhere other than the trial data (e.g. a worktree):

```
python run.py --config config.asym.json
python run.py --config config.bounce.json
python run.py --config config.holder.json
```

Each runner owns its own files and shares nothing writable with the others:

| | BASE | BOUNCE | HOLDER |
|---|---|---|---|
| db | `memebot-asym.sqlite` | `memebot-bounce.sqlite` | `memebot-holder.sqlite` |
| log | `memebot-asym.log` | `memebot-bounce.log` | `memebot-holder.log` |
| ws state | `reports/ws_state.json` | `reports/ws_state-bounce.json` | `reports/ws_state-holder.json` |
| ws status | `reports/ws_status.json` | `reports/ws_status-bounce.json` | `reports/ws_status-holder.json` |

The websocket snapshot paths come from the new `strategy` config key. Before
this, they were module constants — two runners would have kept restoring each
other's watchlist and candle store. `strategy` defaults to `"base"`, which keeps
the original filenames, so an existing deployment restarts warm exactly as it
did.

## How each entry gate runs live

`websocket.setup_family` picks which validated pattern the live candle gate
applies.

**BOUNCE** (`setup_family: "bounce"`) is `loop8_eval.bounce_entry` config P3 —
dd 0.55, trigger volume $500, max 1 collapse candle — re-implemented against the
live candle store in `birdeye_ws._bounce_gate`. It walks candles from listing,
tracks the credible peak (volume floor + close at least half the high, peak
capped at 2x close) and fires on the first credible green candle 55% below that
peak with $2k cumulative volume behind it.

**HOLDER** (`setup_family: "base"`) is the base-entry gate plus the H1
holder-distribution gate, which is the existing `insiders` block taken out of
shadow mode and set to the H1 thresholds: holders >= 50, top10 <= 50%,
top1 <= 20%, snipers <= 30%. `max_insider_pct` and `max_creator_pct` are set to
100 so only the H1 legs bind. `max_top1_pct` is new; it defaults to 100, so
configs written before it are unaffected.

Both use their registered exit doctrine. The live exit machine arms its trailing
stop when a take-profit stage fires, so `"sell_fraction_of_remaining": 0.0`
expresses the backtest's `arm` level: advance the stage, sell nothing, start
trailing. Previously a zero fraction would have fired a 1-unit dust sell.

## Where live cannot match the backtest

Worth writing down, because these are the seams where a paper trial will
diverge from the lab number:

- **Bounce triggers in the first ~4 minutes are unreachable.** The gate needs
  candles and `filters.min_age_min` is 15. Replaying the fresh sample, the live
  gate agrees with `bounce_entry` on 56 of 59 triggers; all 3 misses fired
  before minute 5.
- **Stale triggers are refused, not chased.** `max_trigger_age_min` (3 min)
  rejects a bounce that fired earlier. The backtest bought at the trigger
  candle's close; buying an hour later is a different, worse strategy. On the
  first live cycle this refused WIF (150m late), sharkdog (425m) and Neném
  (353m).
- **The forming candle is excluded** from the bounce scan. Its close and volume
  are not final and the trigger tests both.
- **Sniper share is effectively unavailable.** H1's sniper leg needs a number
  only GMGN publishes, and GMGN is Cloudflare-gated; RugCheck reports -1 and the
  gate skips that leg. Live H1 is therefore holders/top10/top1 only.
- **Holder features are measured differently.** The lab derived them from an
  entry-time trade-tape replay (`holdertape.py`); live they come from RugCheck's
  current holder graph. Same concept, different instrument.
- **BOUNCE carries live-only funnel filters** the backtest had none of
  (liquidity floor, rugcheck, round-trip cost). Its momentum gate is opened up
  (`min_chg_h1_pct: -100`) because a token 55% off its peak fails any momentum
  filter by construction — leaving BASE's `-20` there would have produced zero
  trades.
- **HOLDER should trade rarely.** H1 passed 28 of 351 base entries in the lab —
  8%. Expect roughly one HOLDER entry per twelve BASE entries.

## Cost

Three runners on one Birdeye Premium key (20M CU/month). A runner in steady
state costs ~2k CU/hour, so three is ~4.3M/month against the plan. The ceiling
is `websocket.max_backfills_per_cycle` (4) — every REST OHLCV backfill is
~110 CU, so a runner that backfills its full budget every 45s cycle would spend
~35k CU/hour. Watch the dashboard's usage panel; if it climbs, lower that key
first.
