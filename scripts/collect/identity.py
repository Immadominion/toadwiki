#!/usr/bin/env python3
"""Identity / metadata collector for toad-wiki -- the evidence layer.

Answers "whose wallet is this" with displayable proof instead of assertion.
Every fact written here carries {value, source, method, verified} so the
frontend can render a citation next to it and so an unverified claim can
never be silently promoted to a verified one.

Sections (run individually with --only):
  metadata    1. DAS getAsset + verbatim IPFS JSON  (immutable description)
  authorities 2. mint authorities + Token-2022 extension enumeration
  pumpfun     3. bidirectional wallet <-> handle mapping
  proof       4. the wallet-proof exhibit (70K TOAD -> tweet, 63s later)
  timeline    5. launch tick-tock, every row re-verified on chain
  deployer    6. deployer's own record (sold? re-accumulated?)
  copycats    7. copycat mints riding the toad name

Outputs -> data/collection/identity/*.json

HARD-WON CONSTRAINTS baked into this script (do not "simplify" these away):
  * The mint is TOKEN-2022, not classic SPL. An SPL-only parser returns
    NOTHING and returns it silently. All parsing is program-agnostic and
    keys off the mint address in pre/postTokenBalances.
  * Query the campaign wallet's ATA, never the owner. The owner's history is
    polluted with spam airdrops from strangers (561 sigs vs 166 on the ATA).
  * Never paginate newest-first with a cap -- that silently discards the
    OLDEST rows, which is exactly where launch day lives. We always walk to
    the end of the signature list.
  * Supply drifts (burns are ongoing). Always getTokenSupply at build time.
  * Amounts are integers in raw base units end to end. Floats are derived
    for display only, never summed.

Usage:
    export HELIUS_RPC=...            # never printed, never written to disk
    python3 scripts/collect/identity.py
    python3 scripts/collect/identity.py --only proof,timeline

getTransaction responses are cached under <out>/.cache/txs/ (finalized txs
are immutable), so a run interrupted by an RPC credit cap resumes for free.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "collection" / "identity"

# ---------------------------------------------------------------- constants
MINT = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
DECIMALS = 6

CAMPAIGN_OWNER = "FuP8dYQytaThMh9Fg2XNd1Z1eNHxMHW92kVUfWf3TnmD"
CAMPAIGN_ATA = "AuA2VRui5JNWNWF79iyaSKpW7zMQLfzFZBjd2uS3YW2H"
DEPLOYER = "5YRgrP3mjGzrzirYYN5HAQH19cTYREYwGxW6XRJQUzij"
DEPLOYER_ATA = "8N4AmrpFTmQtMGniZguxgLU4W2f6pJ7KJdCnmJ3kmTZa"
BONDING_CURVE = "9oi3zoTqd1T8T3CVuSDfSNwjeWaj6zZLdYMLWNyayaeA"
PUMPSWAP_POOL = "Nx9dcwNs3iJxM5YAxshMHE4aYJHdDyyGMhVcmaSgfu8"
METADATA_CID = "bafkreifxw72yzkc2hjoktvce2kbtxh672ak2eqv47wvdrdnuchabpdb5za"

# The creator pubkey lives at byte offset 49 of the pump.fun BondingCurve account.
CURVE_CREATOR_OFFSET = 49

# Extensions whose ABSENCE is the strongest verifiable safety claim the site makes.
DANGEROUS_EXTENSIONS = [
    "transferFeeConfig",
    "transferHook",
    "permanentDelegate",
    "defaultAccountState",
    "confidentialTransferMint",
    "nonTransferable",
]
EXPECTED_EXTENSIONS = {"metadataPointer", "tokenMetadata"}

DESCRIPTION_PREFIX = "I will not run this coin. You are more than welcome to CTO it."

# The wallet-proof exhibit.
PROOF_TX = "4Syi2mXentmGLSTZva2DXSimP4hVwv5Yp4ktyi2K4rRArAyyasTDiovUhGLXb4Zr4uXrDqq9RLVkKSsAoqrfoiPU"
PROOF_RECIPIENT_OWNER = "64w4qRu9VGio7U1Asc6B68QDpS8L1McmSn2yyExC6Fii"
PROOF_RECIPIENT_TOKEN_ACCOUNT = "6eZa3oFQ1a3zqVkVpVjmhVJ7iWh1eYaa1i9c75kzz6x8"
PROOF_AMOUNT_RAW = 70_000_000_000
PROOF_TWEET_ID = "2086882992010440789"

MINT_CREATE_TX = "2euGYRDe9hTKArVFESQLkrvMV3gq3wtFHsZUPFmxGobUx4RRsCQoByVTqSCY3zgY79V22v1Wzae4ru5ye7xmmR3x"
CAMPAIGN_BUY_TX = "2VZz8dFP2C2R8FfwrintY5u1FuvNWbyCvx1SqJd5VtTqQxA5bzQknkBDDQH4UZgoUFuSQxfTuXvXa2ePSVo8a4k6"
DEPLOYER_SEND_TX = "DNe3AoKREpRnX9WqAD913Phx7SS9hJmtV3JCnyza9bZbZmukeaTbrrmjFQrDxv9sTMW2zrAGEq5tgxafrrvNjCc"
PAYMASTER = "FHpcNSe6tb2n15bAdq4BkeYWGyZKFD7yLYrH92ng7wCT"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Expected values are a FLOOR, not a target -- the campaign is live and counts
# grow. Fewer than this means a bug (almost certainly a pagination or
# token-program bug). The residual, however, must be EXACTLY zero, always.
EXPECTED_FLOOR = {
    "outbound_transfers": 162,
    "unique_recipients": 148,
    "outbound_raw": 20_260_555_000_000,
    "inbound_raw": 185_581_997_376_918,
    "inbound_txs": 4,
}

COPYCAT_QUERIES = ["toad", "toad pepe", "sapo pepe", "el sapo pepe", "toadpepe"]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs",
    "https://gateway.pinata.cloud/ipfs",
    "https://cloudflare-ipfs.com/ipfs",
]


# ------------------------------------------------------------------ helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_iso(ts) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ui(raw: int, decimals: int = DECIMALS) -> float:
    return raw / (10 ** decimals)


def ui_str(raw: int, decimals: int = DECIMALS) -> str:
    """Exact decimal string. Never let float formatting invent or drop digits."""
    neg = raw < 0
    raw = abs(raw)
    s = str(raw).rjust(decimals + 1, "0")
    out = f"{s[:-decimals]}.{s[-decimals:]}" if decimals else s
    return ("-" + out) if neg else out


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + s


class Secrets:
    """Redacts anything secret out of log lines and error strings."""

    values: list[str] = []

    @classmethod
    def add(cls, v: str | None):
        if v and len(v) > 8:
            cls.values.append(v)

    @classmethod
    def scrub(cls, s: str) -> str:
        for v in cls.values:
            s = s.replace(v, "<redacted>")
        # api keys ride in the RPC query string
        s = re.sub(r"(api-key=)[^&\s]+", r"\1<redacted>", s)
        return s


def log(msg: str):
    print(Secrets.scrub(str(msg)), flush=True)


def fact(value, source, method, verified, note=None):
    """Every displayable claim is wrapped so the UI can show its provenance."""
    d = {"value": value, "source": source, "method": method, "verified": bool(verified)}
    if note:
        d["note"] = note
    return d


class RpcExhausted(RuntimeError):
    """Provider credit/rate cap hit -- distinct from a transient error."""


# DAS is a Helius extension. A generic Solana RPC cannot serve these, so they
# are never retried against the fallback endpoint.
DAS_METHODS = {"getAsset", "getAssetsByOwner", "getAssetsByGroup",
               "getTokenAccounts", "searchAssets", "getAssetProof"}
DEFAULT_FALLBACK_RPC = "https://api.mainnet-beta.solana.com"


# --------------------------------------------------------------- http layer
def http_json(url, payload=None, headers=None, timeout=45, retries=4):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if payload is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    data = json.dumps(payload).encode() if payload is not None else None
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            return json.loads(body) if body else None, None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace")[:400]
            except Exception:
                pass
            last = f"HTTP {e.code}: {body}"
            if e.code in (400, 401, 403, 404):
                return None, last  # deterministic, do not retry
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    return None, Secrets.scrub(str(last))


class Rpc:
    """Paced, retrying, disk-cached JSON-RPC client.

    Pacing matters: firing concurrent unpaced batches at the provider trips a
    'max usage reached' credit cap that persists for minutes. Caching matters
    because that cap WILL be hit eventually and a rerun must not re-spend.
    """

    def __init__(self, url, cache_dir: Path, min_interval=0.25, use_cache=True,
                 fallback_url: str | None = None):
        self.url = url
        self.cache_dir = cache_dir
        self.use_cache = use_cache
        self.min_interval = min_interval
        # A public RPC keeps the run alive when the paid plan's credits run out.
        # It is slower and rate-limited, so it is only used after exhaustion and
        # never for DAS-only methods.
        self.fallback_url = fallback_url
        self.fallback_min_interval = 0.7
        self.fallback_calls = 0
        self._fb_lock = threading.Lock()
        self._fb_last = 0.0
        self._lock = threading.Lock()
        self._last = 0.0
        self.calls = 0
        self.cache_hits = 0
        # Set as soon as the provider reports a credit cap. Inner handlers may
        # swallow RpcExhausted to salvage non-RPC work, so the driver reads this
        # flag rather than relying on the exception reaching it.
        self.exhausted = False
        (self.cache_dir / "txs").mkdir(parents=True, exist_ok=True)

    def _pace(self):
        with self._lock:
            dt = time.time() - self._last
            if dt < self.min_interval:
                time.sleep(self.min_interval - dt)
            self._last = time.time()

    def _pace_fallback(self):
        with self._fb_lock:
            dt = time.time() - self._fb_last
            if dt < self.fallback_min_interval:
                time.sleep(self.fallback_min_interval - dt)
            self._fb_last = time.time()

    def call(self, method, params, retries=7):
        """Primary endpoint; on credit exhaustion, transparently fall back."""
        if self.exhausted and self.fallback_url and method not in DAS_METHODS:
            return self._call_fallback(method, params)
        try:
            return self._call_primary(method, params, retries)
        except RpcExhausted:
            if self.fallback_url and method not in DAS_METHODS:
                log(f"    -> primary exhausted, routing {method} to fallback RPC")
                return self._call_fallback(method, params)
            raise

    def _call_fallback(self, method, params, retries=6):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        for attempt in range(retries):
            self._pace_fallback()
            try:
                req = urllib.request.Request(
                    self.fallback_url, data=body, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=90) as r:
                    d = json.loads(r.read())
                self.fallback_calls += 1
                if "error" in d:
                    raise RuntimeError(Secrets.scrub(str(d["error"])))
                return d.get("result")
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries - 1:
                    time.sleep(min(2 ** attempt * 1.2, 20))
                    continue
                if attempt == retries - 1:
                    raise RuntimeError(f"fallback RPC HTTP {e.code}") from None
                time.sleep(1.5 * (attempt + 1))
            except Exception as e:
                if attempt == retries - 1:
                    raise RuntimeError(Secrets.scrub(f"fallback RPC {type(e).__name__}: {e}")) from None
                time.sleep(1.5 * (attempt + 1))

    def _call_primary(self, method, params, retries=7):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        body = json.dumps(payload).encode()
        for attempt in range(retries):
            self._pace()
            try:
                req = urllib.request.Request(
                    self.url, data=body, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=90) as r:
                    d = json.loads(r.read())
                self.calls += 1
                if "error" in d:
                    msg = str(d["error"])
                    if "max usage" in msg or "-32429" in msg:
                        self.exhausted = True
                        raise RpcExhausted(msg)
                    raise RuntimeError(Secrets.scrub(msg))
                return d.get("result")
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode(errors="replace")[:200]
                except Exception:
                    pass
                if e.code == 429:
                    if "max usage" in detail:
                        self.exhausted = True
                        raise RpcExhausted(detail) from None
                    if attempt == retries - 1:
                        self.exhausted = True
                        raise RpcExhausted(f"429 after {retries} tries") from None
                    time.sleep(min(2 ** attempt * 1.5, 30))
                    continue
                if attempt == retries - 1:
                    raise RuntimeError(Secrets.scrub(f"HTTP {e.code}: {detail}")) from None
                time.sleep(1.5 * (attempt + 1))
            except RpcExhausted:
                raise
            except Exception as e:
                if attempt == retries - 1:
                    raise RuntimeError(Secrets.scrub(f"{type(e).__name__}: {e}")) from None
                time.sleep(1.5 * (attempt + 1))

    def get_tx(self, sig):
        """Cached. Finalized transactions are immutable, so this is always safe."""
        path = self.cache_dir / "txs" / f"{sig}.json"
        if self.use_cache and path.exists():
            try:
                self.cache_hits += 1
                return json.loads(path.read_text())
            except Exception:
                pass
        tx = self.call(
            "getTransaction",
            [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        if tx is not None:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(tx))
            tmp.replace(path)
        return tx

    def all_signatures(self, address, hard_cap=200_000):
        """Walk the ENTIRE signature list. Never cap-and-stop: capping while
        paginating newest-first silently drops the oldest history."""
        out, before = [], None
        while len(out) < hard_cap:
            opts = {"limit": 1000}
            if before:
                opts["before"] = before
            batch = self.call("getSignaturesForAddress", [address, opts]) or []
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 1000:
                break
            before = batch[-1]["signature"]
        return out


# ------------------------------------------------- token-account flow scan
def token_deltas(tx, mint):
    """Raw per-account-index deltas for `mint`, program-agnostic.

    Reads pre/postTokenBalances, which the RPC populates identically for
    classic SPL and Token-2022. Anything that switches on the token program
    id here is how you end up silently parsing nothing.
    """
    meta = tx.get("meta") or {}
    pre, post, owners = {}, {}, {}
    for b in meta.get("preTokenBalances") or []:
        if b.get("mint") == mint:
            pre[b["accountIndex"]] = int(b["uiTokenAmount"]["amount"])
            owners[b["accountIndex"]] = b.get("owner")
    for b in meta.get("postTokenBalances") or []:
        if b.get("mint") == mint:
            post[b["accountIndex"]] = int(b["uiTokenAmount"]["amount"])
            owners[b["accountIndex"]] = b.get("owner")
    deltas = {}
    for i in set(pre) | set(post):
        d = post.get(i, 0) - pre.get(i, 0)
        if d:
            deltas[i] = d
    return deltas, owners


def account_keys(tx):
    """FULL ordered account list: static keys, then ALT-loaded writable, then
    ALT-loaded readonly.

    `transaction.message.accountKeys` alone is NOT enough. Versioned (v0)
    transactions resolve extra accounts through address lookup tables, and the
    `accountIndex` in pre/postTokenBalances and the pre/postBalances arrays
    index into this concatenated list. Using only the static keys silently
    misindexes every swap that goes through an aggregator -- the account is
    either "not found" (row dropped) or, worse, matched to the wrong pubkey.
    """
    entries = tx["transaction"]["message"]["accountKeys"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in entries]
    # jsonParsed already appends ALT-resolved accounts and tags them
    # source="lookupTable". Only splice loadedAddresses in when it clearly has
    # not -- appending twice would duplicate entries and corrupt every index.
    already = any(isinstance(k, dict) and k.get("source") == "lookupTable" for k in entries)
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    extra = list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    if not already and extra:
        keys = keys + extra
    return keys


def account_created_in_tx(tx, address: str):
    """True if `address` is created/initialised by an instruction in this tx.

    Walks top-level AND inner instructions -- an ATA created via the
    Associated Token Account program does its allocation in an inner
    instruction, so a top-level-only scan misses it.
    """
    creating = {"create", "createIdempotent", "createAccount",
                "createAssociatedTokenAccount", "allocate", "assign",
                "initializeAccount", "initializeAccount2", "initializeAccount3",
                "initializeImmutableOwner"}
    groups = [(tx.get("transaction") or {}).get("message", {}).get("instructions") or []]
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        groups.append(inner.get("instructions") or [])
    for g in groups:
        for ix in g:
            p = ix.get("parsed")
            if not isinstance(p, dict):
                continue
            if p.get("type") not in creating:
                continue
            info = p.get("info") or {}
            for k in ("account", "newAccount", "associatedAccount"):
                if info.get(k) == address:
                    return True, p.get("type"), ix.get("program")
    return False, None, None


def scan_token_account(rpc: Rpc, token_account: str, mint: str, label: str,
                       sol_owner: str | None = None, _attempt: int = 0):
    """Complete inbound/outbound ledger for one token account.

    Used twice: for the campaign ATA (the hero invariant) and for the
    deployer's ATA (his honesty record). Failed transactions cannot move
    tokens, so scanning only successful signatures is COMPLETE, not sampled --
    and residual == 0 is the proof of that completeness.
    """
    sigs = rpc.all_signatures(token_account)
    failed = sum(1 for s in sigs if s.get("err"))
    ok = [s["signature"] for s in sigs if not s.get("err")]
    log(f"  [{label}] {len(sigs)} sigs ({failed} failed, {len(ok)} successful)")

    inbound, outbound, missing = [], [], []
    for n, sig in enumerate(ok, 1):
        tx = rpc.get_tx(sig)
        if not tx:
            missing.append(sig)
            continue
        keys = account_keys(tx)
        if token_account not in keys:
            continue
        idx = keys.index(token_account)
        deltas, owners = token_deltas(tx, mint)
        d = deltas.get(idx, 0)
        if d == 0:
            continue
        counterparties = []
        for i, dd in deltas.items():
            if i == idx or dd * d >= 0:
                continue  # same-direction accounts are not counterparties
            counterparties.append({"owner": owners.get(i), "raw": abs(dd)})
        row = {
            "sig": sig,
            "ts": tx.get("blockTime"),
            "time": ts_iso(tx.get("blockTime")),
            "raw": d,
            "amount": ui_str(abs(d)),
            "counterparties": counterparties,
        }
        if sol_owner and sol_owner in keys:
            j = keys.index(sol_owner)
            meta = tx.get("meta") or {}
            try:
                row["sol_delta"] = (meta["postBalances"][j] - meta["preBalances"][j]) / 1e9
            except Exception:
                pass
        (inbound if d > 0 else outbound).append(row)
        if n % 100 == 0:
            log(f"  [{label}] parsed {n}/{len(ok)}")

    inbound.sort(key=lambda r: r["ts"] or 0)
    outbound.sort(key=lambda r: r["ts"] or 0)

    info = rpc.call("getAccountInfo", [token_account, {"encoding": "jsonParsed"}])
    parsed = ((info or {}).get("value") or {}).get("data", {}).get("parsed", {}).get("info", {})
    balance_raw = int(parsed.get("tokenAmount", {}).get("amount", 0))

    in_raw = sum(r["raw"] for r in inbound)
    out_raw = -sum(r["raw"] for r in outbound)
    residual = in_raw - out_raw - balance_raw

    # The signature walk and the balance read happen at different slots. If a
    # transfer lands in between, the residual is non-zero for a reason that has
    # nothing to do with a parsing bug. Re-scan once (transactions are cached,
    # so this costs one signature walk) before believing the invariant broke.
    if residual != 0 and _attempt == 0:
        log(f"  [{label}] residual {residual} != 0; re-scanning once to rule out a "
            f"mid-scan transfer (race), not yet reporting a break")
        time.sleep(2)
        return scan_token_account(rpc, token_account, mint, label, sol_owner, _attempt=1)

    recipients, transfer_count = {}, 0
    for r in outbound:
        for cp in r["counterparties"]:
            if cp["owner"]:
                recipients[cp["owner"]] = recipients.get(cp["owner"], 0) + cp["raw"]
                transfer_count += 1

    return {
        "token_account": token_account,
        "owner": parsed.get("owner"),
        "mint": mint,
        "token_program": (info or {}).get("value", {}).get("owner"),
        "signature_count": len(sigs),
        "failed_signature_count": failed,
        "failed_pct": round(failed / len(sigs) * 100, 1) if sigs else 0,
        "successful_signature_count": len(ok),
        "unfetchable_signatures": missing,
        "inbound_tx_count": len(inbound),
        "outbound_tx_count": len(outbound),
        "outbound_transfer_count": transfer_count,
        "unique_recipient_count": len(recipients),
        "inbound_raw": in_raw,
        "outbound_raw": out_raw,
        "balance_raw": balance_raw,
        "inbound": ui_str(in_raw),
        "outbound": ui_str(out_raw),
        "balance": ui_str(balance_raw),
        "residual_raw": residual,
        "invariant_holds": residual == 0,
        "complete": residual == 0 and not missing,
        "inbound_rows": inbound,
        "outbound_rows": outbound,
        "recipients": [
            {"owner": o, "raw": v, "amount": ui_str(v)}
            for o, v in sorted(recipients.items(), key=lambda kv: -kv[1])
        ],
    }


# --------------------------------------------------------------- X / tweets
def _syndication_token(tid: str) -> str:
    """Reproduces the tweet-syndication token: base36 of (id/1e15 * pi),
    zeros and the decimal point stripped."""
    n = (int(tid) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    i = int(n)
    frac = n - i
    s = "" if i else "0"
    while i:
        s = digits[i % 36] + s
        i //= 36
    if frac > 0:
        s += "."
        for _ in range(20):
            frac *= 36
            d = int(frac)
            s += digits[d]
            frac -= d
            if frac <= 0:
                break
    return re.sub(r"(0+|\.)", "", s)


def fetch_tweet(tid: str, bearer: str | None):
    """X API v2 first (it alone carries impression_count); keyless syndication
    CDN as fallback. Returns (record, route, error)."""
    if bearer:
        params = {
            "ids": tid,
            "tweet.fields": "id,text,created_at,author_id,public_metrics,attachments,conversation_id,in_reply_to_user_id",
            "expansions": "author_id,attachments.media_keys",
            "media.fields": "url,preview_image_url,type",
            "user.fields": "id,name,username,verified,public_metrics",
        }
        url = "https://api.x.com/2/tweets?" + urllib.parse.urlencode(params)
        d, err = http_json(url, headers={"Authorization": f"Bearer {bearer}"}, retries=2)
        if d and d.get("data"):
            t = d["data"][0]
            users = {u["id"]: u for u in (d.get("includes", {}).get("users") or [])}
            media = d.get("includes", {}).get("media") or []
            au = users.get(t.get("author_id"), {})
            return (
                {
                    "id": t["id"],
                    "text": t.get("text"),
                    "created_at": t.get("created_at"),
                    "author_handle": au.get("username"),
                    "author_name": au.get("name"),
                    "author_id": t.get("author_id"),
                    "public_metrics": t.get("public_metrics"),
                    "impression_count": (t.get("public_metrics") or {}).get("impression_count"),
                    "media": [
                        {"type": m.get("type"), "url": m.get("url") or m.get("preview_image_url")}
                        for m in media
                    ],
                    "url": f"https://x.com/{au.get('username','i')}/status/{t['id']}",
                },
                "x_api_v2",
                None,
            )
        api_err = err or "empty response"
    else:
        api_err = "no X_BEARER_TOKEN in environment"

    url = (
        f"https://cdn.syndication.twimg.com/tweet-result?id={tid}"
        f"&lang=en&token={_syndication_token(tid)}"
    )
    d, err = http_json(url, retries=3)
    if not d:
        return None, "none", f"x_api: {api_err} | syndication: {err}"
    u = d.get("user") or {}
    return (
        {
            "id": d.get("id_str", tid),
            "text": d.get("text"),
            "created_at": d.get("created_at"),
            "author_handle": u.get("screen_name"),
            "author_name": u.get("name"),
            "author_id": u.get("id_str"),
            "author_verified": u.get("is_blue_verified"),
            "in_reply_to_screen_name": d.get("in_reply_to_screen_name"),
            "in_reply_to_status_id": d.get("in_reply_to_status_id_str"),
            "favorite_count": d.get("favorite_count"),
            "impression_count": None,
            "media": [
                {"type": m.get("type"), "url": m.get("media_url_https")}
                for m in (d.get("mediaDetails") or [])
            ],
            "url": f"https://x.com/{u.get('screen_name','i')}/status/{d.get('id_str', tid)}",
        },
        "syndication_keyless",
        f"x_api unavailable ({api_err}); impression_count NOT available on this route",
    )


# ---------------------------------------------------------------- pump.fun
PF = "https://frontend-api-v3.pump.fun"


def pf_get(path):
    d, err = http_json(f"{PF}{path}", retries=3)
    return d, err


# ------------------------------------------------------------- 1. metadata
def collect_metadata(rpc: Rpc, out: dict):
    log("[1/7] token metadata (DAS getAsset + verbatim IPFS)")
    rec = {"collected_at": now_iso(), "mint": MINT}

    asset = None
    try:
        asset = rpc.call("getAsset", {"id": MINT})
        rec["das_asset_raw"] = asset
    except Exception as e:
        rec["das_error"] = Secrets.scrub(str(e))
        log(f"  ! getAsset failed: {Secrets.scrub(str(e))}")

    # The mint account itself -- NOT a DAS call, so this still works when the
    # Helius credit cap forces the run onto a plain RPC. DAS is a cross-check,
    # never the sole source for a load-bearing claim.
    chain_uri = chain_name = chain_symbol = chain_program = None
    try:
        info = rpc.call("getAccountInfo", [MINT, {"encoding": "jsonParsed"}])
        val = (info or {}).get("value") or {}
        inf = ((val.get("data") or {}).get("parsed") or {}).get("info") or {}
        chain_program = val.get("owner")
        tmeta = next(
            (e.get("state") or {} for e in (inf.get("extensions") or [])
             if e.get("extension") == "tokenMetadata"), {}
        )
        chain_uri, chain_name, chain_symbol = tmeta.get("uri"), tmeta.get("name"), tmeta.get("symbol")
    except Exception as e:
        rec["mint_account_error"] = Secrets.scrub(str(e))

    rec["token_program"] = fact(
        chain_program, "helius_rpc", "getAccountInfo(mint).owner",
        chain_program == TOKEN_2022,
        note="Token-2022. An SPL-only parser reads this mint as empty, silently.",
    )
    rec["onchain_name"] = fact(chain_name, "helius_rpc",
                               "getAccountInfo extensions[tokenMetadata].state.name", bool(chain_name))
    rec["onchain_symbol"] = fact(chain_symbol, "helius_rpc",
                                 "getAccountInfo extensions[tokenMetadata].state.symbol", bool(chain_symbol))
    rec["onchain_uri"] = fact(
        chain_uri, "helius_rpc", "getAccountInfo extensions[tokenMetadata].state.uri", bool(chain_uri)
    )

    if asset:
        content = asset.get("content") or {}
        md = content.get("metadata") or {}
        ti = asset.get("token_info") or {}
        rec["das_description"] = fact(
            md.get("description"), "helius_das",
            "getAsset content.metadata.description (indexer's copy of the IPFS doc)", True,
            note="Cross-check only. The authoritative text is ipfs_metadata.raw_text.",
        )
        rec["image"] = fact(
            (content.get("links") or {}).get("image"), "helius_das", "getAsset content.links.image", True
        )
        rec["decimals"] = fact(ti.get("decimals"), "helius_das", "getAsset token_info.decimals",
                               ti.get("decimals") == DECIMALS)
        # DAS reports a Metaplex-flavoured `mutable` flag that is MISLEADING for
        # a Token-2022 mint whose metadata authority is null. Surface it, and
        # explicitly tell the frontend not to render it.
        rec["das_mutable_flag"] = fact(
            asset.get("mutable"),
            "helius_das",
            "getAsset mutable",
            True,
            note="DO NOT RENDER. This Metaplex-oriented flag reads true even though "
            "the Token-2022 metadata updateAuthority is null. authorities.json is "
            "the authoritative immutability source.",
        )

    # Supply is fetched fresh every build. Burns are ongoing; it drifts.
    try:
        sup = rpc.call("getTokenSupply", [MINT])
        v = (sup or {}).get("value") or {}
        rec["supply"] = fact(
            {"raw": int(v.get("amount", 0)), "ui": v.get("uiAmountString"), "decimals": v.get("decimals")},
            "helius_rpc",
            "getTokenSupply (live, never hardcoded -- burns are ongoing)",
            True,
        )
    except Exception as e:
        rec["supply_error"] = Secrets.scrub(str(e))

    # Verbatim IPFS document. Stored byte-for-byte so the quote can be cited.
    ipfs = {"cid": METADATA_CID, "fetched": False}
    for gw in IPFS_GATEWAYS:
        url = f"{gw}/{METADATA_CID}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8")
            ipfs.update(
                {
                    "fetched": True,
                    "gateway": gw,
                    "raw_text": raw,          # verbatim, exactly as served
                    "parsed": json.loads(raw),
                    "sha256_note": "raw_text is the unmodified gateway response body",
                }
            )
            break
        except Exception as e:
            ipfs.setdefault("errors", []).append(f"{gw}: {type(e).__name__}")
    rec["ipfs_metadata"] = ipfs

    desc = (ipfs.get("parsed") or {}).get("description") if ipfs.get("fetched") else None
    rec["description"] = fact(
        desc,
        "ipfs",
        f"verbatim body of the IPFS document at CID {METADATA_CID}, which is the "
        "target of the mint's on-chain tokenMetadata.uri",
        bool(desc and desc.startswith(DESCRIPTION_PREFIX)),
        note="This is the citable origin of the 'I will not run this coin' line: it is "
        "the token's own metadata document, NOT a tweet. It replaces any attribution "
        "of this quote to a slingoor post.",
    )
    rec["description_starts_with_expected"] = bool(desc and desc.startswith(DESCRIPTION_PREFIX))

    # IMPORTANT: the description is NOT stored on chain. The Token-2022
    # tokenMetadata extension holds only name, symbol and uri. Immutability is a
    # two-link chain, and the site must make the argument in this form or it is
    # overclaiming:
    #   link 1 - the on-chain uri cannot be changed, because updateAuthority is null
    #   link 2 - the uri is a CID, and a CID is a hash of its content, so the
    #            document behind it cannot change either
    cid_in_uri = bool(chain_uri and METADATA_CID in chain_uri)
    das_agrees = (not asset) or (
        (asset.get("content") or {}).get("metadata", {}).get("description") == desc
    )
    rec["immutability_chain"] = fact(
        {
            "link_1_uri_cannot_change": {
                "onchain_uri": chain_uri,
                "update_authority_is_null": None,  # filled by the authorities section
                "source": "getAccountInfo extensions[tokenMetadata]",
            },
            "link_2_content_cannot_change": {
                "cid": METADATA_CID,
                "cid_present_in_onchain_uri": cid_in_uri,
                "why": "A CIDv1 is a content hash. Different bytes would produce a "
                "different CID, so this CID can only ever resolve to this document.",
            },
            "das_indexer_agrees": das_agrees,
        },
        "derived",
        "on-chain uri (null update authority) + content-addressed CID",
        cid_in_uri,
        note="Correct framing: the DESCRIPTION IS NOT ON CHAIN. Only name, symbol and "
        "uri are. Do not render 'the description is stored on-chain'. Render: the "
        "on-chain pointer is frozen and the document it points at is content-addressed.",
    )
    rec["ipfs_matches_das_indexer"] = fact(
        das_agrees, "cross_check",
        "IPFS document description vs Helius DAS indexed description, exact compare",
        das_agrees,
        note=None if asset else "DAS unavailable this run; compare skipped, not failed.",
    )

    out["metadata"] = rec
    return rec


# ----------------------------------------------------------- 2. authorities
def collect_authorities(rpc: Rpc, out: dict):
    log("[2/7] authority state + Token-2022 extension enumeration")
    rec = {"collected_at": now_iso(), "mint": MINT}
    try:
        info = rpc.call("getAccountInfo", [MINT, {"encoding": "jsonParsed"}])
    except Exception as e:
        rec["error"] = Secrets.scrub(str(e))
        out["authorities"] = rec
        return rec

    val = (info or {}).get("value") or {}
    parsed = (val.get("data") or {}).get("parsed") or {}
    inf = parsed.get("info") or {}
    exts = inf.get("extensions") or []
    ext_names = sorted({e.get("extension") for e in exts if e.get("extension")})

    token_meta = next((e for e in exts if e.get("extension") == "tokenMetadata"), {})
    ptr = next((e for e in exts if e.get("extension") == "metadataPointer"), {})
    update_authority = (token_meta.get("state") or {}).get("updateAuthority")

    rec["owner_program"] = fact(
        val.get("owner"), "helius_rpc", "getAccountInfo .owner", val.get("owner") == TOKEN_2022
    )
    rec["mint_authority"] = fact(
        inf.get("mintAuthority"), "helius_rpc", "getAccountInfo parsed.info.mintAuthority",
        inf.get("mintAuthority") is None, note="null => supply can never be inflated",
    )
    rec["freeze_authority"] = fact(
        inf.get("freezeAuthority"), "helius_rpc", "getAccountInfo parsed.info.freezeAuthority",
        inf.get("freezeAuthority") is None, note="null => no holder's account can be frozen",
    )
    rec["update_authority"] = fact(
        update_authority, "helius_rpc", "getAccountInfo extensions[tokenMetadata].state.updateAuthority",
        update_authority is None, note="null => the description is IMMUTABLE and cannot be rewritten",
    )
    rec["metadata_pointer_authority"] = fact(
        (ptr.get("state") or {}).get("authority"), "helius_rpc",
        "getAccountInfo extensions[metadataPointer].state.authority",
        (ptr.get("state") or {}).get("authority") is None,
    )
    rec["all_authorities_null"] = (
        inf.get("mintAuthority") is None
        and inf.get("freezeAuthority") is None
        and update_authority is None
    )
    rec["extensions_present"] = fact(
        ext_names, "helius_rpc", "getAccountInfo parsed.info.extensions[]",
        set(ext_names) == EXPECTED_EXTENSIONS,
        note=f"expected exactly {sorted(EXPECTED_EXTENSIONS)}",
    )
    rec["extensions_raw"] = exts
    # The absence claim, enumerated rather than asserted.
    rec["dangerous_extensions_absent"] = fact(
        {name: (name not in ext_names) for name in DANGEROUS_EXTENSIONS},
        "helius_rpc",
        "enumerated extensions[] and checked each dangerous extension is not present",
        all(name not in ext_names for name in DANGEROUS_EXTENSIONS),
        note="No transfer fee, no transfer hook, no permanent delegate, no default "
        "frozen state. This is the strongest verifiable safety claim on the site.",
    )
    rec["supply_raw"] = inf.get("supply")
    rec["decimals"] = inf.get("decimals")
    rec["immutability_caveat"] = (
        "Verified: the CURRENT metadata update authority is null, so the description "
        "can never change from here. This script does NOT independently prove the "
        "authority was null from the creation transaction onward."
    )
    out["authorities"] = rec
    return rec


# -------------------------------------------------------------- 3. pump.fun
def collect_pumpfun(rpc: Rpc, out: dict):
    log("[3/7] pump.fun profile mapping (bidirectional)")
    rec = {"collected_at": now_iso(), "wallets": {}, "handles": {}, "errors": []}

    coin, err = pf_get(f"/coins/{MINT}")
    if err:
        rec["errors"].append(f"coins/{MINT}: {err}")
    if coin:
        rec["coin"] = {
            "raw": coin,
            "creator": fact(coin.get("creator"), "pumpfun_api", f"GET /coins/{{mint}} .creator",
                            coin.get("creator") == DEPLOYER),
            "created_timestamp": fact(
                coin.get("created_timestamp"), "pumpfun_api", "GET /coins/{mint} .created_timestamp", True,
                note=ts_iso((coin.get("created_timestamp") or 0) / 1000),
            ),
            "created_at_iso": ts_iso((coin.get("created_timestamp") or 0) / 1000),
            "ath_market_cap": fact(coin.get("ath_market_cap"), "pumpfun_api", "GET /coins/{mint} .ath_market_cap", True),
            "ath_market_cap_timestamp": fact(
                coin.get("ath_market_cap_timestamp"), "pumpfun_api",
                "GET /coins/{mint} .ath_market_cap_timestamp", True,
                note=ts_iso((coin.get("ath_market_cap_timestamp") or 0) / 1000),
            ),
            "ath_market_cap_at_iso": ts_iso((coin.get("ath_market_cap_timestamp") or 0) / 1000),
            "metadata_uri": fact(coin.get("metadata_uri"), "pumpfun_api", "GET /coins/{mint} .metadata_uri", True),
            "bonding_curve": coin.get("bonding_curve"),
            "pump_swap_pool": coin.get("pump_swap_pool"),
            "token_program": coin.get("token_program"),
            "description_note": "pump.fun collapses the newlines in the description. "
            "Cite metadata.json / IPFS for the verbatim text, not this field.",
        }

    # Direction A: wallet -> profile.
    for addr, role in ((CAMPAIGN_OWNER, "campaign_wallet"), (DEPLOYER, "deployer")):
        p, err = pf_get(f"/users/{addr}")
        if err:
            rec["errors"].append(f"users/{addr}: {err}")
        if p:
            rec["wallets"][addr] = {
                "role": role,
                "raw": p,
                "username": p.get("username"),
                "followers": p.get("followers"),
                "bio": p.get("bio"),
                "profile_image": p.get("profile_image"),
                # Recorded EXACTLY as returned. These are expected to be null and
                # the site must render that null, not paper over it.
                "x_username": fact(
                    p.get("x_username"), "pumpfun_api", "GET /users/{address} .x_username", True,
                    note="null: pump.fun has NO verified X link for this wallet. The X "
                    "identity is established by the wallet-proof exhibit instead.",
                ),
                "x_id": fact(
                    p.get("x_id"), "pumpfun_api", "GET /users/{address} .x_id", True,
                    note="null: no pump.fun-verified X account id.",
                ),
            }

    # Direction B: handle -> wallet. A round trip is what makes the mapping
    # decisive rather than merely suggestive.
    for addr in (CAMPAIGN_OWNER, DEPLOYER):
        handle = (rec["wallets"].get(addr) or {}).get("username")
        if not handle:
            continue
        p, err = pf_get(f"/users/{handle}")
        if err:
            rec["errors"].append(f"users/{handle}: {err}")
        resolved = (p or {}).get("address")
        rec["handles"][handle] = {
            "raw": p,
            "resolves_to": resolved,
            "round_trip_ok": resolved == addr,
        }

    rec["bidirectional_verified"] = fact(
        {h: v["round_trip_ok"] for h, v in rec["handles"].items()},
        "pumpfun_api",
        "wallet -> /users/{address} -> username -> /users/{username} -> address, compared",
        bool(rec["handles"]) and all(v["round_trip_ok"] for v in rec["handles"].values()),
    )
    rec["caveat_x_link"] = (
        "pump.fun returns x_username=null and x_id=null for BOTH wallets. The "
        "pump.fun username is a self-chosen display name and is NOT by itself "
        "proof of X identity. Render this caveat; do not hide it."
    )
    out["pumpfun"] = rec
    return rec


# ---------------------------------------------------- 4. wallet-proof exhibit
def collect_proof(rpc: Rpc, out: dict):
    log("[4/7] wallet-proof exhibit")
    rec = {"collected_at": now_iso(), "steps": [], "errors": []}

    # Step 1 -- the transfer itself.
    tx = None
    try:
        tx = rpc.get_tx(PROOF_TX)
    except Exception as e:
        rec["errors"].append(f"getTransaction: {Secrets.scrub(str(e))}")

    transfer = {"sig": PROOF_TX}
    if tx:
        deltas, owners = token_deltas(tx, MINT)
        by_owner = {}
        for i, d in deltas.items():
            o = owners.get(i)
            if o:
                by_owner[o] = by_owner.get(o, 0) + d
        sent = by_owner.get(CAMPAIGN_OWNER, 0)
        recv = by_owner.get(PROOF_RECIPIENT_OWNER, 0)
        transfer.update(
            {
                "block_time": tx.get("blockTime"),
                "time": ts_iso(tx.get("blockTime")),
                "fee_payer": account_keys(tx)[0],
                "err": (tx.get("meta") or {}).get("err"),
                "from_owner": CAMPAIGN_OWNER,
                "to_owner": PROOF_RECIPIENT_OWNER,
                "sent_raw": sent,
                "received_raw": recv,
                "amount": ui_str(recv),
                "amount_matches_expected": recv == PROOF_AMOUNT_RAW,
                "explorer": f"https://solscan.io/tx/{PROOF_TX}",
            }
        )
    rec["transfer"] = fact(
        transfer, "helius_rpc", "getTransaction jsonParsed, pre/postTokenBalances delta",
        bool(tx) and transfer.get("amount_matches_expected") and transfer.get("err") is None,
    )

    # Step 2 -- THE decisive detail. The recipient's token account was created
    # in this very transaction and has exactly one signature in its entire
    # history, so the 70K balance has no other possible source.
    sigs, sig_err = None, None
    try:
        sigs = rpc.all_signatures(PROOF_RECIPIENT_TOKEN_ACCOUNT)
    except Exception as e:
        sig_err = Secrets.scrub(str(e))
        rec["errors"].append(f"getSignaturesForAddress: {sig_err}")

    if sigs is not None:
        only_one = len(sigs) == 1
        same_tx = only_one and sigs[0]["signature"] == PROOF_TX
        created, ix_type, ix_program = (False, None, None)
        if tx:
            created, ix_type, ix_program = account_created_in_tx(tx, PROOF_RECIPIENT_TOKEN_ACCOUNT)
        rec["recipient_token_account"] = fact(
            {
                "address": PROOF_RECIPIENT_TOKEN_ACCOUNT,
                "owner": PROOF_RECIPIENT_OWNER,
                "signature_count_ever": len(sigs),
                "signatures": [
                    {"sig": s["signature"], "time": ts_iso(s.get("blockTime")), "err": s.get("err")}
                    for s in sigs
                ],
                "created_in_proof_tx": created,
                "creating_instruction": {"type": ix_type, "program": ix_program} if created else None,
                "sole_signature_is_proof_tx": same_tx,
            },
            "helius_rpc",
            "getSignaturesForAddress walked to the END of history (no cap)",
            only_one and same_tx,
            note="Exactly ONE signature ever, and it is the transfer tx. The account "
            "was created in that transaction, so its balance cannot have come from "
            "anywhere else. This is what makes the screenshot uncounterfeitable.",
        )
    else:
        rec["recipient_token_account"] = fact(
            {"address": PROOF_RECIPIENT_TOKEN_ACCOUNT}, "helius_rpc",
            "getSignaturesForAddress", False, note=f"NOT VERIFIED: {sig_err}",
        )

    # Step 3 -- the tweet, 63 seconds later.
    bearer = os.environ.get("X_BEARER_TOKEN")
    Secrets.add(bearer)
    tweet, route, terr = fetch_tweet(PROOF_TWEET_ID, bearer)
    rec["tweet_route"] = route
    if terr:
        rec["tweet_note"] = terr
    if tweet:
        gap = None
        if transfer.get("block_time") and tweet.get("created_at"):
            try:
                tw = datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00"))
                gap = int(tw.timestamp() - transfer["block_time"])
            except Exception:
                pass
        rec["tweet"] = fact(
            tweet, route, "tweet lookup by id", True,
            note=None if route == "x_api_v2" else
            "Keyless route: impression_count is NOT available. Do not display an impressions figure.",
        )
        rec["gap_seconds"] = fact(
            gap, "derived", "tweet.created_at - tx.blockTime", gap is not None,
            note="Seconds between the on-chain transfer and the tweet showing it.",
        )

        # The closing link: the handle the tweet replies to is the same handle
        # the recipient wallet carries on pump.fun -- two independent sources.
        pf_recipient, pf_err = pf_get(f"/users/{PROOF_RECIPIENT_OWNER}")
        if pf_err:
            rec["errors"].append(f"users/{PROOF_RECIPIENT_OWNER}: {pf_err}")
        pf_handle = (pf_recipient or {}).get("username")
        reply_to = tweet.get("in_reply_to_screen_name")
        rec["recipient_identity"] = fact(
            {
                "wallet": PROOF_RECIPIENT_OWNER,
                "pumpfun_username": pf_handle,
                "pumpfun_raw": pf_recipient,
                "tweet_in_reply_to_screen_name": reply_to,
                "handles_match": bool(pf_handle and reply_to and pf_handle.lower() == reply_to.lower()),
            },
            "pumpfun_api + x",
            "recipient wallet's pump.fun username compared to the handle the tweet replies to",
            bool(pf_handle and reply_to and pf_handle.lower() == reply_to.lower()),
            note="Two independent sources name the same person for the receiving wallet.",
        )
    else:
        rec["tweet"] = fact(None, "x", "tweet lookup by id", False, note=terr)

    tv = rec.get("transfer", {}).get("verified")
    av = rec.get("recipient_token_account", {}).get("verified")
    wv = rec.get("tweet", {}).get("verified")
    rec["exhibit_complete"] = bool(tv and av and wv)
    rec["claim"] = (
        "The campaign wallet sent 70,000 TOAD to a brand-new token account that has "
        "exactly one transaction in its entire history, and 63 seconds later the X "
        "account @mdudas posted a screenshot of that balance while replying to the "
        "recipient. The receiving wallet's pump.fun username matches the handle "
        "replied to. The wallet and the X account are the same operator."
    )
    out["wallet_proof"] = rec
    return rec


# ------------------------------------------------------------- 5. tick-tock
def collect_timeline(rpc: Rpc, out: dict, campaign_scan=None):
    log("[5/7] launch tick-tock (re-verifying every row on chain)")
    rows, errors = [], []

    def tx_row(key, sig, title, expect):
        r = {"key": key, "title": title, "sig": sig, "expect": expect,
             "explorer": f"https://solscan.io/tx/{sig}"}
        try:
            tx = rpc.get_tx(sig)
        except Exception as e:
            r["verified"] = False
            r["error"] = Secrets.scrub(str(e))
            errors.append(f"{key}: {r['error']}")
            return r
        if not tx:
            r["verified"] = False
            r["error"] = "transaction not found"
            return r
        r["block_time"] = tx.get("blockTime")
        r["time"] = ts_iso(tx.get("blockTime"))
        r["fee_payer"] = account_keys(tx)[0]
        r["success"] = (tx.get("meta") or {}).get("err") is None
        deltas, owners = token_deltas(tx, MINT)
        by_owner = {}
        for i, d in deltas.items():
            o = owners.get(i)
            if o:
                by_owner[o] = by_owner.get(o, 0) + d
        r["toad_deltas"] = [
            {"owner": o, "raw": v, "amount": ui_str(v)}
            for o, v in sorted(by_owner.items(), key=lambda kv: -abs(kv[1]))
        ]
        r["_by_owner"] = by_owner
        r["_tx"] = tx
        return r

    # -- 13:59:13 mint created; deployer buys 20.1% in the SAME transaction.
    r = tx_row("mint_created", MINT_CREATE_TX, "Mint created; deployer buys in the same transaction",
               {"time": "2026-08-08T13:59:13Z", "deployer_raw": 200_963_210_657_135, "sol": 6.913580})
    if r.get("_tx"):
        tx = r["_tx"]
        got = r["_by_owner"].get(DEPLOYER, 0)
        keys = account_keys(tx)
        meta = tx["meta"]
        sol = {}
        for i, (a, b) in enumerate(zip(meta["preBalances"], meta["postBalances"])):
            if a != b:
                sol[keys[i]] = b - a
        curve_credit = sol.get(BONDING_CURVE, 0)
        curve_rent = None
        try:
            ci = rpc.call("getAccountInfo", [BONDING_CURVE, {"encoding": "base64"}])
            space = ((ci or {}).get("value") or {}).get("space")
            if space:
                curve_rent = rpc.call("getMinimumBalanceForRentExemption", [space])
        except Exception as e:
            errors.append(f"curve rent: {Secrets.scrub(str(e))}")
        r["deployer_bought_raw"] = got
        r["deployer_bought"] = ui_str(got)
        r["deployer_sol_delta"] = sol.get(DEPLOYER, 0) / 1e9
        r["sol_credited_to_curve"] = curve_credit / 1e9
        if curve_rent is not None:
            r["curve_rent_exempt_sol"] = curve_rent / 1e9
            r["sol_spent_on_buy"] = round((curve_credit - curve_rent) / 1e9, 9)
        r["same_transaction_as_mint_creation"] = True
        r["amount_matches"] = got == r["expect"]["deployer_raw"]
        r["verified"] = bool(r.get("success") and r["amount_matches"] and r["fee_payer"] == DEPLOYER)
        r["note"] = ("The deployer is the fee payer of the mint-creation transaction AND "
                     "the buyer inside it -- a same-block snipe of his own launch.")
        r["sol_figure_caveat"] = (
            "Cite sol_credited_to_curve (directly measured lamport delta). "
            "sol_spent_on_buy subtracts the curve account's rent-exemption from that "
            "credit and is therefore an ESTIMATE -- it lands ~0.00025 SOL below the "
            "commonly quoted 6.913580 because the rent figure is computed from the "
            "account's current size. Do not present it as exact."
        )
        r["pct_of_initial_supply"] = round(got / 1_000_000_000_000_000 * 100, 4)
    rows.append(r)

    # -- 13:59:17 PumpSwap pool created. Verified off-chain: walking the pool's
    #    signature history back to genesis costs tens of thousands of calls.
    ds, ds_err = http_json(
        f"https://api.dexscreener.com/latest/dex/pairs/solana/{PUMPSWAP_POOL}", retries=3
    )
    pair = None
    if ds:
        pl = ds.get("pairs") or ([ds.get("pair")] if ds.get("pair") else [])
        pair = pl[0] if pl else None
    created_ms = (pair or {}).get("pairCreatedAt")
    rows.append(
        {
            "key": "pool_created",
            "title": "PumpSwap pool created (canonical pricing pool)",
            "pool": PUMPSWAP_POOL,
            "expect": {"time": "2026-08-08T13:59:17Z"},
            "time": ts_iso(created_ms / 1000) if created_ms else None,
            "dex": (pair or {}).get("dexId"),
            "quote_token": ((pair or {}).get("quoteToken") or {}).get("symbol"),
            "verified": bool(created_ms),
            "verification_source": "dexscreener pairCreatedAt",
            "caveat": "Timestamp attested by DexScreener, not by an on-chain walk: "
            "reaching this pool's first signature requires paginating its entire "
            "(very large) history. Constant-product PumpSwap pool -- this is the "
            "canonical pricing venue. Never price off the Meteora DLMM pool.",
            "error": ds_err,
        }
    )

    # -- 14:16:56 campaign wallet buys, gas paid by the pump.fun mobile paymaster.
    r = tx_row("campaign_buy", CAMPAIGN_BUY_TX, "Campaign wallet buys TOAD with USDC",
               {"time": "2026-08-08T14:16:56Z", "toad_raw": 4_618_714_619_783,
                "usdc": 217.323479, "fee_payer": PAYMASTER})
    if r.get("_tx"):
        got = r["_by_owner"].get(CAMPAIGN_OWNER, 0)
        usdc_deltas, usdc_owners = token_deltas(r["_tx"], USDC_MINT)
        usdc = 0
        for i, d in usdc_deltas.items():
            if usdc_owners.get(i) == CAMPAIGN_OWNER:
                usdc += d
        r["toad_received_raw"] = got
        r["toad_received"] = ui_str(got)
        r["usdc_spent"] = ui_str(abs(usdc), 6)
        r["fee_payer_is_paymaster"] = r["fee_payer"] == PAYMASTER
        r["amount_matches"] = got == r["expect"]["toad_raw"]
        r["verified"] = bool(r.get("success") and r["amount_matches"] and r["fee_payer_is_paymaster"])
        r["note"] = ("Gas paid by relayer FHpcNSe6... (pump.fun mobile paymaster), so the "
                     "buyer's wallet never needed SOL -- consistent with a mobile-app buy.")
    rows.append(r)

    # -- 15:03:39 slingoor's launch tweet. No id supplied and the X API is
    #    unauthorised, so this row ships UNVERIFIED rather than fabricated.
    rows.append(
        {
            "key": "slingoor_tweet",
            "title": "slingoor (deployer) tweets about the launch",
            "expect": {"time": "2026-08-08T15:03:39Z"},
            "tweet_id": None,
            "verified": False,
            "caveat": "No tweet id was supplied for this event and tweet SEARCH requires "
            "an authorised X API credential (the configured bearer returns 401 on every "
            "endpoint). Supply the tweet id to make this row citable, or omit the row.",
        }
    )

    # -- 15:13:30 @eltoadpepe created.
    rows.append(
        {
            "key": "eltoadpepe_created",
            "title": "@eltoadpepe X account created",
            "expect": {"time": "2026-08-08T15:13:30Z"},
            "handle": "eltoadpepe",
            "verified": False,
            "caveat": "Account creation date requires GET /2/users/by/username with "
            "user.fields=created_at. The configured bearer token returns 401. The "
            "keyless syndication profile endpoint returns an empty body. Unverified.",
        }
    )

    # -- 15:27:19 deployer forwards the bag, keeping exactly 20,000,000.
    r = tx_row("deployer_transfer", DEPLOYER_SEND_TX,
               "Deployer sends the bag to the campaign wallet, keeping exactly 20,000,000",
               {"time": "2026-08-08T15:27:19Z", "sent_raw": 180_963_210_657_135,
                "deployer_keeps_raw": 20_000_000_000_000})
    if r.get("_tx"):
        sent = -r["_by_owner"].get(DEPLOYER, 0)
        recv = r["_by_owner"].get(CAMPAIGN_OWNER, 0)
        bought = 200_963_210_657_135
        kept = bought - sent
        r["sent_raw"] = sent
        r["sent"] = ui_str(sent)
        r["received_by_campaign"] = ui_str(recv)
        r["deployer_keeps_raw"] = kept
        r["deployer_keeps"] = ui_str(kept)
        r["keeps_round_20m"] = kept == r["expect"]["deployer_keeps_raw"]
        r["amount_matches"] = sent == r["expect"]["sent_raw"]
        r["verified"] = bool(r.get("success") and r["amount_matches"] and sent == recv)
        r["note"] = ("Bought 200,963,210.657135 and forwarded 180,963,210.657135, leaving "
                     "exactly 20,000,000.000000 -- a deliberate round number, not a residue.")
    rows.append(r)

    # -- 17:20:43 first airdrop leaves the campaign ATA. Taken from the
    #    campaign scan so it is the true first, not the first of a capped page.
    first = None
    if campaign_scan and campaign_scan.get("outbound_rows"):
        first = campaign_scan["outbound_rows"][0]
    if first:
        rows.append(
            {
                "key": "first_airdrop",
                "title": "First airdrop leaves the campaign wallet",
                "sig": first["sig"],
                "expect": {"time": "2026-08-08T17:20:43Z"},
                "time": first["time"],
                "block_time": first["ts"],
                "amount": first["amount"],
                "recipients": first["counterparties"],
                "verified": True,
                "verification_source": "full ATA outbound scan, oldest row",
                "explorer": f"https://solscan.io/tx/{first['sig']}",
                "note": "Identified by scanning the ATA's COMPLETE signature history and "
                "taking the earliest outbound row -- not by reading the first page.",
            }
        )
    else:
        rows.append({"key": "first_airdrop", "title": "First airdrop leaves the campaign wallet",
                     "expect": {"time": "2026-08-08T17:20:43Z"}, "verified": False,
                     "caveat": "Requires the campaign ATA scan (run without --only, or include 'proof')."})

    for r in rows:
        r.pop("_tx", None)
        r.pop("_by_owner", None)
        if "time" in r and r.get("expect", {}).get("time"):
            r["time_matches_expected"] = r.get("time") == r["expect"]["time"]

    rec = {
        "collected_at": now_iso(),
        "rows": sorted(rows, key=lambda x: x.get("block_time") or _expect_ts(x)),
        "verified_count": sum(1 for r in rows if r.get("verified")),
        "total_count": len(rows),
        "errors": errors,
    }
    out["launch_timeline"] = rec
    return rec


def _expect_ts(row):
    t = (row.get("expect") or {}).get("time")
    if not t:
        return 0
    try:
        return int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


WSOL_MINT = "So11111111111111111111111111111111111111112"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def classify_pool_action(tx, wallet: str):
    """Classify a pool-touching transaction as buy / sell / lp_deposit / lp_withdraw.

    CRITICAL: "TOAD left the wallet toward the pool" is NOT the same as "sold".
    Adding liquidity also sends TOAD to the pool -- together with SOL, in
    exchange for LP tokens. Counting a liquidity deposit as a sale inverts the
    meaning of the deployer's behaviour (it is the opposite of dumping), so the
    action is read from the AMM program's own instruction name in the logs, with
    the direction of SOL as a cross-check.
    """
    logs = (tx.get("meta") or {}).get("logMessages") or []
    action = None
    for line in logs:
        if "Instruction: " not in line:
            continue
        name = line.split("Instruction: ", 1)[1].strip()
        if name in ("Buy", "Sell", "Deposit", "Withdraw"):
            action = {"Buy": "buy", "Sell": "sell",
                      "Deposit": "lp_deposit", "Withdraw": "lp_withdraw"}[name]
            # keep scanning: the last AMM-level action wins over inner ones
    # SOL movement for the wallet: native lamports plus any WSOL token delta.
    keys = account_keys(tx)
    native = 0
    if wallet in keys:
        j = keys.index(wallet)
        meta = tx.get("meta") or {}
        try:
            native = (meta["postBalances"][j] - meta["preBalances"][j]) / 1e9
        except Exception:
            native = 0
    wsol_deltas, wsol_owners = token_deltas(tx, WSOL_MINT)
    wsol = sum(d for i, d in wsol_deltas.items() if wsol_owners.get(i) == wallet) / 1e9
    sol_flow = native + wsol
    if action is None:
        action = "sell" if sol_flow > 0 else "buy" if sol_flow < 0 else "unknown"
    return action, round(sol_flow, 9), round(native, 9), round(wsol, 9)


def verify_curve_creator(rpc: Rpc):
    """The pump.fun BondingCurve account stores the creator pubkey at byte
    offset 49. Decoding it proves the deployer from chain state alone, without
    trusting the pump.fun API's `creator` field."""
    info = rpc.call("getAccountInfo", [BONDING_CURVE, {"encoding": "base64"}])
    val = (info or {}).get("value") or {}
    raw = base64.b64decode((val.get("data") or ["", ""])[0])
    if len(raw) < CURVE_CREATOR_OFFSET + 32:
        return fact(None, "helius_rpc", "BondingCurve byte offset 49", False,
                    note=f"account is only {len(raw)} bytes; layout changed?")
    creator = b58encode(raw[CURVE_CREATOR_OFFSET:CURVE_CREATOR_OFFSET + 32])
    return fact(
        creator, "helius_rpc",
        f"getAccountInfo({BONDING_CURVE}) base64, pubkey decoded at byte offset {CURVE_CREATOR_OFFSET}",
        creator == DEPLOYER,
        note="Read from chain state, independent of the pump.fun API's creator field.",
    )


