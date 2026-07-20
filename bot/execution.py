from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Optional, Tuple

from . import jupiter
from .config import Config

log = logging.getLogger("memebot.execution")


class PaperBroker:
    """Simulates fills using live Jupiter quotes. No keys, no funds at risk.

    Fills are optimistic versus live trading: no latency between quote and
    fill, no failed transactions, no MEV. Treat paper results as an upper
    bound, not a forecast.
    """

    def __init__(self, cfg: Config, session):
        self.cfg = cfg
        self.session = session

    def buy(self, mint: str, lamports: int) -> Optional[Tuple[int, int, str]]:
        q = jupiter.get_quote(self.session, jupiter.SOL_MINT, mint, lamports, self.cfg.slippage_bps)
        if not q:
            return None
        tokens = int(q["outAmount"])
        if tokens <= 0:
            return None
        spent = lamports + self.cfg.paper_fee_lamports
        return tokens, spent, "paper"

    def sell(self, mint: str, tokens: int) -> Optional[Tuple[int, str]]:
        q = jupiter.get_quote(self.session, mint, jupiter.SOL_MINT, tokens, self.cfg.slippage_bps)
        if not q:
            return None
        received = max(0, int(q["outAmount"]) - self.cfg.paper_fee_lamports)
        return received, "paper"


class LiveBroker:
    """Real on-chain swaps via Jupiter. Requires the solders package, a funded
    wallet key in MEMEBOT_PRIVATE_KEY, and MEMEBOT_I_UNDERSTAND_THE_RISKS=yes."""

    def __init__(self, cfg: Config, session):
        confirm = os.environ.get("MEMEBOT_I_UNDERSTAND_THE_RISKS", "").strip().lower()
        if confirm not in ("yes", "true", "1"):
            raise SystemExit(
                "Refusing to start live mode: set MEMEBOT_I_UNDERSTAND_THE_RISKS=yes in .env"
                " after reading the README risk section."
            )
        try:
            from solders.keypair import Keypair
            from solders.transaction import VersionedTransaction
        except ImportError:
            raise SystemExit("Live mode requires the 'solders' package: pip install solders")
        self._VersionedTransaction = VersionedTransaction

        key = os.environ.get("MEMEBOT_PRIVATE_KEY", "").strip()
        if not key:
            raise SystemExit("Live mode requires MEMEBOT_PRIVATE_KEY in .env (base58 string or JSON byte array).")
        if key.startswith("["):
            self.keypair = Keypair.from_bytes(bytes(json.loads(key)))
        else:
            self.keypair = Keypair.from_base58_string(key)
        self.pubkey = str(self.keypair.pubkey())
        if cfg.wallet_pubkey and self.pubkey != cfg.wallet_pubkey:
            raise SystemExit(
                f"Refusing to trade: the loaded private key controls {self.pubkey}, but"
                f" config.json pins wallet_pubkey {cfg.wallet_pubkey}. Fix whichever is wrong."
            )
        self.cfg = cfg
        self.session = session
        log.info("live wallet: %s | balance %.4f SOL", self.pubkey, self.sol_balance() / 1e9)

    def _rpc(self, method: str, params: list):
        r = self.session.post(
            self.cfg.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=25,
        )
        r.raise_for_status()
        payload = r.json()
        if "error" in payload:
            raise RuntimeError(f"RPC {method} error: {payload['error']}")
        return payload.get("result")

    def sol_balance(self) -> int:
        return int(self._rpc("getBalance", [self.pubkey])["value"])

    def token_balance(self, mint: str) -> int:
        res = self._rpc(
            "getTokenAccountsByOwner",
            [self.pubkey, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        total = 0
        for acct in (res or {}).get("value", []):
            info = acct["account"]["data"]["parsed"]["info"]
            total += int(info["tokenAmount"]["amount"])
        return total

    def _send_and_confirm(self, tx_b64: str) -> str:
        raw = base64.b64decode(tx_b64)
        tx = self._VersionedTransaction.from_bytes(raw)
        signed = self._VersionedTransaction(tx.message, [self.keypair])
        sig = self._rpc(
            "sendTransaction",
            [base64.b64encode(bytes(signed)).decode(), {"encoding": "base64", "maxRetries": 5}],
        )
        deadline = time.time() + 90
        while time.time() < deadline:
            status = self._rpc("getSignatureStatuses", [[sig]])["value"][0]
            if status:
                if status.get("err"):
                    raise RuntimeError(f"transaction {sig} failed: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return sig
            time.sleep(2)
        raise RuntimeError(f"transaction {sig} not confirmed within 90s")

    def buy(self, mint: str, lamports: int) -> Optional[Tuple[int, int, str]]:
        q = jupiter.get_quote(self.session, jupiter.SOL_MINT, mint, lamports, self.cfg.slippage_bps)
        if not q:
            return None
        before = self.token_balance(mint)
        tx = jupiter.build_swap_tx(self.session, q, self.pubkey)
        if not tx:
            log.warning("could not build swap transaction for %s", mint)
            return None
        sig = self._send_and_confirm(tx)
        tokens = 0
        for _ in range(6):
            time.sleep(2)
            tokens = self.token_balance(mint) - before
            if tokens > 0:
                break
        if tokens <= 0:
            tokens = int(q["outAmount"])
            log.warning("could not observe token balance change for %s; using quoted amount", mint)
        return tokens, lamports, sig

    def sell(self, mint: str, tokens: int) -> Optional[Tuple[int, str]]:
        q = jupiter.get_quote(self.session, mint, jupiter.SOL_MINT, tokens, self.cfg.slippage_bps)
        if not q:
            return None
        before = self.sol_balance()
        tx = jupiter.build_swap_tx(self.session, q, self.pubkey)
        if not tx:
            return None
        sig = self._send_and_confirm(tx)
        received = 0
        for _ in range(6):
            time.sleep(2)
            received = self.sol_balance() - before
            if received > 0:
                break
        if received <= 0:
            received = int(q["outAmount"])
            log.warning("could not observe SOL balance change for %s; using quoted amount", mint)
        return received, sig
