#!/usr/bin/env python
"""Loop 8: PRE-REGISTERED evaluation of the bounce strategy on the 60-day sample.

Written while bdgrow.py was still fetching - the configs below were fixed
BEFORE any candle of days 30-60 was seen. No other configs may be added to
this file after the fetch completes (add exploratory sweeps elsewhere, and
they get no verdict authority).

Configs (from loops 5/6 on the 30-day sample):
  P1-P4: incumbents (best validated as of loop 6)
  C1-C4: deep-clean corner (dd>=70 + nukes<=1), untestable at n>=100 on 30d

Verdict rule (locked): the 60% WR / 2.0x avg target is HIT iff some
pre-registered config, on the pooled 60-day sample under the locked sha1
both-halves rule with n>=100 per half, shows min(train,valid) avg >= 2.0 and
min WR >= 60 - AND its stats on the out-of-time subset alone (the new 30-60d
tokens, never touched by any optimization) show avg >= 1.5 at WR >= 55
(sanity floor against regime luck). Anything else is NOT a hit.

Engine: v5 (liquidity-honest + credibility rule). New tokens have no
liquidity snapshot; their data_end remainders are valued at 0 - strictly
conservative. Old-token remainders use reports/bd_liquidity.json as before.

Usage: python loop8_eval.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
from sweep import token_bucket

CONFIGS = [
    ("P1", dict(dd=0.75, cv=1000.0, max_nukes=None), dict(stop=60, arm=1.5, trail=30, hard=8.0, hold=480.0)),
    ("P2", dict(dd=0.75, cv=1000.0, max_nukes=None), dict(stop=60, arm=1.5, trail=30, hard=10.0, hold=480.0)),
    ("P3", dict(dd=0.55, cv=500.0, max_nukes=1), dict(stop=50, arm=1.5, trail=25, hard=10.0, hold=480.0)),
    ("P4", dict(dd=0.55, cv=1000.0, max_nukes=1), dict(stop=50, arm=1.5, trail=25, hard=10.0, hold=480.0)),
    ("C1", dict(dd=0.70, cv=1000.0, max_nukes=1), dict(stop=60, arm=1.5, trail=25, hard=10.0, hold=480.0)),
    ("C2", dict(dd=0.70, cv=1000.0, max_nukes=1), dict(stop=60, arm=1.5, trail=30, hard=10.0, hold=480.0)),
    ("C3", dict(dd=0.75, cv=1000.0, max_nukes=1), dict(stop=60, arm=1.5, trail=30, hard=10.0, hold=480.0)),
    ("C4", dict(dd=0.70, cv=500.0, max_nukes=1), dict(stop=50, arm=1.5, trail=25, hard=8.0, hold=480.0)),
]

NUKE_BODY = 0.70  # any-volume body-collapse definition (validated on 30d)


def bounce_entry(candles, dd, cv, max_nukes, floor=500.0, min_cum_vol=2000.0,
                 max_wait_min=720.0):
    created = candles[0][0]
    peak, cum_vol, nukes = 0.0, 0.0, 0
    for i, (ts, o, h, l, cl, v) in enumerate(candles):
        if (ts - created) / 60 > max_wait_min:
            return None
        cum_vol += v
        credible = v >= floor and cl >= 0.5 * h
        if credible:
            peak = max(peak, min(h, cl * 2))
        if peak > 0 and (cl / peak <= 1 - dd and credible and cl > o
                         and v >= cv and cum_vol >= min_cum_vol):
            if max_nukes is None or nukes <= max_nukes:
                return i, cl
            return None
        if o > 0 and cl / o <= NUKE_BODY:
            nukes += 1
    return None


def sim(candles, idx, entry, ex, intact_end, cost_pct=4.0, floor=500.0, fill=None):
    """Exit machine for the loop8/freshlab families.

    Censoring was already handled here: falling out of the loop with stock held
    credits `last_rc` only when `intact_end` says the pool still has liquidity,
    otherwise 0.0. freshlab passes intact_end=False, which is why it read BASE
    at 0.226x while backtest.py - which had no such guard - read 1.605x.

    `fill` (P2, see backtest.FILL_MODEL) is what was missing: stops and trails
    filled at exactly their trigger level regardless of how far the candle low
    sat below. Live trailing stops filled 15-27% under trigger. Pass
    fill={"use_candle_low": False, ...} to reproduce the pre-fix numbers.
    """
    fill = backtest.FILL_MODEL if fill is None else fill
    stop_slip = 1 - fill.get("stop_slip_pct", 0.0) / 100
    trail_slip = 1 - fill.get("trail_slip_pct", 0.0) / 100
    stop_mult = 1 - ex["stop"] / 100
    post = candles[idx + 1:]
    if not post:
        return None
    entry_ts = candles[idx][0]
    remaining, received = 1.0, 0.0
    armed = False
    peak = 1.0
    last_rc, last_rt = None, None
    for ts, _o, h, l, cl, v in post:
        age = (ts - entry_ts) / 60
        real = v >= floor
        credible = real and cl >= 0.5 * h
        lo_m, hi_m, cl_m = l / entry, h / entry, cl / entry
        if not armed and lo_m <= stop_mult:
            got = stop_mult if real else min(stop_mult, cl_m)
            received += remaining * backtest.fill_at(got, lo_m, fill) * stop_slip
            remaining = 0.0
            break
        if armed:
            level = peak * (1 - ex["trail"] / 100)
            if lo_m <= level:
                got = level if real else min(level, cl_m)
                received += remaining * backtest.fill_at(got, lo_m, fill) * trail_slip
                remaining = 0.0
                break
        if credible:
            if not armed and hi_m >= ex["arm"]:
                armed = True
            if hi_m >= ex["hard"]:
                received += remaining * ex["hard"]
                remaining = 0.0
                break
            peak = max(peak, min(hi_m, cl_m * 2))
            last_rc, last_rt = cl_m, ts
        elif real:
            last_rc, last_rt = cl_m, ts
        if age >= ex["hold"]:
            # not `fill` - that name is the fill-model parameter
            got = cl_m if real else (last_rc if last_rt and ts - last_rt <= 1800 else 0.0)
            received += remaining * got
            remaining = 0.0
            break
    if remaining > 1e-12:
        received += remaining * (last_rc if (intact_end and last_rc is not None) else 0.0)
    return received * (1 - cost_pct / 100)


def wr_avg(arr):
    return (statistics.mean(arr), 100 * sum(1 for m in arr if m > 1) / len(arr), len(arr))


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join("reports", "bd_liquidity.json"), "r", encoding="utf-8") as f:
        liq = json.load(f)
    intact = {m for m, v in liq.items() if (v.get("liquidity") or 0) >= 1000}
    with open(os.path.join("reports", "bd_tokens.json"), "r", encoding="utf-8") as f:
        old_mints = {t["mint"] for t in json.load(f)}

    tokens = backtest.load_token_index(os.path.join("reports", "bd_tokens_60d.json"))
    data = []
    for tok in tokens:
        c = backtest.load_cached_candles(".bd_cache_ext", tok.pool)
        if c:
            data.append((c, token_bucket(tok.mint), tok.mint in intact,
                         tok.mint in old_mints))
    n_new = sum(1 for x in data if not x[3])
    print(f"tokens with candles: {len(data)} ({n_new} out-of-time)")

    verdict_hit = []
    for name, ent, ex in CONFIGS:
        pooled = {"train": [], "valid": []}
        oot = []
        for c, b, ok, is_old in data:
            e = bounce_entry(c, ent["dd"], ent["cv"], ent["max_nukes"])
            if not e:
                continue
            m = sim(c, e[0], e[1], ex, ok if is_old else False)
            if m is None:
                continue
            pooled[b].append(m)
            if not is_old:
                oot.append(m)
        t, v = pooled["train"], pooled["valid"]
        if len(t) < 100 or len(v) < 100:
            print(f"{name}: pooled n too small ({len(t)}/{len(v)})")
            continue
        ta, twr, nt = wr_avg(t)
        va, vwr, nv = wr_avg(v)
        oa, owr, no = wr_avg(oot) if oot else (0, 0, 0)
        minavg, minwr = min(ta, va), min(twr, vwr)
        hit = minavg >= 2.0 and minwr >= 60 and oa >= 1.5 and owr >= 55
        if hit:
            verdict_hit.append(name)
        print(f"{name}: pooled tr {ta:.3f}/{twr:.0f}%/n{nt} va {va:.3f}/{vwr:.0f}%/n{nv} "
              f"| min {minavg:.3f}/{minwr:.0f}% | OOT {oa:.3f}/{owr:.0f}%/n{no} "
              f"| {'*** HIT ***' if hit else 'miss'}")

    print(f"\nPRE-REGISTERED VERDICT: {'TARGET HIT by ' + ', '.join(verdict_hit) if verdict_hit else 'target NOT hit'}")


if __name__ == "__main__":
    main()
