# Three strategies that clear 2.0x/trade and 60% profitable paths

**The ask:** three distinct strategies that pass the real engine, average ~2.0x
per trade (0.25 SOL in, 0.50 SOL out), and finish profitable on at least 60% of
Monte Carlo paths.

**The result:** three found, all three clear both bars on both halves of the
sample. They are also the *first* strategies in this repo to survive the
`no_top` test that killed BASE, BOUNCE and H1.

| | n | mean | median | WR | trim5 | train | holdout | worst of 5 blocks | MC P(profit) | no_top |
|---|---|---|---|---|---|---|---|---|---|---|
| **BELOW-VWAP** | 126 | **2.384** | 0.982 | 46.8% | 1.557 | 2.466 | 2.305 | 1.316 | **100.0%** | 99.9% |
| **DIP** | 107 | **2.410** | 0.801 | 43.0% | 1.502 | 2.493 | 2.300 | 1.522 | **100.0%** | 99.3% |
| **SURGE** | 107 | **2.174** | 1.006 | 51.4% | 1.380 | 2.125 | 2.241 | 1.622 | **100.0%** | 99.7% |

`trim5` = mean with the best 5% of trades deleted. `no_top` = share of Monte
Carlo paths still profitable after the top 5% of the pool is removed entirely.
Full output, every number in this document included: `STRATEGY-SEARCH-2026-07-27-evidence.txt`
(regenerate with `python validate.py qvwap qdip qsurge`).

Monte Carlo is 10,000 paths x 100 trades, 0.25 SOL fixed stake from 5.00 SOL,
through `montecarlo.run_scenario`. Median final equity is 33-40 SOL; every
scenario (iid, block-10 bootstrap, +5% cost, 5% fractional sizing) lands at
99.7-100% profitable paths, against a 60% bar.

---

## First: the engine was still flattering strategies (P5)

The search found a screen reading 2.0x mean. Its single biggest trade was a 50x
hard-take-profit that filled like this:

```
GOAT   exit candle:  high = 593.9x entry   close = 2.02x entry   volume = $16
       (that token's median candle volume: $4,387)
```

A $16 print is not an exit. **P2 had made stops and trails fill pessimistically
toward the candle low, but left every profitable exit realizing its price with
no liquidity check at all.** The same audit found a 36x *time-stop* on a candle
that traded $25 and a 22x time-stop on a candle that traded $4.

`FILL_MODEL` now carries the mirror-image rule, applied to **every** exit:

1. **Sellable** - a candle can absorb the exit only if it traded at least
   `tp_min_vol_usd` ($200, ~4x a 0.25 SOL exit) *and* at least `tp_min_vol_frac`
   (25%) of the trailing-20-candle median volume.
2. **Liquidity cap** - `liq_peak` tracks the highest close-multiple printed on a
   sellable candle. No exit realizes more than `max(2.0, liq_peak)`.
3. **Wick** - a take-profit fills `high_weight` (0.5) of the way back from its
   trigger toward the candle close, plus 3% slip. The exact top tick is no more
   available to a market order than the exact bottom tick is.

All three are causal - they read only candles up to and including the exit
candle. A blocked take-profit rung does not advance the ladder; it is retried
next candle. Below 2.0x nothing is capped, which is why the live calibration
(whose 20 closed positions contain no >=2x exit) is untouched.

Effect on the audited trades: GOAT 46.6 -> 0.80, CRED 46.6 -> 1.82, bandit
21.8 -> 1.92, while genuinely liquid runs survive intact (a $11k-volume candle
keeps its 46.6x, a $20k-volume candle keeps its 37.0x).

**Verified as a pure tightening**: across 1,500 random tokens P5 lowered 188
multiples and raised **zero**. Turning it off *raises* the reported mean of
these strategies (DIP 2.410 -> 2.933, SURGE 2.174 -> 2.636), so nothing below
depends on the guard being generous.

---

## What actually generates the edge

Not a better screen. A shorter hold, entered earlier.

A censored ("dark") position pays `min(last close, stop) x 0.95 x 0.55` ~ 0.29x
*regardless of where the price actually is* - so the dominant cost in this
market is being caught holding when the candles stop. Every family in
`ENGINE-FIXES-2026-07-25.md` used a 180-480 minute hold and was 47-63% censored.
Censoring by hold length, unconditional, at 5m entry:

| max hold | mean | censored |
|---|---|---|
| 15m | 0.933 | 29.9% |
| 30m | 0.860 | 42.6% |
| 60m | 0.836 | 47.8% |
| 120m | 0.821 | 51.8% |

The median cached token trades for **26 minutes**. A 5-15 minute hold converts
dark exits into real time-exits at market. Unconditionally, with no screen at
all, entry at 3m with a 15m hold reads **1.183x** - already above breakeven,
where every previously-tested family sat at 0.51-0.91x.

The screens then add selectivity on top of that.

## The three strategies

All enter at **3 minutes** of token age. Definitions live in one place,
`strategies.py`, which both the backtest and the live gate call - verified to
select identical trade sets on all 6,285 evaluated tokens.

**BELOW-VWAP** (`config.qvwap.json`) - *everyone who bought the first three
minutes is under water.*
`vwap_ratio < 0.80 & peak_age_frac >= 0.667`
Exit: bank 50% at 10x, 60% stop, 20% trail, 50x cap, 10m hold.

**DIP** (`config.qdip.json`) - *a big green candle printed and price has since
fallen under 45% of its own high; buy the giveback, not the spike.*
`frac_of_peak < 0.45 & best_body >= 1.546`
Exit: bank 50% at 10x, 60% stop, 20% trail, 50x cap, 10m hold.

