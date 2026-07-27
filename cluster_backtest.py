#!/usr/bin/env python
"""Unbiased cluster-confirmation backtest.

The copy-trade backtest (copytrade_backtest.py) was optimistic: its token set
was the scout's COMPLETED profitable round trips - the wallets' winners. This
one pulls EVERY buy each monitored wallet made from its raw swap stream
(/trader/txs/seek_by_time), so bags and rugs are in the universe too, then asks
one question:

    does requiring a 2nd tracked wallet to confirm a token (co-accumulation)
    beat just copying every buy - on the same honest, unfiltered set?

Trade sets (one entry per token, this bot's exit doctrine via backtest.simulate):
  ALLBUYS     enter at the 1st monitored buy of every token          (copy everything)
  SOLO        tokens only one monitored wallet bought                (control, worst)
  CLUSTER_T1  tokens >=2 wallets bought within W, enter at 1st buy   (optimistic - needs prediction)
  CLUSTER_T2  same tokens, enter at the 2nd (confirming) buy         (the realistic, tradeable rule)
  CLUSTER_T2 + setup gate  as above, gated by the live base-entry filter

If CLUSTER_T2 expectancy > ALLBUYS > SOLO and clears cost, the pattern earns a
shadow slot in the paper trial. If not, it dies here - cheaply.

  python cluster_backtest.py --collect-only     size the universe, no OHLCV
  python cluster_backtest.py --window-min 60     tighter co-accumulation window
  python cluster_backtest.py --cost 3
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import bdusage
from backtest import DEFAULT_SETUP, simulate
from bot.config import EXCLUDED_MINTS
from bot.util import load_dotenv, make_session
from copytrade_backtest import fetch_ohlcv, stats_block

SCOUT_PATH = os.path.join("reports", "wallet_scout.json")
OUT_PATH = os.path.join("reports", "cluster_backtest.json")
REST = "https://public-api.birdeye.so"
LAGS = [0, 5, 15, 30]
MAX_HOLD_MIN = 480
PAUSE = 0.14


def headers():
    return {"x-api-key": os.environ.get("BIRDEYE_API_KEY", "").strip(), "x-chain": "solana"}


def wallet_buys(session, wallet, cutoff_ts, max_pages=12):
    """Earliest buy per token for one wallet over the retained history window.
    A 'buy of T' = the swap leg with type_swap 'to' whose token isn't SOL/stable
    (they acquired T). Time-paged via before_time (offset paging caps at ~62)."""
    out = {}  # mint -> {"ts": earliest, "symbol": sym}
    before = None
    for _ in range(max_pages):
        params = {"address": wallet, "tx_type": "swap", "limit": 100}
        if before:
            params["before_time"] = before
        try:
            bdusage.record("/trader/txs/seek_by_time")
            r = session.get(f"{REST}/trader/txs/seek_by_time", params=params,
                            headers=headers(), timeout=20)
            items = ((r.json().get("data") or {}).get("items")) or []
        except Exception:
            break
        if not items:
            break
        oldest = None
        for it in items:
            ts = it.get("block_unix_time")
            if ts is None:
                continue
            ts = int(ts)
            oldest = ts if oldest is None else min(oldest, ts)
            for leg in (it.get("base") or {}, it.get("quote") or {}):
                if leg.get("type_swap") != "to":
                    continue
                mint = leg.get("address")
                if not mint or mint in EXCLUDED_MINTS:
                    continue
                cur = out.get(mint)
                if cur is None or ts < cur["ts"]:
                    out[mint] = {"ts": ts, "symbol": leg.get("symbol") or "?"}
        time.sleep(PAUSE)
        if oldest is None or oldest <= cutoff_ts:
            break
        before = oldest
    return {m: v for m, v in out.items() if v["ts"] >= cutoff_ts}


def run_set(name, entries, candles_by_mint, first_ts, exits, cost, setup=None):
    """entries: list of (mint, entry_ts). Returns (stats, raw): per-lag
    stats_block and per-lag raw follower-multiple lists (the raw lists feed the
    Monte Carlo strategy pools), one entry per token."""
    per_lag = {L: [] for L in LAGS}
    for mint, e0 in entries:
        cs = candles_by_mint.get(mint)
        if not cs:
            continue
        for L in LAGS:
            e_ts = e0 + L * 60
            age = (e_ts - first_ts[mint]) / 60
            if age < 0:
                continue
            sr = simulate(cs, exits, age, cost, min_entry_vol=0, setup=setup)
            if sr:
                per_lag[L].append(sr["multiple"])
    return ({L: stats_block(per_lag[L]) for L in LAGS},
            {L: [round(x, 6) for x in per_lag[L]] for L in LAGS})


def main():
    ap = argparse.ArgumentParser(description="Unbiased cluster-confirmation backtest")
    ap.add_argument("--window-min", type=float, default=180,
                    help="max minutes between 1st and 2nd buy to count as a cluster")
    ap.add_argument("--days", type=int, default=14, help="history cutoff (API retains less)")
    ap.add_argument("--cost", type=float, default=3.0)
    ap.add_argument("--collect-only", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    session = make_session()
    scout = json.load(open(SCOUT_PATH, encoding="utf-8"))
    pool = [w["wallet"] for w in scout["wallets"]]
    cutoff = int(time.time()) - args.days * 86400
    exits = json.load(open("config.asym.json", encoding="utf-8"))["exits"]
    W = args.window_min

    print(f"pulling raw buy history for {len(pool)} wallets ...")
    buys_by_mint = defaultdict(dict)  # mint -> {wallet: ts}
    symbol = {}
    per_wallet_n = []
    for i, w in enumerate(pool):
        b = wallet_buys(session, w, cutoff)
        per_wallet_n.append(len(b))
        for mint, v in b.items():
            prev = buys_by_mint[mint].get(w)
            if prev is None or v["ts"] < prev:
                buys_by_mint[mint][w] = v["ts"]
            symbol.setdefault(mint, v["symbol"])
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(pool)} wallets, {len(buys_by_mint)} tokens so far")
    print(f"total unique tokens bought: {len(buys_by_mint)}"
          f" | median buys/wallet: {statistics.median(per_wallet_n):.0f}")

    # classify
    solo, cluster = [], []  # (mint, entry_ts); cluster keeps (mint, t1, t2)
    loose = 0
    for mint, wmap in buys_by_mint.items():
        order = sorted(wmap.values())
        if len(wmap) == 1:
            solo.append((mint, order[0]))
        elif order[1] - order[0] <= W * 60:
            cluster.append((mint, order[0], order[1]))
        else:
            loose += 1
    allbuys = [(mint, min(wmap.values())) for mint, wmap in buys_by_mint.items()]
    print(f"\nUNIVERSE (window {W:.0f}m): {len(allbuys)} tokens | "
          f"{len(solo)} solo | {len(cluster)} cluster(>=2 in window) | {loose} loose(>W apart)")
    print(f"cluster rate: {100*len(cluster)/len(allbuys):.1f}% of tokens")

    if args.collect_only:
        return

    # fetch OHLCV for every token we simulate (allbuys covers all)
    mints = list(buys_by_mint.keys())
    print(f"\nfetching OHLCV for {len(mints)} tokens (cached where possible) ...")
    candles_by_mint, first_ts, miss = {}, {}, 0
    for i, mint in enumerate(mints):
        first_buy = min(buys_by_mint[mint].values())
        tf = first_buy - 3600
        tt = first_buy + int((W + MAX_HOLD_MIN + 120) * 60)
        cs, _ = fetch_ohlcv(session, mint, tf, tt)
        if cs:
            candles_by_mint[mint] = cs
            first_ts[mint] = cs[0][0]
        else:
            miss += 1
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(mints)}  ({miss} no-data)")
    print(f"coverage: {len(candles_by_mint)}/{len(mints)} tokens ({miss} missing)")

    # ORGANIC-only copy variants (manipscan): keep only tokens that look
    # human. HIND = full-history class (LOOK-AHEAD - knows if it rugged later,
    # an upper bound). LIVE = class judged from candles up to the trigger buy
    # only (entry-time honest, the tradeable version).
    import manipscan
    cluster_mints = {m for m, _, _ in cluster}
    organic_hind, organic_live = [], []
    for m, t1 in allbuys:
        cs = candles_by_mint.get(m)
        if not cs:
            continue
        if (manipscan.classify(cs, m in cluster_mints) or {}).get("class") == "ORGANIC":
            organic_hind.append((m, t1))
        if (manipscan.classify(cs, m in cluster_mints, up_to_ts=t1) or {}).get("class") == "ORGANIC":
            organic_live.append((m, t1))
    print(f"organic filter: {len(organic_hind)}/{len(allbuys)} pass HINDSIGHT, "
          f"{len(organic_live)}/{len(allbuys)} pass AT-ENTRY (of {len(allbuys)} all-buys)")

    specs = [
        ("ALLBUYS", allbuys, None),
        ("SOLO", solo, None),
        ("COPY_ORGANIC_HIND", organic_hind, None),
        ("COPY_ORGANIC_LIVE", organic_live, None),
        ("CLUSTER_T1", [(m, t1) for m, t1, t2 in cluster], None),
        ("CLUSTER_T2", [(m, t2) for m, t1, t2 in cluster], None),
        ("CLUSTER_T2_SETUP", [(m, t2) for m, t1, t2 in cluster], DEFAULT_SETUP),
    ]
    sets, raws = {}, {}
    for nm, entries, setup in specs:
        st, rw = run_set(nm, entries, candles_by_mint, first_ts, exits, args.cost, setup=setup)
        sets[nm], raws[nm] = st, rw

    out = {"generated": scout.get("generated"), "window_min": W, "cost_pct": args.cost,
           "universe": {"tokens": len(allbuys), "solo": len(solo), "cluster": len(cluster),
                        "loose": loose, "coverage": len(candles_by_mint)},
           "sets": sets, "raw": raws,
           # per-trade records so tradecards.py can render journal cards for
           # the copy/cluster strategies (trigger_ts = the buy the follower
           # reacts to; the card adds the follow lag).
           "card_trades": {
               "copy_all": [{"mint": m, "symbol": symbol.get(m, "?"), "trigger_ts": t1}
                            for m, t1 in allbuys if m in candles_by_mint],
               "cluster": [{"mint": m, "symbol": symbol.get(m, "?"), "trigger_ts": t2}
                           for m, t1, t2 in cluster if m in candles_by_mint],
           }}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)

    # ---- report ----
    def line(label, s):
        if not s:
            print(f"  {label:<22} (no data)"); return
        exp = (s["avg"] - 1) * 100
        print(f"  {label:<22} n={s['n']:<4} WR {s['win_rate']:>5}%  avg {s['avg']:>6}x"
              f"  med {s['median']:>6}x  exp {exp:>+6.1f}%/trade  PnL {s['total_pnl_sol']:>8} SOL")

    print(f"\n=== UNBIASED CLUSTER-CONFIRMATION BACKTEST (cost {args.cost}%, window {W:.0f}m) ===")
    for name in ("ALLBUYS", "SOLO", "COPY_ORGANIC_HIND", "COPY_ORGANIC_LIVE",
                 "CLUSTER_T1", "CLUSTER_T2", "CLUSTER_T2_SETUP"):
        print(f"\n{name}:")
        for L in LAGS:
            line(f"+{L}m lag", sets[name][L])
    print(f"\nwrote {OUT_PATH}")
    print("\nVERDICT CHECK: compare CLUSTER_T2 vs ALLBUYS vs SOLO expectancy at +5/+15m.")
    print("Cluster earns a shadow slot only if CLUSTER_T2 > ALLBUYS > SOLO and clears cost.")
    print("\nORGANIC FILTER CHECK (+5m): does 'only enter human-looking' beat ALLBUYS?")
    for name in ("ALLBUYS", "COPY_ORGANIC_HIND", "COPY_ORGANIC_LIVE"):
        s = sets[name].get(5)
        if s:
            print(f"  {name:<20} n={s['n']:<4} WR {s['win_rate']:>5}%  avg {s['avg']:>6}x"
                  f"  exp {100*(s['avg']-1):>+6.1f}%/trade")
    print("  HIND is look-ahead (upper bound); LIVE (class-at-trigger) is the honest, tradeable number.")


if __name__ == "__main__":
    main()
