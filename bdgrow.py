#!/usr/bin/env python
"""Grow the Birdeye sample from 30 to 60 days for the round-5+ bounce campaign.

Why: the deep-dip + clean-chart corner (dd>=70% bounce entries with <=1
wash-ladder nuke) has the only verified ceiling that supports 60% WR / 2.0x,
but 30 days of listings leaves it under n=100 per half. Doubling the window
makes it testable, and the extra 30 days double as a TRUE out-of-time set:
no parameter in the campaign has ever seen them.

Phases (each resumable, shared request cap):
  1. enumerate 60 days of new_listing -> reports/bd_tokens_60d.json
     (merged with the existing 30d index; original file untouched)
  2. 1m candles, listing -> +720min, for tokens without a cache file
     (into .bd_cache, mirrored to .bd_cache_ext)
  3. second chunk (+720 -> +1440min) ONLY for new tokens whose first window
     contains a loosest-variant bounce trigger (dd50/cv500, entry-side
     condition - completion of entered trades, not outcome selection)

END-valuation note: new tokens get NO liquidity snapshot; their data_end
remainders are valued at 0 in evaluation (strictly conservative - the 30d
sample showed only 0.7% of pools stay intact anyway).

Usage: python bdgrow.py [--days 60] [--max-requests 20000]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

import bdfetch
from bdfetch import bd_get, load_key, enumerate_tokens, fetch_candles

OLD_INDEX = os.path.join("reports", "bd_tokens.json")
NEW_INDEX = os.path.join("reports", "bd_tokens_60d.json")
CACHE = ".bd_cache"
CACHE_EXT = ".bd_cache_ext"


def bounce_triggered(candles, dd_req=0.5, conf_vol=500.0, floor=500.0,
                     min_cum_vol=2000.0, max_wait_min=720.0):
    created = candles[0][0]
    peak, cum_vol = 0.0, 0.0
    for ts, o, h, l, cl, v in candles:
        if (ts - created) / 60 > max_wait_min:
            return False
        cum_vol += v
        credible = v >= floor and cl >= 0.5 * h
        if credible:
            peak = max(peak, min(h, cl * 2))
        if peak > 0 and (cl / peak <= 1 - dd_req and credible and cl > o
                         and v >= conf_vol and cum_vol >= min_cum_vol):
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Grow Birdeye sample to 60 days")
    ap.add_argument("--days", type=float, default=60)
    ap.add_argument("--max-tokens", type=int, default=30000)
    ap.add_argument("--min-age-hours", type=float, default=10)
    ap.add_argument("--window-min", type=int, default=720)
    ap.add_argument("--max-requests", type=int, default=20000)
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    key = load_key()
    session = requests.Session()
    session.headers.update({"X-API-KEY": key, "x-chain": "solana",
                            "accept": "application/json"})

    with open(OLD_INDEX, "r", encoding="utf-8") as f:
        old = json.load(f)
    known = {t["mint"] for t in old}

    if os.path.exists(NEW_INDEX):
        with open(NEW_INDEX, "r", encoding="utf-8") as f:
            merged = json.load(f)
        print(f"resuming with {NEW_INDEX}: {len(merged)} tokens", flush=True)
    else:
        print(f"enumerating {args.days:.0f} days of listings...", flush=True)
        found = enumerate_tokens(session, args)
        fresh = [t for t in found if t["mint"] not in known]
        merged = old + fresh
        with open(NEW_INDEX, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=1)
        print(f"enumerated {len(found)} ({len(fresh)} new) -> merged index "
              f"{len(merged)} tokens", flush=True)

    new_tokens = [t for t in merged if t["mint"] not in known]
    print(f"phase 2: candles for {len(new_tokens)} new tokens", flush=True)
    have = fetched = failed = 0
    for i, tok in enumerate(new_tokens, 1):
        cpath = os.path.join(CACHE, f"{tok['mint']}.json")
        if os.path.exists(cpath):
            have += 1
            continue
        try:
            rows = fetch_candles(session, tok, args)
        except RuntimeError as exc:
            print(f"stopping phase 2 early: {exc}", flush=True)
            break
        if rows:
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump({"candles": rows, "cmin": 1}, f)
            fetched += 1
        else:
            failed += 1
        if i % 500 == 0:
            print(f"  {i}/{len(new_tokens)} | new {fetched} no-data {failed}", flush=True)
    print(f"phase 2 done: {fetched} fetched, {failed} no data, {have} already present",
          flush=True)

    print("phase 3: +720min completion for bounce-triggered new tokens", flush=True)
    done = ext = 0
    for i, tok in enumerate(new_tokens, 1):
        cpath = os.path.join(CACHE, f"{tok['mint']}.json")
        epath = os.path.join(CACHE_EXT, f"{tok['mint']}.json")
        if not os.path.exists(cpath) or os.path.exists(epath):
            continue
        try:
            with open(cpath, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            continue
        candles = cached.get("candles") or []
        if not candles or not bounce_triggered(candles):
            continue
        t0 = int(tok["created_ts"])
        t_from = candles[-1][0] + 60
        t_final = min(t0 + 1440 * 60, int(time.time()))
        rows = []
        if t_from < t_final:
            try:
                r = bd_get(session, "/defi/v3/ohlcv",
                           {"address": tok["mint"], "type": "1m", "currency": "usd",
                            "time_from": t_from, "time_to": t_final}, args.max_requests)
            except RuntimeError as exc:
                print(f"stopping phase 3 early: {exc}", flush=True)
                break
            if r is not None and r.status_code == 200:
                items = ((r.json().get("data") or {}).get("items")) or []
                for it in items:
                    try:
                        ts = int(it.get("unix_time", it.get("unixTime")))
                        o, h, l, c = float(it["o"]), float(it["h"]), float(it["l"]), float(it["c"])
                        vol = it.get("v_usd", it.get("vUsd"))
                        vol = float(vol) if vol is not None else float(it.get("v", 0)) * c
                    except (KeyError, TypeError, ValueError):
                        continue
                    if ts >= t_from:
                        rows.append([ts, o, h, l, c, vol])
                rows.sort(key=lambda x: x[0])
        seen = {c[0] for c in candles}
        mergedc = candles + [r_ for r_ in rows if r_[0] not in seen]
        mergedc.sort(key=lambda x: x[0])
        with open(epath, "w", encoding="utf-8") as f:
            json.dump({"candles": mergedc, "cmin": 1, "ext_candles": len(rows)}, f)
        done += 1
        ext += 1 if rows else 0
        if done % 200 == 0:
            print(f"  completed {done} bounce-triggered tokens ({ext} gained candles)",
                  flush=True)
    print(f"phase 3 done: {done} completed, {ext} gained extension candles", flush=True)

    copied = 0
    for tok in new_tokens:
        cpath = os.path.join(CACHE, f"{tok['mint']}.json")
        epath = os.path.join(CACHE_EXT, f"{tok['mint']}.json")
        if os.path.exists(cpath) and not os.path.exists(epath):
            shutil.copy2(cpath, epath)
            copied += 1
    print(f"mirrored {copied} new tokens into {CACHE_EXT}; total requests "
          f"{bdfetch._req_count[0]}", flush=True)


if __name__ == "__main__":
    main()
