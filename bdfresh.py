#!/usr/bin/env python
"""Grow the sample FORWARD with recent listings (last ~3 days).

Birdeye's new_listing feed refuses time_to older than ~3 days (see bdgrow2.py,
which grew the sample backward via token/list for that reason). This script
takes the other direction: enumerate the freshest listings the feed still
serves, keep those old enough for a full observation window, fetch their 1m
candles, and merge them into reports/bd_tokens.json so every consumer
(backtest, tradecards, sweeps) sees the grown universe.

Dedupes against the existing index, the candle cache, and the no-data ledger;
failures are added to the ledger so future runs never re-pay for them. All
requests go through bdfetch.bd_get and are metered into the Birdeye usage
ledger (bdusage).

Usage: python bdfresh.py [--days 3] [--min-age-hours 10] [--max-tokens 4000]
                         [--max-requests 6000] [--window-min 720]
After it finishes: python blocklist.py, then re-run backtest/tradecards.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

from bdfetch import (CACHE_DIR, INDEX_PATH, NODATA_PATH, enumerate_tokens,
                     fetch_candles, load_key, write_progress, _req_count)


def main() -> None:
    ap = argparse.ArgumentParser(description="Grow the Birdeye sample with recent listings")
    ap.add_argument("--days", type=float, default=3, help="feed only serves ~3 days back")
    ap.add_argument("--min-age-hours", type=float, default=10)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--max-requests", type=int, default=6000)
    ap.add_argument("--window-min", type=int, default=720)
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    key = load_key()
    session = requests.Session()
    session.headers.update({"X-API-KEY": key, "x-chain": "solana",
                            "accept": "application/json"})

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        index = json.load(f)
    existing = {t["mint"] for t in index}
    try:
        with open(NODATA_PATH, "r", encoding="utf-8") as f:
            nodata = set(json.load(f))
    except (OSError, ValueError):
        nodata = set()

    print(f"enumerating listings from the last {args.days:.0f}d "
          f"(min age {args.min_age_hours:.0f}h)...", flush=True)
    toks = enumerate_tokens(session, args)
    fresh = [t for t in toks
             if t["mint"] not in existing and t["mint"] not in nodata
             and not os.path.exists(os.path.join(CACHE_DIR, f"{t['mint']}.json"))]
    print(f"enumerated {len(toks)} listings -> {len(fresh)} new to the sample "
          f"({_req_count[0]} requests so far)", flush=True)

    fetched = failed = 0
    for i, tok in enumerate(fresh, 1):
        try:
            rows = fetch_candles(session, tok, args)
        except RuntimeError as exc:
            print(f"stopping early: {exc}", flush=True)
            fresh = fresh[:i - 1]  # only merge tokens we actually attempted
            break
        if rows:
            with open(os.path.join(CACHE_DIR, f"{tok['mint']}.json"), "w", encoding="utf-8") as f:
                json.dump({"candles": rows, "cmin": 1}, f)
            fetched += 1
        else:
            failed += 1
            nodata.add(tok["mint"])
        if i % 100 == 0:
            print(f"  {i}/{len(fresh)} | candles {fetched}, no-data {failed} | "
                  f"{_req_count[0]} reqs", flush=True)
            write_progress("fresh-fetch", i, len(fresh), fetched, failed)

    index.extend(fresh)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1)
    with open(NODATA_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(nodata), f)
    write_progress("done", len(fresh), len(fresh), fetched, failed)
    print(f"\ndone: {fetched} new tokens with candles, {failed} no-data, "
          f"index grown to {len(index)} | requests this run: {_req_count[0]}", flush=True)
    print("next: python blocklist.py, then backtest/tradecards", flush=True)


if __name__ == "__main__":
    main()
