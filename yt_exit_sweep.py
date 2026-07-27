#!/usr/bin/env python
"""Test the YouTube-genre exit thesis against the honest engine.

The genre's claim is specific and mechanical: profitable memecoin traders run a
15-25% win rate and make it work on a 3-10:1 payoff ratio, achieved by (a) not
banking until 2x, (b) trailing only AFTER a 2x, and (c) not capping the runners.
The live asym ladder does the opposite - it banks 30% at 1.35x and trails from
entry - and the honest pool shows 14.6% of all trades landing in 1.0-1.5x.

So: hold the ENTRY filter fixed (DEFAULT_SETUP, 30m age, $8k vol) and sweep only
the exit ladder. Entry-side is identical across configs, so the set of entered
tokens never changes - load their candles once, then reuse.

Train on bd_tokens.json, then re-run the top configs on bd_tokens_oot.json,
which has never been used for selection.

Usage: python yt_exit_sweep.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import backtest as bt

ENTRY_AGE_MIN = 30.0
MIN_ENTRY_VOL = 8000.0
COST_PCT = 4.0
CACHE_DIR = ".bd_cache"

LIVE_EXITS = {"stop_loss_pct": 40,
              "take_profits": [{"multiple": 1.35, "sell_fraction_of_remaining": 0.3}],
              "hard_tp_multiple": 20.0, "trailing_stop_pct": 30, "max_hold_min": 480}


def entered_candles(tokens_json: str) -> list:
    """Candle sets for every token the FIXED entry filter would have entered.
    Exit params don't affect entry, so this set is constant across the sweep."""
    tokens = bt.load_token_index(tokens_json)
    out = []
    for tok in tokens:
        candles = bt.load_cached_candles(CACHE_DIR, tok.pool)
        if not candles:
            continue
        if bt.simulate(candles, LIVE_EXITS, ENTRY_AGE_MIN, COST_PCT,
                       MIN_ENTRY_VOL, setup=bt.DEFAULT_SETUP):
            out.append((tok.symbol, candles))
    return out


def score(rows: list, exits: dict) -> dict | None:
    mults = []
    for _sym, candles in rows:
        sim = bt.simulate(candles, exits, ENTRY_AGE_MIN, COST_PCT,
                          MIN_ENTRY_VOL, setup=bt.DEFAULT_SETUP)
        if sim:
            mults.append(sim["multiple"])
    if len(mults) < 30:
        return None
    wins = [m for m in mults if m > 1]
    losses = [m for m in mults if m <= 1]
    aw = statistics.mean(wins) if wins else 1.0
    al = statistics.mean(losses) if losses else 1.0
    return {
        "n": len(mults), "wr": 100 * len(wins) / len(mults),
        "avg": statistics.mean(mults), "median": statistics.median(mults),
        "avg_win": aw, "avg_loss": al,
        "payoff": (aw - 1) / abs(al - 1) if al < 1 else float("inf"),
        "best": max(mults),
    }


def ladder(name: str, tps: list, trail: int, stop: int, hard: float) -> tuple:
    return (name, {"stop_loss_pct": stop, "take_profits": tps,
                   "hard_tp_multiple": hard, "trailing_stop_pct": trail,
                   "max_hold_min": 480})


def build_grid() -> list:
    """Exit ladders drawn from the researched rules, plus the live baseline."""
    shapes = [
        ("live 1.35x/30%", [{"multiple": 1.35, "sell_fraction_of_remaining": 0.3}]),
        ("arm@2x no-sell", [{"multiple": 2.0, "sell_fraction_of_remaining": 0.0}]),
        ("arm@3x no-sell", [{"multiple": 3.0, "sell_fraction_of_remaining": 0.0}]),
        ("bank25@2x", [{"multiple": 2.0, "sell_fraction_of_remaining": 0.25}]),
        ("bank50@2x", [{"multiple": 2.0, "sell_fraction_of_remaining": 0.5}]),
        ("genre 2/3/5/10 @25%", [{"multiple": 2.0, "sell_fraction_of_remaining": 0.25},
                                 {"multiple": 3.0, "sell_fraction_of_remaining": 0.25},
                                 {"multiple": 5.0, "sell_fraction_of_remaining": 0.25},
                                 {"multiple": 10.0, "sell_fraction_of_remaining": 0.25}]),
    ]
    grid = []
    for sname, tps in shapes:
        for trail in (25, 30, 40, 50):
            for stop in (40, 50):
                for hard in (20.0, 50.0, 1000.0):
                    hl = "uncapped" if hard > 999 else f"{hard:.0f}x"
                    grid.append(ladder(f"{sname} | trail{trail} stop{stop} hard{hl}",
                                       tps, trail, stop, hard))
    return grid


def show(tag: str, res: dict) -> None:
    print(f"  {tag:44s} n={res['n']:4d} WR={res['wr']:5.1f}% avg={res['avg']:6.3f}x "
          f"med={res['median']:5.3f}x payoff={res['payoff']:5.2f}:1 best={res['best']:6.1f}x")


def main() -> None:
    print("loading TRAIN universe (bd_tokens.json) ...", flush=True)
    train = entered_candles(os.path.join("reports", "bd_tokens.json"))
    print(f"  {len(train)} entered tokens\n", flush=True)

    grid = build_grid()
    print(f"sweeping {len(grid)} exit ladders (entry filter FIXED) ...\n", flush=True)
    scored = []
    for name, exits in grid:
        r = score(train, exits)
        if r:
            scored.append((name, exits, r))
    scored.sort(key=lambda x: -x[2]["avg"])

    base = next(r for n, _e, r in scored if n.startswith("live 1.35x/30% | trail30 stop40 hard20x"))
    print("BASELINE (what the bot runs live):")
    show("live 1.35x/30% | trail30 stop40 hard20x", base)

    print(f"\nTOP 12 ON TRAIN (of {len(scored)}):")
    for name, _e, r in scored[:12]:
        show(name, r)

    print("\nWORST 3 ON TRAIN (sanity - the sweep should have a losing tail):")
    for name, _e, r in scored[-3:]:
        show(name, r)

    print("\n" + "=" * 100)
    print("OUT-OF-TIME VALIDATION (bd_tokens_oot.json - never used for selection)")
    print("=" * 100, flush=True)
    oot = entered_candles(os.path.join("reports", "bd_tokens_oot.json"))
    print(f"  {len(oot)} entered tokens\n", flush=True)

    picks = [("BASELINE live ladder", LIVE_EXITS)] + [(n, e) for n, e, _r in scored[:5]]
    for name, exits in picks:
        r = score(oot, exits)
        if r:
            show(name, r)
        else:
            print(f"  {name:44s} too few trades on OOT")

    out = {"train_n": len(train), "oot_n": len(oot), "grid": len(grid),
           "train_top": [{"name": n, "exits": e, **r} for n, e, r in scored[:12]],
           "baseline_train": base}
    with open(os.path.join("reports", "yt_exit_sweep.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print("\nwrote reports/yt_exit_sweep.json")


if __name__ == "__main__":
    main()
