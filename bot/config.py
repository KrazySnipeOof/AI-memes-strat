from __future__ import annotations

import json
import os
from typing import Any, Dict

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
EXCLUDED_MINTS = {SOL_MINT, USDC_MINT, USDT_MINT}
LAMPORTS_PER_SOL = 1_000_000_000


class Config:
    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw
        self.mode: str = raw.get("mode", "paper")
        self.rpc_url: str = os.environ.get("MEMEBOT_RPC_URL") or raw.get(
            "rpc_url", "https://api.mainnet-beta.solana.com"
        )
        self.wallet_pubkey: str = str(
            os.environ.get("MEMEBOT_WALLET_PUBKEY") or raw.get("wallet_pubkey") or ""
        ).strip()
        self.db_path: str = raw.get("db_path", "memebot.sqlite")
        self.log_path: str = raw.get("log_path", "memebot.log")
        self.scan_interval_sec: int = int(raw.get("scan_interval_sec", 45))
        self.position_size_sol: float = float(raw.get("position_size_sol", 0.25))
        self.max_positions: int = int(raw.get("max_positions", 3))
        self.max_entries_per_cycle: int = int(raw.get("max_entries_per_cycle", 1))
        self.max_deep_checks_per_cycle: int = int(raw.get("max_deep_checks_per_cycle", 5))
        self.reentry_cooldown_min: float = float(raw.get("reentry_cooldown_min", 240))
        self.slippage_bps: int = int(raw.get("slippage_bps", 500))
        self.paper_fee_lamports: int = int(raw.get("paper_fee_lamports", 150_000))
        self.api_pause_sec: float = float(raw.get("api_pause_sec", 0.4))

        f = raw.get("filters", {})
        self.min_liquidity_usd: float = float(f.get("min_liquidity_usd", 20_000))
        self.max_liquidity_usd: float = float(f.get("max_liquidity_usd", 2_000_000))
        self.min_age_min: float = float(f.get("min_age_min", 10))
        self.max_age_min: float = float(f.get("max_age_min", 1_440))
        self.max_fdv_to_liquidity: float = float(f.get("max_fdv_to_liquidity", 60))
        self.min_vol_m5_usd: float = float(f.get("min_vol_m5_usd", 2_000))
        self.min_sells_m5: int = int(f.get("min_sells_m5", 3))
        self.max_buy_sell_ratio: float = float(f.get("max_buy_sell_ratio", 12))

        s = raw.get("safety", {})
        self.require_rugcheck: bool = bool(s.get("require_rugcheck", True))
        self.max_rugcheck_score: float = float(s.get("max_rugcheck_score", 40))
        self.max_roundtrip_loss_pct: float = float(s.get("max_roundtrip_loss_pct", 12))

        i = raw.get("insiders", {})
        self.insiders_enabled: bool = bool(i.get("enabled", True))
        self.insiders_shadow: bool = bool(i.get("shadow", False))
        self.try_gmgn: bool = bool(i.get("try_gmgn", True))
        self.insiders_require_data: bool = bool(i.get("require_data", True))
        self.max_insider_pct: float = float(i.get("max_insider_pct", 15))
        self.max_sniper_pct: float = float(i.get("max_sniper_pct", 20))
        self.max_top10_pct: float = float(i.get("max_top10_pct", 45))
        self.max_creator_pct: float = float(i.get("max_creator_pct", 10))
        self.min_holders: int = int(i.get("min_holders", 50))

        sm = raw.get("smart_money", {})
        self.smart_money_enabled: bool = bool(sm.get("enabled", True))
        self.smart_money_shadow: bool = bool(sm.get("shadow", False))
        self.smart_money_require: bool = bool(sm.get("require_data", False))
        self.min_top_traders: int = int(sm.get("min_top_traders", 6))
        self.max_top1_volume_share: float = float(sm.get("max_top1_volume_share", 45))
        self.min_buy_share: float = float(sm.get("min_buy_share", 35))
        self.max_buy_share: float = float(sm.get("max_buy_share", 80))
        self.max_bundlers: int = int(sm.get("max_bundlers", 3))

        e = raw.get("entry", {})
        self.min_chg_m5_pct: float = float(e.get("min_chg_m5_pct", 1.0))
        self.max_chg_m5_pct: float = float(e.get("max_chg_m5_pct", 60.0))
        self.min_chg_h1_pct: float = float(e.get("min_chg_h1_pct", -20.0))
        self.min_buy_sell_edge: float = float(e.get("min_buy_sell_edge", 1.1))

        n = raw.get("narrative", {})
        self.narrative_keywords = [
            str(k).strip().lower() for k in n.get("keywords", []) if str(k).strip()
        ]
        self.narrative_require: bool = bool(n.get("require", False))

        x = raw.get("exits", {})
        self.stop_loss_pct: float = float(x.get("stop_loss_pct", 35))
        self.take_profits = x.get(
            "take_profits",
            [
                {"multiple": 2.0, "sell_fraction_of_remaining": 0.5},
                {"multiple": 3.0, "sell_fraction_of_remaining": 0.5},
            ],
        )
        self.hard_tp_multiple: float = float(x.get("hard_tp_multiple", 5.0))
        self.trailing_stop_pct: float = float(x.get("trailing_stop_pct", 25))
        self.max_hold_min: float = float(x.get("max_hold_min", 240))

        r = raw.get("risk", {})
        self.daily_loss_limit_sol: float = float(r.get("daily_loss_limit_sol", 0.75))

        # Birdeye websocket feed (Premium plan) - fully opt-in; enabled: false
        # keeps the original zero-Birdeye data path byte-identical.
        w = raw.get("websocket", {})
        self.ws_enabled: bool = bool(w.get("enabled", False))
        self.ws_discovery: bool = bool(w.get("discovery", True))
        self.ws_setup_filter: bool = bool(w.get("apply_setup_filter", True))
        self.ws_backfill: bool = bool(w.get("backfill_rest", True))
        self.ws_max_backfills_per_cycle: int = int(w.get("max_backfills_per_cycle", 4))
        self.ws_max_promotions_per_cycle: int = int(w.get("max_promotions_per_cycle", 3))
        self.ws_max_price_subs: int = int(w.get("max_price_subs", 95))
        self.ws_min_listing_liquidity: float = float(w.get("min_listing_liquidity_usd", 2_000))
        self.ws_min_cum_vol_usd: float = float(w.get("min_cum_vol_usd", 8_000))
        self.ws_persist_state: bool = bool(w.get("persist_state", True))

    @property
    def position_size_lamports(self) -> int:
        return int(self.position_size_sol * LAMPORTS_PER_SOL)

    @property
    def daily_loss_limit_lamports(self) -> int:
        return int(self.daily_loss_limit_sol * LAMPORTS_PER_SOL)

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))
