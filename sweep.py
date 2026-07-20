#!/usr/bin/env python
"""Exit-parameter sweep over the backtester's cached OHLCV data.

Goal: find exit configs that maximize winrate and average multiple WITHOUT
cheating. Anti-cheat rules enforced here:

  * trending cohort is EXCLUDED from the optimization objective (survivor
    bias: those tokens are on the leaderboard because they pumped). Its
    stats are shown for reference only.
  * tokens are split into train/validation halves deterministically by mint
    hash; configs are ranked on train and must hold up on validation.
  * same conservative candle rule as backtest.simulate (stop before TP) and
    the same round-trip cost haircut.
  * the number of combos tested is printed - judge significance accordingly.

Usage: python sweep.py [--cost-pct 4] [--min-train 20]
Requires a prior `python backtest.py` run to have populated .ohlcv_cache.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
from bot.config import Config

FAIR_COHORTS = {"db", "pump"}


def token_bucket(mint: str) -> str:
    return "train" if hashlib.sha1(mint.encode()).digest()[0] % 2 == 0 else "valid"


def stats(mults):
    if not mults:
        return None
    wins = sum(1 for m in mults if m > 1.0)
    return {
        "n": len(mults),
        "wr": 100.0 * wins / len(mults),
        "avg": statistics.mean(mults),
        "median": statistics.median(mults),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Sweep exit params over cached backtest data")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--cost-pct", type=float, default=4)
    ap.add_argument("--min-entry-vol", type=float, default=2000)
    ap.add_argument("--min-train", type=int, default=20, help="min train-set trades to score a combo")
    ap.add_argument("--out", default=os.path.join("reports", "sweep_results.json"))
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config.load(args.config)
    session = backtest.plain_session()

    print("collecting token lists (for cohort labels; candles come from cache)...")
    tokens = backtest.collect_tokens(session, 200, cfg.db_path, 80)
    data = []  # (token, candles, bucket)
    for tok in tokens:
        cpath = os.path.join(backtest.CACHE_DIR, f"{tok.pool}.json")
        if not os.path.exists(cpath):
            continue  # cache-only: never spend rate budget inside the sweep
        candles, _ = backtest.fetch_candles(session, tok)
        if candles:
            data.append((tok, candles, token_bucket(tok.mint)))

    by = {}
    for tok, _, bucket in data:
        key = (tok.cohort, bucket)
        by[key] = by.get(key, 0) + 1
    print(f"tokens with cached history: {len(data)} | breakdown {by}")
    fair_train = sum(1 for t, _, b in data if t.cohort in FAIR_COHORTS and b == "train")
    fair_valid = sum(1 for t, _, b in data if t.cohort in FAIR_COHORTS and b == "valid")
    print(f"fair (db+pump) tokens: train={fair_train} valid={fair_valid}")

    grid = {
        "entry_age": [10.0, 20.0],
        "stop": [25, 30, 35, 40],
        "tp1_mult": [1.25, 1.3, 1.35, 1.4, 1.5, 1.75, 2.0],
        "tp1_frac": [0.0, 0.3, 0.5, 0.6, 0.75],
        "tp2": [None, (3.0, 0.3), (4.0, 0.25)],
        "trail": [25, 30, 35, 45],
        "hard": [8.0, 12.0, 20.0],
        "hold": [480.0],
    }
    combos = list(itertools.product(*grid.values()))
    print(f"testing {len(combos)} combos "
          f"(multiple-testing warning: best-of-{len(combos)} cherry-picks noise; "
          f"validation column is the one to trust)")

    results = []
    for entry_age, stop, tp1m, tp1f, tp2, trail, hard, hold in combos:
        tps = [{"multiple": tp1m, "sell_fraction_of_remaining": tp1f}]
        if tp2:
            tps.append({"multiple": tp2[0], "sell_fraction_of_remaining": tp2[1]})
        exits = {"stop_loss_pct": stop, "take_profits": tps,
                 "hard_tp_multiple": hard, "trailing_stop_pct": trail, "max_hold_min": hold}
        buckets = {"train": [], "valid": [], "trending": []}
        for tok, candles, bucket in data:
            sim = backtest.simulate(candles, exits, entry_age, args.cost_pct, args.min_entry_vol)
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
            "params": {"entry_age": entry_age, "stop": stop, "tp1": [tp1m, tp1f],
                       "tp2": list(tp2) if tp2 else None, "trail": trail,
                       "hard": hard, "hold": hold},
            "train": tr,
            "valid": stats(buckets["valid"]),
            "trending_ref": stats(buckets["trending"]),
        })

    # a combo "hits target" only if BOTH halves clear it
    def hits(r):
        v = r["valid"]
        return (r["train"]["wr"] >= 60 and r["train"]["avg"] >= 2.0
                and v and v["n"] >= 10 and v["wr"] >= 60 and v["avg"] >= 2.0)

    def frontier(rows, key_wr, key_avg):
        rows = sorted(rows, key=lambda r: (-r["train"][key_wr], -r["train"][key_avg]))
        out, best_avg = [], -1.0
        for r in rows:
            if r["train"][key_avg] > best_avg:
                out.append(r)
                best_avg = r["train"][key_avg]
        return out

    hitters = [r for r in results if hits(r)]
    results.sort(key=lambda r: -(min(r["train"]["wr"], 60) * 10 + 100 * r["train"]["avg"]))

    def show(r):
        p = r["params"]
        tp2 = f" tp2 {p['tp2'][0]}x/{int(p['tp2'][1] * 100)}%" if p["tp2"] else ""
        v = r["valid"]
        vtxt = (f"valid n={v['n']} wr={v['wr']:.0f}% avg={v['avg']:.2f}x" if v else "valid: none")
        t = r["trending_ref"]
        ttxt = (f" | trending(ref) wr={t['wr']:.0f}% avg={t['avg']:.2f}x" if t else "")
        print(f"  age{p['entry_age']:.0f} stop{p['stop']} tp1 {p['tp1'][0]}x/{int(p['tp1'][1] * 100)}%"
              f"{tp2} trail{p['trail']} hard{p['hard']:.0f} | "
              f"train n={r['train']['n']} wr={r['train']['wr']:.0f}% avg={r['train']['avg']:.2f}x | "
              f"{vtxt}{ttxt}")

    print(f"\nscored combos: {len(results)}")
    print(f"\n== combos hitting 60% WR and 2.0x avg on BOTH train and validation (fair cohorts): "
          f"{len(hitters)} ==")
    for r in hitters[:10]:
        show(r)

    print("\n== train-set winrate/avg Pareto frontier (fair cohorts) ==")
    for r in frontier(results, "wr", "avg")[:12]:
        show(r)

    print("\n== top 10 by train avg multiple (any winrate) ==")
    for r in sorted(results, key=lambda r: -r["train"]["avg"])[:10]:
        show(r)

    print("\n== top 10 by train winrate (any avg) ==")
    for r in sorted(results, key=lambda r: -r["train"]["wr"])[:10]:
        show(r)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"n_combos": len(combos), "n_scored": len(results),
                   "hitters": hitters, "all": results}, fh, indent=1)
    print(f"\nfull results: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
