#!/usr/bin/env python
"""Compact binary universe cache for fast strategy search.

Reading 10.7k JSON candle files takes ~90s. This flattens the whole cached
universe into one pickle of numpy arrays so a search loop can re-scan it in
under a second. Blocklist and manual-flag rules are applied here exactly as
`backtest.load_token_index` applies them, so the search sees the same universe
every other consumer sees.

Usage:  python universe.py            # build (or rebuild) the cache
        from universe import load     # in a search script
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CACHE_DIR = os.path.join(HERE, ".bd_cache")
OUT = os.path.join(os.environ.get("CLAUDE_JOB_DIR", HERE), "tmp", "universe.pkl")

INDEXES = ["bd_tokens.json", "bd_tokens_60d.json", "bd_tokens_fresh.json",
           "bd_tokens_oot.json"]

# Deliberately low. The median cached token has 22 one-minute candles - it dies
# almost immediately - so a high floor here would quietly delete the fastest
# deaths from the universe and flatter every strategy measured on it. The only
# real requirement is "enough candles to have a pre-entry history and at least
# one candle after entry"; the entry-age filter enforces the rest per-run.
MIN_CANDLES = 8


def _read_one(args):
    mint, pool, symbol, created, source = args
    path = os.path.join(CACHE_DIR, f"{pool}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            c = json.load(f).get("candles")
    except (OSError, ValueError):
        return None
    if not c or len(c) < MIN_CANDLES:
        return None
    a = np.asarray(c, dtype=np.float64)
    if a.ndim != 2 or a.shape[1] != 6:
        return None
    # ascending by ts, drop non-positive prices
    a = a[np.argsort(a[:, 0])]
    if not np.all(a[:, 1:5] > 0):
        keep = np.all(a[:, 1:5] > 0, axis=1)
        a = a[keep]
        if len(a) < MIN_CANDLES:
            return None
    return {"mint": mint, "pool": pool, "symbol": symbol,
            "created": created or int(a[0, 0]), "source": source,
            "ts": a[:, 0].astype(np.int64), "ohlcv": a[:, 1:].astype(np.float32)}


def build() -> dict:
    import backtest as bt

    blocked = bt.load_blocklist()
    mint_ts, sym_ts = bt._manual_cutoffs(bt.load_manual_flags())

    seen, jobs = set(), []
    for name in INDEXES:
        p = os.path.join(HERE, "reports", name)
        if not os.path.exists(p):
            continue
        for t in json.load(open(p, encoding="utf-8")):
            mint = t["mint"]
            if mint in seen or mint in blocked:
                continue
            created = int(t["created_ts"])
            sym = str(t.get("symbol") or "").strip().upper()
            cut, scut = mint_ts.get(mint), sym_ts.get(sym)
            if (cut is not None and created > cut) or (scut is not None and created > scut):
                continue
            seen.add(mint)
            jobs.append((mint, t["pool"], t.get("symbol", "?"), created,
                         t.get("source", "?")))

    # Cached pools that no index lists. They were fetched by other workstreams
    # (copy-trade wallet buys, home-run screens), so they are a SELECTED sample,
    # not part of the broad listing universe. Tagged "offindex" and kept apart:
    # useful as the candidate pool for a wallet-follow family, never mixed into
    # a broad-universe measurement.
    idx_pools = {j[1] for j in jobs}
    off = 0
    for fn in os.listdir(CACHE_DIR):
        pool = fn[:-5]
        if not fn.endswith(".json") or pool in idx_pools or pool in blocked:
            continue
        jobs.append((pool, pool, "?", 0, "offindex"))
        off += 1

    print(f"index: {len(jobs) - off} indexed + {off} off-index cached pools; reading candles...")
    toks = []
    with ProcessPoolExecutor(max_workers=10) as ex:
        for r in ex.map(_read_one, jobs, chunksize=64):
            if r is not None:
                toks.append(r)
    toks.sort(key=lambda t: t["created"])
    print(f"universe: {len(toks)} tokens with >={MIN_CANDLES} cached candles")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as f:
        pickle.dump(toks, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"wrote {OUT} ({os.path.getsize(OUT) / 1e6:.0f} MB)")
    return toks


def load():
    if not os.path.exists(OUT):
        return build()
    with open(OUT, "rb") as f:
        return pickle.load(f)


if __name__ == "__main__":
    u = build()
    import statistics
    spans = [(t["ts"][-1] - t["ts"][0]) / 3600 for t in u]
    print(f"candle span hours: median {statistics.median(spans):.2f} "
          f"p75 {sorted(spans)[int(.75 * len(spans))]:.2f} "
          f"p95 {sorted(spans)[int(.95 * len(spans))]:.2f}")
    print(f"created_ts range: {min(t['created'] for t in u)} .. {max(t['created'] for t in u)}")
