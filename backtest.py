#!/usr/bin/env python
"""Backtest the bot's exit rules on historical Solana memecoin OHLCV data.

Axiom and GMGN expose no public APIs (both are Cloudflare-gated to browsers),
so history comes from GeckoTerminal, which retains OHLCV for pools even after
the token dies. Cohorts:

  db        - candidates the bot itself recorded while scanning (unbiased; grows
              as the bot runs)
  pump      - top pools on pump.fun / pumpswap / raydium (volume-ranked, mixed
              outcomes; moderately survivor-biased)
  trending  - current trending/top pools (HEAVILY survivor-biased: these are
              the tokens that made it; treat their numbers as an upper bound)

Usage: python backtest.py [--max-tokens 60] [--entry-age 20] [--cost-pct 4]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

from bot.config import Config
from bot.util import parse_iso

GECKO = "https://api.geckoterminal.com/api/v2"
_MIN_INTERVAL = 2.2  # seconds between GeckoTerminal calls (~27 req/min)
_last_call = [0.0]


def plain_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/json", "User-Agent": "memebot-backtest/0.1"})
    return s


def gt_get(session, url: str, params: dict):
    """Throttled GET with real 429 backoff. The bot's shared retry adapter
    retries 429s quickly, which burns the rate budget - so the backtester
    paces itself instead."""
    for attempt in range(4):
        wait = _last_call[0] + _MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            r = session.get(url, params=params, timeout=20)
        except requests.RequestException:
            time.sleep(5)
            continue
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1))
            continue
        return r
    return None

EXCLUDE_SYMBOLS = {
    "SOL", "WSOL", "USDC", "USDT", "USDS", "USD1", "PYUSD", "USDE", "MSOL", "JITOSOL",
    "BSOL", "STSOL", "JLP", "WBTC", "WETH", "CBBTC", "JUP", "RAY", "PYTH", "W", "JTO",
    "TNSR", "KMNO", "DRIFT",
}


@dataclass
class Token:
    mint: str
    pool: str
    symbol: str
    created_ts: int
    cohort: str


@dataclass
class Trade:
    symbol: str
    cohort: str
    multiple: float
    reason: str


def _parse_pool(item: dict, cohort: str) -> Optional[Token]:
    a = item.get("attributes") or {}
    rel = item.get("relationships") or {}
    base = (((rel.get("base_token") or {}).get("data")) or {}).get("id", "").split("_", 1)[-1]
    name = a.get("name") or ""
    sym = (name.split("/")[0] or "?").strip()
    created = parse_iso(a.get("pool_created_at"))
    if not created or not base or not a.get("address"):
        return None
    if sym.upper() in EXCLUDE_SYMBOLS:
        return None
    return Token(base, a["address"], sym, int(created.timestamp()), cohort)


def _db_tokens(db_path: str) -> List[Token]:
    import sqlite3
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT mint, symbol, pool, pool_created_at FROM candidates WHERE pool_created_at IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        created = parse_iso(r["pool_created_at"])
        if created and r["pool"]:
            out.append(Token(r["mint"], r["pool"], r["symbol"] or "?", int(created.timestamp()), "db"))
    return out


def collect_tokens(session, max_tokens: int, db_path: str, min_age_min: float) -> List[Token]:
    sources = (
        ("pump", "dexes/pump-fun/pools", (1, 2)),
        ("pump", "dexes/pumpswap/pools", (1, 2)),
        ("pump", "dexes/raydium/pools", (1,)),
        ("trending", "trending_pools", (1, 2)),
        ("trending", "pools", (1,)),
    )
    seen, pump, trending = set(), [], []
    now = time.time()
    for cohort, endpoint, pages in sources:
        for page in pages:
            r = gt_get(session, f"{GECKO}/networks/solana/{endpoint}", {"page": page})
            if r is None or r.status_code != 200:
                print(f"  list fetch {endpoint} p{page} failed"
                      f" ({'no response' if r is None else r.status_code})")
                continue
            for item in (r.json().get("data") or []):
                t = _parse_pool(item, cohort)
                if not t or t.mint in seen:
                    continue
                if (now - t.created_ts) / 60 < min_age_min:
                    continue  # too young to have enough history
                seen.add(t.mint)
                (pump if cohort == "pump" else trending).append(t)
    db = [t for t in _db_tokens(db_path) if t.mint not in seen and (now - t.created_ts) / 60 >= min_age_min]
    pump_cap = pump[: int(max_tokens * 0.6)]
    rest_cap = (db + trending)[: max_tokens - len(pump_cap)]
    return pump_cap + rest_cap


CACHE_DIR = ".ohlcv_cache"
CACHE_TTL = 6 * 3600


def fetch_candles(session, token: Token) -> Tuple[Optional[list], int]:
    """Earliest candles from pool creation. 5-minute first; hourly fallback for
    pools too old for minute retention. Returns (ascending candles, candle_minutes).
    Successful fetches are cached on disk so rerunning a backtest or regenerating
    the journal doesn't re-spend the GeckoTerminal rate budget."""
    cpath = os.path.join(CACHE_DIR, f"{token.pool}.json")
    try:
        if os.path.exists(cpath) and time.time() - os.path.getmtime(cpath) < CACHE_TTL:
            with open(cpath, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("candles"):
                return cached["candles"], cached["cmin"]
    except Exception:
        pass
    for tf, agg, sec in (("minute", 5, 300), ("hour", 1, 3600)):
        r = gt_get(session, f"{GECKO}/networks/solana/pools/{token.pool}/ohlcv/{tf}",
                   {"aggregate": agg, "limit": 1000, "currency": "usd",
                    "before_timestamp": token.created_ts + 1000 * sec})
        if r is None or r.status_code != 200:
            continue
        try:
            lst = (((r.json().get("data") or {}).get("attributes")) or {}).get("ohlcv_list") or []
        except ValueError:
            continue
        lst = sorted((c for c in lst if c[0] >= token.created_ts), key=lambda c: c[0])
        if len(lst) >= 8:
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(cpath, "w", encoding="utf-8") as f:
                    json.dump({"candles": lst, "cmin": sec // 60}, f)
            except OSError:
                pass
            return lst, sec // 60
    return None, 0


def simulate(candles: list, exits: dict, entry_age_min: float, cost_pct: float,
             min_entry_vol: float) -> Optional[dict]:
    """Run the exit state machine over one token's candles.

    Conservative candle-ambiguity rule: stops/trailing are checked against the
    candle low BEFORE take-profits are checked against its high, and the peak
    for trailing only advances after the candle is fully processed.
    """
    created = candles[0][0]
    entry_ts = created + entry_age_min * 60
    pre = [c for c in candles if c[0] <= entry_ts]
    post = [c for c in candles if c[0] > entry_ts]
    if not pre or not post:
        return None
    entry = pre[-1][4]
    if entry <= 0 or sum(c[5] for c in pre) < min_entry_vol:
        return None

    stop_mult = 1 - exits["stop_loss_pct"] / 100
    tps = exits["take_profits"]
    hard = exits["hard_tp_multiple"]
    trail = exits["trailing_stop_pct"]
    max_hold = exits["max_hold_min"]

    remaining, received = 1.0, 0.0
    stage, peak = 0, 1.0
    reason = "data_end"
    events = []
    end_ts = post[-1][0]
    for ts, _o, h, l, cl, _v in post:
        age = (ts - entry_ts) / 60
        lo_m, hi_m, cl_m = l / entry, h / entry, cl / entry
        if stage == 0 and lo_m <= stop_mult:
            events.append({"ts": ts, "kind": "STOP", "portion": remaining, "mult": stop_mult})
            received += remaining * stop_mult
            remaining, reason, end_ts = 0.0, "stop_loss", ts
            break
        if stage > 0 and lo_m <= peak * (1 - trail / 100):
            level = peak * (1 - trail / 100)
            events.append({"ts": ts, "kind": "TRAIL", "portion": remaining, "mult": level})
            received += remaining * level
            remaining, reason, end_ts = 0.0, "trailing_stop", ts
            break
        while stage < len(tps) and hi_m >= float(tps[stage]["multiple"]):
            frac = float(tps[stage]["sell_fraction_of_remaining"])
            if frac > 0:
                events.append({"ts": ts, "kind": f"TP{stage + 1}", "portion": remaining * frac,
                               "mult": float(tps[stage]["multiple"])})
            received += remaining * frac * float(tps[stage]["multiple"])
            remaining *= 1 - frac
            stage += 1
        if remaining <= 1e-12:
            reason, end_ts = "take_profit", ts
            break
        if hi_m >= hard:
            events.append({"ts": ts, "kind": "HARD-TP", "portion": remaining, "mult": hard})
            received += remaining * hard
            remaining, reason, end_ts = 0.0, "hard_take_profit", ts
            break
        peak = max(peak, hi_m)
        if age >= max_hold:
            events.append({"ts": ts, "kind": "TIME", "portion": remaining, "mult": cl_m})
            received += remaining * cl_m
            remaining, reason, end_ts = 0.0, "time_stop", ts
            break
    if remaining > 1e-12:
        cl_m = post[-1][4] / entry
        events.append({"ts": post[-1][0], "kind": "END", "portion": remaining, "mult": cl_m})
        received += remaining * cl_m
    return {"multiple": received * (1 - cost_pct / 100), "reason": reason,
            "entry_ts": entry_ts, "entry_price": entry, "events": events, "end_ts": end_ts}


def summarize(name: str, trades: List[Trade]) -> None:
    print(f"\n== {name} ==")
    if not trades:
        print("  no trades")
        return
    mults = [t.multiple for t in trades]
    wins = [m for m in mults if m > 1.0]
    losses = [m for m in mults if m <= 1.0]
    n = len(mults)
    print(f"  trades {n} | win rate {100 * len(wins) / n:.0f}% | avg {statistics.mean(mults):.2f}x"
          f" | median {statistics.median(mults):.2f}x | expectancy {100 * (statistics.mean(mults) - 1):+.1f}%/trade")
    if wins:
        print(f"  avg winner {statistics.mean(wins):.2f}x (n={len(wins)})", end="")
    if losses:
        print(f" | avg loser {statistics.mean(losses):.2f}x (n={len(losses)})", end="")
    print()
    reasons = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    print("  exits: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
    for cohort in ("db", "pump", "trending"):
        ct = [t.multiple for t in trades if t.cohort == cohort]
        if ct:
            cw = sum(1 for m in ct if m > 1.0)
            print(f"    [{cohort:<8}] n={len(ct):<3} wr={100 * cw / len(ct):3.0f}% avg={statistics.mean(ct):.2f}x")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest memebot exit rules on GeckoTerminal history")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--max-tokens", type=int, default=60)
    ap.add_argument("--entry-age", type=float, default=20, help="minutes after pool creation to enter")
    ap.add_argument("--cost-pct", type=float, default=4, help="total round-trip cost haircut in %%")
    ap.add_argument("--min-entry-vol", type=float, default=2000, help="min USD volume before entry")
    ap.add_argument("--narrative", default="", help="comma-separated keywords; adds a subset report"
                    " for tokens whose symbol matches")
    ap.add_argument("--report-file", default=os.path.join("reports", "backtest_report.html"),
                    help="per-trade HTML journal for the D variant (empty string to skip)")
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config.load(args.config)
    session = plain_session()

    base_exits = {
        "stop_loss_pct": cfg.stop_loss_pct, "take_profits": cfg.take_profits,
        "hard_tp_multiple": cfg.hard_tp_multiple, "trailing_stop_pct": cfg.trailing_stop_pct,
        "max_hold_min": cfg.max_hold_min,
    }
    tp_desc = "/".join(f"{tp['multiple']}x:{int(float(tp['sell_fraction_of_remaining']) * 100)}%"
                       for tp in cfg.take_profits) or "none"
    variants = [
        (f"A: config.json exits (stop {cfg.stop_loss_pct:.0f}%, tp {tp_desc},"
         f" hard {cfg.hard_tp_multiple:.0f}x)", base_exits),
        ("B: let-winners-run (arm 35% trail at 2x, cap 10x)", {
            "stop_loss_pct": 40,
            "take_profits": [{"multiple": 2.0, "sell_fraction_of_remaining": 0.0}],
            "hard_tp_multiple": 10.0, "trailing_stop_pct": 35, "max_hold_min": 480,
        }),
        ("C: tight scalp (all out at 1.5x, stop 25%)", {
            "stop_loss_pct": 25,
            "take_profits": [{"multiple": 1.5, "sell_fraction_of_remaining": 1.0}],
            "hard_tp_multiple": 1.5, "trailing_stop_pct": 25, "max_hold_min": 120,
        }),
        ("D: hybrid scalp-runner (bank 60% @1.5x, trail 30% to 8x)", {
            "stop_loss_pct": 30,
            "take_profits": [{"multiple": 1.5, "sell_fraction_of_remaining": 0.6}],
            "hard_tp_multiple": 8.0, "trailing_stop_pct": 30, "max_hold_min": 360,
        }),
        # E targets high winrate + high avg: TP1 at 1.35x banks enough that the
        # trade is net-positive even if the runner half round-trips to the 35%
        # trail (0.675 + 0.5*1.35*0.65 = 1.11x pre-cost), while the wide trail
        # and 12x cap leave room for the outliers that drive the average.
        ("E: asym-runner (bank 50% @1.35x, trail 35% to 12x)", {
            "stop_loss_pct": 30,
            "take_profits": [{"multiple": 1.35, "sell_fraction_of_remaining": 0.5}],
            "hard_tp_multiple": 12.0, "trailing_stop_pct": 35, "max_hold_min": 480,
        }),
    ]

    min_age = args.entry_age + 60  # need at least an hour of post-entry history
    print(f"collecting cohorts (max {args.max_tokens} tokens, min age {min_age:.0f}m)...")
    tokens = collect_tokens(session, args.max_tokens, cfg.db_path, min_age)
    counts = {}
    for t in tokens:
        counts[t.cohort] = counts.get(t.cohort, 0) + 1
    print(f"cohort sizes: {counts}")

    results = {name: [] for name, _ in variants}
    details = {name: [] for name, _ in variants}
    skipped = 0
    for i, tok in enumerate(tokens, 1):
        candles, cmin = fetch_candles(session, tok)
        if not candles:
            skipped += 1
            continue
        entered = False
        for name, exits in variants:
            sim = simulate(candles, exits, args.entry_age, args.cost_pct, args.min_entry_vol)
            if sim:
                entered = True
                results[name].append(Trade(tok.symbol, tok.cohort, sim["multiple"], sim["reason"]))
                details[name].append({"token": tok, "candles": candles, "sim": sim,
                                      "exits": exits})
        if i % 10 == 0:
            print(f"  {i}/{len(tokens)} processed ({tok.symbol}, {cmin}m candles, entered={entered})")

    print(f"\ntokens processed: {len(tokens)} | no usable history: {skipped}")
    nar_keys = [k.strip().lower() for k in args.narrative.split(",") if k.strip()]
    for name, _ in variants:
        summarize(name, results[name])
        if nar_keys:
            subset = [t for t in results[name] if any(k in t.symbol.lower() for k in nar_keys)]
            summarize(name + " -- NARRATIVE SUBSET", subset)

    if args.report_file and any(details.values()):
        import btreport
        btreport.render_report(details, nar_keys, args.cost_pct, args.report_file)
        print(f"\nper-trade journal written: {os.path.abspath(args.report_file)}")

    print("""
CAVEATS - read before believing any number above:
 * Trending cohort is survivor-biased: those tokens are on the leaderboard
   BECAUSE they pumped. Their stats are an upper bound, not an expectation.
 * Fills are assumed exactly at trigger prices; live fills are worse (latency,
   slippage beyond the haircut, failed transactions, MEV).
 * Entry filters are approximated by age + early volume only - the live bot's
   liquidity/rugcheck/insider screens can't be reconstructed historically here.
 * Candle-level simulation: intra-candle ordering of stop vs target is assumed
   conservatively (stop first), but 5-minute candles still hide sequence risk.
 * Small samples move a lot between runs. Rerun on different days; trust the
   bot's own recorded candidates cohort ([db]) as it grows - it is the only
   unbiased one.""")


if __name__ == "__main__":
    main()
