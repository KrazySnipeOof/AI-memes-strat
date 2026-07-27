#!/usr/bin/env python
"""Backtest + config generator for the 3 new family strategies:
  sniper          ultra-early listings with real two-sided volume
  volume_anomaly  abnormal volume spike vs the token's own baseline
  dip             mean-reversion: pullback off a local peak + green bounce

All three: ORGANIC-only (bot.manip) and max-hold a few hours. Entry screens
come from bot/screens.py so the backtest and the live bot judge entries
identically. Writes reports/newstrat_backtest.json (per-strategy multiples for
the Monte Carlo + card_trades for the journal) and config.<name>.json (guarded
- won't clobber an existing/live config).
"""
from __future__ import annotations

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import backtest as bt
from bot import manip, screens

INDEXES = ("reports/bd_tokens.json", "reports/bd_tokens_60d.json", "reports/bd_tokens_oot.json")
CACHES = (".bd_cache_ext", ".bd_cache")
COST_PCT = 4.0
MIN_ENTRY_VOL = 1500.0
DENSITY = 15  # backtest sample is ~15x denser than live token flow

# Each strategy: entry age, screen family + params, exit doctrine (<=3h hold).
STRATS = {
    "sniper": {
        "label": "Sniper", "entry_age": 10, "family": "sniper",
        "params": {"min_cum_vol_usd": 3000, "min_two_sided": 0.12},
        "exits": {"stop_loss_pct": 40, "take_profits": [{"multiple": 1.5, "sell_fraction_of_remaining": 0.5}],
                  "trailing_stop_pct": 25, "hard_tp_multiple": 8.0, "max_hold_min": 120},
        "filters": {"min_age_min": 5, "max_age_min": 40, "min_liquidity_usd": 3000,
                    "max_liquidity_usd": 500000, "min_vol_m5_usd": 1500},
    },
    "volanomaly": {
        "label": "Volume-anomaly", "entry_age": 30, "family": "volume_anomaly",
        "entry_mode": "scan", "min_scan_age": 15,
        "params": {"min_vol_ratio": 4.0, "min_spike_vol_usd": 2000},
        "exits": {"stop_loss_pct": 45, "take_profits": [{"multiple": 1.5, "sell_fraction_of_remaining": 0.4}],
                  "trailing_stop_pct": 30, "hard_tp_multiple": 10.0, "max_hold_min": 120},
        "filters": {"min_age_min": 15, "max_age_min": 720, "min_liquidity_usd": 5000,
                    "max_liquidity_usd": 1000000, "min_vol_m5_usd": 2000},
    },
    "dip": {
        "label": "Dip-buy", "entry_age": 45, "family": "dip",
        "entry_mode": "scan", "min_scan_age": 30,
        "params": {"peak_lookback": 20, "min_drawdown": 0.18, "max_drawdown": 0.45,
                   "min_bounce_vol_usd": 1000},
        "exits": {"stop_loss_pct": 35, "take_profits": [{"multiple": 1.8, "sell_fraction_of_remaining": 0.5}],
                  "trailing_stop_pct": 25, "hard_tp_multiple": 6.0, "max_hold_min": 180},
        "filters": {"min_age_min": 30, "max_age_min": 1440, "min_liquidity_usd": 8000,
                    "max_liquidity_usd": 2000000, "min_vol_m5_usd": 2000},
    },
}


def load_universe():
    seen, out = set(), []
    for idx in INDEXES:
        if not os.path.exists(idx):
            continue
        for tok in bt.load_token_index(idx):
            if tok.mint in seen:
                continue
            seen.add(tok.mint)
            for cd in CACHES:
                c = bt.load_cached_candles(cd, tok.pool)
                if c:
                    out.append((tok, c))
                    break
    return out


def _entry_index(candles, spec):
    """Index of the entry candle. 'fixed' mode = first candle at/after entry_age;
    'scan' mode = first candle (past min_scan_age) where the screen fires - so
    event-driven signals (volume spike, dip bounce) are caught whenever they
    occur, matching the live bot that checks every cycle."""
    created = candles[0][0]
    fam, params = spec["family"], spec["params"]
    if spec.get("entry_mode") == "scan":
        min_scan = spec.get("min_scan_age", 15)
        for i in range(len(candles)):
            if (candles[i][0] - created) / 60 < min_scan:
                continue
            rows = candles[:i + 1]
            if len(rows) < 8 or sum(c[5] for c in rows) < MIN_ENTRY_VOL:
                continue
            ok, _ = screens.check(fam, rows, params)
            if ok:
                return i
        return None
    entry_ts = created + spec["entry_age"] * 60
    for i in range(len(candles)):
        if candles[i][0] >= entry_ts:
            return i
    return None


def backtest_strategy(universe, spec):
    """Trades for one strategy: pre-entry screen + ORGANIC + simulate exits."""
    fam, params, exits = spec["family"], spec["params"], spec["exits"]
    fixed = spec.get("entry_mode") != "scan"
    trades, cards = [], []
    for tok, candles in universe:
        i = _entry_index(candles, spec)
        if i is None or i >= len(candles) - 1:
            continue
        pre = candles[:i + 1]
        if len(pre) < 8 or pre[-1][4] <= 0 or sum(c[5] for c in pre) < MIN_ENTRY_VOL:
            continue
        if fixed:  # scan mode already confirmed the screen at the entry candle
            ok, _ = screens.check(fam, pre, params)
            if not ok:
                continue
        if (manip.classify(pre, False) or {}).get("class") != "ORGANIC":
            continue
        entry_age = (candles[i][0] - candles[0][0]) / 60
        sim = bt.simulate(candles, exits, entry_age, COST_PCT, MIN_ENTRY_VOL, setup=None)
        if not sim:
            continue
        hold = (sim["end_ts"] - sim["entry_ts"]) / 60
        trades.append({"mult": sim["multiple"], "hold": hold, "entry_ts": sim["entry_ts"],
                       "reason": sim["reason"]})
        cards.append({"mint": tok.mint, "symbol": tok.symbol, "pool": tok.pool,
                      "entry_ts": int(sim["entry_ts"])})
    return trades, cards


