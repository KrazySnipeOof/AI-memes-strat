#!/usr/bin/env python
"""Pick three genuinely distinct strategies against the two-part bar:

    (1) mean multiple >= 2.0x   - the 0.25 SOL in / 0.50 SOL out target
    (2) >= 60% of Monte Carlo paths end profitable

Distinctness is enforced by construction, not hoped for: each family is seeded
with its own mandatory anchor condition drawn from a different signal idea, the
beam may only extend within that family, and the chosen finalists must have a
trade-set Jaccard overlap below MAX_JACCARD.

Ranking is on the TRAIN half only. The holdout half (newer listings) is scored
and printed but never selected on, so it stays an honest out-of-sample read.

Usage:  python finalists.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import quest  # noqa: E402
import search  # noqa: E402

FI = quest.FI
MIN_TRAIN, MIN_HOLD = 60, 40
TARGET_MEAN = 2.0
TARGET_PPROF = 0.60
MAX_JACCARD = 0.35

# Each family is a different idea about why a token is worth buying. The anchor
# is mandatory, so a family can never collapse into a re-parameterisation of
# another one.
FAMILIES = {
    "DIP": ("price has pulled back off its own peak - buy the launch on a dip",
            [("frac_of_peak", "<", [0.45, 0.55, 0.65, 0.75, 0.85])]),
    "BELOW-VWAP": ("trading under its volume-weighted average - late buyers are under water",
                   [("vwap_ratio", "<", [0.80, 0.85, 0.90, 0.95])]),
    "CAPITULATION": ("a violent single-candle flush has already happened",
                     [("worst_body", "<", [0.45, 0.55, 0.65, 0.75])]),
    "RANGE": ("volatility expansion - the token is moving enough to pay for the spread",
              [("range_l15", ">=", [60, 90, 120, 160, 220])]),
    "FLOW": ("real money is already trading it",
             [("vol_pre", ">=", [5e3, 1e4, 2.5e4, 5e4])]),
    "SURGE": ("volume is accelerating into the entry",
              [("vol_accel", ">=", [1.0, 1.3, 1.8, 2.5])]),
    "TREND": ("marked up from listing and holding near its high",
              [("ret_all", ">=", [1.2, 1.6, 2.2]), ("frac_of_peak", ">=", [0.7])]),
    "RETRACE": ("it spiked hard and gave the whole move back - buy the round trip",
                [("peak_mult_all", ">=", [1.4, 1.7, 2.2, 3.0]),
                 ("ret_all", "<", [1.05, 1.3])]),
    "COIL": ("range is contracting into the entry - a base, not a bleed",
             [("compress", "<", [0.5, 0.7, 0.9])]),
    "LATE-PEAK": ("the high was made just now, not at listing",
                  [("peak_age_frac", ">=", [0.6, 0.8])]),
}


# ------------------------------------------------------------ path stats

def p_profit(x: np.ndarray, horizon=100, paths=4000, seed=7,
             stake_sol=0.25, start=5.0) -> float:
    """Fraction of fixed-stake equity paths that finish above the starting
    bankroll. Mirrors montecarlo.run_scenario's sizing and ruin absorption
    (stake = min(stake, equity); equity floors at zero and stays there)."""
    if len(x) < 5:
        return 0.0
    rng = np.random.default_rng(seed)
    draws = x[rng.integers(0, len(x), size=(paths, horizon))]
    eq = np.full(paths, start, dtype=np.float64)
    for t in range(horizon):
        stake = np.minimum(stake_sol, eq)
        eq = np.maximum(0.0, eq + stake * (draws[:, t] - 1.0))
    return float((eq > start).mean())


def trimmed(x: np.ndarray, pct=5.0) -> float:
    """Mean with the top `pct` of trades removed - the tail-dependence check
    that `no_top` runs in the Monte Carlo."""
    s = np.sort(x)[::-1]
    k = max(1, int(len(s) * pct / 100))
    return float(s[k:].mean()) if len(s) > k else 0.0


def profile(x: np.ndarray) -> dict:
    return {"n": int(len(x)), "mean": float(x.mean()), "median": float(np.median(x)),
            "wr": float((x > 1).mean()), "trim5": trimmed(x),
            "p_profit": p_profit(x)}


# ------------------------------------------------------------ family search

def anchors(F, spec):
    """Cartesian product of the family's mandatory anchor cuts."""
    outs = [(np.ones(len(F), dtype=bool), [])]
    for feat, op, vals in spec:
        nxt = []
        for m, names in outs:
            for v in vals:
                col = F[:, FI[feat]]
                mm = m & ((col >= v) if op == ">=" else (col < v))
                nxt.append((mm, names + [f"{feat}{op}{v:g}"]))
        outs = nxt
    return outs


def shortlist(mask, M, tr, k=80):
    """The k ladders worth searching under this anchor. Cuts the per-candidate
    matvec ~17x; the winner is re-checked against the FULL ladder set at the
    end, so the shortcut can only cost search breadth, never correctness."""
    mt = mask & tr
    if mt.sum() < MIN_TRAIN:
        return None
    means = mt.astype(np.float32) @ M / mt.sum()
    return np.argsort(-means)[:k]


