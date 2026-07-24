#!/usr/bin/env python
"""Round-4: brutal-honest engine (v4) + scalp AND runner exit families.

Evidence forcing the tightening (rounds 3/3b):
  * +72h extension: only 71/892 union tokens ever print another candle after
    going silent inside the original 12h window.
  * liquidity snapshot (reports/bd_liquidity.json): 886/892 pools are drained
    to ~$0 today. Silent means rugged, not dormant.

Engine v4 rules (superset of v3's liquidity-integrity rule; locked anti-cheat
rules unchanged: sha1 split, both-halves target, 4% haircut, stop-before-TP,
survivor-free sample, trending excluded):

  * upside fills (TP arm/fill, hard TP, trail peak advance) - real candles
    (vol >= min_fill_vol) only, as in v3.
  * fixed stop: on a real candle fills at the stop level; on a dust candle
    fills at min(level, close) - a pool-drain candle pays its collapse price,
    not your limit price.
  * trailing stop: may trigger on dust candles too, at min(level, close).
  * time stop: pays the candle close if the candle is real; else the last
    real close if it is recent (<= 30 min old); else 0 (market already gone).
  * data_end remainder: 0 unless the pool is intact today per the liquidity
    snapshot (then last real close).

Usage: python sweep4.py [--cost-pct 4] [--min-fill-vol 500]
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
from sweep import FAIR_COHORTS, token_bucket, stats
from sweep3 import entry_features, passes

CACHE = ".bd_cache_ext"
LIQ_PATH = os.path.join("reports", "bd_liquidity.json")


def simulate4(candles, exits, entry_age_min, cost_pct, min_entry_vol,
              min_fill_vol, intact_end):
    created = candles[0][0]
    entry_ts = created + entry_age_min * 60
    pre = [c for c in candles if c[0] <= entry_ts]
    post = [c for c in candles if c[0] > entry_ts]
    if not pre or not post:
        return None
    entry = pre[-1][4]
    if entry <= 0 or sum(c[5] for c in pre) < min_entry_vol:
        return None
    if pre[-1][5] < min_fill_vol:
        return None

    stop_mult = 1 - exits["stop_loss_pct"] / 100
    tps = exits["take_profits"]
    hard = exits["hard_tp_multiple"]
    trail = exits["trailing_stop_pct"]
    max_hold = exits["max_hold_min"]

    remaining, received = 1.0, 0.0
    stage, peak = 0, 1.0
    reason = "data_end"
    last_real_close, last_real_ts = None, None
    for ts, _o, h, l, cl, v in post:
        age = (ts - entry_ts) / 60
        real = v >= min_fill_vol
        lo_m, hi_m, cl_m = l / entry, h / entry, cl / entry
        if stage == 0 and lo_m <= stop_mult:
            fill = stop_mult if real else min(stop_mult, cl_m)
            received += remaining * fill
            remaining, reason = 0.0, "stop_loss"
            break
        if stage > 0:
            level = peak * (1 - trail / 100)
            if lo_m <= level:
                fill = level if real else min(level, cl_m)
                received += remaining * fill
                remaining, reason = 0.0, "trailing_stop"
                break
        if real:
            while stage < len(tps) and hi_m >= float(tps[stage]["multiple"]):
                frac = float(tps[stage]["sell_fraction_of_remaining"])
                received += remaining * frac * float(tps[stage]["multiple"])
                remaining *= 1 - frac
                stage += 1
            if remaining <= 1e-12:
                reason = "take_profit"
                break
            if hi_m >= hard:
                received += remaining * hard
                remaining, reason = 0.0, "hard_take_profit"
                break
            peak = max(peak, hi_m)
            last_real_close, last_real_ts = cl_m, ts
        if age >= max_hold:
            if real:
                fill = cl_m
            elif last_real_ts is not None and ts - last_real_ts <= 1800:
                fill = last_real_close
            else:
                fill = 0.0
            received += remaining * fill
            remaining, reason = 0.0, "time_stop"
            break
    if remaining > 1e-12:
        fill = last_real_close if (intact_end and last_real_close is not None) else 0.0
        received += remaining * fill
    return {"multiple": received * (1 - cost_pct / 100), "reason": reason}


ENTRY_GRID = {
    "entry_age": [30.0, 45.0, 60.0],
    "min_pre_vol": [8000.0, 20000.0, 50000.0],
    "momentum": [(1.1, 4.0), (1.2, 3.0), (1.5, 6.0)],
    "min_pullback": [0.85, 0.9],
    # wash-ladder rejection: None = off, 1 = tolerate one pre-entry nuke
    # candle, 0 = zero tolerance (Kenny: avoid obvious scam charts)
    "max_nukes": [None, 1, 0],
}

SCALP_EXITS = [  # sell everything at tp; short holds beat the drain clock
    {"stop_loss_pct": s, "take_profits": [{"multiple": tp, "sell_fraction_of_remaining": 1.0}],
     "hard_tp_multiple": tp, "trailing_stop_pct": 99, "max_hold_min": hold}
    for s in (25, 40, 50)
    for tp in (1.3, 1.5, 2.0, 2.5, 3.0)
    for hold in (60.0, 120.0, 240.0, 480.0)
]

RUNNER_EXITS = [
    {"stop_loss_pct": s, "take_profits": [{"multiple": arm, "sell_fraction_of_remaining": 0.0}],
     "hard_tp_multiple": hard, "trailing_stop_pct": tr, "max_hold_min": hold}
    for s in (40, 50)
    for arm in (1.5, 2.0, 2.5, 3.0)
    for tr in (25, 30, 40, 50)
    for hard in (30.0, 100.0)
    for hold in (660.0, 1440.0, 4320.0)
]

HYBRID_EXITS = [  # bank a fraction early at tp1, trail the rest
    {"stop_loss_pct": s, "take_profits": [{"multiple": tp1, "sell_fraction_of_remaining": f1}],
     "hard_tp_multiple": 30.0, "trailing_stop_pct": tr, "max_hold_min": hold}
    for s in (40, 50)
    for tp1 in (1.5, 2.0)
    for f1 in (0.5, 0.75)
    for tr in (30, 40)
    for hold in (240.0, 480.0, 1440.0)
]


def exit_desc(x):
    tp = x["take_profits"][0]
    fam = ("scalp" if tp["sell_fraction_of_remaining"] == 1.0
           else "runner" if tp["sell_fraction_of_remaining"] == 0.0 else "hybrid")
    return (f"{fam} stop{x['stop_loss_pct']} tp{tp['multiple']}x/"
            f"{int(tp['sell_fraction_of_remaining'] * 100)}% trail{x['trailing_stop_pct']} "
            f"hard{x['hard_tp_multiple']:.0f} hold{x['max_hold_min']:.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Round-4 brutal-honest sweep")
    ap.add_argument("--cost-pct", type=float, default=4)
    ap.add_argument("--min-fill-vol", type=float, default=500.0)
    ap.add_argument("--min-train", type=int, default=100)
    ap.add_argument("--min-valid", type=int, default=100)
    ap.add_argument("--out", default=os.path.join("reports", "sweep4_results.json"))
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with open(LIQ_PATH, "r", encoding="utf-8") as f:
        liq = json.load(f)
    intact = {m for m, v in liq.items() if (v.get("liquidity") or 0) >= 1000}
    print(f"intact-today pools: {len(intact)}")

    tokens = backtest.load_token_index(os.path.join("reports", "bd_tokens.json"))
    ages = ENTRY_GRID["entry_age"]
    data = []
    for tok in tokens:
        if tok.cohort not in FAIR_COHORTS:
            continue
        candles = backtest.load_cached_candles(CACHE, tok.pool)
        if candles:
            feats = {age: entry_features(candles, age) for age in ages}
            data.append((tok, candles, token_bucket(tok.mint), feats,
                         tok.mint in intact))
    print(f"fair tokens with candles: {len(data)}")

    entry_combos = [dict(zip(ENTRY_GRID.keys(), v))
                    for v in itertools.product(*ENTRY_GRID.values())]
    exit_combos = SCALP_EXITS + RUNNER_EXITS + HYBRID_EXITS
    print(f"{len(entry_combos)} entries x {len(exit_combos)} exits "
          f"({len(SCALP_EXITS)} scalp / {len(RUNNER_EXITS)} runner / "
          f"{len(HYBRID_EXITS)} hybrid) = {len(entry_combos) * len(exit_combos)} combos")

    results = []
    for i, e in enumerate(entry_combos):
        usable = [(candles, bucket, ok) for tok, candles, bucket, feats, ok in data
                  if feats[e["entry_age"]] and passes(
                      feats[e["entry_age"]], e["min_pre_vol"], e["momentum"],
                      e["min_pullback"], None, None, e["max_nukes"])]
        if not usable:
            continue
        for exits in exit_combos:
            buckets = {"train": [], "valid": []}
            for candles, bucket, ok in usable:
                sim = simulate4(candles, exits, e["entry_age"], args.cost_pct,
                                e["min_pre_vol"], args.min_fill_vol, ok)
                if sim:
                    buckets[bucket].append(sim["multiple"])
            tr = stats(buckets["train"])
            if not tr or tr["n"] < args.min_train:
                continue
            results.append({
                "params": {**{k: (list(v) if isinstance(v, tuple) else v)
                              for k, v in e.items()}},
                "exits": exits,
                "train": tr, "valid": stats(buckets["valid"]),
            })
        if (i + 1) % 10 == 0:
            print(f"  entry {i + 1}/{len(entry_combos)}", flush=True)

    def hits(r):
        v = r["valid"]
        return (r["train"]["wr"] >= 60 and r["train"]["avg"] >= 2.0
                and v and v["n"] >= args.min_valid
                and v["wr"] >= 60 and v["avg"] >= 2.0)

    hitters = [r for r in results if hits(r)]

    def show(r):
        p, t, v = r["params"], r["train"], r["valid"]
        vtxt = f"valid n={v['n']} wr={v['wr']:.0f}% avg={v['avg']:.2f}x" if v else "valid none"
        nk = "any" if p.get("max_nukes") is None else str(p["max_nukes"])
        print(f"  age{p['entry_age']:.0f} vol{p['min_pre_vol']/1000:.0f}k "
              f"mom{p['momentum'][0]}-{p['momentum'][1]} pb{p['min_pullback']} nk{nk} | "
              f"{exit_desc(r['exits'])} | train n={t['n']} wr={t['wr']:.0f}% "
              f"avg={t['avg']:.2f}x | {vtxt}")

    print(f"\nscored: {len(results)}")
    print(f"\n== HITTERS (60% WR + 2.0x avg BOTH halves, engine v4): {len(hitters)} ==")
    for r in sorted(hitters, key=lambda r: -min(r["train"]["avg"], r["valid"]["avg"]))[:15]:
        show(r)

    ok_rows = [r for r in results if r["valid"] and r["valid"]["n"] >= args.min_valid
               and r["train"]["wr"] >= 60 and r["valid"]["wr"] >= 60]
    print("\n== best by min(train,valid) avg among WR>=60-both ==")
    for r in sorted(ok_rows, key=lambda r: -min(r["train"]["avg"], r["valid"]["avg"]))[:15]:
        show(r)

    print("\n== best by min(train,valid) avg at ANY WR ==")
    any_rows = [r for r in results if r["valid"] and r["valid"]["n"] >= args.min_valid]
    for r in sorted(any_rows, key=lambda r: -min(r["train"]["avg"], r["valid"]["avg"]))[:10]:
        show(r)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"engine": "v4", "min_fill_vol": args.min_fill_vol,
                   "n_scored": len(results), "hitters": hitters, "all": results},
                  fh, indent=1)
    print(f"\nfull results: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