def stats(trades):
    if not trades:
        return {"n": 0}
    m = [t["mult"] for t in trades]
    wins = [x for x in m if x > 1.0]
    ts = [t["entry_ts"] for t in trades]
    weeks = max(1e-9, (max(ts) - min(ts)) / 86400 / 7)
    return {"n": len(m), "live_wk_est": round(len(m) / weeks / DENSITY, 1),
            "wr": round(100 * len(wins) / len(m), 1),
            "avg": round(statistics.mean(m), 3), "median": round(statistics.median(m), 3),
            "exp_pct": round(100 * (statistics.mean(m) - 1), 1),
            "median_hold_min": round(statistics.median(t["hold"] for t in trades), 0),
            "twox": sum(1 for x in m if x >= 2), "fivex": sum(1 for x in m if x >= 5),
            "best": round(max(m), 2)}


def make_config(key, spec):
    ex = spec["exits"]
    f = spec["filters"]
    return {
        "strategy": key, "mode": "paper", "paper_starting_balance_sol": 1.0,
        "db_path": f"memebot-{key}.sqlite", "log_path": f"memebot-{key}.log",
        "scan_interval_sec": 45, "position_size_sol": 0.25, "max_positions": 3,
        "max_entries_per_cycle": 1, "max_deep_checks_per_cycle": 5, "reentry_cooldown_min": 240,
        "slippage_bps": 500, "paper_fee_lamports": 150000, "api_pause_sec": 0.4,
        "filters": {"min_liquidity_usd": f["min_liquidity_usd"], "max_liquidity_usd": f["max_liquidity_usd"],
                    "min_age_min": f["min_age_min"], "max_age_min": f["max_age_min"],
                    "max_fdv_to_liquidity": 100, "min_vol_m5_usd": f["min_vol_m5_usd"],
                    "min_sells_m5": 1, "max_buy_sell_ratio": 20},
        "safety": {"require_rugcheck": True, "max_rugcheck_score": 40, "max_roundtrip_loss_pct": 15},
        "smart_money": {"enabled": True, "shadow": True, "require_data": False, "min_top_traders": 6,
                        "max_top1_volume_share": 45, "min_buy_share": 35, "max_buy_share": 85, "max_bundlers": 3},
        "insiders": {"enabled": True, "shadow": True, "try_gmgn": True, "require_data": False,
                     "max_insider_pct": 20, "max_sniper_pct": 25, "max_top10_pct": 50,
                     "max_creator_pct": 15, "min_holders": 50},
        "narrative": {"keywords": [], "require": False},
        "entry": {"min_chg_m5_pct": -50.0, "max_chg_m5_pct": 500.0, "min_chg_h1_pct": -80.0,
                  "min_buy_sell_edge": 1.0},
        "exits": ex, "risk": {"daily_loss_limit_sol": 0.75},
        "human_filter": {"enabled": True, "shadow": False, "min_class": "ORGANIC"},
        "entry_screen": {"enabled": True, "family": spec["family"], **spec["params"]},
        "websocket": {"enabled": True, "discovery": True, "apply_setup_filter": False,
                      "backfill_rest": True, "max_backfills_per_cycle": 4, "max_promotions_per_cycle": 10,
                      "max_price_subs": 95, "min_listing_liquidity_usd": 500, "min_cum_vol_usd": 0,
                      "persist_state": True},
    }


def main():
    print("loading universe ...")
    universe = load_universe()
    print(f"  {len(universe)} tokens with candles\n")

    out = {"cost_pct": COST_PCT, "density": DENSITY, "strategies": {}}
    for key, spec in STRATS.items():
        trades, cards = backtest_strategy(universe, spec)
        s = stats(trades)
        out["strategies"][key] = {
            "label": spec["label"], "family": spec["family"], "entry_age": spec["entry_age"],
            "exits": spec["exits"], "stats": s,
            "multiples": [round(t["mult"], 6) for t in trades],
            "card_trades": cards[:400],
        }
        ex = spec["exits"]
        print(f"=== {spec['label']} ({spec['family']}): stop -{ex['stop_loss_pct']}% · "
              f"TP {ex['take_profits'][0]['multiple']}x bank {int(ex['take_profits'][0]['sell_fraction_of_remaining']*100)}% · "
              f"trail -{ex['trailing_stop_pct']}% · hard {ex['hard_tp_multiple']}x · hold {ex['max_hold_min']}m ===")
        if not s.get("n"):
            print("  no trades\n"); continue
        print(f"  n={s['n']} · ~{s['live_wk_est']}/wk live · WR {s['wr']}% · avg {s['avg']}x · "
              f"exp {s['exp_pct']:+}%/trade · med hold {s['median_hold_min']:.0f}m · "
              f"2x {s['twox']} · 5x {s['fivex']} · best {s['best']}x\n")

    with open(os.path.join("reports", "newstrat_backtest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print("wrote reports/newstrat_backtest.json")

    for key, spec in STRATS.items():
        path = f"config.{key}.json"
        if os.path.exists(path):
            print(f"  {path} exists - left as-is")
            continue
        with open(path, "w", encoding="utf-8") as f:
            json.dump(make_config(key, spec), f, indent=2)
        print(f"  wrote {path}")


if __name__ == "__main__":
    main()