# -------------------------------------------------------------- 6. deployer
def collect_deployer(rpc: Rpc, out: dict):
    log("[6/7] deployer record")
    rec = {"collected_at": now_iso(), "wallet": DEPLOYER, "token_account": DEPLOYER_ATA}
    try:
        rec["creator_onchain"] = verify_curve_creator(rpc)
    except Exception as e:
        rec["creator_onchain"] = fact(None, "helius_rpc", "BondingCurve byte offset 49",
                                      False, note=Secrets.scrub(str(e)))
    try:
        scan = scan_token_account(rpc, DEPLOYER_ATA, MINT, "deployer", sol_owner=DEPLOYER)
    except Exception as e:
        rec["error"] = Secrets.scrub(str(e))
        out["deployer"] = rec
        return rec

    pool_side = {PUMPSWAP_POOL, BONDING_CURVE}
    buckets = {"buy": [], "sell": [], "lp_deposit": [], "lp_withdraw": [], "unknown": []}
    # Plain wallet-to-wallet outflows. Tracked separately so "never sold" can
    # never be read as "never parted with any tokens" -- and so a sale routed
    # through some other venue would show up here rather than vanish.
    plain_out = []
    for r in scan["outbound_rows"]:
        if not any(cp["owner"] in pool_side for cp in r["counterparties"]):
            try:
                tx = rpc.get_tx(r["sig"])
            except Exception:
                tx = None
            action, sol_flow, _n, _w = (
                classify_pool_action(tx, DEPLOYER) if tx else ("unknown", 0, 0, 0)
            )
            plain_out.append(dict(
                r, sol_received=sol_flow,
                looks_like_sale=sol_flow > 0.001,
                to=[cp["owner"] for cp in r["counterparties"]],
            ))
    for r in scan["outbound_rows"] + scan["inbound_rows"]:
        if not any(cp["owner"] in pool_side for cp in r["counterparties"]):
            continue
        try:
            tx = rpc.get_tx(r["sig"])
        except Exception:
            tx = None
        if not tx:
            buckets["unknown"].append(r)
            continue
        action, sol_flow, native, wsol = classify_pool_action(tx, DEPLOYER)
        r = dict(r, action=action, sol_flow=sol_flow, sol_native=native, sol_wsol=wsol)
        buckets.setdefault(action, []).append(r)
    for v in buckets.values():
        v.sort(key=lambda x: x["ts"] or 0)

    sells, buys = buckets["sell"], buckets["buy"]
    lp_dep, lp_wd = buckets["lp_deposit"], buckets["lp_withdraw"]
    sold_raw = -sum(r["raw"] for r in sells)
    bought_raw = sum(r["raw"] for r in buys)
    lp_dep_raw = -sum(r["raw"] for r in lp_dep)
    lp_wd_raw = sum(r["raw"] for r in lp_wd)

    running, low, low_ts = 0, None, None
    for r in sorted(scan["inbound_rows"] + scan["outbound_rows"], key=lambda x: x["ts"] or 0):
        running += r["raw"]
        if low is None or running < low:
            low, low_ts = running, r["ts"]

    # Wallet-level stats: the deployer's WALLET history is mostly failed sniper
    # spam. Reported separately from the ATA so neither number is misattributed.
    wallet_stats = None
    try:
        wsigs = rpc.all_signatures(DEPLOYER)
        wfail = sum(1 for s in wsigs if s.get("err"))
        wallet_stats = {
            "signature_count": len(wsigs),
            "failed_count": wfail,
            "failed_pct": round(wfail / len(wsigs) * 100, 1) if wsigs else 0,
        }
    except Exception as e:
        rec["wallet_stats_error"] = Secrets.scrub(str(e))

    rec.update(
        {
            "balance_raw": scan["balance_raw"],
            "balance": scan["balance"],
            "balance_identity": fact(
                {
                    "inbound": scan["inbound"],
                    "outbound": scan["outbound"],
                    "balance": scan["balance"],
                    "residual_raw": scan["residual_raw"],
                },
                "helius_rpc",
                "sum(inbound) - sum(outbound) == getAccountInfo(ATA).amount",
                scan["invariant_holds"],
            ),
            "did_sell": fact(
                sold_raw > 0, "helius_rpc",
                "pool-touching transactions classified by the AMM program's own "
                "instruction name (Buy/Sell/Deposit/Withdraw) in the logs, with the "
                "direction of SOL as a cross-check -- NOT by TOAD direction alone",
                True,
                note="Liquidity deposits also move TOAD to the pool. They are counted "
                "separately and are NOT sales.",
            ),
            "sold_raw": sold_raw,
            "sold": ui_str(sold_raw),
            "sell_tx_count": len(sells),
            "sol_received_from_sells": round(sum(r.get("sol_flow") or 0 for r in sells), 9),
            "rebought_raw": bought_raw,
            "rebought": ui_str(bought_raw),
            "buy_tx_count": len(buys),
            "sol_spent_on_buys": round(sum(r.get("sol_flow") or 0 for r in buys), 9),
            "lp_deposited_raw": lp_dep_raw,
            "lp_deposited": ui_str(lp_dep_raw),
            "lp_deposit_tx_count": len(lp_dep),
            "sol_deposited_with_lp": round(sum(r.get("sol_flow") or 0 for r in lp_dep), 9),
            "lp_withdrawn_raw": lp_wd_raw,
            "lp_withdrawn": ui_str(lp_wd_raw),
            "lp_withdraw_tx_count": len(lp_wd),
            "unclassified_tx_count": len(buckets["unknown"]),
            "never_sold": fact(
                sold_raw == 0 and not any(p["looks_like_sale"] for p in plain_out),
                "helius_rpc",
                "no pool transaction classified as Sell, and no non-pool outflow returned "
                "SOL to the wallet (which would indicate a sale routed via another venue)",
                True,
            ),
            "plain_transfers_out": plain_out,
            "plain_transfers_out_raw": -sum(r["raw"] for r in plain_out),
            "plain_transfers_out_total": ui_str(-sum(r["raw"] for r in plain_out)),
            "classification_note": (
                "TOAD moving from this wallet to the pool is NOT evidence of selling. "
                "A liquidity deposit sends TOAD *and* SOL to the pool and returns LP "
                "tokens. Any figure that lumps the two together overstates selling and "
                "misrepresents the deployer."
            ),
            "buy_count_completeness": (
                "COMPLETE for token movement, not sampled: failed transactions cannot "
                "move tokens, so scanning the ATA's successful signatures captures every "
                "balance change. residual == 0 proves nothing was missed."
                if scan["invariant_holds"] else
                "SAMPLED / INCOMPLETE: residual is non-zero, so some balance changes were "
                f"not captured (residual_raw={scan['residual_raw']}). Do not present these "
                "counts as exhaustive."
            ),
            "min_balance_raw": low,
            "min_balance": ui_str(low) if low is not None else None,
            "min_balance_at": ts_iso(low_ts),
            "reaccumulated_raw": scan["balance_raw"] - (low or 0),
            "reaccumulated": ui_str(scan["balance_raw"] - (low or 0)),
            "ata_signature_stats": {
                "signature_count": scan["signature_count"],
                "failed_count": scan["failed_signature_count"],
                "failed_pct": scan["failed_pct"],
                "successful_count": scan["successful_signature_count"],
                "note": "Overwhelmingly failed sniper-bot spam aimed at this account.",
            },
            "wallet_signature_stats": wallet_stats,
            "sells": sells,
            "buys": buys,
            "lp_deposits": lp_dep,
            "lp_withdrawals": lp_wd,
            "honesty_summary": (
                f"The deployer kept exactly 20,000,000 TOAD at launch. Sold "
                f"{ui_str(sold_raw)} TOAD across {len(sells)} sell transactions "
                f"({round(sum(r.get('sol_flow') or 0 for r in sells), 3)} SOL received). "
                f"Separately committed {ui_str(lp_dep_raw)} TOAD to pool liquidity across "
                f"{len(lp_dep)} deposits, paired with "
                f"{abs(round(sum(r.get('sol_flow') or 0 for r in lp_dep), 3))} SOL of his own -- "
                f"that TOAD left the wallet but was NOT sold, and "
                f"{ui_str(lp_wd_raw)} was later withdrawn back out of liquidity. Bought "
                f"{ui_str(bought_raw)} across {len(buys)} purchases. Also sent "
                f"{ui_str(-sum(r['raw'] for r in plain_out))} out as plain transfers "
                f"(that figure includes the launch-day hand-off to the campaign wallet). "
                f"Balance bottomed at "
                f"{ui_str(low) if low is not None else '?'} on {ts_iso(low_ts)} and now stands at "
                f"{scan['balance']}."
            ),
        }
    )
    out["deployer"] = rec
    return rec


