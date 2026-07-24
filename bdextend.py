#!/usr/bin/env python
"""Extend Birdeye candle windows +72h for the round-3 entry-filter union.

Why: under the round-3 liquidity-honest engine, 78% of champion-config trades
are still open when the original 12h fetch window ends (the token stopped
printing 1m candles mid-window). Their true outcomes are unmeasured: revival
-> the trail/stop resolves at real prices; permanent silence -> the position
is honestly near-worthless. This fetcher settles it with data.

Uniformity (anti-cheat): EVERY token passing the loosest finalist entry
bounds (vol>=8k, momentum 1.1-6.0, pullback>=0.85 at age 30/45/60) gets the
same extension - winners, corpses, and everything between. No conditioning
on outcome.

Output: .bd_cache_ext/<mint>.json holding original + extension candles,
deduped by timestamp. Tokens outside the union are hard-copied so sweeps can
point --cache-dir at .bd_cache_ext alone. Resumable: extended tokens are
skipped on re-run (presence of the ext file marks completion).

Usage: python bdextend.py [--extra-hours 72] [--max-requests 15000]
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

import backtest
from bdfetch import bd_get, load_key
from sweep3 import entry_features, passes

SRC_DIR = ".bd_cache"
EXT_DIR = ".bd_cache_ext"
ORIG_WINDOW_MIN = 720
CHUNK_MIN = 720


def union_tokens(tokens):
    out = []
    for tok in tokens:
        c = backtest.load_cached_candles(SRC_DIR, tok.pool)
        if not c:
            continue
        for age in (30.0, 45.0, 60.0):
            f = entry_features(c, age)
            if f and passes(f, 8000.0, (1.1, 6.0), 0.85, None, None):
                out.append((tok, c))
                break
    return out


def fetch_extension(session, mint: str, t_from: int, t_final: int, max_requests: int):
    rows = []
    cursor = t_from
    while cursor < t_final:
        chunk_to = min(cursor + CHUNK_MIN * 60, t_final)
        r = bd_get(session, "/defi/v3/ohlcv",
                   {"address": mint, "type": "1m", "currency": "usd",
                    "time_from": cursor, "time_to": chunk_to}, max_requests)
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
                if cursor <= ts < chunk_to + 60:
                    rows.append([ts, o, h, l, c, vol])
        cursor = chunk_to
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Extend Birdeye candles for the round-3 union")
    ap.add_argument("--extra-hours", type=float, default=72)
    ap.add_argument("--max-requests", type=int, default=15000)
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    key = load_key()
    session = requests.Session()
    session.headers.update({"X-API-KEY": key, "x-chain": "solana",
                            "accept": "application/json"})

    tokens = backtest.load_token_index(os.path.join("reports", "bd_tokens.json"))
    uni = union_tokens(tokens)
    print(f"union tokens to extend: {len(uni)}", flush=True)
    os.makedirs(EXT_DIR, exist_ok=True)

    now = int(time.time())
    done = revived = dead = skipped = 0
    started = time.time()
    for i, (tok, candles) in enumerate(uni, 1):
        ext_path = os.path.join(EXT_DIR, f"{tok.pool}.json")
        if os.path.exists(ext_path):
            skipped += 1
            continue
        t_final = min(int(tok.created_ts + (ORIG_WINDOW_MIN + args.extra_hours * 60) * 60), now)
        t_from = candles[-1][0] + 60
        try:
            ext = fetch_extension(session, tok.mint, t_from, t_final, args.max_requests)
        except RuntimeError as exc:
            print(f"stopping early: {exc} (re-run to resume)", flush=True)
            break
        seen = {c[0] for c in candles}
        merged = candles + [r for r in ext if r[0] not in seen]
        merged.sort(key=lambda r: r[0])
        with open(ext_path, "w", encoding="utf-8") as f:
            json.dump({"candles": merged, "cmin": 1,
                       "extended_to": t_final, "ext_candles": len(ext)}, f)
        done += 1
        revived += 1 if ext else 0
        dead += 0 if ext else 1
        if i % 50 == 0:
            rate = done / max(1.0, time.time() - started)
            print(f"  {i}/{len(uni)} | extended {done} (revived {revived}, silent {dead}, "
                  f"resumed-skip {skipped}) @ {rate:.1f} tok/s", flush=True)

    print(f"\nextension done: {done} fetched ({revived} revived, {dead} silent), "
          f"{skipped} already present", flush=True)

    # hard-copy every remaining cached token so EXT_DIR is self-sufficient
    copied = 0
    for fn in os.listdir(SRC_DIR):
        if fn.endswith(".json") and not os.path.exists(os.path.join(EXT_DIR, fn)):
            shutil.copy2(os.path.join(SRC_DIR, fn), os.path.join(EXT_DIR, fn))
            copied += 1
    print(f"copied {copied} unextended tokens; {EXT_DIR} is now self-sufficient", flush=True)


if __name__ == "__main__":
    main()
