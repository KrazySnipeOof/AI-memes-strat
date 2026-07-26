# Honest engine: P0-P4, and the BASE remake

The BASE paper trial (2026-07-23..25, `memebot-asym`) closed 8 trades at
**12.5% WR / 0.759x avg / -0.482 SOL (1.00 -> 0.518)** against a backtest that
read **84.9% WR / 1.605x**. This is what was wrong and what changed.

## The headline

**73.6% of the 318-trade backtest never finished.** `simulate()` defaulted its
exit reason to `data_end`: when a token's candle history ran out, the position
was marked closed at the last candle price and counted as a trade. Median hold
for those was **0.99h against an 8h `max_hold_min`** - three quarters of the
book was frozen at the one-hour mark, before the losses that arrive later could
land, and the frozen subset scored 87.2% WR / 1.425x.

Candles stop because **the token stops trading**. Checked on the fresh sample:
last-candle timestamps spread uniformly over 28 hours with no clustering (max
6.2% in any single hour), so this is not a fetch cutoff. Median candle span is
**0.72h and 83% of fresh listings go silent within 3h of listing.** An
unsellable bag is a total loss, so that is now the default treatment.

## What changed

### P0 - censored trades (`backtest.py`)

`simulate()` takes `coverage=`:

| policy | behaviour |
|---|---|
| `loss` (**default**) | un-banked remainder -> 0.0, reason `no_coverage`. Realistic and conservative. |
| `drop` | exclude the trade. Honest about not knowing, but biased toward tokens the provider kept covering. |
| `last` | the old behaviour. Retained **only** so the bias can be measured; the CLI prints a warning. |

Already-banked take-profit proceeds are kept - only the remainder dies. Every
run now prints `censored (candles ended before a real exit): n/N`, and the count
is written into `reports/backtest_summary.json`.

### P1 - age-invariant entry gate (`backtest.py`, `tradecards.py`)

`entry_setup_ok` computed `cred_peak` and the nuke count over the token's
**entire** pre-entry life, so the gate silently changed meaning with age: at the
fitted 30m entry age it saw ~30 candles; live entries at 11h fed it ~660. It was
measuring "60% of the 11-hour high" and "<=1 collapse in 660 candles" instead of
the validated rule. New `peak_window_min: 30` bounds both. Verified: on an
identical recent base, the old gate flips to reject at 300m+ purely because
older history piled up nukes; the new one returns the same verdict at 30m, 60m,
120m, 300m and 660m.

`bot/birdeye_ws.py` calls `entry_setup_ok` directly, so the live gate for BASE
and HOLDER picks this up with no change. `tradecards.py` reimplemented the loop
and was fixed to match, or its tiles would disagree with the gate's own verdict.

### P2 - realistic stop/trail fills (`backtest.py`)

Stops and trailing stops filled at *exactly* their trigger level no matter how
far the candle low sat below. Measured against the live trial, trailing stops
filled **15-27% below trigger** (RDLN -27.1%, sharkdog -22.6%, RAKO -15.0%) and
stops 1-21% below. `FILL_MODEL` now places the fill `low_weight` of the way
from the trigger toward the candle low (default 0.7) plus a slip haircut (3%
stops, 5% trails). `--low-weight 0` / `--fill-at-trigger` restores the old model.
See the fill-sensitivity table under the fresh lab - this dial matters more than
anything else in the engine.

### P3 - entry age < 2h, hold < 3h (all three configs)

| | before | after |
|---|---|---|
| `filters.max_age_min` | 1440 / 720 / 1440 | **120** |
| `exits.max_hold_min` | 480 | **180** |
| `websocket.bounce.max_wait_min` | 720 | **120** (was dead config under a 120m entry cap) |

Checked before applying, so this does not quietly break the one strategy that
works: **a 120m cap keeps 93.2% of BOUNCE triggers** (median trigger age 27m).

### P4 - uptime (`autostart.ps1`)

The trial ran 58h wall-clock with **14.18h dark (24.5%)**, including one
unbroken 10.74h gap during which `looong` was held 13.4h against an 8h
`max_hold`. `autostart.ps1` registers all five processes with Task Scheduler at
logon with restart-on-failure every 60s.

