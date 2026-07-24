#!/usr/bin/env python
"""Sample blocklist: purge wash-trade RAMP charts from the token universe.

The pattern (Kenny, 2026-07-20): a near-perfect staircase of tiny uniform green
candles gliding up for an hour+ - a ramp bot buying its own token to paint a
chart. The prices were never realizable by an outside seller, so any simulated
WIN on such a chart is manufactured data. These tokens are removed from the
sample entirely via reports/bd_blocklist.json, which backtest.load_token_index
enforces for every consumer (backtest, sweeps, tradecards).

Honesty rule - why only RISING ramps are blocklisted:
  * a rising ramp fabricates WINS -> keeping it inflates results -> purge.
  * a smooth decline produces LOSSES that a real buyer would genuinely have
    eaten -> purging those would delete real losses and inflate results ->
    they STAY in the sample (the strategy avoids them via entry filters,
    which are validated train/valid like everything else).

Detector (full-history, conservative thresholds):
  n >= 20 candles, close_end >= 1.5x close_start (meaningfully up),
  green fraction >= 0.70, and Kaufman efficiency ratio of the close path
  >= 0.70 (organic price action scores ~0.2-0.5; ramp bots score ~0.8+).

Second rule - confirmed scam factories (outcome-blind, so also purge-safe):
a symbol launched >= 3 times inside the sample window AND with at least one
family member chart-flagged as a wash-ramp is one operation redeploying the
same scam (observed: 29x "e-superscript-x", 4x JIMOTHY, 3x OE). The whole
family is removed - wins and losses together - so the cleanup cannot inflate
results. Bare name-duplication alone is NOT enough (organic copycat clusters
around trending names would purge 80%+ of the sample). Live-bot equivalent:
skip symbols already seen deployed before.

Re-run this after every new candle fetch: python blocklist.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest

CACHES = (".bd_cache_ext", ".bd_cache")
OUT = os.path.join("reports", "bd_blocklist.json")

MIN_CANDLES = 20
MIN_RISE = 1.5
MIN_GREEN = 0.70
MIN_ER = 0.70
DUP_SYMBOL_MIN = 3  # same symbol launched this many times = scam factory


def ramp_score(candles: list):
    """(is_rising_ramp, metrics) over the token's full candle history."""
    closes = [c[4] for c in candles if c[4] > 0]
    if len(closes) < MIN_CANDLES or closes[0] <= 0:
        return False, None
    net = closes[-1] - closes[0]
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    er = abs(net) / path if path > 0 else 0.0
    green = sum(1 for c in candles if c[4] > c[1]) / len(candles)
    rise = closes[-1] / closes[0]
    m = {"er": round(er, 3), "green_frac": round(green, 3),
         "rise": round(rise, 2), "n_candles": len(closes)}
    return (rise >= MIN_RISE and green >= MIN_GREEN and er >= MIN_ER), m


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    # raw index read: load_token_index would apply an existing blocklist and
    # hide previously-flagged tokens from re-classification. All sample eras
    # are scanned so the purge stays symmetric across them.
    tokens, seen_mints = [], set()
    for idx in ("bd_tokens.json", "bd_tokens_60d.json", "bd_tokens_oot.json"):
        path = os.path.join("reports", idx)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for t in json.load(f):
                if t["mint"] not in seen_mints:
                    seen_mints.add(t["mint"])
                    tokens.append(t)

    sym_count = {}
    for t in tokens:
        s = (t.get("symbol") or "").strip().upper()
        if s and s != "?":
            sym_count[s] = sym_count.get(s, 0) + 1

    # pass 1: chart-shape scan
    blocked, scanned, ramp_syms = {}, 0, set()
    for t in tokens:
        candles = None
        for cache in CACHES:
            candles = backtest.load_cached_candles(cache, t["pool"])
            if candles:
                break
        if not candles:
            continue
        scanned += 1
        is_ramp, m = ramp_score(candles)
        if is_ramp:
            blocked[t["mint"]] = {"symbol": t.get("symbol", "?"),
                                  "reason": "wash_ramp", **m}
            s = (t.get("symbol") or "").strip().upper()
            if s and s != "?":
                ramp_syms.add(s)

    # pass 2: confirmed factories = duplicated symbol + >=1 ramp-flagged member
    factories = {s for s in ramp_syms if sym_count.get(s, 0) >= DUP_SYMBOL_MIN}
    for t in tokens:
        s = (t.get("symbol") or "").strip().upper()
        if s in factories and t["mint"] not in blocked:
            blocked[t["mint"]] = {"symbol": t.get("symbol", "?"),
                                  "reason": "serial_redeploy",
                                  "deployments": sym_count[s]}

    os.makedirs("reports", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(blocked, f, indent=1)
    n_ramp = sum(1 for v in blocked.values() if v["reason"] == "wash_ramp")
    n_dup = len(blocked) - n_ramp
    print(f"blocklisted {len(blocked)} tokens -> {OUT} "
          f"({n_ramp} wash-ramps of {scanned} scanned; {n_dup} serial redeploys "
          f"across {len(factories)} factory symbols)")
    for mint, m in list(blocked.items())[:15]:
        print(f"  {m['symbol']:<14} er={m['er']} green={m['green_frac']} "
              f"rise={m['rise']}x n={m['n_candles']}")
    if len(blocked) > 15:
        print(f"  ... and {len(blocked) - 15} more")


if __name__ == "__main__":
    main()
