from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from . import wallets
from .config import Config
from .util import fnum

log = logging.getLogger("memebot.smartmoney")

BASE = "https://public-api.birdeye.so"
_cache = {}  # mint -> (ts, report-or-None)
_TTL = 600  # Birdeye free tier is rate/credit-limited; don't re-ask for 10 min


@dataclass
class SmartMoneyReport:
    traders: int      # active top traders in the last 24h (max 10 requested)
    top1_share: float  # % of top-trader USD volume from the single biggest wallet
    buy_share: float   # % of top-trader USD volume that is buys
    bundlers: int      # wallets Birdeye tags as bundle snipers
    fresh: int         # brand-new wallets among the top traders


def api_key() -> str:
    return os.environ.get("BIRDEYE_API_KEY", "").strip()


def check(session, mint: str, cfg: Config,
          symbol: Optional[str] = None) -> Optional[SmartMoneyReport]:
    """Top-trader profile for a token from Birdeye. Uses USD volumes only -
    the raw `volume` fields are in token units and useless for comparison."""
    if not api_key():
        return None
    hit = _cache.get(mint)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    rep = None
    try:
        r = session.get(
            f"{BASE}/defi/v2/tokens/top_traders",
            params={"address": mint, "time_frame": "24h", "sort_type": "desc",
                    "sort_by": "volume", "offset": 0, "limit": 10},
            headers={"X-API-KEY": api_key(), "x-chain": "solana"},
            timeout=12,
        )
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}")
        items = ((r.json().get("data") or {}).get("items")) or []
        wallets.record_sighting(mint, items, symbol=symbol)
        vols = [fnum(i.get("volumeUsd")) for i in items]
        total = sum(vols)
        buys = sum(fnum(i.get("volumeBuyUSD")) for i in items)
        tags = [set(i.get("tags") or []) for i in items]
        rep = SmartMoneyReport(
            traders=len(items),
            top1_share=100 * max(vols) / total if total > 0 and vols else 0.0,
            buy_share=100 * buys / total if total > 0 else 0.0,
            bundlers=sum(1 for t in tags if "bundler" in t),
            fresh=sum(1 for t in tags if "fresh" in t),
        )
    except Exception as exc:
        log.debug("birdeye top_traders failed for %s: %s", mint, exc)
    _cache[mint] = (time.time(), rep)
    return rep


def verdict(rep: Optional[SmartMoneyReport], cfg: Config) -> Tuple[bool, str]:
    if rep is None:
        if cfg.smart_money_require:
            return False, "no top-trader data (Birdeye unavailable); rejecting"
        return True, "no top-trader data; allowed by config"
    if rep.traders < cfg.min_top_traders:
        return False, f"only {rep.traders} active top traders in 24h (thin, manipulable)"
    if rep.top1_share > cfg.max_top1_volume_share:
        return False, f"one wallet is {rep.top1_share:.0f}% of top-trader volume (wash-trade risk)"
    if not (cfg.min_buy_share <= rep.buy_share <= cfg.max_buy_share):
        side = "dumping" if rep.buy_share < cfg.min_buy_share else "one-sided pump"
        return False, (f"top-trader flow {rep.buy_share:.0f}% buys - outside"
                       f" {cfg.min_buy_share:.0f}-{cfg.max_buy_share:.0f}% band ({side})")
    if rep.bundlers > cfg.max_bundlers:
        return False, f"{rep.bundlers} bundle-sniper wallets among top traders"
    return True, (f"smart money ok: {rep.traders} traders, top1 {rep.top1_share:.0f}%,"
                  f" {rep.buy_share:.0f}% buys, {rep.bundlers} bundler/{rep.fresh} fresh")
