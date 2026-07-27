from __future__ import annotations

"""Top-trader wallet harvester + tracked-wallet shadow annotations.

The smart-money gate already pays Birdeye for tokens/top_traders on every
deep-checked candidate; this module keeps the wallet addresses that call
returns instead of discarding them (sightings ledger, wallets.sqlite) and
flags candidates whose top traders include a wallet from
reports/tracked_wallets.json.

Observational only: nothing here may influence an entry/exit decision, so
every public entry point swallows its own exceptions - a tracker failure
must never cost the trial a cycle.

Birdeye item field names verified against a live response 2026-07-24:
owner, volumeUsd, volumeBuyUSD, trade, tags, totalPnl, realizedPnl,
firstTradeUnixTime.
"""

import json
import logging
import os
import sqlite3
import threading
from typing import List, Optional

from .util import fnum, iso_now

log = logging.getLogger("memebot.wallets")

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_db_path = "wallets.sqlite"
_tracked_path = os.path.join("reports", "tracked_wallets.json")
_tracked = {}  # address -> display name
_tracked_mtime: float = -1.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    wallet TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    rank INTEGER,
    volume_usd REAL,
    buy_usd REAL,
    trades INTEGER,
    total_pnl REAL,
    realized_pnl REAL,
    first_trade_ts INTEGER,
    tags TEXT
);
CREATE INDEX IF NOT EXISTS idx_sightings_wallet ON sightings(wallet);
CREATE INDEX IF NOT EXISTS idx_sightings_mint ON sightings(mint);
"""


def init(cfg) -> None:
    """Open the sightings db. Called once from Bot.__init__; a failure here
    disables the tracker for the run but never raises."""
    global _conn, _db_path, _tracked_path
    if not getattr(cfg, "wt_enabled", False):
        return
    try:
        _db_path = cfg.wt_db_path
        _tracked_path = cfg.wt_tracked_path
        _conn = sqlite3.connect(_db_path, check_same_thread=False)
        _conn.executescript(SCHEMA)
        _conn.commit()
        _load_tracked()
        log.info("wallet harvester on: db=%s, %d tracked wallets (%s)",
                 _db_path, len(_tracked), _tracked_path)
    except Exception as exc:
        log.warning("wallet harvester init failed (disabled): %s", exc)
        _conn = None


def _load_tracked() -> None:
    """Reload reports/tracked_wallets.json when its mtime changes, so Kenny
    can edit the watchlist without a bot restart."""
    global _tracked, _tracked_mtime
    try:
        mtime = os.path.getmtime(_tracked_path)
    except OSError:
        _tracked, _tracked_mtime = {}, -1.0
        return
    if mtime == _tracked_mtime:
        return
    try:
        with open(_tracked_path, "r", encoding="utf-8") as fh:
            rows = (json.load(fh) or {}).get("wallets", [])
        _tracked = {
            str(r.get("wallet", "")).strip(): str(r.get("name") or "?").strip()
            for r in rows if str(r.get("wallet", "")).strip()
        }
        _tracked_mtime = mtime
        log.info("tracked wallets reloaded: %d entries", len(_tracked))
    except Exception as exc:
        log.warning("tracked_wallets.json unreadable (keeping previous list): %s", exc)


def record_sighting(mint: str, items, symbol: Optional[str] = None) -> None:
    """Persist one top_traders response. Called from smartmoney.check on
    every fresh (non-cached) fetch."""
    if _conn is None or not items:
        return
    try:
        ts = iso_now()
        rows = []
        for rank, it in enumerate(items):
            owner = str(it.get("owner", "")).strip()
            if not owner:
                continue
            rows.append((
                ts, owner, mint, symbol, rank,
                fnum(it.get("volumeUsd")), fnum(it.get("volumeBuyUSD")),
                int(fnum(it.get("trade"))), fnum(it.get("totalPnl")),
                fnum(it.get("realizedPnl")), int(fnum(it.get("firstTradeUnixTime"))),
                ",".join(sorted(it.get("tags") or [])),
            ))
        with _lock:
            _conn.executemany(
                "INSERT INTO sightings (ts, wallet, mint, symbol, rank, volume_usd,"
                " buy_usd, trades, total_pnl, realized_pnl, first_trade_ts, tags)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            _conn.commit()
    except Exception as exc:
        log.debug("record_sighting failed for %s: %s", mint, exc)


def tracked_for_mint(mint: str) -> List[str]:
    """Tracked wallets seen among this mint's top traders in the last 24h,
    as 'name(addr8)' labels. Empty list on any failure."""
    if _conn is None:
        return []
    try:
        _load_tracked()
        if not _tracked:
            return []
        with _lock:
            seen = {r[0] for r in _conn.execute(
                "SELECT DISTINCT wallet FROM sightings"
                " WHERE mint = ? AND ts >= datetime('now', '-1 day')", (mint,))}
        return [f"{_tracked[w]}({w[:8]})" for w in sorted(seen & set(_tracked))]
    except Exception as exc:
        log.debug("tracked_for_mint failed for %s: %s", mint, exc)
        return []


def snapshot(db_path: str, tracked_path: str) -> Optional[dict]:
    """Read-only stats block for the dashboard. Opens its own connection so
    the server never touches the bot's handle. None if no db yet."""
    if not os.path.exists(db_path):
        return None
    try:
        with open(tracked_path, "r", encoding="utf-8") as fh:
            rows = (json.load(fh) or {}).get("wallets", [])
        tracked = {str(r.get("wallet", "")).strip(): str(r.get("name") or "?")
                   for r in rows if str(r.get("wallet", "")).strip()}
    except Exception:
        tracked = {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            n, wallets_n, mints_n = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT wallet), COUNT(DISTINCT mint)"
                " FROM sightings").fetchone()
            top = [
                {"wallet": w, "mints": m, "sightings": s, "last_seen": last,
                 "symbols": syms, "tracked": tracked.get(w)}
                for w, m, s, last, syms in conn.execute(
                    "SELECT wallet, COUNT(DISTINCT mint), COUNT(*), MAX(ts),"
                    " GROUP_CONCAT(DISTINCT symbol) FROM sightings"
                    " GROUP BY wallet ORDER BY COUNT(DISTINCT mint) DESC,"
                    " COUNT(*) DESC LIMIT 8")
            ]
            hits = [
                {"ts": ts, "symbol": sym, "name": tracked.get(w), "wallet": w}
                for ts, sym, w in conn.execute(
                    "SELECT ts, symbol, wallet FROM sightings WHERE wallet IN (%s)"
                    " ORDER BY ts DESC LIMIT 5"
                    % ",".join("?" * len(tracked)), list(tracked))
            ] if tracked else []
        finally:
            conn.close()
        return {"sightings": n, "wallets": wallets_n, "tokens": mints_n,
                "tracked_count": len(tracked), "top": top, "tracked_hits": hits}
    except Exception as exc:
        log.debug("wallet snapshot failed: %s", exc)
        return None
