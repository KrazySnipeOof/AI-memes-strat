#!/usr/bin/env python
"""Home-run strategy backtest: enter selectively, RIDE winners to 10x+, cut
losers fast. The opposite of the base strategy (which banks 1.35x and bleeds).

Defining pieces:
  EXIT   no early take-profit. A 0%-sell TP at `arm_x` only ARMS the trailing
         stop (backtest.simulate gates the trail on stage>0), so below arm_x the
         position rides on the hard stop and above it rides a wide trail; a 10x
         hard-take-profit banks the home run. Two exit variants tested.
  ENTRY  moonshot screen decidable at entry: low market cap (room to 10x),
         real volume, positive momentum, and (optional) ORGANIC per bot.manip.
         Tightened until the implied rate is ~1-3 trades/week.

Because a memecoin's 10x is unknowable at entry, the strategy is asymmetric:
most trades are small stop-outs, a few are 10x. The backtest measures whether
the rare home runs outweigh the many small losses, and how selective the entry
must be for ~1-3/week.

CAVEAT: the cached samples span only ~6 days, so weekly frequency is an
estimate from the observed pass-rate, not a multi-week measurement.

  python homerun_backtest.py            run + write config.homerun.json + json
"""
from __future__ import annotations

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import backtest as bt
from bot import manip

SUPPLY = 1e9          # pump.fun launch supply, for the market-cap screen
ENTRY_AGE_MIN = 30.0
MIN_ENTRY_VOL = 2000.0
COST_PCT = 4.0
INDEXES = ("reports/bd_tokens.json", "reports/bd_tokens_60d.json", "reports/bd_tokens_oot.json")
CACHES = (".bd_cache_ext", ".bd_cache")

# Home-run exit doctrines. take_profits with sell_fraction 0 arm the trail
# without selling; the trail then rides the move, hard_tp banks the home run.
HR_EXITS = {
    "bank10": {"stop_loss_pct": 55, "take_profits": [{"multiple": 2.5, "sell_fraction_of_remaining": 0.0}],
               "trailing_stop_pct": 45, "hard_tp_multiple": 10.0, "max_hold_min": 5760},
    "ride30": {"stop_loss_pct": 55, "take_profits": [{"multiple": 2.5, "sell_fraction_of_remaining": 0.0}],
               "trailing_stop_pct": 55, "hard_tp_multiple": 30.0, "max_hold_min": 5760},
}


def load_universe():
    """Deduped tokens across every sample era, each paired with its candles."""
    seen, out = set(), []
    for idx in INDEXES:
        if not os.path.exists(idx):
            continue
        for tok in bt.load_token_index(idx):
            if tok.mint in seen:
                continue
            seen.add(tok.mint)
            candles = None
            for cd in CACHES:
                candles = bt.load_cached_candles(cd, tok.pool)
                if candles:
                    break
            if candles:
                out.append((tok, candles))
    return out


def entry_features(candles):
    """(entry_price, entry_ts, mcap, momentum_15m, organic, theo_peak) or None."""
    created = candles[0][0]
    entry_ts = created + ENTRY_AGE_MIN * 60
    pre = [c for c in candles if c[0] <= entry_ts]
    post = [c for c in candles if c[0] > entry_ts]
    if not pre or not post:
        return None
    entry = pre[-1][4]
    if entry <= 0 or sum(c[5] for c in pre) < MIN_ENTRY_VOL:
        return None
    ref = [c[4] for c in pre if c[0] <= entry_ts - 15 * 60]
    momentum = entry / ref[-1] if ref and ref[-1] > 0 else 1.0
    organic = (manip.classify(pre, False) or {}).get("class") == "ORGANIC"
    theo_peak = max(c[2] for c in post) / entry
    return entry, int(entry_ts), entry * SUPPLY, momentum, organic, theo_peak


