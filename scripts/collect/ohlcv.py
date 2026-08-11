#!/usr/bin/env python3
"""Collect 1-minute USD price history for $TOAD — the input for "USD at drop".

Primary source : GeckoTerminal 1-minute OHLCV for the canonical PumpSwap pool.
Cross-check    : pump.fun swap-api 5m candles (independent oracle, asserted at build time).
Both are free and keyless. This script reads NO secrets and prints none.

WHY MINUTE CANDLES, NOT HOURLY
------------------------------
The airdrop window spans a 2.83x price swing ($0.007001 -> $0.019800). Pricing a drop off
its hour bucket gives median 5.55% error, p90 16.85%, max 70.79%. Minute resolution is the
entire point of the exhibit, so hourly buckets are not an acceptable fallback here.

WHY THIS POOL
-------------
PumpSwap Nx9dcw... is constant-product and is the canonical pricing venue. Do NOT price off
the Meteora DLMM pool — its reserve ratio is 6.6x off true spot and yields a silently
corrupt chart.

LAUNCH PRICE CAVEAT
-------------------
GeckoTerminal's first minute candle opens ~15x above the true launch price (it misses the
opening bonding-curve ticks). The true launch open comes from pump.fun's first candle and is
independently corroborated by bonding-curve math:
    30 virtual SOL / 1,073,000,191 virtual tokens * $75.83 = $0.0000021202  (0.4% match)
The series is NOT silently spliced. GeckoTerminal remains the sole source of `candles`;
pump.fun supplies `launch` and `launch_window_5m`, each labelled with its own source.

RATE LIMITS (measured, not documented)
--------------------------------------
GeckoTerminal's real limit is far stricter than its published 30/min and it returns NO
Retry-After header. 35 rapid calls gave 3x200 / 32x429; even paced at 2.2s the first 429 hit
on call #7. A full history is only ~4 calls, so this script paces at ~0.45s inside a burst of
6, backs off exponentially on 429, and caches raw pages to data/raw/. BUILD TIME ONLY —
never call this at runtime or from the browser.

Usage:
  python3 scripts/collect/ohlcv.py              # use cache if fresh, else fetch
  python3 scripts/collect/ohlcv.py --refresh    # force a full re-fetch
  python3 scripts/collect/ohlcv.py --max-age 0  # treat any cache as stale
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "collection" / "market"

# --- verified constants. Re-derive from chain before changing any of these. ---
MINT = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"
POOL = "Nx9dcwNs3iJxM5YAxshMHE4aYJHdDyyGMhVcmaSgfu8"  # PumpSwap, constant-product
MINT_CREATED_TS = 1786197553  # 2026-08-08T13:59:13Z
POOL_CREATED_TS = 1786197557  # 2026-08-08T13:59:17Z
LAUNCH_TS = MINT_CREATED_TS

GT_URL = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{POOL}/ohlcv/minute"
PF_URL = f"https://swap-api.pump.fun/v2/coins/{MINT}/candles"

GT_PAGE_LIMIT = 1000
GT_MAX_PAGES = 12  # ~12k minutes; a hard stop so a bad cursor can never loop forever
DIVERGENCE_THRESHOLD_PCT = 2.0  # median GT-vs-pump.fun; above this we FAIL, not publish
PROBE_OFFSETS = (0, -1, 1, -2)  # minute-bucket probe order for price_at()


# ----------------------------------------------------------------------------- utils
def iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class Pacer:
    """Burst-limited pacer: <=`burst` calls spaced `spacing`s, then `burst_pause`s."""

    def __init__(self, spacing: float = 0.45, burst: int = 6, burst_pause: float = 20.0):
        self.spacing = spacing
        self.burst = burst
        self.burst_pause = burst_pause
        self._last = 0.0
        self._in_burst = 0

    def wait(self) -> None:
        if self._in_burst >= self.burst:
            print(f"  [pace] burst of {self.burst} reached, pausing {self.burst_pause:.0f}s")
            time.sleep(self.burst_pause)
            self._in_burst = 0
        gap = time.monotonic() - self._last
        if gap < self.spacing:
            time.sleep(self.spacing - gap)
        self._last = time.monotonic()
        self._in_burst += 1


def get_json(url: str, params: dict, pacer: Pacer, tries: int = 6, label: str = ""):
    """GET with exponential backoff. Treats 429/5xx as retryable."""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    full = f"{url}?{qs}" if qs else url
    delay = 5.0
    for attempt in range(1, tries + 1):
        pacer.wait()
        req = urllib.request.Request(
            full,
            headers={"Accept": "application/json", "User-Agent": "toad-wiki/1.0 (build-time collector)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or e.code >= 500
            if not retryable or attempt == tries:
                body = ""
                try:
                    body = e.read().decode()[:300]
                except Exception:
                    pass
                raise RuntimeError(f"{label or url} HTTP {e.code}: {body}") from e
            # GeckoTerminal sends no Retry-After; honour it only if present.
            ra = e.headers.get("Retry-After") if e.headers else None
            sleep_for = float(ra) if (ra or "").strip().isdigit() else delay
            print(f"  [backoff] {label} HTTP {e.code}, retry {attempt}/{tries} in {sleep_for:.0f}s")
            time.sleep(sleep_for)
            delay = min(delay * 2, 120.0)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == tries:
                raise RuntimeError(f"{label or url} network error: {e}") from e
            print(f"  [backoff] {label} {e}, retry {attempt}/{tries} in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 120.0)
    raise RuntimeError(f"{label or url}: exhausted retries")


# ------------------------------------------------------------------- price lookup API
def build_price_index(candles: list) -> dict[int, float]:
    """minute bucket (ts // 60) -> close. Consumed by price_at()."""
    return {int(c[0]) // 60: float(c[4]) for c in candles}


def price_at(index: dict[int, float], ts: int | None, probes=PROBE_OFFSETS):
    """Price for a wall-clock unix ts.

    Returns (price, minute_offset). `minute_offset` is how many minutes away the quote
    came from: 0 = exact minute, -1 = previous minute, etc. Callers that care about
    precision should surface a non-zero offset rather than pretending it was exact.
    Returns (None, None) when no candle is within the probe window.
    """
    if ts is None:
        return None, None
    m = int(ts) // 60
    for d in probes:
        p = index.get(m + d)
        if p is not None:
            return p, d
    return None, None


# ------------------------------------------------------------------------- collection
def fetch_gt_history(pacer: Pacer) -> tuple[dict[int, list], list[dict]]:
    """Page GeckoTerminal minute candles backwards to launch. Returns (by_ts, page_meta)."""
    by_ts: dict[int, list] = {}
    pages: list[dict] = []
    before = None
    for page in range(1, GT_MAX_PAGES + 1):
        params = {"aggregate": 1, "limit": GT_PAGE_LIMIT, "currency": "usd", "before_timestamp": before}
        payload = get_json(GT_URL, params, pacer, label=f"geckoterminal p{page}")
        rows = (((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        raw_path = RAW / f"ohlcv_gt_minute_p{page}.json"
        raw_path.write_text(json.dumps(payload, indent=2) + "\n")
        if not rows:
            print(f"  page {page}: 0 candles — end of history")
            pages.append({"page": page, "before_timestamp": before, "candles": 0, "raw": raw_path.name})
            break
        # newest-first; dedupe by ts so an inclusive/exclusive cursor cannot corrupt the set
        new = 0
        for c in rows:
            t = int(c[0])
            if t not in by_ts:
                by_ts[t] = [t, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
                new += 1
        oldest = min(int(c[0]) for c in rows)
        newest = max(int(c[0]) for c in rows)
        pages.append(
            {
                "page": page,
                "before_timestamp": before,
                "candles": len(rows),
                "new_candles": new,
                "oldest_ts": oldest,
                "newest_ts": newest,
                "oldest_iso": iso(oldest),
                "newest_iso": iso(newest),
                "raw": raw_path.name,
            }
        )
        print(f"  page {page}: {len(rows)} candles ({new} new)  {iso(oldest)} -> {iso(newest)}")
        if oldest <= LAUNCH_TS:
            print("  reached launch minute — stopping")
            break
        if new == 0:
            print("  page added nothing new — stopping to avoid a cursor loop")
            break
        before = oldest
    else:
        print(f"  WARNING: hit GT_MAX_PAGES={GT_MAX_PAGES} without reaching launch")
    return by_ts, pages


def fetch_pumpfun(pacer: Pacer) -> list[dict]:
    """pump.fun 5m candles. `createdTs` is required by the API (it keys the coin's series)."""
    params = {"interval": "5m", "limit": 1000, "currency": "USD", "createdTs": MINT_CREATED_TS}
    payload = get_json(PF_URL, params, pacer, label="pump.fun")
    if not isinstance(payload, list):
        raise RuntimeError(f"pump.fun returned {type(payload).__name__}, expected list")
    (RAW / "ohlcv_pumpfun_5m.json").write_text(json.dumps(payload, indent=2) + "\n")
    out = []
    for row in payload:
        out.append(
            {
                "ts": int(row["timestamp"]) // 1000,  # API reports milliseconds
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        )
    out.sort(key=lambda r: r["ts"])
    return out


# ---------------------------------------------------------------------------- analysis
def cross_check(candles: list, pf: list) -> dict:
    """Median |GT - pump.fun| / pump.fun over aligned 5m buckets. FAILs above threshold."""
    buckets: dict[int, tuple[int, float]] = {}
    for c in candles:
        t = int(c[0])
        b = t - (t % 300)
        if b not in buckets or t > buckets[b][0]:
            buckets[b] = (t, float(c[4]))  # close of the last GT minute in the bucket
    diffs = []
    for row in pf:
        hit = buckets.get(row["ts"])
        if hit and row["close"] > 0:
            diffs.append(abs(hit[1] - row["close"]) / row["close"] * 100.0)
    if len(diffs) < 20:
        raise RuntimeError(f"cross-check has only {len(diffs)} overlapping candles — refusing to publish unverified prices")
    med = statistics.median(diffs)
    stats = {
        "oracle": "pump.fun swap-api 5m",
        "overlap_candles": len(diffs),
        "median_pct": round(med, 4),
        "mean_pct": round(statistics.fmean(diffs), 4),
        "p90_pct": round(statistics.quantiles(diffs, n=10)[8], 4),
        "max_pct": round(max(diffs), 4),
        "threshold_pct": DIVERGENCE_THRESHOLD_PCT,
        "passed": med < DIVERGENCE_THRESHOLD_PCT,
    }
    return stats


def find_gaps(candles: list) -> tuple[int, list[dict]]:
    """Missing 1-minute buckets between the first and last candle."""
    mins = sorted({int(c[0]) // 60 for c in candles})
    gaps = []
    total = 0
    for a, b in zip(mins, mins[1:]):
        if b - a > 1:
            missing = b - a - 1
            total += missing
            gaps.append(
                {
                    "after_ts": a * 60,
                    "after_iso": iso(a * 60),
                    "before_ts": b * 60,
                    "before_iso": iso(b * 60),
                    "missing_minutes": missing,
                }
            )
    return total, gaps


def coverage_probe(index: dict[int, float]) -> dict | None:
    """Prove the series actually prices the drops: resolve every known transfer ts."""
    path = ROOT / "data" / "collection" / "onchain" / "transfers.json"
    if not path.exists():
        return None
    try:
        transfers = (json.loads(path.read_text()) or {}).get("transfers") or []
    except Exception:
        return None
    if not transfers:
        return None
    exact = probed = missed = 0
    for t in transfers:
        _, off = price_at(index, t.get("ts"))
        if off is None:
            missed += 1
        elif off == 0:
            exact += 1
        else:
            probed += 1
    return {
        "source": "data/collection/onchain/transfers.json",
        "transfers": len(transfers),
        "priced_exact_minute": exact,
        "priced_via_probe": probed,
        "unpriced": missed,
        "coverage_pct": round((exact + probed) / len(transfers) * 100.0, 3),
    }


# -------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Collect 1-minute $TOAD price history.")
    ap.add_argument("--refresh", action="store_true", help="force a full re-fetch, ignore cache")
    ap.add_argument("--max-age", type=float, default=6.0, help="hours before the cache is stale (default 6)")
    ap.add_argument("--spacing", type=float, default=0.45, help="seconds between calls in a burst")
    args = ap.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    cache_path = RAW / "ohlcv_minute.json"
    pacer = Pacer(spacing=args.spacing)

    print(f"pool={POOL}")
    print(f"mint={MINT}")

    # ---- 1. minute candles (cache-first; GeckoTerminal is rate-limit hostile) ----
    cached = None
    if cache_path.exists() and not args.refresh:
        try:
            cached = json.loads(cache_path.read_text())
        except Exception:
            cached = None
    age_h = None
    if cached:
        age_h = (now_ts() - int(cached.get("fetched_at_ts", 0))) / 3600.0
        covers_launch = int(cached.get("oldest_ts", 1 << 62)) <= LAUNCH_TS + 120
        if age_h <= args.max_age and covers_launch and cached.get("candles"):
            print(f"using cached candles ({age_h:.2f}h old, {len(cached['candles'])} candles) — 0 network calls")
        else:
            reason = "stale" if age_h > args.max_age else "does not reach launch"
            print(f"cache {reason} — refetching")
            cached = None

    if cached:
        by_ts = {int(c[0]): c for c in cached["candles"]}
        pages = cached.get("pages", [])
    else:
        print("fetching GeckoTerminal minute candles (newest-first, paging back to launch)…")
        by_ts, pages = fetch_gt_history(pacer)
        if not by_ts:
            print("FAIL: GeckoTerminal returned no candles")
            return 1

    candles = [by_ts[t] for t in sorted(by_ts)]  # oldest-first
    first_ts, last_ts = int(candles[0][0]), int(candles[-1][0])

    if not cached:
        cache_path.write_text(
            json.dumps(
                {
                    "fetched_at": iso(now_ts()),
                    "fetched_at_ts": now_ts(),
                    "source": "geckoterminal",
                    "endpoint": GT_URL,
                    "pool": POOL,
                    "interval": "1m",
                    "currency": "usd",
                    "oldest_ts": first_ts,
                    "newest_ts": last_ts,
                    "pages": pages,
                    "candles": candles,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"cached raw candles -> {cache_path}")

    # ---- 2. independent oracle + build-time assert ----
    print("fetching pump.fun cross-check oracle…")
    pf = fetch_pumpfun(pacer)
    print(f"  {len(pf)} 5m candles  {iso(pf[0]['ts'])} -> {iso(pf[-1]['ts'])}")
    xc = cross_check(candles, pf)
    print(
        f"  divergence: median {xc['median_pct']}%  mean {xc['mean_pct']}%  "
        f"p90 {xc['p90_pct']}%  max {xc['max_pct']}%  over {xc['overlap_candles']} buckets"
    )
    if not xc["passed"]:
        print(
            f"FAIL: median divergence {xc['median_pct']}% >= {DIVERGENCE_THRESHOLD_PCT}% threshold. "
            "Refusing to publish a price series two independent sources disagree on."
        )
        return 1
    print(f"  cross-check PASSED (< {DIVERGENCE_THRESHOLD_PCT}%)")

    # ---- 3. analysis ----
    gap_count, gaps = find_gaps(candles)
    index = build_price_index(candles)

    ath = max(candles, key=lambda c: float(c[2]))
    atl = min(candles, key=lambda c: float(c[3]))
    current = candles[-1]

    launch_price = pf[0]["open"]
    gt_first_open = float(candles[0][1])
    launch_overstatement = gt_first_open / launch_price if launch_price else None

    probe = coverage_probe(index)

    payload = {
        "collected_at": iso(now_ts()),
        "mint": MINT,
        "pool": POOL,
        "pool_kind": "PumpSwap constant-product (canonical pricing venue)",
        "interval": "1m",
        "currency": "usd",
        "source": {
            "primary": "GeckoTerminal /networks/solana/pools/{pool}/ohlcv/minute",
            "cross_check": "pump.fun swap-api /v2/coins/{mint}/candles?interval=5m",
            "keyless": True,
            "build_time_only": True,
            "note": "Never call either endpoint at runtime or from the browser.",
        },
        "warnings": [
            "Do NOT price off the Meteora DLMM pool — its reserve ratio is 6.6x off true spot.",
            "Do NOT bucket by hour — hourly pricing gives median 5.55% / p90 16.85% / max 70.79% "
            "error across the airdrop window.",
        ],
        "launch": {
            "price_usd": launch_price,
            "ts": MINT_CREATED_TS,
            "iso": iso(MINT_CREATED_TS),
            "candle_bucket_ts": pf[0]["ts"],
            "candle_bucket_iso": iso(pf[0]["ts"]),
            "bucket_note": (
                "pump.fun 5m buckets are floor-aligned, so the launch candle is labelled "
                f"{iso(pf[0]['ts'])} while the mint was actually created {iso(MINT_CREATED_TS)}. "
                "`ts` above is the real launch; `candle_bucket_ts` is the candle it was read from."
            ),
            "source": "pump.fun first 5m candle open",
            "corroboration": "bonding curve: 30 virtual SOL / 1,073,000,191 virtual tokens * $75.83 "
            "= $0.0000021202 (0.4% match)",
            "geckoterminal_first_open_usd": gt_first_open,
            "note": (
                f"GeckoTerminal's first minute candle opens at ${gt_first_open:.10f}, which OVERSTATES "
                f"the true launch by {launch_overstatement:.1f}x because it misses the opening "
                "bonding-curve ticks. Launch price is taken from pump.fun; the `candles` series is "
                "GeckoTerminal only and is not spliced."
            ),
        },
        "coverage": {
            "candle_count": len(candles),
            "first_ts": first_ts,
            "first_iso": iso(first_ts),
            "last_ts": last_ts,
            "last_iso": iso(last_ts),
            "span_minutes": (last_ts - first_ts) // 60 + 1,
            "gap_count": gap_count,
            "gap_runs": gaps,
            "contiguous": gap_count == 0,
            "mint_created_ts": MINT_CREATED_TS,
            "mint_created_iso": iso(MINT_CREATED_TS),
            "pool_created_ts": POOL_CREATED_TS,
            "pool_created_iso": iso(POOL_CREATED_TS),
            "minutes_after_pool_creation": (first_ts - POOL_CREATED_TS) // 60,
        },
        "stats": {
            "ath": {"price_usd": float(ath[2]), "ts": int(ath[0]), "iso": iso(ath[0]), "basis": "candle high"},
            "atl": {"price_usd": float(atl[3]), "ts": int(atl[0]), "iso": iso(atl[0]), "basis": "candle low"},
            "current": {
                "price_usd": float(current[4]),
                "ts": int(current[0]),
                "iso": iso(current[0]),
                "basis": "candle close",
            },
            "from_launch_x": round(float(current[4]) / launch_price, 2) if launch_price else None,
            "ath_from_launch_x": round(float(ath[2]) / launch_price, 2) if launch_price else None,
            "drawdown_from_ath_pct": round((float(current[4]) / float(ath[2]) - 1) * 100.0, 2),
            "total_volume_usd": round(sum(float(c[5]) for c in candles), 2),
        },
        "cross_check": xc,
        "price_lookup": {
            "rule": "bucket = ts // 60; probe minute offsets (0, -1, +1, -2) in that order",
            "reference_impl": "scripts/collect/ohlcv.py :: build_price_index() / price_at()",
            "returns": "(price_usd, minute_offset); offset 0 means the exact minute",
        },
        "drop_pricing_coverage": probe,
        "launch_window_5m": [
            {"ts": r["ts"], "iso": iso(r["ts"]), "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]}
            for r in pf[:12]
        ],
        "candles_schema": ["ts", "open", "high", "low", "close", "volume_usd"],
        "candles": candles,
    }

    out_path = OUT / "ohlcv_minute.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n")

    # ---- 4. report ----
    print()
    print(f"wrote {out_path}")
    print(f"  candles       {len(candles)}  ({iso(first_ts)} -> {iso(last_ts)})")
    print(f"  span          {(last_ts - first_ts) // 60 + 1} minutes")
    print(f"  gaps          {gap_count} missing minute(s) across {len(gaps)} run(s)")
    print(f"  divergence    median {xc['median_pct']}% vs pump.fun ({xc['overlap_candles']} buckets)")
    print(f"  launch        ${launch_price:.10f}  @ {iso(MINT_CREATED_TS)}  (pump.fun first candle open)")
    print(f"  ATH           ${float(ath[2]):.10f} @ {iso(ath[0])}")
    print(f"  current       ${float(current[4]):.10f} @ {iso(current[0])}")
    if probe:
        print(
            f"  drop pricing  {probe['coverage_pct']}% of {probe['transfers']} transfers priced "
            f"({probe['priced_exact_minute']} exact, {probe['priced_via_probe']} probed, {probe['unpriced']} unpriced)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