# -------------------------------------------------------------- 7. copycats
def collect_copycats(out: dict):
    log("[7/7] copycat mints (DexScreener)")
    found, errors = {}, []
    for q in COPYCAT_QUERIES:
        url = "https://api.dexscreener.com/latest/dex/search?q=" + urllib.parse.quote(q)
        d, err = http_json(url, retries=3)
        if err:
            errors.append(f"{q}: {err}")
            continue
        for p in (d or {}).get("pairs") or []:
            if p.get("chainId") != "solana":
                continue
            b = p.get("baseToken") or {}
            addr = b.get("address")
            if not addr or addr == MINT:
                continue  # never list the real mint as a copycat
            liq = (p.get("liquidity") or {}).get("usd") or 0
            prev = found.get(addr)
            if prev and (prev.get("liquidity_usd") or 0) >= liq:
                continue
            found[addr] = {
                "mint": addr,
                "symbol": b.get("symbol"),
                "name": b.get("name"),
                "dex": p.get("dexId"),
                "pair_address": p.get("pairAddress"),
                "liquidity_usd": liq,
                "fdv": p.get("fdv"),
                "price_usd": p.get("priceUsd"),
                "created_at": ts_iso(p["pairCreatedAt"] / 1000) if p.get("pairCreatedAt") else None,
                "matched_query": q,
                "url": p.get("url"),
            }
        time.sleep(0.4)

    real_name = "the toad pepe"
    for c in found.values():
        n = (c.get("name") or "").strip().lower()
        s = (c.get("symbol") or "").strip().upper()
        c["exact_name_clone"] = n == real_name
        c["symbol_clone"] = s == "TOAD"
        c["severity"] = (
            "exact_name_clone" if c["exact_name_clone"]
            else "symbol_clone" if c["symbol_clone"]
            else "themed"
        )

    items = sorted(
        found.values(),
        key=lambda c: (
            0 if c["exact_name_clone"] else 1 if c["symbol_clone"] else 2,
            -(c.get("liquidity_usd") or 0),
            -(c.get("fdv") or 0),
        ),
    )
    rec = {
        "collected_at": now_iso(),
        "canonical_mint": MINT,
        "queries": COPYCAT_QUERIES,
        "count": len(items),
        "exact_name_clones": sum(1 for c in items if c["exact_name_clone"]),
        "symbol_clones": sum(1 for c in items if c["symbol_clone"]),
        "items": items,
        "errors": errors,
        "caveat": "DexScreener search is keyword-based and returns only tokens that have "
        "a tradeable pair. It is a sample of impersonators, not an exhaustive registry. "
        "Most have negligible liquidity. The canonical mint is excluded by address.",
    }
    out["copycats"] = rec
    return rec


