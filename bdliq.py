#!/usr/bin/env python
"""Current liquidity+price snapshot for the round-3 union tokens.

Purpose: settle the END-valuation question with evidence instead of a blanket
assumption. A token whose candle stream went silent is either (a) drained/
rugged - the LP is gone, the remainder is worthless - or (b) dormant but
intact - the AMM pool still fills a small sell near the last price. 1m candles
cannot distinguish the two; the pool's liquidity today can:

  * liquidity >= $1k today (weeks after listing) -> the pool survived; valuing
    the remainder at last real close was fair (if anything conservative).
  * liquidity ~ $0 today -> assume drained at silence time; the honest value
    of the remainder is ~0.

Output: reports/bd_liquidity.json {mint: {"liquidity": usd, "price": usd}}.
Resumable: existing entries are skipped.

Usage: python bdliq.py [--max-requests 2000]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

import backtest
from bdfetch import bd_get, load_key
from sweep3 import entry_features, passes

OUT = os.path.join("reports", "bd_liquidity.json")


def main() -> None:
    ap = argparse.ArgumentParser(description="Liquidity snapshot for union tokens")
    ap.add_argument("--max-requests", type=int, default=2000)
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    key = load_key()
    session = requests.Session()
    session.headers.update({"X-API-KEY": key, "x-chain": "solana",
                            "accept": "application/json"})

    tokens = backtest.load_token_index(os.path.join("reports", "bd_tokens.json"))
    union = []
    for tok in tokens:
        c = backtest.load_cached_candles(".bd_cache", tok.pool)
        if not c:
            continue
        for age in (30.0, 45.0, 60.0):
            f = entry_features(c, age)
            if f and passes(f, 8000.0, (1.1, 6.0), 0.85, None, None):
                union.append(tok)
                break

    snap = {}
    if os.path.exists(OUT):
        with open(OUT, "r", encoding="utf-8") as f:
            snap = json.load(f)
    todo = [t for t in union if t.mint not in snap]
    print(f"union {len(union)} | already snapped {len(union) - len(todo)} | to fetch {len(todo)}",
          flush=True)

    for i, tok in enumerate(todo, 1):
        try:
            r = bd_get(session, "/defi/price",
                       {"address": tok.mint, "include_liquidity": "true"},
                       args.max_requests)
        except RuntimeError as exc:
            print(f"stopping early: {exc} (re-run to resume)", flush=True)
            break
        liq = price = None
        if r is not None and r.status_code == 200:
            d = (r.json() or {}).get("data") or {}
            liq, price = d.get("liquidity"), d.get("value")
        snap[tok.mint] = {"liquidity": liq, "price": price}
        if i % 100 == 0:
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump(snap, f)
            alive = sum(1 for v in snap.values() if (v["liquidity"] or 0) >= 1000)
            print(f"  {i}/{len(todo)} | pools alive (liq>=$1k): {alive}/{len(snap)}", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snap, f)
    alive = sum(1 for v in snap.values() if (v["liquidity"] or 0) >= 1000)
    drained = sum(1 for v in snap.values() if (v["liquidity"] or 0) < 1000)
    print(f"\nsnapshot: {len(snap)} tokens | intact (>=$1k liq): {alive} | "
          f"drained: {drained} -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
