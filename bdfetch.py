#!/usr/bin/env python
"""Birdeye 10k-sample fetcher: unbiased token enumeration + per-token OHLCV.

Why this exists: GeckoTerminal's list endpoints stop at ~10 pages, capping the
sample at a few hundred tokens. Birdeye's new_listing feed pages backward
through EVERY pool that added liquidity - winners and corpses alike - which is
exactly the survivor-free population an honest backtest needs.

Two phases, both resumable:

  1. Enumerate: walk /defi/v2/tokens/new_listing back in time until
     --max-tokens tokens (or --days of history) are collected. Tokens younger
     than --min-age-hours are skipped so every token has a full observation
     window (entry up to 45m + hold up to 480m). Index written to
     reports/bd_tokens.json (mint, symbol, created_ts, source, cohort="new").
  2. Fetch candles: /defi/v3/ohlcv 1-minute candles from listing time to
     listing + --window-min, written to .bd_cache/<mint>.json in the exact
     format backtest.simulate consumes ([ts, o, h, l, c, vol_usd]). Tokens
     with an existing cache file are skipped, so re-runs are free.

Cost control: --max-requests hard-caps total API calls (default 25000; Lite
plan is 2.5M CUs/month - check your usage on the Birdeye dashboard). Pacing
is ~11 req/s against the plan's 15 RPS, with backoff on 429.

The sweeps consume this sample via:
  python sweep.py  --tokens-json reports/bd_tokens.json --cache-dir .bd_cache --min-train 100 --min-valid 100
  python sweep2.py --tokens-json reports/bd_tokens.json --cache-dir .bd_cache --min-train 100 --min-valid 100

Usage: python bdfetch.py [--max-tokens 10000] [--days 30] [--min-age-hours 10]
Requires BIRDEYE_API_KEY in .env (never commit or print the key).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

import bdusage
from backtest import EXCLUDE_SYMBOLS

BD = "https://public-api.birdeye.so"
CACHE_DIR = ".bd_cache"
INDEX_PATH = os.path.join("reports", "bd_tokens.json")
NODATA_PATH = os.path.join("reports", "bd_nodata.json")

_last = [0.0]
_req_count = [0]


def load_key() -> str:
    key = os.environ.get("BIRDEYE_API_KEY", "")
    if not key and os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("BIRDEYE_API_KEY="):
                    key = line.strip().split("=", 1)[1].strip()
    if not key:
        sys.exit("BIRDEYE_API_KEY not found in environment or .env")
    return key


def bd_get(session: requests.Session, path: str, params: dict, max_requests: int):
    """Paced GET with 429 backoff and a hard request-count cap."""
    if _req_count[0] >= max_requests:
        raise RuntimeError(f"request cap {max_requests} reached - raise --max-requests to continue")
    for attempt in range(5):
        wait = _last[0] + 0.09 - time.time()  # ~11 req/s vs the plan's 15 RPS
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        _req_count[0] += 1
        bdusage.record(path)
        try:
            r = session.get(f"{BD}{path}", params=params, timeout=20)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        return r
    return None


def write_progress(phase: str, done: int, total: int, cached: int, no_data: int):
    """Tiny status file the NERV dashboard polls to draw its progress bar."""
    try:
        os.makedirs("reports", exist_ok=True)
        with open(os.path.join("reports", "bd_progress.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "BIRDEYE 10K SAMPLE", "phase": phase, "done": done,
                       "total": total, "cached": cached, "no_data": no_data,
                       "requests": _req_count[0], "updated": time.time()}, f)
    except OSError:
        pass


def parse_listing_time(val) -> int:
    """liquidityAddedAt arrives as ISO text (UTC) or epoch seconds."""
    if isinstance(val, (int, float)):
        return int(val)
    dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def enumerate_tokens(session, args) -> list:
    """Walk new_listing backward via the time_to cursor until targets are met."""
    now = int(time.time())
    cursor = now - int(args.min_age_hours * 3600)
    floor_ts = now - int(args.days * 86400)
    seen, out = set(), []
    pages = 0
    while len(out) < args.max_tokens and cursor > floor_ts:
        r = bd_get(session, "/defi/v2/tokens/new_listing",
                   {"time_to": cursor, "limit": 20}, args.max_requests)
        if r is None or r.status_code != 200:
            print(f"listing page failed ({'no response' if r is None else r.status_code}); "
                  f"stopping enumeration at {len(out)} tokens", flush=True)
            break
        items = ((r.json().get("data") or {}).get("items")) or []
        if not items:
            print("empty listing page - end of feed", flush=True)
            break
        oldest = cursor
        new_here = 0
        for it in items:
            addr = it.get("address")
            try:
                ts = parse_listing_time(it.get("liquidityAddedAt"))
            except (ValueError, TypeError):
                continue
            oldest = min(oldest, ts)
            if not addr or addr in seen:
                continue
            sym = (it.get("symbol") or "?").strip()
            if sym.upper() in EXCLUDE_SYMBOLS:
                continue
            if ts > now - args.min_age_hours * 3600 or ts < floor_ts:
                continue
            seen.add(addr)
            new_here += 1
            out.append({"mint": addr, "pool": addr, "symbol": sym,
                        "created_ts": ts, "cohort": "new",
                        "source": it.get("source") or "?"})
        if oldest >= cursor:
            if new_here == 0:
                print("cursor stuck (time_to pagination not advancing); stopping "
                      f"at {len(out)} tokens", flush=True)
                break
            oldest = cursor - 1
        cursor = oldest
        pages += 1
        if pages % 25 == 0:
            span_h = (now - cursor) / 3600
            print(f"  enumerated {len(out)} tokens, walked back {span_h:.1f}h, "
                  f"{_req_count[0]} requests", flush=True)
            write_progress("enumerate", len(out), args.max_tokens, 0, 0)
    return out


def fetch_candles(session, tok: dict, args):
    """1m candles from listing to listing+window; backtest-compatible rows."""
    t0 = tok["created_ts"]
    r = bd_get(session, "/defi/v3/ohlcv",
               {"address": tok["mint"], "type": "1m", "currency": "usd",
                "time_from": t0, "time_to": t0 + args.window_min * 60},
               args.max_requests)
    if r is None or r.status_code != 200:
        return None
    items = ((r.json().get("data") or {}).get("items")) or []
    rows = []
    for it in items:
        try:
            ts = int(it.get("unix_time", it.get("unixTime")))
            o, h, l, c = float(it["o"]), float(it["h"]), float(it["l"]), float(it["c"])
            vol = it.get("v_usd", it.get("vUsd"))
            vol = float(vol) if vol is not None else float(it.get("v", 0)) * c
        except (KeyError, TypeError, ValueError):
            continue
        if ts >= t0:
            rows.append([ts, o, h, l, c, vol])
    rows.sort(key=lambda x: x[0])
    return rows if len(rows) >= 8 else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Birdeye unbiased-sample fetcher")
    ap.add_argument("--max-tokens", type=int, default=10000)
    ap.add_argument("--days", type=float, default=30, help="how far back to enumerate")
    ap.add_argument("--min-age-hours", type=float, default=10,
                    help="skip tokens younger than this (full observation window)")
    ap.add_argument("--window-min", type=int, default=720,
                    help="minutes of candles per token (entry 45 + hold 480 + slack)")
    ap.add_argument("--max-requests", type=int, default=25000, help="hard API-call cap")
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    key = load_key()
    session = requests.Session()
    session.headers.update({"X-API-KEY": key, "x-chain": "solana",
                            "accept": "application/json"})

    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            tokens = json.load(f)
        print(f"resuming with existing index: {len(tokens)} tokens "
              f"(delete {INDEX_PATH} to re-enumerate)", flush=True)
    else:
        print(f"enumerating up to {args.max_tokens} listings "
              f"({args.days:.0f}d window, min age {args.min_age_hours:.0f}h)...", flush=True)
        tokens = enumerate_tokens(session, args)
        os.makedirs("reports", exist_ok=True)
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=1)
        print(f"index written: {len(tokens)} tokens -> {INDEX_PATH}", flush=True)

    os.makedirs(CACHE_DIR, exist_ok=True)
    # No-data memory: tokens that returned no usable candles (dead / past
    # Birdeye's minute retention) are remembered so resumes don't re-spend CU
    # re-asking about them (the 2026-07-22 resume burned ~7k requests on
    # exactly that). Delete the file to force a retry of all of them.
    try:
        with open(NODATA_PATH, "r", encoding="utf-8") as f:
            nodata = set(json.load(f))
    except (OSError, ValueError):
        nodata = set()

    def save_nodata():
        try:
            with open(NODATA_PATH, "w", encoding="utf-8") as f:
                json.dump(sorted(nodata), f)
        except OSError:
            pass

    have = fetched = failed = skipped_dead = 0
    started = time.time()
    for i, tok in enumerate(tokens, 1):
        cpath = os.path.join(CACHE_DIR, f"{tok['mint']}.json")
        if os.path.exists(cpath):
            have += 1
            continue
        if tok["mint"] in nodata:
            skipped_dead += 1
            continue
        try:
            rows = fetch_candles(session, tok, args)
        except RuntimeError as exc:
            print(f"stopping candle fetch early: {exc} (re-run to resume)", flush=True)
            break
        if rows:
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump({"candles": rows, "cmin": 1}, f)
            fetched += 1
        else:
            failed += 1
            nodata.add(tok["mint"])
        if i % 100 == 0:
            save_nodata()
        if i % 100 == 0:
            rate = _req_count[0] / max(1.0, time.time() - started)
            print(f"  {i}/{len(tokens)} | cached {have + fetched} "
                  f"(new {fetched}, no-data {failed}) | {_req_count[0]} reqs "
                  f"@ {rate:.1f}/s", flush=True)
            write_progress("fetch", i, len(tokens), have + fetched, failed)
    save_nodata()
    write_progress("done", len(tokens), len(tokens), have + fetched, failed + skipped_dead)
    print(f"\ndone: {have + fetched}/{len(tokens)} tokens with candles "
          f"({fetched} new, {have} already cached, {failed} without usable data, "
          f"{skipped_dead} known-dead skipped free) | "
          f"total API requests this run: {_req_count[0]}", flush=True)
    print(f"next: python blocklist.py  (classify new tokens - wash-ramps must never "
          f"re-enter the sample), then sweeps with --tokens-json {INDEX_PATH}", flush=True)


if __name__ == "__main__":
    main()
