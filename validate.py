#!/usr/bin/env python
"""Independent end-to-end validation of the finalist strategies.

Deliberately does NOT read quest.py's cached result matrix. Every number here
is produced by calling `backtest.simulate()` directly, token by token, so a bug
in the search cache cannot survive into the reported result.

What it checks, in order of how likely each one is to kill a candidate:

  1. Full sample / train / holdout means and path-profitability.
  2. Five chronological blocks - a strategy that only works in one week is not
     a strategy. The single holdout window here is only ~4 days wide, so this
     is the stronger stability read.
  3. The real Monte Carlo battery via montecarlo.run_scenario: iid, block
     bootstrap, +5% cost stress, top-5% removed, fractional sizing.
  4. Engine-assumption sensitivity: dark slip, fill low_weight, the P5 print
     guard, and trading cost. A result that only exists at one setting of an
     unmeasured dial is not a result.
  5. Pairwise trade-set overlap, so "three strategies" means three.

Usage:  python validate.py
"""
from __future__ import annotations

import json
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import backtest as bt  # noqa: E402
import montecarlo as mc  # noqa: E402
import quest  # noqa: E402
import universe as uni  # noqa: E402
from finalists import p_profit, trimmed  # noqa: E402

TMP = os.path.join(os.environ.get("CLAUDE_JOB_DIR", HERE), "tmp")
COST = 4.0
COND = re.compile(r"^([a-z0-9_]+)(>=|<)(-?[\d.]+(?:[eE][-+]?\d+)?)$")


def parse_ladder(name) -> dict:
    """A ladder key like 'b2/s45/t35/h30/x50', or an exits dict passed through
    unchanged (strategies.py stores the dict directly)."""
    if isinstance(name, dict):
        return name
    for k, v in quest.ladders():
        if k == name:
            return v
    raise KeyError(name)


def predicate(conds: list):
    parsed = []
    for c in conds:
        m = COND.match(c)
        if not m:
            raise ValueError(f"unparseable condition: {c}")
        parsed.append((quest.FI[m.group(1)], m.group(2), float(m.group(3))))

    def ok(f):
        return all((f[i] >= v) if op == ">=" else (f[i] < v) for i, op, v in parsed)
    return ok


def run(strategy, U, fill=None, censor=None, cost=COST):
    """Every trade the strategy would have taken, straight from the engine."""
    age = strategy["age"]
    exits = parse_ladder(strategy["ladder"])
    ok = predicate(strategy["conds"])
    mults, created, mints = [], [], []
    for t in U:
        ts, oh = t["ts"], t["ohlcv"].astype(np.float64)
        entry_ts = int(ts[0]) + age * 60
        pm = ts <= entry_ts
        npre = int(pm.sum())
        if npre < 3 or npre == len(ts):
            continue
        o, h, l, c, v = (oh[pm, i] for i in range(5))
        entry = float(c[-1])
        if entry <= 0:
            continue
        if not ok(quest.features(ts[pm], o, h, l, c, v, entry, age)):
            continue
        candles = [(int(ts[i]), *(float(z) for z in oh[i])) for i in range(len(ts))]
        r = bt.simulate(candles, exits, age, cost, 0.0, setup=None,
                        fill=fill, censor=censor)
        if r is None:
            continue
        mults.append(r["multiple"])
        created.append(t["created"])
        mints.append(t["mint"])
    return np.array(mults), np.array(created), mints


def line(tag, x):
    if len(x) == 0:
        return f"  {tag:22} (no trades)"
    return (f"  {tag:22} n={len(x):5d}  mean {x.mean():6.3f}  med {np.median(x):6.3f}  "
            f"WR {100 * (x > 1).mean():5.1f}%  trim5 {trimmed(x):6.3f}  "
            f"P(profit) {100 * p_profit(x):5.1f}%")


SCEN = [("iid", dict(sampler="iid", haircut=1.0, stake_frac=0.0)),
        ("block10", dict(sampler="block", haircut=1.0, stake_frac=0.0)),
        ("stress +5% cost", dict(sampler="iid", haircut=0.95, stake_frac=0.0)),
        ("frac 5% sizing", dict(sampler="iid", haircut=1.0, stake_frac=0.05))]


def monte_carlo(x):
    print(f"  {'scenario':18} {'P(profit)':>10} {'median final':>13} {'p5':>8} "
          f"{'p95':>9} {'maxDD p50':>10} {'bust':>6}")
    verdicts = {}
    for label, kw in SCEN:
        r = mc.run_scenario(list(map(float, x)), paths=10000, horizon=100, start=5.0,
                            stake_sol=0.25, block=10, seed=42, **kw)
        pp = 100 * (1 - r["p_loss"])
        verdicts[label] = pp
        print(f"  {label:18} {pp:9.1f}% {r['final'][50]:12.2f} {r['final'][5]:8.2f} "
              f"{r['final'][95]:9.2f} {r['dd'][50]:9.1f}% {r['busted']:6d}")
    # no_top: strip the best 5% of trades - the tail-dependence test that has
    # now failed live twice
    s = np.sort(x)[::-1]
    cut = s[max(1, int(len(s) * 0.05)):]
    r = mc.run_scenario(list(map(float, cut)), paths=10000, horizon=100, start=5.0,
                        stake_sol=0.25, stake_frac=0.0, sampler="iid", block=10,
                        haircut=1.0, seed=42)
    pp = 100 * (1 - r["p_loss"])
    verdicts["no_top5"] = pp
    print(f"  {'no_top (top 5% cut)':18} {pp:9.1f}% {r['final'][50]:12.2f} "
          f"{r['final'][5]:8.2f} {r['final'][95]:9.2f} {r['dd'][50]:9.1f}% {r['busted']:6d}")
    return verdicts


