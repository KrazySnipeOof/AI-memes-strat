#!/usr/bin/env python
"""Strategy lab: pre-registered evaluation of ALL strategy families on the
fresh out-of-time sample (reports/bd_tokens_fresh.json, listings 2026-07-21+,
never seen by any sweep).

PRE-REGISTRATION (frozen 2026-07-24 BEFORE any fresh-sample outcome was
computed; no config may be added after the first --run):

  BASE     the live paper-trial strategy: age-30m entry, cum vol >= $8k,
           base-entry setup gate (backtest.DEFAULT_SETUP). This is the first
           true out-of-time read on the strategy the bot trades live.
  BOUNCE   loop8 incumbent P3: bounce_entry(dd=0.55, cv=500, max_nukes=1),
           exit stop50/arm1.5/tr25/hard10/hold480 (its registered exit).
  VOLMC    finalists V1-V4 from the train-half leaderboard (sweepvol.py,
           reports/volmc_train.json) — the valid half, virgin week and this
           fresh sample were never scored during that sweep.
  HOLDER   holder-distribution gates on the BASE entry population, features
           from entry-time trade-tape replay (holdertape.py). Thresholds are
           reasoning-only (no tuning data existed when frozen):
             H0  no gate (baseline population with features present)
             H1  distributed: holders>=50, top10<=50%, top1<=20%, snipers<=30%
             H2  loose:       holders>=30, top10<=65%, top1<=30%
             H3  concentrated (INVERSE control, expected to lose): top10>=75%
           Primary read: H1/H2 uplift vs H0 on the same entries+exits.

Exits shared by BASE and HOLDER: EA stop60/arm1.5/tr25/hard10/hold480,
                                 EB stop60/arm1.5/tr25/hard20/hold480.
Engine: v5 verbatim (loop8_eval.sim); fresh tokens have no liquidity
snapshot => data-end remainders valued at 0 (strictly conservative); 4% cost.

Usage: python freshlab.py --run
Output: console table + reports/strategy_lab.json (dashboard panel + per-trade
strategy tags).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
from loop8_eval import bounce_entry, sim, wr_avg
from sweep import token_bucket
from sweepvol import churn_entry, LAUNCHPAD_1B

FRESH_INDEX = os.path.join("reports", "bd_tokens_fresh.json")
FEATURES_PATH = os.path.join("reports", "holder_features.json")
OUT_PATH = os.path.join("reports", "strategy_lab.json")

ENTRY_AGE_MIN = 30.0
MIN_CUM_VOL = 8000.0

EA = dict(stop=60, arm=1.5, trail=25, hard=10.0, hold=480.0)
EB = dict(stop=60, arm=1.5, trail=25, hard=20.0, hold=480.0)

VOLMC_FINALS = [
    ("V1", dict(universe="lp1b", ratio=0.5, window=30, max_nukes=1), EB),
    ("V2", dict(universe="lp1b", ratio=0.5, window=None, max_nukes=1), EB),
    ("V3", dict(universe="lp1b", ratio=1.0, window=None, max_nukes=None), EB),
    ("V4", dict(universe="lp1b", ratio=1.0, window=30, max_nukes=None), EB),
]

HOLDER_GATES = [
    ("H0", lambda f: True),
    ("H1", lambda f: f["holders"] >= 50 and f["top10_share"] <= 0.50
        and f["top1_share"] <= 0.20 and f["sniper_share"] <= 0.30),
    ("H2", lambda f: f["holders"] >= 30 and f["top10_share"] <= 0.65
        and f["top1_share"] <= 0.30),
    ("H3", lambda f: f["top10_share"] >= 0.75),
]


def age_entry(candles):
    """(idx, entry_price, pre) for the age-30m/vol8k entry family, or None."""
    entry_ts = candles[0][0] + ENTRY_AGE_MIN * 60
    pre = [c for c in candles if c[0] <= entry_ts]
    post = [c for c in candles if c[0] > entry_ts]
    if not pre or not post or pre[-1][4] <= 0:
        return None
    if sum(c[5] for c in pre) < MIN_CUM_VOL:
        return None
    return len(pre) - 1, pre[-1][4], pre


def stats(mults):
    if not mults:
        return None
    a, w, n = wr_avg(mults)
    return {"avg": round(a, 3), "wr": round(w, 1), "n": n,
            "med": round(statistics.median(mults), 3)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()
    if not args.run:
        ap.print_help()
        return
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    tokens = backtest.load_token_index(FRESH_INDEX)
    with open(FRESH_INDEX, encoding="utf-8") as f:
        source_of = {t["mint"]: t.get("source") or "?" for t in json.load(f)}
    try:
        with open(FEATURES_PATH, encoding="utf-8") as f:
            feats = json.load(f)
    except (OSError, ValueError):
        feats = {}
    data = []
    for tok in tokens:
        c = backtest.load_cached_candles(".bd_cache", tok.pool)
        if c:
            data.append((tok, c, token_bucket(tok.mint)))
    print(f"fresh sample: {len(data)} tokens with candles "
          f"({sum(1 for t, _, _ in data if t.mint in feats)} with holder features)")

    strategies = []

    def record(key, label, status, desc, results):
        """results: list of (mult, reason, tok, bucket, entry_ts)"""
        mults = [r[0] for r in results]
        halves = {b: [r[0] for r in results if r[3] == b] for b in ("train", "valid")}
        strategies.append({
            "key": key, "label": label, "status": status, "desc": desc,
            "pooled": stats(mults),
            "train": stats(halves["train"]), "valid": stats(halves["valid"]),
            "trades": [{"symbol": r[2].symbol, "mint": r[2].mint,
                        "multiple": round(r[0], 3), "reason": r[1],
                        "entry_ts": r[4], "strategy": key}
                       for r in sorted(results, key=lambda r: -r[4])[:15]],
        })
        s = stats(mults)
        line = f"  {key:<6} {label:<34} " + (
            f"{s['avg']:.3f}x wr{s['wr']:.0f}% med{s['med']:.2f} n{s['n']}" if s else "n=0")
        for b in ("train", "valid"):
            hs = stats(halves[b])
            line += f" | {b[0]}:{hs['avg']:.2f}/n{hs['n']}" if hs else f" | {b[0]}:—"
        print(line)

    def run_sim(tok, c, idx, entry, ex, bucket, out):
        r = sim(c, idx, entry, ex, False)
        if r is not None:
            out.append((r, "v5", tok, bucket, int(c[idx][0])))

    print("\n== FRESH OOT SAMPLE — pre-registered strategies, engine v5 ==")

    # BASE (live strategy) + HOLDER gates share the age30/vol8k population
    base_pop = []
    for tok, c, bucket in data:
        ae = age_entry(c)
        if ae:
            base_pop.append((tok, c, bucket, *ae))

    for ex_name, ex in (("EA", EA), ("EB", EB)):
        results = []
        for tok, c, bucket, idx, entry, pre in base_pop:
            if not backtest.entry_setup_ok(pre, int(c[idx][0]), entry, backtest.DEFAULT_SETUP):
                continue
            run_sim(tok, c, idx, entry, ex, bucket, results)
        record(f"BASE-{ex_name}", f"base-entry gate (live strategy) {ex_name}",
               "LIVE" if ex_name == "EA" else "RESEARCH",
               "age30 vol8k + DEFAULT_SETUP", results)

    results = []
    for tok, c, bucket in data:
        e = bounce_entry(c, 0.55, 500.0, 1)
        if e:
            run_sim(tok, c, e[0], e[1], dict(stop=50, arm=1.5, trail=25,
                                             hard=10.0, hold=480.0), bucket, results)
    record("BOUNCE", "bounce P3 (loop8 incumbent)", "RESEARCH",
           "dd55 cv500 nk1 | stop50 arm1.5 tr25 hard10", results)

    for name, ent, ex in VOLMC_FINALS:
        results = []
        for tok, c, bucket in data:
            if ent["universe"] == "lp1b" and source_of.get(tok.mint) not in LAUNCHPAD_1B:
                continue
            e = churn_entry(c, ent["ratio"], ent["window"], ent["max_nukes"])
            if e:
                run_sim(tok, c, e[0], e[1], ex, bucket, results)
        record(name, f"vol/MC churn r{ent['ratio']} w{ent['window']} nk{ent['max_nukes']}",
               "RESEARCH", "pre-registered from train leaderboard", results)

    n_feat_gated = 0
    for gname, gate in HOLDER_GATES:
        for ex_name, ex in (("EA", EA), ("EB", EB)):
            results = []
            for tok, c, bucket, idx, entry, pre in base_pop:
                f = feats.get(tok.mint)
                if not f or f.get("no_tape") or "top10_share" not in f:
                    continue
                if not gate(f):
                    continue
                run_sim(tok, c, idx, entry, ex, bucket, results)
            record(f"{gname}-{ex_name}",
                   {"H0": "holder baseline (no gate)",
                    "H1": "holder distributed (primary)",
                    "H2": "holder loose",
                    "H3": "holder concentrated (inverse ctl)"}[gname] + f" {ex_name}",
                   "RESEARCH", "entry-time tape features", results)

    out = {"updated": time.time(),
           "sample": {"index": FRESH_INDEX, "tokens_with_candles": len(data),
                      "base_population": len(base_pop),
                      "with_features": sum(1 for t, _, _ in data if t.mint in feats)},
           "strategies": strategies}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"\nwritten: {OUT_PATH}")


if __name__ == "__main__":
    main()
