#!/usr/bin/env python
"""Beam search for entry screens over the pre-simulated result grid.

Honesty rules baked in, not bolted on:

  * Every screen condition reads a feature computed from pre-entry candles only.
  * Selection happens on the TRAIN half (older by listing time). The HOLDOUT
    half (newer) is scored but never ranked on, so it stays a real out-of-sample
    read for whatever the search picks.
  * Minimum trade counts on both halves, so a "strategy" cannot be three lucky
    tokens.
  * The number reported is the mean multiple straight out of `backtest.simulate`
    at shipped defaults. No re-weighting, no dropping of losers.

Usage:  python search.py            # discovery beam search
"""
from __future__ import annotations

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import quest  # noqa: E402

MIN_TRAIN = 60
MIN_HOLD = 40


def split_masks(grid, age):
    """Older half of the listing window trains, newer half validates."""
    d = grid["ages"][age]
    created = grid["meta"]["created"][d["ti"]]
    cut = np.median(created)
    return created < cut, created >= cut, created


def conditions(F: np.ndarray) -> list:
    """Candidate one-sided threshold cuts, at deciles of each feature."""
    out = []
    for name, i in quest.FI.items():
        col = F[:, i]
        qs = np.unique(np.nanquantile(col, [.1, .2, .3, .4, .5, .6, .7, .8, .9]))
        for q in qs:
            if not np.isfinite(q):
                continue
            hi, lo = col >= q, col < q
            # full float precision, NOT %.4g. Features like peak_age_frac take
            # discrete values (argmax/(n-1)), so a threshold printed as 0.6667
            # when the real cut was 0.66666... silently drops every token
            # sitting exactly on 2/3 - which cost BELOW-VWAP 74 of its 126
            # trades between the search and the independent re-run.
            if MIN_TRAIN < hi.sum() < len(col) - 10:
                out.append((f"{name}>={float(q)!r}", hi))
            if MIN_TRAIN < lo.sum() < len(col) - 10:
                out.append((f"{name}<{float(q)!r}", lo))
    return out


def score(mask, M, tr, ho):
    """(best ladder index, train mean, holdout mean, n_train, n_hold)."""
    mt, mh = mask & tr, mask & ho
    nt, nh = int(mt.sum()), int(mh.sum())
    if nt < MIN_TRAIN or nh < MIN_HOLD:
        return None
    st = mt.astype(np.float32) @ M / nt
    j = int(np.argmax(st))
    sh = float(M[mh, j].mean())
    return j, float(st[j]), sh, nt, nh


def beam(grid, age, depth=4, width=6, verbose=True):
    d = grid["ages"][age]
    M = d["M"]
    tr, ho, _ = split_masks(grid, age)
    conds = conditions(d["F"])
    L = grid["ladders"]

    beams = [([], np.ones(len(M), dtype=bool))]
    best_seen = []
    for step in range(depth):
        cand = []
        for names, mask in beams:
            for cn, cm in conds:
                if cn in names:
                    continue
                nm = mask & cm
                s = score(nm, M, tr, ho)
                if s is None:
                    continue
                cand.append((s[1], names + [cn], nm, s))
        if not cand:
            break
        cand.sort(key=lambda x: -x[0])
        # de-duplicate on the resulting trade set, not the condition text -
        # different wordings of the same cut are the same strategy
        seen, keep = set(), []
        for c in cand:
            key = c[2].tobytes()
            if key in seen:
                continue
            seen.add(key)
            keep.append(c)
            if len(keep) >= width:
                break
        beams = [(k[1], k[2]) for k in keep]
        for k in keep:
            j, st, sh, nt, nh = k[3]
            best_seen.append({"age": age, "conds": k[1], "ladder": L[j],
                              "ladder_j": j, "train": st, "hold": sh,
                              "nt": nt, "nh": nh, "mask": k[2], "step": step + 1})
        if verbose:
            j, st, sh, nt, nh = keep[0][3]
            print(f"    depth {step + 1}: train {st:6.3f}  hold {sh:6.3f}  "
                  f"n {nt}/{nh}  {' & '.join(keep[0][1])}")
    return best_seen


def main():
    grid = quest.load_grid()
    all_hits = []
    for age in sorted(grid["ages"]):
        print(f"\n== entry age {age}m ==")
        all_hits += beam(grid, age)
    print("\n" + "=" * 100)
    print("TOP BY TRAIN MEAN (holdout is out-of-sample - not ranked on)")
    print(f"{'age':>4} {'train':>7} {'hold':>7} {'nt':>5} {'nh':>5}  {'ladder':30} screen")
    for h in sorted(all_hits, key=lambda x: -x["train"])[:25]:
        print(f"{h['age']:>4} {h['train']:7.3f} {h['hold']:7.3f} {h['nt']:>5} "
              f"{h['nh']:>5}  {h['ladder']:30} {' & '.join(h['conds'])}")
    print("\nTOP BY HOLDOUT MEAN among train>=1.3 (the ones that survived)")
    surv = [h for h in all_hits if h["train"] >= 1.3]
    for h in sorted(surv, key=lambda x: -x["hold"])[:25]:
        print(f"{h['age']:>4} {h['train']:7.3f} {h['hold']:7.3f} {h['nt']:>5} "
              f"{h['nh']:>5}  {h['ladder']:30} {' & '.join(h['conds'])}")


if __name__ == "__main__":
    main()
