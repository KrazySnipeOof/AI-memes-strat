from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("memebot.jupiter")

SOL_MINT = "So11111111111111111111111111111111111111112"

QUOTE_URLS = (
    "https://lite-api.jup.ag/swap/v1/quote",
    "https://quote-api.jup.ag/v6/quote",
)
SWAP_URLS = (
    "https://lite-api.jup.ag/swap/v1/swap",
    "https://quote-api.jup.ag/v6/swap",
)


def get_quote(session, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> Optional[dict]:
    if amount <= 0:
        return None
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(int(amount)),
        "slippageBps": str(int(slippage_bps)),
        "swapMode": "ExactIn",
    }
    for url in QUOTE_URLS:
        try:
            r = session.get(url, params=params, timeout=12)
            if r.status_code != 200:
                continue
            q = r.json()
            if q and q.get("outAmount"):
                return q
        except Exception as exc:
            log.debug("quote via %s failed: %s", url, exc)
    return None


def build_swap_tx(session, quote: dict, user_pubkey: str) -> Optional[str]:
    body = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    fallback = {k: v for k, v in body.items() if k != "prioritizationFeeLamports"}
    for url in SWAP_URLS:
        for payload in (body, fallback):
            try:
                r = session.post(url, json=payload, timeout=20)
                if r.status_code != 200:
                    continue
                tx = (r.json() or {}).get("swapTransaction")
                if tx:
                    return tx
            except Exception as exc:
                log.debug("swap build via %s failed: %s", url, exc)
    return None