def build_trades(universe, exits):
    """All simulated home-run trades (entry at 30m, home-run exit), each carrying
    its entry-time features so filters can be applied afterward."""
    trades = []
    for tok, candles in universe:
        feat = entry_features(candles)
        if feat is None:
            continue
        entry, entry_ts, mcap, mom, organic, theo = feat
        sim = bt.simulate(candles, exits, ENTRY_AGE_MIN, COST_PCT, MIN_ENTRY_VOL, setup=None)
        if not sim:
            continue
        trades.append({"symbol": tok.symbol, "mint": tok.mint, "mult": sim["multiple"],
                       "reason": sim["reason"], "mcap": mcap, "mom": mom,
                       "organic": organic, "theo_peak": theo, "entry_ts": entry_ts})
    return trades


def span_weeks(trades):
    ts = [t["entry_ts"] for t in trades]
    return max(1e-9, (max(ts) - min(ts)) / 86400 / 7) if ts else 1e-9


def summarize(name, trades, all_weeks):
    if not trades:
        return {"name": name, "n": 0}
    m = [t["mult"] for t in trades]
    wins = [x for x in m if x > 1.0]
    tenx = sum(1 for x in m if x >= 9.5)          # realized 10x (hard_tp banks ~10x pre-cost)
    stake = 0.25
    pnl = sum((x - 1) * stake for x in m)
    avail10 = sum(1 for t in trades if t["theo_peak"] >= 10)
    return {"name": name, "n": len(m),
            "per_week": round(len(m) / all_weeks, 2),
            "live_wk_est": round(len(m) / all_weeks / 15, 1),  # ~15x sample-vs-live density
            "wr": round(100 * len(wins) / len(m), 1),
            "avg": round(statistics.mean(m), 3), "median": round(statistics.median(m), 3),
            "tenx": tenx, "fivex": sum(1 for x in m if x >= 5),
            "twox": sum(1 for x in m if x >= 2),
            "exp_pct": round(100 * (statistics.mean(m) - 1), 1),
            "pnl_sol": round(pnl, 3), "best": round(max(m), 2),
            "avail_10x": avail10, "capture_10x": f"{tenx}/{avail10}"}


