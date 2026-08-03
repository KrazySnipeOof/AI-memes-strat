#!/usr/bin/env python
"""Multiple-testing correction for the parameter sweeps.

Every sweep in this repo reports "best of N configs" as if it were a single
measurement. It is not. sweep.py tests 5040 combos, sweep4.py tests 2160,
sweepvol.py tested 768 -- and the top of any such leaderboard is partly the
best config and partly the luckiest draw. That gap is the selection bias, and
until you subtract it you cannot tell a real edge from the right tail of noise.

This module subtracts it two ways, both stdlib-only (no numpy/scipy -- the
repo's requirements.txt is `requests` and it stays that way, see mlgate.py).

  1. EXPECTED-MAX HAIRCUT (Bailey & Lopez de Prado). Under the null that every
     config shares one true performance level, the best of N observations is
     drawn from a maximum-order statistic, not the mean. Its expectation is

         E[max] = sd * [ (1 - g) * Z(1 - 1/N) + g * Z(1 - 1/(N*e)) ]

     with g = Euler-Mascheroni and Z the inverse normal CDF. Anything at or
     below that line is what a dead strategy family looks like from the top.
     Applied to per-trade Sharpe, which needs std -- so the verdict is only
     available on sweeps re-run after this commit.

     The same machinery shrinks the winner's WINRATE for selection bias, and
     that needs only (n, wr), so it re-grades every sweep JSON already on
     disk. Shrinkage only, not a verdict: across a grid that mixes exit
     families the winrate spread is structural, not noise, so there is no
     honest pass/fail to be had from winrates alone. Supply the ladder's
     breakeven winrate (--breakeven-wr) if you want one.

  2. DEFLATED SHARPE RATIO. PSR of the observed Sharpe against E[max SR] as
     the benchmark, with the third and fourth moments carried through -- which
     matters here more than in equities: memecoin multiples are floored at 0,
     have a fat right tail, and a normal-theory t-test overstates significance.

Both assume independent trials. Sweep configs share tokens and are heavily
correlated, so the true trial count is smaller than N and the haircut printed
here is CONSERVATIVE. --eff-trials lets you say how much smaller; there is no
honest way to estimate it from summary stats alone, so it is not guessed.

Library use (what the sweeps call):

    import mtest
    mtest.print_haircut(results, half="train", attempted=len(combos))

CLI use (re-grading a finished run):

    python mtest.py reports/sweep4_results.json
    python mtest.py reports/volmc_train.json --half pooled --top 10
    python mtest.py reports/sweep_results.json --eff-trials 200
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from statistics import NormalDist

_N = NormalDist()
EULER = 0.5772156649015329
BREAKEVEN = 1.0  # a trade multiple of 1.0x is the no-edge outcome


# ---------------------------------------------------------------- moments ---

def moments(mults):
    """Full moment set for a list of trade multiples.

    Returns the same keys sweep.stats() emits, so the two are interchangeable.
    skew/kurt are population moments (kurt is NON-excess: 3.0 == normal),
    which is the convention the PSR formula below expects.
    """
    n = len(mults)
    if n == 0:
        return None
    avg = statistics.fmean(mults)
    med = statistics.median(mults)
    wr = 100.0 * sum(1 for m in mults if m > BREAKEVEN) / n
    if n < 2:
        return {"n": n, "avg": avg, "median": med, "wr": wr,
                "std": 0.0, "skew": 0.0, "kurt": 0.0}
    std = statistics.stdev(mults)
    m2 = sum((m - avg) ** 2 for m in mults) / n
    if m2 <= 0:
        skew = kurt = 0.0
    else:
        m3 = sum((m - avg) ** 3 for m in mults) / n
        m4 = sum((m - avg) ** 4 for m in mults) / n
        skew = m3 / m2 ** 1.5
        kurt = m4 / m2 ** 2
    return {"n": n, "avg": avg, "median": med, "wr": wr,
            "std": std, "skew": skew, "kurt": kurt}


def sharpe(avg, std, benchmark=BREAKEVEN):
    """Per-trade Sharpe: excess over breakeven per unit of trade dispersion.

    Deliberately NOT annualised -- these strategies have no fixed trade
    frequency, and scaling by a made-up trades-per-year would only make the
    number look bigger.
    """
    if std is None or std <= 0:
        return None
    return (avg - benchmark) / std


# ------------------------------------------------------- order statistics ---

def expected_max_z(n_trials):
    """E[max of n_trials standard normals], Bailey/Lopez de Prado approximation."""
    n = int(n_trials)
    if n < 2:
        return 0.0
    a = _N.inv_cdf(1.0 - 1.0 / n)
    b = _N.inv_cdf(1.0 - 1.0 / (n * math.e))
    return (1.0 - EULER) * a + EULER * b


def expected_max(sd, n_trials):
    """Expected best-of-N under the null, in the units of `sd`."""
    if sd is None or sd <= 0:
        return 0.0
    return sd * expected_max_z(n_trials)


def psr(sr, sr_benchmark, n, skew, kurt):
    """Probabilistic Sharpe Ratio: P(true SR > sr_benchmark) given the sample.

    Pass sr_benchmark = E[max SR under the null] and this is the Deflated
    Sharpe Ratio.
    """
    if sr is None or n is None or n < 3:
        return None
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return None
    return _N.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom))


def one_sided_p(sr, n):
    """P(observing this Sharpe or better | no edge). Normal approx; n>=30."""
    if sr is None or not n:
        return None
    return 1.0 - _N.cdf(sr * math.sqrt(n))


def benjamini_hochberg(pvals):
    """BH q-values, same order as the input (None passes through)."""
    idx = [i for i, p in enumerate(pvals) if p is not None]
    m = len(idx)
    q = [None] * len(pvals)
    if not m:
        return q
    order = sorted(idx, key=lambda i: pvals[i])
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        prev = min(prev, pvals[i] * m / (rank + 1))
        q[i] = min(1.0, prev)
    return q


# ---------------------------------------------------------------- analysis --

def _half(row, half):
    """Pull the stats block for `half`; sweepvol-style flat rows count as pooled."""
    if half in row and isinstance(row[half], dict):
        return row[half]
    if half == "pooled" and "avg" in row and "n" in row:
        return row
    return None


def analyse(rows, half="train", eff_trials=None, min_n=1, attempted=None,
            breakeven_wr=None):
    """Deflate a sweep leaderboard.

    rows: sweep result dicts, each carrying rows[half] = {n, wr, avg, [std,
          skew, kurt]}. Rows missing the half, or below min_n, are dropped.
    Returns a report dict; `scored` rows are annotated in place under "mtest".
    """
    scored = []
    for r in rows:
        s = _half(r, half)
        if s and s.get("n", 0) >= min_n:
            scored.append((r, s))
    n_trials = len(scored)
    rep = {"half": half, "n_trials": n_trials, "n_attempted": attempted,
           "eff_trials": int(eff_trials) if eff_trials else n_trials,
           "min_n": min_n, "wr": None, "sr": None, "rows": []}
    if not n_trials:
        return rep
    n_eff = rep["eff_trials"]

    # --- winrate track: SHRINKAGE, not a significance test --------------------
    #
    # This is deliberately not phrased as pass/fail. Across a grid that mixes
    # exit families, configs have genuinely different winrates by construction
    # (a 25% stop and a 60% stop are not the same bet), so "is the best WR
    # bigger than the spread of WRs?" answers nothing about edge. What IS well
    # posed from summary stats alone: the config you selected BECAUSE it topped
    # the board has an observed WR biased upward by roughly the expected
    # maximum of its own sampling noise. Subtract that and you get an unbiased
    # read on the winner's true winrate.
    #
    #   shrunk = best_wr - sqrt(p(1-p)/n) * E[max of N standard normals]
    #
    # Compare it to the exit ladder's breakeven WR (--breakeven-wr) to get a
    # verdict; without that number this reports the corrected estimate only.
    # The well-posed significance test is the Sharpe track below.
    wrs = [(s["wr"], s["n"]) for _r, s in scored if s.get("wr") is not None]
    if wrs:
        best, best_n = max(wrs)
        p = best / 100.0
        se = math.sqrt(max(p * (1 - p), 1e-12) / max(best_n, 1)) * 100.0
        bias = se * expected_max_z(n_eff)
        rep["wr"] = {"best_pct": best, "best_n": best_n, "se_pct": se,
                     "selection_bias_pp": bias, "shrunk_pct": best - bias,
                     "median_n": statistics.median([n for _w, n in wrs]),
                     "pooled_pct": statistics.fmean([w for w, _n in wrs])}
        if breakeven_wr is not None:
            rep["wr"]["breakeven_pct"] = breakeven_wr
            rep["wr"]["margin_pp"] = (best - bias) - breakeven_wr
            rep["wr"]["clears"] = (best - bias) > breakeven_wr

    # --- Sharpe track: needs std, i.e. a sweep re-run after this commit ------
    srs = []
    for r, s in scored:
        sr = sharpe(s.get("avg"), s.get("std"))
        srs.append(sr)
    have = [x for x in srs if x is not None]
    if len(have) >= 2:
        sr_sd = statistics.stdev(have)
        sr_star = expected_max(sr_sd, n_eff)
        pvals = [one_sided_p(x, s["n"]) for x, (_r, s) in zip(srs, scored)]
        qvals = benjamini_hochberg(pvals)
        best_i = max(range(len(srs)), key=lambda i: (srs[i] is not None, srs[i] or -9e9))
        for i, (r, s) in enumerate(scored):
            need = BREAKEVEN + sr_star * s["std"] if s.get("std") else None
            r["mtest"] = {
                "sr": srs[i],
                "dsr": psr(srs[i], sr_star, s["n"], s.get("skew", 0.0),
                           s.get("kurt", 3.0)),
                "p_raw": pvals[i],
                "p_bonferroni": min(1.0, pvals[i] * n_eff) if pvals[i] is not None else None,
                "q_bh": qvals[i],
                "avg_needed": need,
                "avg_net": (s["avg"] - need) if need is not None else None,
            }
            rep["rows"].append({"i": i, **r["mtest"]})
        bs = scored[best_i][1]
        rep["sr"] = {
            "cross_sd": sr_sd, "sr_star": sr_star,
            "best_sr": srs[best_i], "best_avg": bs["avg"], "best_n": bs["n"],
            "avg_haircut": sr_star * bs["std"],
            "avg_needed": BREAKEVEN + sr_star * bs["std"],
            "avg_net": bs["avg"] - (BREAKEVEN + sr_star * bs["std"]),
            "dsr": psr(srs[best_i], sr_star, bs["n"], bs.get("skew", 0.0),
                       bs.get("kurt", 3.0)),
            "clears": bs["avg"] > BREAKEVEN + sr_star * bs["std"],
        }
    return rep


# ----------------------------------------------------------------- output ---

def print_haircut(rows, half="train", attempted=None, eff_trials=None, min_n=1,
                  breakeven_wr=None):
    """One-block console summary. Sweeps call this right before writing JSON."""
    rep = analyse(rows, half=half, eff_trials=eff_trials, min_n=min_n,
                  attempted=attempted, breakeven_wr=breakeven_wr)
    print(f"\n== MULTIPLE-TESTING HAIRCUT ({half} half) ==")
    if not rep["n_trials"]:
        print("  no scored configs")
        return rep
    att = f" of {rep['n_attempted']} attempted" if rep["n_attempted"] else ""
    eff = "" if rep["eff_trials"] == rep["n_trials"] else f" (using N_eff={rep['eff_trials']})"
    print(f"  trials scored           : {rep['n_trials']}{att}{eff}")
    w = rep["wr"]
    if w:
        print(f"  best WR as reported     : {w['best_pct']:.1f}%  (n={w['best_n']}, "
              f"pooled across configs {w['pooled_pct']:.1f}%)")
        print(f"  selection-bias shrinkage: -{w['selection_bias_pp']:.1f}pp  "
              f"(sampling se {w['se_pct']:.2f}pp x best-of-{rep['eff_trials']})")
        line = f"  winner's true WR (est.) : {w['shrunk_pct']:.1f}%"
        if "clears" in w:
            line += (f"   vs breakeven {w['breakeven_pct']:.1f}%  "
                     f"({w['margin_pp']:+.1f}pp)  "
                     f"{'CLEARS' if w['clears'] else 'DOES NOT CLEAR'}")
        else:
            line += "   (pass --breakeven-wr for a verdict)"
        print(line)
    s = rep["sr"]
    if s:
        verdict = "CLEARS" if s["clears"] else "INDISTINGUISHABLE FROM NOISE"
        print(f"  cross-sectional SR sd   : {s['cross_sd']:.4f}")
        print(f"  E[max SR | no edge]     : {s['sr_star']:.4f}   "
              f"= {s['avg_haircut']:.3f}x of average multiple")
        print(f"  a no-edge config tops out at : {s['avg_needed']:.3f}x")
        print(f"  observed best avg       : {s['best_avg']:.3f}x  (SR {s['best_sr']:.4f}, "
              f"n={s['best_n']})")
        print(f"  net of selection bias   : {s['avg_net']:+.3f}x   "
              f"deflated Sharpe {s['dsr']:.3f}  {verdict}")
    else:
        print("  Sharpe track unavailable: rows carry no `std`. Re-run the sweep "
              "(sweep.stats() emits std/skew/kurt as of this commit).")
    print("  note: trials share tokens and are correlated, so the true N is "
          "lower and this haircut is conservative (--eff-trials to override).")
    return rep


def _load_rows(path):
    """Accept every result-file shape this repo writes."""
    with open(path, "r", encoding="utf-8") as f:
        blob = json.load(f)
    if isinstance(blob, list):
        return blob, None
    for key in ("all", "results", "rows", "strategies", "hitters"):
        if isinstance(blob.get(key), list):
            return blob[key], blob.get("n_combos")
    raise SystemExit(f"{path}: no recognisable result list "
                     f"(keys: {sorted(blob)[:8]})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("results", help="a reports/*_results.json from any sweep")
    ap.add_argument("--half", default="train",
                    help="train | valid | pooled (default train)")
    ap.add_argument("--eff-trials", type=int, default=None,
                    help="override N with an effective (correlation-adjusted) count")
    ap.add_argument("--min-n", type=int, default=1, help="drop configs below this trade count")
    ap.add_argument("--breakeven-wr", type=float, default=None,
                    help="winrate the exit ladder needs to break even (e.g. 55.4 "
                         "for SCALP-50); enables the winrate verdict")
    ap.add_argument("--top", type=int, default=10, help="rows to list")
    ap.add_argument("--json", default="", help="also write the report here")
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    rows, attempted = _load_rows(args.results)
    print(f"{args.results}: {len(rows)} rows"
          + (f" ({attempted} combos attempted)" if attempted else ""))
    rep = print_haircut(rows, half=args.half, attempted=attempted,
                        eff_trials=args.eff_trials, min_n=args.min_n,
                        breakeven_wr=args.breakeven_wr)

    annotated = [r for r in rows if r.get("mtest")]
    if annotated:
        annotated.sort(key=lambda r: -(r["mtest"]["sr"] or -9e9))
        print(f"\n== top {args.top} after correction ==")
        for r in annotated[:args.top]:
            m, s = r["mtest"], _half(r, args.half)
            flag = "keep" if (m["q_bh"] or 1) <= 0.05 and (m["avg_net"] or 0) > 0 else "drop"
            print(f"  {s['avg']:.3f}x wr{s['wr']:.0f}% n{s['n']} | SR {m['sr']:.3f} "
                  f"DSR {m['dsr']:.3f} | p_bonf {m['p_bonferroni']:.3g} "
                  f"q_bh {m['q_bh']:.3g} | net {m['avg_net']:+.3f}x  {flag}")

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1)
        print(f"\nwritten: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
