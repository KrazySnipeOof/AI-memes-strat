from __future__ import annotations

"""Funder-graph resolver: a token's deployer wallet -> the wallet that funded it.

Ruggers rotate the deployer wallet every launch (see [[memebot-deployer-reputation]]:
~1% deployer repeat), but the SOL to fund each fresh deployer often comes from a
repeating source. `resolve_funder` finds that source: the deployer's OLDEST
transaction is where it first received SOL, and the account that lost SOL in
that tx is the funder.

Caveat the caller must handle: a funder that is a CEX hot wallet (Binance etc.)
funds millions of wallets and is NOT a rugger signal - classify by how many of
OUR tokens it funds + its total tx count before trusting it.
"""

import time


def _rpc(session, rpc_url, method, params, retries=3):
    for attempt in range(retries):
        try:
            r = session.post(rpc_url, json={"jsonrpc": "2.0", "id": 1,
                                            "method": method, "params": params}, timeout=15)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            j = r.json()
            return j.get("result")
        except Exception:
            time.sleep(0.6 * (attempt + 1))
    return None


def oldest_signature(session, rpc_url, wallet, max_pages=4):
    """Earliest signature involving `wallet` (paginates back via `before`)."""
    before, oldest = None, None
    for _ in range(max_pages):
        params = [wallet, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        sigs = _rpc(session, rpc_url, "getSignaturesForAddress", params)
        if not sigs:
            break
        oldest = sigs[-1]["signature"]
        if len(sigs) < 1000:
            break
        before = oldest
    return oldest


def resolve_funder(session, rpc_url, wallet):
    """(funder_addr, sol_in) for `wallet`, or (None, 0). The funder = the account
    that lost the most SOL in the wallet's oldest transaction (i.e. sent it its
    first lamports)."""
    sig = oldest_signature(session, rpc_url, wallet)
    if not sig:
        return None, 0.0
    tx = _rpc(session, rpc_url, "getTransaction",
              [sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}])
    if not tx:
        return None, 0.0
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    addrs = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
    # deployer's inflow, and the account with the most-negative delta = funder
    funder, worst = None, 0
    for i, a in enumerate(addrs):
        if a == wallet or i >= len(pre) or i >= len(post):
            continue
        delta = post[i] - pre[i]
        if delta < worst:
            worst, funder = delta, a
    return funder, abs(worst) / 1e9


def wallet_tx_count(session, rpc_url, wallet):
    """Rough activity level: len of the first (recent) signature page, capped at
    1000. A funder near 1000 is a high-traffic wallet (likely a CEX/router, not a
    private rugger master)."""
    sigs = _rpc(session, rpc_url, "getSignaturesForAddress", [wallet, {"limit": 1000}])
    return len(sigs) if sigs else 0
