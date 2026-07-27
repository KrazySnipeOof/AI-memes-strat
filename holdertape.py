#!/usr/bin/env python
"""Holder-distribution features at ENTRY TIME, reconstructed from the Birdeye
per-token trade tape (2026-07-24 holder-signal campaign).

Why the tape: a holder snapshot taken today for a token that listed days ago
is POST-OUTCOME data (winners have many holders because they pumped) — using
it as an entry signal is lookahead. Replaying swaps from listing to entry_ts
gives the distribution a live bot would actually have seen. Only trades with
block_unix_time <= entry_ts enter the features.

Endpoint: /defi/txs/token/seek_by_time (after_time/before_time, 100/page),
metered into bdusage. Tape capped at --max-pages pages/token (truncated flag
recorded; concentration from the first N swaps is then an approximation).

Feature set per token (at entry_ts = created + 30m, the validated entry age):
  buyers, sellers      unique wallets that ever bought / sold pre-entry
  holders              wallets with net position > 0.05% of net-held total
  top1_share, top10_share   largest / top-10 net positions as share of total
  sniper_share         net-held share of wallets whose first buy was within
                       120s of the first tape trade (block-0 snipers)
  swaps, truncated     tape size bookkeeping

Only tokens passing the age30/cum-vol>=$8k entry family are fetched (that is
the population every strategy here enters from; no CU is spent on corpses).

Usage: python holdertape.py [--probe MINT] [--max-pages 30] [--max-requests 8000]
Output: reports/holder_features.json  {mint: {...features}}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

import backtest
import bdusage
from bdfetch import load_key

BD = "https://public-api.birdeye.so"
TAPE_PATH = "/defi/txs/token/seek_by_time"
FRESH_INDEX = os.path.join("reports", "bd_tokens_fresh.json")
OUT_PATH = os.path.join("reports", "holder_features.json")
ENTRY_AGE_MIN = 30.0
MIN_CUM_VOL = 8000.0
SNIPER_WINDOW_SEC = 120
DUST_FRAC = 0.0005

_last = [0.0]
_req = [0]


def bd_get(session, params, cap):
    if _req[0] >= cap:
        raise RuntimeError(f"request cap {cap} reached")
    for attempt in range(5):
        wait = _last[0] + 0.1 - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        _req[0] += 1
        bdusage.record(TAPE_PATH)
        try:
            r = session.get(f"{BD}{TAPE_PATH}", params=params, timeout=20)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None
        try:
            return (r.json().get("data") or {})
        except ValueError:
            return None
    return None


def token_delta(item, mint):
    """Signed token amount for the owner. Field shape verified live
    2026-07-24: from/to legs with camelCase uiAmount; to.address == mint
    means the token flowed TO the owner (buy)."""
    for leg_name, sign in (("to", 1.0), ("from", -1.0)):
        leg = item.get(leg_name)
        if isinstance(leg, dict) and leg.get("address") == mint:
            try:
                return sign * float(leg.get("uiAmount") or leg.get("ui_amount") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def fetch_tape(session, mint, t_from, t_to, max_pages, cap):
    """Swaps for `mint` in [t_from, t_to], oldest-first, [(ts, owner, delta)].
    seek_by_time accepts ONE time bound (verified live): page ascending from
    after_time and stop once blockUnixTime passes t_to."""
    rows, truncated = [], False
    for page in range(max_pages):
        data = bd_get(session, {"address": mint, "tx_type": "swap",
                                "after_time": t_from,
                                "offset": page * 100, "limit": 100}, cap)
        if data is None:
            return None, False
        items = data.get("items") or []
        past_window = False
        for it in items:
            owner = it.get("owner")
            try:
                ts = int(float(it.get("blockUnixTime") or it.get("block_unix_time") or 0))
            except (TypeError, ValueError):
                continue
            if ts > t_to:
                past_window = True
                break
            if not owner or ts < t_from:
                continue
            d = token_delta(it, mint)
            if d:
                rows.append((ts, owner, d))
        if past_window or not items or not (data.get("has_next") or data.get("hasNext")):
            break
    else:
        truncated = True
    rows.sort(key=lambda r: r[0])
    return rows, truncated


def features_from_tape(rows, truncated):
    if not rows:
        return None
    bal, first_buy = {}, {}
    buyers, sellers = set(), set()
    t0 = rows[0][0]
    for ts, owner, d in rows:
        bal[owner] = bal.get(owner, 0.0) + d
        if d > 0:
            buyers.add(owner)
            first_buy.setdefault(owner, ts)
        else:
            sellers.add(owner)
    pos = {w: b for w, b in bal.items() if b > 0}
    total = sum(pos.values())
    if total <= 0:
        return None
    shares = sorted((b / total for b in pos.values()), reverse=True)
    holders = [s for s in shares if s >= DUST_FRAC]
    snipers = {w for w, ts in first_buy.items() if ts - t0 <= SNIPER_WINDOW_SEC}
    sniper_share = sum(pos.get(w, 0.0) for w in snipers) / total
    return {"buyers": len(buyers), "sellers": len(sellers),
            "holders": len(holders),
            "top1_share": round(shares[0], 4),
            "top10_share": round(sum(shares[:10]), 4),
            "sniper_share": round(sniper_share, 4),
            "swaps": len(rows), "truncated": truncated}


def eligible_tokens():
    """Fresh tokens passing the age30/vol8k entry family, with entry_ts."""
    out = []
    for tok in backtest.load_token_index(FRESH_INDEX):
        candles = backtest.load_cached_candles(".bd_cache", tok.pool)
        if not candles:
            continue
        entry_ts = candles[0][0] + ENTRY_AGE_MIN * 60
        pre = [c for c in candles if c[0] <= entry_ts]
        post = [c for c in candles if c[0] > entry_ts]
        if not pre or not post or pre[-1][4] <= 0:
            continue
        if sum(c[5] for c in pre) < MIN_CUM_VOL:
            continue
        out.append((tok, int(candles[0][0]), int(entry_ts)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", help="fetch one page for MINT and dump the raw item shape")
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--max-requests", type=int, default=8000)
    args = ap.parse_args()
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    session = requests.Session()
    session.headers.update({"X-API-KEY": load_key(), "x-chain": "solana",
                            "accept": "application/json"})

    if args.probe:
        now = int(time.time())
        data = bd_get(session, {"address": args.probe, "tx_type": "swap",
                                "after_time": now - 86400, "offset": 0, "limit": 3},
                      args.max_requests)
        print(json.dumps(data, indent=1)[:3000] if data else "no data")
        return

    todo = eligible_tokens()
    print(f"eligible (age30/vol8k) fresh tokens: {len(todo)}", flush=True)
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            done = json.load(f)
    except (OSError, ValueError):
        done = {}
    fetched = empty = 0
    for i, (tok, created, entry_ts) in enumerate(todo, 1):
        if tok.mint in done:
            continue
        try:
            rows, trunc = fetch_tape(session, tok.mint, created - 60, entry_ts,
                                     args.max_pages, args.max_requests)
        except RuntimeError as exc:
            print(f"stopping early: {exc} (re-run to resume)", flush=True)
            break
        if rows is None:
            continue
        feats = features_from_tape(rows, trunc)
        done[tok.mint] = feats or {"holders": 0, "swaps": 0, "no_tape": True}
        fetched += 1
        if feats is None:
            empty += 1
        if i % 25 == 0:
            with open(OUT_PATH, "w", encoding="utf-8") as f:
                json.dump(done, f)
            print(f"  {i}/{len(todo)} | features {fetched} (empty {empty}) | "
                  f"{_req[0]} reqs", flush=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(done, f)
    print(f"done: {len(done)} tokens with holder features ({empty} empty tapes), "
          f"requests {_req[0]}", flush=True)


if __name__ == "__main__":
    main()
