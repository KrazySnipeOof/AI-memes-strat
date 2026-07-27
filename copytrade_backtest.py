#!/usr/bin/env python
"""Copy-trade backtest over the scouted wallets (reports/wallet_scout.json).

For every round trip a scouted wallet made, simulate a FOLLOWER who sees the
wallet's buy and enters some minutes later (latency is the whole game with
snipers). Two exit models per lag:

  MIRROR   - sell when the wallet sold (their last_sell_ts + the same lag)
  STRATEGY - apply this bot's own exit doctrine (config.asym.json exits) via
             backtest.simulate, so the follower isn't tied to the leader's exit

Contrast target: the leader's own realized multiple vs what a follower can
actually capture at 0/5/15/30/60-minute lag. The gap is the "exit-liquidity
tax" - how much of the move is already gone by the time you can copy it.

Also derives, per wallet: hold time, token age at entry (lower bound from the
fetched window), scale-in behavior, multiple distribution; and across wallets:
co-accumulation clusters (tokens >=2 tracked wallets bought) and which wallet
leads the pack - the real, latency-tolerant "front-run" signal.

  python copytrade_backtest.py                 fetch + run, all scouted wallets
  python copytrade_backtest.py --top 8         only the 8 best-ranked wallets
  python copytrade_backtest.py --cost 3        round-trip cost assumption (%)

OHLCV: Birdeye /defi/v3/ohlcv, fields per bot/birdeye_ws.backfill (verified
live). Cached per token+resolution under .copytrade_cache/. CU logged to
bdusage.
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
from backtest import simulate
from bot.util import load_dotenv, make_session

SCOUT_PATH = os.path.join("reports", "wallet_scout.json")
OUT_PATH = os.path.join("reports", "copytrade_backtest.json")
PROGRESS_PATH = os.path.join("reports", "copytrade_progress.json")
CACHE_DIR = ".copytrade_cache"
REST = "https://public-api.birdeye.so"
LAGS_MIN = [0, 5, 15, 30, 60]
PAUSE = 0.14
MAX_HOLD_MIN = 480  # must cover the strategy doctrine's max hold for a fair sim


def headers():
    return {"x-api-key": os.environ.get("BIRDEYE_API_KEY", "").strip(), "x-chain": "solana"}


def write_progress(phase, done, total, status="running", started=None):
    """Emit reports/copytrade_progress.json so the WALLETS tab can draw a live
    progress bar. Cheap best-effort write; a failure never stops the run."""
    try:
        pct = round(100 * done / total, 1) if total else (100.0 if status == "done" else 0.0)
        os.makedirs("reports", exist_ok=True)
        tmp = PROGRESS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"phase": phase, "done": done, "total": total, "pct": pct,
                       "status": status, "started": started,
                       "updated": int(time.time())}, f)
        os.replace(tmp, PROGRESS_PATH)
    except OSError:
        pass


def _resolution(span_min: float):
    """Pick the finest Birdeye candle type that keeps the window under ~950
    rows (their per-call item cap is ~1000)."""
    for typ, m in (("1m", 1), ("5m", 5), ("15m", 15), ("30m", 30), ("1H", 60)):
        if span_min / m <= 950:
            return typ, m
    return "4H", 240


def fetch_ohlcv(session, mint, time_from, time_to):
    """Ascending [[ts,o,h,l,c,v_usd],...] for a token, cached per resolution."""
    span_min = (time_to - time_from) / 60
    typ, cmin = _resolution(span_min)
    cpath = os.path.join(CACHE_DIR, f"{mint}_{typ}.json")
    if os.path.exists(cpath):
        try:
            with open(cpath, "r", encoding="utf-8") as f:
                c = json.load(f)
            if c.get("candles"):
                return c["candles"], c["cmin"]
        except (OSError, ValueError):
            pass
    rows = []
    for attempt in range(2):
        try:
            bdusage.record("/defi/v3/ohlcv")
            r = session.get(f"{REST}/defi/v3/ohlcv",
                            params={"address": mint, "type": typ, "currency": "usd",
                                    "time_from": int(time_from), "time_to": int(time_to)},
                            headers=headers(), timeout=25)
            items = ((r.json().get("data") or {}).get("items")) or []
            for it in items:
                try:
                    ts = int(it.get("unix_time", it.get("unixTime")))
                    o, h, l, cl = float(it["o"]), float(it["h"]), float(it["l"]), float(it["c"])
                    v = it.get("v_usd", it.get("vUsd"))
                    v = float(v) if v is not None else float(it.get("v", 0)) * cl
                    rows.append([ts, o, h, l, cl, v])
                except (KeyError, TypeError, ValueError):
                    continue
            break
        except Exception:
            time.sleep(0.5)
    rows.sort(key=lambda c: c[0])
    time.sleep(PAUSE)
    if rows:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump({"candles": rows, "cmin": cmin}, f)
    return rows, cmin


def price_at(candles, ts):
    """Close of the last candle at or before ts; None if ts precedes all."""
    prev = None
    for c in candles:
        if c[0] <= ts:
            prev = c[4]
        else:
            break
    return prev


def mirror_multiple(candles, entry_ts, exit_ts, cost_pct):
    ep = price_at(candles, entry_ts)
    xp = price_at(candles, exit_ts)
    if not ep or ep <= 0:
        return None
    if xp is None:  # exit beyond data -> mark at last candle
        xp = candles[-1][4]
    return (xp / ep) * (1 - cost_pct / 100)


def collect_trips(scout, top):
    wallets = scout["wallets"][:top] if top else scout["wallets"]
    trips = []
    for w in wallets:
        for t in w.get("trips", []):
            if t.get("first_buy_ts") and t.get("last_sell_ts"):
                trips.append({"wallet": w["wallet"], "mint": t["mint"],
                              "symbol": t.get("symbol", "?"),
                              "first_buy_ts": int(t["first_buy_ts"]),
                              "last_sell_ts": int(t["last_sell_ts"]),
                              "n_buys": t.get("n_buys", 1),
                              "leader_mult": t.get("multiple", 0)})
    return wallets, trips


def stats_block(mults):
    if not mults:
        return None
    wins = [m for m in mults if m > 1.0]
    stake = 0.25
    pnl = sum((m - 1) * stake for m in mults)
    return {"n": len(mults), "win_rate": round(100 * len(wins) / len(mults), 1),
            "avg": round(statistics.mean(mults), 3),
            "median": round(statistics.median(mults), 3),
            "total_pnl_sol": round(pnl, 4),
            "best": round(max(mults), 2), "worst": round(min(mults), 3)}


def main():
    ap = argparse.ArgumentParser(description="Copy-trade backtest over scouted wallets")
    ap.add_argument("--top", type=int, default=0, help="limit to top-N ranked wallets")
    ap.add_argument("--cost", type=float, default=3.0, help="round-trip cost %% assumption")
    args = ap.parse_args()

    started = int(time.time())
    write_progress("starting", 0, 0, started=started)
    load_dotenv()
    session = make_session()
    scout = json.load(open(SCOUT_PATH, encoding="utf-8"))
    exits = json.load(open("config.asym.json", encoding="utf-8"))["exits"]

    wallets, trips = collect_trips(scout, args.top)
    print(f"loaded {len(trips)} round trips across {len(wallets)} wallets")

    # union window per token so multi-wallet tokens fetch once
    win = {}
    for t in trips:
        lo, hi = win.get(t["mint"], (t["first_buy_ts"], t["last_sell_ts"]))
        win[t["mint"]] = (min(lo, t["first_buy_ts"]), max(hi, t["last_sell_ts"]))

    print(f"fetching OHLCV for {len(win)} unique tokens ...")
    candles_by_mint, first_ts = {}, {}
    miss = 0
    for i, (mint, (lo, hi)) in enumerate(win.items()):
        tf = lo - 60 * 60
        tt = max(hi, lo + MAX_HOLD_MIN * 60) + 120 * 60
        cs, _ = fetch_ohlcv(session, mint, tf, tt)
        if cs:
            candles_by_mint[mint] = cs
            first_ts[mint] = cs[0][0]
        else:
            miss += 1
        write_progress("fetch", i + 1, len(win), started=started)
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(win)}  ({miss} no-data)")
    print(f"coverage: {len(candles_by_mint)}/{len(win)} tokens have candles ({miss} missing)")

    # ---- per-trip simulation across lags ----
    per_lag_mirror = {L: [] for L in LAGS_MIN}
    per_lag_strat = {L: [] for L in LAGS_MIN}
    leader_from_candles = []
    per_wallet = defaultdict(lambda: {"mirror0": [], "strat15": [], "holds": [],
                                      "ages": [], "n_buys": [], "leader": []})
    trip_rows = []

    for si, t in enumerate(trips):
        if si % 10 == 0:
            write_progress("simulate", si, len(trips), started=started)
        cs = candles_by_mint.get(t["mint"])
        if not cs:
            continue
        fb, ls = t["first_buy_ts"], t["last_sell_ts"]
        hold_min = (ls - fb) / 60
        age_min = (fb - first_ts[t["mint"]]) / 60  # lower bound (window-clipped)
        lead = mirror_multiple(cs, fb, ls, args.cost)  # match them exactly
        if lead is not None:
            leader_from_candles.append(lead)
        pw = per_wallet[t["wallet"]]
        pw["holds"].append(hold_min); pw["ages"].append(age_min)
        pw["n_buys"].append(t["n_buys"]); pw["leader"].append(t["leader_mult"])

        row = {"wallet": t["wallet"], "symbol": t["symbol"], "hold_min": round(hold_min, 1),
               "leader_mult": t["leader_mult"], "lags": {}}
        for L in LAGS_MIN:
            e_ts = fb + L * 60
            m = mirror_multiple(cs, e_ts, ls + L * 60, args.cost)
            if m is not None:
                per_lag_mirror[L].append(m)
                if L == 0:
                    pw["mirror0"].append(m)
            age_at_entry = (e_ts - first_ts[t["mint"]]) / 60
            sr = simulate(cs, exits, age_at_entry, args.cost, min_entry_vol=0, setup=None)
            s = sr["multiple"] if sr else None
            if s is not None:
                per_lag_strat[L].append(s)
                if L == 15:
                    pw["strat15"].append(s)
            row["lags"][L] = {"mirror": round(m, 3) if m is not None else None,
                              "strategy": round(s, 3) if s is not None else None}
        trip_rows.append(row)

    # ---- co-accumulation / lead analysis ----
    by_mint = defaultdict(list)
    for t in trips:
        if t["mint"] in candles_by_mint:
            by_mint[t["mint"]].append(t)
    clusters = []
    lead_counts = defaultdict(int)
    for mint, ts_list in by_mint.items():
        uniq = {}
        for t in ts_list:  # earliest buy per wallet on this token
            if t["wallet"] not in uniq or t["first_buy_ts"] < uniq[t["wallet"]]["first_buy_ts"]:
                uniq[t["wallet"]] = t
        if len(uniq) >= 2:
            order = sorted(uniq.values(), key=lambda x: x["first_buy_ts"])
            lead_counts[order[0]["wallet"]] += 1
            gap_min = (order[1]["first_buy_ts"] - order[0]["first_buy_ts"]) / 60
            clusters.append({"symbol": order[0]["symbol"], "n_wallets": len(uniq),
                             "leader": order[0]["wallet"], "gap_to_2nd_min": round(gap_min, 1),
                             "leader_mult": order[0]["leader_mult"]})

    solo_mults = [t["leader_mult"] for m, l in by_mint.items() if len({x["wallet"] for x in l}) == 1 for t in l]
    cluster_mults = [t["leader_mult"] for m, l in by_mint.items() if len({x["wallet"] for x in l}) >= 2 for t in l]

    wallet_summ = []
    for w in wallets:
        pw = per_wallet.get(w["wallet"])
        if not pw or not pw["holds"]:
            continue
        wallet_summ.append({
            "wallet": w["wallet"], "trips_covered": len(pw["holds"]),
            "median_hold_min": round(statistics.median(pw["holds"]), 1),
            "median_age_at_entry_min": round(statistics.median(pw["ages"]), 1),
            "avg_n_buys": round(statistics.mean(pw["n_buys"]), 2),
            "median_leader_mult": round(statistics.median(pw["leader"]), 2),
            "follower_mirror_lag0": stats_block(pw["mirror0"]),
            "follower_strategy_lag15": stats_block(pw["strat15"]),
            "leads_cluster": lead_counts.get(w["wallet"], 0),
        })

    out = {
        "generated": scout.get("generated"), "cost_pct": args.cost,
        "trips_total": len(trips), "trips_covered": len(trip_rows),
        "leader_matched_exact": stats_block(leader_from_candles),
        "follower_mirror_by_lag": {L: stats_block(per_lag_mirror[L]) for L in LAGS_MIN},
        "follower_strategy_by_lag": {L: stats_block(per_lag_strat[L]) for L in LAGS_MIN},
        "cluster": {"n_clusters": len(clusters),
                    "solo_token_avg_leader_mult": round(statistics.mean(solo_mults), 2) if solo_mults else None,
                    "cluster_token_avg_leader_mult": round(statistics.mean(cluster_mults), 2) if cluster_mults else None,
                    "lead_counts": dict(lead_counts), "clusters": clusters[:20]},
        "wallets": wallet_summ,
        "trips": trip_rows,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    write_progress("done", len(trips), len(trips), status="done", started=started)

    # ---- console report ----
    def line(label, s):
        if not s:
            print(f"  {label:<26} (no data)"); return
        print(f"  {label:<26} n={s['n']:<4} WR {s['win_rate']:>5}%  avg {s['avg']:>7}x"
              f"  med {s['median']:>6}x  PnL {s['total_pnl_sol']:>8} SOL  best {s['best']}x")

    print(f"\n=== COPY-TRADE BACKTEST (cost {args.cost}%, {len(trip_rows)} trips w/ candles) ===")
    print("\nLEADER (match their exact entry & exit from candles):")
    line("leader exact", out["leader_matched_exact"])
    print("\nFOLLOWER - MIRROR exit (sell when they sold), by entry lag:")
    for L in LAGS_MIN:
        line(f"mirror +{L}m", out["follower_mirror_by_lag"][L])
    print("\nFOLLOWER - STRATEGY exit (your doctrine), by entry lag:")
    for L in LAGS_MIN:
        line(f"strategy +{L}m", out["follower_strategy_by_lag"][L])
    print(f"\nCO-ACCUMULATION: {len(clusters)} tokens bought by >=2 tracked wallets")
    print(f"  solo-token avg leader mult   : {out['cluster']['solo_token_avg_leader_mult']}x")
    print(f"  cluster-token avg leader mult: {out['cluster']['cluster_token_avg_leader_mult']}x")
    if lead_counts:
        top_lead = sorted(lead_counts.items(), key=lambda kv: -kv[1])[:5]
        print("  most-often first into a cluster:")
        for w, n in top_lead:
            print(f"    {w[:8]}..  led {n} clusters")

    print(f"\nPER-WALLET (follower strategy @ +15m lag):")
    print(f"  {'wallet':<12}{'trips':>6}{'medHold':>9}{'medAge':>8}{'nBuys':>7}{'leadMlt':>9}{'F-WR':>7}{'F-avg':>8}{'leads':>7}")
    for w in sorted(wallet_summ, key=lambda x: -(x['follower_strategy_lag15'] or {}).get('avg', 0)):
        fs = w["follower_strategy_lag15"] or {}
        print(f"  {w['wallet'][:10]:<12}{w['trips_covered']:>6}{w['median_hold_min']:>9.0f}"
              f"{w['median_age_at_entry_min']:>8.0f}{w['avg_n_buys']:>7.1f}{w['median_leader_mult']:>9.1f}"
              f"{fs.get('win_rate', 0):>6}%{fs.get('avg', 0):>8}{w['leads_cluster']:>7}")
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()