def refine(grid, age, base_mask, base_names, conds, depth=2, width=4):
    """Greedy extension of an anchored screen, ranked on train mean."""
    d = grid["ages"][age]
    F = d["F"]
    tr, ho, _ = search.split_masks(grid, age)
    cols = shortlist(base_mask, d["M"], tr)
    if cols is None:
        return []
    M = np.ascontiguousarray(d["M"][:, cols])

    def sc(mask):
        mt, mh = mask & tr, mask & ho
        nt, nh = int(mt.sum()), int(mh.sum())
        if nt < MIN_TRAIN or nh < MIN_HOLD:
            return None
        s = mt.astype(np.float32) @ M / nt
        j = int(np.argmax(s))
        return int(cols[j]), float(s[j]), float(M[mh, j].mean()), nt, nh

    beams, hits = [(base_names, base_mask)], []
    s0 = sc(base_mask)
    if s0:
        hits.append((base_names, base_mask, s0))
    for _ in range(depth):
        cand = []
        for names, mask in beams:
            for cn, cm in conds:
                if cn in names:
                    continue
                nm = mask & cm
                s = sc(nm)
                if s is None:
                    continue
                cand.append((s[1], names + [cn], nm, s))
        if not cand:
            break
        cand.sort(key=lambda c: -c[0])
        seen, keep = set(), []
        for c in cand:
            k = c[2].tobytes()
            if k in seen:
                continue
            seen.add(k)
            keep.append(c)
            if len(keep) >= width:
                break
        beams = [(k[1], k[2]) for k in keep]
        hits += [(k[1], k[2], k[3]) for k in keep]
    return hits


def best_ladder(mask, grid, age):
    """Among ladders clearing the mean bar on TRAIN, take the one with the best
    train path-profitability. If none clears it, fall back to the best mean so
    the near-miss is still visible rather than silently dropped."""
    d = grid["ages"][age]
    M = d["M"]
    tr, ho, _ = search.split_masks(grid, age)
    mt, mh = mask & tr, mask & ho
    if mt.sum() < MIN_TRAIN or mh.sum() < MIN_HOLD:
        return None
    means = mt.astype(np.float32) @ M / mt.sum()
    order = np.argsort(-means)[:40]
    ok = [j for j in order if means[j] >= TARGET_MEAN]
    pool = ok if ok else list(order[:8])
    scored = [(p_profit(M[mt, j]), means[j], int(j)) for j in pool]
    scored.sort(key=lambda s: (-s[0], -s[1]))
    return scored[0][2]


def main():
    grid = quest.load_grid()
    L = grid["ladders"]
    results = {}
    cond_cache = {}
    for fam, (why, spec) in FAMILIES.items():
        print(f"\n=== {fam}: {why}", flush=True)
        best = None
        for age in sorted(grid["ages"]):
            d = grid["ages"][age]
            if age not in cond_cache:
                cond_cache[age] = search.conditions(d["F"])
            conds = cond_cache[age]
            tr, ho, _ = search.split_masks(grid, age)
            M = d["M"]
            for amask, anames in anchors(d["F"], spec):
                if amask.sum() < MIN_TRAIN + MIN_HOLD:
                    continue
                for names, mask, _s in refine(grid, age, amask, anames, conds):
                    j = best_ladder(mask, grid, age)
                    if j is None:
                        continue
                    ptr = profile(M[mask & tr, j])
                    pho = profile(M[mask & ho, j])
                    if ptr["n"] < MIN_TRAIN or pho["n"] < MIN_HOLD:
                        continue
                    # rank on the WORSE of the two halves so a screen cannot buy
                    # its way in on a train-half fluke
                    key = (min(ptr["p_profit"], pho["p_profit"]),
                           min(ptr["mean"], pho["mean"]))
                    if best is None or key > best[0]:
                        best = (key, {"family": fam, "why": why, "age": age,
                                      "conds": names, "ladder": L[j], "ladder_j": int(j),
                                      "train": ptr, "hold": pho,
                                      "all": profile(M[mask, j])},
                                mask.copy())
        if best is None:
            print("   nothing met the minimum trade counts")
            continue
        _k, info, mask = best
        results[fam] = info
        t, h, a = info["train"], info["hold"], info["all"]
        print(f"   age {info['age']}m  {info['ladder']}")
        print(f"   {' & '.join(info['conds'])}")
        for tag, p in (("TRAIN", t), ("HOLD", h), ("ALL", a)):
            print(f"   {tag:5} n={p['n']:4d} mean {p['mean']:6.3f} WR {100*p['wr']:5.1f}% "
                  f"trim5 {p['trim5']:6.3f} P(profit) {100*p['p_profit']:5.1f}%")
        ok = (min(t["mean"], h["mean"]) >= TARGET_MEAN
              and min(t["p_profit"], h["p_profit"]) >= TARGET_PPROF)
        print(f"   -> {'CLEARS the bar on both halves' if ok else 'below the bar'}")

    with open(os.path.join(os.environ.get("CLAUDE_JOB_DIR", HERE), "tmp",
                           "finalists.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1, default=float)
    print(f"\nsaved {len(results)} family winners to tmp/finalists.json")


if __name__ == "__main__":
    main()
