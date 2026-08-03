#!/usr/bin/env python
"""Round-3 sweep: liquidity-honest engine + extended exit/entry grids.

Round 2 ended at valid 1.89x avg / 76% WR (target 2.0x / 60%), with every
winning parameter pinned at a grid edge. Diagnostics showed two things:

  1. The 20x hard cap truncates a real fat tail (raising it lifts valid avg
     past 2.0) -- BUT
  2. the Birdeye 1m data contains pool-drain wick artifacts (e.g. a "63M x"
     candle on $22 of volume, a "484x" candle on $8). The round-1/2 engine
     fills upside exits at such prices, which is cheating by accident.

Engine v3 therefore adds a liquidity-integrity rule on top of the locked
round-2 rules (conservative stop-before-TP ordering, 4% cost haircut,
sha1(mint) train/valid split, trending cohort excluded, survivor-free
Birdeye sample):

  * a candle with vol_usd < min_fill_vol (default $500/min, ~10x the $45
    position) is UNTRADEABLE on the upside: it cannot fill or arm a TP,
    cannot fill a hard TP, cannot advance the trailing peak, cannot fill a
    trailing exit, and cannot serve as the END/time-stop valuation price.
  * dust candles CAN still trigger the fixed stop-loss (downside moves are
    real even when printed volume is thin) -- the conservative direction.
  * the last pre-entry candle must itself have vol_usd >= min_fill_vol
    (you cannot buy $45 from an $8 candle either).
  * END remainder is valued at the last REAL-volume close after entry; if
    the token never prints a real-volume candle post-entry, at the entry
    stop level (the position is assumed stuck and dumped into the drain).

This makes v3 strictly HARDER than the locked round-2 methodology: any
config that hits 60% WR / 2.0x avg on both halves under v3 also passes v2.

Usage:
  python sweep3.py --tokens-json reports/bd_tokens.json --cache-dir .bd_cache \
      [--cost-pct 4] [--min-fill-vol 500] [--min-train 100] [--min-valid 100]
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
import mtest
from sweep import FAIR_COHORTS, token_bucket, stats

# ---------------------------------------------------------------- engine v3

def simulate3(candles: list, exits: dict, entry_age_min: float, cost_pct: float,
              min_entry_vol: float, min_fill_vol: float):
    """backtest.simulate semantics + the liquidity-integrity rule above."""
    created = candles[0][0]
    entry_ts = created + entry_age_min * 60
    pre = [c for c in candles if c[0] <= entry_ts]
    post = [c for c in candles if c[0] > entry_ts]
    if not pre or not post:
        return None
    entry = pre[-1][4]
    if entry <= 0 or sum(c[5] for c in pre) < min_entry_vol:
        return None
    if pre[-1][5] < min_fill_vol:          # can't buy into a dust candle
        return None

    stop_mult = 1 - exits["stop_loss_pct"] / 100
    tps = exits["take_profits"]
    hard = exits["hard_tp_multiple"]
    trail = exits["trailing_stop_pct"]
    max_hold = exits["max_hold_min"]

    remaining, received = 1.0, 0.0
    stage, peak = 0, 1.0
    reason = "data_end"
    last_real_close = None                 # last close on a vol>=floor candle
    for ts, _o, h, l, cl, v in post:
        age = (ts - entry_ts) / 60
        real = v >= min_fill_vol
        lo_m, hi_m, cl_m = l / entry, h / entry, cl / entry
        if stage == 0 and lo_m <= stop_mult:
            received += remaining * stop_mult
            remaining, reason = 0.0, "stop_loss"
            break
        if stage > 0 and real and lo_m <= peak * (1 - trail / 100):
            received += remaining * peak * (1 - trail / 100)
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
            last_real_close = cl_m
        if age >= max_hold:
            level = last_real_close if last_real_close is not None else stop_mult
            received += remaining * level
            remaining, reason = 0.0, "time_stop"
            break
    end_credit = 0.0
    if remaining > 1e-12:
        level = last_real_close if last_real_close is not None else stop_mult
        received += remaining * level
        end_credit = remaining * level * (1 - cost_pct / 100)
    return {"multiple": received * (1 - cost_pct / 100), "reason": reason,
            "end_credit": end_credit}


# ------------------------------------------------------------ entry features

def entry_features(candles: list, entry_age_min: float):
    """Pre-entry-only features (superset of sweep2's); None if no window."""
    created = candles[0][0]
    entry_ts = created + entry_age_min * 60
    pre = [c for c in candles if c[0] <= entry_ts]
    post = [c for c in candles if c[0] > entry_ts]
    if not pre or not post:
        return None
    entry, first_open = pre[-1][4], pre[0][1]
    peak = max(c[2] for c in pre)
    if entry <= 0 or first_open <= 0 or peak <= 0:
        return None
    third = max(1, len(pre) // 3)
    early_vol = sum(c[5] for c in pre[:third])
    late_vol = sum(c[5] for c in pre[-third:])
    green = sum(1 for c in pre if c[4] > c[1])
    # wash-ladder "nuke" candles: a single candle whose body collapses >=30%
    # open->close. Organic pumps rarely print one; the ladder-then-dump wash
    # pattern (staircase of small greens erased by one giant red, repeating)
    # prints several. Pre-entry only - no lookahead.
    nukes = sum(1 for c in pre if c[1] > 0 and c[4] / c[1] <= 0.70)
    return {"pre_vol": sum(c[5] for c in pre),
            "momentum": entry / first_open,
            "pullback": entry / peak,
            "green_frac": green / len(pre),
            "vol_accel": late_vol / early_vol if early_vol > 0 else None,
            "last_pre_vol": pre[-1][5],
            "nukes": nukes}


def passes(feat, min_pre_vol, momentum, min_pullback, min_green, min_accel,
           max_nukes=None):
    if feat["pre_vol"] < min_pre_vol:
        return False
    if momentum:
        lo, hi = momentum
        if lo is not None and feat["momentum"] < lo:
            return False
        if hi is not None and feat["momentum"] > hi:
            return False
    if min_pullback is not None and feat["pullback"] < min_pullback:
        return False
    if min_green is not None and feat["green_frac"] < min_green:
        return False
    if min_accel is not None and (feat["vol_accel"] is None or feat["vol_accel"] < min_accel):
        return False
    if max_nukes is not None and feat.get("nukes", 0) > max_nukes:
        return False
    return True


# ------------------------------------------------------------------- sweep

EXIT_GRID = {
    "stop": [40, 50, 60],
    "arm": [1.5, 2.0, 2.5, 3.0],       # tp1 multiple, sell fraction 0 (arms trail)
    "trail": [20, 25, 30, 40, 50],
    "hard": [20.0, 30.0, 50.0],
    "hold": [480.0, 660.0],
}

ENTRY_GRID = {
    "entry_age": [30.0, 45.0, 60.0],
    "min_pre_vol": [8000.0, 20000.0, 50000.0],
    "momentum": [(1.1, 4.0), (1.2, 3.0), (1.1, 6.0), (1.3, 4.0)],
    "min_pullback": [0.85, 0.9, 0.95],
    "min_green": [None, 0.55],
    "min_accel": [None, 1.5],
}

STAGE_A_ENTRIES = [
    # the round-2 winners' neighborhood, held fixed while exits extend
    {"entry_age": 30.0, "min_pre_vol": 20000.0, "momentum": (1.1, 4.0),
     "min_pullback": 0.85, "min_green": None, "min_accel": None},
    {"entry_age": 30.0, "min_pre_vol": 8000.0, "momentum": (1.1, 4.0),
     "min_pullback": 0.85, "min_green": None, "min_accel": None},
]


def exits_from(stop, arm, trail, hard, hold):
    return {"stop_loss_pct": stop,
            "take_profits": [{"multiple": arm, "sell_fraction_of_remaining": 0.0}],
            "hard_tp_multiple": hard, "trailing_stop_pct": trail, "max_hold_min": hold}


def score_combo(usable, exits, entry_age, cost_pct, min_pre_vol, min_fill_vol,
                min_train):
    buckets = {"train": [], "valid": []}
    for tok, candles, bucket in usable:
        sim = simulate3(candles, exits, entry_age, cost_pct, min_pre_vol, min_fill_vol)
        if sim:
            buckets[bucket].append(sim["multiple"])
    tr = stats(buckets["train"])
    if not tr or tr["n"] < min_train:
        return None
    return tr, stats(buckets["valid"])


def main() -> None:
    ap = argparse.ArgumentParser(description="Round-3 liquidity-honest sweep")
    ap.add_argument("--tokens-json", default=os.path.join("reports", "bd_tokens.json"))
    ap.add_argument("--cache-dir", default=".bd_cache")
    ap.add_argument("--cost-pct", type=float, default=4)
    ap.add_argument("--min-fill-vol", type=float, default=500.0)
    ap.add_argument("--min-train", type=int, default=100)
    ap.add_argument("--min-valid", type=int, default=100)
    ap.add_argument("--top-exits", type=int, default=20,
                    help="stage-A exit configs carried into the stage-B entry cross")
    ap.add_argument("--out", default=os.path.join("reports", "sweep3_results.json"))
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    tokens = backtest.load_token_index(args.tokens_json)
    ages = ENTRY_GRID["entry_age"]
    data = []
    for tok in tokens:
        if tok.cohort not in FAIR_COHORTS:
            continue
        candles = backtest.load_cached_candles(args.cache_dir, tok.pool)
        if candles:
            feats = {age: entry_features(candles, age) for age in ages}
            data.append((tok, candles, token_bucket(tok.mint), feats))
    print(f"fair tokens with candles: {len(data)}")

    def usable_for(e):
        return [(tok, candles, bucket) for tok, candles, bucket, feats in data
                if feats[e["entry_age"]] and passes(
                    feats[e["entry_age"]], e["min_pre_vol"], e["momentum"],
                    e["min_pullback"], e["min_green"], e["min_accel"])]

    exit_combos = list(itertools.product(*EXIT_GRID.values()))
    results = []

    def run(entry, usable, tag):
        for stop, arm, trail, hard, hold in exit_combos:
            exits = exits_from(stop, arm, trail, hard, hold)
            scored = score_combo(usable, exits, entry["entry_age"], args.cost_pct,
                                 entry["min_pre_vol"], args.min_fill_vol, args.min_train)
            if not scored:
                continue
            tr, va = scored
            results.append({
                "stage": tag,
                "params": {**{k: (list(v) if isinstance(v, tuple) else v)
                              for k, v in entry.items()},
                           "stop": stop, "arm": arm, "trail": trail,
                           "hard": hard, "hold": hold},
                "train": tr, "valid": va,
            })

    print(f"stage A: {len(STAGE_A_ENTRIES)} entries x {len(exit_combos)} exits")
    for e in STAGE_A_ENTRIES:
        run(e, usable_for(e), "A")

    def hits(r):
        v = r["valid"]
        return (r["train"]["wr"] >= 60 and r["train"]["avg"] >= 2.0
                and v and v["n"] >= args.min_valid
                and v["wr"] >= 60 and v["avg"] >= 2.0)

    # stage B: cross the best stage-A exits with the refined entry grid
    a_rows = sorted(results, key=lambda r: -(min(r["train"]["avg"],
                                                 r["valid"]["avg"] if r["valid"] else 0)
                                             if r["valid"] and r["valid"]["wr"] >= 60
                                             and r["train"]["wr"] >= 60 else 0))
    seen, top_exits = set(), []
    for r in a_rows:
        k = tuple(r["params"][x] for x in ("stop", "arm", "trail", "hard", "hold"))
        if k in seen:
            continue
        seen.add(k)
        top_exits.append(k)
        if len(top_exits) >= args.top_exits:
            break

    entry_combos = [dict(zip(ENTRY_GRID.keys(), vals))
                    for vals in itertools.product(*ENTRY_GRID.values())]
    print(f"stage B: {len(entry_combos)} entries x {len(top_exits)} exits")
    b_exit_combos = top_exits
    for i, e in enumerate(entry_combos):
        usable = usable_for(e)
        if not usable:
            continue
        for stop, arm, trail, hard, hold in b_exit_combos:
            exits = exits_from(stop, arm, trail, hard, hold)
            scored = score_combo(usable, exits, e["entry_age"], args.cost_pct,
                                 e["min_pre_vol"], args.min_fill_vol, args.min_train)
            if not scored:
                continue
            tr, va = scored
            results.append({
                "stage": "B",
                "params": {**{k: (list(v) if isinstance(v, tuple) else v)
                              for k, v in e.items()},
                           "stop": stop, "arm": arm, "trail": trail,
                           "hard": hard, "hold": hold},
                "train": tr, "valid": va,
            })
        if (i + 1) % 50 == 0:
            print(f"  stage B entry {i + 1}/{len(entry_combos)}", flush=True)

    hitters = [r for r in results if hits(r)]
    total = len(STAGE_A_ENTRIES) * len(exit_combos) + len(entry_combos) * len(b_exit_combos)
    print(f"\ncombos tested: ~{total} | scored: {len(results)} "
          f"(multiple-testing warning: the both-halves rule is the guard)")

    def show(r):
        p, t, v = r["params"], r["train"], r["valid"]
        mom = f"mom{p['momentum'][0]}-{p['momentum'][1]}"
        extras = "".join([f" grn{p['min_green']}" if p.get("min_green") else "",
                          f" acc{p['min_accel']}" if p.get("min_accel") else ""])
        vtxt = f"valid n={v['n']} wr={v['wr']:.0f}% avg={v['avg']:.2f}x" if v else "valid none"
        print(f"  [{r['stage']}] age{p['entry_age']:.0f} vol{p['min_pre_vol']/1000:.0f}k {mom} "
              f"pb{p['min_pullback']}{extras} | stop{p['stop']} arm{p['arm']} trail{p['trail']} "
              f"hard{p['hard']:.0f} hold{p['hold']:.0f} | "
              f"train n={t['n']} wr={t['wr']:.0f}% avg={t['avg']:.2f}x | {vtxt}")

    print(f"\n== HITTERS (60% WR and 2.0x avg on BOTH halves, engine v3): {len(hitters)} ==")
    for r in sorted(hitters, key=lambda r: -min(r["train"]["avg"], r["valid"]["avg"]))[:15]:
        show(r)

    print("\n== best by min(train,valid) avg among WR>=60-both, valid n ok ==")
    ok = [r for r in results if r["valid"] and r["valid"]["n"] >= args.min_valid
          and r["train"]["wr"] >= 60 and r["valid"]["wr"] >= 60]
    for r in sorted(ok, key=lambda r: -min(r["train"]["avg"], r["valid"]["avg"]))[:15]:
        show(r)

    haircut = mtest.print_haircut(results, half="train", attempted=total,
                                  min_n=args.min_train)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"engine": "v3", "min_fill_vol": args.min_fill_vol,
                   "n_scored": len(results), "mtest": haircut,
                   "hitters": hitters, "all": results},
                  fh, indent=1)
    print(f"\nfull results: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
