#!/usr/bin/env python
"""Hold the model to account against the live paper record.

The backtest and the Monte Carlo are only worth anything if they predict what
the bot actually does. This scores every candidate engine setting against the
closed paper positions and prints the residual, so "the model is optimistic"
becomes a number instead of an opinion.

Run it whenever the paper record grows. If the best-scoring settings drift away
from the shipped defaults in backtest.py, the defaults are stale.

Usage: python live_calibrate.py [--tokens reports/bd_tokens.json] [--workdir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt

# db, label, config -- the live books to score against
BOOKS = [
    ("memebot-asym.sqlite", "BASE", "config.asym.json"),
    ("memebot-bounce.sqlite", "BOUNCE", "config.bounce.json"),
    ("memebot-holder.sqlite", "HOLDER", "config.holder.json"),
    ("memebot-homerun.sqlite", "HOMERUN", "config.homerun.json"),
]


def live_record(db_path: str) -> dict | None:
    """Closed-position multiples from one paper book."""
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT sol_spent, sol_received, exit_reason FROM positions "
            "WHERE status='closed' AND sol_spent > 0").fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    ms = [r["sol_received"] / r["sol_spent"] for r in rows]
    if not ms:
        return None
    return {
        "n": len(ms), "wr": 100 * sum(1 for m in ms if m > 1) / len(ms),
        "avg": sum(ms) / len(ms), "med": st.median(ms), "min": min(ms),
        "zeros_pct": 100 * sum(1 for m in ms if m < 0.10) / len(ms),
    }


def model_record(cand: list, exits: dict, coverage: str, censor: dict,
                 fill: dict, entry_age: float, cost: float, min_vol: float) -> dict | None:
    ms = []
    for c in cand:
        s = bt.simulate(c, exits, entry_age, cost, min_vol, setup=bt.DEFAULT_SETUP,
                        fill=fill, coverage=coverage, censor=censor)
        if s:
            ms.append(s["multiple"])
    if not ms:
        return None
    return {
        "n": len(ms), "wr": 100 * sum(1 for m in ms if m > 1) / len(ms),
        "avg": sum(ms) / len(ms), "med": st.median(ms), "min": min(ms),
        "zeros_pct": 100 * sum(1 for m in ms if m < 0.10) / len(ms),
    }


def residual(model: dict, live: dict) -> float:
    """Scored on median and the total-loss rate, not the mean.

    With a live sample this small the mean is dominated by whichever tail
    happened to show up; the median and "how often do I lose almost everything"
    are what a small sample can actually pin down."""
    return abs(model["med"] - live["med"]) + abs(model["zeros_pct"] - live["zeros_pct"]) / 100


def fmt(d: dict | None) -> str:
    if not d:
        return "  (no data)"
    return (f"n={d['n']:4d}  WR={d['wr']:5.1f}%  avg={d['avg']:6.3f}  "
            f"med={d['med']:6.3f}  min={d['min']:6.3f}  <0.10={d['zeros_pct']:5.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    root = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--workdir", default=root,
                    help="where the live *.sqlite books live (default: repo root)")
    ap.add_argument("--tokens", default=os.path.join(root, "reports", "bd_tokens.json"))
    ap.add_argument("--cache", default=os.path.join(root, ".bd_cache"))
    ap.add_argument("--entry-age", type=float, default=30.0)
    ap.add_argument("--cost-pct", type=float, default=4.0)
    ap.add_argument("--min-entry-vol", type=float, default=8000.0)
    ap.add_argument("--out", default=os.path.join("reports", "live_calibration.json"))
    args = ap.parse_args()

    print("== LIVE PAPER RECORD ==")
    lives = {}
    for db, label, _cfg in BOOKS:
        rec = live_record(os.path.join(args.workdir, db))
        lives[label] = rec
        print(f"  {label:8s}{fmt(rec)}")

    target = lives.get("BASE")
    if not target:
        sys.exit("\nno closed BASE positions to calibrate against")

    tokens = bt.load_token_index(args.tokens)
    cand = [c for c in (bt.load_cached_candles(args.cache, t.pool) for t in tokens) if c]
    cfg_path = os.path.join(args.workdir, "config.asym.json")
    exits = json.load(open(cfg_path, encoding="utf-8"))["exits"]
    print(f"\nmodel universe: {len(cand)} tokens with candles from {os.path.basename(args.tokens)}")
    print(f"exits: {exits['stop_loss_pct']}% stop, hold {exits['max_hold_min']}m\n")

    print("== MODEL vs LIVE (scored on median + total-loss rate) ==")
    print(f"  {'setting':38s} {'WR':>6} {'avg':>7} {'med':>7} {'<0.10':>7} {'residual':>9}")
    print(f"  {'LIVE (target)':38s} {target['wr']:5.1f}% {target['avg']:7.3f} "
          f"{target['med']:7.3f} {target['zeros_pct']:6.1f}% {'-':>9}")

    trials = [("coverage=last (pre-fix)", "last", {}),
              ("coverage=loss", "loss", {})]
    for ds in (0, 15, 30, 45, 60, 75):
        trials.append((f"coverage=stop, dark slip {ds}%", "stop",
                       {"rug_pct": bt.CENSOR_MODEL["rug_pct"], "dark_slip_pct": float(ds)}))

    results, best = [], None
    for label, cov, censor in trials:
        m = model_record(cand, exits, cov, censor or bt.CENSOR_MODEL, bt.FILL_MODEL,
                         args.entry_age, args.cost_pct, args.min_entry_vol)
        if not m:
            continue
        r = residual(m, target)
        star = ""
        if best is None or r < best[0]:
            best, star = (r, label), ""
        results.append({"setting": label, "coverage": cov, "censor": censor,
                        "model": m, "residual": round(r, 4)})
        print(f"  {label:38s} {m['wr']:5.1f}% {m['avg']:7.3f} {m['med']:7.3f} "
              f"{m['zeros_pct']:6.1f}% {r:9.3f}{star}")

    best = min(results, key=lambda r: r["residual"])
    shipped = f"coverage=stop, dark slip {int(bt.CENSOR_MODEL['dark_slip_pct'])}%"
    print(f"\n  best fit : {best['setting']}  (residual {best['residual']:.3f})")
    print(f"  shipped  : {shipped}")
    print("  -> defaults match the live record"
          if best["setting"] == shipped else
          "  -> DEFAULTS ARE STALE: backtest.CENSOR_MODEL no longer matches the live record")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"live": lives, "target": target, "trials": results,
                   "best": best["setting"], "shipped": shipped,
                   "shipped_is_best": best["setting"] == shipped}, f, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
