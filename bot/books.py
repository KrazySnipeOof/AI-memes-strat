"""Independent paper books - one bankroll per strategy.

Each book is a self-contained paper account: its own starting balance (1.0
SOL by default), its own positions, its own entry rule, its own exit
doctrine and its own daily-loss breaker. Cash is ENFORCED - a book that has
spent its SOL stops trading until an exit returns some.

Books never touch the base strategy's ledger. Positions live in the same
sqlite file under a distinct Portfolio mode ("book:BOUNCE"), and every
Portfolio query already filters on mode, so the base book (mode "paper")
reads exactly as it did before this module existed. Books also ride the
candidate stream the base strategy already discovered, so they add no
discovery API cost - only the quotes and gate lookups their own trades need.

Entry rules are imported verbatim from the pre-registered research modules
(loop8_eval.bounce_entry, sweepvol.churn_entry, and backtest.entry_setup_ok
via the feed's setup_gate) so a book trades the strategy the lab measured,
not a paraphrase of it. Exits mirror loop8_eval.sim: hard target, pre-arm
stop, trailing stop once armed at `arm`x, time stop - no partial banking.

Documented divergences from the backtest (unavoidable, live):
  * a candle-triggered entry (BOUNCE, V1) fires only if the trigger candle
    is at most `books.max_trigger_age_sec` old. The backtest enters at the
    trigger close; chasing an hours-old trigger is a different strategy.
  * V1's lp1b universe filter cannot be applied live (the launchpad id is
    not carried on a live candidate), so the V1 book runs the churn rule on
    every candidate with candle coverage. Its 1B-supply MC assumption is
    exact only for launchpad-curve tokens - the same caveat sweepvol.py
    discloses, now unfiltered.
  * H1's sniper share needs GMGN, which is usually Cloudflare-gated;
    unknown components of a holder gate pass rather than reject, so the
    live gate is effectively holders/top10/top1.
  * fills are Jupiter-quoted (real route impact + paper fee), not the
    backtest's candle model with its flat 4% haircut.

Live trading is refused: books are a research harness and run in paper mode
only (see BookBench.build).
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from . import insiders, jupiter
from .config import Config, LAMPORTS_PER_SOL
from .portfolio import Portfolio, Position
from .scanner import Candidate

from loop8_eval import bounce_entry   # repo root (run.py sys.path); pre-registered
from sweepvol import churn_entry      # rules imported verbatim, never re-typed

log = logging.getLogger("memebot.books")

# same coverage slack BirdeyeFeed.setup_gate uses for "history from listing"
COVERAGE_SLACK_SEC = 180
MIN_CANDLES = 8

# exit doctrines, verbatim from freshlab.py / loop8_eval.CONFIGS
EA = dict(stop=60, arm=1.5, trail=25, hard=10.0, hold=480.0)
EB = dict(stop=60, arm=1.5, trail=25, hard=20.0, hold=480.0)
BOUNCE_EX = dict(stop=50, arm=1.5, trail=25, hard=10.0, hold=480.0)

# holder-distribution gates, verbatim from freshlab.HOLDER_GATES (top1 and
# sniper shares are skipped when the source cannot report them)
H1_GATE = dict(min_holders=50, max_top10_pct=50.0, max_top1_pct=20.0, max_sniper_pct=30.0)
H2_GATE = dict(min_holders=30, max_top10_pct=65.0, max_top1_pct=30.0, max_sniper_pct=None)

BOOK_DEFS: Dict[str, dict] = {
    "BOUNCE": {
        "label": "bounce P3 - dd55 cv500 nk1",
        "entry": "bounce",
        "params": dict(dd=0.55, cv=500.0, max_nukes=1),
        "exits": BOUNCE_EX,
        "enabled": True,
        "lab": "loop8 incumbent; fresh-OOT 1.14x",
    },
    "V1": {
        "label": "vol/MC churn - r0.5 w30 nk1",
        "entry": "churn",
        "params": dict(ratio=0.5, window_min=30, max_nukes=1),
        "exits": EB,
        "enabled": True,
        "lab": "sweepvol train finalist; family failed backtest",
    },
    "H1": {
        "label": "holder distributed - h50 t10<=50 t1<=20",
        "entry": "holder",
        "params": H1_GATE,
        "exits": EA,
        "enabled": True,
        "lab": "fresh-OOT 1.03x vs 0.60x baseline",
    },
    # controls, off by default: H0 is the same population with no holder
    # gate (the honest control for H1), H2 the loose variant
    "H0": {
        "label": "holder baseline - no gate (control)",
        "entry": "holder",
        "params": None,
        "exits": EA,
        "enabled": False,
        "lab": "control for H1",
    },
    "H2": {
        "label": "holder loose - h30 t10<=65 t1<=30",
        "entry": "holder",
        "params": H2_GATE,
        "exits": EA,
        "enabled": False,
        "lab": "fresh-OOT variant",
    },
}


def sol(lamports: int) -> float:
    return lamports / LAMPORTS_PER_SOL


def book_mode(key: str) -> str:
    return f"book:{key}"


def exit_action(multiple: float, peak: float, armed: bool, age_min: float,
                ex: dict) -> Optional[dict]:
    """loop8_eval.sim's exit doctrine, evaluated on live quotes.

    `multiple` is the total-value multiple (banked SOL + what the remaining
    tokens would fetch now) / SOL spent, so our own price impact is already
    in it. The pre-arm stop stops applying once the trail arms, exactly as
    the simulator does; every book exit is full-size (the lab's exits bank
    nothing on the way up)."""
    if multiple >= ex["hard"]:
        return {"kind": "hard_take_profit", "why": f"hit {ex['hard']:.1f}x hard target at {multiple:.2f}x"}
    if not armed and multiple <= 1.0 - ex["stop"] / 100.0:
        return {"kind": "stop_loss", "why": f"stop loss at {multiple:.2f}x"}
    if armed and multiple <= peak * (1.0 - ex["trail"] / 100.0):
        return {"kind": "trailing_stop", "why": f"trailing stop: {multiple:.2f}x off peak {peak:.2f}x"}
    if age_min >= ex["hold"]:
        return {"kind": "time_stop", "why": f"max hold {ex['hold']:.0f}m reached"}
    return None


class Book:
    """One strategy, one bankroll."""

    def __init__(self, key: str, defn: dict, cfg: Config):
        over = (cfg.books_overrides.get(key) or {})
        self.key = key
        self.defn = defn
        self.label = defn["label"]
        self.cfg = cfg
        self.mode = book_mode(key)
        self.db = Portfolio(cfg.db_path, self.mode)
        self.exits = defn["exits"]
        self.start_lamports = int(
            float(over.get("starting_balance_sol", cfg.books_start_sol)) * LAMPORTS_PER_SOL)
        self.size_lamports = int(
            float(over.get("position_size_sol", cfg.books_position_size_sol)) * LAMPORTS_PER_SOL)
        self.max_positions = int(over.get("max_positions", cfg.books_max_positions))
        self.loss_limit_lamports = int(
            float(over.get("daily_loss_limit_sol", cfg.books_daily_loss_limit_sol))
            * LAMPORTS_PER_SOL)
        self._breaker_day = None

    # ------------------------------------------------------------- accounting
    def cash_lamports(self) -> int:
        """Uninvested SOL: the starting bankroll plus every fill this book has
        ever made. Open positions are money already out the door."""
        return self.start_lamports + self.db.net_flow_lamports()

    # ------------------------------------------------------------------ exits
    def manage(self, bench: "BookBench") -> None:
        for pos in self.db.open_positions():
            try:
                self._manage_one(pos, bench)
            except Exception:
                log.exception("book %s: error managing #%s (%s)", self.key, pos.id, pos.symbol)
            time.sleep(self.cfg.api_pause_sec)

    def _manage_one(self, pos: Position, bench: "BookBench") -> None:
        if pos.tokens_raw <= 0:
            self.db.close_position(pos, "dust")
            return
        q = jupiter.get_quote(bench.session, pos.mint, jupiter.SOL_MINT,
                              pos.tokens_raw, self.cfg.slippage_bps)
        if q is None:
            fails = self.db.bump_quote_failures(pos)
            suffix = " - liquidity may be gone (possible rug)" if fails >= 8 else ""
            log.warning("book %s: no sell quote for %s (attempt %d)%s",
                        self.key, pos.symbol, fails, suffix)
            return
        self.db.reset_quote_failures(pos)

        value = int(q["outAmount"])
        multiple = (pos.sol_received + value) / pos.sol_spent
        peak = max(pos.peak_multiple, multiple)
        # tp_stage doubles as the armed flag for books: 0 = pre-arm (the stop
        # is live), 1 = armed (the trail is live). Same column, no migration.
        armed = pos.tp_stage > 0 or peak >= self.exits["arm"]
        self.db.set_progress(pos, peak, 1 if armed else 0)

        action = exit_action(multiple, peak, armed, pos.age_min, self.exits)
        if not action:
            log.debug("book %s hold %-12s %5.2fx (peak %.2fx, %s, %.0fm old)",
                      self.key, pos.symbol, multiple, peak,
                      "armed" if armed else "pre-arm", pos.age_min)
            return
        res = bench.broker.sell(pos.mint, pos.tokens_raw)
        if res is None:
            log.warning("book %s: sell failed for %s (%s); retrying next cycle",
                        self.key, pos.symbol, action["kind"])
            return
        received, ref = res
        self.db.record_sell(pos, pos.tokens_raw, received, f"{action['kind']}:{ref}")
        self.db.close_position(pos, action["kind"])
        pnl = pos.sol_received - pos.sol_spent
        log.info("BOOK %s EXIT %-12s %s | pnl %+.4f SOL (%.2fx) | cash %.4f SOL",
                 self.key, pos.symbol, action["why"], sol(pnl),
                 pos.sol_received / pos.sol_spent, sol(self.cash_lamports()))

    # ---------------------------------------------------------------- entries
    def try_enter(self, candidates: List[Candidate], bench: "BookBench") -> int:
        open_ps = self.db.open_positions()
        slots = self.max_positions - len(open_ps)
        if slots <= 0:
            return 0
        realized = self.db.realized_today_lamports()
        if realized <= -self.loss_limit_lamports:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            if self._breaker_day != day:
                self._breaker_day = day
                log.warning("book %s: daily loss limit hit (%+.4f SOL today) - no new entries",
                            self.key, sol(realized))
            return 0
        cash = self.cash_lamports()
        need = self.size_lamports + self.cfg.paper_fee_lamports
        if cash < need:
            log.info("book %s: cash %.4f SOL < one position (%.4f) - no new entries",
                     self.key, sol(cash), sol(need))
            return 0

        held = {p.mint for p in open_ps}
        entered, paid_checks = 0, 0
        for cand in candidates:
            if entered >= min(self.cfg.books_max_entries_per_cycle, slots):
                break
            if paid_checks >= self.cfg.books_max_gate_checks_per_cycle:
                break
            if cand.mint in held or self.db.recently_traded(cand.mint, self.cfg.reentry_cooldown_min):
                continue
            before = bench.api_calls
            ok, why = self.entry_gate(cand, bench)
            if bench.api_calls > before:   # only lookups that cost an API call are budgeted
                paid_checks += 1
            if not ok:
                log.debug("book %s skip %-12s %s", self.key, cand.symbol, why)
                continue
            res = bench.broker.buy(cand.mint, self.size_lamports)
            if res is None:
                log.warning("book %s: buy failed for %s", self.key, cand.symbol)
                continue
            tokens, spent, ref = res
            pid = self.db.create_position(cand.mint, cand.symbol, cand.pool, spent, tokens)
            entered += 1
            log.info("BOOK %s ENTER %-12s #%d | %.3f SOL | cash %.4f -> %.4f | %s | ref %s",
                     self.key, cand.symbol, pid, sol(spent), sol(cash),
                     sol(self.cash_lamports()), why, ref)
            time.sleep(self.cfg.api_pause_sec)
        return entered

    def entry_gate(self, cand: Candidate, bench: "BookBench") -> Tuple[bool, str]:
        kind = self.defn["entry"]
        if kind == "bounce":
            return self._candle_gate(cand, bench, bounce_entry)
        if kind == "churn":
            return self._candle_gate(cand, bench, churn_entry)
        if kind == "holder":
            return self._holder_gate(cand, bench)
        return False, f"unknown entry rule '{kind}'"

    def _history(self, cand: Candidate, bench: "BookBench"):
        """Candle history from listing, backfilled once if the store is short
        or stale. Same contract as BirdeyeFeed.setup_gate: no coverage from
        listing means no trade, because the backtest population always had
        the full history its rule scans."""
        feed = bench.feed
        now = int(time.time())
        wl = feed.watch.get(cand.mint)
        created = wl["listed_ts"] if wl else None
        if created is None and cand.age_min is not None:
            created = now - int(cand.age_min * 60)
        if created is None:
            return None, "unknown listing time"
        rows = feed.candle_rows(cand.mint)
        stale = bool(rows) and now - rows[-1][0] > COVERAGE_SLACK_SEC
        if ((not rows or rows[0][0] > created + COVERAGE_SLACK_SEC or stale)
                and self.cfg.ws_backfill and bench.backfill_budget[0] > 0):
            bench.backfill_budget[0] -= 1
            bench.api_calls += 1
            feed.backfill(bench.session, cand.mint, created, now)
            rows = feed.candle_rows(cand.mint)
        if not rows or rows[0][0] > created + COVERAGE_SLACK_SEC:
            return None, "no candle coverage from listing"
        rows = [r for r in rows if r[0] <= now]
        if len(rows) < MIN_CANDLES:
            return None, f"only {len(rows)} candles"
        return rows, ""

    def _candle_gate(self, cand: Candidate, bench: "BookBench", rule) -> Tuple[bool, str]:
        rows, why = self._history(cand, bench)
        if rows is None:
            return False, why
        trig = rule(rows, **self.defn["params"])
        if not trig:
            return False, "no trigger"
        idx, _price = trig
        age = int(time.time()) - int(rows[idx][0])
        if age > self.cfg.books_max_trigger_age_sec:
            return False, f"trigger {age / 60:.0f}m stale"
        return True, f"trigger at candle {idx + 1}/{len(rows)} ({age}s old)"

    def _holder_gate(self, cand: Candidate, bench: "BookBench") -> Tuple[bool, str]:
        # the lab's holder families sit on the base-entry population: age >=
        # 30m, cum vol >= $8k, backtest.DEFAULT_SETUP. The lab entered at
        # exactly 30m; live we enter whenever the token is discovered inside
        # the same age window the base funnel uses, so the age cap applies
        # too - a day-old token is a different setup than a 30m-old one.
        if cand.age_min is not None and cand.age_min < self.cfg.min_age_min:
            return False, f"age {cand.age_min:.0f}m < {self.cfg.min_age_min:.0f}m"
        if cand.age_min is not None and cand.age_min > self.cfg.max_age_min:
            return False, f"age {cand.age_min:.0f}m > {self.cfg.max_age_min:.0f}m"
        ok, why = bench.feed.setup_gate(cand, bench.session, bench.backfill_budget)
        if not ok:
            return False, why
        gate = self.defn["params"]
        if not gate:
            return True, f"{why} | no holder gate (control)"
        rep = bench.insider_report(cand.mint)
        if rep is None:
            return False, "no holder data (GMGN gated, RugCheck report failed)"
        if rep.total_holders is None or rep.total_holders < gate["min_holders"]:
            return False, f"holders {rep.total_holders} < {gate['min_holders']}"
        if rep.top10_pct > gate["max_top10_pct"]:
            return False, f"top10 {rep.top10_pct:.1f}% > {gate['max_top10_pct']:.0f}%"
        if rep.top1_pct >= 0 and rep.top1_pct > gate["max_top1_pct"]:
            return False, f"top1 {rep.top1_pct:.1f}% > {gate['max_top1_pct']:.0f}%"
        if (gate["max_sniper_pct"] is not None and rep.sniper_pct >= 0
                and rep.sniper_pct > gate["max_sniper_pct"]):
            return False, f"snipers {rep.sniper_pct:.1f}% > {gate['max_sniper_pct']:.0f}%"
        unknown = []
        if rep.top1_pct < 0:
            unknown.append("top1")
        if rep.sniper_pct < 0 and gate["max_sniper_pct"] is not None:
            unknown.append("sniper")
        tail = f" (no {'/'.join(unknown)} data)" if unknown else ""
        return True, (f"{why} | holders {rep.total_holders}, top10 {rep.top10_pct:.1f}%,"
                      f" top1 {rep.top1_pct:.1f}%{tail}")

    # ----------------------------------------------------------------- status
    def summary(self) -> str:
        return (f"{self.key} {len(self.db.open_positions())}/{self.max_positions}op"
                f" cash {sol(self.cash_lamports()):.3f}")


class BookBench:
    """All active books plus the per-cycle context they share."""

    def __init__(self, cfg: Config, session, broker, feed):
        self.cfg = cfg
        self.session = session
        self.broker = broker
        self.feed = feed
        self.books: List[Book] = []
        for key, defn in BOOK_DEFS.items():
            over = cfg.books_overrides.get(key) or {}
            if bool(over.get("enabled", defn["enabled"])):
                self.books.append(Book(key, defn, cfg))
        self.backfill_budget = [0]
        self._insiders: Dict[str, Optional[insiders.InsiderReport]] = {}
        self.api_calls = 0

    @classmethod
    def build(cls, cfg: Config, session, broker, feed) -> Optional["BookBench"]:
        """A bench, or None with the reason logged. Books are paper-only: they
        are a research harness and must never place a live order."""
        if not cfg.books_enabled:
            return None
        if cfg.mode != "paper":
            log.warning("strategy books are paper-only; not starting them in %s mode", cfg.mode)
            return None
        if feed is None:
            log.warning("strategy books need the Birdeye websocket feed for candles"
                        " (websocket.enabled) - not starting them")
            return None
        bench = cls(cfg, session, broker, feed)
        if not bench.books:
            log.warning("books.enabled is true but no book is enabled")
            return None
        return bench

    # ------------------------------------------------------------ cycle hooks
    def begin_cycle(self) -> None:
        self.backfill_budget = [self.cfg.books_max_backfills_per_cycle]
        self._insiders = {}

    def note_insider_report(self, mint: str, rep) -> None:
        """Base hands over the report it already paid for this cycle."""
        self._insiders.setdefault(mint, rep)

    def insider_report(self, mint: str):
        if mint not in self._insiders:
            self.api_calls += 1
            self._insiders[mint] = insiders.check(self.session, mint, self.cfg)
        return self._insiders[mint]

    def open_mints(self) -> List[str]:
        return [p.mint for b in self.books for p in b.db.open_positions()]

    def manage(self) -> None:
        for book in self.books:
            book.manage(self)

    def step(self, candidates: List[Candidate]) -> None:
        new = {}
        for book in self.books:
            try:
                new[book.key] = book.try_enter(candidates, self)
            except Exception:
                log.exception("book %s: entry pass failed", book.key)
                new[book.key] = 0
        log.info("books: %s", " | ".join(
            f"{b.summary()}{' +' + str(new[b.key]) if new.get(b.key) else ''}"
            for b in self.books))

    def log_startup(self) -> None:
        for b in self.books:
            log.info("book %-7s %.3f SOL start | %.3f SOL cash | %d open | %.2f SOL x %d slots"
                     " | stop%.0f arm%.1f trail%.0f hard%.0fx hold%.0fm | %s",
                     b.key, sol(b.start_lamports), sol(b.cash_lamports()),
                     len(b.db.open_positions()), sol(b.size_lamports), b.max_positions,
                     b.exits["stop"], b.exits["arm"], b.exits["trail"], b.exits["hard"],
                     b.exits["hold"], b.label)
