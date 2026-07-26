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
stops 1-21% below. `FILL_MODEL` now fills at `min(trigger, candle low)` plus a
slip haircut (3% stops, 5% trails). `--fill-at-trigger` restores the old model.

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
were. What actually changed BASE's numbers was capping the hold:

| BASE ladder (corrected engine) | WR | avg | median | censored |
|---|---|---|---|---|
| stop 40 / bank 30% @1.35x / trail 30% / hard 20x / **hold 8h** | 9% | 1.19 | 0.39 | 75.4% |
| same ladder, **hold 3h** | **26%** | **1.47** | 0.39 | **53.8%** |

A shorter hold means fewer positions die mid-position - censoring falls 75.4% ->
53.8% and WR nearly triples. Entry-age sensitivity on the corrected engine holds
across 30-120m (train/valid 1.78/1.31 at 30m, 1.37/1.40 at 60m, 0.97/1.17 at
120m), so the `max_age_min: 120` ceiling is at the edge of what the data
supports - **30-60m is the better-supported zone** if it is ever tightened.

Alternative ladders were fitted on a TRAIN half with VALID held out. A scalp
(all out at 1.20x, stop 30%, hold 3h) gives much steadier trade-by-trade
behaviour - 73.8% WR, median 1.152, only 12/65 total losses - but averages
**0.898**, below breakeven. Entry liveness gates (recent volume + traded-candle
continuity) were swept and did not discriminate: 32 -> 29 trades with the alive
fraction unchanged.

### Read this before trusting the remade BASE

The remade ladder averages above 1.0, but on a **26% win rate with a 0.39
median** - 35% of trades are total losses and the average is carried by a
handful of 20x hard-take-profits in n=65. Removing the top 2 trades leaves
1.150, so it is not one lucky ticket, but it is the same tail-dependent shape
that just lost 24% of the book live. n=65 on one fresh sample is not proof.

The fresh-OOT lab still ranks **BOUNCE (1.135x @ 57.4% WR, n=54)** and
**H1/HOLDER (1.028x @ 60.7%, n=28)** above BASE, and both win by being right
often rather than by hitting a rare 20x. Nothing here changes that ordering.
Validating the remake needs a **freshly fetched window** with a pre-registered
eval - `reports/bd_tokens_fresh.json` has now been used for selection and can no
longer serve as its own out-of-sample.
