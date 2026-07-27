#!/usr/bin/env python
"""Copy entry: enter when a screened "grinder" wallet buys.

The GRINDER archetype (measured 2026-07-26 over 34 scouted wallets / 499 real
realized round trips): win rate >= 60% (median 77%), >= 10 completed trips, and
a median hold >= 45 min. Those wallets out-earned the low-win-rate tail-chasers
about 2:1, and — critically — they are the COPYABLE ones. The flashy snipers
realize their edge inside the launch candle, below 1-minute resolution, so a
follower cannot reconstruct their fill; the grinders hold long enough that a
<=5 minute follow lag still lands in the same move.

This module polls each screened wallet's swap stream and reports the mints they
have just bought. It does NOT decide to trade - main.py runs every candidate
through the same safety/strategy gates as any other source. The wallet buy is a
DISCOVERY signal, replacing the 30-minute funnel that would never surface these
tokens in time.

Cost: one /trader/txs/seek_by_time request per wallet per poll. At the defaults
(6 wallets, 150s) that is ~3.5k requests/day, ~104k CU/day, ~15% of the 20M
plan. Both knobs are in config under wallet_tracker.copy_entry.

IMPORTANT: the pool this strategy was specified from is wallet-SELECTION biased
(the wallets were picked because they had already won). Paper trading forward is
the only test of whether the signal is real - which is the point of running it.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
from typing import List, Optional

import bdusage
from bot import scanner
from bot.config import EXCLUDED_MINTS
from bot.util import fnum

log = logging.getLogger("memebot")

BIRDEYE = "https://public-api.birdeye.so"
SEEK = "/trader/txs/seek_by_time"


class CopyFeed:
    """Screened grinder wallets -> the mints they just bought."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.api_key = os.environ.get("BIRDEYE_API_KEY", "").strip()
        if not self.api_key:
            log.error("copy: BIRDEYE_API_KEY missing - copy entry will find nothing")
        self.wallets: List[dict] = []
        self.seen: set = set()          # (wallet, mint) already acted on
        self.last_poll: float = 0.0
        self.state_path = cfg.copy_state_path or os.path.join(
            "reports", f"copy_state-{getattr(cfg, 'strategy', 'grinder') or 'grinder'}.json")
        self._load_state()
        self._screen()

    # ---------------------------------------------------------------- screen

    def _screen(self) -> None:
        """Pick wallets by the grinder profile. Median hold is the load-bearing
        filter: it is what separates a copyable wallet from a sniper whose edge
        lives below candle resolution."""
        try:
            with open(self.cfg.copy_scout_path, "r", encoding="utf-8") as f:
                scout = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning("copy: cannot read %s (%s) - no wallets screened",
                        self.cfg.copy_scout_path, exc)
            return
        picked = []
        for w in scout.get("wallets") or []:
            trips = w.get("trips") or []
            if w.get("win_rate", 0) < self.cfg.copy_min_win_rate:
                continue
            if w.get("round_trips", 0) < self.cfg.copy_min_round_trips:
                continue
            holds = [(t["last_sell_ts"] - t["first_buy_ts"]) / 60.0 for t in trips
                     if t.get("last_sell_ts") and t.get("first_buy_ts")
                     and t["last_sell_ts"] > t["first_buy_ts"]]
            med_hold = statistics.median(holds) if holds else 0.0
            if med_hold < self.cfg.copy_min_median_hold_min:
                continue
            picked.append({"wallet": w["wallet"], "win_rate": w.get("win_rate", 0),
                           "trips": w.get("round_trips", 0), "median_hold_min": round(med_hold, 1)})
        picked.sort(key=lambda x: (-x["win_rate"], -x["trips"]))
        self.wallets = picked[: self.cfg.copy_max_wallets]
        if self.wallets:
            log.info("copy: %d grinder wallets screened (of %d scouted): %s",
                     len(self.wallets), len(scout.get("wallets") or []),
                     ", ".join(f"{w['wallet'][:8]}(WR{w['win_rate']:.0f}%/"
                               f"{w['trips']}t/{w['median_hold_min']:.0f}m)" for w in self.wallets))
        else:
            log.warning("copy: NO wallets passed the grinder screen "
                        "(WR>=%.0f%%, trips>=%d, medHold>=%.0fm) - copy entry will idle",
                        self.cfg.copy_min_win_rate, self.cfg.copy_min_round_trips,
                        self.cfg.copy_min_median_hold_min)

    # ----------------------------------------------------------------- state

    def _load_state(self) -> None:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                self.seen = {tuple(x) for x in (json.load(f) or {}).get("seen") or []}
        except (OSError, ValueError):
            self.seen = set()

    def _save_state(self) -> None:
        """Bounded + atomic: a restart must not re-fire buys already acted on."""
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            keep = list(self.seen)[-4000:]
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"seen": keep, "saved": int(time.time())}, f)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.debug("copy: state save failed: %s", exc)

    # ------------------------------------------------------------------ poll

    def _recent_buys(self, session, wallet: str) -> List[dict]:
        """Buy legs from this wallet's newest swaps. `type_swap: to` means the
        wallet received that token."""
        try:
            bdusage.record(SEEK)
            r = session.get(f"{BIRDEYE}{SEEK}",
                            params={"address": wallet, "tx_type": "swap",
                                    "offset": 0, "limit": 20},
                            headers={"X-API-KEY": self.api_key, "x-chain": "solana"},
                            timeout=20)
            if r.status_code != 200:
                log.warning("copy: seek_by_time %s -> http %s", wallet[:8], r.status_code)
                return []
            items = ((r.json() or {}).get("data") or {}).get("items") or []
        except Exception as exc:
            log.debug("copy: seek_by_time %s failed: %s", wallet[:8], exc)
            return []
        cutoff = time.time() - self.cfg.copy_max_follow_lag_min * 60
        out = []
        for s in items:
            ts = int(fnum(s.get("block_unix_time")))
            if ts < cutoff:
                continue
            for leg in (s.get("base") or {}, s.get("quote") or {}):
                mint = leg.get("address")
                if not mint or mint in EXCLUDED_MINTS:
                    continue
                if leg.get("type_swap") != "to":
                    continue
                out.append({"mint": mint, "symbol": leg.get("symbol") or "?",
                            "ts": ts, "wallet": wallet,
                            "usd": fnum(s.get("volume_usd"))})
        return out

    def candidates(self, session, known: Optional[set] = None) -> List:
        """Candidates for mints a screened wallet just bought. Rate-limited by
        copy_poll_interval_sec so the scan cycle can run faster than the poll."""
        if not self.cfg.copy_entry_enabled or not self.wallets:
            return []
        now = time.time()
        if now - self.last_poll < self.cfg.copy_poll_interval_sec:
            return []
        self.last_poll = now
        known = known or set()

        fresh, fired = [], False
        for w in self.wallets:
            for buy in self._recent_buys(session, w["wallet"]):
                key = (w["wallet"], buy["mint"])
                if key in self.seen:
                    continue
                self.seen.add(key)
                fired = True
                if buy["mint"] in known:
                    continue
                fresh.append(buy)
        if fired:
            self._save_state()
        if not fresh:
            return []

        out = []
        for buy in fresh:
            lag = (now - buy["ts"]) / 60.0
            cand = scanner.lookup_token(session, buy["mint"], source="copy")
            if cand is None:
                log.info("copy: %-12s no pool data yet (wallet %s, %.1fm ago)",
                         buy["symbol"], buy["wallet"][:8], lag)
                continue
            log.info("copy: %-12s bought by %s $%.0f, %.1fm lag, age %.1fm - promoting",
                     cand.symbol, buy["wallet"][:8], buy["usd"], lag,
                     cand.age_min if cand.age_min is not None else -1)
            out.append(cand)
        return out