def main():
    print("loading universe (all sample eras) ...")
    universe = load_universe()
    print(f"  {len(universe)} tokens with candles\n")

    results, all_trades = {}, {}
    for exkey, exits in HR_EXITS.items():
        trades = build_trades(universe, exits)
        all_trades[exkey] = trades
        wk = span_weeks(trades)  # span of the full trade set (~sample span)
        # entry-filter tiers: progressively selective toward 1-3/week
        def screen(mc, mo):
            return [t for t in trades if t["mcap"] <= mc and t["mom"] > mo and t["organic"]]
        tiers = [
            ("ALL (vol only)", trades),
            ("+mcap<=$60k +mom>1.1 +organic", screen(60_000, 1.1)),
            ("+mcap<=$40k +mom>1.2 +organic", screen(40_000, 1.2)),
            ("+mcap<=$25k +mom>1.3 +organic", screen(25_000, 1.3)),
            ("+mcap<=$15k +mom>1.4 +organic (tightest)", screen(15_000, 1.4)),
        ]
        results[exkey] = {"weeks": round(wk, 2),
                          "tiers": [summarize(n, tr, wk) for n, tr in tiers]}

    # raw per-trade multiples for the DEPLOYED variant (ride30 exit + the
    # config's moonshot screen), so the Monte Carlo can resample it.
    variant = [t for t in all_trades["ride30"]
               if t["mcap"] <= 25_000 and t["mom"] > 1.3 and t["organic"]]
    config_variant = {"exit": "ride30", "screen": "mcap<=$25k / mom>1.3 / organic",
                      "n": len(variant), "multiples": [round(t["mult"], 6) for t in variant]}
    with open(os.path.join("reports", "homerun_backtest.json"), "w", encoding="utf-8") as f:
        json.dump({"cost_pct": COST_PCT, "entry_age_min": ENTRY_AGE_MIN,
                   "exits": HR_EXITS, "results": results,
                   "config_variant": config_variant}, f, indent=1)

    for exkey in HR_EXITS:
        r = results[exkey]
        ex = HR_EXITS[exkey]
        print(f"=== EXIT '{exkey}': stop -{ex['stop_loss_pct']}% · ride (arm trail @{ex['take_profits'][0]['multiple']}x, "
              f"trail -{ex['trailing_stop_pct']}%) · hard {ex['hard_tp_multiple']}x · hold {ex['max_hold_min']}m "
              f"· sample {r['weeks']} wk ===")
        print(f"  {'entry filter':<44}{'n':>5}{'~lv/wk':>7}{'WR':>6}{'avg':>7}{'exp%':>7}{'10x':>5}{'5x':>5}{'2x':>5}  cap10x")
        for s in r["tiers"]:
            if not s.get("n"):
                print(f"  {s['name']:<44}{'0':>5}  (no trades)"); continue
            print(f"  {s['name']:<44}{s['n']:>5}{s['live_wk_est']:>7}{s['wr']:>5}%{s['avg']:>7}{s['exp_pct']:>+7}"
                  f"{s['tenx']:>5}{s['fivex']:>5}{s['twox']:>5}  {s['capture_10x']}")
        print()

    # write the strategy config: ride-to-30x exit (captures the 10x floor AND
    # lets monsters run) + the tight moonshot screen (~1-3/week live target)
    ex = HR_EXITS["ride30"]
    cfg = {
        "strategy": "homerun", "mode": "paper", "paper_starting_balance_sol": 1.0,
        "db_path": "memebot-homerun.sqlite", "log_path": "memebot-homerun.log",
        "scan_interval_sec": 45, "position_size_sol": 0.25, "max_positions": 3,
        "max_entries_per_cycle": 1, "max_deep_checks_per_cycle": 5, "reentry_cooldown_min": 240,
        "slippage_bps": 500, "paper_fee_lamports": 150000, "api_pause_sec": 0.4,
        "filters": {"min_liquidity_usd": 3000, "max_liquidity_usd": 150000, "min_age_min": 30,
                    "max_age_min": 720, "max_fdv_to_liquidity": 100, "min_vol_m5_usd": 2000,
                    "min_sells_m5": 1, "max_buy_sell_ratio": 20},
        "safety": {"require_rugcheck": True, "max_rugcheck_score": 40, "max_roundtrip_loss_pct": 15},
        "smart_money": {"enabled": True, "shadow": True, "require_data": False, "min_top_traders": 6,
                        "max_top1_volume_share": 45, "min_buy_share": 35, "max_buy_share": 85, "max_bundlers": 3},
        "insiders": {"enabled": True, "shadow": True, "try_gmgn": True, "require_data": False,
                     "max_insider_pct": 20, "max_sniper_pct": 25, "max_top10_pct": 50,
                     "max_creator_pct": 15, "min_holders": 50},
        "narrative": {"keywords": [], "require": False},
        "entry": {"min_chg_m5_pct": 2.0, "max_chg_m5_pct": 200.0, "min_chg_h1_pct": -10.0,
                  "min_buy_sell_edge": 1.2},
        "exits": ex,
        "risk": {"daily_loss_limit_sol": 0.75},
        "human_filter": {"enabled": True, "shadow": False, "min_class": "ORGANIC"},
        "homerun_screen": {"enabled": True, "supply": 1_000_000_000, "max_entry_mcap_usd": 25000,
                           "min_momentum_15m": 1.3,
                           "note": "moonshot screen: low mcap = room to 10x; enforced live via the bot gate"},
        "websocket": {"enabled": True, "discovery": True, "apply_setup_filter": False,
                      "backfill_rest": True, "max_backfills_per_cycle": 4, "max_promotions_per_cycle": 10,
                      "max_price_subs": 95, "min_listing_liquidity_usd": 500, "min_cum_vol_usd": 2000,
                      "persist_state": True},
    }
    # don't clobber an existing (possibly hand-tuned + live-loaded) config
    if not os.path.exists("config.homerun.json"):
        with open("config.homerun.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print("wrote config.homerun.json (ride-to-30x exit + moonshot screen)")
    else:
        print("config.homerun.json exists - left as-is (delete it to regenerate)")
    print("wrote reports/homerun_backtest.json")


if __name__ == "__main__":
    main()
