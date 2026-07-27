#!/usr/bin/env python
"""Fresh OUT-OF-TIME sample fetcher (2026-07-24, holder-signal campaign).

Like bdfresh.py but writes to reports/bd_tokens_fresh.json and NEVER touches
reports/bd_tokens.json — the main index defines the "original universe" for
loop8/9 and sweepvol liquidity/intact logic, so growing it mid-campaign would
contaminate those evals. The 60d sample's newest listing is 2026-07-20; this
fetches the last ~3 days (min age 10h for a full 720m observation window), so
every token here is out-of-time for every strategy family tested so far.

Candles land in .bd_cache/<mint>.json (same format; pool==mint for these).

Usage: python freshfetch.py [--days 3] [--min-age-hours 10]
                            [--max-tokens 3000] [--max-requests 6000]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

from bdfetch import (CACHE_DIR, NODATA_PATH, enumerate_tokens, fetch_candles,
                     load_key, write_progress, _req_count)

FRESH_INDEX = os.path.join("reports", "bd_tokens_fresh.json")
EXISTING_INDEXES = ("bd_tokens.json", "bd_tokens_60d.json", "bd_tokens_oot.json")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch a fresh OOT sample to a separate index")
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--min-age-hours", type=float, default=10)
    ap.add_argument("--max-tokens", type=int, default=3000)
    ap.add_argument("--max-requests", type=int, default=6000)
    ap.add_argument("--window-min", type=int, default=720)
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    key = load_key()
    session = requests.Session()
    session.headers.update({"X-API-KEY": key, "x-chain": "solana",
                            "accept": "application/json"})

    existing = set()
    for name in EXISTING_INDEXES:
        path = os.path.join("reports", name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                existing |= {t["mint"] for t in json.load(f)}
    try:
        with open(NODATA_PATH, encoding="utf-8") as f:
            nodata = set(json.load(f))
    except (OSError, ValueError):
        nodata = set()

    # resume: keep prior fresh-index entries, only enumerate if file absent
    if os.path.exists(FRESH_INDEX):
        with open(FRESH_INDEX, encoding="utf-8") as f:
            fresh = json.load(f)
        print(f"resuming fresh index: {len(fresh)} tokens", flush=True)
    else:
        print(f"enumerating last {args.days:.1f}d listings (min age {args.min_age_hours:.0f}h)...",
              flush=True)
        toks = enumerate_tokens(session, args)
        fresh = [t for t in toks if t["mint"] not in existing]
        os.makedirs("reports", exist_ok=True)
        with open(FRESH_INDEX, "w", encoding="utf-8") as f:
            json.dump(fresh, f, indent=1)
        print(f"enumerated {len(toks)} -> {len(fresh)} new-to-any-index tokens "
              f"({_req_count[0]} requests)", flush=True)

    fetched = failed = have = skipped = 0
    for i, tok in enumerate(fresh, 1):
        cpath = os.path.join(CACHE_DIR, f"{tok['mint']}.json")
        if os.path.exists(cpath):
            have += 1
            continue
        if tok["mint"] in nodata:
            skipped += 1
            continue
        try:
            rows = fetch_candles(session, tok, args)
        except RuntimeError as exc:
            print(f"stopping early: {exc} (re-run to resume)", flush=True)
            break
        if rows:
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump({"candles": rows, "cmin": 1}, f)
            fetched += 1
        else:
            failed += 1
            nodata.add(tok["mint"])
        if i % 100 == 0:
            print(f"  {i}/{len(fresh)} | candles new {fetched} have {have} "
                  f"| no-data {failed} | {_req_count[0]} reqs", flush=True)
            write_progress("fresh-oot", i, len(fresh), have + fetched, failed)
    with open(NODATA_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(nodata), f)
    write_progress("done", len(fresh), len(fresh), have + fetched, failed)
    print(f"\ndone: {have + fetched} fresh tokens with candles ({fetched} new, "
          f"{failed} no-data, {skipped} known-dead) | requests: {_req_count[0]}", flush=True)


if __name__ == "__main__":
    main()
