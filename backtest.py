#!/usr/bin/env python
"""Backtest the bot's exit rules on historical Solana memecoin OHLCV data.

Axiom and GMGN expose no public APIs (both are Cloudflare-gated to browsers),
so history comes from GeckoTerminal, which retains OHLCV for pools even after
the token dies. Cohorts:

  db        - candidates the bot itself recorded while scanning (unbiased; grows
              as the bot runs)
  pump      - top pools on pump.fun / pumpswap / raydium (volume-ranked, mixed
              outcomes; moderately survivor-biased)
  new       - recently created pools regardless of outcome (new_pools list;
              the least survivor-biased list the API offers)
  trending  - current trending/top pools (HEAVILY survivor-biased: these are
              the tokens that made it; treat their numbers as an upper bound)

Usage: python backtest.py [--max-tokens 60] [--entry-age 20] [--cost-pct 4]
"""
from __future__ import annotations

import argparse
import hashlib
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
    """Enumerate as many distinct tokens as the public list endpoints allow.
    GeckoTerminal paginates each list to ~10 pages of 20 pools, so the hard
    enumeration ceiling is a few hundred unique tokens after dedup - thousands
    are not reachable from these endpoints no matter what max_tokens asks for.

    Fair cohorts: pump (volume-ranked, mildly biased), new (new_pools - every
    recently created pool regardless of outcome, the least biased list), db
    (the bot's own scanner records, unbiased). trending stays a survivor-biased
    reference only."""
    sources = (
        ("pump", "dexes/pump-fun/pools", range(1, 11)),
        ("pump", "dexes/pumpswap/pools", range(1, 11)),
        ("pump", "dexes/raydium/pools", range(1, 6)),
        ("new", "new_pools", range(1, 11)),
        ("trending", "trending_pools", range(1, 6)),
        ("trending", "pools", range(1, 4)),
    )
    seen, buckets = set(), {"pump": [], "new": [], "trending": []}
    now = time.time()
    for cohort, endpoint, pages in sources:
        for page in pages:
            r = gt_get(session, f"{GECKO}/networks/solana/{endpoint}", {"page": page})
            if r is None or r.status_code != 200:
                print(f"  list fetch {endpoint} p{page} failed"
                      f" ({'no response' if r is None else r.status_code}); skipping rest of endpoint")
                break
            items = r.json().get("data") or []
            if not items:
                break  # past the last page
            for item in items:
                t = _parse_pool(item, cohort)
                if not t or t.mint in seen:
                    continue
                if (now - t.created_ts) / 60 < min_age_min:
                    continue  # too young to have enough history
                seen.add(t.mint)
                buckets[cohort].append(t)
    db = [t for t in _db_tokens(db_path) if t.mint not in seen and (now - t.created_ts) / 60 >= min_age_min]
    fair = db + buckets["pump"] + buckets["new"]
    fair_cap = fair[: int(max_tokens * 0.85)]
    trend_cap = buckets["trending"][: max_tokens - len(fair_cap)]
    return fair_cap + trend_cap


CACHE_DIR = ".ohlcv_cache"
CACHE_TTL = 6 * 3600


def load_cached_candles(cache_dir: str, pool: str):
    """Direct cache read - no TTL, no network. Historical candles are
    immutable, so index-driven runs (e.g. the Birdeye sample) and the sweeps
    read cache files as-is."""
    cpath = os.path.join(cache_dir, f"{pool}.json")
    if not os.path.exists(cpath):
        return None
    try:
        with open(cpath, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError):
        return None
    return cached.get("candles") or None


BLOCKLIST_PATH = os.path.join("reports", "bd_blocklist.json")
MANUAL_BLOCKLIST_PATH = os.path.join("reports", "bd_blocklist_manual.json")


