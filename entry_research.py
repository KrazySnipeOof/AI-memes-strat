#!/usr/bin/env python
"""Research whether a more SELECTIVE ENTRY produces a real edge under the honest
engine (the exit is fixed at the short-hold bleed-reducer). If any variant is
profitable (avg>1.0) with >=50% of Monte-Carlo paths green, it's a candidate to
deploy; otherwise the honest answer is that the entry side has no edge here.
"""
from __future__ import annotations

import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import backtest as bt   # now the honest engine
from bot import manip, screens

AGE, COST, VOL = 30.0, 4.0, 8000.0
# fixed short-hold exit (the bleed-reducer from the exit sweep)
EXIT = {"stop_loss_pct": 40, "take_profits": [{"multiple": 1.35, "sell_fraction_of_remaining": 0.3}],
        "trailing_stop_pct": 30, "hard_tp_multiple": 20.0, "max_hold_min": 120}


def tighter(**over):
    s = dict(bt.DEFAULT_SETUP)
    s.update(over)
    return s


def organic(pre):
    return (manip.classify(pre, False) or {}).get("class") == "ORGANIC"


def two_sided(pre, frac=0.15):
    tot = sum(c[5] for c in pre) or 1e-9
    return sum(c[5] for c in pre if c[4] < c[1]) / tot >= frac


def volspike(pre):
    ok, _ = screens.check("volume_anomaly", pre, {"min_vol_ratio": 4.0, "min_spike_vol_usd": 2000})
    return ok


# (name, setup dict, extra pre-entry predicate, min_cum_vol)
VARIANTS = [
    ("baseline (setup, vol 8k)", bt.DEFAULT_SETUP, None, VOL),
    ("+organic", bt.DEFAULT_SETUP, organic, VOL),
    ("+vol floor 40k", bt.DEFAULT_SETUP, None, 40_000),
    ("+vol floor 100k", bt.DEFAULT_SETUP, None, 100_000),
    ("+organic +vol 40k", bt.DEFAULT_SETUP, organic, 40_000),
    ("tighter base (range<=15, peak>=0.75)", tighter(max_base_range_pct=15, min_frac_of_peak=0.75), None, VOL),
    ("tighter + organic + vol 40k", tighter(max_base_range_pct=15, min_frac_of_peak=0.75), organic, 40_000),
    ("+two-sided flow + organic", bt.DEFAULT_SETUP, lambda p: two_sided(p) and organic(p), VOL),
    ("+volume-spike + organic", bt.DEFAULT_SETUP, lambda p: volspike(p) and organic(p), VOL),
]


def p_profit(m, paths=6000, hor=100, start=5.0, stake=0.25):
    rng = random.Random(1); win = 0
    for _ in range(paths):
        eq = start
        for _ in range(hor):
            if eq <= 1e-9:
                break
            eq = max(0.0, eq + min(stake, eq) * (m[rng.randrange(len(m))] - 1))
        win += eq >= start
    return 100 * win / paths


def main():
    toks = bt.load_token_index("reports/bd_tokens.json")
    cached = [c for t in toks if (c := bt.load_cached_candles(".bd_cache", t.pool))]
    print(f"honest sample: {len(cached)} tokens · fixed short-hold exit\n")
    print(f"{'entry variant':<42}{'n':>6}{'WR':>7}{'avg':>7}{'exp%':>8}{'P(profit)':>10}")
    hits = []
    for name, setup, pred, vol in VARIANTS:
        m = []
        for c in cached:
            entry_ts = c[0][0] + AGE * 60
            pre = [x for x in c if x[0] <= entry_ts]
            if len(pre) < 8 or sum(x[5] for x in pre) < vol:
                continue
            if pred is not None and not pred(pre):
                continue
            s = bt.simulate(c, EXIT, AGE, COST, vol, setup=setup)
            if s:
                m.append(s["multiple"])
        if len(m) < 20:
            print(f"{name:<42}{len(m):>6}  (too few)"); continue
        wr = 100 * sum(1 for x in m if x > 1) / len(m)
        avg = statistics.mean(m)
        pp = p_profit(m)
        flag = "  <-- clears bar" if avg > 1.0 and pp >= 50 else ""
        print(f"{name:<42}{len(m):>6}{wr:>6.1f}%{avg:>7.3f}{100*(avg-1):>+8.1f}{pp:>9.1f}%{flag}")
        if avg > 1.0 and pp >= 50:
            hits.append((name, avg, pp))
    print("\nGOAL: avg > 1.0 AND P(profit) >= 50%.")
    print("PROFITABLE ENTRIES:" , hits if hits else "NONE - no entry filter creates a real edge on this sample")


if __name__ == "__main__":
    main()
