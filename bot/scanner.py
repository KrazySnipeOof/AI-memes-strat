from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from .config import Config, EXCLUDED_MINTS
from .util import age_minutes, fnum

log = logging.getLogger("memebot.scanner")

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
POOL_SOURCES = ("new_pools", "trending_pools")


@dataclass
class Candidate:
    mint: str
    pool: str
    symbol: str
    price_usd: float
    liquidity_usd: float
    fdv_usd: float
    age_min: Optional[float]
    vol_m5: float
    vol_h1: float
    buys_m5: int
    sells_m5: int
    chg_m5: float
    chg_h1: float
    source: str


def _parse_pool(item: dict, source: str) -> Optional[Candidate]:
    try:
        a = item.get("attributes") or {}
        rel = item.get("relationships") or {}
        base_id = (((rel.get("base_token") or {}).get("data")) or {}).get("id", "")
        quote_id = (((rel.get("quote_token") or {}).get("data")) or {}).get("id", "")
        base_mint = base_id.split("_", 1)[-1]
        quote_mint = quote_id.split("_", 1)[-1]

        name = a.get("name") or ""
        parts = [p.strip() for p in name.split("/")]
        base_sym = parts[0] if parts and parts[0] else "?"
        quote_sym = parts[1] if len(parts) > 1 and parts[1] else "?"

        # The meme leg is usually the base token, but some pools list it as quote.
        if base_mint and base_mint not in EXCLUDED_MINTS:
            mint, symbol, price = base_mint, base_sym, fnum(a.get("base_token_price_usd"))
        elif quote_mint and quote_mint not in EXCLUDED_MINTS:
            mint, symbol, price = quote_mint, quote_sym, fnum(a.get("quote_token_price_usd"))
        else:
            return None

        tx = (a.get("transactions") or {}).get("m5") or {}
        vol = a.get("volume_usd") or {}
        chg = a.get("price_change_percentage") or {}

        return Candidate(
            mint=mint,
            pool=a.get("address") or "",
            symbol=symbol,
            price_usd=price,
            liquidity_usd=fnum(a.get("reserve_in_usd")),
            fdv_usd=fnum(a.get("fdv_usd")),
            age_min=age_minutes(a.get("pool_created_at")),
            vol_m5=fnum(vol.get("m5")),
            vol_h1=fnum(vol.get("h1")),
            buys_m5=int(fnum(tx.get("buys"))),
            sells_m5=int(fnum(tx.get("sells"))),
            chg_m5=fnum(chg.get("m5")),
            chg_h1=fnum(chg.get("h1")),
            source=source,
        )
    except Exception:
        log.exception("failed to parse pool item")
        return None


def discover(session, cfg: Config) -> List[Candidate]:
    found = {}
    for source in POOL_SOURCES:
        try:
            r = session.get(f"{GECKO_BASE}/networks/solana/{source}", params={"page": 1}, timeout=20)
            r.raise_for_status()
            items = (r.json() or {}).get("data") or []
        except Exception as exc:
            log.warning("discovery source %s failed: %s", source, exc)
            continue
        for item in items:
            cand = _parse_pool(item, source)
            if cand and cand.mint not in found:
                found[cand.mint] = cand
    cands = sorted(found.values(), key=lambda c: c.vol_m5, reverse=True)
    log.info("discovery: %d unique candidates", len(cands))
    return cands