def load_blocklist() -> dict:
    """AUTO-detected wash-ramp tokens purged from the sample (see blocklist.py).
    Their painted prices fabricate wins, so they are never considered again by
    any consumer of the token index. Retroactive removal is safe here because
    the detector is outcome-asymmetric by construction (only rising ramps -
    fabricated wins - are ever purged). Hand-flagged charts live in a separate
    file with forward-only semantics: see load_manual_flags()."""
    try:
        with open(BLOCKLIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_manual_flags() -> dict:
    """Hand-flagged charts from the dashboard's Blacklist button
    (reports/bd_blocklist_manual.json, reason "manual"). FORWARD-ONLY: a
    manual flag never removes the flagged trade from existing stats - flagging
    a known loser would otherwise inflate every downstream number (locked
    honesty rule). It only excludes tokens LISTED AFTER the flag time whose
    mint or symbol matches, and files the chart on the /blacklist tab as an
    avoid-pattern reference."""
    try:
        with open(MANUAL_BLOCKLIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _manual_cutoffs(manual: dict) -> Tuple[dict, dict]:
    """(mint -> flag_epoch, SYMBOL -> earliest flag_epoch) for forward-only
    exclusion. Entries with an unparseable flag time exclude nothing (never
    retroactive by accident)."""
    mint_ts, sym_ts = {}, {}
    for mint, v in manual.items():
        flagged = parse_iso(v.get("flagged_at") or "")
        if not flagged:
            continue
        ts = flagged.timestamp()
        mint_ts[mint] = ts
        s = str(v.get("symbol") or "").strip().upper()
        if s and s != "?":
            sym_ts[s] = min(ts, sym_ts.get(s, ts))
    return mint_ts, sym_ts


def load_token_index(path: str) -> List[Token]:
    """Tokens from a fetcher-written index (e.g. bdfetch.py's reports/bd_tokens.json).
    Auto-blocklisted wash-ramp tokens are dropped unconditionally; manually
    flagged charts drop only FUTURE listings (created after the flag) matching
    the flagged mint or symbol, so hand-flagging never edits existing stats.
    Every consumer (backtest, sweeps, tradecards, montecarlo) sees the same
    universe."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    blocked = load_blocklist()
    mint_ts, sym_ts = _manual_cutoffs(load_manual_flags())
    tokens, dropped_fwd = [], 0
    for t in raw:
        if t["mint"] in blocked:
            continue
        created = int(t["created_ts"])
        sym = str(t.get("symbol") or "").strip().upper()
        cut = mint_ts.get(t["mint"])
        scut = sym_ts.get(sym)
        if (cut is not None and created > cut) or (scut is not None and created > scut):
            dropped_fwd += 1
            continue
        tokens.append(Token(mint=t["mint"], pool=t["pool"], symbol=t.get("symbol", "?"),
                            created_ts=created, cohort=t.get("cohort", "new")))
    if blocked:
        print(f"token index: {len(raw) - len(tokens) - dropped_fwd} blocklisted wash-ramp tokens "
              f"excluded ({len(tokens)} remain)")
    if dropped_fwd:
        print(f"token index: {dropped_fwd} post-flag listings excluded by forward-only manual flags")
    return tokens


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


# Base-entry setup filter: only take tokens that look like a consolidation
# base at entry time — flat recent range, no collapse candle, price still near
# its credible peak, and not mid-spike. Validated on the Birdeye sample
# (age30/vol8k entries): baseline 1028 trades 68% WR / 1.23x avg -> filtered
# 171 trades 81% WR / 1.54x avg, stable across both sha1 halves
# (train 1.75x/82%, valid 1.36x/80%). All windows are time-based so the same
# rule works on 1m and 5m candles.
DEFAULT_SETUP = {
    "base_window_min": 10,      # lookback for the consolidation-range check
    "max_base_range_pct": 25.0, # (high-low) of that window, as % of entry
    "max_nukes": 1,             # collapse candles allowed inside peak_window_min
    "nuke_body": 0.70,          # close/open at or below this = collapse candle
    "min_frac_of_peak": 0.60,   # entry must be >= this fraction of the credible peak
    "cred_vol_usd": 500.0,      # candle volume needed to count toward the credible peak
    "peak_window_min": 30,      # P1: bounded lookback for cred_peak AND the nuke
                                # count. Previously both scanned the token's whole
                                # pre-entry life, so the gate silently changed
                                # meaning with age: at the fitted 30m entry age it
                                # saw ~30 candles, but live entries at 11h fed it
                                # ~660 - measuring "60% of the 11-hour high" and
                                # "<=1 collapse in 660 candles" instead of the
                                # validated rule. 30 reproduces the fitted
                                # semantics exactly at entry_age 30 and keeps them
                                # identical at any age.
    "trend_window_min": 15,     # lookback for the not-chasing check
    "trend_lo": 0.90,           # entry / close-15m-ago bounds: below = bleeding,
    "trend_hi": 1.25,           #   above = entering mid-spike
}


def entry_setup_ok(pre: list, entry_ts: int, entry: float, setup: dict) -> bool:
    """Entry-time-only check of the base-then-breakout setup on the pre-entry
    candles. Credible peak uses the engine-v5 wash-guard (volume floor + close
    at least half the high, peak capped at 2x close)."""
    base = [c for c in pre if c[0] > entry_ts - setup["base_window_min"] * 60]
    if base:
        rng = max(c[2] for c in base) - min(c[3] for c in base)
        if 100 * rng / entry > setup["max_base_range_pct"]:
            return False
    # P1: both of these are bounded to peak_window_min so the gate means the
    # same thing whether the token is 30 minutes or 20 hours old. Absent a
    # window key (older callers / tuned setups) fall back to the whole history.
    pw = setup.get("peak_window_min")
    scan = [c for c in pre if c[0] > entry_ts - pw * 60] if pw else pre
    cred_peak, nukes = 0.0, 0
    for _ts, o, h, _l, cl, v in scan:
        if v >= setup["cred_vol_usd"] and cl >= 0.5 * h:
            cred_peak = max(cred_peak, min(h, cl * 2))
        if o > 0 and cl / o <= setup["nuke_body"]:
            nukes += 1
    if nukes > setup["max_nukes"]:
        return False
    if cred_peak > 0 and entry / cred_peak < setup["min_frac_of_peak"]:
        return False
    refs = [c[4] for c in pre if c[0] <= entry_ts - setup["trend_window_min"] * 60]
    ref = refs[-1] if refs else pre[0][4]
    if ref > 0 and not (setup["trend_lo"] <= entry / ref <= setup["trend_hi"]):
        return False
    return True


# P2: how stops and trailing stops actually fill. The pre-fix engine filled
# both at exactly their trigger level no matter how far the candle low sat
# below it, which is free money the market does not offer. Measured against the
# live paper trial (memebot-asym, 2026-07-23..25): trailing stops filled 15-27%
# BELOW their trigger (RDLN -27.1%, sharkdog -22.6%, RAKO -15.0%) and stops
# 1-21% below. use_candle_low captures the gap-through, the slip percentages
# then cover AMM price impact on the way out.
#
# `low_weight` places the fill between the trigger and the candle low:
#   0.0 = fill at the trigger (optimistic bound, the pre-fix behaviour)
#   1.0 = fill at the candle low (pessimistic bound - worst tick, every time)
# Neither bound is the truth. The only live fill data available - the BASE
# trial's three trailing stops, which came in 15%, 23% and 27% under trigger -
# sits nearer the pessimistic end, so the default leans that way without
# assuming the worst tick on every exit.
FILL_MODEL = {
    "use_candle_low": True,   # False pins low_weight to 0 (fill at trigger)
    "low_weight": 0.7,        # how far toward the candle low the fill lands
    "stop_slip_pct": 5.0,     # extra haircut on stop fills (live median: -4.9%)
    "trail_slip_pct": 5.0,    # extra haircut on trailing-stop fills
}


def fill_at(trigger: float, low: float, fill: dict) -> float:
    """Blend the trigger and the candle low per the fill model."""
    if not fill.get("use_candle_low", True) or low >= trigger:
        return trigger
    w = fill.get("low_weight", 1.0)
    return trigger + (low - trigger) * w

# P0: what to do when a token's candle history ends before the position reaches
# a real exit. The pre-fix engine marked the position closed at the last
# available candle and called it a trade ("data_end"). That was 73.6% of the
# 318-trade sample at a 0.99h median hold against an 8h max_hold - it froze
# three quarters of the book at the one-hour mark, before the losses that
# arrive later could land, and scored the result 87.2% WR / 1.425x.
#
#   loss - candles stop because the token stopped trading. An unsellable bag is
#          a total loss on the un-banked remainder. This is the default: it is
#          both the realistic reading for memecoins and the conservative one.
#   drop - exclude the trade from the sample. Honest about not knowing, but
#          biased toward tokens the data provider kept covering (survivors).
#   last - the old behaviour, retained only so the bias can be measured.
#   stop - the bot keeps polling a live AMM quote after the candles stop, so its
#          stop-loss / time-exit still fires. Candles vanish when nobody TRADES;
#          the pool still holds reserves and still quotes. Calibrated against
#          the paper trial: 19 closed positions, ZERO quote failures, worst
#          outcome 0.375, nothing under 0.10 - against which "loss" (63% of
#          trades to 0.0) is plainly too harsh and "last" too generous.
COVERAGE_POLICIES = ("stop", "loss", "drop", "last")
DEFAULT_COVERAGE = "stop"

# What fraction of censored positions are genuine rugs - liquidity pulled, no
# quote at any price, a real 0.0 - rather than merely illiquid. The live record
# is 0/19, but 19 trades behind rugcheck + a round-trip slippage gate cannot
# justify 0%, so this stays deliberately non-zero. Assignment is a deterministic
# hash of the token's own timestamps: reproducible across runs, and it preserves
# the variance a flat expected-value haircut would flatten out.
#
# `dark_slip_pct` is the extra haircut for exiting a pool that has stopped
# trading. It CANNOT be calibrated from the paper record - live has had zero
# dark exits (every one of 19 positions exited normally), so there is no
# observation to fit. It is therefore an explicit, stated assumption and the
# single largest source of uncertainty in the model: sweeping it across a
# plausible range moves BASE's average by ~0.3x. Resolving it needs real dark
# exits observed in paper trading, not more backtesting.
CENSOR_MODEL = {"rug_pct": 5.0, "dark_slip_pct": 45.0}
# 45% is where the model's median trade matches the live median exactly (0.599)
# while keeping near-zero total losses, as observed. That is a fit to 13 closed
# BASE trades - weak evidence, and it should be re-checked as the record grows.
# live_calibrate.py re-runs the comparison; the sensitivity is roughly 0.03x of
# average per 10 points of dark slip.


def _is_rug(created_ts: int, entry_ts: float, rug_pct: float) -> bool:
    """Stable per-token draw - no RNG, so a rerun reproduces the same book."""
    if rug_pct <= 0:
        return False
    h = hashlib.sha1(f"{int(created_ts)}:{int(entry_ts)}".encode()).hexdigest()
    return (int(h[:8], 16) % 10000) < rug_pct * 100


def simulate(candles: list, exits: dict, entry_age_min: float, cost_pct: float,
             min_entry_vol: float, setup: Optional[dict] = None,
             fill: Optional[dict] = None, coverage: str = DEFAULT_COVERAGE,
             censor: Optional[dict] = None) -> Optional[dict]:
    """Run the exit state machine over one token's candles.

    Conservative candle-ambiguity rule: stops/trailing are checked against the
    candle low BEFORE take-profits are checked against its high, and the peak
    for trailing only advances after the candle is fully processed.

    `setup` (e.g. DEFAULT_SETUP) additionally requires the base-entry pattern
    at entry time; None keeps the unconditional age/volume entry.

    `fill` (see FILL_MODEL) sets how stops/trails fill; `coverage` (see
    COVERAGE_POLICIES) sets what happens when the candles run out before a real
    exit fires. Returns None for "drop"; otherwise the result carries
    `censored=True` so callers can count what the policy absorbed.
    """
    fill = FILL_MODEL if fill is None else fill
    censor = CENSOR_MODEL if censor is None else censor
    stop_slip = 1 - fill.get("stop_slip_pct", 0.0) / 100
    trail_slip = 1 - fill.get("trail_slip_pct", 0.0) / 100
    created = candles[0][0]
    entry_ts = created + entry_age_min * 60
    pre = [c for c in candles if c[0] <= entry_ts]
    post = [c for c in candles if c[0] > entry_ts]
    if not pre or not post:
        return None
    entry = pre[-1][4]
    if entry <= 0 or sum(c[5] for c in pre) < min_entry_vol:
        return None
    if setup and not entry_setup_ok(pre, entry_ts, entry, setup):
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
            got = fill_at(stop_mult, lo_m, fill) * stop_slip
            events.append({"ts": ts, "kind": "STOP", "portion": remaining, "mult": got})
            received += remaining * got
            remaining, reason, end_ts = 0.0, "stop_loss", ts
            break
        if stage > 0 and lo_m <= peak * (1 - trail / 100):
            level = peak * (1 - trail / 100)
            got = fill_at(level, lo_m, fill) * trail_slip
            events.append({"ts": ts, "kind": "TRAIL", "portion": remaining, "mult": got})
            received += remaining * got
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
    # P0: falling out of the loop with stock still held means the candles ran
    # out before max_hold elapsed - the exit machine never got to finish. That
    # is censored data, not a trade outcome.
    censored = remaining > 1e-12
    if censored:
        if coverage == "drop":
            return None
        if coverage == "last":
            cl_m = post[-1][4] / entry
            events.append({"ts": post[-1][0], "kind": "END", "portion": remaining, "mult": cl_m})
            received += remaining * cl_m
        elif coverage == "stop":
            # the bot is still polling a live quote: its stop/time-exit fires
            # against the pool even though nobody else is trading
            if _is_rug(candles[0][0], entry_ts, censor.get("rug_pct", 0.0)):
                events.append({"ts": post[-1][0], "kind": "RUG", "portion": remaining, "mult": 0.0})
                reason = "rugged"
            else:
                cl_m = post[-1][4] / entry
                got = (min(cl_m, stop_mult) * stop_slip
                       * (1 - censor.get("dark_slip_pct", 0.0) / 100))
                events.append({"ts": post[-1][0], "kind": "DARK-STOP",
                               "portion": remaining, "mult": got})
                received += remaining * got
                reason = "dark_stop"
        else:  # "loss" - the token stopped trading; the remainder is unsellable
            events.append({"ts": post[-1][0], "kind": "DEAD", "portion": remaining, "mult": 0.0})
            reason = "no_coverage"
        end_ts = post[-1][0]
    return {"multiple": received * (1 - cost_pct / 100), "reason": reason, "censored": censored,
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
    for cohort in ("db", "pump", "new", "trending"):
        ct = [t.multiple for t in trades if t.cohort == cohort]
        if ct:
            cw = sum(1 for m in ct if m > 1.0)
            print(f"    [{cohort:<8}] n={len(ct):<3} wr={100 * cw / len(ct):3.0f}% avg={statistics.mean(ct):.2f}x")


def stats_dict(mults: List[float]) -> Optional[dict]:
    if not mults:
        return None
    wins = [m for m in mults if m > 1.0]
    return {
        "n": len(mults),
        "wr": round(100 * len(wins) / len(mults), 1),
        "avg": round(statistics.mean(mults), 3),
        "median": round(statistics.median(mults), 3),
        "expectancy_pct": round(100 * (statistics.mean(mults) - 1), 1),
    }


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
    ap.add_argument("--no-report", action="store_true",
                    help="skip the HTML journal (recommended for multi-thousand-token samples)")
    ap.add_argument("--tokens-json", default="",
                    help="token index file (e.g. reports/bd_tokens.json) instead of live GeckoTerminal lists")
    ap.add_argument("--cache-dir", default="", help="candle cache dir (default .ohlcv_cache)")
    ap.add_argument("--no-setup-filter", action="store_true",
                    help="disable the base-entry setup filter and take every age/volume entry")
    ap.add_argument("--coverage", default=DEFAULT_COVERAGE, choices=COVERAGE_POLICIES,
                    help="what to do when candles run out before a real exit fires: "
                         "loss (default, unsellable bag), drop (exclude, survivor-biased), "
                         "last (pre-fix behaviour - mark at last price; inflates results)")
    ap.add_argument("--stop-slip", type=float, default=FILL_MODEL["stop_slip_pct"],
                    help="extra haircut %% on stop fills (default %(default)s)")
    ap.add_argument("--trail-slip", type=float, default=FILL_MODEL["trail_slip_pct"],
                    help="extra haircut %% on trailing-stop fills (default %(default)s)")
    ap.add_argument("--dark-slip", type=float, default=CENSOR_MODEL["dark_slip_pct"],
                    help="extra %% haircut exiting a pool that stopped trading, under "
                         "--coverage stop. UNCALIBRATED - live has had no dark exits "
                         "(default %(default)s)")
    ap.add_argument("--rug-pct", type=float, default=CENSOR_MODEL["rug_pct"],
                    help="%% of censored positions treated as real rugs (0.0) under "
                         "--coverage stop; the rest exit at the stop (default %(default)s)")
    ap.add_argument("--low-weight", type=float, default=FILL_MODEL["low_weight"],
                    help="how far from trigger toward the candle low stops/trails fill: "
                         "0=trigger (optimistic), 1=candle low (pessimistic) (default %(default)s)")
    ap.add_argument("--fill-at-trigger", action="store_true",
                    help="pre-fix fill model: fill stops/trails at their trigger level even when "
                         "the candle gapped straight through it")
    args = ap.parse_args()
    fill = {"use_candle_low": not args.fill_at_trigger, "low_weight": args.low_weight,
            "stop_slip_pct": args.stop_slip, "trail_slip_pct": args.trail_slip}
    censor = {"rug_pct": args.rug_pct, "dark_slip_pct": args.dark_slip}

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config.load(args.config)
    session = plain_session()
    cache_dir = args.cache_dir or CACHE_DIR

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
        # E = the adopted asym-runner v2: best cleanly-executable config from the
        # round-2 Birdeye sweeps (valid 81% WR / 1.65x avg with age30/vol8k entry
        # filters). Wide 40% stop + small 30% bank at 1.35x arms a 30% trail with
        # a 20x cap so the rare runners can pay for the losers.
        ("E: asym-runner v2 (bank 30% @1.35x, trail 30% to 20x)", {
            "stop_loss_pct": 40,
            "take_profits": [{"multiple": 1.35, "sell_fraction_of_remaining": 0.3}],
            "hard_tp_multiple": 20.0, "trailing_stop_pct": 30, "max_hold_min": 480,
        }),
    ]

    if args.tokens_json:
        tokens = load_token_index(args.tokens_json)
        print(f"token index: {len(tokens)} tokens from {args.tokens_json} (cache: {cache_dir})")
    else:
        min_age = args.entry_age + 60  # need at least an hour of post-entry history
        print(f"collecting cohorts (max {args.max_tokens} tokens, min age {min_age:.0f}m)...")
        tokens = collect_tokens(session, args.max_tokens, cfg.db_path, min_age)
    counts = {}
    for t in tokens:
        counts[t.cohort] = counts.get(t.cohort, 0) + 1
    print(f"cohort sizes: {counts}")
    setup = None if args.no_setup_filter else DEFAULT_SETUP
    if setup:
        print(f"base-entry setup filter ON: {setup['base_window_min']}m range <= "
              f"{setup['max_base_range_pct']:.0f}% · nukes <= {setup['max_nukes']} · >= "
              f"{setup['min_frac_of_peak']:.0%} of credible peak · {setup['trend_window_min']}m trend "
              f"{setup['trend_lo']:.2f}-{setup['trend_hi']:.2f}x")
    else:
        print("base-entry setup filter OFF (--no-setup-filter)")
    if setup and setup.get("peak_window_min"):
        print(f"  peak/nuke lookback bounded to {setup['peak_window_min']}m (age-invariant gate)")
    print(f"coverage policy: {args.coverage}"
          + (f" (rug {args.rug_pct:.1f}%, dark slip {args.dark_slip:.1f}%)"
             if args.coverage == "stop" else "")
          + ("  [WARNING: 'last' is the pre-fix behaviour and inflates results]"
             if args.coverage == "last" else ""))
    print(f"fill model: stops/trails {(str(int(100 * fill['low_weight'])) + '% toward candle low') if fill['use_candle_low'] else 'at trigger'}"
          f" · stop slip {fill['stop_slip_pct']:.1f}% · trail slip {fill['trail_slip_pct']:.1f}%")

    def gt_progress(phase: str, done: int, cached: int, no_data: int):
        """Status file the NERV dashboard polls to draw its progress bar."""
        try:
            os.makedirs("reports", exist_ok=True)
            with open(os.path.join("reports", "gt_progress.json"), "w", encoding="utf-8") as f:
                json.dump({"name": "GECKOTERMINAL SAMPLE", "phase": phase, "done": done,
                           "total": len(tokens), "cached": cached, "no_data": no_data,
                           "updated": time.time()}, f)
        except OSError:
            pass

    results = {name: [] for name, _ in variants}
    details = {name: [] for name, _ in variants}
    censored = {name: 0 for name, _ in variants}
    skipped = 0
    for i, tok in enumerate(tokens, 1):
        if args.tokens_json:
            candles, cmin = load_cached_candles(cache_dir, tok.pool), 1
        else:
            candles, cmin = fetch_candles(session, tok)
        if not candles:
            skipped += 1
            continue
        entered = False
        for name, exits in variants:
            sim = simulate(candles, exits, args.entry_age, args.cost_pct, args.min_entry_vol,
                           setup=setup, fill=fill, coverage=args.coverage, censor=censor)
            if sim:
                entered = True
                censored[name] += bool(sim.get("censored"))
                results[name].append(Trade(tok.symbol, tok.cohort, sim["multiple"], sim["reason"]))
                if not args.no_report:
                    details[name].append({"token": tok, "candles": candles, "sim": sim,
                                          "exits": exits})
        if i % 10 == 0 and not args.tokens_json:
            print(f"  {i}/{len(tokens)} processed ({tok.symbol}, {cmin}m candles, entered={entered})",
                  flush=True)
            gt_progress("fetch", i, i - skipped, skipped)
    if not args.tokens_json:
        gt_progress("done", len(tokens), len(tokens) - skipped, skipped)

    print(f"\ntokens processed: {len(tokens)} | no usable history: {skipped}")
    nar_keys = [k.strip().lower() for k in args.narrative.split(",") if k.strip()]
    for name, _ in variants:
        summarize(name, results[name])
        n, cen = len(results[name]), censored[name]
        if n:
            # Loud, because this number is the whole reason the pre-fix engine
            # read 1.605x on a strategy the fresh sample scored at 0.226x.
            print(f"  censored (candles ended before a real exit): {cen}/{n} "
                  f"({100 * cen / n:.1f}%) - policy '{args.coverage}'")
        if nar_keys:
            subset = [t for t in results[name] if any(k in t.symbol.lower() for k in nar_keys)]
            summarize(name + " -- NARRATIVE SUBSET", subset)

    # machine-readable summary (read by dashboard/server.py for the asym panel)
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sample": {"source": args.tokens_json or "geckoterminal lists",
                   "max_tokens": args.max_tokens, "entry_age_min": args.entry_age,
                   "cost_pct": args.cost_pct, "cohorts": counts,
                   "tokens_processed": len(tokens) - skipped, "no_history": skipped,
                   "setup_filter": setup,
                   "coverage_policy": args.coverage, "fill_model": fill,
                   "censor_model": censor},
        "variants": [],
    }
    for name, exits in variants:
        trades = results[name]
        cohorts = {}
        for cohort in ("db", "pump", "new", "trending"):
            cs = stats_dict([t.multiple for t in trades if t.cohort == cohort])
            if cs:
                cohorts[cohort] = cs
        summary["variants"].append({
            "name": name, "exits": exits, "censored": censored[name],
            "overall": stats_dict([t.multiple for t in trades]),
            "fair": stats_dict([t.multiple for t in trades if t.cohort in ("db", "pump", "new")]),
            "cohorts": cohorts,
        })
    spath = os.path.join("reports", "backtest_summary.json")
    os.makedirs("reports", exist_ok=True)
    with open(spath, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    print(f"machine-readable summary: {os.path.abspath(spath)}")

    if args.report_file and not args.no_report and any(details.values()):
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
