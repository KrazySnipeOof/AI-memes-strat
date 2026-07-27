#!/usr/bin/env python
"""Sweep exit doctrines for the BASE strategy on the current (cleaned, honest)
sample to find one that is actually profitable with >=50% of Monte-Carlo paths
ending in profit. Entry = the validated base-entry setup filter (unchanged);
only the EXIT is varied. Reports WR / avg / expectancy / P(loss) per variant.
"""
from __future__ import annotations

import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import backtest as bt

ENTRY_AGE, COST, MIN_VOL = 30.0, 4.0, 8000.0


def V(stop, tps, trail, hard, hold):
    return {"stop_loss_pct": stop, "take_profits": tps, "trailing_stop_pct": trail,
            "hard_tp_multiple": hard, "max_hold_min": hold}


def tp(m, f):
    return {"multiple": m, "sell_fraction_of_remaining": f}


VARIANTS = {
    "current (bank30@1.35 trail30)": V(40, [tp(1.35, 0.3)], 30, 20, 480),
    "lock-fast (bank60@1.3+40@1.8 trail18)": V(35, [tp(1.3, 0.6), tp(1.8, 0.4)], 18, 10, 300),
    "scalp (bank75@1.15+50@1.5 trail15)": V(30, [tp(1.15, 0.75), tp(1.5, 0.5)], 15, 6, 240),
    "very-tight (bank85@1.1 trail12)": V(25, [tp(1.1, 0.85)], 12, 4, 180),
    "cutfast-run (stop25 bank50@1.4 trail20 hard15)": V(25, [tp(1.4, 0.5)], 20, 15, 300),
    "runner (arm@2 trail35 hard30)": V(45, [tp(2.0, 0.0)], 35, 30, 480),
    "balanced (bank50@1.25+50@1.7 trail20)": V(32, [tp(1.25, 0.5), tp(1.7, 0.5)], 20, 12, 300),
}


def p_loss(mults, paths=8000, horizon=100, start=5.0, stake=0.25, seed=1):
    """IID-bootstrap fraction of equity paths ending below the start bankroll."""
    rng = random.Random(seed)
    lose = 0
    for _ in range(paths):
        eq = start
        for _ in range(horizon):
            if eq <= 1e-9:
                break
            m = mults[rng.randrange(len(mults))]
            eq = max(0.0, eq + min(stake, eq) * (m - 1.0))
        lose += eq < start
    return lose / paths


def main():
    tokens = bt.load_token_index("reports/bd_tokens.json")
    cached = []
    for t in tokens:
        c = bt.load_cached_candles(".bd_cache", t.pool)
        if c:
            cached.append(c)
    print(f"sample: {len(cached)} tokens with candles\n")
    print(f"{'exit variant':<48}{'n':>6}{'WR':>7}{'avg':>7}{'exp%':>8}{'P(loss)':>9}{'P(profit)':>10}")
    rows = []
    for name, ex in VARIANTS.items():
        mults = []
        for c in cached:
            sim = bt.simulate(c, ex, ENTRY_AGE, COST, MIN_VOL, setup=bt.DEFAULT_SETUP)
            if sim:
                mults.append(sim["multiple"])
        if not mults:
            print(f"{name:<48}  no trades"); continue
        wr = 100 * sum(1 for x in mults if x > 1) / len(mults)
        avg = statistics.mean(mults)
        pl = p_loss(mults)
        rows.append((name, len(mults), wr, avg, pl))
        print(f"{name:<48}{len(mults):>6}{wr:>6.1f}%{avg:>7.3f}{100*(avg-1):>+8.1f}{100*pl:>8.1f}%{100*(1-pl):>9.1f}%")
    print("\nGOAL: avg > 1.0 (profitable) AND P(profit) >= 50%.")
    ok = [r for r in rows if r[3] > 1.0 and (1 - r[4]) >= 0.5]
    if ok:
        best = max(ok, key=lambda r: r[3])
        print(f"BEST PROFITABLE: {best[0]}  (avg {best[3]:.3f}x, P(profit) {100*(1-best[4]):.1f}%)")
    else:
        best = max(rows, key=lambda r: r[3])
        print(f"NONE clears the bar on the honest sample. Least-bad: {best[0]} "
              f"(avg {best[3]:.3f}x, P(profit) {100*(1-best[4]):.1f}%)")


if __name__ == "__main__":
    main()
