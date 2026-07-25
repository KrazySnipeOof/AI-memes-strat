from __future__ import annotations

import logging
import time

from . import insiders, jupiter, safety, scanner, smartmoney, strategy
from .config import Config, LAMPORTS_PER_SOL
from .execution import LiveBroker, PaperBroker
from .portfolio import Portfolio, Position
from .util import make_session

log = logging.getLogger("memebot")


def setup_logging(cfg: Config) -> None:
    root = logging.getLogger("memebot")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(cfg.log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def sol(lamports: int) -> float:
    return lamports / LAMPORTS_PER_SOL


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = make_session()
        self.db = Portfolio(cfg.db_path, cfg.mode)
        self.broker = LiveBroker(cfg, self.session) if cfg.mode == "live" else PaperBroker(cfg, self.session)
        self.feed = None
        if cfg.ws_enabled:
            from .birdeye_ws import BirdeyeFeed
            self.feed = BirdeyeFeed(cfg)
            self.feed.start()

    # ------------------------------------------------------------------
    # Open-position management
    # ------------------------------------------------------------------
    def manage_positions(self) -> None:
        for pos in self.db.open_positions():
            try:
                self._manage_one(pos)
            except Exception:
                log.exception("error managing position #%s (%s)", pos.id, pos.symbol)
            time.sleep(self.cfg.api_pause_sec)

    def _manage_one(self, pos: Position) -> None:
        if pos.tokens_raw <= 0:
            self.db.close_position(pos, "dust")
            return
        q = jupiter.get_quote(self.session, pos.mint, jupiter.SOL_MINT, pos.tokens_raw, self.cfg.slippage_bps)
        if q is None:
            fails = self.db.bump_quote_failures(pos)
            suffix = " - liquidity may be gone (possible rug)" if fails >= 8 else ""
            log.warning("no sell quote for %s (attempt %d)%s", pos.symbol, fails, suffix)
            return
        self.db.reset_quote_failures(pos)

        value = int(q["outAmount"])
        multiple = (pos.sol_received + value) / pos.sol_spent
        peak = max(pos.peak_multiple, multiple)
        self.db.set_progress(pos, peak, pos.tp_stage)

        action = strategy.exit_action(multiple, peak, pos.tp_stage, pos.age_min, self.cfg)
        if not action:
            log.info(
                "hold %-12s %5.2fx (peak %.2fx, stage %d, %.0fm old)",
                pos.symbol, multiple, peak, pos.tp_stage, pos.age_min,
            )
            return

        frac = action["fraction"]
        if frac <= 0:
            # Arm-only stage. The bounce/holder exit doctrines arm their
            # trailing stop at 1.5x without banking anything, and the live
            # trail only activates once tp_stage > 0 - so advancing the stage
            # IS the whole action. Falling through would fire a 1-unit dust
            # sell just to move a counter.
            self.db.set_progress(pos, peak, pos.tp_stage + 1)
            log.info("ARM %-12s %s | trailing stop %.0f%% now live",
                     pos.symbol, action["why"], self.cfg.trailing_stop_pct)
            return
        to_sell = pos.tokens_raw if frac >= 1.0 else max(1, int(pos.tokens_raw * frac))
        res = self.broker.sell(pos.mint, to_sell)
        if res is None:
            log.warning("sell failed for %s (%s); will retry next cycle", pos.symbol, action["kind"])
            return
        received, ref = res
        self.db.record_sell(pos, to_sell, received, f"{action['kind']}:{ref}")

        if frac >= 1.0 or pos.tokens_raw <= 0:
            self.db.close_position(pos, action["kind"])
            pnl = pos.sol_received - pos.sol_spent
            log.info(
                "EXIT %-12s %s | pnl %+.4f SOL (%.2fx)",
                pos.symbol, action["why"], sol(pnl), pos.sol_received / pos.sol_spent,
            )
        else:
            self.db.set_progress(pos, peak, pos.tp_stage + 1)
            log.info(
                "PARTIAL EXIT %-12s %s | sold %.0f%% of remaining, %.4f SOL banked",
                pos.symbol, action["why"], frac * 100, sol(received),
            )

    # ------------------------------------------------------------------
    # New entries
    # ------------------------------------------------------------------
    def try_enter(self) -> None:
        open_ps = self.db.open_positions()
        slots = self.cfg.max_positions - len(open_ps)
        if slots <= 0:
            log.info("all %d position slots full", self.cfg.max_positions)
            return
        realized = self.db.realized_today_lamports()
        if realized <= -self.cfg.daily_loss_limit_lamports:
            log.warning("daily loss limit hit (%+.4f SOL realized today) - no new entries", sol(realized))
            return

        held = {p.mint for p in open_ps}
        candidates = scanner.discover(self.session, self.cfg)
        if self.feed and self.cfg.ws_discovery:
            candidates.extend(self.feed.promote_candidates(
                self.session, known={c.mint for c in candidates} | held))
        self.db.record_candidates(candidates)
        if self.cfg.narrative_keywords:
            if self.cfg.narrative_require:
                candidates = [c for c in candidates if strategy.narrative_match(c.symbol, self.cfg)]
            else:
                candidates.sort(key=lambda c: not strategy.narrative_match(c.symbol, self.cfg))
        entered = 0
        deep_checks = 0
        passed_cheap = 0
        backfill_budget = [self.cfg.ws_max_backfills_per_cycle]

        for cand in candidates:
            if entered >= min(self.cfg.max_entries_per_cycle, slots):
                break
            if deep_checks >= self.cfg.max_deep_checks_per_cycle:
                break
            if cand.mint in held or self.db.recently_traded(cand.mint, self.cfg.reentry_cooldown_min):
                continue
            ok, why = safety.basic_filter(cand, self.cfg)
            if not ok:
                log.debug("skip %s: %s", cand.symbol, why)
                continue
            ok, signal_why = strategy.entry_signal(cand, self.cfg)
            if not ok:
                log.debug("skip %s: %s", cand.symbol, signal_why)
                continue
            if self.feed and self.cfg.ws_setup_filter:
                ok, setup_why = self.feed.setup_gate(cand, self.session, backfill_budget)
                if not ok:
                    log.info("reject %-12s %s", cand.symbol, setup_why)
                    continue
                signal_why = f"{signal_why} | {setup_why}"
            passed_cheap += 1
            deep_checks += 1

            rc = safety.rugcheck(self.session, cand.mint, self.cfg)
            if not rc.ok:
                log.info("reject %-12s %s", cand.symbol, rc.reason)
                time.sleep(self.cfg.api_pause_sec)
                continue
            if self.cfg.insiders_enabled:
                irep = insiders.check(self.session, cand.mint, self.cfg)
                ok, ins_why = insiders.verdict(irep, self.cfg)
                if not ok:
                    if self.cfg.insiders_shadow:
                        log.info("shadow %-12s would reject: %s", cand.symbol, ins_why)
                        ins_why = f"SHADOW-FAIL insiders: {ins_why}"
                    else:
                        log.info("reject %-12s %s", cand.symbol, ins_why)
                        time.sleep(self.cfg.api_pause_sec)
                        continue
            else:
                ins_why = "insider check disabled"
            if self.cfg.smart_money_enabled:
                sm_rep = smartmoney.check(self.session, cand.mint, self.cfg)
                ok, sm_why = smartmoney.verdict(sm_rep, self.cfg)
                if not ok:
                    if self.cfg.smart_money_shadow:
                        log.info("shadow %-12s would reject: %s", cand.symbol, sm_why)
                        sm_why = f"SHADOW-FAIL smart-money: {sm_why}"
                    else:
                        log.info("reject %-12s %s", cand.symbol, sm_why)
                        time.sleep(self.cfg.api_pause_sec)
                        continue
            else:
                sm_why = "smart-money check disabled"
            ok, rt_why = safety.roundtrip_check(
                self.session, cand.mint, self.cfg.position_size_lamports, self.cfg
            )
            if not ok:
                log.info("reject %-12s %s", cand.symbol, rt_why)
                time.sleep(self.cfg.api_pause_sec)
                continue

            res = self.broker.buy(cand.mint, self.cfg.position_size_lamports)
            if res is None:
                log.warning("buy failed for %s", cand.symbol)
                continue
            tokens, spent, ref = res
            pid = self.db.create_position(cand.mint, cand.symbol, cand.pool, spent, tokens)
            age = f"{cand.age_min:.0f}" if cand.age_min is not None else "?"
            if self.cfg.narrative_keywords:
                nar = "narrative HIT" if strategy.narrative_match(cand.symbol, self.cfg) else "off-narrative"
                signal_why = f"{nar} | {signal_why}"
            log.info(
                "ENTER %-12s #%d | %.3f SOL | liq $%s | age %sm | %s | %s | %s | %s | %s | ref %s",
                cand.symbol, pid, sol(spent), f"{cand.liquidity_usd:,.0f}", age,
                rc.reason, ins_why, sm_why, rt_why, signal_why, ref,
            )
            entered += 1
            time.sleep(self.cfg.api_pause_sec)

        log.info(
            "entry scan: %d candidates, %d passed filters, %d deep-checked, %d entered",
            len(candidates), passed_cheap, deep_checks, entered,
        )

    # ------------------------------------------------------------------
    def cycle(self) -> None:
        if self.feed:
            self.feed.note_open_positions([p.mint for p in self.db.open_positions()])
        self.manage_positions()
        self.try_enter()

    def run(self, once: bool = False) -> None:
        tps = "/".join(
            f"{tp['multiple']}x" + ("(arm)" if float(tp["sell_fraction_of_remaining"]) <= 0 else "")
            for tp in self.cfg.take_profits
        )
        log.info(
            "memebot starting | strategy=%s | mode=%s | size=%.3f SOL | max_positions=%d "
            "| stop=%.0f%% | tp=%s | trail=%.0f%% | hard_tp=%.1fx | db=%s",
            self.cfg.strategy, self.cfg.mode, self.cfg.position_size_sol,
            self.cfg.max_positions, self.cfg.stop_loss_pct, tps,
            self.cfg.trailing_stop_pct, self.cfg.hard_tp_multiple, self.cfg.db_path,
        )
        if self.cfg.mode != "live":
            log.info("PAPER MODE - simulated fills on live quotes, no real funds")
        if self.feed:
            log.info("BIRDEYE WS ON - listing discovery + live 1m candles + "
                     "%s entry gate (max %d price subs)",
                     self.cfg.ws_setup_family, self.cfg.ws_max_price_subs)
        try:
            while True:
                started = time.time()
                try:
                    self.cycle()
                except KeyboardInterrupt:
                    raise
                except Exception:
                    log.exception("cycle failed; continuing")
                if once:
                    break
                elapsed = time.time() - started
                time.sleep(max(1.0, self.cfg.scan_interval_sec - elapsed))
        finally:
            if self.feed:
                self.feed.stop()


def report(cfg: Config) -> None:
    session = make_session()
    db = Portfolio(cfg.db_path, cfg.mode)
    print(f"mode: {cfg.mode}")

    open_ps = db.open_positions()
    print(f"\nOpen positions ({len(open_ps)}):")
    for p in open_ps:
        q = None
        if p.tokens_raw > 0:
            q = jupiter.get_quote(session, p.mint, jupiter.SOL_MINT, p.tokens_raw, cfg.slippage_bps)
        value = int(q["outAmount"]) if q else 0
        multiple = (p.sol_received + value) / p.sol_spent if p.sol_spent else 0.0
        warn = "  [NO SELL QUOTE]" if q is None and p.tokens_raw > 0 else ""
        print(
            f"  #{p.id:<4} {p.symbol:<12} {multiple:5.2f}x  spent {sol(p.sol_spent):.4f} SOL"
            f"  banked {sol(p.sol_received):.4f}  age {p.age_min:.0f}m  stage {p.tp_stage}{warn}"
        )

    closed = db.closed_positions()
    print(f"\nClosed positions: {len(closed)}")
    if closed:
        wins = [p for p in closed if p.sol_received > p.sol_spent]
        total = sum(p.sol_received - p.sol_spent for p in closed)
        best = max(closed, key=lambda p: p.sol_received - p.sol_spent)
        worst = min(closed, key=lambda p: p.sol_received - p.sol_spent)
        print(f"  win rate : {100 * len(wins) / len(closed):.0f}% ({len(wins)}/{len(closed)})")
        print(f"  total pnl: {sol(total):+.4f} SOL")
        print(f"  best     : {best.symbol} {sol(best.sol_received - best.sol_spent):+.4f} SOL")
        print(f"  worst    : {worst.symbol} {sol(worst.sol_received - worst.sol_spent):+.4f} SOL")
        print("\n  last 10:")
        for p in closed[:10]:
            mult = p.sol_received / p.sol_spent if p.sol_spent else 0.0
            print(
                f"    {p.closed_at}  {p.symbol:<12} {mult:5.2f}x"
                f"  {sol(p.sol_received - p.sol_spent):+.4f} SOL  {p.exit_reason}"
            )
    print(f"\nRealized today: {sol(db.realized_today_lamports()):+.4f} SOL")
