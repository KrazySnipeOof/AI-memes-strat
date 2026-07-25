#!/usr/bin/env python
"""Mine Birdeye for wallets with repeated large realized multiples.

Candidate wallets come from /trader/gainers-losers (all timeframes) plus our
own top-trader harvest (wallets.sqlite). For each candidate the swap history
(/trader/txs/seek_by_time) is folded into per-token round trips, and wallets
are ranked by how often a completed round trip realized >= 10x. Headline
leaderboard PnL is ignored on purpose: the top "gainers" are unrealized-mark
mirages (observed live 2026-07-24: #1 showed $57.8M pnl, $0.06 realized).

Output: reports/wallet_scout.json (round-trip detail included, so a future
copy-trade backtest can replay entries) + console table.

  python walletscout.py                     scan with defaults
  python walletscout.py --days 14 --max-wallets 40
  python walletscout.py --merge 5           also add top 5 to tracked_wallets.json

Field names verified against live responses 2026-07-24 (gainers-losers:
address/pnl/realized_pnl/trade_count/volume; seek_by_time: base/quote legs
with type_swap from|to, volume_usd, block_unix_time, has_next).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import bdusage
from bot.config import EXCLUDED_MINTS
from bot.util import fnum, iso_now, load_dotenv, make_session

BASE = "https://public-api.birdeye.so"
OUT_PATH = os.path.join("reports", "wallet_scout.json")
TRACKED_PATH = os.path.join("reports", "tracked_wallets.json")
HARVEST_DB = "wallets.sqlite"
PAUSE = 0.12  # Premium is 50 RPS; stay far under it


def headers():
    return {"X-API-KEY": os.environ.get("BIRDEYE_API_KEY", "").strip(), "x-chain": "solana"}


def get(session, path, params):
    r = session.get(f"{BASE}{path}", params=params, headers=headers(), timeout=15)
    bdusage.record(path)
    time.sleep(PAUSE)
    if r.status_code != 200:
        raise RuntimeError(f"http {r.status_code} on {path}")
    return r.json().get("data") or {}


def leaderboard_candidates(session, min_realized, verbose=True):
    """Wallets from gainers-losers with real REALIZED pnl and enough trades."""
    out = {}
    for tf in ("today", "yesterday", "1W"):
        pages = 0
        for offset in range(0, 100, 10):
            try:
                items = get(session, "/trader/gainers-losers",
                            {"type": tf, "sort_by": "PnL", "sort_type": "desc",
                             "offset": offset, "limit": 10}).get("items") or []
            except Exception as exc:
                if verbose:
                    print(f"  gainers-losers {tf} offset {offset}: {exc}")
                break
            pages += 1
            for it in items:
                w = it.get("address")
                realized = fnum(it.get("realized_pnl"))
                trades = int(fnum(it.get("trade_count")))
                if w and realized >= min_realized and trades >= 10:
                    prev = out.get(w, {}).get("realized", 0)
                    out[w] = {"source": f"gainers-{tf}", "realized": max(realized, prev),
                              "trades": trades}
            if len(items) < 10:
                break
        if verbose:
            print(f"  {tf}: {pages} pages scanned, {len(out)} qualified so far")
    return out


def harvest_candidates(min_realized):
    """Wallets our own bot has already seen (wallets.sqlite), by realized pnl."""
    import sqlite3
    if not os.path.exists(HARVEST_DB):
        return {}
    out = {}
    conn = sqlite3.connect(HARVEST_DB)
    try:
        rows = conn.execute(
            "SELECT wallet, MAX(realized_pnl), COUNT(DISTINCT mint) FROM sightings"
            " GROUP BY wallet ORDER BY MAX(realized_pnl) DESC LIMIT 20").fetchall()
    finally:
        conn.close()
    for w, realized, mints in rows:
        if fnum(realized) >= min_realized:
            out[w] = {"source": "our-harvest", "realized": fnum(realized), "trades": mints}
    return out


def swap_history(session, wallet, cutoff_ts, max_pages):
    """Chronological swaps for a wallet back to cutoff_ts."""
    swaps = []
    for page in range(max_pages):
        data = get(session, "/trader/txs/seek_by_time",
                   {"address": wallet, "tx_type": "swap",
                    "offset": page * 50, "limit": 50})
        items = data.get("items") or []
        swaps.extend(items)
        oldest = min((int(fnum(i.get("block_unix_time"))) for i in items), default=0)
        if not data.get("has_next") or not items or oldest < cutoff_ts:
            break
    return [s for s in swaps if int(fnum(s.get("block_unix_time"))) >= cutoff_ts]


def round_trips(swaps):
    """Fold swaps into per-token USD ledgers. A leg with type_swap 'to' means
    the wallet received that token (buy); 'from' means it sold it."""
    ledger = {}  # mint -> dict
    for s in swaps:
        ts = int(fnum(s.get("block_unix_time")))
        usd = fnum(s.get("volume_usd"))
        if usd <= 0:
            continue
        for leg in (s.get("base") or {}, s.get("quote") or {}):
            mint = leg.get("address")
            if not mint or mint in EXCLUDED_MINTS:
                continue
            row = ledger.setdefault(mint, {"symbol": leg.get("symbol") or "?",
                                           "buys_usd": 0.0, "sells_usd": 0.0,
                                           "n_buys": 0, "n_sells": 0,
                                           "first_buy_ts": None, "last_sell_ts": None})
            if leg.get("type_swap") == "to":
                row["buys_usd"] += usd
                row["n_buys"] += 1
                if row["first_buy_ts"] is None or ts < row["first_buy_ts"]:
                    row["first_buy_ts"] = ts
            elif leg.get("type_swap") == "from":
                row["sells_usd"] += usd
                row["n_sells"] += 1
                if row["last_sell_ts"] is None or ts > row["last_sell_ts"]:
                    row["last_sell_ts"] = ts
    trips = []
    for mint, r in ledger.items():
        if r["buys_usd"] >= 50 and r["n_sells"] > 0:  # completed, non-dust round trip
            trips.append({"mint": mint, "symbol": r["symbol"],
                          "buys_usd": round(r["buys_usd"], 2),
                          "sells_usd": round(r["sells_usd"], 2),
                          "multiple": round(r["sells_usd"] / r["buys_usd"], 3),
                          "n_buys": r["n_buys"], "n_sells": r["n_sells"],
                          "first_buy_ts": r["first_buy_ts"],
                          "last_sell_ts": r["last_sell_ts"]})
    return sorted(trips, key=lambda t: -t["multiple"])


def score(wallet, meta, trips):
    n = len(trips)
    if n == 0:
        return None
    wins = sum(1 for t in trips if t["multiple"] >= 1.0)
    return {
        "wallet": wallet, "source": meta["source"],
        "leaderboard_realized_usd": round(meta["realized"], 2),
        "round_trips": n,
        "win_rate": round(100 * wins / n, 1),
        "tenx": sum(1 for t in trips if t["multiple"] >= 10),
        "fivex": sum(1 for t in trips if t["multiple"] >= 5),
        "twox": sum(1 for t in trips if t["multiple"] >= 2),
        "best_multiple": trips[0]["multiple"],
        "total_bought_usd": round(sum(t["buys_usd"] for t in trips), 2),
        "trips": trips,
    }


def merge_tracked(scored, top_n):
    with open(TRACKED_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    have = {w["wallet"] for w in data["wallets"]}
    added = []
    for s in scored[:top_n]:
        if s["wallet"] in have:
            continue
        data["wallets"].append({
            "name": f"scout-{s['tenx']}x10-{s['wallet'][:4]}",
            "wallet": s["wallet"], "source": "birdeye-scout",
            "added": iso_now()[:10],
            "note": f"{s['round_trips']} round trips, {s['tenx']}x >=10x, "
                    f"{s['win_rate']}% wins (scout {iso_now()[:10]})"})
        added.append(s["wallet"])
    if added:
        with open(TRACKED_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
    return added


def main():
    ap = argparse.ArgumentParser(description="Scout consistently-profitable wallets via Birdeye")
    ap.add_argument("--days", type=int, default=14, help="history window per wallet")
    ap.add_argument("--max-wallets", type=int, default=40)
    ap.add_argument("--max-pages", type=int, default=6, help="swap pages per wallet (50/page)")
    ap.add_argument("--min-realized", type=float, default=5_000,
                    help="min leaderboard realized pnl USD to deep-scan")
    ap.add_argument("--merge", type=int, default=0,
                    help="add top N scouted wallets to tracked_wallets.json")
    args = ap.parse_args()

    load_dotenv()
    session = make_session()
    cutoff = int(time.time()) - args.days * 86400

    print("phase 1: candidates")
    cands = leaderboard_candidates(session, args.min_realized)
    for w, meta in harvest_candidates(args.min_realized).items():
        cands.setdefault(w, meta)
    ranked = sorted(cands.items(), key=lambda kv: -kv[1]["realized"])[:args.max_wallets]
    print(f"  deep-scanning {len(ranked)} wallets ({args.days}d history each)")

    print("phase 2: swap history -> round trips")
    scored = []
    for i, (w, meta) in enumerate(ranked):
        try:
            trips = round_trips(swap_history(session, w, cutoff, args.max_pages))
            s = score(w, meta, trips)
            if s:
                scored.append(s)
            print(f"  [{i+1}/{len(ranked)}] {w[:8]}..  trips={len(trips)}"
                  f" tenx={s['tenx'] if s else 0} src={meta['source']}")
        except Exception as exc:
            print(f"  [{i+1}/{len(ranked)}] {w[:8]}..  FAILED: {exc}")

    scored.sort(key=lambda s: (-s["tenx"], -s["win_rate"], -s["round_trips"]))
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump({"generated": iso_now(), "days": args.days,
                   "note": "realized round-trip multiples from swap history; "
                           "unrealized leaderboard pnl deliberately ignored. "
                           "trips[] carries first_buy_ts per token for copy-trade backtests.",
                   "wallets": scored}, fh, indent=1)
    print(f"\nwrote {OUT_PATH} ({len(scored)} wallets)")

    print(f"\n{'wallet':<46}{'trips':>6}{'win%':>7}{'10x':>5}{'5x':>5}{'2x':>5}{'best':>9}  source")
    for s in scored[:15]:
        print(f"{s['wallet']:<46}{s['round_trips']:>6}{s['win_rate']:>7}{s['tenx']:>5}"
              f"{s['fivex']:>5}{s['twox']:>5}{s['best_multiple']:>9}  {s['source']}")

    if args.merge:
        added = merge_tracked(scored, args.merge)
        print(f"\nmerged {len(added)} new wallets into {TRACKED_PATH}: "
              + ", ".join(w[:8] + ".." for w in added))


if __name__ == "__main__":
    main()
