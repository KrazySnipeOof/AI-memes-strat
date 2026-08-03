#!/usr/bin/env python
"""Audit the train/valid split for leakage.

Every verdict in this repo -- the locked both-halves rule, loop8's
pre-registered target, sweepvol's finalists -- rests on sweep.token_bucket():

    train if sha1(mint)[0] % 2 == 0 else valid

That is a random split on the MINT. It buys independence of mint, and nothing
else. Two things it does not buy, both of which matter here:

  TICKER LINEAGE. A relaunched ticker is a new mint, so USOH #1 and USOH #77
  hash independently and land on opposite halves roughly half the time. If a
  ticker's behaviour is a property of the ticker (same community, same
  playbook, often the same operator) rather than of the mint, the validation
  half has already seen the answer.

  TIME. A hash split is not a time split. Both halves cover the same days, the
  same pump waves, the same market regime. A config tuned on train is
  validated against the same weather it was fitted to -- which is exactly the
  regime luck the 60-day out-of-time subset was later added to catch.

This script measures both, then asks the only question that matters: does
fixing the split change the number? It re-runs one reference strategy (the
live BASE entry, engine v5) under four splits and prints the train/valid gap
for each:

    hash         sha1(mint) % 2                     -- the incumbent
    symbol       sha1(SYMBOL) % 2                   -- lineage kept together
    time         first half by created_ts = train   -- honest chronology
    time_purged  time, minus an embargo band around the boundary whose width
                 is the strategy's max hold (Lopez de Prado purging: a trade
                 opened just before the cut is still running after it)

If the gap widens as the split gets stricter, the incumbent split is
flattering every result it has ever graded.

Usage:
  python splitaudit.py                                   # 60d sample, structure + sim
  python splitaudit.py --tokens reports/bd_tokens_fresh.json --cache .bd_cache
  python splitaudit.py --no-sim                          # structure only, seconds
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
from loop8_eval import bounce_entry, sim
from sweep import token_bucket

# the live BASE entry family (freshlab.py's age_entry / EA), used as the probe
ENTRY_AGE_MIN = 30.0
MIN_CUM_VOL = 8000.0
EA = dict(stop=60, arm=1.5, trail=25, hard=10.0, hold=480.0)


def sym_bucket(symbol):
    s = (symbol or "?").strip().upper()
    return "train" if hashlib.sha1(s.encode()).digest()[0] % 2 == 0 else "valid"


def age_entry(candles):
    """(idx, entry_price, pre) for the age-30m/vol-8k family, or None."""
    entry_ts = candles[0][0] + ENTRY_AGE_MIN * 60
    pre = [c for c in candles if c[0] <= entry_ts]
    post = [c for c in candles if c[0] > entry_ts]
    if not pre or not post or pre[-1][4] <= 0:
        return None
    if sum(c[5] for c in pre) < MIN_CUM_VOL:
        return None
    return len(pre) - 1, pre[-1][4], pre


def wr_avg(arr):
    if not arr:
        return None
    return {"n": len(arr), "avg": statistics.fmean(arr),
            "wr": 100.0 * sum(1 for m in arr if m > 1.0) / len(arr),
            "sd": statistics.stdev(arr) if len(arr) > 1 else 0.0}


def gap_se(t_arr, v_arr):
    """Standard error of (valid mean - train mean). Without this the gap
    column is unreadable: at n~90 per half these distributions are wide
    enough that a +-0.2x swing is a coin flip, not a finding."""
    if len(t_arr) < 2 or len(v_arr) < 2:
        return None
    return math.sqrt(statistics.variance(t_arr) / len(t_arr)
                     + statistics.variance(v_arr) / len(v_arr))


def fmt(s):
    return f"n{s['n']:<5} {s['avg']:.3f}x  wr{s['wr']:.1f}%" if s else "n0"


def ascii_safe(s):
    """Memecoin tickers carry emoji and exotic scripts; the Windows console is
    cp1252. Print a transliterated form rather than crashing the audit."""
    return (s or "?").encode("ascii", "replace").decode("ascii")


# ------------------------------------------------------------------ structure

def audit_structure(tokens):
    n = len(tokens)
    buckets = [token_bucket(t.mint) for t in tokens]
    n_train = sum(1 for b in buckets if b == "train")
    print(f"\n== SAMPLE ==")
    print(f"  tokens                 : {n}   (train {n_train} / valid {n - n_train})")

    dup_mint = [m for m, c in collections.Counter(t.mint for t in tokens).items() if c > 1]
    dup_pool = [p for p, c in collections.Counter(t.pool for t in tokens).items() if c > 1]
    if dup_mint or dup_pool:
        print(f"  DUPLICATES             : {len(dup_mint)} mints, {len(dup_pool)} pools "
              f"appear more than once in the index")

    # ---- ticker lineage -----------------------------------------------------
    by_sym = collections.defaultdict(list)
    for t, b in zip(tokens, buckets):
        by_sym[(t.symbol or "?").strip().upper()].append(b)
    relaunched = {s: bs for s, bs in by_sym.items() if len(bs) > 1}
    straddling = {s: bs for s, bs in relaunched.items() if len(set(bs)) > 1}
    n_tok_straddle = sum(len(bs) for bs in straddling.values())

    print(f"\n== TICKER LINEAGE (the split hashes the mint, not the ticker) ==")
    print(f"  distinct tickers       : {len(by_sym)}")
    print(f"  tickers relaunched >=2x: {len(relaunched)}")
    print(f"  ...of those, STRADDLING the split: {len(straddling)}")
    print(f"  tokens in a straddling ticker    : {n_tok_straddle} "
          f"({100.0 * n_tok_straddle / max(n, 1):.1f}% of the sample)")
    if straddling:
        top = sorted(straddling.items(), key=lambda kv: -len(kv[1]))[:10]
        print("  worst offenders (ticker: launches, train/valid):")
        for s, bs in top:
            print(f"    {ascii_safe(s):<14} {len(bs):>4} launches   "
                  f"{bs.count('train')}/{bs.count('valid')}")

    # ---- time ---------------------------------------------------------------
    tr_ts = [t.created_ts for t, b in zip(tokens, buckets) if b == "train"]
    va_ts = [t.created_ts for t, b in zip(tokens, buckets) if b == "valid"]
    print(f"\n== TIME SEPARATION ==")
    if tr_ts and va_ts:
        span = lambda a: (min(a), max(a))
        t0, t1 = span(tr_ts)
        v0, v1 = span(va_ts)
        overlap = max(0, min(t1, v1) - max(t0, v0))
        union = max(t1, v1) - min(t0, v0)
        iso = lambda s: time.strftime("%Y-%m-%d %H:%M", time.gmtime(s))
        print(f"  train window           : {iso(t0)} -> {iso(t1)}")
        print(f"  valid window           : {iso(v0)} -> {iso(v1)}")
        print(f"  overlap                : {100.0 * overlap / max(union, 1):.1f}% "
              f"of the total span")
        print(f"  median gap train->valid: {abs(statistics.median(tr_ts) - statistics.median(va_ts)) / 3600:.1f} h")
        print("  => the halves are contemporaneous by construction: same days, "
              "same pump waves,\n     same regime. A hash split cannot test "
              "whether an edge survives a new week.")

    # ---- co-listing ---------------------------------------------------------
    order = sorted(zip(tokens, buckets), key=lambda tb: tb[0].created_ts)
    near = 0
    for i in range(len(order) - 1):
        (a, ba), (b, bb) = order[i], order[i + 1]
        if abs(b.created_ts - a.created_ts) <= 300 and ba != bb:
            near += 1
    print(f"\n== CO-LISTING STRADDLE ==")
    print(f"  adjacent-in-time pairs within 5 min landing on opposite halves: {near}")
    print(f"  ({100.0 * near / max(len(order) - 1, 1):.1f}% of consecutive pairs - "
          f"tokens launched into the same minute of the same wave)")

    return {"n": n, "tickers": len(by_sym), "relaunched": len(relaunched),
            "straddling_tickers": len(straddling),
            "tokens_in_straddling": n_tok_straddle,
            "colisting_straddle_pairs": near}


# ------------------------------------------------------------------ strategy

PROBES = {
    # name: (entry fn -> (idx, entry_price) or None, exit ladder, description)
    "base": (None, EA,
             f"BASE entry (age{ENTRY_AGE_MIN:.0f}m, cum vol ${MIN_CUM_VOL:,.0f}, "
             f"DEFAULT_SETUP)"),
    "bounce": (lambda c: bounce_entry(c, 0.55, 500.0, 1),
               dict(stop=50, arm=1.5, trail=25, hard=10.0, hold=480.0),
               "BOUNCE P3 (dd55 cv500 nk1) - the loop8 incumbent"),
}


def build_population(tokens, cache_dir, intact, probe="base"):
    entry_fn, ex, _desc = PROBES[probe]
    pop = []
    for tok in tokens:
        c = backtest.load_cached_candles(cache_dir, tok.pool)
        if not c:
            continue
        if entry_fn is None:                      # BASE: age gate + setup gate
            ae = age_entry(c)
            if not ae:
                continue
            idx, entry, pre = ae
            if not backtest.entry_setup_ok(pre, int(c[idx][0]), entry,
                                           backtest.DEFAULT_SETUP):
                continue
        else:
            e = entry_fn(c)
            if not e:
                continue
            idx, entry = e
        m = sim(c, idx, entry, ex, tok.mint in intact)
        if m is not None:
            pop.append((tok, m, int(c[idx][0])))
    return pop


def split_assign(pop, scheme, hold_min):
    """-> {'train': [...], 'valid': [...]}, dropping purged trades."""
    out = {"train": [], "valid": []}
    if scheme == "hash":
        for tok, m, _ts in pop:
            out[token_bucket(tok.mint)].append(m)
    elif scheme == "symbol":
        for tok, m, _ts in pop:
            out[sym_bucket(tok.symbol)].append(m)
    elif scheme in ("time", "time_purged"):
        ordered = sorted(pop, key=lambda p: p[2])
        cut_i = len(ordered) // 2
        if not ordered:
            return out
        cut_ts = ordered[cut_i][2]
        embargo = hold_min * 60 if scheme == "time_purged" else 0
        for tok, m, ts in ordered:
            if ts < cut_ts - embargo:
                out["train"].append(m)
            elif ts >= cut_ts + embargo:
                out["valid"].append(m)
            # inside the embargo band: purged, its outcome spans the boundary
    return out


def audit_strategy(tokens, cache_dir, intact, probe="base"):
    _fn, ex, desc = PROBES[probe]
    print(f"\n== STRATEGY-LEVEL IMPACT ==")
    print(f"  probe: {desc}")
    print(f"         exit stop{ex['stop']}/arm{ex['arm']}/tr{ex['trail']}"
          f"/hard{ex['hard']:.0f}/hold{ex['hold']:.0f} | engine v5")
    pop = build_population(tokens, cache_dir, intact, probe)
    print(f"  trades: {len(pop)}")
    if len(pop) < 20:
        print("  too few trades to compare splits")
        return {}
    rows = {}
    print(f"\n  {'split':<13} {'train':<26} {'valid':<26} {'gap (valid-train)'}")
    for scheme in ("hash", "symbol", "time", "time_purged"):
        halves = split_assign(pop, scheme, ex["hold"])
        t, v = wr_avg(halves["train"]), wr_avg(halves["valid"])
        gap = (v["avg"] - t["avg"]) if (t and v) else None
        se = gap_se(halves["train"], halves["valid"])
        rows[scheme] = {"train": t, "valid": v, "gap": gap, "gap_se": se,
                        "purged": len(pop) - len(halves["train"]) - len(halves["valid"])}
        gtxt = f"{gap:+.3f}x +-{se:.3f}" if gap is not None and se else (
            f"{gap:+.3f}x" if gap is not None else "-")
        note = ""
        if scheme == "time_purged" and rows[scheme]["purged"]:
            note = f"   ({rows[scheme]['purged']} purged at boundary)"
        print(f"  {scheme:<13} {fmt(t):<26} {fmt(v):<26} {gtxt}{note}")
    print("  (+- is the standard error of the gap; anything inside ~2 se is a coin flip)")
    return rows


def main():
    ap = argparse.ArgumentParser(description="Train/valid split leakage audit")
    ap.add_argument("--tokens", default=os.path.join("reports", "bd_tokens_60d.json"))
    ap.add_argument("--cache", default=".bd_cache_ext")
    ap.add_argument("--probe", default="base", choices=sorted(PROBES),
                    help="strategy used to measure whether the split changes the "
                         "answer (default base)")
    ap.add_argument("--no-sim", action="store_true",
                    help="structure only; skip the four-split strategy re-run")
    ap.add_argument("--out", default=os.path.join("reports", "split_audit.json"))
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    tokens = backtest.load_token_index(args.tokens)
    print(f"index: {args.tokens}   cache: {args.cache}")

    report = {"index": args.tokens, "cache": args.cache}
    report["structure"] = audit_structure(tokens)

    if not args.no_sim:
        try:
            with open(os.path.join("reports", "bd_liquidity.json"), encoding="utf-8") as f:
                liq = json.load(f)
            intact = {m for m, v in liq.items() if (v.get("liquidity") or 0) >= 1000}
        except (OSError, ValueError):
            intact = set()
            print("\n(no liquidity snapshot: every data-end remainder valued at 0)")
        report["probe"] = args.probe
        report["splits"] = audit_strategy(tokens, args.cache, intact, args.probe)

    print(f"\n== VERDICT ==")
    s = report["structure"]
    if s["straddling_tickers"]:
        print(f"  LEAKAGE PRESENT: {s['tokens_in_straddling']} tokens "
              f"({100.0 * s['tokens_in_straddling'] / max(s['n'], 1):.1f}%) belong to a "
              f"ticker that appears on BOTH sides of the split.")
    else:
        print("  no ticker straddles the split")
    sp = report.get("splits") or {}
    h, tp = sp.get("hash"), sp.get("time_purged")
    if h and tp and h["gap"] is not None and tp["gap"] is not None:
        d = tp["gap"] - h["gap"]
        # the two gaps share the same trades, so this is a rough combined se
        se = math.sqrt((h["gap_se"] or 0) ** 2 + (tp["gap_se"] or 0) ** 2)
        print(f"  validation gap under the incumbent hash split : {h['gap']:+.3f}x")
        print(f"  ...under a purged chronological split         : {tp['gap']:+.3f}x")
        print(f"  difference                                    : {d:+.3f}x "
              f"(combined se ~{se:.3f})")
        if se and abs(d) < 2 * se:
            print("  => WITHIN NOISE at this trade count. The structural leakage above "
                  "is real and\n     large, but this probe cannot show it moving the "
                  "number. Re-run with a probe\n     that has measurable edge, or a "
                  "bigger sample, before concluding either way.")
        elif d < 0:
            print(f"  => the hash split is FLATTERING validation by {-d:.3f}x")
        else:
            print(f"  => the hash split is not flattering validation ({d:+.3f}x)")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, default=str)
    print(f"\nwritten: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
