#!/usr/bin/env python
"""Entry-side selection sweep (round 2 of the honest optimization).

Round 1 (sweep.py) tuned exits only: zero combos hit 60% WR + 2.0x avg on both
train and validation halves. This sweep adds the one honest lever that CAN be
reconstructed from OHLCV history: entry-side selection. Every entry feature is
computed strictly from candles at or before entry - no lookahead:

  entry_age  - minutes after pool creation to enter
  pre_vol    - USD volume traded before entry (min threshold)
  momentum   - entry price / first candle open (is the token already moving)
  pullback   - entry price / pre-entry peak high (avoid entering into a dump)

Exit configs are not re-searched from scratch: the top exit configs from
reports/sweep_results.json (hitters + Pareto frontier + top-by-wr + top-by-avg,
deduped, capped at --max-exits) are crossed with the entry grid, plus the
asym-runner baseline. Anti-cheat rules identical to sweep.py:

  * trending cohort EXCLUDED from the objective (survivor bias) - reference only
  * deterministic train/valid split by sha1(mint); a config counts only if it
    clears the target on BOTH halves (valid n >= 10)
  * same conservative candle rule (stop before TP) via backtest.simulate
  * same round-trip cost haircut
  * combo count printed - entry filters shrink samples, which makes
    best-of-N noise mining EASIER; the validation column is the one to trust

Usage: python sweep2.py [--cost-pct 4] [--min-train 15] [--max-exits 40]
Requires .ohlcv_cache populated and reports/sweep_results.json from sweep.py.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
from bot.config import Config
from sweep import FAIR_COHORTS, token_bucket, stats, load_cached_candles, load_tokens

ENTRY_GRID = {
    "entry_age": [10.0, 20.0, 30.0, 45.0],
    "min_pre_vol": [2000.0, 8000.0, 20000.0],
    "momentum": [None, (1.1, None), (1.5, None), (1.1, 4.0)],  # (min, max) on entry/first_open
    "min_pullback": [None, 0.7, 0.85],                          # entry / pre-entry peak high
}


def entry_features(candles: list, entry_age_min: float):
    """Pre-entry-only features; None if the token has no usable entry window."""
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
    return {"pre_vol": sum(c[5] for c in pre),
            "momentum": entry / first_open,
            "pullback": entry / peak}


def passes(feat: dict, min_pre_vol: float, momentum, min_pullback) -> bool:
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
    return True


def exits_from_params(p: dict) -> dict:
    tps = [{"multiple": p["tp1"][0], "sell_fraction_of_remaining": p["tp1"][1]}]
    if p.get("tp2"):
        tps.append({"multiple": p["tp2"][0], "sell_fraction_of_remaining": p["tp2"][1]})
    return {"stop_loss_pct": p["stop"], "take_profits": tps,
            "hard_tp_multiple": p["hard"], "trailing_stop_pct": p["trail"],
            "max_hold_min": p["hold"]}


def exit_key(p: dict):
    return (p["stop"], tuple(p["tp1"]), tuple(p["tp2"]) if p.get("tp2") else None,
            p["trail"], p["hard"], p["hold"])


def pick_exit_configs(path: str, max_exits: int) -> list:
    """Hitters, then Pareto frontier, then top-by-wr / top-by-avg from the round-2
    exit sweep, deduped on exit params (entry_age is re-swept here)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    rows = data["all"]
    frontier, best_avg = [], -1.0
    for r in sorted(rows, key=lambda r: (-r["train"]["wr"], -r["train"]["avg"])):
        if r["train"]["avg"] > best_avg:
            frontier.append(r)
            best_avg = r["train"]["avg"]
    ranked = (data.get("hitters", []) + frontier
              + sorted(rows, key=lambda r: -r["train"]["wr"])[:15]
              + sorted(rows, key=lambda r: -r["train"]["avg"])[:15])
    picked, seen = [], set()
    # asym-runner baseline always included
    asym = {"stop": 30, "tp1": [1.35, 0.5], "tp2": None, "trail": 35, "hard": 12.0, "hold": 480.0}
    for p in [asym] + [r["params"] for r in ranked]:
        k = exit_key(p)
        if k in seen:
            continue
        seen.add(k)
        picked.append(p)
        if len(picked) >= max_exits:
            break
    return picked


