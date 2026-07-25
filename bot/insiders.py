from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from .config import Config
from .util import fnum

log = logging.getLogger("memebot.insiders")

# GMGN has no official API; these internal endpoints sit behind Cloudflare bot
# management and reject non-browser clients (403). We try them opportunistically
# with browser-like headers and stop after repeated failures. The real workhorse
# is RugCheck's full report, which carries per-holder insider flags from their
# wallet-graph analysis, the creator wallet, and AMM/locker account tagging.
GMGN_URLS = (
    "https://gmgn.ai/defi/quotation/v1/tokens/sol/{mint}",
    "https://gmgn.ai/api/v1/token_security_sol/sol/{mint}",
)
GMGN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                  " (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://gmgn.ai/",
}
RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

_gmgn_failures = 0
_GMGN_GIVE_UP_AFTER = 3


@dataclass
class InsiderReport:
    source: str            # "gmgn" or "rugcheck-graph"
    insider_pct: float     # % of supply held by insider-flagged wallets (top-20 holders)
    sniper_pct: float      # % held by sniper wallets; -1 when the source can't tell
    top10_pct: float       # % held by top-10 holders, AMM/locker accounts excluded
    top1_pct: float        # % held by the largest single such holder; -1 if unknown
    creator_pct: float     # % still held by the deployer wallet
    insider_networks: int  # distinct insider wallet-networks detected
    total_holders: Optional[int]
    rugged: bool


def _from_gmgn(session, mint: str) -> Optional[InsiderReport]:
    global _gmgn_failures
    if _gmgn_failures >= _GMGN_GIVE_UP_AFTER:
        return None
    for url in GMGN_URLS:
        try:
            r = session.get(url.format(mint=mint), headers=GMGN_HEADERS, timeout=8)
            if r.status_code != 200:
                continue
            data = (r.json() or {}).get("data") or {}
            tok = data.get("token") or data
            top10 = fnum(tok.get("top_10_holder_rate"), -1.0)
            holders = tok.get("holder_count")
            if top10 < 0 or holders is None:
                continue
            insider = fnum(tok.get("insider_percentage", tok.get("insider_rate")), -1.0)
            sniper = fnum(tok.get("sniper_percentage", tok.get("sniper_rate")), -1.0)
            creator = fnum(tok.get("creator_percentage", tok.get("creator_token_rate")), -1.0)

            def pct(v: float) -> float:
                return v * 100 if 0 <= v <= 1 else v

            _gmgn_failures = 0
            return InsiderReport(
                source="gmgn",
                insider_pct=pct(insider) if insider >= 0 else -1.0,
                sniper_pct=pct(sniper) if sniper >= 0 else -1.0,
                top10_pct=pct(top10),
                # GMGN publishes no per-holder breakdown, so the largest single
                # holder is unknown here; -1 makes the gate skip that leg.
                top1_pct=-1.0,
                creator_pct=pct(creator) if creator >= 0 else -1.0,
                insider_networks=0,
                total_holders=int(holders),
                rugged=False,
            )
        except Exception as exc:
            log.debug("gmgn probe %s failed: %s", url, exc)
    _gmgn_failures += 1
    if _gmgn_failures == _GMGN_GIVE_UP_AFTER:
        log.info("GMGN unreachable %d times (Cloudflare-gated); using RugCheck insider graph only",
                 _GMGN_GIVE_UP_AFTER)
    return None


def _from_rugcheck(session, mint: str) -> Optional[InsiderReport]:
    """Insider metrics from RugCheck's wallet-graph report.

    insider_pct is computed over the top-20 holders RugCheck returns, so it
    understates a widely-spread insider network - but concentrated insider
    holdings (the kind that dumps on you) show up exactly there.
    """
    try:
        r = session.get(RUGCHECK_REPORT_URL.format(mint=mint), timeout=15)
        if r.status_code != 200:
            return None
        d = r.json() or {}
    except Exception as exc:
        log.debug("rugcheck report for %s failed: %s", mint, exc)
        return None

    known = d.get("knownAccounts") or {}
    non_market = {
        addr for addr, meta in known.items()
        if str((meta or {}).get("type", "")).upper() in ("AMM", "LOCKER")
    }
    holders = [
        h for h in (d.get("topHolders") or [])
        if h.get("address") not in non_market and h.get("owner") not in non_market
    ]
    creator = d.get("creator")
    insider_pct = sum(fnum(h.get("pct")) for h in holders if h.get("insider"))
    ranked = sorted(holders, key=lambda h: fnum(h.get("pct")), reverse=True)
    top10_pct = sum(fnum(h.get("pct")) for h in ranked[:10])
    top1_pct = fnum(ranked[0].get("pct")) if ranked else -1.0
    creator_pct = sum(
        fnum(h.get("pct")) for h in holders
        if creator and (h.get("owner") == creator or h.get("address") == creator)
    )
    networks = d.get("insiderNetworks")
    return InsiderReport(
        source="rugcheck-graph",
        insider_pct=insider_pct,
        sniper_pct=-1.0,
        top10_pct=top10_pct,
        top1_pct=top1_pct,
        creator_pct=creator_pct,
        insider_networks=len(networks) if isinstance(networks, list) else 0,
        total_holders=d.get("totalHolders"),
        rugged=bool(d.get("rugged")),
    )


def check(session, mint: str, cfg: Config) -> Optional[InsiderReport]:
    rep = _from_gmgn(session, mint) if cfg.try_gmgn else None
    if rep is None:
        rep = _from_rugcheck(session, mint)
    return rep


def verdict(rep: Optional[InsiderReport], cfg: Config) -> Tuple[bool, str]:
    if rep is None:
        if cfg.insiders_require_data:
            return False, "no wallet/insider data (GMGN gated, RugCheck report failed); rejecting"
        return True, "no wallet/insider data; allowed by config"
    if rep.rugged:
        return False, "token flagged as RUGGED"
    if rep.insider_pct >= 0 and rep.insider_pct > cfg.max_insider_pct:
        return False, f"insider wallets hold {rep.insider_pct:.1f}% > max {cfg.max_insider_pct:.0f}%"
    if rep.sniper_pct >= 0 and rep.sniper_pct > cfg.max_sniper_pct:
        return False, f"sniper wallets hold {rep.sniper_pct:.1f}% > max {cfg.max_sniper_pct:.0f}%"
    if rep.top10_pct > cfg.max_top10_pct:
        return False, f"top-10 holders control {rep.top10_pct:.1f}% > max {cfg.max_top10_pct:.0f}%"
    if rep.top1_pct >= 0 and rep.top1_pct > cfg.max_top1_pct:
        return False, f"largest holder controls {rep.top1_pct:.1f}% > max {cfg.max_top1_pct:.0f}%"
    if rep.creator_pct >= 0 and rep.creator_pct > cfg.max_creator_pct:
        return False, f"creator still holds {rep.creator_pct:.1f}% > max {cfg.max_creator_pct:.0f}%"
    if rep.total_holders is not None and rep.total_holders < cfg.min_holders:
        return False, f"only {rep.total_holders} holders < min {cfg.min_holders}"
    nets = f", {rep.insider_networks} insider nets" if rep.insider_networks else ""
    top1 = f", top1 {rep.top1_pct:.1f}%" if rep.top1_pct >= 0 else ""
    holders = f", {rep.total_holders} holders" if rep.total_holders is not None else ""
    return True, (
        f"wallets ok: insiders {rep.insider_pct:.1f}%, top10 {rep.top10_pct:.1f}%{top1}"
        f"{holders}, creator {rep.creator_pct:.1f}%{nets} ({rep.source})"
    )
