from __future__ import annotations

from typing import Optional, Tuple

from .config import Config
from .scanner import Candidate


def narrative_match(symbol: str, cfg: Config) -> bool:
    s = (symbol or "").lower()
    return any(k in s for k in cfg.narrative_keywords)


def entry_signal(c: Candidate, cfg: Config) -> Tuple[bool, str]:
    if c.chg_m5 < cfg.min_chg_m5_pct:
        return False, f"5m change {c.chg_m5:+.1f}% below min"
    if c.chg_m5 > cfg.max_chg_m5_pct:
        return False, f"5m change {c.chg_m5:+.1f}% too extended (chasing)"
    if c.chg_h1 < cfg.min_chg_h1_pct:
        return False, f"1h change {c.chg_h1:+.1f}% too negative"
    if c.sells_m5 <= 0 or c.buys_m5 / max(c.sells_m5, 1) < cfg.min_buy_sell_edge:
        return False, f"buy pressure too weak ({c.buys_m5}b/{c.sells_m5}s)"
    return True, f"momentum ok (5m {c.chg_m5:+.1f}%, {c.buys_m5}b/{c.sells_m5}s)"


def exit_action(multiple: float, peak: float, stage: int, age_min: float, cfg: Config) -> Optional[dict]:
    """Decide what to do with an open position.

    `multiple` is total-value multiple: (SOL already banked + SOL the remaining
    tokens would fetch right now) / SOL spent. Using sellable value instead of
    spot price means price impact on our own exit is already accounted for.
    """
    if multiple >= cfg.hard_tp_multiple:
        return {
            "kind": "hard_take_profit",
            "fraction": 1.0,
            "why": f"hit {cfg.hard_tp_multiple:.1f}x hard target at {multiple:.2f}x",
        }
    tps = cfg.take_profits
    if stage < len(tps) and multiple >= float(tps[stage]["multiple"]):
        return {
            "kind": "take_profit",
            "fraction": float(tps[stage]["sell_fraction_of_remaining"]),
            "why": f"take-profit {stage + 1} at {multiple:.2f}x",
        }
    if stage == 0 and multiple <= 1.0 - cfg.stop_loss_pct / 100.0:
        return {"kind": "stop_loss", "fraction": 1.0, "why": f"stop loss at {multiple:.2f}x"}
    if stage > 0 and peak > 1.0 and multiple <= peak * (1.0 - cfg.trailing_stop_pct / 100.0):
        return {
            "kind": "trailing_stop",
            "fraction": 1.0,
            "why": f"trailing stop: {multiple:.2f}x off peak {peak:.2f}x",
        }
    if age_min >= cfg.max_hold_min:
        return {"kind": "time_stop", "fraction": 1.0, "why": f"max hold {cfg.max_hold_min:.0f}m reached"}
    return None
