#!/usr/bin/env python
"""Strategy search against the real (calibrated) engine.

Architecture note - why this is fast AND still honest:

  `backtest.simulate()` does not know or care why a token was entered. So for a
  fixed entry age the exit machine can be run ONCE per (token, exit-ladder) and
  every candidate entry screen evaluated afterwards as a boolean mask over the
  same result matrix. Nothing about the screen can leak into the simulation,
  and every number printed comes out of the shipped engine with shipped
  defaults (coverage="stop", FILL_MODEL low_weight 0.7, rug 10%, dark slip 45%).

  The features a screen may use are computed from `pre` candles only - the
  exact same slice `simulate()` uses to set the entry price - so there is no
  lookahead by construction.

Usage:
    python quest.py grid          # build the result matrices (slow, cached)
    python quest.py search        # search screens x ladders, train/holdout
"""
from __future__ import annotations

import itertools
import json
import os
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# NOTE: no os.chdir at import time. `strategies.py` imports this module and the
# live bot imports that, so a module-level chdir would silently move the
# running bot's working directory out from under its own relative paths.
# build_grid() chdirs for itself.

import backtest as bt  # noqa: E402
import universe as uni  # noqa: E402

TMP = os.path.join(os.environ.get("CLAUDE_JOB_DIR", HERE), "tmp")
COST_PCT = 4.0

# ------------------------------------------------------------------ features

FEATS = ["n_pre", "age", "vol_pre", "vol_l5", "vol_l15", "vol_accel", "ret_all",
         "ret_l5", "ret_l15", "frac_of_peak", "range_l15", "active_l15",
         "green_l15", "worst_body", "vwap_ratio", "compress", "px_usd_vol",
         "n_dry", "up_candles_all", "peak_mult_all",
         "x_from_low", "peak_age_frac", "close_pos", "vol_conc", "vol_l1",
         "up_streak", "best_body", "ret_l3"]
FI = {k: i for i, k in enumerate(FEATS)}


def features(ts: np.ndarray, o: np.ndarray, h: np.ndarray, l: np.ndarray,
             c: np.ndarray, v: np.ndarray, entry: float, age: float) -> np.ndarray:
    """Entry-time-only descriptors. Every input array is the PRE slice."""
    f = np.zeros(len(FEATS), dtype=np.float64)
    n = len(c)
    f[FI["n_pre"]] = n
    f[FI["age"]] = age
    f[FI["vol_pre"]] = v.sum()
    l5, l15 = c[-5:], c[-15:]
    f[FI["vol_l5"]] = v[-5:].sum()
    f[FI["vol_l15"]] = v[-15:].sum()
    mean_v = v.mean() if n else 0.0
    f[FI["vol_accel"]] = (v[-5:].mean() / mean_v) if mean_v > 0 else 0.0
    f[FI["ret_all"]] = entry / c[0] if c[0] > 0 else 0.0
    f[FI["ret_l5"]] = entry / l5[0] if l5[0] > 0 else 0.0
    f[FI["ret_l15"]] = entry / l15[0] if l15[0] > 0 else 0.0
    pk = h.max()
    f[FI["frac_of_peak"]] = entry / pk if pk > 0 else 0.0
    f[FI["peak_mult_all"]] = pk / c[0] if c[0] > 0 else 0.0
    h15, l15lo = h[-15:], l[-15:]
    f[FI["range_l15"]] = 100 * (h15.max() - l15lo.min()) / entry if entry > 0 else 0.0
    f[FI["active_l15"]] = float((v[-15:] > 0).mean())
    f[FI["green_l15"]] = float((c[-15:] > o[-15:]).mean())
    body = np.where(o > 0, c / np.maximum(o, 1e-30), 1.0)
    f[FI["worst_body"]] = body.min()
    tv = v.sum()
    f[FI["vwap_ratio"]] = (entry / (((h + l + c) / 3 * v).sum() / tv)) if tv > 0 else 0.0
    # recent range vs the range before it: <1 = coiling into entry
    if n >= 20:
        r_new = (h[-10:].max() - l[-10:].min()) / max(c[-10:].mean(), 1e-30)
        r_old = (h[-20:-10].max() - l[-20:-10].min()) / max(c[-20:-10].mean(), 1e-30)
        f[FI["compress"]] = r_new / r_old if r_old > 0 else 0.0
    else:
        f[FI["compress"]] = 1.0
    f[FI["px_usd_vol"]] = v[-15:].sum() / 15.0
    f[FI["n_dry"]] = float((v[-15:] <= 0).sum())
    f[FI["up_candles_all"]] = float((c > o).mean())
    lo = l.min()
    f[FI["x_from_low"]] = entry / lo if lo > 0 else 0.0
    # where in the pre-entry window the high happened: ~0 = spiked at listing
    # and has been bleeding since, ~1 = making highs right now
    f[FI["peak_age_frac"]] = float(np.argmax(h)) / max(n - 1, 1)
    rng = h[-1] - l[-1]
    f[FI["close_pos"]] = float((entry - l[-1]) / rng) if rng > 0 else 0.5
    # one candle holding most of the volume is the wash-trade fingerprint
    f[FI["vol_conc"]] = float(v.max() / tv) if tv > 0 else 1.0
    f[FI["vol_l1"]] = float(v[-1])
    streak = 0
    for i in range(n - 1, -1, -1):
        if c[i] > o[i]:
            streak += 1
        else:
            break
    f[FI["up_streak"]] = float(streak)
    f[FI["best_body"]] = float(body.max())
    f[FI["ret_l3"]] = entry / c[-3] if n >= 3 and c[-3] > 0 else 1.0
    return f


