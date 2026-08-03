#!/usr/bin/env python
"""Round-3b: the sweep3 engine over the +72h extended cache (.bd_cache_ext).

Same locked rules (sha1 split, both-halves target, 4% haircut, stop-before-TP,
survivor-free sample) and the same v3 liquidity-integrity rule. What changes:

  * candles now run to listing+84h for the 892-token entry-filter union, so
    multi-day holds are honestly measurable instead of window-truncated;
  * hold grid extends to 4320 min; trail/hard widen accordingly;
  * finalists get an END-sensitivity report: remainder at last real close
    (baseline) vs remainder at ZERO for tokens silent through the extended
    window (the brutal-honesty floor).

Usage: python sweep3b.py [--cost-pct 4] [--min-fill-vol 500]
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
import mtest
from sweep import FAIR_COHORTS, token_bucket, stats
from sweep3 import entry_features, passes, simulate3, exits_from

CACHE = ".bd_cache_ext"

ENTRY_GRID = {
    # stays inside the fetched union bounds (vol>=8k, mom 1.1-6.0, pb>=0.85)
    "entry_age": [30.0, 45.0, 60.0],
    "min_pre_vol": [8000.0, 20000.0],
    "momentum": [(1.1, 4.0), (1.2, 3.0), (1.1, 6.0)],
    "min_pullback": [0.85, 0.9],
}

EXIT_GRID = {
    "stop": [40, 50],
    "arm": [1.5, 2.0, 2.5, 3.0],
    "trail": [25, 30, 40, 50],
    "hard": [30.0, 50.0, 100.0],
    "hold": [660.0, 1440.0, 2880.0, 4320.0],
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Round-3b sweep over extended cache")
    ap.add_argument("--cost-pct", type=float, default=4)
    ap.add_argument("--min-fill-vol", type=float, default=500.0)
    ap.add_argument("--min-train", type=int, default=100)
    ap.add_argument("--min-valid", type=int, default=100)
    ap.add_argument("--out", default=os.path.join("reports", "sweep3b_results.json"))
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    tokens = backtest.load_token_index(os.path.join("reports", "bd_tokens.json"))
    ages = ENTRY_GRID["entry_age"]
    data = []
    for tok in tokens:
        if tok.cohort not in FAIR_COHORTS:
            continue
        candles = backtest.load_cached_candles(CACHE, tok.pool)
        if candles:
            feats = {age: entry_features(candles, age) for age in ages}
            data.append((tok, candles, token_bucket(tok.mint), feats))
    print(f"fair tokens with candles: {len(data)}")

    entry_combos = [dict(zip(ENTRY_GRID.keys(), v))
                    for v in itertools.product(*ENTRY_GRID.values())]
    exit_combos = list(itertools.product(*EXIT_GRID.values()))
    print(f"{len(entry_combos)} entries x {len(exit_combos)} exits = "
          f"{len(entry_combos) * len(exit_combos)} combos "
          f"(both-halves rule is the noise guard)")

    results = []
    for i, e in enumerate(entry_combos):
        usable = [(tok, candles, bucket) for tok, candles, bucket, feats in data
                  if feats[e["entry_age"]] and passes(
                      feats[e["entry_age"]], e["min_pre_vol"], e["momentum"],
                      e["min_pullback"], None, None)]
        if not usable:
            continue
        for stop, arm, trail, hard, hold in exit_combos:
            exits = exits_from(stop, arm, trail, hard, hold)
            buckets = {"train": [], "valid": []}
            for tok, candles, bucket in usable:
                sim = simulate3(candles, exits, e["entry_age"], args.cost_pct,
                                e["min_pre_vol"], args.min_fill_vol)
                if sim:
                    buckets[bucket].append((sim["multiple"], sim["end_credit"]))
            tr = stats([m for m, _ in buckets["train"]])
            if not tr or tr["n"] < args.min_train:
                continue
            tr["avg_zero_end"] = round(statistics.mean(
                [m - ec for m, ec in buckets["train"]]), 3)
            va = stats([m for m, _ in buckets["valid"]])
            if va:
                va["avg_zero_end"] = round(statistics.mean(
                    [m - ec for m, ec in buckets["valid"]]), 3)
            results.append({
                "params": {**{k: (list(v) if isinstance(v, tuple) else v)
                              for k, v in e.items()},
                           "stop": stop, "arm": arm, "trail": trail,
                           "hard": hard, "hold": hold},
                "train": tr, "valid": va,
            })
        print(f"  entry {i + 1}/{len(entry_combos)} done ({len(usable)} usable)", flush=True)

    def hits(r):
        v = r["valid"]
        return (r["train"]["wr"] >= 60 and r["train"]["avg"] >= 2.0
                and v and v["n"] >= args.min_valid
                and v["wr"] >= 60 and v["avg"] >= 2.0)

    hitters = [r for r in results if hits(r)]

    def show(r):
        p, t, v = r["params"], r["train"], r["valid"]
        vtxt = (f"valid n={v['n']} wr={v['wr']:.0f}% avg={v['avg']:.2f}x"
                f" (zero-END {v.get('avg_zero_end', 0):.2f}x)" if v else "valid none")
        print(f"  age{p['entry_age']:.0f} vol{p['min_pre_vol']/1000:.0f}k "
              f"mom{p['momentum'][0]}-{p['momentum'][1]} pb{p['min_pullback']} | "
              f"stop{p['stop']} arm{p['arm']} trail{p['trail']} hard{p['hard']:.0f} "
              f"hold{p['hold']:.0f} | train n={t['n']} wr={t['wr']:.0f}% "
              f"avg={t['avg']:.2f}x (z{t.get('avg_zero_end', 0):.2f}) | {vtxt}")

    print(f"\nscored: {len(results)}")
    print(f"\n== HITTERS (60% WR + 2.0x avg BOTH halves, v3 engine, extended data): "
          f"{len(hitters)} ==")
    for r in sorted(hitters, key=lambda r: -min(r["train"]["avg"], r["valid"]["avg"]))[:15]:
        show(r)

    ok = [r for r in results if r["valid"] and r["valid"]["n"] >= args.min_valid
          and r["train"]["wr"] >= 60 and r["valid"]["wr"] >= 60]
    print("\n== best by min(train,valid) avg among WR>=60-both ==")
    for r in sorted(ok, key=lambda r: -min(r["train"]["avg"], r["valid"]["avg"]))[:15]:
        show(r)

    haircut = mtest.print_haircut(results, half="train", min_n=args.min_train,
                                  attempted=len(entry_combos) * len(exit_combos))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"engine": "v3+ext", "min_fill_vol": args.min_fill_vol,
                   "n_scored": len(results), "mtest": haircut,
                   "hitters": hitters, "all": results},
                  fh, indent=1)
    print(f"\nfull results: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
