#!/usr/bin/env python
"""Build reports/funder_graph.json: the on-chain lineage of serial-scam families
(same ticker relaunched many times) so the dashboard can SHOW why deployer- and
funder-reputation don't work — every launch rotates a fresh deployer AND a fresh
funder, so the lineage fans OUT instead of converging on a reusable identity.

For each blocklisted serial-redeploy family: sample members, resolve
token -> deployer (rugcheck creator) -> funder (bot.funder), and record the
edges + distinct-wallet counts.

  python funder_graph.py [--families 5] [--per-family 8]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from bot import funder
from bot.util import load_dotenv, make_session

RC = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
OUT = os.path.join("reports", "funder_graph.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--families", type=int, default=5)
    ap.add_argument("--per-family", type=int, default=8)
    args = ap.parse_args()
    load_dotenv()
    s = make_session()
    rpc = os.environ.get("MEMEBOT_RPC_URL", "https://api.mainnet-beta.solana.com")

    bl = json.load(open(os.path.join("reports", "bd_blocklist.json"), encoding="utf-8"))
    fam = defaultdict(list)
    for mint, v in bl.items():
        if v.get("reason") == "serial_redeploy":
            fam[(v.get("symbol") or "?").upper()].append(mint)
    fams = sorted(fam.items(), key=lambda kv: -len(kv[1]))[:args.families]

    out_fams = []
    for sym, mints in fams:
        members = []
        funders_seen = {}
        for m in mints[:args.per_family]:
            try:
                d = s.get(RC.format(mint=m), timeout=12).json()
            except Exception:
                continue
            cr = d.get("creator")
            if not cr:
                continue
            f, sol_in = funder.resolve_funder(s, rpc, cr)
            members.append({"mint": m, "deployer": cr, "funder": f,
                            "sol_in": round(sol_in, 2)})
            if f and f not in funders_seen:
                funders_seen[f] = funder.wallet_tx_count(s, rpc, f)
            time.sleep(0.3)
            print(f"  {sym}: {m[:6]}.. dep {cr[:6]}.. fund {str(f)[:6]}..")
        deployers = {x["deployer"] for x in members if x["deployer"]}
        funders = {x["funder"] for x in members if x["funder"]}
        out_fams.append({
            "symbol": sym, "launches": len(mints), "checked": len(members),
            "n_deployers": len(deployers), "n_funders": len(funders),
            "members": members,
            "funder_txcounts": funders_seen,
        })
        print(f"{sym}: {len(mints)} launches | {len(deployers)} deployers -> {len(funders)} funders\n")

    doc = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # from the 2026-07-27 deployer probe (see memebot-deployer-reputation)
        "deployer_probe": {"sampled": 180, "unique_deployers": 179, "repeat_deployers": 1},
        "families": out_fams,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"wrote {OUT} ({len(out_fams)} families)")


if __name__ == "__main__":
    main()
