#!/usr/bin/env python3
"""Collect the complete $TOAD holder set and classify every airdrop recipient.

This is the collector behind the held/sold column. It answers two questions:

  1. Globally: who holds $TOAD, how much, and how concentrated is it?
  2. Per recipient: of the TOAD the campaign wallet sent them, how much is
     still sitting in their wallet right now?

Three facts about this mint break naive collectors, so they are pinned here:

  * $TOAD is a **Token-2022** mint. An SPL-classic-only scan returns an empty
    set and NO error. Every account query in this file targets
    TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb.
  * Supply DRIFTS (burns are ongoing). Concentration is always computed
    against a live getTokenSupply, never a hardcoded 1e9.
  * "No token account" is NOT "holds zero". A recipient who closed their
    account has no account at all. Those are emitted as separate states
    (account_closed vs zero_balance) because they mean different things:
    account_closed is an affirmative exit, zero_balance is an empty but
    still-open position.

The recipient set is read from transfers.json when it looks complete, and
re-derived straight from the campaign ATA's history when it does not, so a
stale trace downgrades to a slower run rather than to wrong percentages.

Writes data/collection/onchain/holders.json. Exits non-zero if the holder scan
fails to reconcile against supply, so a broken run cannot be mistaken for a
good one by whatever consumes the file next.

Usage:
    cd toad-wiki
    set -a && . ./.env && set +a
    python3 scripts/collect/holders.py

    --recipients derive   ignore transfers.json, re-derive from the ATA
    --recipients none     global holder scan only, no recipient classification
    --no-scan             skip getProgramAccounts, exercise the fallback path
    --max-holders 1000    write only the top N rows (stats still use all)

Env:
    HELIUS_RPC             primary endpoint (required)
    SOLANA_RPC_FALLBACKS   optional comma-separated failover endpoints

Never prints or serialises the RPC URL. Note that stripping a ?api-key= query
is NOT sufficient for Helius: it issues endpoints whose key is the subdomain,
so the bare hostname is itself a working credential. See endpoint_label.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, getcontext
from pathlib import Path

getcontext().prec = 50

SCRIPT_VERSION = "1.0.0"
# Matches scripts/collect/transfers.py: *_raw fields are decimal strings of
# base units, *_ui are exact decimal renderings. The site reads both files.
SCHEMA_VERSION = "2.0.0"

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "collection" / "onchain"
DEFAULT_OUT = OUT_DIR / "holders.json"
DEFAULT_TRANSFERS = OUT_DIR / "transfers.json"

MINT = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
DECIMALS = 6  # verified, but the live value from getTokenSupply wins

CAMPAIGN_OWNER = "FuP8dYQytaThMh9Fg2XNd1Z1eNHxMHW92kVUfWf3TnmD"
# Query the ATA, not the owner. The owner's signature history is polluted with
# spam airdrops from strangers (561 sigs vs 166 on the ATA).
CAMPAIGN_ATA = "AuA2VRui5JNWNWF79iyaSKpW7zMQLfzFZBjd2uS3YW2H"
DEPLOYER = "5YRgrP3mjGzrzirYYN5HAQH19cTYREYwGxW6XRJQUzij"
PUMPSWAP_POOL = "Nx9dcwNs3iJxM5YAxshMHE4aYJHdDyyGMhVcmaSgfu8"
BONDING_CURVE = "9oi3zoTqd1T8T3CVuSDfSNwjeWaj6zZLdYMLWNyayaeA"

# Owners that are infrastructure, not people. Concentration is reported twice:
# once raw, once with these removed, because "top 10 hold 29%" is misleading
# when the #1 slot is the undistributed campaign treasury and #5 is an AMM pool.
LABELLED_OWNERS = {
    CAMPAIGN_OWNER: "campaign_wallet",
    DEPLOYER: "deployer",
    PUMPSWAP_POOL: "pumpswap_pool",
    BONDING_CURVE: "bonding_curve",
}

# Known floor for the airdrop recipient set as of 2026-08-11T08:14Z. A cached
# transfers.json below this is stale (the previous collector capped pagination
# at 150 sigs newest-first and silently dropped launch day), so we re-derive.
RECIPIENT_FLOOR = 148

# Failover order. HELIUS_RPC first (fast, batch-friendly), then anything in
# SOLANA_RPC_FALLBACKS, then the public node. The public node is slower and
# rate-limits harder, but it does serve the full getProgramAccounts scan, which
# is what matters when the paid key hits its credit cap mid-run.
PUBLIC_FALLBACK_RPC = "https://api.mainnet-beta.solana.com"

# Token-2022 base account layout: mint[0:32] owner[32:64] amount[64:72] u64 LE.
# We slice owner+amount only; the full response is ~14 MB even sliced.
SLICE_OFFSET = 32
SLICE_LENGTH = 40
TOKEN_ACCOUNT_MIN_SPACE = 165  # 165 = base; >165 means Token-2022 extensions

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    """Base58 encode 32-byte pubkeys pulled out of a dataSlice."""
    n = int.from_bytes(raw, "big")
    out = []
    while n:
        n, rem = divmod(n, 58)
        out.append(_B58[rem])
    for byte in raw:
        if byte:
            break
        out.append("1")
    return "".join(reversed(out))


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ui(raw: int, decimals: int = DECIMALS) -> str:
    """Exact decimal rendering of a base-unit integer, by string slicing.

    Never float division: 960573204223632 / 1e6 is not exactly representable,
    and a rounding error in the supply denominator moves every concentration
    percentage on the page.
    """
    if decimals <= 0:
        return str(raw)
    sign = "-" if raw < 0 else ""
    digits = str(abs(raw)).rjust(decimals + 1, "0")
    return f"{sign}{digits[:-decimals]}.{digits[-decimals:]}"


def pct_str(part: int, whole: int, places: int = 6) -> str | None:
    """Exact percentage from two integers, via Decimal. Never float."""
    if not whole:
        return None
    value = (Decimal(part) * 100 / Decimal(whole)).quantize(
        Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
    )
    return format(value, "f")


class RpcRejected(Exception):
    """Every endpoint refused, or the request itself is malformed."""


# Hosts that are public and unauthenticated, so naming them in committed output
# reveals nothing. Anything not on this list is treated as credential-bearing.
PUBLIC_RPC_HOSTS = frozenset({
    "api.mainnet-beta.solana.com",
    "api.devnet.solana.com",
    "api.testnet.solana.com",
    "solana-rpc.publicnode.com",
})


def endpoint_label(url: str) -> str:
    """A provenance-safe name for an endpoint.

    Redacting the query string is not enough. Helius issues endpoints whose
    API key IS the subdomain -- there is no ?api-key= to strip, and a plain
    hostname like <account-slug>-mainnet.helius-rpc.com is a working
    credential on its own. This file is committed and published, so anything
    not on the public allowlist is reduced to its registrable domain, which
    identifies the provider without handing out access.
    """
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    if not host:
        return "unknown"
    if host in PUBLIC_RPC_HOSTS:
        return host
    parts = host.split(".")
    domain = ".".join(parts[-2:]) if len(parts) > 2 else host
    return f"{domain} (keyed endpoint, subdomain redacted)"


class Rpc:
    """JSON-RPC client with backoff, jitter, batching, and endpoint failover.

    Two distinct failure modes are handled, because they need opposite
    responses:

      * Transient (socket reset, timeout, burst 429, 5xx) -- retry the SAME
        endpoint with exponential backoff and jitter.
      * Exhausted (Helius `max usage reached`, 401/403, sustained 429) --
        retrying is pointless; rotate to the next endpoint permanently.

    A run that quietly returned partial data would poison the held/sold column,
    so exhausting every endpoint raises rather than returning what it managed
    to collect.
    """

    RETRY_HTTP = {408, 425, 429, 500, 502, 503, 504}
    ROTATE_HTTP = {401, 402, 403}  # auth walls: another try will never help
    RETRY_RPC_CODES = {-32005, -32603}  # node behind / internal
    # Helius answers a spent plan with this, sometimes as a 200 body. It is a
    # wall, not a burst limit -- backing off just burns wall-clock time.
    CREDIT_WALL_MARKERS = ("max usage reached", "credit limit", "quota")
    MAX_PASSES = 3  # full trips around the endpoint ring before giving up

    def __init__(self, urls, max_retries: int = 6, base_delay: float = 1.0,
                 timeout: int = 180, min_interval: float = 0.05, verbose: bool = True) -> None:
        self._urls = [u for u in urls if u]
        if not self._urls:
            raise SystemExit("no RPC endpoint: set HELIUS_RPC (see .env.example)")
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.timeout = timeout
        self.min_interval = min_interval
        self.verbose = verbose
        self.request_count = 0
        self.retry_count = 0
        self.rotations = 0
        self.throttled = 0
        self._idx = 0
        self._last_sent = 0.0
        # Adaptive pacing. The public node allows a fraction of Helius's rate,
        # and its 429 is a burst limit that recovers -- so widen the gap on
        # every 429 and let it decay back down once requests start landing,
        # instead of alternating between hammering and stalling.
        self._interval = min_interval
        self.endpoints_used: list[str] = [endpoint_label(self._urls[0])]

    @property
    def endpoint(self) -> str:
        return endpoint_label(self._urls[self._idx])

    @property
    def on_primary(self) -> bool:
        return self._idx == 0

    def _rotate(self, why: str) -> None:
        """Move to the next endpoint, wrapping around the ring.

        Wrapping matters because a credit cap can lift mid-run and the primary
        is much faster than any public fallback. The per-request pass budget in
        _send is what stops the ring from spinning forever.
        """
        wrapped = self._idx + 1 >= len(self._urls)
        self._idx = 0 if wrapped else self._idx + 1
        self.rotations += 1
        self._interval = self._floor_interval()  # fresh endpoint, fresh budget
        label = self.endpoint
        if label not in self.endpoints_used:
            self.endpoints_used.append(label)
        if self.verbose:
            print(f"    endpoint unusable ({why}) -> failing over to {label}", file=sys.stderr)
        if wrapped and len(self._urls) > 1:
            if self.verbose:
                print("    wrapped through every endpoint; cooling down 30s", file=sys.stderr)
            time.sleep(30)

    def _floor_interval(self) -> float:
        """Public endpoints need a much wider default gap than a paid one."""
        return self.min_interval if self.on_primary else max(self.min_interval, 0.35)

    def _post(self, payload):
        # Pacing costs a few seconds per run and buys back far more in avoided
        # backoff, especially on the shared public endpoints.
        gap = self._interval - (time.monotonic() - self._last_sent)
        if gap > 0:
            time.sleep(gap)
        self._last_sent = time.monotonic()
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._urls[self._idx],
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            out = json.loads(resp.read())
        self._interval = max(self._floor_interval(), self._interval * 0.92)
        return out

    def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        self.retry_count += 1
        if retry_after:
            try:
                time.sleep(min(60.0, float(retry_after)))
                return
            except ValueError:
                pass
        time.sleep(min(30.0, self.base_delay * (2 ** attempt)) + random.random() * 0.5)

    def _send(self, payload, what: str):
        last = "?"
        # Budget is per request, not per run. A global budget gets spent by the
        # early phases and leaves the last few calls with no failover left --
        # which is exactly how a run dies 6 lookups from the finish line.
        for _pass in range(len(self._urls) * self.MAX_PASSES):
            for attempt in range(self.max_retries):
                try:
                    self.request_count += 1
                    return self._post(payload)
                except urllib.error.HTTPError as exc:
                    last = f"HTTP {exc.code} @{self.endpoint}"
                    if exc.code in self.ROTATE_HTTP:
                        break  # auth wall: stop retrying, rotate
                    if exc.code not in self.RETRY_HTTP:
                        raise RpcRejected(f"{what}: HTTP {exc.code} (not retryable)") from exc
                    if exc.code == 429:
                        try:
                            detail = exc.read().decode("utf-8", "replace")[:400].lower()
                        except Exception:  # noqa: BLE001
                            detail = ""
                        if any(m in detail for m in self.CREDIT_WALL_MARKERS):
                            last = f"credit wall @{self.endpoint}"
                            break  # spent plan: rotate now, do not wait it out
                        # burst limit: widen the gap so the retry is not wasted
                        self._interval = min(3.0, max(self._interval * 2, 0.25))
                        self.throttled += 1
                    if attempt == self.max_retries - 1:
                        break
                    if self.verbose:
                        print(f"    {what}: HTTP {exc.code}, backing off ({attempt + 1}), "
                              f"pacing {self._interval:.2f}s", file=sys.stderr)
                    self._sleep(attempt, exc.headers.get("Retry-After"))
                except Exception as exc:  # noqa: BLE001 - transport blips are many-shaped
                    # RemoteDisconnected, ConnectionReset, socket timeout,
                    # truncated JSON. Network noise; only exhaustion is fatal.
                    last = f"{type(exc).__name__}: {exc}"
                    if attempt == self.max_retries - 1:
                        break
                    if self.verbose:
                        print(f"    {what}: {type(exc).__name__}, backing off ({attempt + 1})", file=sys.stderr)
                    self._sleep(attempt)
            self._rotate(last)
        raise RpcRejected(f"{what}: all endpoints failed, last error {last}")

    def call(self, method: str, params):
        for attempt in range(self.max_retries):
            data = self._send({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, method)
            err = data.get("error")
            if err:
                code = err.get("code") if isinstance(err, dict) else None
                if code in self.RETRY_RPC_CODES and attempt < self.max_retries - 1:
                    self._sleep(attempt)
                    continue
                # Credit walls sometimes arrive as a 200 with an error body.
                if code == -32429 and attempt < self.max_retries - 1:
                    self._rotate(f"rpc {code}")
                    continue
                raise RpcRejected(f"{method}: {err}")
            return data.get("result")
        raise RpcRejected(f"{method}: exhausted retries")

    def batch(self, calls, chunk: int = 20, pause: float = 0.02):
        """Run (method, params) pairs as JSON-RPC batches, preserving order.

        Batching is what makes the per-transaction walk survivable on a paid
        endpoint: 166 solo requests trip the rate limiter, 9 batches do not.

        Public endpoints price a batch as N requests and reject multi-call
        batches of heavy methods outright, so on a fallback the chunk collapses
        to 1 and the wider per-endpoint pacing carries the load instead.
        """
        results = [None] * len(calls)
        start = 0
        while start < len(calls):
            width = chunk if self.on_primary else 1
            window = list(range(start, min(start + width, len(calls))))
            self._batch_window(calls, window, results)
            start = window[-1] + 1
            if pause:
                time.sleep(pause)
        return results

    def _batch_window(self, calls, window, results) -> None:
        if len(window) == 1:
            idx = window[0]
            results[idx] = self.call(*calls[idx])
            return
        payload = [
            {"jsonrpc": "2.0", "id": idx, "method": calls[idx][0], "params": calls[idx][1]}
            for idx in window
        ]
        try:
            data = self._send(payload, f"batch[{len(window)}]")
            if isinstance(data, dict):  # server collapsed the batch into one error
                raise RpcRejected(f"batch: {data.get('error')}")
            by_id = {row.get("id"): row for row in data}
            missing = [i for i in window if by_id.get(i) is None]
            if missing:
                raise RpcRejected(f"batch: missing responses for ids {missing[:5]}")
            failed = [i for i in window if by_id[i].get("error")]
            if failed:
                raise RpcRejected(f"batch {calls[failed[0]][0]}: {by_id[failed[0]]['error']}")
        except RpcRejected:
            # Split rather than lose the window: one poison call must not take
            # 19 good ones with it, and halving also walks the batch down to
            # single calls if the endpoint simply refuses to serve batches.
            mid = len(window) // 2
            self._batch_window(calls, window[:mid], results)
            self._batch_window(calls, window[mid:], results)
            return
        for idx in window:
            results[idx] = by_id[idx].get("result")


# --------------------------------------------------------------------------
# supply + holder scan
# --------------------------------------------------------------------------


def fetch_supply(rpc: Rpc) -> dict:
    """Live supply. Never hardcode this: burns are ongoing and it drifts."""
    res = rpc.call("getTokenSupply", [MINT, {"commitment": "confirmed"}])
    value = res["value"]
    return {
        "raw": int(value["amount"]),
        "decimals": int(value["decimals"]),
        "ui": float(value["uiAmountString"]),
        "slot": res["context"]["slot"],
    }


def fetch_campaign_ata(rpc: Rpc) -> dict:
    res = rpc.call("getAccountInfo", [CAMPAIGN_ATA, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    value = res.get("value")
    if not value:
        return {"exists": False, "raw": 0, "slot": res["context"]["slot"]}
    info = value["data"]["parsed"]["info"]
    return {
        "exists": True,
        "raw": int(info["tokenAmount"]["amount"]),
        "owner": info["owner"],
        "program_owner": value["owner"],
        "slot": res["context"]["slot"],
    }


def scan_holders(rpc: Rpc) -> dict:
    """Enumerate every Token-2022 account for the mint via getProgramAccounts.

    memcmp offset 0 == mint selects token accounts of this mint. dataSlice
    keeps only owner+amount, which is the difference between a ~14 MB response
    and a ~250 MB one. No dataSize filter: Token-2022 accounts carry extensions
    (the campaign ATA itself is 170 bytes, not 165), so filtering on 165 would
    drop most of the set. Non-token accounts are excluded by `space` instead.
    """
    res = rpc.call(
        "getProgramAccounts",
        [
            TOKEN_2022_PROGRAM,
            {
                "encoding": "base64",
                "commitment": "confirmed",
                "withContext": True,
                "dataSlice": {"offset": SLICE_OFFSET, "length": SLICE_LENGTH},
                "filters": [{"memcmp": {"offset": 0, "bytes": MINT}}],
            },
        ],
    )
    slot = res["context"]["slot"]
    rows = res["value"]

    by_owner: dict[str, dict] = {}
    scanned = 0
    skipped_space = 0
    skipped_short = 0
    total_raw = 0

    for row in rows:
        # Only reject on a space the node actually reported. Some RPC builds
        # omit `space`; treating "missing" as 0 would silently discard the
        # entire holder set, which is exactly the class of bug this collector
        # exists to stop.
        space = row["account"].get("space")
        if space is not None and space < TOKEN_ACCOUNT_MIN_SPACE:
            skipped_space += 1
            continue
        raw = base64.b64decode(row["account"]["data"][0])
        if len(raw) < SLICE_LENGTH:
            skipped_short += 1
            continue
        owner = b58encode(raw[:32])
        amount = struct.unpack("<Q", raw[32:40])[0]
        scanned += 1
        total_raw += amount
        entry = by_owner.get(owner)
        if entry is None:
            by_owner[owner] = {"raw": amount, "accounts": 1, "nonzero_accounts": 1 if amount else 0}
        else:
            entry["raw"] += amount
            entry["accounts"] += 1
            if amount:
                entry["nonzero_accounts"] += 1

    return {
        "slot": slot,
        "endpoint": rpc.endpoint,
        "by_owner": by_owner,
        "token_accounts": scanned,
        "token_accounts_returned": len(rows),
        "skipped_not_token_account": skipped_space,
        "skipped_short_slice": skipped_short,
        "total_raw": total_raw,
    }


def scan_until_reconciled(rpc: Rpc, attempts: int = 4, verbose: bool = True):
    """Scan, then verify the sum against live supply, and re-scan if it is off.

    The mint has no mint authority, so supply cannot rise -- which makes
    "every token account summed == getTokenSupply" an exact identity, and any
    deviation proof that the scan is wrong rather than the supply.

    That matters because getProgramAccounts is not guaranteed to be internally
    consistent under load. A node serving a 49k-account response can mix
    account versions across slots and count an in-flight transfer on both
    sides. Observed on the public endpoint: one scan came back 342 TOAD over,
    the next reconciled exactly. Nothing in the response marks the bad one.

    So the invariant is a gate, not a report: an unreconciled scan is retried
    and, if it never closes, flagged rather than published as fact.
    """
    last = None
    for attempt in range(attempts):
        scan = scan_holders(rpc)
        # Read supply AFTER the scan so a burn landing mid-scan is visible
        # rather than being mistaken for a scan defect.
        supply = fetch_supply(rpc)
        delta = scan["total_raw"] - supply["raw"]
        last = (scan, supply, delta)
        if delta == 0:
            if verbose and attempt:
                print(f"  reconciled on attempt {attempt + 1}")
            return scan, supply, True
        if verbose:
            print(f"  scan sum off by {delta / 10 ** supply['decimals']:+,.6f} TOAD "
                  f"(attempt {attempt + 1}/{attempts}) -- node served an inconsistent "
                  f"snapshot, re-scanning", file=sys.stderr)
        if attempt < attempts - 1:
            time.sleep(2.0 * (attempt + 1))
    return last[0], last[1], False


# --------------------------------------------------------------------------
# recipient set
# --------------------------------------------------------------------------


def recipients_from_transfers(path: Path) -> dict | None:
    """Read the airdrop recipient set out of transfers.json, if it looks sane."""
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None

    totals: dict[str, int] = {}
    for row in doc.get("recipients") or []:
        wallet = row.get("wallet")
        if not wallet:
            continue
        if row.get("total_raw") is not None:
            # schema >=2: decimal string in base units
            totals[wallet] = totals.get(wallet, 0) + int(row["total_raw"])
        elif row.get("total") is not None:
            # schema 1: float in UI units. Lossy by construction, so round-trip
            # it and let the invariant check downstream expose any drift.
            totals[wallet] = totals.get(wallet, 0) + round(float(row["total"]) * 10 ** DECIMALS)
    if not totals:
        return None

    tot = doc.get("totals") or {}
    inv = doc.get("invariant") or {}

    def as_int(*candidates):
        for value in candidates:
            if value is not None:
                return int(value)
        return None

    return {
        "totals": totals,
        "source": "transfers.json",
        "source_schema": doc.get("schema_version"),
        "source_generator": doc.get("generator"),
        "source_collected_at": doc.get("collected_at"),
        "outbound_raw": as_int(tot.get("outbound_total_raw"), inv.get("outflow_raw")) or sum(totals.values()),
        "inbound_raw": as_int(tot.get("inbound_total_raw"), inv.get("inflow_raw")),
        "inbound_txs": tot.get("inbound_tx_count"),
        "transfer_count": tot.get("outbound_transfer_count") or doc.get("transfer_count"),
        "first_drop_ts": tot.get("first_outbound_ts"),
        "last_drop_ts": tot.get("last_outbound_ts"),
    }


def derive_recipients(rpc: Rpc, verbose: bool = True) -> dict:
    """Re-derive the recipient set from the campaign ATA's own history.

    Signatures come off the ATA (166) rather than the owner (561: the rest is
    inbound spam from strangers), and pagination runs to exhaustion oldest
    included -- capping at N newest silently amputates launch day.

    Amounts are read as raw integer strings from pre/postTokenBalances, never
    uiAmount floats, so the ledger invariant closes exactly:
        sum(inbound_raw) - sum(outbound_raw) == ATA balance
    """
    sigs = []
    before = None
    while True:
        opts = {"limit": 1000, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        batch = rpc.call("getSignaturesForAddress", [CAMPAIGN_ATA, opts]) or []
        if not batch:
            break
        sigs.extend(batch)
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    if verbose:
        print(f"  ATA signatures: {len(sigs)}")

    ok = [s for s in sigs if not s.get("err")]
    calls = [
        ("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        for s in ok
    ]
    txs = rpc.batch(calls, chunk=20)

    totals: dict[str, int] = {}
    transfers = 0
    outbound_raw = 0
    inbound_raw = 0
    inbound_txs = 0
    first_ts = None
    last_ts = None

    # A transaction we could not fetch is a hole in the trace, and a hole here
    # is invisible downstream -- it just makes the recipient set look smaller.
    # That is precisely how the previous collector lost launch day. Refuse.
    unfetched = [s["signature"] for s, tx in zip(ok, txs) if not tx]
    if unfetched:
        raise RpcRejected(
            f"{len(unfetched)} of {len(ok)} ATA transactions did not fetch "
            f"(e.g. {unfetched[0]}); refusing to derive a recipient set from an "
            "incomplete history"
        )

    for sig_row, tx in zip(ok, txs):
        meta = tx.get("meta") or {}
        if meta.get("err"):
            continue  # failed tx moved nothing

        def side(key):
            agg: dict[str, int] = {}
            for bal in meta.get(key) or []:
                if bal.get("mint") != MINT:
                    continue
                owner = bal.get("owner")
                if not owner:
                    continue
                agg[owner] = agg.get(owner, 0) + int(bal["uiTokenAmount"]["amount"])
            return agg

        pre, post = side("preTokenBalances"), side("postTokenBalances")
        delta = {o: post.get(o, 0) - pre.get(o, 0) for o in set(pre) | set(post)}
        moved = delta.get(CAMPAIGN_OWNER, 0)
        if moved == 0:
            continue
        ts = sig_row.get("blockTime")
        if ts:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        if moved > 0:
            inbound_raw += moved
            inbound_txs += 1
            continue
        outbound_raw += -moved
        for owner, change in delta.items():
            if owner == CAMPAIGN_OWNER or change <= 0:
                continue
            totals[owner] = totals.get(owner, 0) + change
            transfers += 1

    return {
        "totals": totals,
        "source": "derived_from_campaign_ata",
        "source_collected_at": utcnow(),
        "outbound_raw": outbound_raw,
        "inbound_raw": inbound_raw,
        "inbound_txs": inbound_txs,
        "transfer_count": transfers,
        "signatures_scanned": len(sigs),
        "first_drop_ts": first_ts,
        "last_drop_ts": last_ts,
    }


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def classify(received_raw: int, balance_raw: int, has_account: bool) -> str:
    """Four states, and the first two are NOT the same thing.

    account_closed: getTokenAccountsByOwner returns nothing. For someone who
        provably received tokens this is an affirmative exit -- they emptied
        the account and reclaimed its rent.
    zero_balance:   the account exists and holds 0. Empty, still open.
    holding_partial / holding_full: self-explanatory. holding_full covers
        "bought more on top", so held_pct can exceed 100.
    """
    if not has_account:
        return "account_closed"
    if balance_raw == 0:
        return "zero_balance"
    if balance_raw >= received_raw:
        return "holding_full"
    return "holding_partial"


def build_recipient_rows(rpc: Rpc, totals: dict[str, int], by_owner: dict | None,
                         verbose: bool = True) -> tuple[list[dict], dict]:
    rows = []
    missing = []

    for owner, received in totals.items():
        entry = by_owner.get(owner) if by_owner is not None else None
        if by_owner is None or entry is None:
            missing.append(owner)
            rows.append({"wallet": owner, "received_raw": received, "_pending": True})
        else:
            rows.append(
                {
                    "wallet": owner,
                    "received_raw": received,
                    "balance_raw": entry["raw"],
                    "token_accounts": entry["accounts"],
                    "status": classify(received, entry["raw"], True),
                    "checked_via": "program_scan",
                }
            )

    # Anyone absent from the scan gets a direct second look. Absence is the
    # signal for account_closed, and it is worth one RPC each to confirm it
    # rather than inferring a closure from a gap in a 49k-row response.
    scan_gaps = 0
    if missing:
        if verbose:
            print(f"  {len(missing)} recipient(s) absent from scan -> direct getTokenAccountsByOwner")
        calls = [
            ("getTokenAccountsByOwner", [o, {"mint": MINT}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
            for o in missing
        ]
        results = rpc.batch(calls, chunk=20)
        direct = {}
        for owner, res in zip(missing, results):
            # An empty list is the proof of a closed account, so a *missing*
            # result must never be allowed to look like one. Fail loudly
            # instead of reporting a lookup failure as "they closed and left".
            if res is None or "value" not in res:
                raise RpcRejected(
                    f"getTokenAccountsByOwner returned no result for {owner}; refusing to "
                    "record an unverified account_closed"
                )
            accounts = res["value"] or []
            raw = sum(
                int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in accounts
            )
            direct[owner] = (len(accounts), raw)
        for row in rows:
            if not row.pop("_pending", False):
                continue
            n_accounts, raw = direct[row["wallet"]]
            row["balance_raw"] = raw
            row["token_accounts"] = n_accounts
            row["status"] = classify(row["received_raw"], raw, n_accounts > 0)
            row["checked_via"] = "direct_lookup"
            if n_accounts > 0 and by_owner is not None:
                # Scan said no account, direct lookup says otherwise: slot skew
                # or an incomplete scan. Flag it rather than silently trusting.
                row["scan_gap"] = True
                scan_gaps += 1

    for row in rows:
        received = row["received_raw"]
        row["held_pct"] = pct_str(row["balance_raw"], received, places=4)
        row["still_held_raw"] = min(row["balance_raw"], received)
        row["moved_out_raw"] = max(received - row["balance_raw"], 0)
        if row["wallet"] in LABELLED_OWNERS:
            row["label"] = LABELLED_OWNERS[row["wallet"]]

    rows.sort(key=lambda r: (-r["received_raw"], r["wallet"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank

    summary = {"scan_gaps": scan_gaps, "direct_lookups": len(missing)}
    return rows, summary


def recipient_to_wire(row: dict, decimals: int) -> dict:
    """Serialise a recipient row: integers out as decimal strings, per units."""
    out = {
        "rank": row["rank"],
        "wallet": row["wallet"],
        "status": row["status"],
        "received_raw": str(row["received_raw"]),
        "received_ui": ui(row["received_raw"], decimals),
        "balance_raw": str(row["balance_raw"]),
        "balance_ui": ui(row["balance_raw"], decimals),
        "still_held_raw": str(row["still_held_raw"]),
        "still_held_ui": ui(row["still_held_raw"], decimals),
        "moved_out_raw": str(row["moved_out_raw"]),
        "moved_out_ui": ui(row["moved_out_raw"], decimals),
        "held_pct": row["held_pct"],
        "token_accounts": row["token_accounts"],
        "checked_via": row["checked_via"],
    }
    if "label" in row:
        out["label"] = row["label"]
    if row.get("scan_gap"):
        out["scan_gap"] = True
    return out


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------


def gini(values: list[int]) -> str | None:
    """Gini over non-zero balances. 0 = perfectly even, 1 = one wallet owns all."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    total = sum(ordered)
    if total == 0:
        return None
    weighted = sum((i + 1) * v for i, v in enumerate(ordered))
    value = (Decimal(2 * weighted) / Decimal(n * total)) - (Decimal(n + 1) / Decimal(n))
    return format(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")


def concentration(balances: list[int], denominator_raw: int, decimals: int) -> dict:
    ordered = sorted(balances, reverse=True)
    out: dict = {"denominator_raw": str(denominator_raw), "denominator_ui": ui(denominator_raw, decimals)}
    running = 0
    cuts = (1, 10, 25, 50, 100, 500, 1000)
    for k in cuts:
        running = sum(ordered[:k])
        out[f"top{k}_raw"] = str(running)
        out[f"top{k}_ui"] = ui(running, decimals)
        out[f"top{k}_pct"] = pct_str(running, denominator_raw)
    out["gini"] = gini(ordered)
    return out


def build_stats(by_owner: dict, supply: dict) -> dict:
    supply_raw = supply["raw"]
    decimals = supply["decimals"]
    nonzero = {o: e["raw"] for o, e in by_owner.items() if e["raw"] > 0}
    balances = list(nonzero.values())

    unit = 10 ** decimals
    buckets = {}
    for label, floor in (
        ("gte_1", 1), ("gte_1k", 1_000), ("gte_10k", 10_000),
        ("gte_100k", 100_000), ("gte_1m", 1_000_000), ("gte_10m", 10_000_000),
    ):
        buckets[label] = sum(1 for v in balances if v >= floor * unit)

    # Same numbers with the four known infrastructure accounts removed. The raw
    # top-10 is dominated by the undistributed campaign treasury and an AMM
    # pool, which reads as insider concentration when it is the opposite.
    ex_infra = {o: v for o, v in nonzero.items() if o not in LABELLED_OWNERS}
    labelled_raw = sum(v for o, v in nonzero.items() if o in LABELLED_OWNERS)

    return {
        "holder_count": len(nonzero),
        "owner_count_including_zero": len(by_owner),
        "zero_balance_owner_count": len(by_owner) - len(nonzero),
        "balance_buckets": buckets,
        "concentration": concentration(balances, supply_raw, decimals),
        "concentration_excluding_infrastructure": {
            **concentration(list(ex_infra.values()), supply_raw - labelled_raw, decimals),
            "holder_count": len(ex_infra),
            "excluded": LABELLED_OWNERS,
            "excluded_raw": str(labelled_raw),
            "excluded_ui": ui(labelled_raw, decimals),
            "note": (
                "Denominator is supply minus the excluded accounts, so these "
                "percentages describe concentration among holders who are not "
                "the treasury, the deployer, or a liquidity venue."
            ),
        },
    }


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def dump(payload: dict, holders_rows: list, path: Path) -> None:
    """Pretty-print the document but keep the 19k-row holder table one-per-line."""
    marker = "@@HOLDERS@@"
    payload["holders"] = marker
    text = json.dumps(payload, indent=2)
    if holders_rows:
        table = "[\n" + ",\n".join(
            "    " + json.dumps(r, separators=(",", ":")) for r in holders_rows
        ) + "\n  ]"
    else:
        table = "[]"
    text = text.replace(f'"{marker}"', table)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--transfers", default=str(DEFAULT_TRANSFERS),
                    help="cached transfer trace used for the recipient set")
    ap.add_argument("--recipients", choices=("auto", "cache", "derive", "none"), default="auto",
                    help="auto: use cache if it clears --recipient-floor, else re-derive")
    ap.add_argument("--recipient-floor", type=int, default=RECIPIENT_FLOOR,
                    help="reject a cached recipient set smaller than this as stale")
    ap.add_argument("--no-scan", action="store_true",
                    help="skip getProgramAccounts to exercise the fallback path")
    ap.add_argument("--scan-attempts", type=int, default=4,
                    help="re-scan this many times trying to close the supply invariant")
    ap.add_argument("--allow-unreconciled", action="store_true",
                    help="exit 0 even if the scan never reconciles against supply")
    ap.add_argument("--max-holders", type=int, default=0,
                    help="cap rows written to the holders table (0 = all); stats always use all")
    ap.add_argument("--no-public-fallback", action="store_true",
                    help="do not fail over to the public RPC if the keyed endpoint is exhausted")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    verbose = not args.quiet

    endpoints = [os.environ.get("HELIUS_RPC", "").strip()]
    endpoints += [u.strip() for u in os.environ.get("SOLANA_RPC_FALLBACKS", "").split(",")]
    if not args.no_public_fallback:
        endpoints.append(PUBLIC_FALLBACK_RPC)
    rpc = Rpc([u for u in endpoints if u], verbose=verbose)
    started = time.time()

    if verbose:
        print(f"mint     {MINT}")
        print(f"program  {TOKEN_2022_PROGRAM} (Token-2022)")
        print(f"rpc      {rpc.endpoint} (+{len(rpc._urls) - 1} failover)")

    # ---- global scan, gated on the supply invariant -------------------------
    scan = None
    scan_error = None
    reconciles = False
    if args.no_scan:
        scan_error = "skipped via --no-scan"
        supply = fetch_supply(rpc)
    else:
        if verbose:
            print("scanning getProgramAccounts (Token-2022, memcmp mint, dataSlice owner+amount)...")
        try:
            scan, supply, reconciles = scan_until_reconciled(
                rpc, attempts=args.scan_attempts, verbose=verbose
            )
        except RpcRejected as exc:
            scan_error = str(exc)
            print(f"  SCAN FAILED: {exc}", file=sys.stderr)
            print("  falling back to per-recipient lookups; holder_count -> null", file=sys.stderr)
            supply = fetch_supply(rpc)

    if verbose:
        print(f"supply   {supply['ui']:,.6f} TOAD @ slot {supply['slot']} (live, not hardcoded)")

    ata = fetch_campaign_ata(rpc)

    by_owner = scan["by_owner"] if scan else None
    if scan and verbose:
        print(f"  {scan['token_accounts']:,} token accounts -> {len(by_owner):,} owners @ slot {scan['slot']}")

    # ---- recipient set -----------------------------------------------------
    recips = None
    cache_rejected = None
    if args.recipients in ("auto", "cache"):
        recips = recipients_from_transfers(Path(args.transfers))
        if recips and args.recipients == "auto" and len(recips["totals"]) < args.recipient_floor:
            cache_rejected = (
                f"{Path(args.transfers).name} holds {len(recips['totals'])} recipients, "
                f"below floor {args.recipient_floor} -- treating as stale"
            )
            if verbose:
                print(f"  {cache_rejected}")
            recips = None
    if recips is None and args.recipients in ("auto", "derive"):
        if verbose:
            print("deriving recipient set from campaign ATA history...")
        recips = derive_recipients(rpc, verbose=verbose)

    recipient_rows: list[dict] = []
    recipient_summary: dict = {}
    invariant: dict | None = None

    if recips:
        if verbose:
            print(f"  {len(recips['totals'])} recipients from {recips['source']}")
        recipient_rows, meta = build_recipient_rows(rpc, recips["totals"], by_owner, verbose=verbose)

        counts: dict[str, int] = {}
        raws: dict[str, int] = {}
        for row in recipient_rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            raws[row["status"]] = raws.get(row["status"], 0) + row["received_raw"]
        received_total = sum(r["received_raw"] for r in recipient_rows)
        held_total = sum(r["still_held_raw"] for r in recipient_rows)
        closed_raw = raws.get("account_closed", 0)

        dec = supply["decimals"]
        recipient_summary = {
            "source": recips["source"],
            "source_schema": recips.get("source_schema"),
            "source_generator": recips.get("source_generator"),
            "source_collected_at": recips.get("source_collected_at"),
            "cache_rejected": cache_rejected,
            "recipient_count": len(recipient_rows),
            "transfer_count": recips.get("transfer_count"),
            "received_raw": str(received_total),
            "received_ui": ui(received_total, dec),
            "still_held_raw": str(held_total),
            "still_held_ui": ui(held_total, dec),
            "still_held_pct_of_airdrop": pct_str(held_total, received_total),
            "moved_out_raw": str(received_total - held_total),
            "moved_out_ui": ui(received_total - held_total, dec),
            "status_counts": counts,
            "status_received_raw": {k: str(v) for k, v in raws.items()},
            "status_received_ui": {k: ui(v, dec) for k, v in raws.items()},
            "account_closed_raw": str(closed_raw),
            "account_closed_ui": ui(closed_raw, dec),
            "scan_gaps": meta["scan_gaps"],
            "direct_lookups": meta["direct_lookups"],
            "note": (
                "moved_out means the tokens left the wallet. That covers selling, "
                "transferring to another wallet, and depositing to an exchange -- it is "
                "not proof of a sale. account_closed and zero_balance are distinct: "
                "account_closed means no token account exists at all, which is an "
                "affirmative exit; zero_balance means the account is open and empty."
            ),
        }

        if recips.get("inbound_raw") is not None and ata.get("exists"):
            # Re-checked here against a LIVE ATA read rather than copied from
            # transfers.json. That makes this a staleness detector as well as a
            # ledger proof: if drops have landed since the trace was collected,
            # the residual goes nonzero and names the reason.
            residual = recips["inbound_raw"] - recips["outbound_raw"] - ata["raw"]
            invariant = {
                "formula": "sum(inbound_raw) - sum(outbound_raw) - getAccountInfo(ATA).amount == 0",
                "inflow_raw": str(recips["inbound_raw"]),
                "inflow_ui": ui(recips["inbound_raw"], dec),
                "inflow_txs": recips.get("inbound_txs"),
                "outflow_raw": str(recips["outbound_raw"]),
                "outflow_ui": ui(recips["outbound_raw"], dec),
                "balance_onchain_raw": str(ata["raw"]),
                "balance_onchain_ui": ui(ata["raw"], dec),
                "residual_raw": str(residual),
                "holds": residual == 0,
                "checked_at_slot": ata["slot"],
                "flows_from": recips["source"],
                "note": (
                    "Flow totals come from the transfer trace; the balance is read live "
                    "at collection time. A nonzero residual means the trace no longer "
                    "covers all on-chain activity (re-run the transfer collector), not "
                    "that the ledger is wrong."
                ),
            }
            if verbose:
                verdict = "EXACT (residual 0)" if residual == 0 else f"residual {residual} -- transfer trace is stale"
                print(f"  invariant: {verdict}")

    # ---- stats -------------------------------------------------------------
    stats = build_stats(by_owner, supply) if by_owner is not None else None

    holders_table = []
    if by_owner is not None:
        ordered = sorted(
            ((o, e) for o, e in by_owner.items() if e["raw"] > 0),
            key=lambda kv: (-kv[1]["raw"], kv[0]),
        )
        if args.max_holders:
            ordered = ordered[: args.max_holders]
        for rank, (owner, entry) in enumerate(ordered, start=1):
            row = {
                "rank": rank,
                "owner": owner,
                "raw": str(entry["raw"]),
                "ui": ui(entry["raw"], supply["decimals"]),
                "pct": pct_str(entry["raw"], supply["raw"]),
                "token_accounts": entry["accounts"],
            }
            if owner in LABELLED_OWNERS:
                row["label"] = LABELLED_OWNERS[owner]
            holders_table.append(row)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generator": "scripts/collect/holders.py",
        "collected_at": utcnow(),
        "units": {
            "decimals": supply["decimals"],
            "canonical_field_suffix": "_raw",
            "note": (
                "Every *_raw field is an INTEGER number of base units carried as a "
                "decimal string. Divide by 10**decimals exactly once, at render time. "
                "Never parse a raw value into a float before dividing, and never sum "
                "the *_ui strings. The *_ui and *_pct fields are exact decimal "
                "renderings produced by integer/Decimal arithmetic, not float division; "
                "they are a convenience, not the source of truth."
            ),
        },
        "token": {
            "mint": MINT,
            "token_program": TOKEN_2022_PROGRAM,
            "token_program_name": "Token-2022",
            "token_program_note": (
                "An SPL-classic (TokenkegQ...) scan of this mint returns an empty set "
                "and no error. Every account query here targets Token-2022."
            ),
            "decimals": supply["decimals"],
            "supply_raw": str(supply["raw"]),
            "supply_ui": ui(supply["raw"], supply["decimals"]),
            "supply_slot": supply["slot"],
            "supply_note": (
                "Read live via getTokenSupply. Supply drifts as burns land; never "
                "hardcode. Every percentage in this file uses this value as its "
                "denominator unless the block names another."
            ),
        },
        "provenance": {
            "collected_at": utcnow(),
            "script": f"scripts/collect/holders.py v{SCRIPT_VERSION}",
            # Hostnames only. RPC URLs carry API keys and must never be serialised.
            "rpc_endpoints_used": rpc.endpoints_used,
            "rpc_failovers": rpc.rotations,
            "mint": MINT,
            "token_program": TOKEN_2022_PROGRAM,
            "token_program_note": "Token-2022. An SPL-classic scan of this mint returns nothing, silently.",
            "decimals": supply["decimals"],
            "method": "getProgramAccounts" if scan else "getTokenAccountsByOwner (per-recipient fallback)",
            "scan_complete": scan is not None,
            # A scan that ran but did not reconcile is present but not
            # trustworthy; consumers should treat holder_count as approximate.
            "scan_trustworthy": bool(scan) and reconciles,
            "scan_attempts_allowed": args.scan_attempts,
            "fallback_used": scan is None,
            "scan_error": scan_error,
            "scan_endpoint": scan["endpoint"] if scan else None,
            "scan_slot": scan["slot"] if scan else None,
            "supply_slot": supply["slot"],
            "slot_skew": (scan["slot"] - supply["slot"]) if scan else None,
            "rpc_requests": rpc.request_count,
            "rpc_retries": rpc.retry_count,
            "rpc_rate_limited": rpc.throttled,
            "elapsed_seconds": round(time.time() - started, 2),
        },
        "integrity": {
            "scan_sum_raw": str(scan["total_raw"]) if scan else None,
            "supply_raw": str(supply["raw"]),
            "delta_raw": str(scan["total_raw"] - supply["raw"]) if scan else None,
            "supply_reconciles": reconciles if scan else None,
            "reconcile_note": (
                "The mint has no mint authority, so supply cannot rise and every token "
                "account of the mint summed must equal getTokenSupply exactly. An exact "
                "match proves the scan enumerated the complete holder set with a "
                "self-consistent snapshot. Any nonzero delta means the node mixed "
                "account versions across slots, not that supply moved -- the scan is "
                "retried until it closes."
            ),
            "token_accounts_returned": scan["token_accounts_returned"] if scan else None,
            "token_accounts_parsed": scan["token_accounts"] if scan else None,
            "skipped_not_token_account": scan["skipped_not_token_account"] if scan else None,
            "skipped_short_slice": scan["skipped_short_slice"] if scan else None,
        },
        "campaign_wallet": {
            "owner": CAMPAIGN_OWNER,
            "ata": CAMPAIGN_ATA,
            "balance_raw": str(ata["raw"]),
            "balance_ui": ui(ata["raw"], supply["decimals"]),
            "balance_slot": ata["slot"],
            "note": (
                "Signature history is read off the ATA, not the owner: the owner's "
                "history is polluted with unsolicited airdrops from strangers "
                "(166 sigs on the ATA vs 561 on the owner)."
            ),
        },
        "invariant": invariant,
        "holder_count": stats["holder_count"] if stats else None,
        "holder_count_available": stats is not None,
        "holder_count_unavailable_reason": None if stats else (scan_error or "scan did not run"),
        "stats": stats,
        "recipients_summary": recipient_summary or None,
        "recipients": [recipient_to_wire(r, supply["decimals"]) for r in recipient_rows],
        "holders_note": (
            "Every owner with a non-zero balance, aggregated across all of their token "
            "accounts (one person can hold several), ranked by balance. Fields: rank, "
            "owner, raw, ui, pct (of live supply), token_accounts, and label on the four "
            "known infrastructure accounts. Owners whose accounts all hold exactly zero "
            "are counted in stats.zero_balance_owner_count but omitted from this list."
        ),
        "holders_truncated": bool(args.max_holders) and stats is not None
        and args.max_holders < stats["holder_count"],
        "holders": [],
    }

    out_path = Path(args.out)
    dump(payload, holders_table, out_path)

    if verbose:
        print(f"\nwrote {out_path} ({out_path.stat().st_size / 1e6:.2f} MB)")
        if stats:
            c = stats["concentration"]
            xc = stats["concentration_excluding_infrastructure"]
            print(f"  holders          {stats['holder_count']:,}")
            print(f"  top 1            {c['top1_pct']}%")
            print(f"  top 10           {c['top10_pct']}%")
            print(f"  top 100          {c['top100_pct']}%")
            print(f"  gini             {c['gini']}")
            print(f"  top 10 ex-infra  {xc['top10_pct']}%  (of {xc['denominator_ui']} TOAD)")
            print(f"  supply reconciles: {reconciles}")
        else:
            print("  holder_count: UNAVAILABLE (scan fell back)")
        if recipient_summary:
            print(f"  recipients       {recipient_summary['recipient_count']}")
            for status, count in sorted(recipient_summary["status_counts"].items(), key=lambda kv: -kv[1]):
                print(f"    {status:<16} {count}")
            print(f"  still held       {recipient_summary['still_held_ui']} TOAD "
                  f"({recipient_summary['still_held_pct_of_airdrop']}% of airdropped)")

    if scan and not reconciles:
        delta = scan["total_raw"] - supply["raw"]
        print(
            f"\nFAILED INVARIANT: scan sum - supply = {delta:+d} raw "
            f"({delta / 10 ** supply['decimals']:+,.6f} TOAD) after {args.scan_attempts} attempts.\n"
            "The holder table is written but marked scan_trustworthy=false. Do not "
            "publish concentration from it; re-run when the endpoint settles.",
            file=sys.stderr,
        )
        if not args.allow_unreconciled:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
