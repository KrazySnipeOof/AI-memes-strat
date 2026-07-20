from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import List, Optional

from .util import iso_now, parse_iso, utc_now

log = logging.getLogger("memebot.portfolio")

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL,
  mint TEXT NOT NULL,
  symbol TEXT,
  pool TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  sol_spent INTEGER NOT NULL,
  tokens_initial TEXT NOT NULL,
  tokens_raw TEXT NOT NULL,
  sol_received INTEGER NOT NULL DEFAULT 0,
  tp_stage INTEGER NOT NULL DEFAULT 0,
  peak_multiple REAL NOT NULL DEFAULT 1.0,
  quote_failures INTEGER NOT NULL DEFAULT 0,
  exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  side TEXT NOT NULL,
  sol_lamports INTEGER NOT NULL,
  tokens_raw TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
  mint TEXT PRIMARY KEY,
  symbol TEXT,
  pool TEXT,
  source TEXT,
  first_seen TEXT,
  pool_created_at TEXT,
  liquidity_usd REAL,
  fdv_usd REAL
);
"""


@dataclass
class Position:
    id: int
    mode: str
    mint: str
    symbol: str
    pool: str
    status: str
    opened_at: str
    closed_at: Optional[str]
    sol_spent: int
    tokens_initial: int
    tokens_raw: int
    sol_received: int
    tp_stage: int
    peak_multiple: float
    quote_failures: int
    exit_reason: Optional[str]

    @property
    def age_min(self) -> float:
        dt = parse_iso(self.opened_at)
        if dt is None:
            return 0.0
        return (utc_now() - dt).total_seconds() / 60.0


def _to_position(row: sqlite3.Row) -> Position:
    return Position(
        id=row["id"],
        mode=row["mode"],
        mint=row["mint"],
        symbol=row["symbol"] or "?",
        pool=row["pool"] or "",
        status=row["status"],
        opened_at=row["opened_at"],
        closed_at=row["closed_at"],
        sol_spent=row["sol_spent"],
        tokens_initial=int(row["tokens_initial"]),
        tokens_raw=int(row["tokens_raw"]),
        sol_received=row["sol_received"],
        tp_stage=row["tp_stage"],
        peak_multiple=row["peak_multiple"],
        quote_failures=row["quote_failures"],
        exit_reason=row["exit_reason"],
    )


class Portfolio:
    def __init__(self, path: str, mode: str):
        self.mode = mode
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def open_positions(self) -> List[Position]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status='open' AND mode=? ORDER BY id", (self.mode,)
        ).fetchall()
        return [_to_position(r) for r in rows]

    def closed_positions(self, limit: int = 500) -> List[Position]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status='closed' AND mode=? ORDER BY closed_at DESC LIMIT ?",
            (self.mode, limit),
        ).fetchall()
        return [_to_position(r) for r in rows]

    def create_position(self, mint: str, symbol: str, pool: str, sol_spent: int, tokens: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO positions (mode, mint, symbol, pool, opened_at, sol_spent, tokens_initial, tokens_raw)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (self.mode, mint, symbol, pool, iso_now(), int(sol_spent), str(int(tokens)), str(int(tokens))),
        )
        pid = cur.lastrowid
        self._fill(pid, "buy", int(sol_spent), int(tokens), "entry")
        self.conn.commit()
        return pid

    def record_sell(self, pos: Position, tokens_sold: int, lamports_received: int, note: str) -> None:
        remaining = max(0, pos.tokens_raw - int(tokens_sold))
        self.conn.execute(
            "UPDATE positions SET tokens_raw=?, sol_received=sol_received+? WHERE id=?",
            (str(remaining), int(lamports_received), pos.id),
        )
        self._fill(pos.id, "sell", int(lamports_received), int(tokens_sold), note)
        self.conn.commit()
        pos.tokens_raw = remaining
        pos.sol_received += int(lamports_received)

    def close_position(self, pos: Position, reason: str) -> None:
        self.conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, exit_reason=? WHERE id=?",
            (iso_now(), reason, pos.id),
        )
        self.conn.commit()

    def set_progress(self, pos: Position, peak: float, stage: int) -> None:
        self.conn.execute(
            "UPDATE positions SET peak_multiple=?, tp_stage=? WHERE id=?", (peak, stage, pos.id)
        )
        self.conn.commit()

    def bump_quote_failures(self, pos: Position) -> int:
        n = pos.quote_failures + 1
        self.conn.execute("UPDATE positions SET quote_failures=? WHERE id=?", (n, pos.id))
        self.conn.commit()
        pos.quote_failures = n
        return n

    def reset_quote_failures(self, pos: Position) -> None:
        if pos.quote_failures:
            self.conn.execute("UPDATE positions SET quote_failures=0 WHERE id=?", (pos.id,))
            self.conn.commit()
            pos.quote_failures = 0

    def realized_today_lamports(self) -> int:
        today = iso_now()[:10]
        row = self.conn.execute(
            "SELECT COALESCE(SUM(sol_received - sol_spent), 0) AS pnl FROM positions"
            " WHERE status='closed' AND mode=? AND substr(closed_at, 1, 10)=?",
            (self.mode, today),
        ).fetchone()
        return int(row["pnl"])

    def recently_traded(self, mint: str, cooldown_min: float) -> bool:
        row = self.conn.execute(
            "SELECT closed_at FROM positions WHERE mint=? AND mode=? AND closed_at IS NOT NULL"
            " ORDER BY closed_at DESC LIMIT 1",
            (mint, self.mode),
        ).fetchone()
        if not row:
            return False
        dt = parse_iso(row["closed_at"])
        if not dt:
            return False
        return (utc_now() - dt).total_seconds() / 60.0 < cooldown_min

    def record_candidates(self, cands) -> None:
        """Log every discovered candidate so backtest.py accumulates an unbiased
        cohort (the ranking endpoints only ever show survivors)."""
        from datetime import timedelta
        now = utc_now()
        rows = []
        for c in cands:
            created = None
            if c.age_min is not None:
                created = (now - timedelta(minutes=c.age_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append((c.mint, c.symbol, c.pool, c.source, iso_now(), created,
                         c.liquidity_usd, c.fdv_usd))
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO candidates"
                " (mint, symbol, pool, source, first_seen, pool_created_at, liquidity_usd, fdv_usd)"
                " VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            self.conn.commit()

    def _fill(self, position_id: int, side: str, lamports: int, tokens: int, note: str) -> None:
        self.conn.execute(
            "INSERT INTO fills (position_id, ts, side, sol_lamports, tokens_raw, note) VALUES (?,?,?,?,?,?)",
            (position_id, iso_now(), side, lamports, str(tokens), note),
        )
