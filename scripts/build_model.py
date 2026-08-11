#!/usr/bin/env python3
"""Construct data/model.json from the collector outputs under data/collection/**.

THIS IS A BUILD STEP, NOT A MUTATOR.

The previous version of this file read data/model.json and wrote it back to the
same path. That is not a pipeline: deleting data/model.json destroyed the site's
only copy of the data and nothing could regenerate it. Every figure on the site
was therefore an unfalsifiable hand-edit.

This version reads ONLY the collector artifacts:

    data/collection/onchain/transfers.json     campaign ATA transfer trace
    data/collection/onchain/holders.json       full Token-2022 holder scan
    data/collection/market/ohlcv_minute.json   1m candles, canonical PumpSwap pool
    data/collection/social/*.json              keyless-hydrated key moments
    data/collection/identity/*.json            mint/authority/deployer/proof exhibits

...plus two live RPC reads (getAccountInfo on the campaign ATA and getTokenSupply
on the mint) which act as the build's tamper check. It writes data/model.json.
Reading data/model.json from this script is a bug; there is an explicit guard.

HARD GATES (all of these exit 1 and leave the previous model.json untouched):

  1. INVARIANT   sum(inbound_raw) - sum(outbound_raw) - ATA.amount == 0 exactly,
                 in integer base units. Not "close". This single line would have
                 caught the 61 transfers the old owner-wallet collector dropped.
  2. AGREEMENT   the three independent collectors must report the same flows.
  3. FLOORS      >= 162 transfers / 148 recipients / 20,260,555 TOAD distributed.
  4. PRICE       GeckoTerminal vs pump.fun median divergence must be < 2%.
                 (A chart built off the Meteora DLMM pool is 6.6x off spot and
                 looks perfectly fine until you check it against a second venue.)
  5. STALENESS   if the live ATA balance no longer matches the collected trace,
                 new activity has landed and the ledger is incomplete. Re-run the
                 collectors; --allow-stale downgrades this to a warning.

UNITS. Money is integer base units carried as decimal strings (`*_raw`) and
converted for display exactly once, here, by integer slicing (`*_ui`). Floats
appear only in fields the frontend consumes as numbers. There is no code path in
which a raw base-unit integer can be rendered as a token amount.

NEVER-ZERO RULE. An unknown value is null so the UI can render "--". A hardcoded
0 is a lie that looks like a measurement.

SOLD REQUIRES EVIDENCE. ~21% of the airdropped TOAD has left recipient wallets.
That is "reduced", not "sold": a wallet-to-wallet transfer is not a sale. Only a
destination that is a known pool token account, or a DEX program in the same
transaction, promotes a movement to "sold". No recipient meets that bar today,
and the honest rendering of an unmeasured thing is null, not 0%.

Usage:
    python3 scripts/build_model.py                 # live chain verification
    python3 scripts/build_model.py --offline       # trust collector snapshots
    python3 scripts/build_model.py --allow-stale   # tolerate post-collection drops

Secrets: reads HELIUS_RPC from the environment. The endpoint's API key IS its
subdomain, so the bare hostname is a working credential -- it is reduced to a
registrable domain before it is written anywhere, and never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COLLECTION = ROOT / "data" / "collection"
OUT = ROOT / "data" / "model.json"
RAW = ROOT / "data" / "raw"

SCHEMA_VERSION = "3.0.0"
GENERATOR = "scripts/build_model.py"

# ---------------------------------------------------------------------------
# Verified chain constants. Every one of these is asserted against the
# collectors' own recorded values before it is used -- they are a tripwire for
# a collector that silently pointed at the wrong account, not an input.
# ---------------------------------------------------------------------------
MINT = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"
TOKEN_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAM_NAME = "Token-2022"
DECIMALS = 6
CAMPAIGN_OWNER = "FuP8dYQytaThMh9Fg2XNd1Z1eNHxMHW92kVUfWf3TnmD"
CAMPAIGN_ATA = "AuA2VRui5JNWNWF79iyaSKpW7zMQLfzFZBjd2uS3YW2H"
DEPLOYER = "5YRgrP3mjGzrzirYYN5HAQH19cTYREYwGxW6XRJQUzij"
PRICING_POOL = "Nx9dcwNs3iJxM5YAxshMHE4aYJHdDyyGMhVcmaSgfu8"
BONDING_CURVE = "9oi3zoTqd1T8T3CVuSDfSNwjeWaj6zZLdYMLWNyayaeA"

# Known-good floors, re-derived from chain. The campaign is LIVE, so true numbers only
# grow. Coming in UNDER a floor means data was lost, which is the failure mode
# that shipped last time.
FLOOR_OUTBOUND_TRANSFERS = 162
FLOOR_RECIPIENTS = 148
FLOOR_OUTBOUND_RAW = 20_260_555_000_000
FLOOR_INBOUND_RAW = 185_581_997_376_918

PRICE_DIVERGENCE_MAX_PCT = 2.0

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"

# Wallets worth naming in the ledger. Everything here is chain-verifiable; see
# data/collection/identity/. Labels are descriptive, never accusatory.
KNOWN_WALLETS: dict[str, str] = {
    CAMPAIGN_OWNER: "Campaign wallet (the airdrop source)",
    CAMPAIGN_ATA: "Campaign wallet token account",
    DEPLOYER: "Deployer (pump.fun creator; kept exactly 20,000,000)",
    PRICING_POOL: "PumpSwap TOAD pool (canonical pricing venue)",
    BONDING_CURVE: "pump.fun bonding curve",
    "64w4qRu9VGio7U1Asc6B68QDpS8L1McmSn2yyExC6Fii": "lbexplorer (wallet-proof exhibit: 70,000 TOAD, tweeted 63s later)",
}

# Editorial lore. Not on-chain and never presented as if it were: each row
# carries kind="lore" and its own citation, and the on-chain rows carry a
# signature. The site must be able to tell them apart.
LORE_ROWS = [
    {
        "date": "1988",
        "event": "El Sapo Pepe debuts on Carlitos Bala's Argentine children's television show: green toad, red shirt, yellow suspenders. Seventeen years before the internet frog.",
        "source": "https://elreinoinfantil.fandom.com/wiki/El_Sapo_Pepe",
    },
    {
        "date": "2005",
        "event": "Matt Furie publishes Boy's Club #1 and introduces Pepe the Frog. The toad was already seventeen years old.",
        "source": "https://en.wikipedia.org/wiki/Pepe_the_Frog",
    },
    {
        "date": "2013",
        "event": "El Reino Infantil / La Granja de Zenon release the El Sapo Pepe song, which becomes a Latin American childhood anthem.",
        "source": "https://elreinoinfantil.fandom.com/wiki/El_Sapo_Pepe",
    },
]

# Quotes are rendered as verbatim speech, so each one must be an exact substring
# of a tweet body this pipeline actually collected. A quote that fails the check
# is DROPPED, loudly. The repo previously shipped a 265-char "pledge" welded from
# five non-adjacent fragments of a 1,193-char post; that is what this prevents.
QUOTE_CANDIDATES = [
    {
        "fragment": "i obviously will never sell any of the tokens in my possession, so it's a matter of getting them out to the community",
        "tweet_id": "2086595256208748852",
        "who": "Mike Dudas (@mdudas)",
        "context": "origin post, 2026-08-09",
    },
    {
        "fragment": "the toad pepe is my favorite kind of pepe.",
        "tweet_id": "2086404728359915929",
        "who": "Mike Dudas (@mdudas)",
        "context": "2026-08-09",
    },
    {
        "fragment": "btw i do intend to completely transfer over toad to a cto team in which fees can be pointed towards a multi sig",
        "tweet_id": "2086927633011331359",
        "who": "sling (@slingoorio)",
        "context": "2026-08-10",
    },
]


# ---------------------------------------------------------------------------
# tiny utilities
# ---------------------------------------------------------------------------
def die(msg: str, *extra: str) -> "None":
    print("\n" + "=" * 78, file=sys.stderr)
    print("BUILD FAILED: " + msg, file=sys.stderr)
    for line in extra:
        print("  " + line, file=sys.stderr)
    print("data/model.json was NOT modified.", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    sys.exit(1)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_ui(raw: str | int, decimals: int = DECIMALS) -> str:
    """Integer base units -> exact decimal string. No float, ever."""
    n = int(raw)
    sign = "-" if n < 0 else ""
    s = str(abs(n)).rjust(decimals + 1, "0")
    return f"{sign}{s[:-decimals]}.{s[-decimals:]}" if decimals else f"{sign}{s}"


def ui_num(raw: str | int, decimals: int = DECIMALS) -> float:
    """Display float for the frontend. Derived from the exact string, not from
    a float division, so the decimal digits are the ones on chain."""
    return float(to_ui(raw, decimals))


def load(path: Path, label: str, required: bool = True) -> Any:
    if not path.exists():
        if not required:
            return None
        die(
            f"missing collector artifact: {label}",
            f"expected at {path}",
            "This build refuses to invent data. Run the collector that produces it:",
            "  set -a && . ./.env && set +a",
            "  cd ../toad-wiki && python3 scripts/collect/<collector>.py",
        )
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        die(f"{label} is not valid JSON ({e})", f"at {path}")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def redact_endpoint(url: str) -> str:
    """The Helius API key IS the subdomain -- there is no ?api-key= to strip, so
    the bare hostname is a live credential. Only public hosts survive intact."""
    host = url.split("//", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0]
    public = {"api.mainnet-beta.solana.com", "api.devnet.solana.com", "solana-api.projectserum.com"}
    if host in public:
        return host
    parts = host.split(".")
    registrable = ".".join(parts[-2:]) if len(parts) >= 2 else host
    return f"{registrable} (keyed endpoint, subdomain redacted)"


def round_sig(v: float | None, sig: int = 3) -> float | None:
    """Round to `sig` significant figures.

    The PUBLISHED number is the rounded one, not an exact number with a rounded
    label beside it. A consumer that reads the obvious field must get a figure
    the data can support, even if it never reads the caveat.
    """
    if v is None or v == 0:
        return v
    mag = len(f"{int(abs(v))}")
    q = max(0, mag - sig)
    return float(round(v, -q)) if q else float(round(v))


def approx_usd(v: float | None, sig: int = 3) -> str | None:
    """'~$281,000', never '$281,311'.

    Sampled drops land 33s and 46s into their candle, and the candle's own range
    is 0.8%-3.4% wide, so digits past the third are noise dressed as precision.
    """
    if v is None:
        return None
    if v == 0:
        return "$0"
    neg = v < 0
    a = abs(v)
    if a < 1:
        return ("-" if neg else "") + f"~${a:.4f}".rstrip("0").rstrip(".")
    mag = len(f"{int(a)}")
    q = max(0, mag - sig)
    rounded = round(a, -q) if q else round(a)
    return ("-" if neg else "") + f"~${rounded:,.0f}"


# ---------------------------------------------------------------------------
# RPC -- two calls, both of them tamper checks
# ---------------------------------------------------------------------------
class Rpc:
    """Minimal JSON-RPC client. Tries the keyed endpoint, falls back to public.

    The URL lives in a private attribute and is never logged, never returned,
    and never written to disk. `self.used` holds redacted hostnames only.
    """

    def __init__(self) -> None:
        self._urls: list[str] = []
        keyed = os.environ.get("HELIUS_RPC", "").strip()
        if keyed:
            self._urls.append(keyed)
        self._urls.append(PUBLIC_RPC)
        self.used: list[str] = []
        self.calls = 0
        self.retries = 0
        self.errors: list[str] = []

    def call(self, method: str, params: list[Any]) -> Any | None:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        for url in self._urls:
            for attempt in range(3):
                try:
                    self.calls += 1
                    req = urllib.request.Request(
                        url, data=payload, headers={"content-type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=30) as r:
                        body = json.loads(r.read())
                    if "error" in body:
                        self.errors.append(f"{redact_endpoint(url)} {method}: {body['error'].get('message')}")
                        break  # an application-level error will not fix itself
                    host = redact_endpoint(url)
                    if host not in self.used:
                        self.used.append(host)
                    return body.get("result")
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
                    code = getattr(e, "code", None)
                    self.errors.append(f"{redact_endpoint(url)} {method}: {code or e}")
                    if code in (429, 500, 502, 503, 504) or code is None:
                        self.retries += 1
                        time.sleep(min(8.0, 0.6 * (2**attempt)) + random.random() * 0.3)
                        continue
                    break
        return None


# ---------------------------------------------------------------------------
# price index
# ---------------------------------------------------------------------------
class PriceIndex:
    """Minute-bucketed close prices from the canonical PumpSwap pool.

    Hourly bucketing was measured at 7.8% median / 68.6% max error against these
    minute candles on the real drop timestamps -- a $1,794 swing on the campaign
    total. Minutes are not a nicety here.
    """

    PROBES = (0, -1, 1, -2)

    def __init__(self, candles: list[list[float]]) -> None:
        self.by_minute: dict[int, list[float]] = {}
        for c in candles:
            self.by_minute[int(c[0]) // 60] = c
        self.hits = {p: 0 for p in self.PROBES}
        self.misses = 0

    def at(self, ts: int) -> tuple[float | None, int | None, float | None]:
        """-> (close_usd, minute_offset, candle_range_pct). offset 0 == exact."""
        m = int(ts) // 60
        for off in self.PROBES:
            c = self.by_minute.get(m + off)
            if c is None:
                continue
            self.hits[off] += 1
            close = float(c[4])
            hi, lo = float(c[2]), float(c[3])
            rng = round((hi - lo) / close * 100, 4) if close else None
            return close, off, rng
        self.misses += 1
        return None, None, None


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Build data/model.json from data/collection/**.")
    ap.add_argument("--src", default=str(COLLECTION), help="collection directory")
    ap.add_argument("--out", default=str(OUT), help="output model path")
    ap.add_argument("--offline", action="store_true", help="skip live RPC verification")
    ap.add_argument("--allow-stale", action="store_true", help="warn instead of fail when the live balance has moved past the collected trace")
    ap.add_argument("--no-raw-mirror", action="store_true", help="skip mirroring captures into data/raw/")
    args = ap.parse_args()

    src = Path(args.src)
    out_path = Path(args.out)
    t0 = time.time()

    # --- guard: this script must never read its own output -----------------
    # Structural, not existence-based. An earlier version compared the output
    # against the files already present in src, which silently passed on the
    # first run and only tripped on the second -- after it had already polluted
    # the input directory with its own output. Compare the paths themselves.
    out_abs = out_path.resolve()
    src_abs = src.resolve()
    if out_abs == src_abs or src_abs in out_abs.parents:
        die("the output path is inside the collection directory",
            f"  out: {out_abs}",
            f"  src: {src_abs}",
            "build_model.py must never read its own output, and must never write into the",
            "input set. A model that is both source and product is the bug this file exists",
            "to fix: delete it and nothing can regenerate it.")
    stray = src_abs / out_abs.name
    if stray.exists():
        die(f"{stray} exists inside the collection directory",
            "That is this script's output sitting in its own input set. Delete it -- if it is",
            "ever loaded as a source, the pipeline becomes self-referential again.")

    print("build_model.py -- constructing data/model.json from collector artifacts")
    print(f"  source: {src}")

    # ---------------------------------------------------------------- load
    transfers_doc = load(src / "onchain" / "transfers.json", "onchain/transfers.json")
    holders_doc = load(src / "onchain" / "holders.json", "onchain/holders.json")
    ohlcv_doc = load(src / "market" / "ohlcv_minute.json", "market/ohlcv_minute.json")
    moments_doc = load(src / "social" / "key_moments.json", "social/key_moments.json")
    profiles_doc = load(src / "social" / "profiles.json", "social/profiles.json")
    social_manifest = load(src / "social" / "_manifest.json", "social/_manifest.json")
    timelines_doc = load(src / "social" / "timelines.json", "social/timelines.json")
    search_doc = load(src / "social" / "search_toad.json", "social/search_toad.json")
    graph_doc = load(src / "social" / "graph.json", "social/graph.json", required=False) or {}
    ident_index = load(src / "identity" / "index.json", "identity/index.json")
    ident_meta = load(src / "identity" / "metadata.json", "identity/metadata.json")
    ident_auth = load(src / "identity" / "authorities.json", "identity/authorities.json")
    ident_pump = load(src / "identity" / "pumpfun.json", "identity/pumpfun.json")
    ident_proof = load(src / "identity" / "wallet_proof.json", "identity/wallet_proof.json")
    ident_launch = load(src / "identity" / "launch_timeline.json", "identity/launch_timeline.json")
    ident_deployer = load(src / "identity" / "deployer.json", "identity/deployer.json")
    ident_copycats = load(src / "identity" / "copycats.json", "identity/copycats.json")
    ident_inv = load(src / "identity" / "campaign_invariant.json", "identity/campaign_invariant.json")

    # ------------------------------------------------- constants tripwire
    tok = transfers_doc["token"]
    if tok["mint"] != MINT:
        die(f"transfers.json traced the wrong mint: {tok['mint']}")
    if tok["token_program"] != TOKEN_PROGRAM:
        die(f"transfers.json used the wrong token program: {tok['token_program']}",
            "An SPL-classic parser reads this Token-2022 mint as EMPTY, with no error.")
    if int(tok["decimals"]) != DECIMALS:
        die(f"decimals mismatch: {tok['decimals']}")
    if transfers_doc["wallet"]["campaign_ata"] != CAMPAIGN_ATA:
        die("transfers.json paginated the wrong account",
            f"got {transfers_doc['wallet']['campaign_ata']}, expected the ATA {CAMPAIGN_ATA}",
            "The owner wallet's history is polluted with spam airdrops; querying it lost 55% of the data.")
    if holders_doc["token"]["token_program"] != TOKEN_PROGRAM:
        die("holders.json scanned the wrong token program")

    # ------------------------------------------------------- live chain
    rpc = Rpc()
    live: dict[str, Any] = {
        "attempted": not args.offline,
        "ok": False,
        "endpoints_used": [],
        "ata_balance_raw": None,
        "ata_slot": None,
        "ata_owner_program": None,
        "supply_raw": None,
        "supply_slot": None,
        "errors": [],
    }
    raw_captures: list[tuple[str, Any]] = []
    if not args.offline:
        acc = rpc.call("getAccountInfo", [CAMPAIGN_ATA, {"encoding": "jsonParsed", "commitment": "finalized"}])
        sup = rpc.call("getTokenSupply", [MINT, {"commitment": "finalized"}])
        if acc and acc.get("value"):
            info = acc["value"]["data"]["parsed"]["info"]
            live["ata_balance_raw"] = info["tokenAmount"]["amount"]
            live["ata_slot"] = acc.get("context", {}).get("slot")
            live["ata_owner_program"] = acc["value"]["owner"]
            raw_captures.append(("chain_getAccountInfo_campaign_ata.json", acc))
        if sup and sup.get("value"):
            live["supply_raw"] = sup["value"]["amount"]
            live["supply_slot"] = sup.get("context", {}).get("slot")
            raw_captures.append(("chain_getTokenSupply_mint.json", sup))
        live["ok"] = bool(live["ata_balance_raw"] and live["supply_raw"])
        live["endpoints_used"] = rpc.used
        live["errors"] = rpc.errors[-6:]
        if live["ok"] and live["ata_owner_program"] != TOKEN_PROGRAM:
            die(f"the campaign ATA is owned by {live['ata_owner_program']}, not Token-2022")
        print(f"  live chain: {'ok via ' + ', '.join(rpc.used) if live['ok'] else 'UNAVAILABLE (falling back to collector snapshots)'}")
    else:
        print("  live chain: skipped (--offline)")

    # ------------------------------------------------- GATE 1: invariant
    inv = transfers_doc["invariant"]
    inflow_raw = int(inv["inflow_raw"])
    outflow_raw = int(inv["outflow_raw"])
    snap_balance_raw = int(inv["balance_onchain_raw"])
    residual_raw = inflow_raw - outflow_raw - snap_balance_raw

    if residual_raw != 0:
        die(
            "THE INVARIANT DOES NOT CLOSE",
            "sum(inbound_raw) - sum(outbound_raw) - ATA.amount must be EXACTLY 0 base units.",
            f"  inbound   {inflow_raw:>18,}",
            f"  outbound  {outflow_raw:>18,}",
            f"  balance   {snap_balance_raw:>18,}",
            f"  residual  {residual_raw:>18,}  <-- must be 0",
            "A nonzero residual means the transfer trace is not exhaustive: transfers are",
            "missing, or the pagination stopped early. Re-run scripts/collect/transfers.py.",
        )

    # ---------------------------------------- GATE 2: source agreement
    hinv = holders_doc["invariant"]
    disagreements = []
    if int(hinv["inflow_raw"]) != inflow_raw or int(hinv["outflow_raw"]) != outflow_raw or int(hinv["balance_onchain_raw"]) != snap_balance_raw:
        disagreements.append(f"holders.json: in={hinv['inflow_raw']} out={hinv['outflow_raw']} bal={hinv['balance_onchain_raw']}")
    if int(ident_inv["inbound_raw"]) != inflow_raw or int(ident_inv["outbound_raw"]) != outflow_raw or int(ident_inv["balance_raw"]) != snap_balance_raw:
        disagreements.append(f"identity/campaign_invariant.json: in={ident_inv['inbound_raw']} out={ident_inv['outbound_raw']} bal={ident_inv['balance_raw']}")
    if disagreements:
        die("independent collectors disagree about the campaign flows",
            f"transfers.json: in={inflow_raw} out={outflow_raw} bal={snap_balance_raw}",
            *disagreements,
            "Three collectors walked this account separately. If they disagree, at least one",
            "is stale or wrong -- re-run all of them before publishing.")

    # --------------------------------------------- GATE 3: live staleness
    stale = False
    if live["ok"]:
        live_bal = int(live["ata_balance_raw"])
        if live_bal != snap_balance_raw:
            stale = True
            drift = live_bal - snap_balance_raw
            msg = [
                f"collected balance {snap_balance_raw:,} != live balance {live_bal:,} (drift {drift:+,} base units)",
                "The campaign is live. New transfers have landed since the trace was collected,",
                "so the ledger below is no longer complete. Re-run scripts/collect/transfers.py",
                "and scripts/collect/holders.py, then rebuild.",
            ]
            if args.allow_stale:
                print("  WARNING: ledger is STALE -- " + msg[0])
            else:
                die("the live chain has moved past the collected ledger", *msg,
                    "(pass --allow-stale to publish anyway; the model will be flagged stale)")

    # ------------------------------------------------------ GATE 4: floors
    totals = transfers_doc["totals"]
    out_count = int(totals["outbound_transfer_count"])
    rec_count = int(totals["recipient_count"])
    out_raw = int(totals["outbound_total_raw"])
    floor_failures = []
    if out_count < FLOOR_OUTBOUND_TRANSFERS:
        floor_failures.append(f"outbound transfers {out_count} < floor {FLOOR_OUTBOUND_TRANSFERS}")
    if rec_count < FLOOR_RECIPIENTS:
        floor_failures.append(f"recipients {rec_count} < floor {FLOOR_RECIPIENTS}")
    if out_raw < FLOOR_OUTBOUND_RAW:
        floor_failures.append(f"distributed {out_raw} < floor {FLOOR_OUTBOUND_RAW}")
    if inflow_raw < FLOOR_INBOUND_RAW:
        floor_failures.append(f"inbound {inflow_raw} < floor {FLOOR_INBOUND_RAW}")
    if floor_failures:
        die("the ledger came in UNDER a known-good floor -- data was lost", *floor_failures,
            "The campaign only grows. Fewer rows than the floor means a collector regressed.")

    # ------------------------------------------------------- GATE 5: price
    cc = ohlcv_doc.get("cross_check") or {}
    median_div = cc.get("median_pct")
    if median_div is None:
        die("ohlcv_minute.json has no cross-check against a second price oracle",
            "A single-source price series can be 6.6x off spot and look perfectly normal.")
    if float(median_div) >= PRICE_DIVERGENCE_MAX_PCT:
        die(f"price series diverges {median_div}% from the pump.fun oracle (limit {PRICE_DIVERGENCE_MAX_PCT}%)",
            f"overlap: {cc.get('overlap_candles')} buckets",
            "This is the Meteora-DLMM failure mode: a chart that is silently, entirely wrong.")
    if not cc.get("passed", False):
        die("ohlcv_minute.json reports its own cross-check as FAILED")
    if ohlcv_doc.get("pool") != PRICING_POOL:
        die(f"candles came from pool {ohlcv_doc.get('pool')}, not the canonical PumpSwap pool {PRICING_POOL}")
    gap_count = (ohlcv_doc.get("coverage") or {}).get("gap_count")
    if gap_count is None:
        die("ohlcv_minute.json does not report gap_count")

    print(f"  gates passed: invariant residual 0 | 3 sources agree | floors met | price divergence {median_div}%")

    # ---------------------------------------------------------- supply
    if live["ok"]:
        supply_raw = int(live["supply_raw"])
        supply_source = "getTokenSupply (live, at build time)"
        supply_slot = live["supply_slot"]
    else:
        supply_raw = int(holders_doc["token"]["supply_raw"])
        supply_source = "getTokenSupply, captured by scripts/collect/holders.py (live RPC unavailable at build time)"
        supply_slot = holders_doc["token"].get("supply_slot")
    nominal_raw = 1_000_000_000 * 10**DECIMALS
    burned_raw = nominal_raw - supply_raw

    # ------------------------------------------------------ price index
    px = PriceIndex(ohlcv_doc["candles"])

    # ------------------------------------------ transfers priced in USD
    transfers_out = []
    usd_values: list[float] = []
    total_usd = 0.0
    unpriced = 0
    for t in transfers_doc["transfers"]:
        amt_raw = int(t["amount_raw"])
        amt_ui = ui_num(amt_raw)
        price, offset, rng = px.at(t["ts"])
        usd = round(amt_ui * price, 2) if price is not None else None
        if usd is None:
            unpriced += 1
        else:
            total_usd += usd
            usd_values.append(usd)
        transfers_out.append({
            "sig": t["sig"],
            "ts": t["ts"],
            "ts_iso": t["ts_iso"],
            "slot": t["slot"],
            "to": t["to_owner"],
            "to_token_account": t["to_token_account"],
            "amount_raw": t["amount_raw"],
            "amount_ui": t["amount_ui"],
            "amount": amt_ui,
            "price_usd": price,
            "price_minute_offset": offset,
            "price_candle_range_pct": rng,
            "usd": usd,
            "usd_display": approx_usd(usd),
            "is_sale": bool(t.get("is_sale")),
            "attribution": t.get("attribution"),
        })

    # ---------------------------------------------- pricing uncertainty
    # Do not merely assert that the USD total should be rounded -- measure how
    # wrong it can be. A drop is priced at its minute's CLOSE, but it actually
    # filled somewhere inside that minute, so the candle's own high-low range is
    # an honest error bar. Publishing "$280,083.29" against a +/-2.7% band is a
    # precision claim the data cannot support.
    ranges = [t["price_candle_range_pct"] for t in transfers_out if t["price_candle_range_pct"] is not None]
    median_range = round(statistics.median(ranges), 4) if ranges else None
    hourly_total = 0.0
    hourly_index: dict[int, list[float]] = {}
    for c in ohlcv_doc["candles"]:
        hourly_index.setdefault(int(c[0]) // 3600, c)
    for t in transfers_out:
        c = hourly_index.get(t["ts"] // 3600) or hourly_index.get(t["ts"] // 3600 - 1)
        if c is not None:
            hourly_total += ui_num(t["amount_raw"]) * float(c[4])
    usd_precision = {
        "method": "amount x close of the 1-minute candle containing the transfer; probe order 0, -1, +1, -2 minutes",
        "priced_exact_minute": px.hits[0],
        "priced_via_probe": sum(px.hits[o] for o in (-1, 1, -2)),
        "unpriced": unpriced,
        "coverage_pct": round((len(transfers_out) - unpriced) / len(transfers_out) * 100, 2) if transfers_out else None,
        "median_candle_range_pct": median_range,
        "max_candle_range_pct": round(max(ranges), 4) if ranges else None,
        "implied_uncertainty_usd": round(total_usd * (median_range or 0) / 100, 2),
        "hourly_bucketed_total_usd": round(hourly_total, 2),
        "hourly_vs_minute_delta_usd": round(hourly_total - total_usd, 2),
        "why_rounded": (
            "A drop fills at some instant inside its minute, not at the close. The median "
            f"candle spans {median_range}% high-to-low, so the total carries roughly "
            f"+/-${round(total_usd * (median_range or 0) / 100):,.0f} of irreducible uncertainty. "
            "Render the rounded figure. Every digit past the third is invented precision."
        ),
        "row_level_policy": (
            "Headline totals (stats.total_usd_at_drop, airdrop_daily[].usd) are published "
            "rounded to 3 significant figures. Per-transfer and per-recipient usd fields keep "
            "cent resolution so they still reconcile to *_exact. A rounded total therefore will "
            "not equal the sum of the rows -- that is the intended behaviour, not a bug."
        ),
        "why_minute_not_hourly": (
            f"Bucketing the same transfers hourly moves the total by "
            f"${abs(hourly_total - total_usd):,.0f} ({abs(hourly_total - total_usd) / total_usd * 100:.1f}%). "
            "Hourly candles are not a cheaper approximation here; they are a different answer."
        ),
    }

    # --------------------------------------------------------- inbound
    inbound_out = []
    for t in transfers_doc["inbound"]:
        price, offset, _ = px.at(t["ts"])
        amt_ui = ui_num(t["amount_raw"])
        inbound_out.append({
            "sig": t["sig"],
            "ts": t["ts"],
            "ts_iso": t["ts_iso"],
            "from": t["from_owner"],
            "amount_raw": t["amount_raw"],
            "amount_ui": t["amount_ui"],
            "amount": amt_ui,
            "usd": round(amt_ui * price, 2) if price is not None else None,
            "usd_display": approx_usd(round(amt_ui * price, 2)) if price is not None else None,
            "source_is_known_pool": t.get("source_is_known_pool"),
            "source_pool_label": t.get("source_pool_label"),
            "label": (
                "market buy" if t.get("source_is_known_pool")
                else ("deployer hand-off" if t["from_owner"] == DEPLOYER else "transfer in")
            ),
        })

    # ------------------------------------------------------ recipients
    holder_rows = {r["wallet"]: r for r in holders_doc["recipients"]}
    holders_by_owner = {h["owner"]: h for h in holders_doc["holders"]}
    STATUS_MAP = {
        "holding_full": "holding",
        "holding_partial": "partial",
        "zero_balance": "zero_balance",
        "account_closed": "account_closed",
    }
    usd_by_wallet: dict[str, float | None] = {}
    recipients_out = []
    for r in transfers_doc["recipients"]:
        wallet = r["wallet"]
        txs = []
        usd_total = 0.0
        usd_known = False
        for tx in r["txs"]:
            price, offset, rng = px.at(tx["ts"])
            amt_ui = ui_num(tx["amount_raw"])
            usd = round(amt_ui * price, 2) if price is not None else None
            if usd is not None:
                usd_total += usd
                usd_known = True
            txs.append({
                "sig": tx["sig"],
                "ts": tx["ts"],
                "ts_iso": iso(tx["ts"]),
                "amount_raw": tx["amount_raw"],
                "amount": amt_ui,
                "price_usd": price,
                "price_minute_offset": offset,
                "usd": usd,
                "usd_display": approx_usd(usd),
                "is_sale": bool(tx.get("is_sale")),
            })

        h = holder_rows.get(wallet)
        received_raw = int(r["total_raw"])
        if h is None:
            # Not in the holder snapshot at all: unknown, not zero.
            status = "unknown"
            balance_raw = None
            balance_now = None
            held_pct = None
            moved_out_raw = None
            disposition = "unknown"
        else:
            status = STATUS_MAP.get(h["status"], "unknown")
            balance_raw = int(h["balance_raw"])
            balance_now = ui_num(balance_raw)
            held_pct = round(balance_raw / received_raw, 6) if received_raw else None
            moved_out_raw = int(h["moved_out_raw"])
            if moved_out_raw <= 0:
                disposition = "holding"
            else:
                # "sold" demands evidence: a pool token account as destination or
                # a DEX program in the transaction. We do not trace recipients'
                # outbound transactions, so we cannot see it -- and an unmeasured
                # sale is null, not a claim.
                disposition = "reduced"

        usd_at_drop = round(usd_total, 2) if usd_known else None
        usd_by_wallet[wallet] = usd_at_drop
        recipients_out.append({
            "rank": r["rank"],
            "wallet": wallet,
            "known_label": KNOWN_WALLETS.get(wallet) or (holders_by_owner.get(wallet, {}) or {}).get("label"),
            "identity": None,  # per-recipient identity resolution is not collected yet
            "total_raw": r["total_raw"],
            "total_ui": r["total_ui"],
            "total": ui_num(received_raw),
            "usd_at_drop": usd_at_drop,
            "usd_at_drop_display": approx_usd(usd_at_drop),
            "tx_count": r["tx_count"],
            "first_ts": r["first_ts"],
            "last_ts": r["last_ts"],
            "balance_raw": None if balance_raw is None else str(balance_raw),
            "balance_now": balance_now,
            "held_pct": held_pct,
            "moved_out_raw": None if moved_out_raw is None else str(moved_out_raw),
            "moved_out": None if moved_out_raw is None else ui_num(moved_out_raw),
            "status": status,
            "disposition": disposition,
            "sold_confirmed": None,          # requires pool/DEX evidence; not collected
            "sale_evidence": [],
            "token_accounts": r.get("token_accounts"),
            "balance_checked_via": None if h is None else h.get("checked_via"),
            "txs": txs,
        })

    status_counts: dict[str, int] = {}
    for r in recipients_out:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    n_rec = len(recipients_out)

    # ------------------------------------------------------ daily buckets
    daily: dict[str, dict[str, Any]] = {}
    for t in transfers_out:
        day = datetime.fromtimestamp(t["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        b = daily.setdefault(day, {"date": day, "count": 0, "amount_raw": 0, "usd": 0.0, "unpriced": 0})
        b["count"] += 1
        b["amount_raw"] += int(t["amount_raw"])
        if t["usd"] is None:
            b["unpriced"] += 1
        else:
            b["usd"] += t["usd"]
    airdrop_daily = []
    for day in sorted(daily):
        b = daily[day]
        airdrop_daily.append({
            "date": day,
            "count": b["count"],
            "amount_raw": str(b["amount_raw"]),
            "amount": ui_num(b["amount_raw"]),
            "usd": round_sig(b["usd"]),
            "usd_exact": round(b["usd"], 2),
            "usd_display": approx_usd(round(b["usd"], 2)),
            "unpriced_transfers": b["unpriced"] or None,
        })

    # -------------------------------------------------------- key moments
    moments = [m for m in moments_doc["moments"] if m.get("status") == "ok"]
    moments.sort(key=lambda m: m.get("created_at") or "")

    def moment_label(m: dict[str, Any]) -> str:
        """The collector labels two moments 'referenced by model.json', which is a
        note-to-self about provenance, not a description of the post. Rendering it
        on the site would be gibberish, so fall back to the post's own opening."""
        lbl = (m.get("label") or "").strip()
        if not lbl or "model.json" in lbl:
            text = " ".join((m.get("text") or "").split())
            return (text[:70].rstrip() + "...") if len(text) > 70 else text
        return lbl

    profiles = {p["handle"]: p for p in profiles_doc.get("profiles", [])}
    tweets = []
    for m in moments:
        a = m["author"]
        prof = profiles.get(a["handle"], {})
        media = m.get("media") or []
        tweets.append({
            "id": m["id"],
            "url": m["url"],
            "author": {
                "name": a.get("name"),
                "handle": a.get("handle"),
                "avatar": a.get("avatar_url") or prof.get("avatar_url"),
                "followers": a.get("followers_at_capture"),
            },
            "date": m["created_at"],
            "label": moment_label(m),
            "label_source": "collector" if (m.get("label") and "model.json" not in m["label"]) else "first 70 chars of the post",
            "text": m["text"],
            "text_len": m.get("text_len"),
            "text_sha256": m.get("text_sha256"),
            "text_integrity": m.get("text_integrity"),
            "is_note_tweet": m.get("is_note_tweet"),
            "likes": (m.get("metrics") or {}).get("likes"),
            "replies": (m.get("metrics") or {}).get("replies"),
            "retweets": (m.get("metrics") or {}).get("retweets"),
            "views": (m.get("metrics") or {}).get("views"),
            "metrics_captured_at": (m.get("metrics") or {}).get("captured_at"),
            "photo": (media[0].get("url") if media and isinstance(media[0], dict) else None),
            "quoted": None,
            "mentions_mint": m.get("mentions_mint"),
        })
    tweets_by_id = {t["id"]: t for t in tweets}

    # ------------------------------------------- thread-order integrity
    # Curated labels number some posts "(1/3)", "(2/3)". Nothing verified that
    # the numbering matches reality -- conversation_id is null on every hydrated
    # moment, so the "thread" is an editorial assertion. If the numbering
    # disagrees with the timestamps, say so instead of rendering a false order.
    thread_warnings: list[str] = []
    threads: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for t in tweets:
        m = re.search(r"\((\d+)\s*/\s*(\d+)\)", t.get("label") or "")
        if m:
            threads.setdefault(m.group(2), []).append((int(m.group(1)), t))
    for total, members in threads.items():
        members.sort(key=lambda x: x[0])
        chrono = sorted(members, key=lambda x: x[1]["date"])
        if [x[0] for x in members] != [x[0] for x in chrono]:
            order = ", ".join(f"({n}/{total}) @{t['author']['handle']} {t['date'][11:16]}Z" for n, t in chrono)
            thread_warnings.append(
                f"The \"{total}-part thread\" numbering is the curator's, not the platform's: "
                f"no conversation_id was returned for any of these posts, and in chronological "
                f"order they read {order}. Render them by timestamp, and do not claim they are one reply chain."
            )

    # ------------------------------------------------------------ quotes
    quotes = []
    dropped_quotes = []
    for q in QUOTE_CANDIDATES:
        t = tweets_by_id.get(q["tweet_id"])
        if t is None:
            dropped_quotes.append(f"{q['tweet_id']}: tweet not in the collected set")
            continue
        if q["fragment"] not in t["text"]:
            dropped_quotes.append(f"{q['tweet_id']}: fragment is NOT a verbatim substring of the collected text")
            continue
        quotes.append({
            "text": q["fragment"],
            "who": q["who"],
            "context": q["context"],
            "source": t["url"],
            "tweet_id": t["id"],
            "verbatim": True,
            "char_offset": t["text"].index(q["fragment"]),
            "of_full_text_len": t["text_len"],
            "verification": "exact substring of the hydrated tweet body (fxtwitter, cross-checked against vxtwitter)",
        })
    for d in dropped_quotes:
        print(f"  DROPPED QUOTE -- {d}")

    # ---------------------------------------------------------- timeline
    timeline = [{**row, "kind": "lore", "verified": False, "onchain": False} for row in LORE_ROWS]
    # The identity collector could not verify the deployer's launch tweet because
    # tweet SEARCH needs an authorised X credential. The social collector hydrated
    # that exact tweet id from its curated list. Joining the two closes the row.
    launch_tweet = tweets_by_id.get("2086106068636127279")
    for row in ident_launch["rows"]:
        ev = {
            "date": (row.get("time") or "")[:10] or None,
            "ts_iso": row.get("time"),
            "event": row["title"],
            "kind": "onchain" if row.get("sig") else "attested",
            "verified": bool(row.get("verified")),
            "sig": row.get("sig"),
            "source": row.get("explorer") or row.get("verification_source"),
            "note": row.get("note"),
            "caveat": row.get("caveat"),
        }
        if row["key"] == "slingoor_tweet" and launch_tweet is not None:
            ev.update({
                "date": launch_tweet["date"][:10],
                "ts_iso": launch_tweet["date"],
                "kind": "social",
                "verified": True,
                "source": launch_tweet["url"],
                "tweet_id": launch_tweet["id"],
                "note": "Tweet id was missing from the identity collection (X search is unauthorised); "
                        "recovered from the social collector's keyless hydration of the curated moment list.",
                "caveat": None,
            })
        if row["key"] == "mint_created":
            ev["detail"] = {
                "deployer_bought": row.get("deployer_bought"),
                "pct_of_initial_supply": row.get("pct_of_initial_supply"),
                "sol_credited_to_curve": row.get("sol_credited_to_curve"),
            }
        timeline.append(ev)
    for t in tweets:
        if t["id"] in {"2086921237855719848", "2086927633011331359", "2086595256208748852"}:
            timeline.append({
                "date": t["date"][:10],
                "ts_iso": t["date"],
                "event": f"@{t['author']['handle']}: {t['label']}",
                "kind": "social",
                "verified": True,
                "source": t["url"],
                "tweet_id": t["id"],
            })
    ath_mc = ((ident_pump.get("coin") or {}).get("ath_market_cap") or {})
    if ath_mc.get("value"):
        timeline.append({
            "date": ((ident_pump["coin"].get("ath_market_cap_at_iso")) or "")[:10],
            "ts_iso": ident_pump["coin"].get("ath_market_cap_at_iso"),
            "event": f"Peak market cap ${ath_mc['value']:,.0f} (pump.fun ath_market_cap). "
                     f"The '~$50M peak' repeated elsewhere is not supported by any source we can read.",
            "kind": "attested",
            "verified": True,
            "source": f"https://pump.fun/coin/{MINT}",
        })
    # Rows the collectors could not date (the X credential is dead, so account
    # creation and un-id'd tweets have no timestamp) go last, and say so in the
    # date column. A blank date cell reads as a rendering bug; "undated" reads as
    # the honest state it is.
    for e in timeline:
        if not e.get("ts_iso") and not e.get("date"):
            e["date"] = "undated"
            e["undated"] = True
    timeline.sort(key=lambda e: (
        1 if e.get("undated") else 0,
        e.get("ts_iso") or ((e.get("date") or "") + "T00:00:00Z"),
    ))

    # ----------------------------------------------------------- holders
    top_holders = []
    for h in holders_doc["holders"][:100]:
        top_holders.append({
            "rank": h["rank"],
            "owner": h["owner"],
            "raw": h["raw"],
            "amount": ui_num(h["raw"]),
            "pct_of_supply": float(h["pct"]),
            "label": h.get("label") or KNOWN_WALLETS.get(h["owner"]),
            "token_accounts": h.get("token_accounts"),
        })

    conc = holders_doc["stats"]["concentration"]
    conc_ex = holders_doc["stats"]["concentration_excluding_infrastructure"]

    # --------------------------------------------------- reconciliation
    rs = holders_doc["recipients_summary"]
    reconciliation = {
        "headline": "Every base unit that entered the campaign wallet is accounted for.",
        "formula": "sum(inbound) - sum(outbound) - getAccountInfo(ATA).amount == 0",
        "received_raw": str(inflow_raw),
        "received": ui_num(inflow_raw),
        "received_tx_count": int(totals["inbound_transfer_count"]),
        "distributed_raw": str(outflow_raw),
        "distributed": ui_num(outflow_raw),
        "distributed_transfer_count": out_count,
        "distributed_recipient_count": rec_count,
        "held_raw": str(snap_balance_raw),
        "held": ui_num(snap_balance_raw),
        "held_source": "getAccountInfo(ATA).amount" + ("" if live["ok"] else " (collector snapshot; live RPC unavailable at build time)"),
        "residual_raw": str(residual_raw),
        "residual": ui_num(residual_raw),
        "holds": residual_raw == 0,
        "checked_at_slot": live["ata_slot"] or inv.get("checked_at_slot"),
        "verified_live_at_build": bool(live["ok"]),
        "stale": stale,
        "sells_detected": int(transfers_doc["classification"]["sale_count"]),
        "sells_detected_raw": transfers_doc["classification"]["sale_amount_raw"],
        "sell_detection_method": transfers_doc["classification"]["method"],
        "sell_detection_note": (
            "0 of the campaign wallet's outbound transfers went to a pool token account or "
            f"invoked a DEX program at any CPI depth. All {len(recipients_out)} destination owners are on the "
            "ed25519 curve, and every Solana pool authority is a PDA -- which is by "
            "construction OFF the curve -- so none of them can be a liquidity pool."
        ),
        "pct_of_supply_distributed": float(totals["pct_of_supply_distributed"]),
        "recipient_side": {
            "still_held_raw": rs["still_held_raw"],
            "still_held": ui_num(rs["still_held_raw"]),
            "still_held_pct_of_airdrop": float(rs["still_held_pct_of_airdrop"]),
            "moved_out_raw": rs["moved_out_raw"],
            "moved_out": ui_num(rs["moved_out_raw"]),
            "moved_out_pct_of_airdrop": round(100 - float(rs["still_held_pct_of_airdrop"]), 6),
            "sold_raw": None,
            "sold_pct_of_airdrop": None,
            "status_counts": rs["status_counts"],
            "account_closed_raw": rs["account_closed_raw"],
            "wording": (
                "moved_out means the tokens LEFT the wallet. That covers selling, sending to "
                "another wallet, and depositing to an exchange. It is not proof of a sale, and "
                "the site must not call it one -- sold is null because it was never measured."
            ),
        },
        "invariant_is_the_test": (
            "This residual is also the pipeline's unit test. The previous collector queried the "
            "owner wallet with a 150-signature cap and reported 101 transfers / 9,033,500 TOAD. "
            "It was missing 61 transfers and 11,227,055 TOAD -- and the residual would have been "
            "nonzero on the first line of this build."
        ),
    }

    # ------------------------------------------------------------ market
    st = ohlcv_doc["stats"]
    launch = ohlcv_doc["launch"]
    cov = ohlcv_doc["coverage"]
    current_price = st["current"]["price_usd"]
    supply_ui_dec = Decimal(to_ui(supply_raw))
    mcap = float(supply_ui_dec * Decimal(str(current_price)))
    market = {
        "pool": PRICING_POOL,
        "pool_kind": ohlcv_doc.get("pool_kind"),
        "interval": "1m",
        "source": ohlcv_doc.get("source"),
        "launch": {
            "price_usd": launch["price_usd"],
            "ts": launch["ts"],
            "iso": launch["iso"],
            "source": launch["source"],
            "note": launch.get("note"),
        },
        "ath": st["ath"],
        "atl": st["atl"],
        "current": {**st["current"], "captured_at": ohlcv_doc["collected_at"]},
        "from_launch_x": st["from_launch_x"],
        "ath_from_launch_x": st["ath_from_launch_x"],
        "drawdown_from_ath_pct": st["drawdown_from_ath_pct"],
        "total_volume_usd": st["total_volume_usd"],
        "market_cap_usd": round(mcap, 2),
        "market_cap_basis": "live circulating supply x last candle close; NOT a nominal 1B supply "
                            "(1B would understate the top holder at 16.53% instead of 17.21%)",
        "pumpfun_ath_market_cap_usd": ath_mc.get("value"),
        "pumpfun_ath_market_cap_at": (ident_pump.get("coin") or {}).get("ath_market_cap_at_iso"),
        "candle_count": cov["candle_count"],
        "coverage": cov,
        "cross_check": cc,
        "price_lookup": ohlcv_doc.get("price_lookup"),
        "drop_pricing": usd_precision,
    }

    # ------------------------------------------------------------- stats
    median_usd = round(statistics.median(usd_values), 2) if usd_values else None
    stats = {
        # canonical
        "recipient_count": rec_count,
        "outbound_transfer_count": out_count,
        "distributed_raw": str(out_raw),
        "distributed": ui_num(out_raw),
        # legacy names the current frontend reads -- same values, display units
        "recipients": rec_count,
        "transfers": out_count,
        "total_amount": ui_num(out_raw),
        # The published total is ROUNDED. Any consumer that reads the obvious
        # field gets a figure the data supports; the exact sum is kept beside it
        # for reconciliation and is explicitly not for display.
        "total_usd_at_drop": round_sig(total_usd),
        "total_usd_at_drop_display": approx_usd(round(total_usd, 2)),
        "total_usd_at_drop_exact": round(total_usd, 2),
        "total_usd_at_drop_exact_note": "Reconciliation value. Do not render: it claims cent precision on a +/-2.7% estimate.",
        "total_usd_at_drop_precision": usd_precision,
        "median_drop_usd": round_sig(median_usd),
        "largest_drop_usd": round_sig(max(usd_values)) if usd_values else None,
        "smallest_drop_usd": round_sig(min(usd_values)) if usd_values else None,
        "largest_drop_amount": max((ui_num(t["amount_raw"]) for t in transfers_out), default=None),
        "smallest_drop_amount": min((ui_num(t["amount_raw"]) for t in transfers_out), default=None),
        "unpriced_transfers": unpriced,
        "first_drop_ts": totals["first_outbound_ts"],
        "last_drop_ts": totals["last_outbound_ts"],
        "holders": holders_doc["holder_count"],
        "holding_pct": round(status_counts.get("holding", 0) / n_rec, 4) if n_rec else None,
        "partial_pct": round(status_counts.get("partial", 0) / n_rec, 4) if n_rec else None,
        "zero_balance_pct": round(status_counts.get("zero_balance", 0) / n_rec, 4) if n_rec else None,
        "account_closed_pct": round(status_counts.get("account_closed", 0) / n_rec, 4) if n_rec else None,
        "still_holding_something_pct": round(
            (status_counts.get("holding", 0) + status_counts.get("partial", 0)) / n_rec, 4
        ) if n_rec else None,
        "sold_pct": None,  # never measured -- see reconciliation.recipient_side.wording
        "tokens_still_held_pct_of_airdrop": float(rs["still_held_pct_of_airdrop"]),
        "recipients_with_identity": sum(1 for r in recipients_out if r["identity"]),
        "status_counts": status_counts,
        "pct_of_supply_distributed": float(totals["pct_of_supply_distributed"]),
    }

    # -------------------------------------------------------- provenance
    now = utcnow()
    # Whether X enumeration actually ran. This was previously assumed to have
    # failed, because it had failed on the build where this block was written --
    # which crashed the moment the credential started working, and would have
    # published "not collected: X search" alongside 641 collected posts.
    # Derive it from the artifacts instead of hardcoding either outcome.
    _tl = (timelines_doc.get("timelines") or {})
    _x_ok = search_doc.get("status") == "ok"
    _tl_ok = {h: t for h, t in _tl.items() if t.get("status") in ("ok", "partial")}

    if _x_ok or _tl_ok:
        _x_collected = [{
            "what": "X timelines, $TOAD full-archive search, viral board",
            "why": (
                f"Enumerated at capture time: "
                f"{len(search_doc.get('tweets') or []):,} unique $TOAD posts over "
                f"{(search_doc.get('window') or {}).get('start','?')} -> "
                f"{(search_doc.get('window') or {}).get('end','?')}, plus timelines for "
                + ", ".join(f"@{h} ({t.get('total_tweets')} posts, {t.get('toad_relevant_count')} $TOAD-relevant)"
                            for h, t in _tl_ok.items())
                + ". Search reflects what the API returned in that window, not a guarantee "
                  "of every post ever made."
            ),
        }]
        _x_not_collected = []
    else:
        _x_collected = []
        _reason = search_doc.get("reason") or (
            (_tl.get("mdudas") or {}).get("reason") or "x_api_unavailable"
        )
        _x_not_collected = [{
            "what": "X timelines, $TOAD search, per-day post counts, viral board",
            "why": (
                "Enumeration requires an authorised X credential and the configured token "
                f"was rejected ({social_manifest.get('x_api', {}).get('status')}). "
                "Empty means unsearched, NOT 'no activity found'."
            ),
            "refused": {
                "timelines": (_tl.get("mdudas") or {}).get("reason"),
                "search": search_doc.get("reason"),
            },
        }]

    # impression_count is only absent when the paid API is; keyless `views` covers
    # the rest. Only claim the gap when it is real.
    _impressions_note = [] if _x_ok else [{
        "what": "Tweet impression counts from the paid API",
        "why": "The keyless route supplies `views` (which is why the numbers above exist at all), but not the API's impression_count field.",
    }]

    # ---- conversation board -------------------------------------------------
    # The raw graph carries every post the three principals made in the window,
    # most of which has nothing to do with $TOAD. The board is the $TOAD-relevant
    # subgraph: curated key moments, posts flagged TOAD-relevant, and anything
    # directly joined to one of those by a reply or quote. Anything else is noise
    # and would make the board look busy while saying less.
    _g_nodes = {n["id"]: dict(n) for n in (graph_doc.get("nodes") or [])}
    _g_edges = list(graph_doc.get("edges") or [])

    # join toad_relevance from the timelines (the graph itself does not carry it)
    _relevant_ids = set()
    for _h, _t in (timelines_doc.get("timelines") or {}).items():
        for _r in _t.get("tweets") or []:
            if _r.get("toad_relevant"):
                _relevant_ids.add(_r.get("id"))
    for _n in _g_nodes.values():
        _n["toad_relevant"] = _n["id"] in _relevant_ids

    _seed = {i for i, n in _g_nodes.items() if n["is_key_moment"] or n["toad_relevant"]}
    # one hop out, so a reply to a $TOAD post keeps its parent for context
    _keep = set(_seed)
    for _e in _g_edges:
        if _e["from"] in _seed or _e["to"] in _seed:
            _keep.add(_e["from"]); _keep.add(_e["to"])

    _board_nodes = []
    for _i in _keep:
        _n = _g_nodes.get(_i)
        if not _n:
            # referenced but never fetched -- keep it as an explicit unknown so the
            # renderer can show an open endpoint instead of silently dropping an edge
            _board_nodes.append({"id": _i, "author_handle": None, "author_id": None,
                                 "created_at": None, "text": None, "metrics": None,
                                 "sources": [], "is_key_moment": False, "label": None,
                                 "resolved": False, "toad_relevant": False})
            continue
        _board_nodes.append(_n)
    _board_nodes.sort(key=lambda n: n.get("created_at") or "", reverse=True)
    _board_edges = [e for e in _g_edges if e["from"] in _keep and e["to"] in _keep]

    from collections import Counter as _C
    _board = {
        "nodes": _board_nodes,
        "edges": _board_edges,
        "stats": {
            "node_count": len(_board_nodes),
            "resolved_nodes": sum(1 for n in _board_nodes if n.get("resolved")),
            "edge_count": len(_board_edges),
            "by_kind": dict(_C(e["kind"] for e in _board_edges)),
            "key_moments": sum(1 for n in _board_nodes if n.get("is_key_moment")),
            "seeded_from": len(_seed),
            "graph_total_nodes": len(_g_nodes),
        },
        "method": (
            "Seeded from curated key moments plus posts matching $TOAD, then expanded one "
            "hop along reply/quote edges so each post keeps the post it answers. Edges are "
            "X's own referenced_tweets relationships, not inferred from text."
        ),
        "caveat": (
            "A node with resolved=false was referenced by a post we hold but was never "
            "fetched: we know it exists, not what it said. Render it as an open endpoint."
        ),
        "generated_at": graph_doc.get("generated_at"),
    }

    # ---- campaign archive: the timelines completed, so this is publishable ----
    _archive = []
    for _h, _t in _tl_ok.items():
        for _r in _t.get("tweets") or []:
            if not _r.get("toad_relevant"):
                continue
            _m = _r.get("metrics") or {}
            _txt = _r.get("text") or ""
            _archive.append({
                "id": _r.get("id"),
                "handle": _h,
                "date": _r.get("created_at"),
                "text": _txt,
                "likes": _m.get("likes"),
                "rts": _m.get("retweets"),
                "replies": _m.get("replies"),
                "views": _m.get("views"),
                "is_reply": _txt.lstrip().startswith("@"),
                "reply_to": None,
                "photo": None,
                "quoted_text": None,
                "captured_at": _m.get("captured_at"),
            })
    _archive.sort(key=lambda r: r["date"] or "", reverse=True)

    # ---- viral board: withheld on purpose ----
    # The search died on HTTP 402 partway through 2026-08-08 and returned nothing
    # for 08-09/10/11 -- the three highest-volume days of the campaign. A "most
    # viral posts" ranking over the surviving days would be a real ranking of a
    # biased sample, which is worse than no ranking: it looks authoritative.
    # Publish it only when every day in the window is complete.
    _days_err = search_doc.get("days_errored") or []
    _viral_ok = _x_ok and not _days_err and not search_doc.get("truncation_suspected")
    _viral = (search_doc.get("top_by_views") or []) if _viral_ok else []
    _viral_status = {
        "status": "ok" if _viral_ok else ("partial" if (search_doc.get("tweets") or []) else "refused"),
        "reason": search_doc.get("reason"),
        "days_errored": _days_err,
        "days_complete": search_doc.get("days_complete") or [],
        "collected_posts": len(search_doc.get("tweets") or []),
        "note": (
            "Withheld. Collection stopped on HTTP 402 (X credits depleted): "
            f"{', '.join(_days_err)} returned no results, and those were the "
            "highest-volume days. A leaderboard over the remaining days would rank a "
            "biased sample. Empty means unsearched, NOT that nothing was posted."
        ) if not _viral_ok else "Complete over the stated window.",
    }

    provenance = {
        "generated_at": now,
        "generator": GENERATOR,
        "schema_version": SCHEMA_VERSION,
        "build_host_rpc_endpoints": live["endpoints_used"],
        # footer.tsx reads chain/price/social
        "chain": {
            "collected_at": transfers_doc["collected_at"],
            "slot": live["ata_slot"] or transfers_doc["provenance"]["rpc_slot"],
            "sigs_scanned": transfers_doc["provenance"]["sigs_scanned"],
            "source": "Solana mainnet JSON-RPC",
            "pages_fetched": transfers_doc["provenance"]["pages_fetched"],
            "truncated": transfers_doc["provenance"]["truncated"],
            "commitment": transfers_doc["provenance"].get("commitment"),
            "window_start": transfers_doc["provenance"]["window_start_iso"],
            "window_end": transfers_doc["provenance"]["window_end_iso"],
            "verified_live_at_build": bool(live["ok"]),
        },
        "holders": {
            "collected_at": holders_doc["collected_at"],
            "slot": holders_doc["provenance"]["scan_slot"],
            "method": holders_doc["provenance"]["method"],
            "token_accounts_scanned": holders_doc["integrity"]["token_accounts_returned"],
            "scan_reconciles_to_supply": holders_doc["integrity"]["supply_reconciles"],
            "source": "getProgramAccounts (Token-2022), summed and gated against getTokenSupply",
        },
        "price": {
            "collected_at": ohlcv_doc["collected_at"],
            "source": "GeckoTerminal 1m (PumpSwap pool), cross-checked vs pump.fun 5m",
            "candles": cov["candle_count"],
            "gap_count": gap_count,
            "median_divergence_pct": median_div,
        },
        "social": {
            "collected_at": moments_doc["generated_at"],
            "source": "keyless hydration: fxtwitter + vxtwitter + syndication CDN",
            "key_moments_resolved": moments_doc["resolved"],
            "x_api_status": social_manifest["x_api"]["status"],
            "x_api_detail": social_manifest["x_api"]["detail"],
        },
        "identity": {
            "collected_at": ident_index["collected_at"],
            "claims_verified": ident_index["verified_count"],
            "claims_total": ident_index["total_claims"],
            "source": "Solana mainnet JSON-RPC + IPFS + pump.fun API",
        },
        "supply": {
            "raw": str(supply_raw),
            "ui": to_ui(supply_raw),
            "source": supply_source,
            "slot": supply_slot,
            "read_at": now if live["ok"] else holders_doc["collected_at"],
        },
        "sources": [
            {"file": "data/collection/onchain/transfers.json", "generator": transfers_doc["generator"], "collected_at": transfers_doc["collected_at"], "schema_version": transfers_doc["schema_version"]},
            {"file": "data/collection/onchain/holders.json", "generator": holders_doc["generator"], "collected_at": holders_doc["collected_at"], "schema_version": holders_doc["schema_version"]},
            {"file": "data/collection/market/ohlcv_minute.json", "generator": "scripts/collect/ohlcv.py", "collected_at": ohlcv_doc["collected_at"], "schema_version": None},
            {"file": "data/collection/social/key_moments.json", "generator": "scripts/collect/social.py", "collected_at": moments_doc["generated_at"], "schema_version": None},
            {"file": "data/collection/identity/index.json", "generator": "scripts/collect/identity.py", "collected_at": ident_index["collected_at"], "schema_version": None},
        ],
        "completeness": {
            "complete": [
                {"what": "Campaign ATA transfer history", "why": f"{transfers_doc['provenance']['sigs_scanned']} signatures walked to the end of history (no cap), 0 failed, 0 missing; the ledger invariant closes at exactly 0 base units, which is only possible if the walk is exhaustive."},
                {"what": "Token-2022 holder set", "why": f"getProgramAccounts returned {holders_doc['integrity']['token_accounts_returned']:,} token accounts and their sum equals getTokenSupply exactly (delta 0). The mint has no mint authority, so that identity is a hard gate, not a coincidence."},
                {"what": "Minute price series", "why": f"{cov['candle_count']:,} contiguous 1m candles from the launch minute, gap_count {gap_count}, median {median_div}% from an independent oracle."},
                {"what": "Mint authorities and extensions", "why": "Read directly from the mint account: mint/freeze/update authority all null, only metadataPointer + tokenMetadata present."},
                {"what": "Deployer token-account history", "why": "Balance identity (inbound - outbound == balance) closes at 0; sells classified by the AMM program's own instruction name."},
            ],
            "sample_or_partial": [
                {"what": "Copycat mints", "why": "DexScreener keyword search only sees tokens that already have a tradeable pair. 36 found; this is a sample of impersonators, not a registry.", "count": ident_copycats["count"]},
                {"what": "Key moments", "why": "A curated list of 10 tweet ids, hydrated keylessly. It is a hand-picked seed, not a ranked top-N of everything posted about $TOAD."},
                {"what": "Pool creation timestamp", "why": "Attested by DexScreener's pairCreatedAt, not by an on-chain walk to the pool's first signature."},
                {"what": "Per-recipient identity", "why": "Not collected. Every recipient's identity field is null rather than guessed."},
                *_x_collected,
            ],
            "not_collected": [
                {"what": "Recipient-side sale evidence", "why": "We trace the campaign wallet's transfers, not each recipient's subsequent transactions. So 'moved out' is measured and 'sold' is null. Calling the difference a sale would be a fabrication."},
                *_x_not_collected,
                *_impressions_note,
                {"what": "Deployer / campaign wallet X-handle binding", "why": "pump.fun returns x_username=null for both wallets. The only verified wallet<->X binding is the 70,000 TOAD wallet-proof exhibit."},
            ],
        },
    }

    # ---------------------------------------------------------- caveats
    # A caveat this build has since resolved is itself a falsehood on the page.
    # The identity collector could not date the deployer's launch tweet because X
    # search is unauthorised; the social collector hydrated that exact id from its
    # curated list. Joining the two closes the row, so the caveat must go with it.
    resolved: list[str] = []
    caveats = []
    for c in (ident_index.get("caveats_the_site_must_render") or []):
        if launch_tweet is not None and c.startswith("slingoor (deployer) tweets about the launch"):
            resolved.append(
                "RESOLVED at build time: the deployer's launch tweet is dated "
                f"{launch_tweet['date']} from {launch_tweet['url']}. The identity collector could "
                "not find it (X search is unauthorised); the social collector had already hydrated "
                "that id keylessly. Cross-referencing the two collections closed the row."
            )
            continue
        caveats.append(c)
    caveats += thread_warnings
    caveats += [
        f"USD-at-drop is a candle-close estimate. A drop fills somewhere inside its minute, and the median candle spans {median_range}% high-to-low, so the campaign total carries about +/-${usd_precision['implied_uncertainty_usd']:,.0f} of irreducible uncertainty. Published rounded; never render a to-the-dollar total.",
        "'Moved out' is not 'sold'. A wallet-to-wallet transfer, an exchange deposit and a sale are indistinguishable without tracing each recipient's own transactions, which this pipeline does not do.",
        "The campaign is live. Every count here is a floor that grows; the timestamp on each figure is part of the figure.",
        "Live circulating supply is ~960.57M and falling as burns land, not the nominal 1B. Percentages computed against 1B understate concentration.",
    ]
    if stale:
        caveats.insert(0, "STALE: new on-chain activity has landed since this ledger was collected. Re-run the collectors.")

    # Reads on the page, not in a debugger. The old template appended the literal
    # field path "provenance.completeness.not_collected", which shipped an internal
    # JSON key to the front page as though it were a sentence.
    # Only recipients whose balance actually moved need tracing; the hardcoded 148
    # here survived a data refresh that took the recipient set to a different size.
    _moved = sum(
        status_counts.get(k, 0) for k in ("partial", "zero_balance", "account_closed")
    )
    open_questions = [
        f"{u.split(': ', 1)[-1].strip().rstrip('.')} — we could not collect a source we trust, "
        f"so it stays unverified rather than being stated."
        for u in ident_index.get("unverified", [])
        if not (launch_tweet is not None and "slingoor" in u and "launch" in u)
    ] + [
        f"Which recipients sold, versus moved tokens between their own wallets? Requires tracing the outbound transactions of the {_moved} recipients whose balance has fallen below what they received.",
        "Is the CTO multi-sig live, and who are the signers?",
        "Can any wallet other than the wallet-proof recipient be bound to an X handle by a signature or a timing exhibit?",
    ]

    # ------------------------------------------------------------ model
    model = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now,
        "generator": GENERATOR,
        "built_from": "data/collection/** (this file is constructed, never mutated in place)",
        "mint": MINT,
        "token_program": TOKEN_PROGRAM_NAME,
        "token_program_address": TOKEN_PROGRAM,
        "airdrop_wallet": CAMPAIGN_OWNER,
        "campaign_ata": CAMPAIGN_ATA,
        "deployer": DEPLOYER,
        "pricing_pool": PRICING_POOL,
        "bonding_curve": BONDING_CURVE,
        "token": {
            "name": ident_meta["onchain_name"]["value"],
            "symbol": ident_meta["onchain_symbol"]["value"],
            "real_mint": MINT,
            "mint": MINT,
            "decimals": DECIMALS,
            "token_program": TOKEN_PROGRAM_NAME,
            "token_program_address": TOKEN_PROGRAM,
            "supply_raw": str(supply_raw),
            "supply": ui_num(supply_raw),
            "supply_ui": to_ui(supply_raw),
            "supply_source": supply_source,
            "supply_slot": supply_slot,
            "supply_note": "Read live at build time. Supply drifts downward as burns land -- never hardcode it, and never write '~1B nominal'.",
            "nominal_supply": ui_num(nominal_raw),
            "burned_raw": str(burned_raw),
            "burned": ui_num(burned_raw),
            "burned_pct": round(burned_raw / nominal_raw * 100, 4),
            "creator": (ident_pump["wallets"][DEPLOYER] or {}).get("username"),
            "creator_wallet": DEPLOYER,
            "creator_x": "https://x.com/slingoorio",
            "creator_x_caveat": "pump.fun returns x_username=null for this wallet. The pump.fun display name 'slingoor' is self-chosen and is NOT proof of X identity.",
            "launch_date": (ident_pump.get("coin") or {}).get("created_at_iso", "")[:10],
            "launch_ts_iso": (ident_pump.get("coin") or {}).get("created_at_iso"),
            "mint_tx": ident_launch["rows"][0].get("sig"),
            "description": ident_meta["description"]["value"],
            "description_source": ident_meta["description"]["source"],
            "description_uri": ident_meta["onchain_uri"]["value"],
            "description_note": "The description is NOT stored on chain. The mint stores name, symbol and a uri; the uri is a content-addressed IPFS CID and the metadata update authority is null, so neither link can change.",
            "image": (ident_meta.get("ipfs_metadata", {}).get("parsed") or {}).get("image"),
            "website": (ident_meta.get("ipfs_metadata", {}).get("parsed") or {}).get("website"),
            "official_x": "https://x.com/eltoadpepe",
            "authorities": {
                "mint_authority": ident_auth["mint_authority"]["value"],
                "freeze_authority": ident_auth["freeze_authority"]["value"],
                "update_authority": ident_auth["update_authority"]["value"],
                "all_null": ident_auth["all_authorities_null"],
                "extensions": ident_auth["extensions_present"]["value"],
                "dangerous_extensions_absent": ident_auth["dangerous_extensions_absent"]["value"],
            },
        },
        "face": {
            "name": (profiles.get("mdudas") or {}).get("name", "Mike Dudas"),
            "handle": "mdudas",
            "x": "https://x.com/mdudas",
            "followers": ((profiles.get("mdudas") or {}).get("metrics") or {}).get("followers"),
            "avatar": (profiles.get("mdudas") or {}).get("avatar_url"),
            "role": "6th Man Ventures - pump.fun seed investor - public face of the $TOAD airdrop campaign",
            "pledge": (quotes[0]["text"] if quotes else None),
            "pledge_source": (quotes[0]["source"] if quotes else None),
            "wallet_attribution": {
                "wallet": CAMPAIGN_OWNER,
                "pumpfun_username": (ident_pump["wallets"][CAMPAIGN_OWNER] or {}).get("username"),
                "confidence": "high",
                "evidence": ident_proof.get("claim"),
                "proof_tx": (ident_proof.get("transfer") or {}).get("value", {}).get("sig"),
                "proof_gap_seconds": (ident_proof.get("gap_seconds") or {}).get("value"),
                "caveat": "pump.fun exposes no x_username for this wallet. The binding rests on the wallet-proof exhibit and the matching pump.fun handle -- label it, never assert it as self-disclosed.",
            },
        },
        "reconciliation": reconciliation,
        "stats": stats,
        "market": market,
        "market_snapshot": {
            "price_usd": current_price,
            "mcap_usd": round(mcap, 2),
            "as_of": st["current"]["iso"],
            "basis": "last 1m candle close on the canonical PumpSwap pool x live supply",
            "note": "Build-time snapshot. The header ticker pulls live from DexScreener client-side.",
        },
        "price_series": [[int(c[0]), float(c[4])] for c in ohlcv_doc["candles"]],
        "airdrop_daily": airdrop_daily,
        "recipients": recipients_out,
        "transfers": transfers_out,
        "inbound": inbound_out,
        "holders": {
            "count": holders_doc["holder_count"],
            "token_accounts": holders_doc["integrity"]["token_accounts_returned"],
            "zero_balance_owners": holders_doc["stats"]["zero_balance_owner_count"],
            "buckets": holders_doc["stats"]["balance_buckets"],
            "concentration": conc,
            "concentration_excluding_infrastructure": conc_ex,
            "concentration_note": "The raw top-1 holder IS the campaign treasury at 17.21%, which reads as insider concentration when it is the opposite. Render the ex-infrastructure column beside it.",
            "top": top_holders,
            "truncated_to": len(top_holders),
            "full_set": "data/raw/collection/onchain/holders.json",
        },
        "deployer_conduct": {
            "wallet": DEPLOYER,
            "balance_raw": str(ident_deployer["balance_raw"]),
            "balance": ui_num(ident_deployer["balance_raw"]),
            "never_sold": ident_deployer["never_sold"]["value"],
            "sells": ident_deployer["sell_tx_count"],
            "buys": ident_deployer["buy_tx_count"],
            "rebought": ui_num(ident_deployer["rebought_raw"]),
            "lp_deposits": ident_deployer["lp_deposit_tx_count"],
            "lp_deposited": ui_num(ident_deployer["lp_deposited_raw"]),
            "lp_withdrawals": ident_deployer["lp_withdraw_tx_count"],
            "lp_withdrawn": ui_num(ident_deployer["lp_withdrawn_raw"]),
            "method": ident_deployer["did_sell"]["method"],
            "correction": ident_deployer["classification_note"],
        },
        "timeline": timeline,
        "tweets": tweets,
        "quotes": quotes,
        "quotes_dropped": dropped_quotes,
        "conversation_board": _board,
        "archive": _archive,
        "viral": _viral,
        "viral_status": _viral_status,
        "copycats": [
            {
                "mint": c["mint"],
                "name": c["name"],
                "symbol": c["symbol"],
                "severity": c["severity"],
                "liquidity_usd": c.get("liquidity_usd"),
                "created_at": c.get("created_at"),
                "url": c.get("url"),
            }
            for c in ident_copycats["items"]
        ],
        "copycats_caveat": ident_copycats["caveat"],
        "wallet_proof": {
            "claim": ident_proof.get("claim"),
            "tx": (ident_proof.get("transfer") or {}).get("value"),
            "recipient_token_account": (ident_proof.get("recipient_token_account") or {}).get("value"),
            "tweet": (ident_proof.get("tweet") or {}).get("value"),
            "gap_seconds": (ident_proof.get("gap_seconds") or {}).get("value"),
            "recipient_identity": (ident_proof.get("recipient_identity") or {}).get("value"),
            "complete": ident_proof.get("exhibit_complete"),
        },
        "verification_ledger": ident_index.get("verification_ledger", []),
        "open_questions": open_questions,
        "caveats": caveats,
        "resolved_at_build": resolved,
        "sources": [
            f"https://solscan.io/token/{MINT}",
            f"https://solscan.io/account/{CAMPAIGN_ATA}",
            f"https://solscan.io/account/{CAMPAIGN_OWNER}",
            f"https://pump.fun/coin/{MINT}",
            f"https://www.geckoterminal.com/solana/pools/{PRICING_POOL}",
            ident_meta["onchain_uri"]["value"],
            "https://x.com/mdudas",
            "https://x.com/slingoorio",
            "https://x.com/eltoadpepe",
            "https://elreinoinfantil.fandom.com/wiki/El_Sapo_Pepe",
        ],
        "provenance": provenance,
    }

    # ------------------------------------------------- post-build asserts
    # A raw base-unit integer rendered as a token amount is a 1,000,000x lie that
    # looks plausible. Catch it here rather than on the page.
    if model["stats"]["total_amount"] != ui_num(out_raw):
        die("internal: stats.total_amount is not in display units")
    if model["stats"]["total_amount"] > 1e9:
        die(f"internal: stats.total_amount {model['stats']['total_amount']} exceeds nominal supply -- raw units leaked into a display field")
    for r in model["recipients"]:
        if r["total"] > 1e9:
            die(f"internal: recipient {r['wallet']} total {r['total']} exceeds nominal supply -- raw units leaked")
        if r["status"] not in {"holding", "partial", "zero_balance", "account_closed", "unknown"}:
            die(f"internal: recipient {r['wallet']} has invalid status {r['status']}")
        if r["status"] == "unknown" and r["balance_now"] is not None:
            die("internal: unknown status must carry a null balance")
    if sum(int(r["total_raw"]) for r in model["recipients"]) != out_raw:
        die("internal: recipient totals do not sum to the distributed total")
    if sum(int(t["amount_raw"]) for t in model["transfers"]) != out_raw:
        die("internal: transfer amounts do not sum to the distributed total")

    # ------------------------------------------------------------- write
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(model, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(out_path)

    # -------------------------------------------------------- raw mirror
    mirrored = []
    if not args.no_raw_mirror:
        RAW.mkdir(parents=True, exist_ok=True)
        dest_root = RAW / "collection"
        if dest_root.exists():
            shutil.rmtree(dest_root)
        for p in sorted(src.rglob("*.json")):
            if ".cache" in p.parts:
                continue
            rel = p.relative_to(src)
            d = dest_root / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, d)
            mirrored.append({
                "file": str((dest_root / rel).relative_to(ROOT)),
                # p inherits --src, which may be relative ("./data/collection");
                # relative_to(ROOT) then raises because it is not an absolute path.
                "source": str(p.resolve().relative_to(ROOT)),
                "bytes": d.stat().st_size,
                "sha256": sha256_file(d),
            })
        build_dir = RAW / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        for name, payload in raw_captures:
            fp = build_dir / name
            fp.write_text(json.dumps(payload, indent=2) + "\n")
            mirrored.append({
                "file": str(fp.relative_to(ROOT)),
                "source": "live JSON-RPC at build time",
                "bytes": fp.stat().st_size,
                "sha256": sha256_file(fp),
            })
        model_sha = sha256_file(out_path)
        (RAW / "MANIFEST.json").write_text(json.dumps({
            "generated_at": now,
            "generator": GENERATOR,
            "purpose": "Stored evidence. Every assertion on the site is reproducible from these captures; "
                       "sha256 lets a reader prove the file behind a claim has not been edited since the build.",
            "model": {"file": "data/model.json", "sha256": model_sha, "bytes": out_path.stat().st_size},
            "rpc_endpoints": live["endpoints_used"],
            "files": mirrored,
        }, indent=2) + "\n")

    # ------------------------------------------------------------ report
    size_kb = out_path.stat().st_size / 1024
    print()
    print("=" * 78)
    print(f"model.json written: {size_kb:,.0f} KB  ({out_path})")
    print("=" * 78)
    print(f"  INVARIANT      {inflow_raw:,} - {outflow_raw:,} - {snap_balance_raw:,} = {residual_raw}")
    print(f"                 residual {residual_raw} base units -- EXACTLY ZERO"
          + ("  (verified live at build time)" if live["ok"] else "  (collector snapshot; RPC unavailable)"))
    print(f"  distributed    {to_ui(out_raw)} TOAD across {out_count} transfers to {rec_count} recipients")
    print(f"  usd at drop    {approx_usd(round(total_usd, 2))}  (exact sum {total_usd:,.2f}, +/-${usd_precision['implied_uncertainty_usd']:,.0f} from candle range; published rounded)")
    print(f"  drop pricing   {usd_precision['priced_exact_minute']}/{len(transfers_out)} at the exact minute, "
          f"{usd_precision['priced_via_probe']} via probe, {unpriced} unpriced; hourly buckets would move the total ${usd_precision['hourly_vs_minute_delta_usd']:,.0f}")
    print(f"  status         holding {status_counts.get('holding', 0)} / partial {status_counts.get('partial', 0)}"
          f" / zero {status_counts.get('zero_balance', 0)} / closed {status_counts.get('account_closed', 0)}"
          f" / unknown {status_counts.get('unknown', 0)}")
    print(f"  still held     {rs['still_held_ui']} TOAD = {rs['still_held_pct_of_airdrop']}% of the airdrop")
    print(f"  sells detected {reconciliation['sells_detected']} (recipient-side sales: null, never measured)")
    print(f"  supply         {to_ui(supply_raw)} live ({model['token']['burned_pct']}% burned) -- not '~1B nominal'")
    print(f"  price          {cov['candle_count']:,} 1m candles, {gap_count} gaps, {median_div}% median divergence")
    print(f"  holders        {holders_doc['holder_count']:,} owners; top1 {conc['top1_pct']}% (ex-infra {conc_ex['top1_pct']}%)")
    print(f"  moments        {len(tweets)} tweets, {len(quotes)} verbatim quotes, {len(dropped_quotes)} dropped")
    print(f"  raw mirror     {len(mirrored)} files -> data/raw/ (sha256 in data/raw/MANIFEST.json)")
    print(f"  rpc            {rpc.calls} calls, {rpc.retries} retries, endpoints: {', '.join(live['endpoints_used']) or 'none'}")
    print(f"  built in       {time.time() - t0:.1f}s")
    if stale:
        print("  !! STALE: live chain has moved past this ledger; re-run the collectors.")
    print()


if __name__ == "__main__":
    main()