# ------------------------------------------------------------------- ladders

def ladders() -> list:
    """Exit doctrines to sweep. Named so a result can be traced back."""
    out = {}

    def add(name, stop, tps, hard, trail, hold):
        out[name] = {"stop_loss_pct": stop, "take_profits": tps,
                     "hard_tp_multiple": hard, "trailing_stop_pct": trail,
                     "max_hold_min": hold}

    tp_sets = {
        "none": [],
        "b12": [{"multiple": 1.2, "sell_fraction_of_remaining": 0.5}],
        "b13": [{"multiple": 1.3, "sell_fraction_of_remaining": 0.6}],
        "b15": [{"multiple": 1.5, "sell_fraction_of_remaining": 0.5}],
        "b15h": [{"multiple": 1.5, "sell_fraction_of_remaining": 0.75}],
        "b2": [{"multiple": 2.0, "sell_fraction_of_remaining": 0.5}],
        "b2h": [{"multiple": 2.0, "sell_fraction_of_remaining": 0.75}],
        "lad": [{"multiple": 1.5, "sell_fraction_of_remaining": 0.4},
                {"multiple": 3.0, "sell_fraction_of_remaining": 0.5}],
        "lad3": [{"multiple": 2.0, "sell_fraction_of_remaining": 0.34},
                 {"multiple": 4.0, "sell_fraction_of_remaining": 0.5},
                 {"multiple": 8.0, "sell_fraction_of_remaining": 0.5}],
        # Runner ladders: the first rung sits high enough that most trades never
        # reach it, so the position keeps its full tail exposure - but once it
        # does fire it banks real money AND arms the trailing stop (simulate()
        # gates the trail on stage > 0), which is the only way to protect a
        # winner that has not yet reached the hard cap.
        "r3": [{"multiple": 3.0, "sell_fraction_of_remaining": 0.4}],
        "r5": [{"multiple": 5.0, "sell_fraction_of_remaining": 0.5}],
        "r10": [{"multiple": 10.0, "sell_fraction_of_remaining": 0.5}],
        "r36": [{"multiple": 3.0, "sell_fraction_of_remaining": 0.3},
                {"multiple": 6.0, "sell_fraction_of_remaining": 0.5}],
    }
    for tk, tv in tp_sets.items():
        # the trailing stop only arms after a take-profit rung fires (simulate()
        # gates it on stage > 0), so sweeping it for the no-TP ladders would
        # just produce identical duplicates.
        trails = (35,) if tk == "none" else (20, 35, 50)
        for stop in (30, 45, 60):
            for trail in trails:
                for hold in (5, 10, 15, 20, 30, 45):
                    for hard in (10.0, 25.0, 50.0):
                        add(f"{tk}/s{stop}/t{trail}/h{hold}/x{int(hard)}",
                            stop, tv, hard, trail, hold)
    return list(out.items())