def sensitivity(strategy, U):
    print("  engine-assumption sensitivity (mean / P(profit)):")
    base_fill, base_cen = dict(bt.FILL_MODEL), dict(bt.CENSOR_MODEL)
    cases = [("shipped defaults", {}, {}, COST),
             ("dark slip 30% (live best fit)", {}, {"dark_slip_pct": 30.0}, COST),
             ("dark slip 60% (harsher)", {}, {"dark_slip_pct": 60.0}, COST),
             ("rug 20% (2x observed)", {}, {"rug_pct": 20.0}, COST),
             ("fills at candle low (worst)", {"low_weight": 1.0}, {}, COST),
             ("P5 guard: vol floor $500", {"tp_min_vol_usd": 500.0}, {}, COST),
             ("P5 guard: fill at close", {"high_weight": 1.0}, {}, COST),
             ("P5 guard OFF (pre-fix)", {"guard_tp_prints": False}, {}, COST),
             ("cost 6% not 4%", {}, {}, 6.0)]
    for label, fo, co, cost in cases:
        f = {**base_fill, **fo}
        c = {**base_cen, **co}
        x, _, _ = run(strategy, U, fill=f, censor=c, cost=cost)
        flag = "" if (len(x) and x.mean() >= 2.0 and p_profit(x) >= 0.60) else "   <-- fails bar"
        print(f"    {label:32} mean {x.mean():6.3f}  P(profit) "
              f"{100 * p_profit(x):5.1f}%{flag}")


def age_sensitivity(strategy, U):
    """Every finalist enters at 3 minutes of token age. Live, the bot enters
    when it NOTICES, which is later and variable. If the edge only exists at
    exactly 3m it is not implementable, so this re-runs the identical screen
    and ladder at later entry ages."""
    print("  entry-age sensitivity (the same screen, entered later):")
    base = strategy["age"]
    for age in (base, base + 1, base + 2, base + 4, base + 7, base + 12):
        s = dict(strategy, age=age)
        x, _, _ = run(s, U)
        if len(x) < 20:
            print(f"    entry at {age:>2}m: only {len(x)} trades")
            continue
        print(f"    entry at {age:>2}m: n={len(x):4d}  mean {x.mean():6.3f}  "
              f"P(profit) {100 * p_profit(x):5.1f}%"
              + ("" if x.mean() >= 2.0 else "   <-- below 2.0x"))


def main():
    # The shipped strategies are the canonical definitions; the search's
    # tmp/finalists.json is only there so a candidate that was NOT shipped can
    # still be re-scored by name.
    import strategies as S
    cands = {k: {**v, "ladder": v["exits"]} for k, v in S.STRATEGIES.items()}
    try:
        with open(os.path.join(TMP, "finalists.json"), encoding="utf-8") as f:
            for k, v in json.load(f).items():
                cands.setdefault(k, v)
    except OSError:
        pass
    U = uni.load()
    global CUT
    CUT = float(np.median([t["created"] for t in U]))
    import datetime as _dt
    print(f"train/holdout boundary (median listing time of the universe): "
          f"{_dt.datetime.utcfromtimestamp(CUT).strftime('%Y-%m-%d %H:%M')} UTC")
    picked = sys.argv[1:] or list(cands)
    trades = {}
    for name in picked:
        s = cands[name]
        print("\n" + "=" * 96)
        print(f"{name}: {s['why']}")
        print(f"  entry age {s['age']}m   screen: {' & '.join(s['conds'])}")
        print(f"  exits: {s['ladder']}  ->  {json.dumps(parse_ladder(s['ladder']))}")
        x, cr, mints = run(s, U)
        trades[name] = (x, cr, set(mints))
        # ONE fixed calendar boundary for every strategy - the median listing
        # time of the whole universe. Splitting on each strategy's own median
        # instead gives each one a different train/holdout date, which is not
        # comparable across strategies and disagreed with the selection split
        # by a full 0.9x on RANGE.
        cut = CUT
        print(line("FULL SAMPLE", x))
        print(line("train (older half)", x[cr < cut]))
        print(line("holdout (newer half)", x[cr >= cut]))
        print("  chronological fifths:")
        edges = np.quantile(cr, [0, .2, .4, .6, .8, 1.0])
        for i in range(5):
            m = (cr >= edges[i]) & (cr <= edges[i + 1] if i == 4 else cr < edges[i + 1])
            print(line(f"  block {i + 1}", x[m]))
        print("  MONTE CARLO (10,000 paths x 100 trades, 0.25 SOL from 5.00):")
        monte_carlo(x)
        sensitivity(s, U)
        age_sensitivity(s, U)

    if len(picked) > 1:
        print("\n" + "=" * 96)
        print("DISTINCTNESS - Jaccard overlap of the actual traded token sets")
        ks = list(trades)
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                a, b = trades[ks[i]][2], trades[ks[j]][2]
                u = len(a | b)
                print(f"  {ks[i]:14} vs {ks[j]:14}  "
                      f"shared {len(a & b):4d} / union {u:4d}  = {len(a & b) / max(u, 1):.2f}")


if __name__ == "__main__":
    main()