```
powershell -ExecutionPolicy Bypass -File autostart.ps1
powershell -ExecutionPolicy Bypass -File autostart.ps1 -Remove
```

## Before / after on the fresh out-of-time sample

`python p0_validate.py` decomposes it one fix at a time (BASE, incumbent
ladder, entry age 30m):

| step | n | WR | avg | censored |
|---|---|---|---|---|
| pre-fix engine, as shipped | 64 | 85.9% | 2.348 | 53 (83%) |
| + P1 bounded gate | 65 | 84.6% | 2.326 | 54 |
| + P2 honest fills | 65 | 83.1% | 2.313 | 54 |
| + P0 `drop` | 11 | 72.7% | 6.791 | 0 |
| **+ P0 `loss` (default)** | **65** | **12.3%** | 1.323 | 54 |

P1 and P2 barely move the number. **P0 is the whole story.** And the corrected
engine predicts **12.3% WR against the 12.5% the live trial actually
delivered** - the fix reproduces reality, the old engine did not.

## The BASE remake

The ladder was never the main problem; the entry age and the censored engine
were. Capping the hold does help - fewer positions die mid-position - but it
does not rescue the strategy. Exact before/after on **the same sample that
produced the original 1.6x** (`reports/bd_tokens.json`, entry age 30m, min vol
$8k, cost 4%):

| | n | WR | avg | median | censored |
|---|---|---|---|---|---|
| OLD engine + OLD ladder (8h) — *the 1.6x number* | 310 | 84.8% | **1.607** | 1.291 | 74.5% |
| NEW engine + OLD ladder (8h) | 314 | 15.9% | 0.657 | 0.389 | 74.2% |
| **NEW engine + NEW ladder (3h) — shipped** | 314 | **26.4%** | **0.752** | 0.389 | **63.1%** |

The 3h cap cuts censoring 74.2% -> 63.1% and lifts WR 15.9% -> 26.4%, but the
average only moves 0.657 -> 0.752. **BASE is negative-expectancy on the full
sample, and the remake does not fix that** - it makes a bad strategy less bad.

Entry-age sensitivity holds across 30-120m, so the `max_age_min: 120` ceiling is
at the edge of what the data supports - **30-60m is the better-supported zone**
if it is ever tightened. Entry liveness gates (recent volume + traded-candle
continuity) were swept and did not discriminate.

### Correction: the fresh sample was tail luck

An earlier pass on `bd_tokens_fresh.json` (n=65) read the remade ladder at
**1.47x / 26% WR** and it was reported as clearing breakeven. The full sample
(n=314, 5x larger, longer window) reads **0.752x** for the identical ladder. The
fresh number was carried by a few 20x hard-take-profits that dilute at scale -
the larger sample has only 4 of them in 314 trades. **Treat 0.752x as the
number.** Both samples were checked for fetch-truncation and neither shows it
(biggest single end-hour holds 1.6% and 6.2% of tokens respectively), so the
gap is sample size and window, not a data artifact.

BOUNCE and H1 still rank above BASE on the fresh sample, but **not** at the
published 1.135x / 1.028x - those were computed with the same optimistic fill.
Corrected, they read 0.808x and 0.832x, i.e. also below breakeven. See the fresh
lab section below.

## Monte Carlo, re-run on the corrected pool

`python montecarlo.py --refresh-trades` regenerated the pool through the fixed
`simulate()` (314 trades, WR 26.4%, avg 0.752, median 0.389; exit mix
`no_coverage=198, time_stop=65, trailing_stop=38, stop_loss=9, hard_tp=4`).

| | pre-fix | corrected |
|---|---|---|
| pool avg | 1.607x | **0.752x** |
| EV per trade | positive | **-24.8% of stake** |
| Sharpe / Sortino | positive | **-0.15 / -0.37** |
| edge margin | +36% extra cost tolerated | **-33% — the edge is already gone** |

10,000 paths x 100 trades, 0.25 SOL stake from 5.00 SOL:

| scenario | median final | P(loss) | P(DD>=50%) |
|---|---|---|---|
| iid | **0.00 SOL** | 93.4% | 97.3% |
| block10 | 0.00 SOL | 95.4% | 97.9% |
| stress (+5% cost) | 0.00 SOL | 95.7% | 98.4% |
| no_top (top 5% removed) | 0.00 SOL | **100.0%** | 100.0% |
| frac5 (5% of equity) | 1.03 SOL | 97.8% | 99.2% |

