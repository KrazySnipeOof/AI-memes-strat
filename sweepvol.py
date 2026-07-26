#!/usr/bin/env python
"""Vol/MC churn strategy — Kenny's "volume higher than market cap" signal
(2026-07-24). Enter when trailing (or cumulative) traded volume exceeds
ratio x market cap; MC(t) = close x 1B supply (the pump.fun-family
convention the repo already uses for MC displays in tradecards.py).

Protocol (locked, mirrors loop8/9 discipline):
  * TRAIN-ONLY sweep (`--train`): every tuned number comes from the
    sha1(mint)[0]%2==0 half of the blocklist-purged 60d universe. Prints a
    leaderboard and writes reports/volmc_train.json.
  * FINAL eval (`--final`): the FINALS list below is hard-coded from the
    train leaderboard BEFORE the valid half or virgin week is ever scored;
    those sets are then touched exactly once. No config may be added to
    FINALS after a --final run (exploratory sweeps get no verdict authority).
  * Engine v5 verbatim: loop8_eval.sim — liquidity-honest fills, wash-guarded
    peaks, dead-token remainders at 0 (no liq snapshot => 0), 4% haircut.
  * Verdict rule (same shape as loop8): target HIT iff a pre-registered
    finalist shows min(train,valid) avg >= 2.0 at n>=100 per half AND
    virgin-week avg >= 1.5 (regime-luck floor). Anything else is NOT a hit.

Universe caveat (disclosed, outcome-blind): the 1B-supply MC is exact for
launchpad-curve sources (pump_amm, meteora_dynamic_bonding_curve,
raydium_launchlab, moonshot, heaven) and approximate elsewhere, so the
sweep carries a universe axis: 'lp1b' (those sources only) vs 'all'.

Usage: python sweepvol.py --train   # tuning pass (train half only)
       python sweepvol.py --final   # one-shot valid + virgin verdict
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
from loop8_eval import sim, wr_avg          # engine v5 + stats, verbatim
from sweep import token_bucket

LAUNCHPAD_1B = {"pump_amm", "meteora_dynamic_bonding_curve",
                "raydium_launchlab", "moonshot", "heaven"}
SUPPLY = 1e9
NUKE_BODY = 0.70

# ---- swept grids (train half ONLY) ----------------------------------------
UNIVERSES = ("lp1b", "all")
RATIOS = (0.5, 1.0, 1.5, 2.0)          # window vol >= ratio * MC
WINDOWS = (None, 30)                    # None = cumulative-since-listing, else trailing minutes
NUKES = (1, None)
EXITS = [dict(stop=stop, arm=arm, trail=trail, hard=hard, hold=480.0)
         for stop in (50, 60) for arm in (1.5, 2.0)
         for trail in (25, 30) for hard in (8.0, 10.0, 20.0)]

# ---- pre-registered finalists for --final ---------------------------------
# Populated ONLY from the --train leaderboard; frozen before any valid/virgin
# number is computed. Each: (name, entry-config dict, exit-config dict).
FINALS: list = []


def churn_entry(candles, ratio, window_min, max_nukes, floor=500.0,
                min_cum_vol=2000.0, max_wait_min=720.0):
    """Index+price of the first credible candle where trailing/cumulative
    volume >= ratio * MC(close). Credibility and nuke bookkeeping follow
    loop8_eval.bounce_entry so the only new idea under test is the ratio."""
    created = candles[0][0]
    cum_vol, nukes = 0.0, 0
    win, wsum = deque(), 0.0
    for i, (ts, o, h, l, cl, v) in enumerate(candles):
        if (ts - created) / 60 > max_wait_min:
            return None
        cum_vol += v
        if window_min is None:
            wsum = cum_vol
        else:
            win.append((ts, v))
            wsum += v
            while win and win[0][0] < ts - window_min * 60:
                wsum -= win.popleft()[1]
        credible = v >= floor and cl >= 0.5 * h
        if (credible and cum_vol >= min_cum_vol and cl > 0
                and wsum >= ratio * cl * SUPPLY):
            if max_nukes is None or nukes <= max_nukes:
                return i, cl
            return None
        if o > 0 and cl / o <= NUKE_BODY:
            nukes += 1
    return None


def load_universe():
    """(candles, bucket, mint, source, intact, has_liq) per token, deduped.
    60d set carries liquidity info for original-index mints (loop8 rule);
    virgin week has no snapshot => remainders are 0 (conservative)."""
    with open(os.path.join("reports", "bd_liquidity.json"), encoding="utf-8") as f:
        liq = json.load(f)
    intact = {m for m, v in liq.items() if (v.get("liquidity") or 0) >= 1000}
    with open(os.path.join("reports", "bd_tokens.json"), encoding="utf-8") as f:
        original = {t["mint"] for t in json.load(f)}
    src = {}
    for path in ("bd_tokens_60d.json", "bd_tokens_oot.json"):
        with open(os.path.join("reports", path), encoding="utf-8") as f:
            for t in json.load(f):
                src.setdefault(t["mint"], t.get("source") or "?")
    out, seen = [], set()
    for path, virgin in (("bd_tokens_60d.json", False), ("bd_tokens_oot.json", True)):
        for tok in backtest.load_token_index(os.path.join("reports", path)):
            if tok.mint in seen:
                continue
            seen.add(tok.mint)
            c = backtest.load_cached_candles(".bd_cache_ext", tok.pool)
            if c:
                out.append((c, token_bucket(tok.mint), tok.mint, src.get(tok.mint, "?"),
                            (tok.mint in intact) and (tok.mint in original) and not virgin,
                            virgin))
    return out


def run_config(data, ent, ex, buckets, virgin_only=False):
    """Multiples per bucket for one entry+exit config. Train/valid halves are
    the 60d sample only; virgin_only=True selects the virgin week instead, so
    the two never mix."""
    res = {b: [] for b in buckets}
    for c, b, _mint, source, intact_ok, virgin in data:
        if virgin != virgin_only:
            continue
        if b not in res:
            continue
        if ent["universe"] == "lp1b" and source not in LAUNCHPAD_1B:
            continue
        e = churn_entry(c, ent["ratio"], ent["window"], ent["max_nukes"])
        if not e:
            continue
        m = sim(c, e[0], e[1], ex, intact_ok)
        if m is not None:
            res[b].append(m)
    return res


def fmt(arr):
    if not arr:
        return "n=0"
    a, w, n = wr_avg(arr)
    med = statistics.median(arr)
    return f"{a:.3f}x/{w:.0f}%/med{med:.2f}/n{n}"


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true", help="tuning sweep on the train half only")
    ap.add_argument("--final", action="store_true", help="one-shot valid+virgin eval of FINALS")
    args = ap.parse_args()

    data = load_universe()
    n_virgin = sum(1 for x in data if x[5])
    print(f"universe: {len(data)} tokens with candles ({n_virgin} virgin-week)")

    if args.train:
        # entry-config cache: churn_entry is exit-independent, compute once
        rows = []
        for uni in UNIVERSES:
            for ratio in RATIOS:
                for window in WINDOWS:
                    for nk in NUKES:
                        ent = dict(universe=uni, ratio=ratio, window=window, max_nukes=nk)
                        entries = []
                        for c, b, _m, source, intact_ok, virgin in data:
                            if virgin or b != "train":
                                continue
                            if uni == "lp1b" and source not in LAUNCHPAD_1B:
                                continue
                            e = churn_entry(c, ratio, window, nk)
                            if e:
                                entries.append((c, e, intact_ok))
                        for ex in EXITS:
                            mults = [m for c, e, ok in entries
                                     if (m := sim(c, e[0], e[1], ex, ok)) is not None]
                            if len(mults) < 30:
                                continue
                            a, w, n = wr_avg(mults)
                            rows.append({"ent": ent, "ex": ex, "avg": round(a, 4),
                                         "wr": round(w, 1), "n": n,
                                         "med": round(statistics.median(mults), 3)})
                        done = f"{uni} r{ratio} w{window} nk{nk}: {len(entries)} entries"
                        print(done, flush=True)
        rows.sort(key=lambda r: -r["avg"])
        os.makedirs("reports", exist_ok=True)
        with open(os.path.join("reports", "volmc_train.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=1)
        print(f"\nTRAIN LEADERBOARD (top 25 of {len(rows)} configs with n>=30) — train half only:")
        for r in rows[:25]:
            e, x = r["ent"], r["ex"]
            print(f"  {r['avg']:.3f}x wr{r['wr']:.0f}% med{r['med']:.2f} n{r['n']} | "
                  f"{e['universe']} r{e['ratio']} w{e['window']} nk{e['max_nukes']} | "
                  f"stop{x['stop']} arm{x['arm']} tr{x['trail']} hard{x['hard']}")
        return

    if args.final:
        print("\nPRE-REGISTERED FINAL EVAL (valid half + virgin week, touched once):")
        hits = []
        for name, ent, ex in FINALS:
            per = run_config(data, ent, ex, ("train", "valid"))
            t, v = per["train"], per["valid"]
            virgin = sum(run_config(data, ent, ex, ("train", "valid"),
                                    virgin_only=True).values(), [])
            line = f"{name}: train {fmt(t)} valid {fmt(v)}"
            if t and v:
                ta, twr, _ = wr_avg(t)
                va, vwr, _ = wr_avg(v)
                minavg, minwr = min(ta, va), min(twr, vwr)
                line += f" | min {minavg:.3f}x/{minwr:.0f}%"
                vg = wr_avg(virgin) if virgin else (0.0, 0.0, 0)
                line += f" | virgin {fmt(virgin)}"
                if (minavg >= 2.0 and minwr >= 60 and len(t) >= 100 and len(v) >= 100
                        and vg[0] >= 1.5):
                    hits.append(name)
                    line += "  *** HIT ***"
            print(line)
        print(f"\nVERDICT: {'TARGET HIT by ' + ', '.join(hits) if hits else 'target NOT hit'}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
