#!/usr/bin/env python
"""Prove the P0-P2 engine fixes on the fresh out-of-time sample, and pick the
BASE remake on the corrected engine.

Stage 1 decomposes the pre-fix number one fix at a time, so it is visible which
correction does the damage. Stage 2 sweeps candidate BASE exit ladders on a
hash-split TRAIN half only and reports the held-out VALID half beside it - a
ladder that only works on train is a fit, not an edge.

Usage: python p0_validate.py [--tokens ...] [--cache ...] [--entry-age 30]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt

# The live ladder as deployed for BASE before the remake.
INCUMBENT = {
    "stop_loss_pct": 40,
    "take_profits": [{"multiple": 1.35, "sell_fraction_of_remaining": 0.3}],
    "hard_tp_multiple": 20.0, "trailing_stop_pct": 30, "max_hold_min": 480,
}

PRE_FIX_FILL = {"use_candle_low": False, "stop_slip_pct": 0.0, "trail_slip_pct": 0.0}
UNBOUNDED_SETUP = {k: v for k, v in bt.DEFAULT_SETUP.items() if k != "peak_window_min"}


def stats(ms: list) -> dict:
    if not ms:
        return {"n": 0, "wr": 0.0, "avg": 0.0, "med": 0.0}
    return {"n": len(ms), "wr": 100 * sum(1 for m in ms if m > 1) / len(ms),
            "avg": sum(ms) / len(ms), "med": st.median(ms)}


def fmt(s: dict) -> str:
    return f"n={s['n']:5d}  WR={s['wr']:5.1f}%  avg={s['avg']:6.3f}  med={s['med']:6.3f}"


def half(mint: str) -> str:
    """Deterministic split that does not move as the sample grows."""
    return "train" if int(hashlib.sha1(mint.encode()).hexdigest(), 16) % 2 == 0 else "valid"


def run(tokens, cache, exits, entry_age, cost, min_vol, setup, fill, coverage):
    """-> {'train': [multiples], 'valid': [...]}, censored_count"""
    out, cen = {"train": [], "valid": []}, 0
    for t in tokens:
        candles = bt.load_cached_candles(cache, t.pool)
        if not candles:
            continue
        sim = bt.simulate(candles, exits, entry_age, cost, min_vol,
                          setup=setup, fill=fill, coverage=coverage)
        if not sim:
            continue
        cen += bool(sim.get("censored"))
        out[half(t.mint)].append(sim["multiple"])
    return out, cen


def main() -> None:
    ap = argparse.ArgumentParser()
    root = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--tokens", default=os.path.join(root, "reports", "bd_tokens_fresh.json"))
    ap.add_argument("--cache", default=os.path.join(root, ".bd_cache"))
    ap.add_argument("--entry-age", type=float, default=30.0)
    ap.add_argument("--cost-pct", type=float, default=4.0)
    ap.add_argument("--min-entry-vol", type=float, default=8000.0)
    ap.add_argument("--out", default=os.path.join("reports", "p0_validate.json"))
    args = ap.parse_args()

    tokens = bt.load_token_index(args.tokens)
    print(f"sample: {len(tokens)} tokens from {args.tokens}")
    print(f"cache:  {args.cache}\n")

    def go(exits, setup, fill, coverage):
        return run(tokens, args.cache, exits, args.entry_age, args.cost_pct,
                   args.min_entry_vol, setup, fill, coverage)

    # ---------------------------------------------------------------- stage 1
    print("=" * 78)
    print("STAGE 1 - where the pre-fix 1.6x came from (BASE, incumbent ladder)")
    print("=" * 78)
    ladders = [
        ("0. pre-fix engine (as shipped)", UNBOUNDED_SETUP, PRE_FIX_FILL, "last"),
        ("1. + P1 bounded gate lookback", bt.DEFAULT_SETUP, PRE_FIX_FILL, "last"),
        ("2. + P2 honest stop/trail fills", bt.DEFAULT_SETUP, bt.FILL_MODEL, "last"),
        ("3. + P0 censored = drop", bt.DEFAULT_SETUP, bt.FILL_MODEL, "drop"),
        ("4. + P0 censored = loss (DEFAULT)", bt.DEFAULT_SETUP, bt.FILL_MODEL, "loss"),
    ]
    stage1 = []
    for label, setup, fill, cov in ladders:
        res, cen = go(INCUMBENT, setup, fill, cov)
        pooled = res["train"] + res["valid"]
        s = stats(pooled)
        print(f"{label:36s} {fmt(s)}   censored={cen}")
        stage1.append({"step": label, "pooled": s, "censored": cen})

    # ---------------------------------------------------------------- stage 2
    print()
    print("=" * 78)
    print("STAGE 2 - BASE remake: exit ladders on the CORRECTED engine")
    print("   fitted on TRAIN only; VALID is the held-out read")
    print("=" * 78)
    H3 = 180  # P3: hold < 3h
    candidates = [
        ("incumbent  1.35x/30% bank, trail 30%, 8h", INCUMBENT),
        ("incumbent ladder, hold capped 3h", {**INCUMBENT, "max_hold_min": H3}),
        ("scalp  all out 1.20x, stop 30%", {
            "stop_loss_pct": 30, "take_profits": [{"multiple": 1.20, "sell_fraction_of_remaining": 1.0}],
            "hard_tp_multiple": 1.20, "trailing_stop_pct": 30, "max_hold_min": H3}),
        ("scalp  all out 1.30x, stop 30%", {
            "stop_loss_pct": 30, "take_profits": [{"multiple": 1.30, "sell_fraction_of_remaining": 1.0}],
            "hard_tp_multiple": 1.30, "trailing_stop_pct": 30, "max_hold_min": H3}),
        ("scalp  all out 1.50x, stop 30%", {
            "stop_loss_pct": 30, "take_profits": [{"multiple": 1.50, "sell_fraction_of_remaining": 1.0}],
            "hard_tp_multiple": 1.50, "trailing_stop_pct": 30, "max_hold_min": H3}),
        ("bank 60% @1.25x, tight trail 12%", {
            "stop_loss_pct": 30, "take_profits": [{"multiple": 1.25, "sell_fraction_of_remaining": 0.6}],
            "hard_tp_multiple": 8.0, "trailing_stop_pct": 12, "max_hold_min": H3}),
        ("bank 60% @1.35x, tight trail 15%", {
            "stop_loss_pct": 30, "take_profits": [{"multiple": 1.35, "sell_fraction_of_remaining": 0.6}],
            "hard_tp_multiple": 8.0, "trailing_stop_pct": 15, "max_hold_min": H3}),
        ("bank 80% @1.25x, trail 20% runner", {
            "stop_loss_pct": 30, "take_profits": [{"multiple": 1.25, "sell_fraction_of_remaining": 0.8}],
            "hard_tp_multiple": 10.0, "trailing_stop_pct": 20, "max_hold_min": H3}),
        ("tight stop 20%, all out 1.30x", {
            "stop_loss_pct": 20, "take_profits": [{"multiple": 1.30, "sell_fraction_of_remaining": 1.0}],
            "hard_tp_multiple": 1.30, "trailing_stop_pct": 20, "max_hold_min": H3}),
    ]
    print(f"{'ladder':44s} {'TRAIN':>34s}   {'VALID':>34s}")
    stage2 = []
    for label, exits in candidates:
        res, cen = go(exits, bt.DEFAULT_SETUP, bt.FILL_MODEL, "loss")
        tr, va = stats(res["train"]), stats(res["valid"])
        print(f"{label:44s} {fmt(tr):>34s}   {fmt(va):>34s}")
        stage2.append({"ladder": label, "exits": exits, "train": tr, "valid": va, "censored": cen})

    # ---------------------------------------------------------------- stage 3
    # 83% of fresh listings stop producing candles within 3h of listing - they
    # die. No exit ladder survives entering a bag that stops trading, so the
    # remake has to be an ENTRY question: can pre-entry candles tell a token
    # that will still be trading in an hour from one that will not?
    print()
    print("=" * 78)
    print("STAGE 3 - BASE remake: entry liveness gates (pre-entry candles only)")
    print("   'alive' = reached a real exit instead of dying mid-position")
    print("=" * 78)

    def liveness(pre, entry_ts, gate):
        """All windows are pre-entry only - no lookahead."""
        recent = [c for c in pre if c[0] > entry_ts - gate["win_min"] * 60]
        if len(recent) < gate["min_candles"]:
            return False
        vol = sum(c[5] for c in recent)
        if vol < gate["min_recent_vol"]:
            return False
        traded = sum(1 for c in recent if c[5] > 0)
        if traded / len(recent) < gate["min_traded_frac"]:
            return False
        return True

    gates = [
        ("none (incumbent)", None),
        ("15m: vol>=$2k, 80% candles traded", {"win_min": 15, "min_candles": 10, "min_recent_vol": 2000, "min_traded_frac": 0.8}),
        ("15m: vol>=$5k, 90% candles traded", {"win_min": 15, "min_candles": 12, "min_recent_vol": 5000, "min_traded_frac": 0.9}),
        ("10m: vol>=$10k, 100% candles traded", {"win_min": 10, "min_candles": 9, "min_recent_vol": 10000, "min_traded_frac": 1.0}),
        ("15m: vol>=$15k, 100% candles traded", {"win_min": 15, "min_candles": 14, "min_recent_vol": 15000, "min_traded_frac": 1.0}),
    ]
    best_exits = {**INCUMBENT, "max_hold_min": H3}
    scalp = dict(candidates[2][1])  # all out 1.20x, stop 30%
    stage3 = []
    for exname, exits in (("runner(3h)", best_exits), ("scalp 1.20x", scalp)):
        print(f"\n-- exits: {exname}")
        print(f"{'liveness gate':38s} {'TRAIN':>34s}   {'VALID':>34s}  alive%")
        for label, gate in gates:
            res = {"train": [], "valid": []}
            alive = tot = 0
            for t in tokens:
                candles = bt.load_cached_candles(args.cache, t.pool)
                if not candles:
                    continue
                entry_ts = candles[0][0] + args.entry_age * 60
                pre = [c for c in candles if c[0] <= entry_ts]
                if gate and (not pre or not liveness(pre, entry_ts, gate)):
                    continue
                sim = bt.simulate(candles, exits, args.entry_age, args.cost_pct,
                                  args.min_entry_vol, setup=bt.DEFAULT_SETUP,
                                  fill=bt.FILL_MODEL, coverage="loss")
                if not sim:
                    continue
                tot += 1
                alive += not sim.get("censored")
                res[half(t.mint)].append(sim["multiple"])
            tr, va = stats(res["train"]), stats(res["valid"])
            ap_ = f"{100 * alive / tot:.0f}%" if tot else "-"
            print(f"{label:38s} {fmt(tr):>34s}   {fmt(va):>34s}  {ap_:>5s}")
            stage3.append({"exits": exname, "gate": label, "gate_cfg": gate,
                           "train": tr, "valid": va, "alive_pct": (100 * alive / tot) if tot else 0})

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"sample": args.tokens, "entry_age_min": args.entry_age,
                   "cost_pct": args.cost_pct, "stage1": stage1, "stage2": stage2, "stage3": stage3}, f, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