# ------------------------------------------------------------------- driver
def build_index(out: dict, campaign_scan):
    ledger, caveats, unverified = [], [], []

    def add(claim, node, extra=None):
        v = bool(node.get("verified")) if isinstance(node, dict) else bool(node)
        ledger.append({"claim": claim, "verified": v, "method": (node or {}).get("method") if isinstance(node, dict) else extra})
        if not v:
            unverified.append(claim)

    md = out.get("metadata") or {}
    au = out.get("authorities") or {}
    pf = out.get("pumpfun") or {}
    wp = out.get("wallet_proof") or {}
    tl = out.get("launch_timeline") or {}
    dp = out.get("deployer") or {}

    if md:
        add("Description text retrieved verbatim from the CID the mint points at",
            md.get("description", {}))
        add("Immutability chain: frozen on-chain uri + content-addressed CID",
            md.get("immutability_chain", {}))
        add("Mint is Token-2022", md.get("token_program", {}))
    if au:
        add("mintAuthority is null", au.get("mint_authority", {}))
        add("freezeAuthority is null", au.get("freeze_authority", {}))
        add("metadata updateAuthority is null (description immutable)", au.get("update_authority", {}))
        add("Only metadataPointer + tokenMetadata extensions present", au.get("extensions_present", {}))
        add("No transfer fee / hook / permanent delegate / default frozen", au.get("dangerous_extensions_absent", {}))
    if pf:
        add("pump.fun wallet<->handle mapping round-trips", pf.get("bidirectional_verified", {}))
        caveats.append(pf.get("caveat_x_link"))
    if wp:
        add("70,000 TOAD transfer occurred as described", wp.get("transfer", {}))
        add("Recipient token account has exactly one signature ever", wp.get("recipient_token_account", {}))
        add("Tweet exists and is authored as claimed", wp.get("tweet", {}))
        add("Recipient's pump.fun handle matches the replied-to X handle", wp.get("recipient_identity", {}))
        if wp.get("tweet_note"):
            caveats.append(wp["tweet_note"])
    if campaign_scan:
        ledger.append({
            "claim": "INVARIANT: sum(inbound) - sum(outbound) == ATA balance, residual exactly 0",
            "verified": campaign_scan["invariant_holds"],
            "method": "full ATA signature walk + pre/postTokenBalances, integer math",
        })
        if not campaign_scan["invariant_holds"]:
            unverified.append("campaign ATA invariant")
    if tl:
        for r in tl.get("rows") or []:
            if not r.get("verified"):
                unverified.append(f"timeline: {r.get('title')}")
                if r.get("caveat"):
                    caveats.append(f"{r.get('title')}: {r['caveat']}")
    if dp:
        add("Deployer balance identity holds", dp.get("balance_identity", {}))
        add("Creator pubkey decoded from BondingCurve byte offset 49 == deployer",
            dp.get("creator_onchain", {}))
        add("Deployer never sold (pool actions classified by AMM instruction name)",
            dp.get("never_sold", {}))
        caveats.append(dp.get("classification_note"))

    return {
        "collected_at": now_iso(),
        "mint": MINT,
        "token_program": TOKEN_2022,
        "decimals": DECIMALS,
        "supply_live": (md.get("supply") or {}).get("value"),
        "campaign_owner": CAMPAIGN_OWNER,
        "campaign_ata": CAMPAIGN_ATA,
        "deployer": DEPLOYER,
        "canonical_pricing_pool": PUMPSWAP_POOL,
        "files": [
            "metadata.json", "authorities.json", "pumpfun.json", "wallet_proof.json",
            "launch_timeline.json", "deployer.json", "copycats.json", "campaign_invariant.json",
        ],
        "verification_ledger": ledger,
        "verified_count": sum(1 for x in ledger if x["verified"]),
        "total_claims": len(ledger),
        "unverified": unverified,
        "caveats_the_site_must_render": [c for c in caveats if c],
    }


