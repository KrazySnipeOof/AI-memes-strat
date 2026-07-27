#!/usr/bin/env python
"""Classify the copy/cluster strategy tokens: pure scam vs human-and-manipulated.

The monitored wallets snipe fresh pump.fun launches; most are bot artefacts.
This separates them into:

  RUG          instant collapse / nuke candle / pumped then dumped to ~0
  WASH_RAMP    bot staircase (blocklist.py signature: ER>=.70, green>=.70, up)
  SPIKE_DUMP   one dominant candle, dies in minutes - sniper in/out, no crowd
  ORGANIC      two-sided volume, sustained trading, natural pullbacks = humans

Then scores every token on two axes from its candles:
  human_score  two-sidedness, lifespan, active candles, choppy (low ER), not
               dominated by a single candle
  manip_score  pump magnitude, sharp coordinated legs, whale-dominated candle,
               and co-accumulation (>=2 monitored wallets = cluster token)

"Coins that look human AND heavily manipulated" = ORGANIC class ranked by
human_score * manip_score: real crowd FOMO into a whale-driven pump - the only
subset worth copy-trading. Everything else is manipulation with no humans.

Reads reports/cluster_backtest.json (card_trades) + .copytrade_cache candles.
Writes reports/manipscan.json (per-mint verdicts, for the trade-journal badge).

  python manipscan.py            classify + rank + write json
  python manipscan.py --top 30   show more of the sweet-spot list
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from bot.manip import VOL_FLOOR, classify, metrics  # noqa: F401  (single source of truth)
from tradecards import _load_copytrade_candles, _read_json

CLUSTER_JSON = os.path.join("reports", "cluster_backtest.json")
CACHE_DIR = ".copytrade_cache"
OUT = os.path.join("reports", "manipscan.json")


def main():
    ap = argparse.ArgumentParser(description="Classify copy/cluster tokens: scam vs human+manipulated")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    data = _read_json(CLUSTER_JSON)
    if not data or "card_trades" not in data:
        sys.exit("no cluster_backtest.json card_trades - run cluster_backtest.py first")
    cluster_mints = {r["mint"] for r in data["card_trades"].get("cluster", [])}
    recs = {}  # mint -> symbol (union of copy_all + cluster)
    for key in ("copy_all", "cluster"):
        for r in data["card_trades"].get(key, []):
            recs.setdefault(r["mint"], r.get("symbol", "?"))

    verdicts = {}
    for mint, sym in recs.items():
        candles = _load_copytrade_candles(CACHE_DIR, mint)
        if not candles:
            continue
        v = classify(candles, mint in cluster_mints)
        if v:
            verdicts[mint] = {"symbol": sym, **v}

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(verdicts, f, indent=1)

    counts = {}
    for v in verdicts.values():
        counts[v["class"]] = counts.get(v["class"], 0) + 1
    n = len(verdicts) or 1
    print(f"classified {len(verdicts)} copy/cluster tokens -> {OUT}\n")
    print("class distribution:")
    for cls in ("RUG", "WASH_RAMP", "SPIKE_DUMP", "ORGANIC"):
        c = counts.get(cls, 0)
        print(f"  {cls:<11} {c:>4}  ({100*c/n:.0f}%)")
    scammy = sum(counts.get(k, 0) for k in ("RUG", "WASH_RAMP", "SPIKE_DUMP"))
    print(f"  {'-'*11}")
    print(f"  scam-like  {scammy:>4}  ({100*scammy/n:.0f}%)   organic {counts.get('ORGANIC',0)} ({100*counts.get('ORGANIC',0)/n:.0f}%)")

    organic = sorted((v | {"mint": m} for m, v in verdicts.items() if v["class"] == "ORGANIC"),
                     key=lambda v: -v["sweet"])
    print(f"\nHUMAN + HEAVILY MANIPULATED — top {args.top} (ORGANIC, ranked by human x manip):")
    print(f"  {'symbol':<12}{'human':>6}{'manip':>6}{'sweet':>6}{'life':>6}{'rise':>7}"
          f"{'redVol':>7}{'ER':>6}{'clus':>5}")
    for v in organic[:args.top]:
        print(f"  {(v['symbol'] or '?')[:11]:<12}{v['human']:>6}{v['manip']:>6}{v['sweet']:>6}"
              f"{v['lifespan_min']:>6.0f}{v['rise']:>7.1f}{v['red_vol_frac']:>7.2f}{v['er']:>6.2f}"
              f"{'Y' if v['is_cluster'] else '-':>5}")
    print(f"\n{len(organic)} organic tokens total; the rest ({scammy}) are bot/scam artefacts.")
    print("These ORGANIC coins are the only copy/cluster subset with real human participation.")


if __name__ == "__main__":
    main()
