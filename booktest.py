#!/usr/bin/env python
"""Self-check for bot/books.py - the per-strategy paper books.

Offline and deterministic: synthetic candles, a fake broker, fake quotes and
a temp sqlite. No network, no Birdeye, no Jupiter. Verifies the parts that
decide whether a book's number can be trusted:

  * cash is enforced (a book cannot spend SOL it does not have)
  * the bankroll is per book and isolated from the base ledger
  * the pre-registered bounce rule actually fires on a bounce, and a stale
    trigger does not
  * the exit doctrine matches loop8_eval.sim (pre-arm stop, arm, trail,
    hard, time stop) and the round trip returns cash correctly
  * the holder gate accepts a distributed token and rejects a concentrated
    one, and unknown fields pass rather than reject

Usage: python booktest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot import books
from bot.config import Config, LAMPORTS_PER_SOL
from bot.insiders import InsiderReport
from bot.portfolio import Portfolio
from bot.scanner import Candidate

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{' - ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(name)


def cand(mint="MINT1", symbol="TEST") -> Candidate:
    return Candidate(mint=mint, pool=mint, symbol=symbol, price_usd=1.0,
                     liquidity_usd=50_000, fdv_usd=500_000, age_min=45.0,
                     vol_m5=5_000, vol_h1=40_000, buys_m5=20, sells_m5=10,
                     chg_m5=2.0, chg_h1=5.0, source="test")


def bounce_candles(now: int, trigger_age_sec: int = 60):
    """Nine 1m candles: ramp to 10x, a nuke down, then a credible up-close at
    38% of peak - the P3 bounce pattern (dd 0.55, cv 500, one nuke allowed).
    The trigger is the LAST candle, `trigger_age_sec` old."""
    bars = [
        (1.0, 1.0, 0.9, 1.0, 1000.0),
        (1.0, 2.0, 1.0, 2.0, 1000.0),
        (2.0, 4.0, 2.0, 4.0, 1000.0),
        (4.0, 8.0, 4.0, 8.0, 1000.0),
        (8.0, 10.0, 8.0, 10.0, 1000.0),
        (10.0, 10.0, 3.8, 3.9, 1000.0),   # nuke: body collapse, not credible
        (4.0, 4.1, 3.5, 3.6, 800.0),      # down close - no trigger
        (3.6, 3.8, 3.4, 3.5, 800.0),      # down close - no trigger
        (3.5, 3.9, 3.4, 3.8, 900.0),      # credible up close at 0.38 of peak
    ]
    last_ts = now - trigger_age_sec
    return [[last_ts - 60 * (len(bars) - 1 - i), o, h, l, c, v]
            for i, (o, h, l, c, v) in enumerate(bars)]


class FakeFeed:
    """Enough BirdeyeFeed surface for the books: a candle store, a watchlist
    and a setup gate whose verdict the test controls."""

    def __init__(self, rows, listed_ts):
        self.rows = rows
        self.watch = {"MINT1": {"listed_ts": listed_ts, "symbol": "TEST"}}
        self.backfills = 0
        self.setup_ok = True

    def candle_rows(self, mint):
        return list(self.rows) if mint in self.watch else []

    def backfill(self, session, mint, time_from, time_to):
        self.backfills += 1
        return True

    def setup_gate(self, c, session, budget):
        return (True, "setup ok (fake)") if self.setup_ok else (False, "setup: fake reject")


class FakeBroker:
    """Fills at a fixed token price; sells return tokens * self.price."""

    def __init__(self):
        self.price = 1e-6      # SOL per token
        self.fee = 150_000

    def buy(self, mint, lamports):
        return int(lamports / self.price), lamports + self.fee, "test"

    def sell(self, mint, tokens):
        return max(0, int(tokens * self.price) - self.fee), "test"


def make_cfg(db_path, **books_over):
    raw = {
        "mode": "paper",
        "db_path": db_path,
        "api_pause_sec": 0,
        "position_size_sol": 0.25,
        "max_positions": 3,
        "paper_fee_lamports": 150_000,
        "websocket": {"enabled": True},
        "books": dict({
            "enabled": True,
            "starting_balance_sol": 1.0,
            "position_size_sol": 0.25,
            "max_positions": 3,
            "max_trigger_age_sec": 300,
            "strategies": {"BOUNCE": {"enabled": True}, "V1": {"enabled": False},
                           "H1": {"enabled": True}, "H0": {"enabled": False},
                           "H2": {"enabled": False}},
        }, **books_over),
    }
    return Config(raw)


def main() -> None:
    now = int(time.time())
    tmp = os.path.join(tempfile.mkdtemp(prefix="booktest-"), "books.sqlite")
    cfg = make_cfg(tmp)
    feed = FakeFeed(bounce_candles(now), listed_ts=now - 9 * 60)
    broker = FakeBroker()
    bench = books.BookBench.build(cfg, session=None, broker=broker, feed=feed)
    bounce = next(b for b in bench.books if b.key == "BOUNCE")
    h1 = next(b for b in bench.books if b.key == "H1")

    print("\n== exit doctrine (loop8_eval.sim semantics) ==")
    ex = books.BOUNCE_EX  # stop 50, arm 1.5, trail 25, hard 10, hold 480
    check("pre-arm stop fires at -50%",
          (books.exit_action(0.50, 1.0, False, 10, ex) or {}).get("kind") == "stop_loss")
    check("pre-arm stop quiet at -40%", books.exit_action(0.60, 1.0, False, 10, ex) is None)
    check("armed position ignores the stop",
          books.exit_action(0.60, 2.0, True, 10, ex) is not None
          and books.exit_action(0.60, 2.0, True, 10, ex)["kind"] == "trailing_stop")
    check("trail quiet within 25% of peak", books.exit_action(1.60, 2.0, True, 10, ex) is None)
    check("trail fires 25% off peak",
          (books.exit_action(1.49, 2.0, True, 10, ex) or {}).get("kind") == "trailing_stop")
    check("hard target fires at 10x",
          (books.exit_action(10.0, 10.0, True, 10, ex) or {}).get("kind") == "hard_take_profit")
    check("time stop fires at 480m",
          (books.exit_action(1.0, 1.0, False, 480, ex) or {}).get("kind") == "time_stop")

    print("\n== bounce entry gate ==")
    bench.begin_cycle()
    ok, why = bounce.entry_gate(cand(), bench)
    check("fresh bounce trigger accepted", ok, why)
    stale_feed = FakeFeed(bounce_candles(now, trigger_age_sec=3600), listed_ts=now - 68 * 60)
    stale_bench = books.BookBench.build(cfg, None, broker, stale_feed)
    stale_bench.begin_cycle()
    ok_s, why_s = next(b for b in stale_bench.books if b.key == "BOUNCE").entry_gate(cand(), stale_bench)
    check("stale trigger rejected", not ok_s and "stale" in why_s, why_s)
    flat = [[now - 60 * (9 - i), 1.0, 1.0, 0.99, 1.0, 900.0] for i in range(9)]
    flat_bench = books.BookBench.build(cfg, None, broker, FakeFeed(flat, now - 9 * 60))
    flat_bench.begin_cycle()
    ok_f, why_f = next(b for b in flat_bench.books if b.key == "BOUNCE").entry_gate(cand(), flat_bench)
    check("no bounce, no entry", not ok_f, why_f)

    print("\n== holder gate ==")
    distributed = InsiderReport(source="rugcheck-graph", insider_pct=2.0, sniper_pct=-1.0,
                                top10_pct=31.0, creator_pct=1.0, insider_networks=0,
                                total_holders=180, rugged=False, top1_pct=8.0)
    concentrated = InsiderReport(source="rugcheck-graph", insider_pct=2.0, sniper_pct=-1.0,
                                 top10_pct=71.0, creator_pct=1.0, insider_networks=0,
                                 total_holders=180, rugged=False, top1_pct=44.0)
    thin = InsiderReport(source="rugcheck-graph", insider_pct=0.0, sniper_pct=-1.0,
                         top10_pct=20.0, creator_pct=0.0, insider_networks=0,
                         total_holders=12, rugged=False, top1_pct=5.0)
    bench.begin_cycle()
    bench.note_insider_report("MINT1", distributed)
    ok_d, why_d = h1.entry_gate(cand(), bench)
    check("distributed holders accepted", ok_d, why_d)
    check("unknown sniper share does not reject", "no sniper data" in why_d, why_d)
    bench.begin_cycle()
    bench.note_insider_report("MINT1", concentrated)
    ok_c, why_c = h1.entry_gate(cand(), bench)
    check("concentrated holders rejected", not ok_c, why_c)
    bench.begin_cycle()
    bench.note_insider_report("MINT1", thin)
    ok_t, why_t = h1.entry_gate(cand(), bench)
    check("too few holders rejected", not ok_t, why_t)
    bench.begin_cycle()
    feed.setup_ok = False
    ok_g, why_g = h1.entry_gate(cand(), bench)
    check("base-entry setup gate still governs the holder book", not ok_g, why_g)
    feed.setup_ok = True
    bench.begin_cycle()
    bench.note_insider_report("MINT1", distributed)
    young = cand()
    young.age_min = 5.0
    ok_y, why_y = h1.entry_gate(young, bench)
    check("too young for the base-entry population", not ok_y, why_y)
    old = cand()
    old.age_min = 26 * 60.0
    ok_o, why_o = h1.entry_gate(old, bench)
    check("older than the base funnel's window rejected", not ok_o, why_o)

    print("\n== bankroll: entry, cash, round trip ==")
    check("book starts at 1 SOL", bounce.cash_lamports() == LAMPORTS_PER_SOL,
          f"{books.sol(bounce.cash_lamports()):.4f} SOL")
    bench.begin_cycle()
    entered = bounce.try_enter([cand()], bench)
    cash_after_buy = bounce.cash_lamports()
    expected = LAMPORTS_PER_SOL - int(0.25 * LAMPORTS_PER_SOL) - 150_000
    check("entry taken", entered == 1)
    check("cash debited by size + fee", cash_after_buy == expected,
          f"{books.sol(cash_after_buy):.5f} vs {books.sol(expected):.5f}")
    check("base ledger untouched", not Portfolio(tmp, "paper").open_positions())
    check("H1 book bankroll untouched by the BOUNCE trade",
          h1.cash_lamports() == LAMPORTS_PER_SOL)

    quotes = {"n": 0}

    def fake_quote(session, in_mint, out_mint, amount, slippage):
        """Position value walks 0.8x -> 1.6x (arms) -> 1.1x (trails out)."""
        mult = [0.8, 1.6, 1.1][min(quotes["n"], 2)]
        quotes["n"] += 1
        pos = bounce.db.open_positions()[0]
        return {"outAmount": int(pos.sol_spent * mult)}

    real_quote = books.jupiter.get_quote
    books.jupiter.get_quote = fake_quote
    try:
        bounce.manage(bench)
        pos = bounce.db.open_positions()[0]
        check("underwater but above stop: still open", pos.tp_stage == 0, f"peak {pos.peak_multiple:.2f}")
        bounce.manage(bench)
        pos = bounce.db.open_positions()[0]
        check("trail arms at 1.5x", pos.tp_stage == 1, f"peak {pos.peak_multiple:.2f}")
        bounce.manage(bench)
        check("trailing stop closes the position", not bounce.db.open_positions())
    finally:
        books.jupiter.get_quote = real_quote

    closed = bounce.db.closed_positions()
    check("close recorded with a trailing-stop reason",
          len(closed) == 1 and closed[0].exit_reason == "trailing_stop",
          closed[0].exit_reason if closed else "none")
    cash_end = bounce.cash_lamports()
    pnl = closed[0].sol_received - closed[0].sol_spent
    check("cash after the round trip = start + realised pnl",
          cash_end == LAMPORTS_PER_SOL + pnl,
          f"cash {books.sol(cash_end):.5f}, pnl {books.sol(pnl):+.5f}")

    print("\n== cash enforcement ==")
    tmp2 = os.path.join(tempfile.mkdtemp(prefix="booktest-"), "books.sqlite")
    poor_cfg = make_cfg(tmp2, starting_balance_sol=0.3)
    poor_bench = books.BookBench.build(poor_cfg, None, broker, FakeFeed(bounce_candles(now), now - 9 * 60))
    poor = next(b for b in poor_bench.books if b.key == "BOUNCE")
    poor_bench.begin_cycle()
    poor.try_enter([cand()], poor_bench)
    left = poor.cash_lamports()
    poor_bench.begin_cycle()
    again = poor.try_enter([cand("MINT2", "TEST2")], poor_bench)
    check("0.3 SOL book takes one 0.25 SOL position", len(poor.db.open_positions()) == 1)
    check("second entry refused - not enough cash", again == 0,
          f"{books.sol(left):.5f} SOL left")

    print("\n== live-mode refusal ==")
    live_cfg = make_cfg(os.path.join(tempfile.mkdtemp(prefix="booktest-"), "b.sqlite"))
    live_cfg.mode = "live"
    check("books refuse to run in live mode",
          books.BookBench.build(live_cfg, None, broker, feed) is None)
    nofeed_cfg = make_cfg(os.path.join(tempfile.mkdtemp(prefix="booktest-"), "b.sqlite"))
    check("books refuse to run without the candle feed",
          books.BookBench.build(nofeed_cfg, None, broker, None) is None)

    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else str(len(FAILURES)) + ' FAILED: ' + ', '.join(FAILURES)}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