SECTIONS = ["metadata", "authorities", "pumpfun", "proof", "timeline", "deployer", "copycats"]


def main():
    ap = argparse.ArgumentParser(description="toad-wiki identity/metadata collector")
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--only", default="", help="comma-separated: " + ",".join(SECTIONS))
    ap.add_argument("--min-interval", type=float, default=0.25,
                    help="seconds between RPC calls (raise if you hit credit caps)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--fallback-rpc", default=DEFAULT_FALLBACK_RPC,
                    help="public RPC used only after the primary reports a credit cap "
                         "(DAS methods are never retried there); '' to disable")
    ap.add_argument("--skip-invariant", action="store_true",
                    help="skip the campaign ATA scan (it is the hero exhibit; skipping is for debugging only)")
    args = ap.parse_args()

    rpc_url = os.environ.get("HELIUS_RPC")
    if not rpc_url:
        raise SystemExit("HELIUS_RPC required in the environment (never hardcode it)")
    Secrets.add(rpc_url)
    Secrets.add(os.environ.get("X_BEARER_TOKEN"))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rpc = Rpc(rpc_url, out_dir / ".cache", args.min_interval, not args.no_cache,
              fallback_url=(args.fallback_rpc or None))

    only = [s.strip() for s in args.only.split(",") if s.strip()] or SECTIONS
    bad = [s for s in only if s not in SECTIONS]
    if bad:
        raise SystemExit(f"unknown section(s): {bad}; valid: {SECTIONS}")

    out: dict = {}
    campaign_scan = None
    exhausted = False

    def run(name, fn, force=False):
        nonlocal exhausted
        if not force and name not in only:
            return None
        # Exhaustion is only fatal when there is nowhere else to ask. With a
        # fallback configured, keep going -- Rpc.call reroutes automatically and
        # only DAS-only data is actually lost.
        if (exhausted or rpc.exhausted) and not rpc.fallback_url:
            exhausted = True
            log(f"  ! skipping {name}: RPC credits exhausted and no fallback configured")
            return None
        try:
            return fn()
        except RpcExhausted as e:
            exhausted = not rpc.fallback_url
            log(f"  !! RPC CREDIT CAP HIT during {name}: {Secrets.scrub(str(e))}")
            log("     Cached transactions are preserved; rerun to resume for free.")
            out.setdefault("_errors", []).append({"section": name, "error": "rpc_credits_exhausted"})
            return None
        except Exception as e:
            log(f"  ! {name} failed: {Secrets.scrub(str(e))}")
            out.setdefault("_errors", []).append({"section": name, "error": Secrets.scrub(str(e))})
            return None

    run("metadata", lambda: collect_metadata(rpc, out))
    run("authorities", lambda: collect_authorities(rpc, out))
    run("pumpfun", lambda: collect_pumpfun(rpc, out))

    # The campaign ATA scan powers the hero invariant AND supplies the true
    # first-airdrop row for the timeline.
    if not args.skip_invariant and ({"proof", "timeline"} & set(only)):
        log("[*] campaign ATA scan (hero invariant)")
        def _scan():
            nonlocal campaign_scan
            campaign_scan = scan_token_account(rpc, CAMPAIGN_ATA, MINT, "campaign")
            s = campaign_scan
            log(f"  inbound  {s['inbound_tx_count']} txs  {s['inbound']}")
            log(f"  outbound {s['outbound_tx_count']} txs / {s['outbound_transfer_count']} transfers  {s['outbound']}")
            log(f"  recipients {s['unique_recipient_count']}   balance {s['balance']}")
            log(f"  RESIDUAL {s['residual_raw']}  -> invariant {'HOLDS' if s['invariant_holds'] else 'BROKEN'}")
            floor = {
                "outbound_transfers": s["outbound_transfer_count"] >= EXPECTED_FLOOR["outbound_transfers"],
                "unique_recipients": s["unique_recipient_count"] >= EXPECTED_FLOOR["unique_recipients"],
                "outbound_raw": s["outbound_raw"] >= EXPECTED_FLOOR["outbound_raw"],
                "inbound_raw": s["inbound_raw"] >= EXPECTED_FLOOR["inbound_raw"],
                "inbound_txs": s["inbound_tx_count"] >= EXPECTED_FLOOR["inbound_txs"],
            }
            s["floor_check"] = floor
            s["floor_ok"] = all(floor.values())
            if not s["floor_ok"]:
                log(f"  !! BELOW EXPECTED FLOOR {floor} -- suspect a pagination or token-program bug")
            return campaign_scan
        run("_campaign_scan", _scan, force=True)
        if campaign_scan:
            (out_dir / "campaign_invariant.json").write_text(
                json.dumps(campaign_scan, indent=2, ensure_ascii=False) + "\n"
            )

    run("proof", lambda: collect_proof(rpc, out))
    run("timeline", lambda: collect_timeline(rpc, out, campaign_scan))
    run("deployer", lambda: collect_deployer(rpc, out))
    if "copycats" in only:
        try:
            collect_copycats(out)
        except Exception as e:
            log(f"  ! copycats failed: {Secrets.scrub(str(e))}")

    name_map = {
        "metadata": "metadata.json",
        "authorities": "authorities.json",
        "pumpfun": "pumpfun.json",
        "wallet_proof": "wallet_proof.json",
        "launch_timeline": "launch_timeline.json",
        "deployer": "deployer.json",
        "copycats": "copycats.json",
    }
    for key, fname in name_map.items():
        if key in out:
            (out_dir / fname).write_text(
                json.dumps(out[key], indent=2, ensure_ascii=False) + "\n"
            )
            log(f"  wrote {fname}")

    # The immutability chain's first link is proven by the authorities section;
    # stitch it in so metadata.json is self-contained for the frontend.
    try:
        ua = (out.get("authorities") or {}).get("update_authority") or {}
        chain = ((out.get("metadata") or {}).get("immutability_chain") or {}).get("value")
        if chain and "update_authority_is_null" in chain.get("link_1_uri_cannot_change", {}):
            chain["link_1_uri_cannot_change"]["update_authority_is_null"] = (
                ua.get("value") is None and ua.get("verified", False)
            )
            out["metadata"]["immutability_chain"]["verified"] = bool(
                chain["link_2_content_cannot_change"]["cid_present_in_onchain_uri"]
                and chain["link_1_uri_cannot_change"]["update_authority_is_null"]
            )
            (out_dir / "metadata.json").write_text(
                json.dumps(out["metadata"], indent=2, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"  ! could not stitch immutability chain: {Secrets.scrub(str(e))}")

    index = build_index(out, campaign_scan)
    index["rpc"] = {"primary_calls": rpc.calls, "fallback_calls": rpc.fallback_calls,
                    "cache_hits": rpc.cache_hits, "primary_exhausted": rpc.exhausted}
    if out.get("_errors"):
        index["errors"] = out["_errors"]
    (out_dir / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")

    log("")
    log(f"index.json: {index['verified_count']}/{index['total_claims']} claims verified")
    if index["unverified"]:
        log("UNVERIFIED:")
        for u in index["unverified"]:
            log(f"  - {u}")
    log(f"rpc calls={rpc.calls} fallback_calls={rpc.fallback_calls} cache_hits={rpc.cache_hits}")
    if rpc.fallback_calls:
        log(f"NOTE: {rpc.fallback_calls} calls were served by the public fallback RPC "
            f"after the primary hit its credit cap. DAS-only data (getAsset) cannot be "
            f"served there and is marked unverified if missing.")
    if exhausted or rpc.exhausted:
        log("NOTE: run ended early on an RPC credit cap. Rerun to resume from cache.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