**SURGE** (`config.qsurge.json`) - *volume accelerating into the entry, price
off its VWAP but well up off the floor.*
`vol_accel >= 1.0 & vwap_ratio < 0.899 & x_from_low >= 1.671`
Exit: bank 50% at 10x, 60% stop, 50% trail, 50x cap, 5m hold.

They are three strategies, not one in three costumes - pairwise Jaccard overlap
of the traded token sets is 0.26 (VWAP/DIP), 0.17 (VWAP/SURGE), 0.27
(DIP/SURGE). The 10x first rung matters: it banks real money *and* arms the
trailing stop, which `simulate()` gates on `stage > 0`.

## Method

`quest.py` -> `search.py` -> `finalists.py` -> `pick.py` -> `validate.py`.

`simulate()` does not know why a token was entered, so for a fixed entry age the
exit machine runs once per (token, ladder) and every candidate screen is a mask
over that matrix - 74M engine calls, then the screen search is free. Screens read
only pre-entry candles, so lookahead is impossible by construction.

Ten anchored families were searched (each with a mandatory, family-defining
condition so a family cannot collapse into a re-parameterisation of another),
ranked on the **worse** of the two halves, then filtered on a one-minute-late
entry test, then the triple was chosen by weakest chronological block.

`validate.py` deliberately ignores the cached matrix and re-runs everything
through `backtest.simulate()` token by token. That caught two real bugs: a
`%.4g` threshold rounding that silently dropped 74 of BELOW-VWAP's 126 trades,
and a train/holdout boundary that differed between search and validation.

## Engine-assumption sensitivity

Every dial, every strategy, mean per trade:

| | BELOW-VWAP | DIP | SURGE |
|---|---|---|---|
| shipped defaults | 2.384 | 2.410 | 2.174 |
| dark slip 30% (current live best fit) | 2.389 | 2.414 | 2.175 |
| dark slip 60% | 2.380 | 2.406 | 2.174 |
| rug 20% (2x observed) | 2.380 | 2.410 | 2.172 |
| fills at the candle low (worst case) | 2.301 | 2.273 | 2.116 |
| P5 vol floor $500 | 2.384 | 2.410 | 2.174 |
| P5 fill at close | 2.381 | 2.393 | 2.155 |
| cost 6% not 4% | 2.335 | 2.360 | 2.129 |

Every cell stays above 2.0x and every cell reports 100.0% profitable paths.
Note `live_calibrate.py` now reports
`DEFAULTS ARE STALE` - the live record has grown to n=19 and best-fits dark slip
**30%**, not the shipped 45%. I did **not** change the default: loosening the
engine while hunting for a strategy that clears a bar is how this repo got
burned twice. Everything above runs at the conservative shipped value; 30% is
shown as sensitivity and is marginally *better*.

---

## Caveats - read these before risking anything

**1. The 3-minute entry is the whole strategy, and it is not what the bot does
today.** The edge decays fast:

| entry age | BELOW-VWAP | DIP | SURGE |
|---|---|---|---|
| 3m | 2.384 | 2.410 | 2.174 |
| 4m | 2.069 | 1.706 | 1.771 |
| 5m | 1.704 | 1.314 | 1.099 |
| 10m | 1.062 | 1.067 | 1.026 |

Live configs today enter at 30-120m. Hitting 3m needs sub-minute launch
discovery and execution; the repo's own list of unmodelled risks already names
"discovery latency". **If the bot is consistently 5 minutes late, these
strategies are worth roughly nothing.** This is the single largest risk and it
is operational, not statistical.

**2. Small samples.** 107-126 trades each over ~44 days - about 2.5 trades/day
per strategy. Chronological blocks hold 20-26 trades, so a single block mean is
noisy.

**3. Still tail-dependent, just no longer fatally.** trim5 of 1.38-1.56 means
deleting the best 5% of trades leaves them profitable - a real improvement on
BASE/BOUNCE/H1, which went to *zero* under the same test. But the mean is still
carried by the right tail, and the median trade is 0.80-1.01. Expect long
stretches of small losses punctuated by the trades that pay for everything.

**4. Search intensity.** ~350 threshold cuts x 8 entry ages x 2,000 ladders x 10
families. That is a large multiple-comparison surface and the mitigations
(both halves, five blocks, engine sensitivity, one-minute-late test, worst-half
ranking) are not the same thing as a fresh out-of-sample. **The universe has now
been fully used for selection and can no longer serve as its own holdout** -
same rule that retired `bd_tokens_fresh.json`.

**5. Live filters were never modelled.** The backtest applied no entry liquidity
floor. `config.q*.json` set `min_liquidity_usd` 3000 and `min_vol_m5_usd` 500
because a 3-minute-old pool is small and the safety layer needs *something*.
Those numbers are a live-only deviation from what was measured.

**6. Unmodelled, as before:** slot contention, downtime, the 0.75 SOL daily loss
limit, and regime change. Both MC samplers assume the future draws from the same
distribution.

## What would falsify this

Paper trading, which is the arbiter here and has twice disagreed with a
backtest. Three configs are ready (`config.qvwap.json`, `config.qdip.json`,
`config.qsurge.json`), each namespaced to its own db/log/state so they can run
concurrently. The first thing to check is not PnL - it is **realized entry age**.
If the median live entry lands past 4 minutes, stop and fix discovery before
reading anything into the returns.
