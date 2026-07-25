#!/usr/bin/env python
"""Monte Carlo equity-path simulation for the adopted strategy
(asym-runner v2 exits + base-entry setup filter).

Resamples the REAL per-trade multiples from the validated backtest sample
(regenerated locally from .bd_cache — zero API calls) and simulates thousands
of equity paths to answer: given this trade distribution, what does the spread
of account outcomes look like over the next N trades?

Risk analysis only — it does not touch strategy parameters (locked rule:
do not retune on this sample; paper trading is the arbiter).

Scenarios:
  iid      IID bootstrap of trade multiples, fixed 0.25 SOL stake (live config)
  block10  circular block bootstrap (block=10) — preserves hot/cold clustering
  stress   IID with an extra per-trade cost haircut (worse live fills)
  no_top   IID with the top 5% winners removed (the runners don't repeat)
  frac5    IID with a 5%-of-equity compounding stake instead of fixed

Usage: python montecarlo.py [--paths 10000] [--horizon 100] [--refresh-trades]
Outputs: console summary, reports/montecarlo.json, reports/montecarlo.html
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import backtest as bt
import bdusage

TOKENS_JSON = os.path.join("reports", "bd_tokens.json")
CACHE_DIR = ".bd_cache"
TRADES_PATH = os.path.join("reports", "mc_trades.json")
CLUSTER_PATH = os.path.join("reports", "cluster_backtest.json")
ENTRY_AGE_MIN = 30.0   # telemetry-standard entry params (HANDOFF.md)
MIN_ENTRY_VOL = 8000.0
COST_PCT = 4.0


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- trade pool

def regenerate_trades() -> dict:
    """Re-run the adopted config over the cached Birdeye sample to get every
    per-trade multiple (trade_journal.json only keeps the newest 50 rows).
    Exits come from config.asym.json so this always mirrors the live config."""
    with open("config.asym.json", "r", encoding="utf-8") as f:
        exits = json.load(f)["exits"]
    tokens = bt.load_token_index(TOKENS_JSON)
    trades, skipped = [], 0
    for tok in tokens:
        candles = bt.load_cached_candles(CACHE_DIR, tok.pool)
        if not candles:
            skipped += 1
            continue
        sim = bt.simulate(candles, exits, ENTRY_AGE_MIN, COST_PCT, MIN_ENTRY_VOL,
                          setup=bt.DEFAULT_SETUP)
        if sim:
            trades.append({"symbol": tok.symbol, "multiple": round(sim["multiple"], 6),
                           "reason": sim["reason"], "entry_ts": int(sim["entry_ts"]),
                           "end_ts": int(sim["end_ts"])})
    trades.sort(key=lambda t: t["entry_ts"])
    doc = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "params": {"exits": exits, "entry_age_min": ENTRY_AGE_MIN,
                   "min_entry_vol": MIN_ENTRY_VOL, "cost_pct": COST_PCT,
                   "setup_filter": bt.DEFAULT_SETUP, "tokens_json": TOKENS_JSON,
                   "cache_dir": CACHE_DIR, "no_history": skipped},
        "n": len(trades),
        "trades": trades,
    }
    with open(TRADES_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"trade pool regenerated: {len(trades)} trades -> {TRADES_PATH}")
    return doc


def load_trades(refresh: bool) -> dict:
    if not refresh and os.path.exists(TRADES_PATH):
        with open(TRADES_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        print(f"trade pool loaded: {doc['n']} trades from {TRADES_PATH} "
              f"(generated {doc['generated_at']}; --refresh-trades to rebuild)")
        return doc
    return regenerate_trades()


# ------------------------------------------------------------------- helpers

def pctl(sorted_xs: list, q: float) -> float:
    """Linear-interpolated percentile on a pre-sorted list, q in [0, 100]."""
    if not sorted_xs:
        return float("nan")
    k = (len(sorted_xs) - 1) * q / 100.0
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_xs[int(k)]
    return sorted_xs[f] + (sorted_xs[c] - sorted_xs[f]) * (k - f)


def risk_stats(mults: list) -> dict:
    """Per-trade risk-adjusted stats on r = m - 1 (return on the staked SOL).
    Risk-free rate 0; Sortino downside deviation vs a 0% target
    (sqrt of the mean squared negative return). Scale by sqrt(N) for an
    N-trade horizon."""
    rets = [m - 1.0 for m in mults]
    mu = statistics.mean(rets)
    sd = statistics.stdev(rets) if len(rets) > 1 else 0.0
    dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets))
    return {
        "ev_pct": round(100 * mu, 1),
        "sharpe": round(mu / sd, 2) if sd > 0 else None,
        "sortino": round(mu / dd, 2) if dd > 0 else None,
    }


def iid_sample(rng: random.Random, pool: list, k: int) -> list:
    return rng.choices(pool, k=k)


def block_sample(rng: random.Random, pool: list, k: int, block: int) -> list:
    """Circular block bootstrap over the CHRONOLOGICAL trade sequence:
    keeps short runs of hot/cold regime together instead of assuming
    every trade is independent."""
    out, n = [], len(pool)
    while len(out) < k:
        s = rng.randrange(n)
        for j in range(min(block, k - len(out))):
            out.append(pool[(s + j) % n])
    return out


# --------------------------------------------------------------- simulation

def run_scenario(pool: list, *, paths: int, horizon: int, start: float,
                 stake_sol: float, stake_frac: float, sampler: str, block: int,
                 haircut: float, seed: int, keep_curves: int = 0) -> dict:
    """One scenario = `paths` equity paths of `horizon` trades each.
    Fixed sizing stakes min(stake_sol, equity); fractional stakes
    stake_frac * equity. `haircut` multiplies every trade multiple
    (e.g. 0.95 = extra 5% cost). Returns distribution stats; optionally
    keeps the first `keep_curves` full curves for percentile bands."""
    rng = random.Random(seed)
    finals, maxdds, streaks = [], [], []
    touch2x = end2x = dd30 = dd50 = busted = 0
    curves = []
    for _ in range(paths):
        seq = (iid_sample(rng, pool, horizon) if sampler == "iid"
               else block_sample(rng, pool, horizon, block))
        eq, peak, maxdd = start, start, 0.0
        streak, worst_streak = 0, 0
        hit2 = False
        curve = [eq] if len(curves) < keep_curves else None
        for m in seq:
            m *= haircut
            if eq > 1e-9:
                stake = eq * stake_frac if stake_frac > 0 else min(stake_sol, eq)
                eq = max(0.0, eq + stake * (m - 1.0))
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0.0
            if dd > maxdd:
                maxdd = dd
            if eq >= 2 * start:
                hit2 = True
            if m <= 1.0:
                streak += 1
                worst_streak = max(worst_streak, streak)
            else:
                streak = 0
            if curve is not None:
                curve.append(eq)
        finals.append(eq)
        maxdds.append(maxdd)
        streaks.append(worst_streak)
        touch2x += hit2
        end2x += eq >= 2 * start
        dd30 += maxdd >= 0.30
        dd50 += maxdd >= 0.50
        busted += eq <= 1e-9
        if curve is not None:
            curves.append(curve)
    sf, sd, ss = sorted(finals), sorted(maxdds), sorted(streaks)
    return {
        "final": {q: round(pctl(sf, q), 4) for q in (5, 25, 50, 75, 95)},
        "final_mean": round(statistics.mean(finals), 4),
        "p_loss": round(sum(1 for x in finals if x < start) / paths, 4),
        "p_end_2x": round(end2x / paths, 4),
        "p_touch_2x": round(touch2x / paths, 4),
        "dd": {q: round(100 * pctl(sd, q), 2) for q in (50, 95)},
        "dd_max": round(100 * sd[-1], 2),
        "p_dd30": round(dd30 / paths, 4),
        "p_dd50": round(dd50 / paths, 4),
        "streak": {q: int(round(pctl(ss, q))) for q in (50, 95)},
        "busted": busted,
        "_finals": finals, "_maxdds": maxdds, "_curves": curves,
    }


def histogram(xs: list, lo: float, hi: float, nbins: int) -> dict:
    """Fixed-range histogram; values above `hi` fold into the last bin
    (tail count reported separately so the chart can disclose the clip)."""
    width = (hi - lo) / nbins
    counts = [0] * nbins
    tail = 0
    for x in xs:
        if x > hi:
            tail += 1
            counts[-1] += 1
            continue
        i = min(nbins - 1, max(0, int((x - lo) / width)))
        counts[i] += 1
    edges = [round(lo + i * width, 4) for i in range(nbins + 1)]
    return {"edges": edges, "counts": counts, "tail": tail}


# --------------------------------------------------- per-strategy MC payload

def build_payload(key: str, label: str, note: str, pool: list, args, seed: int) -> dict:
    """Run the full scenario battery + fan/histograms for one strategy's trade
    pool, returning a self-contained payload the page can render on its own.
    Same shape for every strategy so the dropdown can swap between them."""
    pool = list(pool)
    wins = [m for m in pool if m > 1.0]
    mean = statistics.mean(pool)
    pool_risk = risk_stats(pool)
    live_rate = 3 * 24 / 8.0
    pool_stats = {
        "n": len(pool), "wr": round(100 * len(wins) / len(pool), 1),
        "avg": round(mean, 3), "median": round(statistics.median(pool), 3),
        "min": round(min(pool), 3), "max": round(max(pool), 3),
        "breakeven_extra_cost_pct": round(100 * (1 - 1 / mean), 1) if mean > 0 else 0.0,
        "live_trades_per_day_max": round(live_rate, 1),
        **pool_risk,
    }
    drop = max(1, int(len(pool) * args.drop_top_pct / 100))
    pool_no_top = sorted(pool)[:-drop]
    common = dict(paths=args.paths, horizon=args.horizon, start=args.start_sol,
                  stake_sol=args.stake_sol, block=args.block)
    scenarios = [
        ("iid", "IID bootstrap - fixed stake",
         dict(pool=pool, sampler="iid", stake_frac=0.0, haircut=1.0, keep_curves=2000)),
        ("block10", f"Block bootstrap (block={args.block}) - regime clustering",
         dict(pool=pool, sampler="block", stake_frac=0.0, haircut=1.0)),
        ("stress", f"IID + extra {args.extra_cost_pct:.0f}% per-trade cost",
         dict(pool=pool, sampler="iid", stake_frac=0.0, haircut=1 - args.extra_cost_pct / 100)),
        ("no_top", f"IID minus top {args.drop_top_pct:.0f}% winners (n={len(pool_no_top)})",
         dict(pool=pool_no_top, sampler="iid", stake_frac=0.0, haircut=1.0)),
        ("frac5", f"IID - {args.stake_frac:.0%} of equity compounding stake",
         dict(pool=pool, sampler="iid", stake_frac=args.stake_frac, haircut=1.0)),
    ]
    results = {}
    for i, (k, lab, kw) in enumerate(scenarios):
        p = kw.pop("pool")
        results[k] = {"label": lab, **run_scenario(p, seed=seed + i, **common, **kw),
                      "risk": risk_stats([m * kw["haircut"] for m in p])}
    curves = results["iid"].pop("_curves")
    bands = {str(q): [] for q in (5, 25, 50, 75, 95)}
    for t in range(args.horizon + 1):
        col = sorted(c[t] for c in curves)
        for q in (5, 25, 50, 75, 95):
            bands[str(q)].append(round(pctl(col, q), 4))
    samples = [[round(v, 4) for v in c] for c in curves[:30]]
    finals = results["iid"].pop("_finals")
    maxdds = results["iid"].pop("_maxdds")
    hist_final = histogram(finals, 0.0, pctl(sorted(finals), 99), 30)
    hist_dd = histogram([100 * d for d in maxdds], 0.0, max(1.0, math.ceil(max(maxdds) * 40) * 2.5), 24)
    for k in results:
        for junk in ("_finals", "_maxdds", "_curves"):
            results[k].pop(junk, None)
    return {
        "key": key, "label": label, "note": note, "pool": pool_stats,
        "params": {"paths": args.paths, "horizon": args.horizon, "start_sol": args.start_sol,
                   "stake_sol": args.stake_sol, "stake_frac": args.stake_frac, "seed": seed,
                   "live_days_min": round(args.horizon / live_rate, 1)},
        "scenarios": [{"key": k, **results[k]} for k, _, _ in scenarios],
        "fan": {"bands": bands, "samples": samples},
        "hist_final": hist_final, "hist_dd": hist_dd,
    }


# ------------------------------------------------------------------- report

def build_html(data: dict, pooln: int, path: str) -> None:
    html = (HTML_TEMPLATE
            .replace("%%DATA%%", json.dumps(data))
            .replace("%%POOLN%%", str(pooln))
            .replace("%%BDSNAP%%", json.dumps(bdusage.snapshot())))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"html report: {os.path.abspath(path)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Monte Carlo equity paths for the adopted strategy")
    ap.add_argument("--paths", type=int, default=10000, help="simulated paths per scenario")
    ap.add_argument("--horizon", type=int, default=100, help="trades per path")
    ap.add_argument("--start-sol", type=float, default=5.0, help="starting bankroll (SOL)")
    ap.add_argument("--stake-sol", type=float, default=0.25,
                    help="fixed stake per trade (SOL; live config position_size_sol)")
    ap.add_argument("--stake-frac", type=float, default=0.05,
                    help="fraction of equity staked in the compounding scenario")
    ap.add_argument("--block", type=int, default=10, help="block size for the block bootstrap")
    ap.add_argument("--extra-cost-pct", type=float, default=5.0,
                    help="extra per-trade cost in the stress scenario")
    ap.add_argument("--drop-top-pct", type=float, default=5.0,
                    help="top winners removed in the no_top scenario")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--follow-lag", type=int, default=5,
                    help="entry lag (min) for the copy/cluster pools sourced from cluster_backtest.json")
    ap.add_argument("--refresh-trades", action="store_true",
                    help="rebuild reports/mc_trades.json from the candle cache")
    args = ap.parse_args()

    doc = load_trades(args.refresh_trades)
    base_pool = [t["multiple"] for t in doc["trades"]]
    if len(base_pool) < 30:
        sys.exit(f"only {len(base_pool)} trades in the base pool - not enough to resample")

    strategies = []
    strategies.append(build_payload(
        "base", "Base-entry (paper-trial strategy)",
        f"The bot's own strategy: base-entry setup filter + asym-runner exits, resampled from the "
        f"validated {len(base_pool)}-trade backtest sample. This is the live-arbiter strategy; "
        f"the audit below applies to it.",
        base_pool, args, seed=args.seed))

    # copy / cluster pools come from the UNBIASED cluster backtest (full buy
    # stream, winners AND rugs), at the chosen follow lag.
    cb = _read_json(CLUSTER_PATH)
    lag = str(int(args.follow_lag))
    if cb and cb.get("raw"):
        W = cb.get("window_min", "?")
        ab = (cb["raw"].get("ALLBUYS") or {}).get(lag) or []
        cl = (cb["raw"].get("CLUSTER_T2") or {}).get(lag) or []
        if len(ab) >= 30:
            strategies.append(build_payload(
                "copy_all", "Copy-all buys (unbiased)",
                f"Enter every token a monitored wallet bought (+{lag}m lag), over their full swap "
                f"stream - winners AND rugs. Unbiased at the token level, but still WALLET-selection "
                f"biased (curated profitable wallets) and it inherits the backtest's fill optimism "
                f"(gap-through stops fill at the stop). Not proven live - treat as an upper bound.",
                ab, args, seed=args.seed + 100))
        if len(cl) >= 20:
            strategies.append(build_payload(
                "cluster", "Cluster-confirmed (unbiased)",
                f"Enter only when a 2nd monitored wallet confirms the token within {W}m (+{lag}m "
                f"lag). The unbiased test found this does NOT beat copy-all or solo: co-accumulation "
                f"helps the leader, not a lagged follower whose stops cap the extra upside. Shown so "
                f"you can see the distributions side by side.",
                cl, args, seed=args.seed + 200))

    data = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "active": "base", "follow_lag": int(args.follow_lag),
        "trades_generated_at": doc["generated_at"],
        "strategies": strategies,
    }

    os.makedirs("reports", exist_ok=True)
    jpath = os.path.join("reports", "montecarlo.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    print(f"machine-readable: {os.path.abspath(jpath)}")
    build_html(data, len(base_pool), os.path.join("reports", "montecarlo.html"))

    print(f"\n== MONTE CARLO  {args.paths} paths x {args.horizon} trades x "
          f"{len(strategies)} strategies ==")
    for sp in strategies:
        ps, iid = sp["pool"], next(s for s in sp["scenarios"] if s["key"] == "iid")
        print(f"\n[{sp['key']}] {sp['label']}: pool {ps['n']} | WR {ps['wr']}% | "
              f"avg {ps['avg']}x | EV {ps['ev_pct']:+.1f}%/trade | Sharpe {ps['sharpe']}")
        print(f"  median final {iid['final'][50]:.2f} SOL ({iid['final'][50] / args.start_sol:.2f}x) "
              f"| P(loss) {100 * iid['p_loss']:.1f}% | P(2x end) {100 * iid['p_end_2x']:.1f}% "
              f"| p95 maxDD {iid['dd'][95]:.1f}%")
    print("\nCAVEATS: all pools resample BACKTEST/simulated-fill multiples. copy_all & cluster")
    print("are also wallet-selection biased (curated profitable wallets). Paper trading is the arbiter.")


# ----------------------------------------------------------------- template

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Monte Carlo — equity paths</title>
<style>
  .viz-root {
    color-scheme: light;
    --page:      #f9f9f7; --surface-1: #fcfcfb;
    --ink:       #0b0b0b; --ink-2:     #52514e; --muted: #898781;
    --grid:      #e1e0d9; --baseline:  #c3c2b7;
    --border:    rgba(11,11,11,0.10);
    --series-1:  #2a78d6; --series-2:  #eb6834;
    --good:      #006300; --critical:  #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      color-scheme: dark;
      --page:      #0d0d0d; --surface-1: #1a1a19;
      --ink:       #ffffff; --ink-2:     #c3c2b7; --muted: #898781;
      --grid:      #2c2c2a; --baseline:  #383835;
      --border:    rgba(255,255,255,0.10);
      --series-1:  #3987e5; --series-2:  #d95926;
      --good:      #0ca30c; --critical:  #d03b3b;
    }
  }
  :root[data-theme="dark"] .viz-root {
    color-scheme: dark;
    --page:      #0d0d0d; --surface-1: #1a1a19;
    --ink:       #ffffff; --ink-2:     #c3c2b7; --muted: #898781;
    --grid:      #2c2c2a; --baseline:  #383835;
    --border:    rgba(255,255,255,0.10);
    --series-1:  #3987e5; --series-2:  #d95926;
    --good:      #0ca30c; --critical:  #d03b3b;
  }
  * { box-sizing: border-box; margin: 0; }
  body.viz-root {
    background: var(--page); color: var(--ink);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 24px 16px 48px;
  }
  main { max-width: 1060px; margin: 0 auto; }
  h1 { font-size: 22px; font-weight: 650; }
  h2 { font-size: 15px; font-weight: 650; margin: 0 0 2px; }
  .sub { color: var(--ink-2); margin: 4px 0 12px; }
  .stratbar { display: flex; align-items: center; gap: 10px; margin: 2px 0 10px; flex-wrap: wrap; }
  .stratbar label { color: var(--ink-2); font-size: 13px; font-weight: 600; }
  .stratbar select { font: inherit; padding: 6px 12px; border-radius: 8px;
      border: 1px solid var(--border); background: var(--surface-1); color: var(--ink); cursor: pointer; }
  .provenance { color: var(--ink-2); font-size: 13px; line-height: 1.5; margin: 0 0 18px;
      padding: 10px 13px; border-left: 3px solid var(--series-2); background: var(--surface-1);
      border-radius: 0 8px 8px 0; }
  .caption { color: var(--muted); font-size: 12.5px; margin-top: 6px; }
  .card { background: var(--surface-1); border: 1px solid var(--border);
          border-radius: 10px; padding: 16px 18px; margin-bottom: 18px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           gap: 12px; margin-bottom: 18px; }
  .tile { background: var(--surface-1); border: 1px solid var(--border);
          border-radius: 10px; padding: 12px 14px; }
  .tile .label { color: var(--ink-2); font-size: 12.5px; }
  .tile .value { font-size: 26px; font-weight: 600; margin-top: 2px; }
  .tile .note  { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .legend { display: flex; flex-wrap: wrap; gap: 16px; align-items: center;
            color: var(--ink-2); font-size: 12.5px; margin: 8px 0 4px; }
  .legend .key { display: inline-flex; align-items: center; gap: 6px; }
  .sw-line  { width: 18px; height: 0; border-top: 2px solid var(--series-1); border-radius: 2px; }
  .sw-band  { width: 18px; height: 10px; border-radius: 3px; background: var(--series-1); opacity: .18; }
  .sw-band2 { width: 18px; height: 10px; border-radius: 3px; background: var(--series-1); opacity: .10; }
  .sw-samp  { width: 18px; height: 0; border-top: 1px solid var(--muted); }
  .sw-ref   { width: 18px; height: 0; border-top: 1px solid var(--baseline); }
  svg { display: block; width: 100%; height: auto; }
  svg text { font: 11.5px system-ui, -apple-system, "Segoe UI", sans-serif;
             fill: var(--muted); font-variant-numeric: tabular-nums; }
  svg .axis line { stroke: var(--baseline); }
  svg .grid line { stroke: var(--grid); }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 820px) { .grid2 { grid-template-columns: 1fr; } }
  table { border-collapse: collapse; width: 100%; font-size: 13px;
          font-variant-numeric: tabular-nums; }
  th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--grid); }
  th { color: var(--ink-2); font-weight: 600; }
  th:first-child, td:first-child { text-align: left; }
  td.good { color: var(--good); } td.bad { color: var(--critical); }
  #tt { position: fixed; display: none; pointer-events: none; z-index: 10;
        background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 8px; padding: 7px 10px; font-size: 12.5px;
        color: var(--ink); box-shadow: 0 2px 10px rgba(0,0,0,.12);
        font-variant-numeric: tabular-nums; }
  details { margin-top: 10px; color: var(--ink-2); font-size: 13px; }
  summary { cursor: pointer; color: var(--muted); }
  ul.caveats, ol.caveats { margin: 8px 0 0 18px; color: var(--ink-2); }
  ul.caveats li, ol.caveats li { margin-bottom: 4px; }
  .audit-lead { color: var(--ink-2); margin: 10px 0 0; font-weight: 600; }
  /* NERV app chrome — fixed dark, shared look with / and /trades */
  .nav { position: sticky; top: 0; z-index: 98; display: flex; align-items: center;
         gap: 8px; background: #0a0a0e; border-bottom: 1px solid #2a2a38;
         margin: -24px -16px 18px; padding: 10px 24px;
         font-family: "Cascadia Mono", Consolas, monospace; }
  .nav .tab { background: #14141d; color: #9a9aa8; border: 1px solid #2a2a38;
              padding: 6px 16px; font-size: 11px; letter-spacing: 1px;
              text-decoration: none; white-space: nowrap; }
  .nav .tab:hover { color: #e8e8ea; border-color: #5c5c6c; }
  .nav .tab.on { color: #ffc233; border-color: #ffc233; }
  .nav button.tab { cursor: pointer; font-family: inherit; font-size: 11px; }
  .nav button.tab:disabled { color: #ffc233; border-color: #ffc233; cursor: wait; }
  .nav .bd { margin-left: auto; min-width: 280px; max-width: 440px; text-align: right; }
  .nav .bdlab { color: #ff7a1a; font-weight: bold; font-size: 10px;
                letter-spacing: 1px; margin-right: 8px; }
  #bdtext { font-size: 10.5px; color: #9a9aa8; }
  .bdbar { height: 4px; background: #1e1e2a; margin-top: 4px; border-radius: 2px; overflow: hidden; }
  #bdfill { height: 100%; width: 0; background: #00a869; transition: width .4s; }
</style>
</head>
<body class="viz-root">
<div class="nav">
  <a class="tab" href="/" title="NERV dashboard home">&#8962; HOME</a>
  <a class="tab" href="/trades" title="Per-trade journal cards">TRADE JOURNAL</a>
  <span class="tab on">MONTE CARLO</span>
  <a class="tab" href="/blacklist" title="Charts purged from the sample — manual flags + auto wash-ramps">BLACKLIST</a>
  <a class="tab" href="/wallets" title="Scouted copy-trade wallets + tracked watchlist + live harvest">WALLETS</a>
  <button id="mcref" class="tab" title="Re-run montecarlo.py --refresh-trades via the dashboard: rebuilds the trade pool from the current cleaned sample (config.asym.json exits + base-entry filter), then reloads this page with the new numbers.">⟳ REFRESH POOL</button>
  <div class="bd" title="Lite plan: 2.5M CU/cycle (anchored the 20th), 15 RPS, overage $15/1M CU. Local ledger + reconstructed baseline (Birdeye has no usage API - their Usages/Metrics page is authoritative). OHLCV CU cost is an estimate.">
    <span class="bdlab">BIRDEYE API</span><span id="bdtext">no usage tracked yet</span>
    <div class="bdbar"><div id="bdfill"></div></div>
  </div>
</div>
<main>
  <h1>Monte Carlo — equity paths</h1>
  <div class="stratbar">
    <label for="strat">Strategy</label>
    <select id="strat"></select>
  </div>
  <p class="sub" id="subtitle"></p>
  <p class="provenance" id="provenance"></p>
  <div class="tiles" id="tiles"></div>

  <div class="card">
    <h2>Equity path fan — IID bootstrap, fixed stake</h2>
    <p class="caption" id="fan-caption"></p>
    <div class="legend">
      <span class="key"><span class="sw-line"></span>median path</span>
      <span class="key"><span class="sw-band"></span>25–75%</span>
      <span class="key"><span class="sw-band2"></span>5–95%</span>
      <span class="key"><span class="sw-samp"></span>30 sample paths</span>
      <span class="key"><span class="sw-ref"></span>starting equity</span>
    </div>
    <div id="fan"></div>
    <details><summary>Table view — percentile equity by trade count</summary>
      <div id="fan-table"></div></details>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>Final equity distribution</h2>
      <p class="caption" id="hf-caption"></p>
      <div id="hist-final"></div>
    </div>
    <div class="card">
      <h2>Max drawdown distribution</h2>
      <p class="caption" id="hd-caption"></p>
      <div id="hist-dd"></div>
    </div>
  </div>

  <div class="card">
    <h2>Risk-adjusted returns</h2>
    <p class="caption">Per-trade return r = multiple &minus; 1 on the staked SOL; risk-free rate 0. Sharpe = mean(r) / stdev(r); Sortino = mean(r) / downside deviation vs a 0% target. Multiply a per-trade ratio by &radic;N for an N-trade horizon. Sizing changes path outcomes, not per-trade stats &mdash; the compounding scenario shares the IID pool's numbers.</p>
    <div class="tiles" id="risk-tiles"></div>
    <div id="risk-table"></div>
  </div>

  <div class="card">
    <h2>Scenario comparison</h2>
    <p class="caption">Same trade pool, different resampling and sizing assumptions. All figures per-path over the full horizon.</p>
    <div id="scen-table"></div>
  </div>

  <div class="card">
    <h2>Assumptions &amp; caveats</h2>
    <ul class="caveats">
      <li><b>How to read this page:</b> the <b>Strategy</b> dropdown swaps between three trade pools — every chart, tile and table below re-renders for the selected one. Within a pool, ~all paths ending profitable means sequencing/path risk is negligible <i>under that distribution</i>; the entire remaining risk is <b>distributional</b> — whether live trades draw from anything like it. Paper trading answers that, not this simulation.</li>
      <li><b>The three strategies are not equally trustworthy.</b> <i>Base-entry</i> is the bot's validated backtest sample (the audit below covers it). <i>Copy-all</i> and <i>Cluster-confirmed</i> are sourced from the monitored wallets' real buy stream — unbiased at the token level (rugs included) but still <b>wallet-selection biased</b>: they only exist because those wallets were curated for past profit, so their high win rates partly reflect who was picked, not a repeatable edge. The unbiased test also found cluster-confirm does <b>not</b> beat copy-all. Both inherit the backtest's optimistic fills.</li>
      <li>Trade pool = <b>backtest</b> multiples (simulated fills, 4% round-trip cost, conservative candle-ambiguity rule). The majority of exits are <code>data_end</code> (12h candle windows) — an inherent optimism disclosed in the backtest telemetry.</li>
      <li>Bootstrap assumes future trades draw from the same distribution as the %%POOLN%%-trade sample. <b>Regime change is not modeled</b> — the block and no-top scenarios are partial stress tests, not a substitute.</li>
      <li>Fixed-stake sizing mirrors <code>config.asym.json</code> (0.25 SOL/trade). The bot's daily loss limit (0.75 SOL) is <b>not</b> modeled; it would truncate the worst same-day sequences.</li>
      <li>Paper trading remains the arbiter of real edge (locked project rule). This page quantifies path risk <i>if</i> the backtest distribution holds — it is not evidence that it will.</li>
    </ul>
  </div>

  <div class="card">
    <h2>Backtest integrity audit — anti-cheat safeguards &amp; residual inflation risk</h2>
    <p class="caption">Code audit of the simulation stack that produced the trade pool behind every chart above (2026-07-23). Verdict: <b>no look-ahead bias found</b> — there is no path where future candles influence an entry or exit decision, and intra-candle ambiguity always resolves against the trade. The optimism that remains is in <b>fill realism</b>, not information leakage.</p>
    <p class="audit-lead">Safeguards in place</p>
    <ul class="caveats">
      <li><b>Look-ahead prevention:</b> entry decisions and the base-entry setup filter read only candles at/before entry time; simulation reads only candles after (<code>backtest.simulate</code>, <code>entry_setup_ok</code>).</li>
      <li><b>Conservative candle-ambiguity rule:</b> stops/trails are checked against the candle low <i>before</i> take-profits are checked against its high, and the trailing peak only advances after the candle is fully processed — a candle can never trail-exit off its own high.</li>
      <li><b>Pre-registered evaluation:</b> the verdict configs (<code>loop8_eval.py</code> P1–P4/C1–C4) were frozen before the day-30–60 data was fetched, with a locked pass/fail rule; <code>loop9_eval.py</code> adds a virgin week never touched by any optimization loop. Exploratory sweeps carry no verdict authority.</li>
      <li><b>Deterministic train/valid split:</b> <code>sha1(mint) % 2</code> halves, identical for every consumer; verdict configs must hold on both halves at n ≥ 100 each.</li>
      <li><b>Survivor-free universe:</b> Birdeye <code>new_listing</code> enumeration includes every pool — winners and corpses alike; GeckoTerminal cohorts are labeled, with <code>trending</code> flagged as a survivor-biased upper bound only.</li>
      <li><b>Liquidity-honest fills (engine v5):</b> exits only fill on candles with real volume; dust-candle stops fill at min(stop, close); trailing peaks are wash-guarded (volume floor, peak capped at 2× close); data-end remainders on tokens without a liquidity snapshot are valued at <b>0</b>.</li>
      <li><b>Asymmetric wash-trade purge:</b> only rising wash-ramps (fabricated wins) are blocklisted; smooth declines (real losses) deliberately stay in the sample, so the cleanup can only lower results.</li>
      <li><b>Cost haircut:</b> flat 4% round-trip cost on every simulated trade.</li>
    </ul>
    <p class="audit-lead">Remaining ways these numbers could still be inflated (ranked)</p>
    <ol class="caveats">
      <li><b>Gap-through stops fill at the exact stop price.</b> A rug that gaps 50–90% through the stop in one candle still "fills" at the stop level in every engine version; a real market sell fills near the low. The single most optimistic assumption left, and it hits exactly the worst trades.</li>
      <li><b>TPs/trails fill at the exact trigger price, full size.</b> The $500/candle volume floor proxies "someone traded", not depth for <i>your</i> order size; MEV, failed transactions, and priority fees sit outside the 4% haircut.</li>
      <li><b>Soft overfitting from sample reuse.</b> The sha1 halves are contemporaneous (they catch noise-fitting, not regime luck), and the validation half was consulted across many loops. Only the pre-registered loop-8/9 out-of-time numbers deserve verdict weight.</li>
      <li><b>Plain <code>backtest.py</code> variants (A–E) run the older, more optimistic engine:</b> high-touch fills regardless of volume, data-end remainders credited at the final close. Their output reads higher than the same config under engine v5.</li>
      <li><b>Tokens with no fetchable candle history are dropped</b> — if the API preferentially loses history for instant-rugs, that trims the worst outcomes (counted and printed, but residual).</li>
      <li><b>Entry-timing semantics:</b> a "20-minute entry" fills at the close of the candle containing entry time (up to ~1 extra minute on 1m candles). Decision and fill share the same information — not look-ahead, just a label mismatch plus unmodeled next-tick slippage.</li>
    </ol>
    <p class="caption">Trust hierarchy: <b>loop8/loop9 out-of-time numbers</b> &gt; sha1 both-halves numbers &gt; sweep output &gt; plain <code>backtest.py</code> variants. The backtest-to-live gap comes from items 1–2 above — which is why paper trading, not this page, remains the arbiter of real edge.</p>
  </div>
</main>
<div id="tt"></div>
<script type="application/json" id="mc-data">%%DATA%%</script>
<script>
const ALL = JSON.parse(document.getElementById('mc-data').textContent);
let D, P;  // active strategy payload + its params (set by activate())
const css = n => getComputedStyle(document.body).getPropertyValue(n).trim();
const fmt = (x, d=2) => Number(x).toLocaleString('en-US',
  {minimumFractionDigits: d, maximumFractionDigits: d});
const pc = (x, d=0) => fmt(100 * x, d) + '%';
const tt = document.getElementById('tt');
function showTT(html, ev) {
  tt.innerHTML = html; tt.style.display = 'block';
  const pad = 14, w = tt.offsetWidth, h = tt.offsetHeight;
  let x = ev.clientX + pad, y = ev.clientY - h - pad;
  if (x + w > innerWidth - 8) x = ev.clientX - w - pad;
  if (y < 8) y = ev.clientY + pad;
  tt.style.left = x + 'px'; tt.style.top = y + 'px';
}
const hideTT = () => tt.style.display = 'none';

function niceTicks(lo, hi, n) {
  const span = hi - lo || 1, step0 = span / Math.max(1, n - 1);
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step)
    out.push(Math.round(v * 1e6) / 1e6);
  return out;
}
const S = 5;  // stroke-safe outer pad

// ---------------- header, tiles ----------------
function renderHeader() {
  const iid = D.scenarios.find(s => s.key === 'iid');
  document.getElementById('subtitle').textContent =
    D.label + ' - ' + P.paths.toLocaleString() + ' simulated paths x ' + P.horizon +
    ' trades (>= ' + P.live_days_min + ' days live at the bot\\'s 3-slot ceiling) - pool: ' +
    D.pool.n + ' trades, ' + D.pool.wr + '% WR, ' + D.pool.avg +
    'x avg - generated ' + ALL.generated_at;
  document.getElementById('provenance').textContent = D.note;
  const tiles = [
    ['Paths ending profitable', pc(1 - iid.p_loss), 'after ' + P.horizon + ' trades'],
    ['Median final equity', fmt(iid.final[50] / P.start_sol, 2) + 'x',
     fmt(iid.final[50]) + ' SOL from ' + fmt(P.start_sol)],
    ['5th-percentile final', fmt(iid.final[5] / P.start_sol, 2) + 'x',
     fmt(iid.final[5]) + ' SOL'],
    ['Touched 2x at any point', pc(iid.p_touch_2x), 'within ' + P.horizon + ' trades'],
    ['Median max drawdown', fmt(iid.dd[50], 1) + '%', 'p95: ' + fmt(iid.dd[95], 1) + '%'],
    ['Max DD >= 50%', pc(iid.p_dd50, 2), 'kill-switch territory'],
    ['Edge margin', fmt(D.pool.breakeven_extra_cost_pct, 0) + '%',
     'extra per-trade cost until the mean edge is gone'],
    ['Expected value / trade', (D.pool.ev_pct > 0 ? '+' : '') + fmt(D.pool.ev_pct, 1) + '%',
     (D.pool.ev_pct > 0 ? '+' : '') + fmt(P.stake_sol * D.pool.ev_pct / 100, 3) +
     ' SOL on a ' + P.stake_sol + ' SOL stake'],
  ];
  document.getElementById('tiles').innerHTML = tiles.map(t =>
    '<div class="tile"><div class="label">' + t[0] + '</div><div class="value">' +
    t[1] + '</div><div class="note">' + t[2] + '</div></div>').join('');
}

// ---------------- fan chart ----------------
function renderFan() {
  const W = 1020, H = 400, m = {l: 58, r: 20, t: 12, b: 36};
  const B = D.fan.bands, N = P.horizon;
  const ymax = Math.max(...B['95'], P.start_sol) * 1.05;
  const ymin = Math.min(...B['5'], P.start_sol) * 0.9;
  const X = i => m.l + (W - m.l - m.r) * i / N;
  const Y = v => m.t + (H - m.t - m.b) * (1 - (v - ymin) / (ymax - ymin));
  const line = a => a.map((v, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ',' + Y(v).toFixed(1)).join('');
  // sample paths can exceed the p95-based scale; pin them to the plot top
  const lineC = a => a.map((v, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ',' +
    Math.max(m.t, Y(v)).toFixed(1)).join('');
  const band = (lo, hi) => line(hi) +
    lo.map((v, i) => 'L' + X(lo.length - 1 - i).toFixed(1) + ',' + Y(lo[lo.length - 1 - i]).toFixed(1)).join('') + 'Z';
  let g = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Equity path fan chart">';
  g += '<g class="grid">' + niceTicks(ymin, ymax, 6).map(v =>
    '<line x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/>' +
    '<text x="' + (m.l - 8) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + fmt(v, v < 10 ? 1 : 0) + '</text>'
  ).join('') + '</g>';
  g += '<path d="' + band(B['5'], B['95']) + '" fill="' + css('--series-1') + '" opacity="0.10"/>';
  g += '<path d="' + band(B['25'], B['75']) + '" fill="' + css('--series-1') + '" opacity="0.18"/>';
  g += D.fan.samples.map(s => '<path d="' + lineC(s) + '" fill="none" stroke="' +
    css('--muted') + '" stroke-width="1" opacity="0.28"/>').join('');
  g += '<line x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + Y(P.start_sol) + '" y2="' +
    Y(P.start_sol) + '" stroke="' + css('--baseline') + '" stroke-width="1"/>';
  g += '<path d="' + line(B['50']) + '" fill="none" stroke="' + css('--series-1') +
    '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
  const endY = Y(B['50'][N]);
  g += '<circle cx="' + X(N) + '" cy="' + endY + '" r="4.5" fill="' + css('--series-1') +
    '" stroke="' + css('--surface-1') + '" stroke-width="2"/>';
  g += '<text x="' + (X(N) - 8) + '" y="' + (endY - 10) + '" text-anchor="end" style="fill:' +
    css('--ink') + ';font-weight:600">' + fmt(B['50'][N]) + ' SOL</text>';
  g += '<g class="axis"><line x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + (H - m.b) +
    '" y2="' + (H - m.b) + '"/></g>';
  g += niceTicks(0, N, 6).map(v => '<text x="' + X(v) + '" y="' + (H - m.b + 18) +
    '" text-anchor="middle">' + v + '</text>').join('');
  g += '<text x="' + ((m.l + W - m.r) / 2) + '" y="' + (H - 4) + '" text-anchor="middle">trades</text>';
  g += '<text x="14" y="' + (m.t + 2) + '">SOL</text>';
  g += '<line id="fan-x" y1="' + m.t + '" y2="' + (H - m.b) + '" stroke="' + css('--baseline') +
    '" stroke-width="1" visibility="hidden"/>';
  g += '<rect id="fan-hit" x="' + m.l + '" y="' + m.t + '" width="' + (W - m.l - m.r) +
    '" height="' + (H - m.t - m.b) + '" fill="transparent"/>';
  g += '</svg>';
  const el = document.getElementById('fan');
  el.innerHTML = g;
  document.getElementById('fan-caption').textContent =
    'Fixed ' + P.stake_sol + ' SOL stake from a ' + fmt(P.start_sol) +
    ' SOL start. Bands are per-trade-count percentiles across 2,000 stored paths; stats use all ' +
    P.paths.toLocaleString() + '.';
  const hit = el.querySelector('#fan-hit'), xline = el.querySelector('#fan-x');
  hit.addEventListener('mousemove', ev => {
    const r = hit.getBoundingClientRect();
    const i = Math.max(0, Math.min(N, Math.round((ev.clientX - r.left) / r.width * N)));
    xline.setAttribute('x1', X(i)); xline.setAttribute('x2', X(i));
    xline.setAttribute('visibility', 'visible');
    showTT('<b>trade ' + i + '</b><br>p95: ' + fmt(B['95'][i]) + ' SOL<br>median: ' +
      fmt(B['50'][i]) + ' SOL<br>p5: ' + fmt(B['5'][i]) + ' SOL', ev);
  });
  hit.addEventListener('mouseleave', () => { xline.setAttribute('visibility', 'hidden'); hideTT(); });
  let rows = '<table><tr><th>trades</th><th>p5</th><th>p25</th><th>median</th><th>p75</th><th>p95</th></tr>';
  for (let i = 0; i <= N; i += Math.max(1, Math.round(N / 10)))
    rows += '<tr><td>' + i + '</td>' + [5, 25, 50, 75, 95].map(q =>
      '<td>' + fmt(B[String(q)][i]) + '</td>').join('') + '</tr>';
  document.getElementById('fan-table').innerHTML = rows + '</table>';
}

// ---------------- histograms ----------------
function hist(elId, hd, color, xfmt, ttfmt, refX) {
  const W = 500, H = 300, m = {l: 46, r: 14, t: 12, b: 34};
  const n = hd.counts.length, total = hd.counts.reduce((a, b) => a + b, 0);
  const cmax = Math.max(...hd.counts);
  const slot = (W - m.l - m.r) / n, bw = Math.max(1, slot - 2);
  const Y = c => m.t + (H - m.t - m.b) * (1 - c / cmax);
  let g = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img">';
  g += '<g class="grid">' + niceTicks(0, cmax, 5).filter(v => v > 0).map(v =>
    '<line x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/>' +
    '<text x="' + (m.l - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + v + '</text>'
  ).join('') + '</g>';
  for (let i = 0; i < n; i++) {
    if (!hd.counts[i]) continue;
    const x = m.l + i * slot + 1, y = Y(hd.counts[i]), h = H - m.b - y, r = Math.min(4, bw / 2, h);
    g += '<path class="bar" data-i="' + i + '" d="M' + x + ',' + (y + h) + ' L' + x + ',' + (y + r) +
      ' Q' + x + ',' + y + ' ' + (x + r) + ',' + y + ' L' + (x + bw - r) + ',' + y +
      ' Q' + (x + bw) + ',' + y + ' ' + (x + bw) + ',' + (y + r) + ' L' + (x + bw) + ',' + (y + h) +
      ' Z" fill="' + color + '"/>';
  }
  if (refX !== null && refX >= hd.edges[0] && refX <= hd.edges[n]) {
    const rx = m.l + (refX - hd.edges[0]) / (hd.edges[n] - hd.edges[0]) * (W - m.l - m.r);
    g += '<line x1="' + rx + '" x2="' + rx + '" y1="' + m.t + '" y2="' + (H - m.b) +
      '" stroke="' + css('--baseline') + '" stroke-width="1"/>';
  }
  g += '<g class="axis"><line x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + (H - m.b) +
    '" y2="' + (H - m.b) + '"/></g>';
  g += niceTicks(hd.edges[0], hd.edges[n], 6).map(v => {
    const x = m.l + (v - hd.edges[0]) / (hd.edges[n] - hd.edges[0]) * (W - m.l - m.r);
    return '<text x="' + x + '" y="' + (H - m.b + 18) + '" text-anchor="middle">' + xfmt(v) + '</text>';
  }).join('');
  g += '</svg>';
  const el = document.getElementById(elId);
  el.innerHTML = g;
  el.querySelectorAll('.bar').forEach(b => {
    b.addEventListener('mousemove', ev => {
      const i = +b.dataset.i, c = hd.counts[i];
      showTT('<b>' + ttfmt(hd.edges[i], hd.edges[i + 1]) + '</b><br>' + c.toLocaleString() +
        ' paths (' + pc(c / total, 1) + ')' +
        (i === n - 1 && hd.tail ? '<br>includes ' + hd.tail + ' above range' : ''), ev);
    });
    b.addEventListener('mouseleave', hideTT);
  });
}
function renderHists() {
  hist('hist-final', D.hist_final, css('--series-1'), v => fmt(v, 0),
    (a, b) => fmt(a, 1) + ' - ' + fmt(b, 1) + ' SOL', P.start_sol);
  document.getElementById('hf-caption').textContent =
    'Equity after ' + P.horizon + ' trades, IID scenario (clipped at p99' +
    (D.hist_final.tail ? '; ' + D.hist_final.tail + ' paths above fold into the last bar' : '') +
    '). Reference line = ' + fmt(P.start_sol) + ' SOL start.';
  hist('hist-dd', D.hist_dd, css('--series-2'), v => fmt(v, 0) + '%',
    (a, b) => fmt(a, 1) + '% - ' + fmt(b, 1) + '% max drawdown', null);
  document.getElementById('hd-caption').textContent =
    'Worst peak-to-trough equity drawdown per path, IID scenario.';
}

// ---------------- risk-adjusted returns ----------------
function renderRisk() {
  const R = D.pool, hs = Math.sqrt(P.horizon);
  const sign = (x, d) => (x > 0 ? '+' : '') + fmt(x, d);
  const rtiles = [
    ['Expected value / trade', sign(R.ev_pct, 1) + '%',
     sign(P.stake_sol * R.ev_pct / 100, 3) + ' SOL on a ' + P.stake_sol + ' SOL stake'],
    ['Sharpe (per trade)', fmt(R.sharpe, 2), 'mean / stdev of r'],
    ['Sortino (per trade)', fmt(R.sortino, 2), 'mean / downside deviation'],
    ['Sharpe (' + P.horizon + '-trade)', fmt(R.sharpe * hs, 1),
     'per-trade &times; &radic;' + P.horizon],
    ['Sortino (' + P.horizon + '-trade)', fmt(R.sortino * hs, 1),
     'per-trade &times; &radic;' + P.horizon],
  ];
  document.getElementById('risk-tiles').innerHTML = rtiles.map(t =>
    '<div class="tile"><div class="label">' + t[0] + '</div><div class="value">' + t[1] +
    '</div><div class="note">' + t[2] + '</div></div>').join('');
  let h = '<table><tr><th>scenario</th><th>EV / trade</th><th>EV / trade (SOL)</th>' +
    '<th>Sharpe</th><th>Sortino</th><th>Sharpe &times; &radic;' + P.horizon +
    '</th><th>Sortino &times; &radic;' + P.horizon + '</th></tr>';
  for (const s of D.scenarios) {
    const k = s.risk;
    h += '<tr><td>' + s.label + '</td>' +
      '<td class="' + (k.ev_pct > 0 ? 'good' : 'bad') + '">' + sign(k.ev_pct, 1) + '%</td>' +
      '<td>' + sign(P.stake_sol * k.ev_pct / 100, 3) + '</td>' +
      '<td>' + fmt(k.sharpe, 2) + '</td><td>' + fmt(k.sortino, 2) + '</td>' +
      '<td>' + fmt(k.sharpe * hs, 1) + '</td><td>' + fmt(k.sortino * hs, 1) + '</td></tr>';
  }
  document.getElementById('risk-table').innerHTML = h + '</table>';
}

// ---------------- scenario table ----------------
function renderScen() {
  const cols = ['scenario', 'median final', 'p5', 'p95', 'P(end &lt; start)',
                'P(end &ge; 2x)', 'median maxDD', 'p95 maxDD', 'P(DD &ge; 50%)'];
  let h = '<table><tr>' + cols.map(c => '<th>' + c + '</th>').join('') + '</tr>';
  for (const s of D.scenarios) {
    h += '<tr><td>' + s.label + '</td>' +
      '<td>' + fmt(s.final[50]) + ' <span style="color:var(--muted)">(' +
        fmt(s.final[50] / P.start_sol, 2) + 'x)</span></td>' +
      '<td>' + fmt(s.final[5]) + '</td><td>' + fmt(s.final[95]) + '</td>' +
      '<td class="' + (s.p_loss > 0.25 ? 'bad' : '') + '">' + pc(s.p_loss, 1) + '</td>' +
      '<td class="' + (s.p_end_2x > 0.5 ? 'good' : '') + '">' + pc(s.p_end_2x, 1) + '</td>' +
      '<td>' + fmt(s.dd[50], 1) + '%</td><td>' + fmt(s.dd[95], 1) + '%</td>' +
      '<td>' + pc(s.p_dd50, 2) + '</td></tr>';
  }
  document.getElementById('scen-table').innerHTML = h + '</table>';
}

// ---------------- strategy switcher ----------------
function renderAll() { renderHeader(); renderFan(); renderHists(); renderRisk(); renderScen(); }
function activate(key) {
  D = ALL.strategies.find(s => s.key === key) || ALL.strategies[0];
  P = D.params; renderAll();
}
(function initStrat() {
  const sel = document.getElementById('strat');
  sel.innerHTML = ALL.strategies.map(s =>
    '<option value="' + s.key + '">' + s.label + '</option>').join('');
  sel.value = ALL.active;
  sel.addEventListener('change', () => activate(sel.value));
  activate(sel.value);
})();

// ---------------- Birdeye meter (app chrome, shared with / and /trades) ----------------
const BD_EMBED = %%BDSNAP%%;
const bdFmt = n => n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : String(n);
function bdRender(s, live) {
  if (!s || s.requests === undefined) return;
  const p = s.pct_of_plan || 0;
  document.getElementById('bdtext').textContent =
    s.requests.toLocaleString() + ' req · ~' + bdFmt(s.cu_est) + ' / ' + bdFmt(s.plan_cu) +
    ' CU (' + p.toFixed(1) + '%) · ' + (s.period_label || s.month) +
    (s.overage_usd_est ? ' · overage ~$' + s.overage_usd_est.toFixed(2) : '') +
    (live ? '' : ' · at page build');
  const f = document.getElementById('bdfill');
  f.style.width = Math.min(100, p) + '%';
  f.style.background = p >= 90 ? '#ff3355' : p >= 60 ? '#ffc233' : '#00a869';
}
bdRender(BD_EMBED, false);
const bdPoll = () => fetch('/api/bdusage').then(r => r.json()).then(s => bdRender(s, true)).catch(() => {});
bdPoll();
setInterval(bdPoll, 60000);
// ---------------- refresh-pool button (POST /api/mc/refresh on the dashboard) ----------------
const mcBtn = document.getElementById('mcref');
if (mcBtn) {
  let mcPoll = null;
  function mcWatch() {
    mcPoll = setInterval(() => {
      fetch('/api/mc/refresh').then(r => r.json()).then(s => {
        if (s.running) return;
        clearInterval(mcPoll);
        if (s.exit_code === 0) location.reload();
        else {
          mcBtn.disabled = false; mcBtn.textContent = '⟳ REFRESH POOL';
          alert('refresh failed (exit ' + s.exit_code + '):\\n' + (s.log_tail || []).join('\\n'));
        }
      }).catch(() => {});
    }, 5000);
  }
  // if a refresh is already in flight when the page loads, resume watching it
  fetch('/api/mc/refresh').then(r => r.json()).then(s => {
    if (s.running) { mcBtn.disabled = true; mcBtn.textContent = '⟳ REFRESHING…'; mcWatch(); }
  }).catch(() => {});
  mcBtn.onclick = () => {
    mcBtn.disabled = true; mcBtn.textContent = '⟳ REFRESHING…';
    fetch('/api/mc/refresh', {method: 'POST'})
      .then(r => { if (!r.ok) throw 0; mcWatch(); })
      .catch(() => {
        mcBtn.disabled = false; mcBtn.textContent = '⟳ REFRESH POOL';
        alert('refresh API unreachable — this button needs the page served by the NERV dashboard (restarted since this feature was added).');
      });
  };
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
