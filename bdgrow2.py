#!/usr/bin/env python
"""True out-of-time sample: a virgin listing week via /defi/v3/token/list.

Birdeye's new_listing feed now refuses time_to older than 3 days, but
/defi/v3/token/list accepts max_recent_listing_time as a cursor and reaches
months back. This fetcher builds an out-of-time evaluation set from a week
that PRE-DATES the campaign's original 30-day sample - listings no loop ever
saw or optimized against.

Honesty rules:
  * enumeration keeps ONLY address/symbol/listing-time; every current-value
    field (liquidity, volume, mc - all as-of-today, i.e. survivorship-laden)
    is discarded unread.
  * the candle budget cannot cover ~40k listings, so a SEEDED random
    subsample (outcome-blind, uniform) is drawn; unbiasedness is preserved.
  * candle windows and completion chunks mirror bdgrow.py (720min + 720min
    completion for bounce-triggered tokens only).

Output: reports/bd_tokens_oot.json + candles in .bd_cache / .bd_cache_ext.

Usage: python bdgrow2.py [--from-days 40] [--to-days 33] [--sample 4500]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

import bdfetch
from backtest import EXCLUDE_SYMBOLS
from bdfetch import bd_get, load_key, fetch_candles
from bdgrow import bounce_triggered

INDEX = os.path.join("reports", "bd_tokens_oot.json")
CACHE = ".bd_cache"
CACHE_EXT = ".bd_cache_ext"


def enumerate_window(session, args, t_new: int, t_old: int) -> list:
    """Walk token/list backward from t_new to t_old via max_recent_listing_time."""
    cursor = t_new
    seen, out = set(), []
    pages = 0
    while cursor > t_old:
        r = bd_get(session, "/defi/v3/token/list",
                   {"sort_by": "recent_listing_time", "sort_type": "desc",
                    "limit": 100, "max_recent_listing_time": cursor},
                   args.max_requests)
        if r is None or r.status_code != 200:
            print(f"list page failed ({'none' if r is None else r.status_code}); "
                  f"stopping at {len(out)}", flush=True)
            break
        items = ((r.json().get("data") or {}).get("items")) or []
        if not items:
            break
        oldest = cursor
        for it in items:
            addr, ts = it.get("address"), it.get("recent_listing_time")
            if not addr or not ts:
                continue
            ts = int(ts)
            oldest = min(oldest, ts)
            if addr in seen or ts < t_old or ts > t_new:
                continue
            sym = (it.get("symbol") or "?").strip()
            if sym.upper() in EXCLUDE_SYMBOLS:
                continue
            seen.add(addr)
            out.append({"mint": addr, "pool": addr, "symbol": sym,
                        "created_ts": ts, "cohort": "new", "source": "v3list_oot"})
        cursor = oldest - 1 if oldest >= cursor else oldest
        pages += 1
        if pages % 50 == 0:
            print(f"  {len(out)} tokens, cursor {-(cursor - int(time.time())) / 86400:.1f}d back",
                  flush=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Virgin-week OOT sample fetcher")
    ap.add_argument("--from-days", type=float, default=40, help="older edge of window")
    ap.add_argument("--to-days", type=float, default=33, help="newer edge of window")
    ap.add_argument("--sample", type=int, default=4500)
    ap.add_argument("--seed", type=int, default=1739)
    ap.add_argument("--window-min", type=int, default=720)
    ap.add_argument("--max-requests", type=int, default=12000)
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    key = load_key()
    session = requests.Session()
    session.headers.update({"X-API-KEY": key, "x-chain": "solana",
                            "accept": "application/json"})
    now = int(time.time())

    if os.path.exists(INDEX):
        with open(INDEX, "r", encoding="utf-8") as f:
            picked = json.load(f)
        print(f"resuming with {INDEX}: {len(picked)} sampled tokens", flush=True)
    else:
        t_new = now - int(args.to_days * 86400)
        t_old = now - int(args.from_days * 86400)
        print(f"enumerating virgin window day-{args.from_days:.0f} .. day-{args.to_days:.0f}",
              flush=True)
        found = enumerate_window(session, args, t_new, t_old)
        print(f"enumerated {len(found)} listings in window", flush=True)
        rng = random.Random(args.seed)
        picked = found if len(found) <= args.sample else rng.sample(found, args.sample)
        with open(INDEX, "w", encoding="utf-8") as f:
            json.dump(picked, f, indent=1)
        print(f"seeded sample of {len(picked)} -> {INDEX}", flush=True)

    fetched = failed = have = 0
    for i, tok in enumerate(picked, 1):
        cpath = os.path.join(CACHE, f"{tok['mint']}.json")
        if os.path.exists(cpath):
            have += 1
            continue
        try:
            rows = fetch_candles(session, tok, args)
        except RuntimeError as exc:
            print(f"stopping candle phase early: {exc}", flush=True)
            break
        if rows:
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump({"candles": rows, "cmin": 1}, f)
            fetched += 1
        else:
            failed += 1
        if i % 500 == 0:
            print(f"  {i}/{len(picked)} | candles {fetched} no-data {failed}", flush=True)
    print(f"candles done: {fetched} new, {failed} no data, {have} present", flush=True)

    done = ext = 0
    for tok in picked:
        cpath = os.path.join(CACHE, f"{tok['mint']}.json")
        epath = os.path.join(CACHE_EXT, f"{tok['mint']}.json")
        if not os.path.exists(cpath) or os.path.exists(epath):
            continue
        try:
            with open(cpath, "r", encoding="utf-8") as f:
                candles = (json.load(f).get("candles")) or []
        except (OSError, ValueError):
            continue
        if not candles or not bounce_triggered(candles):
            continue
        t0 = int(tok["created_ts"])
        t_from = candles[-1][0] + 60
        t_final = min(t0 + 1440 * 60, now)
        rows = []
        if t_from < t_final:
            try:
                r = bd_get(session, "/defi/v3/ohlcv",
                           {"address": tok["mint"], "type": "1m", "currency": "usd",
                            "time_from": t_from, "time_to": t_final}, args.max_requests)
            except RuntimeError as exc:
                print(f"stopping completion early: {exc}", flush=True)
                break
            if r is not None and r.status_code == 200:
                for it in (((r.json().get("data")) or {}).get("items")) or []:
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
        seen_ts = {c[0] for c in candles}
        merged = candles + [x for x in rows if x[0] not in seen_ts]
        merged.sort(key=lambda x: x[0])
        with open(epath, "w", encoding="utf-8") as f:
            json.dump({"candles": merged, "cmin": 1, "ext_candles": len(rows)}, f)
        done += 1
        ext += 1 if rows else 0
    print(f"completion done: {done} bounce-triggered ({ext} gained candles)", flush=True)

    copied = 0
    for tok in picked:
        cpath = os.path.join(CACHE, f"{tok['mint']}.json")
        epath = os.path.join(CACHE_EXT, f"{tok['mint']}.json")
        if os.path.exists(cpath) and not os.path.exists(epath):
            shutil.copy2(cpath, epath)
            copied += 1
    print(f"mirrored {copied}; total requests {bdfetch._req_count[0]}", flush=True)


if __name__ == "__main__":
    main()
