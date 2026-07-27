#!/usr/bin/env python
"""Choose three finalists that are actually three strategies.

Seven of the ten family winners clear the 2.0x / 60%-of-paths bar, but a family
label is not evidence of independence - several of them are different wordings
of "the token spiked in its first three minutes and pulled back". This picks the
triple that maximises the WORST pairwise separation between the traded token
sets, subject to every member still clearing the bar on both halves.

Usage:  python select.py
"""
from __future__ import annotations

import itertools
import json
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import quest  # noqa: E402
from finalists import TARGET_MEAN, TARGET_PPROF  # noqa: E402

TMP = os.path.join(os.environ.get("CLAUDE_JOB_DIR", HERE), "tmp")
COND = re.compile(r"^([a-z0-9_]+)(>=|<)(-?[\d.]+(?:[eE][-+]?\d+)?)$")
MAX_JACCARD = 0.35
ADJ_FLOOR = 1.5   # the screen must still beat this one minute later


def mask_for(grid, age, conds):
    F = grid["ages"][age]["F"]
    m = np.ones(len(F), dtype=bool)
    for c in conds:
        g = COND.match(c)
        col, v = F[:, quest.FI[g.group(1)]], float(g.group(3))
        m &= (col >= v) if g.group(2) == ">=" else (col < v)
    return m


def main():
    grid = quest.load_grid()
    mints = np.array(grid["meta"]["mint"])
    cands = json.load(open(os.path.join(TMP, "finalists.json"), encoding="utf-8"))

    ages = sorted(grid["ages"])

    def adjacent_mean(s):
        """Same screen, same ladder, entered at the next age the grid holds.
        Live the bot enters when it NOTICES a listing, not on a stopwatch, so a
        screen whose edge evaporates one minute later is not implementable."""
        later = [a for a in ages if a > s["age"]]
        if not later:
            return None
        a = later[0]
        d = grid["ages"][a]
        m = mask_for(grid, a, s["conds"])
        if m.sum() < 20:
            return None
        return a, float(d["M"][m, s["ladder_j"]].mean()), int(m.sum())

    def worst_block(s, m):
        """Weakest of five equal-count chronological blocks. A screen found by
        beam search over ~350 cuts x 8 ages x 2000 ladders will always look
        good on the half it was fitted to; holding up in EVERY period is the
        statistic that is hard to fake, so it ranks the triples."""
        d = grid["ages"][s["age"]]
        cr = grid["meta"]["created"][d["ti"]][m]
        x = d["M"][m, s["ladder_j"]]
        o = np.argsort(cr)
        return min(float(b.mean()) for b in np.array_split(x[o], 5) if len(b))

    ok, sets, adj, blocks = {}, {}, {}, {}
    rejected = []
    for name, s in cands.items():
        worst_mean = min(s["train"]["mean"], s["hold"]["mean"])
        worst_pp = min(s["train"]["p_profit"], s["hold"]["p_profit"])
        if worst_mean < TARGET_MEAN or worst_pp < TARGET_PPROF:
            continue
        a = adjacent_mean(s)
        adj[name] = a
        if a is None or a[1] < ADJ_FLOOR:
            rejected.append((name, worst_mean, a))
            continue
        m = mask_for(grid, s["age"], s["conds"])
        ok[name] = (worst_mean, worst_pp)
        blocks[name] = worst_block(s, m)
        sets[name] = set(mints[grid["ages"][s["age"]]["ti"][m]])

    for name, wm, a in rejected:
        txt = "no later age available" if a is None else \
            f"drops to {a[1]:.2f}x at {a[0]}m entry (n={a[2]})"
        print(f"  rejected {name:13} (both halves {wm:.2f}x) - {txt}")
    print(f"\n{len(ok)} of {len(cands)} families clear the bar AND survive a "
          f"one-minute-late entry: {', '.join(sorted(ok))}\n")
    print("pairwise Jaccard overlap of traded token sets:")
    names = sorted(ok)
    print("               " + "".join(f"{n[:11]:>13}" for n in names))
    J = {}
    for a in names:
        row = f"  {a[:13]:13}"
        for b in names:
            j = (len(sets[a] & sets[b]) / max(len(sets[a] | sets[b]), 1)) if a != b else 1.0
            J[(a, b)] = j
            row += f"{j:13.2f}"
        print(row)

    # distinctness is governed by the WORST pair, i.e. the LARGEST overlap in
    # the triple - taking the min here would have waved through a triple whose
    # DIP and RANGE legs share 52% of their trades
    print("\nper-family weakest chronological block (of five):")
    for n in names:
        print(f"  {n:13} worst block {blocks[n]:5.3f}x   both halves >= "
              f"{ok[n][0]:.3f}x")

    adm = []
    for tri in itertools.combinations(names, 3):
        worst_pair = max(J[(a, b)] for a, b in itertools.combinations(tri, 2))
        worst_mean = min(ok[n][0] for n in tri)
        worst_blk = min(blocks[n] for n in tri)
        if worst_pair >= MAX_JACCARD:
            continue
        adm.append((round(worst_blk, 3), round(worst_mean, 3), -worst_pair, tri))
    if not adm:
        print(f"\nno triple has every pair under Jaccard {MAX_JACCARD}")
        return
    adm.sort(reverse=True)
    print(f"\nadmissible triples (all pairs under Jaccard {MAX_JACCARD}), ranked by "
          f"weakest block, then weakest half, then separation:")
    for wb, wm, npair, tri in adm[:8]:
        print(f"  worst block {wb:5.3f}x  worst half {wm:.3f}x  overlap<={-npair:.2f}"
              f"   {' + '.join(tri)}")
    wb, wm, npair, tri = adm[0]
    sep = -npair
    print(f"\nCHOSEN TRIPLE (worst pairwise overlap {sep:.2f}, "
          f"worst half-mean {wm:.3f}x):")
    for n in tri:
        s = cands[n]
        a = adj[n]
        print(f"  {n:13} age {s['age']}m  {s['ladder']:22} n={s['all']['n']:4d}  "
              f"(at {a[0]}m entry: {a[1]:.2f}x)  {' & '.join(s['conds'])}")
    json.dump(list(tri), open(os.path.join(TMP, "chosen.json"), "w"), indent=1)
    print(f"\nwrote tmp/chosen.json -> python validate.py {' '.join(tri)}")


if __name__ == "__main__":
    main()
