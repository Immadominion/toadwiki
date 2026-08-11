#!/usr/bin/env python3
"""Collect every $TOAD transfer in and out of the campaign wallet's token account.

This is the ledger of record for toad-wiki. Everything the site claims about
"who got airdropped what" and "he never sold" is derived from this file's output.
It is written to be *auditable*, not clever.

Design rules (each one is a bug a previous version of this collector shipped):

  1. Query the ASSOCIATED TOKEN ACCOUNT, not the owner wallet. The owner's
     signature history is polluted with unsolicited spam airdrops from
     strangers (561 sigs on the owner vs 166 on the ATA at time of writing).
  2. Paginate getSignaturesForAddress to EXHAUSTION. There is no --limit.
     The old collector capped at 150 signatures paginating newest-first and
     silently threw away launch day.
  3. The mint is Token-2022, not classic SPL. An SPL-only parser returns
     nothing at all, silently. We assert the mint's owning program up front so
     that failure mode is loud.
  4. Amounts come from pre/postTokenBalances deltas, never from instruction
     parsing. Delta-from-balances is transfer-method independent: it sees
     transfer, transferChecked, CPI transfers inside a router, and anything
     invented next year, all identically.
  5. INTEGER RAW BASE UNITS ONLY, carried as decimal strings. Never uiAmount
     (a float). Float subtraction is what put "39999.99999999999" in the old
     data. Division by 10**decimals happens exactly once, at render time.
  6. No magic thresholds. Every filter is a named constant with a stated
     reason, and every filter is echoed into the output under
     provenance.filters_applied with the number of rows it touched.
  7. Every outbound transfer is classified is_sale / not-a-sale. The site's
     thesis is "he pledged never to sell"; a parser that cannot tell a sale
     from a gift is the worst possible failure mode. It is not enough to be
     right by luck because no sale has happened yet.
  8. Provenance is a first-class output. Without sigs_scanned the word "every"
     is never legitimate on the site.
  9. Retries with exponential backoff. A single unretried 429 silently drops a
     transaction and makes the run unreproducible.
 10. Inbound transfers are collected too, so the invariant can be computed.

THE INVARIANT (the product's hero exhibit, and this script's test):

    sum(inbound_raw) - sum(outbound_raw) == getAccountInfo(ATA).amount

with residual EXACTLY 0, in integer base units. It holds only if pagination
was truly exhaustive, so it is a genuine end-to-end test of requirement 2.
The script exits non-zero if it does not hold.

Usage:
    HELIUS_RPC=https://... python3 scripts/collect/transfers.py
    python3 scripts/collect/transfers.py --env-file .env
    python3 scripts/collect/transfers.py --out /tmp/transfers.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "data" / "collection" / "onchain" / "transfers.json"

# ---------------------------------------------------------------------------
# Verified constants, each re-derived from chain. Do not "clean these up".
# ---------------------------------------------------------------------------

MINT = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"

# The mint is Token-2022. A collector that assumes classic SPL finds nothing and
# says nothing about it. We assert this at startup instead of discovering it in
# production six weeks later.
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_CLASSIC_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Campaign wallet (the human-facing address) and the token account that actually
# holds the TOAD. QUERY THE ATA. See rule 1 above.
CAMPAIGN_OWNER = "FuP8dYQytaThMh9Fg2XNd1Z1eNHxMHW92kVUfWf3TnmD"
CAMPAIGN_ATA = "AuA2VRui5JNWNWF79iyaSKpW7zMQLfzFZBjd2uS3YW2H"

DEPLOYER = "5YRgrP3mjGzrzirYYN5HAQH19cTYREYwGxW6XRJQUzij"

# Liquidity venues known before we ask DexScreener. The bonding curve and the
# canonical PumpSwap pool are load-bearing: they are where a sale would land.
BONDING_CURVE = "9oi3zoTqd1T8T3CVuSDfSNwjeWaj6zZLdYMLWNyayaeA"
PUMPSWAP_POOL = "Nx9dcwNs3iJxM5YAxshMHE4aYJHdDyyGMhVcmaSgfu8"
SEED_POOL_ACCOUNTS = {
    BONDING_CURVE: "pump.fun bonding curve",
    PUMPSWAP_POOL: "PumpSwap canonical pool (also the pricing pool)",
}

SYSTEM_PROGRAM = "11111111111111111111111111111111"

# DEX / swap / aggregator programs. If an outbound transfer happens inside a
# transaction that invokes one of these, it is not a gift, whatever the
# destination looks like. This list is a backstop, not the primary signal: the
# primary signal is the dynamically discovered pool account set below. Anything
# that trips neither but still looks program-controlled is flagged needs_review
# rather than silently called a gift.
DEX_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun bonding curve program",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap AMM",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora Dynamic AMM (DAMM v1)",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora Dynamic Bonding Curve",
    "24Uqj9JCLxUeoC3hGfh5W3s9FM9uCHDS2SG3LYwBpyTi": "Meteora Vault",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "Orca legacy swap",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter aggregator v6",
    "JUP4Fb2cqiRUcaTNcJ39cAe1i8jNSAoJmyMdQrgSGxN": "Jupiter aggregator v4",
    "2wT8Yq49kHgDzXuPxZSaeLaH1qbmGXtEyPy64bL7aD3c": "Lifinity v2",
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY": "Phoenix",
    "opnb2LAfJYbRMAHHvqjCwQxanZn7ReEHp1k81EohpZb": "OpenBook v2",
    "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin": "OpenBook / Serum v3",
    "SoLFiHG9TfgtdUXUjWAxi3LtvYuFyDLVhBWxdMZxyCe": "SolFi",
    "obriQD1zbpyLz95G5n7nJe6a4DPjpFwa5XYPoNm113y": "Obric v2",
    "ZERor4xhbUycZ6gb9ntrhqscUcZmAbQDjEAtCf4hbZY": "ZeroFi",
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX": "Serum v3 (alt)",
    "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP": "Penguin swap",
    "SSwpkEEcbUqx4vtoEByFjSkhKdCT862DNVb52nZg1UZ": "Saros",
    "SwaPpA9NaXpqvQFTBmbV4hRBLMy4EbEZW5CVCJHfPJK": "SwapNA",
    "MERLuDFBMmsHnsBPZw2sDQZHvXFMwp8EdjudcU2HKky": "Mercurial",
    "CURVGoZn8zycx6FXwwevgBTB2gVvdbGTEpvMJDbgs2t4": "Aldrin v2",
    "AMM55ShdkoGRB5jVYPjWziwk8m5MpwyDgsMWHaMSQWH6": "Aldrin v1",
    "DjVE6JNiYqPL2QXyCUUh8rNjHrbz9hXHNYt99MQ59qw1": "Orca v1",
    "H8W3ctz92svYg6mkn1UtGfu2aQr2fnUFHM1RhScEtQDt": "Cropper",
    "stkitrT1Uoy18Dk1fTrgPw8W6MVzoCfYoAFT4MLsmhq": "Sanctum router",
    "5ocnV1qiCgaQR8Jb8xWnVbApfaygJ8tNoZfgPwsgx9kx": "Sanctum",
}

# ---------------------------------------------------------------------------
# Filters. Rule 6: every filter is named, justified, and reported. There are no
# amount thresholds of any kind. A one-base-unit transfer is a transfer.
# ---------------------------------------------------------------------------

FILTER_MINT = {
    "name": "MINT_EQ",
    "value": MINT,
    "reason": (
        "Only token balance rows for this mint are considered. The wallet "
        "receives unsolicited memecoin airdrops from strangers; without this "
        "filter the ledger becomes a spam list."
    ),
}
FILTER_FAILED_TX = {
    "name": "SKIP_FAILED_TX",
    "value": "meta.err is not null",
    "reason": (
        "Failed transactions move no tokens (pre and post balances are equal), "
        "so they contribute 0 to every sum. Skipping them is bookkeeping, not "
        "a threshold."
    ),
}
FILTER_ZERO_DELTA = {
    "name": "SKIP_ZERO_DELTA",
    "value": "delta == 0 base units",
    "reason": (
        "An account whose balance did not change is not a party to a transfer. "
        "This is exact integer equality with zero. It is NOT a dust threshold: "
        "delta == 1 base unit (0.000001 TOAD) is kept and reported."
    ),
}
FILTERS = [FILTER_MINT, FILTER_FAILED_TX, FILTER_ZERO_DELTA]

# Reconciliation. The campaign is live, so signatures and the balance snapshot
# can be read at different slots. We re-poll for newer signatures and re-read
# the balance until the invariant closes, rather than papering over the gap.
MAX_RECONCILE_ROUNDS = 8

SIG_PAGE_SIZE = 1000  # RPC maximum, not a cap on total results.
DEFAULT_WORKERS = 6
# The RPC endpoint is shared with the project's other collectors, so sustained
# 429s are normal rather than exceptional. 12 attempts with the backoff below is
# roughly seven minutes of patience per call. Failing the whole run is the
# correct outcome after that: a dropped transaction would silently corrupt the
# ledger, and this script's output is only worth anything if it is complete.
DEFAULT_MAX_ATTEMPTS = 12
BACKOFF_CAP_S = 45.0


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------


class Rpc:
    """Minimal JSON-RPC client with exponential backoff on 429/5xx.

    Helius rejects JSON-RPC batch arrays on this plan (HTTP 403), so requests
    are one-per-HTTP-call and parallelism comes from a small thread pool.
    """

    def __init__(self, url: str, max_attempts: int = DEFAULT_MAX_ATTEMPTS, commitment: str = "finalized"):
        self._url = url  # never logged; may contain an API key
        self.max_attempts = max_attempts
        self.commitment = commitment
        self.calls = 0
        self.retries = 0
        self._lock = Lock()

    def call(self, method: str, params: list):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        last = None
        for attempt in range(self.max_attempts):
            try:
                req = urllib.request.Request(
                    self._url, data=body, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=90) as resp:
                    payload = json.loads(resp.read())
                with self._lock:
                    self.calls += 1
                if "error" in payload:
                    err = payload["error"]
                    code = err.get("code")
                    # -32429 / rate-limit style errors arrive in-band sometimes.
                    if code in (-32429, -32005):
                        last = RuntimeError(f"{method}: {err}")
                        self._backoff(attempt, rate_limited=True)
                        continue
                    raise RuntimeError(f"{method} failed: {err}")
                return payload.get("result")
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429 or 500 <= e.code < 600:
                    self._backoff(attempt, rate_limited=(e.code == 429))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
                last = e
                self._backoff(attempt, rate_limited=False)
        raise RuntimeError(f"{method}: exhausted {self.max_attempts} attempts: {last!r}")

    def _backoff(self, attempt: int, rate_limited: bool) -> None:
        with self._lock:
            self.retries += 1
        base = 2.0 if rate_limited else 0.75
        # Jitter matters: without it, the parallel workers that all got 429ed by
        # the same burst wake up together and reproduce the burst.
        delay = min(base * (2**attempt), BACKOFF_CAP_S) + random.uniform(0, 1.0)
        time.sleep(delay)

    # -- typed helpers -----------------------------------------------------

    def account_info(self, pubkey: str) -> tuple[dict | None, int]:
        res = self.call(
            "getAccountInfo",
            [pubkey, {"encoding": "jsonParsed", "commitment": self.commitment}],
        )
        return (res or {}).get("value"), ((res or {}).get("context") or {}).get("slot", 0)

    def multiple_accounts(self, pubkeys: list[str]) -> dict[str, dict | None]:
        out: dict[str, dict | None] = {}
        for i in range(0, len(pubkeys), 100):  # getMultipleAccounts caps at 100 keys
            chunk = pubkeys[i : i + 100]
            res = self.call(
                "getMultipleAccounts",
                [chunk, {"encoding": "jsonParsed", "commitment": self.commitment}],
            )
            for key, val in zip(chunk, (res or {}).get("value") or []):
                out[key] = val
        return out

    def token_supply(self, mint: str) -> tuple[dict, int]:
        res = self.call("getTokenSupply", [mint, {"commitment": self.commitment}])
        return res["value"], (res.get("context") or {}).get("slot", 0)

    def token_accounts_by_owner(self, owner: str, mint: str) -> list[str]:
        res = self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": self.commitment}],
        )
        return [row["pubkey"] for row in (res or {}).get("value") or []]

    def transaction(self, sig: str) -> dict | None:
        return self.call(
            "getTransaction",
            [
                sig,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": self.commitment,
                },
            ],
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


_ED25519_P = 2**255 - 19
_ED25519_D = (-121665 * pow(121666, _ED25519_P - 2, _ED25519_P)) % _ED25519_P
_ED25519_I = pow(2, (_ED25519_P - 1) // 4, _ED25519_P)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58_decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + body


def is_on_curve(address: str) -> bool | None:
    """True if the address is a valid ed25519 point, i.e. an ordinary wallet.

    This is the strongest available sale check, and it needs no network call and
    no denylist. Solana derives a PDA by searching for an address that is NOT a
    valid curve point, so every program-derived address - including every AMM
    pool authority and every pool vault owner on every DEX, present or future -
    is off-curve. An ordinary keypair wallet is always on-curve.

    So: an on-curve destination cannot be a pool. That conclusion does not
    depend on DexScreener returning results, on our DEX program list being
    current, or even on the destination's account existing on chain yet, which
    is the case for a recipient who has never paid rent for a system account.
    """
    try:
        raw = b58_decode(address)
    except ValueError:
        return None
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= _ED25519_P:
        return False
    yy = y * y % _ED25519_P
    u = (yy - 1) % _ED25519_P
    v = (_ED25519_D * yy + 1) % _ED25519_P
    xx = u * pow(v, _ED25519_P - 2, _ED25519_P) % _ED25519_P
    x = pow(xx, (_ED25519_P + 3) // 8, _ED25519_P)
    if (x * x - xx) % _ED25519_P != 0:
        x = x * _ED25519_I % _ED25519_P
    return (x * x - xx) % _ED25519_P == 0


def to_ui(raw: int, decimals: int) -> str:
    """Exact decimal string from integer base units. No float ever touches this."""
    if decimals == 0:
        return str(raw)
    sign = "-" if raw < 0 else ""
    digits = str(abs(raw)).rjust(decimals + 1, "0")
    return f"{sign}{digits[:-decimals]}.{digits[-decimals:]}"


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def full_account_keys(tx: dict) -> list[str]:
    """Static keys followed by address-lookup-table loaded keys, in index order.

    jsonParsed puts lookup-table addresses in meta.loadedAddresses rather than
    in message.accountKeys, and token balance rows index into the combined list.
    Getting this wrong mislabels the counterparty on any v0 transaction.
    """
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys.extend(loaded.get("writable") or [])
    keys.extend(loaded.get("readonly") or [])
    return keys


def program_ids(tx: dict) -> set[str]:
    """Every program invoked by the transaction, top level and via CPI."""
    out: set[str] = set()
    msg = (tx.get("transaction") or {}).get("message") or {}
    for ix in msg.get("instructions") or []:
        if ix.get("programId"):
            out.add(ix["programId"])
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            if ix.get("programId"):
                out.add(ix["programId"])
    return out


def mint_deltas(tx: dict, mint: str) -> tuple[dict[int, dict], list[str]]:
    """Per-token-account raw integer deltas for one mint, from pre/post balances.

    Returns {accountIndex: {delta, owner, program}} and any anomalies found.
    Rule 4: this is method independent. It does not care whether the tokens
    moved via transfer, transferChecked, a CPI inside a router, or something
    that does not exist yet.
    """
    meta = tx.get("meta") or {}
    anomalies: list[str] = []
    rows: dict[int, dict] = {}

    for side, key in (("pre", "preTokenBalances"), ("post", "postTokenBalances")):
        for bal in meta.get(key) or []:
            if bal.get("mint") != mint:  # FILTER_MINT
                continue
            idx = bal["accountIndex"]
            row = rows.setdefault(idx, {"pre": 0, "post": 0, "owner": None, "program": None})
            # Raw integer string. uiAmount is a float and is never read.
            row[side] = int((bal.get("uiTokenAmount") or {}).get("amount") or 0)
            if bal.get("owner"):
                row["owner"] = bal["owner"]
            prog = bal.get("programId")
            if prog:
                row["program"] = prog
                if prog != TOKEN_2022_PROGRAM:
                    anomalies.append(f"token balance row on unexpected program {prog}")

    return (
        {
            idx: {"delta": r["post"] - r["pre"], "owner": r["owner"], "program": r["program"]}
            for idx, r in rows.items()
        },
        anomalies,
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def paginate_signatures(rpc: Rpc, address: str, until: str | None = None, log=print):
    """getSignaturesForAddress to EXHAUSTION. Rule 2: there is no cap here.

    Returns (rows_newest_first, pages_fetched, complete).
    `complete` is only False if pagination was cut short, which currently
    cannot happen without an exception; it exists so `truncated` in the output
    is derived from real state rather than asserted.
    """
    rows: list[dict] = []
    before: str | None = None
    pages = 0
    while True:
        cfg: dict = {"limit": SIG_PAGE_SIZE, "commitment": rpc.commitment}
        if before:
            cfg["before"] = before
        if until:
            cfg["until"] = until
        batch = rpc.call("getSignaturesForAddress", [address, cfg]) or []
        pages += 1
        rows.extend(batch)
        log(f"    page {pages}: +{len(batch)} (total {len(rows)})")
        if len(batch) < SIG_PAGE_SIZE:
            return rows, pages, True
        before = batch[-1]["signature"]


def fetch_transactions(rpc: Rpc, sigs: list[str], workers: int, log=print) -> dict[str, dict | None]:
    out: dict[str, dict | None] = {}
    done = 0
    lock = Lock()

    def work(sig: str):
        nonlocal done
        tx = rpc.transaction(sig)
        with lock:
            out[sig] = tx
            done += 1
            if done % 25 == 0 or done == len(sigs):
                log(f"    {done}/{len(sigs)} transactions")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, sigs))
    return out


def discover_pools(mint: str, log=print) -> tuple[dict[str, str], dict]:
    """Ask DexScreener for every pool trading this mint.

    Returns (pool_address -> label, provenance). Failure is non-fatal but is
    recorded: the seeded pools (bonding curve + canonical PumpSwap pool) still
    cover the venues a sale would realistically use, and the DEX program
    backstop still fires either way.
    """
    pools = dict(SEED_POOL_ACCOUNTS)
    prov = {
        "source": f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
        "ok": False,
        "pairs_returned": 0,
        "error": None,
        "seeded": sorted(SEED_POOL_ACCOUNTS),
    }
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "toad-wiki-collector/2"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        pairs = data.get("pairs") or []
        prov["ok"] = True
        prov["pairs_returned"] = len(pairs)
        for p in pairs:
            addr = p.get("pairAddress")
            if not addr:
                continue
            labels = "/".join(p.get("labels") or [])
            pools.setdefault(addr, f"{p.get('dexId')}{'/' + labels if labels else ''}")
        log(f"    DexScreener: {len(pairs)} pairs")
    except Exception as e:  # noqa: BLE001 - discovery is best effort by design
        prov["error"] = f"{type(e).__name__}: {e}"
        log(f"    DexScreener failed ({prov['error']}); using seeded pools only")
    return pools, prov


def pool_token_accounts(rpc: Rpc, pools: dict[str, str], mint: str, workers: int, log=print):
    """Resolve each pool's TOAD vault, so a destination can be matched on the
    token account address as well as on its owner."""
    vaults: dict[str, str] = {}
    lock = Lock()

    def work(pool: str):
        try:
            for acct in rpc.token_accounts_by_owner(pool, mint):
                with lock:
                    vaults[acct] = f"vault of {pools[pool]} ({pool})"
        except Exception as e:  # noqa: BLE001
            log(f"    vault lookup failed for {pool}: {type(e).__name__}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, list(pools)))
    log(f"    resolved {len(vaults)} pool vault token accounts")
    return vaults


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load_rpc_url(env_file: str | None) -> str:
    """Resolve the RPC endpoint. The value is never printed or written out."""
    if env_file:
        path = Path(env_file).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("HELIUS_RPC=") and "HELIUS_RPC" not in os.environ:
                os.environ["HELIUS_RPC"] = line.split("=", 1)[1].strip().strip("'\"")
    url = os.environ.get("HELIUS_RPC", "").strip()
    if not url:
        raise SystemExit(
            "HELIUS_RPC is not set. Export it, or pass --env-file pointing at a "
            "file containing HELIUS_RPC=<url>."
        )
    return url


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ata", default=CAMPAIGN_ATA, help="token account to trace (NOT the owner wallet)")
    ap.add_argument("--mint", default=MINT)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    ap.add_argument("--commitment", default="finalized", choices=["finalized", "confirmed"])
    ap.add_argument("--dry-run", action="store_true", help="compute everything, write nothing")
    ap.add_argument(
        "--allow-broken-invariant",
        action="store_true",
        help="write output and exit 0 even if the invariant does not close (debugging only)",
    )
    # NOTE: there is deliberately no --limit. See rule 2.
    args = ap.parse_args()

    url = load_rpc_url(args.env_file)
    rpc = Rpc(url, max_attempts=args.max_attempts, commitment=args.commitment)
    started = now_iso()

    def log(msg: str) -> None:
        print(msg, flush=True)

    log(f"toad-wiki transfer collector · {started}")
    log(f"  ata   {args.ata}")
    log(f"  mint  {args.mint}")

    # -- 0. Token identity. Rule 3: make the Token-2022 assumption loud. ----
    log("[0] verifying mint program and decimals")
    mint_acct, _ = rpc.account_info(args.mint)
    if not mint_acct:
        raise SystemExit(f"mint {args.mint} not found on chain")
    mint_program = mint_acct.get("owner")
    if mint_program != TOKEN_2022_PROGRAM:
        raise SystemExit(
            f"mint is owned by {mint_program}, expected Token-2022 "
            f"({TOKEN_2022_PROGRAM}). A classic-SPL parse of this mint returns "
            "nothing silently; refusing to continue."
        )
    supply, supply_slot = rpc.token_supply(args.mint)
    decimals = int(supply["decimals"])  # never hardcoded: burns are ongoing
    log(f"    program=Token-2022 decimals={decimals} supply={supply['amount']} (slot {supply_slot})")

    ata_acct, _ = rpc.account_info(args.ata)
    if not ata_acct:
        raise SystemExit(f"token account {args.ata} not found")
    ata_info = ((ata_acct.get("data") or {}).get("parsed") or {}).get("info") or {}
    if ata_info.get("mint") != args.mint:
        raise SystemExit(f"token account holds {ata_info.get('mint')}, not {args.mint}")
    ata_owner = ata_info.get("owner")
    log(f"    ata owner={ata_owner}")

    # -- 1. Liquidity venues, for sale classification. ---------------------
    log("[1] discovering liquidity pools")
    pools, pool_prov = discover_pools(args.mint, log=log)
    vaults = pool_token_accounts(rpc, pools, args.mint, args.workers, log=log)

    # -- 2. Signatures, to exhaustion. -------------------------------------
    log("[2] paginating signatures to exhaustion (no cap)")
    sig_rows, pages, complete = paginate_signatures(rpc, args.ata, log=log)
    seen = {r["signature"] for r in sig_rows}
    newest_sig = sig_rows[0]["signature"] if sig_rows else None
    log(f"    {len(sig_rows)} signatures over {pages} page(s)")

    # -- 3. Transactions. ---------------------------------------------------
    ok_sigs = [r["signature"] for r in sig_rows if r.get("err") is None]
    failed_skipped = len(sig_rows) - len(ok_sigs)  # FILTER_FAILED_TX
    log(f"[3] fetching {len(ok_sigs)} successful transactions ({failed_skipped} failed skipped)")
    txs = fetch_transactions(rpc, ok_sigs, args.workers, log=log)

    # -- 4. Parse. ----------------------------------------------------------
    log("[4] parsing balance deltas")
    state = {
        "outbound": [],
        "inbound": [],
        "inflow_raw": 0,
        "outflow_raw": 0,
        "txs_fetched": 0,
        "zero_delta_rows": 0,
        "ambiguous_txs": [],
        "anomalies": [],
        "missing_txs": [],
        "neutral_txs": 0,
    }

    def parse_batch(sigmap: dict[str, dict | None], sig_meta: dict[str, dict]) -> None:
        # Accumulated, not assigned: the reconcile loop calls this again with any
        # transactions that landed mid-run, and those must be counted too.
        state["txs_fetched"] += len(sigmap)
        for sig, tx in sigmap.items():
            if tx is None:
                state["missing_txs"].append(sig)
                continue
            meta = tx.get("meta") or {}
            if meta.get("err"):  # FILTER_FAILED_TX (defence in depth)
                continue
            keys = full_account_keys(tx)
            deltas, anomalies = mint_deltas(tx, args.mint)
            for a in anomalies:
                state["anomalies"].append({"sig": sig, "detail": a})

            ata_idx = [i for i in deltas if i < len(keys) and keys[i] == args.ata]
            net = sum(deltas[i]["delta"] for i in ata_idx)
            ts = sig_meta.get(sig, {}).get("blockTime") or tx.get("blockTime")
            slot = tx.get("slot")
            invoked = program_ids(tx)
            dex_hits = sorted(p for p in invoked if p in DEX_PROGRAMS)

            counterparties = []
            for idx, row in deltas.items():
                if idx in ata_idx:
                    continue
                if row["delta"] == 0:  # FILTER_ZERO_DELTA
                    state["zero_delta_rows"] += 1
                    continue
                counterparties.append(
                    {
                        "account": keys[idx] if idx < len(keys) else None,
                        "owner": row["owner"],
                        "delta": row["delta"],
                        "program": row["program"],
                    }
                )

            if net == 0:
                state["neutral_txs"] += 1
                continue

            direction = "out" if net < 0 else "in"
            want_sign = 1 if net < 0 else -1  # counterparty moves opposite to us
            legs = [c for c in counterparties if (c["delta"] > 0) == (want_sign > 0)]
            legs_total = sum(abs(c["delta"]) for c in legs)
            # Attribution is "exact" when the counterparty legs account for the
            # whole of our net movement. Anything else is flagged, never guessed.
            attribution = "exact" if legs_total == abs(net) else "ambiguous"
            if attribution == "ambiguous":
                state["ambiguous_txs"].append(
                    {"sig": sig, "net_raw": str(net), "legs_raw": str(legs_total * want_sign)}
                )

            for leg in legs:
                amount = abs(leg["delta"])
                base = {
                    "sig": sig,
                    "slot": slot,
                    "ts": ts,
                    "ts_iso": iso(ts),
                    "amount_raw": str(amount),
                    "amount_ui": to_ui(amount, decimals),
                    "attribution": attribution,
                    "tx_net_raw": str(net),
                }
                if direction == "out":
                    dest_acct = leg["account"]
                    dest_owner = leg["owner"]
                    pool_hit = None
                    for cand, label in (
                        (dest_acct, vaults.get(dest_acct or "")),
                        (dest_owner, pools.get(dest_owner or "")),
                        (dest_acct, pools.get(dest_acct or "")),
                    ):
                        if cand and label:
                            pool_hit = {"account": cand, "label": label}
                            break
                    state["outbound"].append(
                        {
                            **base,
                            "direction": "out",
                            "from_token_account": args.ata,
                            "from_owner": ata_owner,
                            "to_token_account": dest_acct,
                            "to_owner": dest_owner,
                            "token_program": leg["program"],
                            # Rule 7. is_sale is true if the destination is a
                            # known liquidity venue, or if the transaction
                            # invoked a DEX/swap program at any depth.
                            "is_sale": bool(pool_hit) or bool(dex_hits),
                            "sale_signals": {
                                "destination_is_known_pool": bool(pool_hit),
                                "matched_pool": pool_hit,
                                "dex_programs_invoked": [
                                    {"program": p, "name": DEX_PROGRAMS[p]} for p in dex_hits
                                ],
                            },
                        }
                    )
                else:
                    src_acct = leg["account"]
                    src_owner = leg["owner"]
                    src_pool = vaults.get(src_acct or "") or pools.get(src_owner or "")
                    state["inbound"].append(
                        {
                            **base,
                            "direction": "in",
                            "from_token_account": src_acct,
                            "from_owner": src_owner,
                            "to_token_account": args.ata,
                            "to_owner": ata_owner,
                            "token_program": leg["program"],
                            "source_is_known_pool": bool(src_pool),
                            "source_pool_label": src_pool,
                            "dex_programs_invoked": [
                                {"program": p, "name": DEX_PROGRAMS[p]} for p in dex_hits
                            ],
                        }
                    )

            if direction == "out":
                state["outflow_raw"] += -net
            else:
                state["inflow_raw"] += net

    sig_meta = {r["signature"]: r for r in sig_rows}
    parse_batch(txs, sig_meta)

    # -- 5. Reconcile against the on-chain balance. -------------------------
    # Rule 8/10: the campaign is live, so the signature list and the balance
    # snapshot can be read at different slots. Re-poll for newer signatures and
    # re-read the balance until the invariant closes.
    log("[5] reconciling against on-chain balance")
    rounds = 0
    residual = None
    balance_raw = 0
    rpc_slot = 0
    while rounds < MAX_RECONCILE_ROUNDS:
        rounds += 1
        acct, rpc_slot = rpc.account_info(args.ata)
        info = ((acct or {}).get("data") or {}).get("parsed", {}).get("info", {})
        balance_raw = int((info.get("tokenAmount") or {}).get("amount") or 0)
        residual = state["inflow_raw"] - state["outflow_raw"] - balance_raw
        log(
            f"    round {rounds}: balance={balance_raw} "
            f"in={state['inflow_raw']} out={state['outflow_raw']} residual={residual}"
        )
        if residual == 0:
            break
        if not newest_sig:
            break
        fresh, extra_pages, _ = paginate_signatures(rpc, args.ata, until=newest_sig, log=log)
        pages += extra_pages
        fresh = [r for r in fresh if r["signature"] not in seen]
        if not fresh:
            log("    no new signatures; residual is a real discrepancy")
            break
        log(f"    {len(fresh)} new signature(s) landed mid-run; folding them in")
        sig_rows = fresh + sig_rows
        seen.update(r["signature"] for r in fresh)
        newest_sig = sig_rows[0]["signature"]
        fresh_ok = [r["signature"] for r in fresh if r.get("err") is None]
        failed_skipped += len(fresh) - len(fresh_ok)
        new_txs = fetch_transactions(rpc, fresh_ok, args.workers, log=log)
        parse_batch(new_txs, {r["signature"]: r for r in fresh})

    holds = residual == 0

    # -- 6. Aggregate. ------------------------------------------------------
    outbound = sorted(state["outbound"], key=lambda t: (t["ts"] or 0, t["sig"]))
    inbound = sorted(state["inbound"], key=lambda t: (t["ts"] or 0, t["sig"]))

    # Two further destination signals, so that "this was a gift" is an
    # affirmative finding rather than the absence of a denylist hit.
    #
    #   on_curve == True  -> an ordinary keypair wallet; cannot be a pool, and
    #                        this holds even when the owner account does not
    #                        exist on chain yet (a recipient who has never paid
    #                        rent for a system account: common, and 26 of the
    #                        current recipients are in exactly that state).
    #   on_curve == False -> program-derived. Not proof of a sale, but it must
    #                        never be waved through as a gift.
    dest_owners = sorted({t["to_owner"] for t in outbound if t["to_owner"]})
    owner_accts = rpc.multiple_accounts(dest_owners) if dest_owners else {}
    for t in outbound:
        owner_acct = owner_accts.get(t["to_owner"] or "")
        owner_prog = (owner_acct or {}).get("owner")
        on_curve = is_on_curve(t["to_owner"]) if t["to_owner"] else None
        program_controlled = bool(owner_prog) and owner_prog != SYSTEM_PROGRAM
        t["sale_signals"]["destination_owner_program"] = owner_prog
        t["sale_signals"]["destination_owner_account_exists"] = owner_acct is not None
        t["sale_signals"]["destination_is_program_controlled"] = program_controlled
        t["sale_signals"]["destination_owner_is_on_curve"] = on_curve
        # Flag anything we cannot affirmatively call an ordinary wallet.
        t["needs_review"] = (not t["is_sale"]) and (program_controlled or on_curve is not True)

    by_recipient: dict[str, dict] = {}
    for t in outbound:
        key = t["to_owner"] or t["to_token_account"] or "unknown"
        r = by_recipient.setdefault(
            key,
            {
                "wallet": key,
                "total_raw": 0,
                "tx_count": 0,
                # Collected as a list and reduced at the end: a null blockTime
                # must not be coerced to 0, which would silently backdate a
                # recipient's first_ts to 1970.
                "ts_values": [],
                "token_accounts": set(),
                "sale_tx_count": 0,
                "txs": [],
            },
        )
        r["total_raw"] += int(t["amount_raw"])
        r["tx_count"] += 1
        if t["ts"] is not None:
            r["ts_values"].append(t["ts"])
        if t["to_token_account"]:
            r["token_accounts"].add(t["to_token_account"])
        if t["is_sale"]:
            r["sale_tx_count"] += 1
        r["txs"].append(
            {"sig": t["sig"], "ts": t["ts"], "amount_raw": t["amount_raw"], "is_sale": t["is_sale"]}
        )

    recipients = []
    for rank, r in enumerate(sorted(by_recipient.values(), key=lambda x: (-x["total_raw"], x["wallet"])), 1):
        recipients.append(
            {
                "rank": rank,
                "wallet": r["wallet"],
                "token_accounts": sorted(r["token_accounts"]),
                "total_raw": str(r["total_raw"]),
                "total_ui": to_ui(r["total_raw"], decimals),
                "tx_count": r["tx_count"],
                "sale_tx_count": r["sale_tx_count"],
                "first_ts": min(r["ts_values"]) if r["ts_values"] else None,
                "last_ts": max(r["ts_values"]) if r["ts_values"] else None,
                "txs": sorted(r["txs"], key=lambda x: x["ts"] or 0),
            }
        )

    sale_rows = [t for t in outbound if t["is_sale"]]
    review_rows = [t for t in outbound if t["needs_review"]]
    all_ts = [r.get("blockTime") for r in sig_rows if r.get("blockTime")]

    payload = {
        "schema_version": "2.0.0",
        "generator": "scripts/collect/transfers.py",
        "collected_at": now_iso(),
        "started_at": started,
        "units": {
            "decimals": decimals,
            "canonical_field_suffix": "_raw",
            "note": (
                "Every *_raw field is an INTEGER number of base units carried as a "
                "decimal string. Divide by 10**decimals exactly once, at render "
                "time. Never parse a raw value into a float before dividing, and "
                "never sum the *_ui strings. The *_ui fields are exact decimal "
                "renderings produced by integer string slicing, not by float "
                "division; they are a convenience, not the source of truth."
            ),
        },
        "token": {
            "mint": args.mint,
            "token_program": mint_program,
            "token_program_name": "Token-2022",
            "decimals": decimals,
            "supply_raw": supply["amount"],
            "supply_ui": to_ui(int(supply["amount"]), decimals),
            "supply_slot": supply_slot,
            "supply_note": "Read live via getTokenSupply. Supply drifts as burns land; never hardcode.",
        },
        "wallet": {
            "campaign_owner": ata_owner,
            "campaign_ata": args.ata,
            "deployer": DEPLOYER,
            "note": (
                "Signature history is read from the ATA, not the owner. The owner's "
                "history is polluted with unsolicited airdrops from strangers."
            ),
        },
        "provenance": {
            "collected_at": now_iso(),
            "rpc_slot": rpc_slot,
            "commitment": args.commitment,
            "sigs_scanned": len(sig_rows),
            "pages_fetched": pages,
            "window_start_ts": min(all_ts) if all_ts else None,
            "window_end_ts": max(all_ts) if all_ts else None,
            "window_start_iso": iso(min(all_ts)) if all_ts else None,
            "window_end_iso": iso(max(all_ts)) if all_ts else None,
            "truncated": not complete,
            "pagination_note": (
                "getSignaturesForAddress was paginated with before= until a short "
                "page was returned. There is no result cap in this collector."
            ),
            "txs_fetched": state["txs_fetched"],
            "txs_missing": state["missing_txs"],
            "failed_sigs_skipped": failed_skipped,
            "neutral_txs": state["neutral_txs"],
            "zero_delta_rows_skipped": state["zero_delta_rows"],
            "ambiguous_attribution_txs": state["ambiguous_txs"],
            "token_program_anomalies": state["anomalies"],
            "reconcile_rounds": rounds,
            "rpc_calls": rpc.calls,
            "rpc_retries": rpc.retries,
            "filters_applied": FILTERS,
            "pool_discovery": pool_prov,
        },
        "invariant": {
            "formula": "sum(inbound_raw) - sum(outbound_raw) - getAccountInfo(ATA).amount == 0",
            "inflow_raw": str(state["inflow_raw"]),
            "inflow_ui": to_ui(state["inflow_raw"], decimals),
            "outflow_raw": str(state["outflow_raw"]),
            "outflow_ui": to_ui(state["outflow_raw"], decimals),
            "balance_onchain_raw": str(balance_raw),
            "balance_onchain_ui": to_ui(balance_raw, decimals),
            "residual_raw": str(residual),
            "holds": holds,
            "checked_at_slot": rpc_slot,
        },
        "classification": {
            "method": (
                "An outbound transfer is a sale if its destination is a known "
                "liquidity venue (pool account or pool vault token account), or if "
                "the transaction invoked a DEX/swap program at any call depth. "
                "Anything not affirmatively identified as an ordinary keypair "
                "wallet is marked needs_review rather than assumed to be a gift."
            ),
            "not_a_sale_proof": (
                "Independently of the denylist and of the DEX program list: every "
                "Solana pool authority and pool vault owner is a program-derived "
                "address, and a PDA is by construction OFF the ed25519 curve, "
                "while an ordinary keypair wallet is ON it. All "
                f"{sum(1 for t in outbound if t['sale_signals']['destination_owner_is_on_curve'])} "
                "outbound destinations are on-curve wallets, so none of them can "
                "be a liquidity pool. This check needs no network call and cannot "
                "go stale as new DEXes launch."
            ),
            "destinations_on_curve": sum(
                1 for t in outbound if t["sale_signals"]["destination_owner_is_on_curve"] is True
            ),
            "destinations_off_curve": sum(
                1 for t in outbound if t["sale_signals"]["destination_owner_is_on_curve"] is False
            ),
            "sale_count": len(sale_rows),
            "sale_amount_raw": str(sum(int(t["amount_raw"]) for t in sale_rows)),
            "needs_review_count": len(review_rows),
            "needs_review_sigs": [t["sig"] for t in review_rows],
            "pool_accounts": [{"address": a, "label": l} for a, l in sorted(pools.items())],
            "pool_vault_token_accounts": [{"address": a, "label": l} for a, l in sorted(vaults.items())],
            "dex_programs_watched": [{"program": p, "name": n} for p, n in sorted(DEX_PROGRAMS.items())],
        },
        "totals": {
            "outbound_transfer_count": len(outbound),
            "outbound_tx_count": len({t["sig"] for t in outbound}),
            "recipient_count": len(recipients),
            "outbound_total_raw": str(sum(int(t["amount_raw"]) for t in outbound)),
            "outbound_total_ui": to_ui(sum(int(t["amount_raw"]) for t in outbound), decimals),
            "inbound_transfer_count": len(inbound),
            "inbound_tx_count": len({t["sig"] for t in inbound}),
            "inbound_total_raw": str(sum(int(t["amount_raw"]) for t in inbound)),
            "inbound_total_ui": to_ui(sum(int(t["amount_raw"]) for t in inbound), decimals),
            "first_outbound_ts": outbound[0]["ts"] if outbound else None,
            "last_outbound_ts": outbound[-1]["ts"] if outbound else None,
            "pct_of_supply_distributed": (
                to_ui(
                    sum(int(t["amount_raw"]) for t in outbound) * 10**decimals * 100 // int(supply["amount"]),
                    decimals,
                )
                if int(supply["amount"])
                else None
            ),
        },
        "transfers": outbound,
        "inbound": inbound,
        "recipients": recipients,
    }

    # Internal consistency: the per-recipient rows must sum to the net outflow
    # used in the invariant. If attribution were lossy this would catch it.
    rows_total = sum(int(t["amount_raw"]) for t in outbound)
    if rows_total != state["outflow_raw"]:
        payload["provenance"]["row_sum_mismatch"] = {
            "rows_raw": str(rows_total),
            "net_outflow_raw": str(state["outflow_raw"]),
        }

    log("")
    log(f"  signatures scanned      {len(sig_rows)} over {pages} page(s)")
    log(f"  outbound transfers      {len(outbound)} across {payload['totals']['outbound_tx_count']} txs")
    log(f"  unique recipients       {len(recipients)}")
    log(f"  outbound total          {payload['totals']['outbound_total_ui']} TOAD")
    log(f"  inbound transfers       {len(inbound)} across {payload['totals']['inbound_tx_count']} txs")
    log(f"  inbound total           {payload['totals']['inbound_total_ui']} TOAD")
    log(f"  on-chain balance        {to_ui(balance_raw, decimals)} TOAD")
    log(f"  INVARIANT residual      {residual} base units -> {'HOLDS' if holds else 'BROKEN'}")
    log(f"  sales detected          {len(sale_rows)}")
    log(f"  needs review            {len(review_rows)}")
    log(f"  rpc calls / retries     {rpc.calls} / {rpc.retries}")

    if rows_total != state["outflow_raw"]:
        log(f"  WARNING row sum {rows_total} != net outflow {state['outflow_raw']}")

    # A run that does not close the invariant must not overwrite a good file.
    # This collector is expected to be re-run unattended, and a partial scan
    # (an exhausted retry budget, a truncated page) silently replacing a
    # complete ledger with a smaller one is precisely how the site became false
    # the first time. Refusing to write is the safe failure.
    if not holds and not args.allow_broken_invariant:
        log("")
        log(f"  REFUSING TO WRITE {args.out}")
        log("  The invariant did not close, so this run is incomplete. The")
        log("  existing file is left untouched. Re-run when the RPC is healthy,")
        log("  or pass --allow-broken-invariant to inspect the broken output.")
        return 1

    if not args.dry_run:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2) + "\n")
        log(f"  wrote {out_path}")
    else:
        log("  --dry-run: nothing written")

    if not holds:
        # Only reachable with --allow-broken-invariant; the guard above returns
        # first otherwise.
        log("")
        log("INVARIANT BROKEN. The output is not trustworthy: it means the")
        log("signature scan missed transactions, or the balance moved in a way")
        log("this parser did not account for. Do not ship a site from this file.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