**The prior Monte Carlo conclusion does not survive.** It previously read "path
risk negligible, all risk is distributional, 36% edge margin" - computed on a
pool that was 74.5% censored trades marked at their last price. On the corrected
pool there is no edge to have path risk *around*: the median path is ruin in
every fixed-stake scenario, and fractional sizing only converts ruin into a slow
bleed. `no_top` at 100% loss is the same tail-dependence that failed live.

Validating any remake needs a **freshly fetched window** with a pre-registered
eval - `reports/bd_tokens_fresh.json` has now been used for selection and can no
longer serve as its own out-of-sample.

## Fresh lab, re-run: nothing clears 1.0

`freshlab.py` never used `backtest.simulate()` - it imports `sim` from
`loop8_eval`, a second exit machine. It therefore did **not** inherit P0/P2
automatically. Two things were already right there and one was not:

- **P0 was already handled**: `sim()` credits the un-exited remainder at 0.0
  unless `intact_end` says the pool still has liquidity, and freshlab passes
  `intact_end=False`. That is why freshlab read BASE at 0.226x while
  `backtest.py` read 1.605x on the same strategy. The lab was the honest one.
- **P1 came for free**: freshlab calls `backtest.entry_setup_ok`, so the bounded
  lookback applies with no change.
- **P2 was missing**: `sim()` filled stops and trails at exactly their trigger
  level. Now fixed, sharing `backtest.fill_at`.

`--pre-fix` reproduces the published numbers exactly (BASE-EA 0.226, BOUNCE
1.135, H1-EA 1.028), which is the control that says the harness is sound.

| family | published (pre-fix) | corrected + 3h hold |
|---|---|---|
| BASE-EA | 0.226x @ 6% | **0.508x @ 20%** |
| **BOUNCE** | **1.135x @ 57%** | **0.808x @ 46%** |
| **H1-EA** (holder distributed) | **1.028x @ 61%** | **0.832x @ 50%** |
| H2-EA (holder loose) | 0.850x @ 54% | 0.720x @ 44% |
| H0-EA (no gate) | 0.599x @ 36% | 0.582x @ 30% |
| H3-EA (concentrated, inverse ctl) | 0.487x @ 27% | 0.527x @ 24% |
| V3 (churn) | 0.789x @ 37% | 0.828x @ 34% |
| V1 / V2 / V4 | 0.70-0.72x | 0.73-0.74x |

**Neither BOUNCE nor H1 survives. Not one family clears 1.0.** The two that
justified reallocating away from BASE were carried by the same optimistic
stop/trail fill that flattered BASE.

### How much of this is the fill assumption?

BOUNCE does not use the entry gate, so its whole move is P2. Decomposed
(n=54 throughout):

| fill assumption | BOUNCE avg | WR |
|---|---|---|
| at trigger, no slip (pre-fix, optimistic bound) | 1.135 | 57.4% |
| slip only (3%/5%), still at trigger | 1.080 | 57.4% |
| **70% toward the candle low + slip (shipped)** | **0.808** | **46.3%** |
| 100% at the candle low, no slip (pessimistic bound) | 0.728 | 40.7% |

The slip percentages are almost irrelevant; **where the fill lands between the
trigger and the candle low is everything.** Neither bound is the truth, which is
why `low_weight` is now an explicit dial rather than a hidden assumption. The
default 0.7 leans pessimistic because the only live fill data available - the
BASE trial's three trailing stops, which came in 15%, 23% and 27% under trigger -
sits near that end. If real fills are better than that, BOUNCE's honest number
rises toward 1.08; it does not reach the published 1.135 under any setting that
includes slippage.

### What does survive

The holder signal still **ranks** correctly under the corrected engine:
H1 0.832 > H2 0.720 > H0 0.582 > H3 0.527, monotone, with the deliberately
inverted control (H3, concentrated holders) worst. Same for BOUNCE at 0.808 vs
the 0.582 ungated baseline. **These are real discriminators - they sort good
tokens from bad. They just do not sort hard enough to cover the death rate and
the cost of getting out.** That is a materially different finding from "the
signal was noise", and it is the part worth building on.