# ---------------------------------------------------------------- simulation

_UNI = None


def _init():
    global _UNI
    _UNI = uni.load()


def _one_token(job):
    """(token index, entry_age) -> (feature vector, [multiple per ladder])."""
    ti, age = job
    t = _UNI[ti]
    ts, ohlcv = t["ts"], t["ohlcv"].astype(np.float64)
    created = int(ts[0])
    entry_ts = created + age * 60
    pre_m = ts <= entry_ts
    npre = int(pre_m.sum())
    if npre < 3 or npre == len(ts):
        return None
    o, h, l, c, v = (ohlcv[pre_m, i] for i in range(5))
    entry = float(c[-1])
    if entry <= 0:
        return None
    f = features(ts[pre_m], o, h, l, c, v, entry, age)
    candles = [(int(ts[i]), *(float(x) for x in ohlcv[i])) for i in range(len(ts))]
    mults = np.zeros(len(_LADDERS), dtype=np.float32)
    cens = np.zeros(len(_LADDERS), dtype=bool)
    for j, (_n, ex) in enumerate(_LADDERS):
        r = bt.simulate(candles, ex, age, COST_PCT, 0.0, setup=None)
        if r is None:
            return None
        mults[j] = r["multiple"]
        cens[j] = r["censored"]
    return ti, age, f, mults, cens


_LADDERS = ladders()


def build_grid(ages):
    os.chdir(HERE)
    _init()
    n = len(_UNI)
    print(f"grid: {n} tokens x {len(ages)} ages x {len(_LADDERS)} ladders "
          f"= {n * len(ages) * len(_LADDERS):,} simulate() calls")
    jobs = [(i, a) for a in ages for i in range(n)]
    res = {a: {"ti": [], "F": [], "M": [], "C": []} for a in ages}
    with ProcessPoolExecutor(max_workers=16, initializer=_init) as ex:
        for k, r in enumerate(ex.map(_one_token, jobs, chunksize=32)):
            if k % 20000 == 0:
                print(f"  {k:,}/{len(jobs):,}", flush=True)
            if r is None:
                continue
            ti, age, f, m, c = r
            d = res[age]
            d["ti"].append(ti)
            d["F"].append(f)
            d["M"].append(m)
            d["C"].append(c)
    out = {}
    for a, d in res.items():
        if not d["ti"]:
            continue
        out[a] = {"ti": np.array(d["ti"]), "F": np.array(d["F"]),
                  "M": np.array(d["M"]), "C": np.array(d["C"])}
        print(f"  age {a}m: {len(d['ti'])} entries")
    meta = {"created": np.array([t["created"] for t in _UNI]),
            "symbol": [t["symbol"] for t in _UNI],
            "mint": [t["mint"] for t in _UNI]}
    path = os.path.join(TMP, "grid.pkl")
    with open(path, "wb") as f:
        pickle.dump({"ages": out, "meta": meta,
                     "ladders": [n for n, _ in _LADDERS]}, f, 4)
    print(f"wrote {path} ({os.path.getsize(path) / 1e6:.0f} MB)")


def load_grid():
    with open(os.path.join(TMP, "grid.pkl"), "rb") as f:
        return pickle.load(f)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "grid"
    if cmd == "grid":
        build_grid([3, 4, 5, 7, 10, 15, 20, 30])
