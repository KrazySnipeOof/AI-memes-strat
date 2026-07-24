#!/usr/bin/env python
"""Loop 9: pre-registered configs evaluated on the VIRGIN WEEK (true out-of-time).

Imports the exact P1-P4 / C1-C4 configs from loop8_eval.py (fixed before any
post-30d data was fetched; no additions permitted here). The virgin week
(reports/bd_tokens_oot.json, listings from ~day-40..day-33, i.e. BEFORE the
original sample's window) never touched any loop of this campaign.

Reported per config:
  * virgin-week only: sha1 train/valid halves + combined
  * pooled (original + 60d growth + virgin week): sha1 both-halves

No liquidity snapshot exists for virgin-week tokens: their data_end
remainders are valued at 0 (strictly conservative).

Usage: python loop9_eval.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
from sweep import token_bucket
from loop8_eval import CONFIGS, bounce_entry, sim, wr_avg


def load_set(index_path, cache=".bd_cache_ext"):
    tokens = backtest.load_token_index(index_path)
    out = []
    for tok in tokens:
        c = backtest.load_cached_candles(cache, tok.pool)
        if c:
            out.append((c, token_bucket(tok.mint), tok.mint))
    return out


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join("reports", "bd_liquidity.json"), "r", encoding="utf-8") as f:
        liq = json.load(f)
    intact = {m for m, v in liq.items() if (v.get("liquidity") or 0) >= 1000}

    oot = load_set(os.path.join("reports", "bd_tokens_oot.json"))
    pooled_sets = [
        (os.path.join("reports", "bd_tokens_60d.json"), True),   # old + growth (has liq info for old mints)
        (os.path.join("reports", "bd_tokens_oot.json"), False),
    ]
    print(f"virgin-week tokens with candles: {len(oot)}")

    with open(os.path.join("reports", "bd_tokens.json"), "r", encoding="utf-8") as f:
        original_mints = {t["mint"] for t in json.load(f)}

    for name, ent, ex in CONFIGS:
        virgin, combined_oot = [], []
        pool = {"train": [], "valid": []}
        seen = set()
        for path, has_liq in pooled_sets:
            for c, b, mint in load_set(path):
                if mint in seen:
                    continue
                seen.add(mint)
                e = bounce_entry(c, ent["dd"], ent["cv"], ent["max_nukes"])
                if not e:
                    continue
                m = sim(c, e[0], e[1], ex, (mint in intact) if has_liq else False)
                if m is None:
                    continue
                pool[b].append(m)
                if mint not in original_mints:
                    combined_oot.append(m)
                    if not has_liq:
                        virgin.append(m)
        line = f"{name}:"
        if len(pool["train"]) >= 100 and len(pool["valid"]) >= 100:
            ta, twr, nt = wr_avg(pool["train"])
            va, vwr, nv = wr_avg(pool["valid"])
            line += (f" POOLED(purged) tr {ta:.3f}/{twr:.0f}%/n{nt} "
                     f"va {va:.3f}/{vwr:.0f}%/n{nv} min {min(ta, va):.3f}/{min(twr, vwr):.0f}%")
        else:
            line += f" pooled n small ({len(pool['train'])}/{len(pool['valid'])})"
        if combined_oot:
            oa, owr, no = wr_avg(combined_oot)
            line += f" || OOT-combined {oa:.3f}/{owr:.0f}%/n{no}"
        if virgin:
            va2, vwr2, nv2 = wr_avg(virgin)
            line += f" (virgin only {va2:.3f}/{vwr2:.0f}%/n{nv2})"
        print(line)


if __name__ == "__main__":
    main()
