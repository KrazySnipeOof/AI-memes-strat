from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from . import jupiter
from .config import Config
from .scanner import Candidate
from .util import fnum

log = logging.getLogger("memebot.safety")

RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"


def basic_filter(c: Candidate, cfg: Config) -> Tuple[bool, str]:
    if c.age_min is None:
        return False, "unknown pool age"
    if c.age_min < cfg.min_age_min:
        return False, f"too new ({c.age_min:.0f}m)"
    if c.age_min > cfg.max_age_min:
        return False, f"too old ({c.age_min:.0f}m)"
    if c.liquidity_usd < cfg.min_liquidity_usd:
        return False, f"liquidity ${c.liquidity_usd:,.0f} below min"
    if c.liquidity_usd > cfg.max_liquidity_usd:
        return False, f"liquidity ${c.liquidity_usd:,.0f} above max"
    if c.fdv_usd and c.liquidity_usd and c.fdv_usd / c.liquidity_usd > cfg.max_fdv_to_liquidity:
        return False, f"fdv/liquidity {c.fdv_usd / c.liquidity_usd:.0f}x too high"
    if c.vol_m5 < cfg.min_vol_m5_usd:
        return False, f"5m volume ${c.vol_m5:,.0f} below min"
    if c.sells_m5 < cfg.min_sells_m5:
        return False, f"only {c.sells_m5} sells in 5m (honeypot risk)"
    if c.sells_m5 > 0 and c.buys_m5 / c.sells_m5 > cfg.max_buy_sell_ratio:
        return False, f"buy/sell ratio {c.buys_m5}/{c.sells_m5} suspicious"
    return True, "ok"


@dataclass
class RugcheckResult:
    ok: bool
    reason: str
    score: Optional[float] = None


def rugcheck(session, mint: str, cfg: Config) -> RugcheckResult:
    try:
        r = session.get(RUGCHECK_URL.format(mint=mint), timeout=12)
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}")
        data = r.json() or {}
    except Exception as exc:
        if cfg.require_rugcheck:
            return RugcheckResult(False, f"rugcheck unavailable ({exc}); rejecting")
        return RugcheckResult(True, f"rugcheck unavailable ({exc}); allowed by config")

    dangers = [
        str(risk.get("name"))
        for risk in (data.get("risks") or [])
        if str(risk.get("level", "")).lower() == "danger"
    ]
    if dangers:
        return RugcheckResult(False, "rugcheck danger flags: " + ", ".join(dangers[:4]))

    score = data.get("score_normalised")
    if score is None:
        score = data.get("score")
    score = fnum(score, default=-1.0)
    if score < 0:
        if cfg.require_rugcheck:
            return RugcheckResult(False, "rugcheck returned no score; rejecting")
        return RugcheckResult(True, "rugcheck no score; allowed by config")
    if score > cfg.max_rugcheck_score:
        return RugcheckResult(False, f"rugcheck score {score:.0f} > max {cfg.max_rugcheck_score:.0f}", score)

    lp = fnum(data.get("lpLockedPct"), default=-1.0)
    extra = f", LP locked {lp:.0f}%" if lp >= 0 else ""
    return RugcheckResult(True, f"rugcheck score {score:.0f}{extra}", score)


def roundtrip_check(session, mint: str, lamports: int, cfg: Config) -> Tuple[bool, str]:
    """Quote a buy and an immediate sell of the same size. Catches tokens Jupiter
    cannot route, honeypots that cannot be sold, and pools too thin for our size."""
    buy = jupiter.get_quote(session, jupiter.SOL_MINT, mint, lamports, cfg.slippage_bps)
    if not buy:
        return False, "no buy route on Jupiter"
    tokens = int(buy["outAmount"])
    if tokens <= 0:
        return False, "zero out-amount on buy quote"
    sell = jupiter.get_quote(session, mint, jupiter.SOL_MINT, tokens, cfg.slippage_bps)
    if not sell:
        return False, "no sell route on Jupiter (honeypot risk)"
    back = int(sell["outAmount"])
    loss_pct = (1 - back / lamports) * 100
    if loss_pct > cfg.max_roundtrip_loss_pct:
        return False, f"round-trip loss {loss_pct:.1f}% > {cfg.max_roundtrip_loss_pct:.0f}%"
    return True, f"round-trip loss {loss_pct:.1f}%"