def main() -> None:
    ap = argparse.ArgumentParser(description="Entry-side selection sweep over cached backtest data")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--cost-pct", type=float, default=4)
    ap.add_argument("--min-train", type=int, default=15, help="min train-set trades to score a combo")
    ap.add_argument("--min-valid", type=int, default=10, help="min validation trades for a combo to count as hitting")
    ap.add_argument("--tokens-json", default="", help="token index file (e.g. reports/bd_tokens.json) instead of live lists")
    ap.add_argument("--cache-dir", default="", help="candle cache dir (default: backtest's .ohlcv_cache)")
    ap.add_argument("--max-exits", type=int, default=40)
    ap.add_argument("--exit-results", default=os.path.join("reports", "sweep_results.json"))
    ap.add_argument("--out", default=os.path.join("reports", "sweep2_results.json"))
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config.load(args.config)
    cache_dir = args.cache_dir or backtest.CACHE_DIR

    exit_params = pick_exit_configs(args.exit_results, args.max_exits)
    print(f"exit configs carried into entry sweep: {len(exit_params)}")

    tokens = load_tokens(args, cfg)
    data = []
    for tok in tokens:
        candles = load_cached_candles(cache_dir, tok.pool)
        if candles:
            feats = {age: entry_features(candles, age) for age in ENTRY_GRID["entry_age"]}
            data.append((tok, candles, token_bucket(tok.mint), feats))
    fair_train = sum(1 for t, _, b, _f in data if t.cohort in FAIR_COHORTS and b == "train")
    fair_valid = sum(1 for t, _, b, _f in data if t.cohort in FAIR_COHORTS and b == "valid")
    print(f"tokens with cached history: {len(data)} | fair train={fair_train} valid={fair_valid}")

    entry_combos = list(itertools.product(*ENTRY_GRID.values()))
    total = len(entry_combos) * len(exit_params)
    print(f"testing {total} combos ({len(entry_combos)} entry x {len(exit_params)} exit) "
          f"(multiple-testing warning: entry filters shrink n, making noise-mining easier; "
          f"the validation column is the one to trust)")

    results = []
    for age, min_vol, momentum, min_pb in entry_combos:
        usable = [(tok, candles, bucket) for tok, candles, bucket, feats in data
                  if feats[age] and passes(feats[age], min_vol, momentum, min_pb)]
        if not usable:
            continue
        for p in exit_params:
            exits = exits_from_params(p)
            buckets = {"train": [], "valid": [], "trending": []}
            for tok, candles, bucket in usable:
                sim = backtest.simulate(candles, exits, age, args.cost_pct, min_vol)
                if not sim:
                    continue
                if tok.cohort in FAIR_COHORTS:
                    buckets[bucket].append(sim["multiple"])
                else:
                    buckets["trending"].append(sim["multiple"])
            tr = stats(buckets["train"])
            if not tr or tr["n"] < args.min_train:
                continue
            results.append({
                "params": {"entry_age": age, "min_pre_vol": min_vol,
                           "momentum": list(momentum) if momentum else None,
                           "min_pullback": min_pb,
                           **{k: p[k] for k in ("stop", "tp1", "tp2", "trail", "hard", "hold")}},
                "train": tr,
                "valid": stats(buckets["valid"]),
                "trending_ref": stats(buckets["trending"]),
            })

    def hits(r):
        v = r["valid"]
        return (r["train"]["wr"] >= 60 and r["train"]["avg"] >= 2.0
                and v and v["n"] >= args.min_valid and v["wr"] >= 60 and v["avg"] >= 2.0)

    hitters = [r for r in results if hits(r)]
    results.sort(key=lambda r: -(min(r["train"]["wr"], 60) * 10 + 100 * r["train"]["avg"]))

    def show(r):
        p = r["params"]
        mom = f" mom{p['momentum'][0]}-{p['momentum'][1] or '∞'}" if p["momentum"] else ""
        pb = f" pb{p['min_pullback']}" if p["min_pullback"] is not None else ""
        tp2 = f" tp2 {p['tp2'][0]}x/{int(p['tp2'][1] * 100)}%" if p["tp2"] else ""
        v = r["valid"]
        vtxt = (f"valid n={v['n']} wr={v['wr']:.0f}% avg={v['avg']:.2f}x" if v else "valid: none")
        print(f"  age{p['entry_age']:.0f} vol{p['min_pre_vol']:.0f}{mom}{pb} | "
              f"stop{p['stop']} tp1 {p['tp1'][0]}x/{int(p['tp1'][1] * 100)}%{tp2} "
              f"trail{p['trail']} hard{p['hard']:.0f} | "
              f"train n={r['train']['n']} wr={r['train']['wr']:.0f}% avg={r['train']['avg']:.2f}x | {vtxt}")

    print(f"\nscored combos: {len(results)}")
    print(f"\n== combos hitting 60% WR and 2.0x avg on BOTH halves (fair cohorts): {len(hitters)} ==")
    for r in hitters[:10]:
        show(r)

    print("\n== train-set winrate/avg Pareto frontier ==")
    best_avg = -1.0
    shown = 0
    for r in sorted(results, key=lambda r: (-r["train"]["wr"], -r["train"]["avg"])):
        if r["train"]["avg"] > best_avg:
            show(r)
            best_avg = r["train"]["avg"]
            shown += 1
            if shown >= 12:
                break

    print("\n== top 10 by train avg multiple ==")
    for r in sorted(results, key=lambda r: -r["train"]["avg"])[:10]:
        show(r)

    print("\n== top 10 by train winrate ==")
    for r in sorted(results, key=lambda r: -r["train"]["wr"])[:10]:
        show(r)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"n_combos": total, "n_scored": len(results),
                   "hitters": hitters, "all": results}, fh, indent=1)
    print(f"\nfull results: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
