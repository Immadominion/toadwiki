#!/usr/bin/env python3
"""Collect X/social evidence for toad-wiki.

ARCHITECTURE
------------
Two independent planes, so the wiki degrades gracefully instead of dying:

  ENUMERATION (discovering *which* tweets exist)  -> requires X API v2 bearer token.
      /2/tweets/search/all, /2/tweets/counts/all, /2/users/{id}/tweets
      There is NO working keyless substitute (verified: the old
      cdn.syndication.twimg.com/timeline/profile route now returns HTTP 200 with a
      zero-byte body, and fxtwitter's /{user} route returns profile stats only, no
      timeline array). If the token is dead, enumeration is *reported as refused*,
      never faked.

  HYDRATION (turning a known tweet id into text + metrics) -> 100% KEYLESS, free forever.
      This is what lets the wiki refresh engagement numbers indefinitely at zero cost.

KEYLESS SOURCE MATRIX (measured, not assumed)
---------------------------------------------
  cdn.syndication.twimg.com/tweet-result   text TRUNCATED at 280 chars on note tweets,
                                           but carries entities/media + the note_tweet
                                           marker. No view count.
  api.fxtwitter.com                        FULL note-tweet text AND `views`.  <-- the
                                           impression number. Primary source.
  api.vxtwitter.com                        FULL text, byte-identical to fxtwitter, but
                                           its likes/replies/retweets are hardcoded 0.
                                           TEXT-ONLY CROSS-VALIDATOR. Never take metrics
                                           from it.

The fx/vx pair gives us a real integrity check: two independent renderers must agree
byte-for-byte on the text before we call it verified. That check exists because the repo
previously shipped a FABRICATED 265-char version of the pledge tweet that welded five
non-adjacent fragments together with ellipses, silently deleting the mint address, the
~$200 buy size, and the entire community-transition paragraph from a 1,193-char original.

Every metric carries its own `captured_at`: these numbers move (the pledge tweet gained
5 views between two probes minutes apart) and the site must stamp them.

USAGE
-----
  python3 scripts/collect/social.py                 # everything available
  python3 scripts/collect/social.py --refresh       # keyless metric refresh only (free)
  python3 scripts/collect/social.py --no-api        # force keyless path

Secrets: X_BEARER_TOKEN is read from the environment and never printed, logged, or
written to any output file.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "collection" / "social"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 toad-wiki-collector"

MINT = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"

# Campaign start. The mint was created 2026-08-08T13:59:13Z; we reach back a few days
# to catch the pre-launch chatter that led to it.
CAMPAIGN_START = "2026-08-04T00:00:00Z"

ACCOUNTS = {
    "mdudas": "7184612",
    "slingoorio": "1509239489021030405",
    "eltoadpepe": "2086108195693719552",
}

# Curated key moments. `expected_author` is asserted against what we actually fetch so a
# recycled or misattributed id shows up as a flag instead of a silent lie.
KEY_MOMENTS = [
    {
        "id": "2086595256208748852",
        "expected_author": "mdudas",
        "label": "origin story + \"never sell\" pledge",
        "note": "1,193-char NOTE TWEET. The repo previously shipped a 265-char fabricated "
                "splice of this. Must be stored in full.",
        "is_note_tweet": True,
    },
    {
        "id": "2086882992010440789",
        "expected_author": "mdudas",
        "label": "\"welcome, sent you some\" + 70K screenshot (wallet-proof exhibit)",
    },
    {
        "id": "2086106068636127279",
        "expected_author": "slingoorio",
        "label": "\"Might have to send the supply to Mike Dudas...\"",
    },
    {
        "id": "2086921237855719848",
        "expected_author": "eltoadpepe",
        "label": "art contest",
    },
    {
        "id": "2086922779023008095",
        "expected_author": "slingoorio",
        "label": "slingoor on dumping",
    },
    # Conflict-of-interest thread. Note the first id is @ZeusRWA, not slingoor.
    {
        "id": "2086956264542687581",
        "expected_author": None,
        "label": "conflict-of-interest thread (1/3)",
        "thread": "conflict_of_interest",
    },
    {
        "id": "2086953360914031090",
        "expected_author": "mdudas",
        "label": "conflict-of-interest thread (2/3)",
        "thread": "conflict_of_interest",
    },
    {
        "id": "2086962659555950741",
        "expected_author": "mdudas",
        "label": "conflict-of-interest thread (3/3)",
        "thread": "conflict_of_interest",
    },
    # Already referenced by data/model.json; hydrated so the site stops carrying stale copies.
    {
        "id": "2086927633011331359",
        "expected_author": "slingoorio",
        "label": "referenced by model.json",
    },
    {
        "id": "2086404728359915929",
        "expected_author": "mdudas",
        "label": "referenced by model.json",
    },
]

TOAD_TERMS = ("toad", "$toad", "eltoadpepe", MINT.lower(), "pepe")

SEARCH_QUERY = '($TOAD OR "el toad" OR eltoadpepe OR A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump) -is:retweet'

TWEET_FIELDS = ("id,text,created_at,author_id,public_metrics,entities,referenced_tweets,"
                "conversation_id,in_reply_to_user_id,attachments,note_tweet,lang")
USER_FIELDS = "id,name,username,profile_image_url,public_metrics,verified,created_at,description"
EXPANSIONS = "author_id,attachments.media_keys,referenced_tweets.id"
MEDIA_FIELDS = "url,preview_image_url,type,alt_text"

# Posts returned by the referenced_tweets.id expansion, accumulated across every
# paginated call. Already billed for; keeping them costs nothing extra and is what
# lets the conversation graph resolve its edges.
REF_POOL: dict = {}

# Users returned by the author_id expansion. These bill at $0.010 each -- DOUBLE a
# post read -- so discarding them is the most expensive mistake this collector can
# make. Captured here so every graph node can name its author.
USER_POOL: dict = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------

def http_get(url: str, headers: dict | None = None, timeout: int = 30):
    """Return (payload, error). error is (code, body, headers) or (None, reason, {})."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        if not raw.strip():
            return None, (None, "empty body", {})
        return json.loads(raw), None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        return None, (e.code, body, dict(e.headers))
    except Exception as e:  # network, timeout, bad JSON
        return None, (None, f"{type(e).__name__}: {e}"[:300], {})


def keyless_get(url: str, attempts: int = 3, timeout: int = 20):
    """Keyless GET with backoff. Returns (payload, error_string)."""
    last = "unknown"
    for i in range(attempts):
        payload, err = http_get(url, timeout=timeout)
        if err is None:
            return payload, None
        code, body, _ = err
        last = f"HTTP {code}: {body}" if code else body
        if code in (404, 403):  # deleted / protected — retrying will not help
            return None, last
        time.sleep(1.5 * (i + 1))
    return None, last


# --------------------------------------------------------------------------------------
# X API plane (enumeration)
# --------------------------------------------------------------------------------------

class XAPI:
    """X API v2 client. Classifies auth state up front and refuses to pretend."""

    BASE = "https://api.x.com/2"

    def __init__(self, token: str | None, max_sleep: int = 900):
        self.token = token
        self.max_sleep = max_sleep
        self.status = "untested"
        self.detail = ""
        self.calls = 0

    @property
    def usable(self) -> bool:
        return self.status == "ok"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def probe(self) -> dict:
        """STEP 1. Cheapest possible authenticated call, then classify.

        We probe /2/users/by/username (free, never metered) *and* the two search
        endpoints. That distinction matters: a spend cap hits metered search only and
        leaves the free lookup working, whereas a revoked token 401s everything. Guessing
        wrong here sends the whole run down the wrong branch.
        """
        result = {"checked_at": now_iso(), "endpoints": {}}
        if not self.token:
            self.status, self.detail = "missing", "X_BEARER_TOKEN not set in environment"
            result.update(status=self.status, detail=self.detail)
            return result

        probes = {
            "users_by_username": f"{self.BASE}/users/by/username/mdudas",
            "search_recent": f"{self.BASE}/tweets/search/recent?query=%24TOAD&max_results=10",
            "search_all": f"{self.BASE}/tweets/search/all?query=%24TOAD&max_results=10",
        }
        codes = {}
        for name, url in probes.items():
            payload, err = http_get(url, headers=self._headers(), timeout=25)
            if err is None:
                codes[name] = 200
                result["endpoints"][name] = {"http": 200, "ok": True}
            else:
                code, body, _ = err
                codes[name] = code
                snippet = body.replace("\n", " ")[:160]
                result["endpoints"][name] = {"http": code, "ok": False, "detail": snippet}

        free_ok = codes.get("users_by_username") == 200
        search_ok = 200 in (codes.get("search_recent"), codes.get("search_all"))

        if search_ok:
            self.status, self.detail = "ok", "search endpoints authorized"
        elif free_ok and 403 in codes.values():
            self.status = "spend_capped"
            self.detail = ("free lookups work but metered search returns 403 — "
                           "spend cap reached, enumeration unavailable")
        elif all(c == 401 for c in codes.values()):
            self.status = "unauthorized"
            self.detail = ("every endpoint returned 401, including the free "
                           "/2/users/by/username lookup that is never metered — the token "
                           "is invalid, revoked, or regenerated. This is NOT a spend cap.")
        else:
            self.status = "error"
            self.detail = f"unexpected mix of responses: {codes}"

        result.update(status=self.status, detail=self.detail)
        return result

    def get(self, path: str, params: dict):
        """Authenticated GET. Sleeps until x-rate-limit-reset on 429 instead of aborting."""
        url = f"{self.BASE}{path}?" + urllib.parse.urlencode(params)
        for attempt in range(6):
            payload, err = http_get(url, headers=self._headers(), timeout=45)
            self.calls += 1
            if err is None:
                return payload, None
            code, body, hdrs = err
            if code == 429:
                reset = hdrs.get("x-rate-limit-reset")
                wait = 60
                if reset and str(reset).isdigit():
                    wait = max(5, int(reset) - int(time.time()) + 3)
                wait = min(wait, self.max_sleep)
                log(f"    429 rate limited — sleeping {wait}s until reset (attempt {attempt+1})")
                time.sleep(wait)
                continue
            if code in (500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            return None, f"HTTP {code}: {body[:200]}"
        return None, "exhausted retries (persistent 429/5xx)"

    def paginate(self, path: str, params: dict, cursor_key: str = "next_token",
                 cursor_param: str = "next_token", max_pages: int = 40):
        """Yield pages, following the cursor. Returns (rows, includes, pages, error)."""
        rows, includes_users, includes_media, pages, cursor = [], {}, {}, 0, None
        includes_tweets: dict = {}
        while pages < max_pages:
            p = dict(params)
            if cursor:
                p[cursor_param] = cursor
            payload, err = self.get(path, p)
            if err:
                return rows, (includes_users, includes_media, includes_tweets), pages, err
            pages += 1
            rows.extend(payload.get("data") or [])
            inc = payload.get("includes") or {}
            for u in inc.get("users") or []:
                includes_users[u["id"]] = u
            for m in inc.get("media") or []:
                includes_media[m.get("media_key")] = m
            # Parent/quoted posts pulled in by the referenced_tweets.id expansion.
            # These are the edges of the conversation graph.
            for tw in inc.get("tweets") or []:
                includes_tweets[tw["id"]] = tw
            cursor = (payload.get("meta") or {}).get(cursor_key)
            if not cursor:
                break
            time.sleep(1.2)
        return rows, (includes_users, includes_media, includes_tweets), pages, None


# --------------------------------------------------------------------------------------
# Keyless hydration plane
# --------------------------------------------------------------------------------------

def fetch_syndication(tweet_id: str):
    url = (f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}"
           f"&token=a&lang=en")
    return keyless_get(url)


def fetch_fx(tweet_id: str):
    return keyless_get(f"https://api.fxtwitter.com/i/status/{tweet_id}")


def fetch_vx(tweet_id: str):
    return keyless_get(f"https://api.vxtwitter.com/i/status/{tweet_id}")


def _iso_from_twitter_date(s: str | None) -> str | None:
    """'Sun Aug 09 23:27:30 +0000 2026' -> '2026-08-09T23:27:30Z'."""
    if not s:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return s


def hydrate_tweet(tweet_id: str, meta: dict | None = None) -> dict:
    """Merge the three keyless sources into one verified record.

    Text comes from fxtwitter (full, including note tweets) and is confirmed against
    vxtwitter byte-for-byte. Metrics come from fxtwitter only — vxtwitter reports zeros
    and syndication has no view count. Syndication supplies entities/media and the
    authoritative note_tweet marker.
    """
    meta = meta or {}
    captured = now_iso()
    rec = {
        "id": tweet_id,
        "captured_at": captured,
        "sources_ok": [],
        "sources_failed": {},
    }
    rec.update({k: v for k, v in meta.items() if k in ("label", "note", "thread")})

    syn, syn_err = fetch_syndication(tweet_id)
    fx, fx_err = fetch_fx(tweet_id)

    fx_tweet = (fx or {}).get("tweet") if isinstance(fx, dict) else None
    if fx_tweet:
        rec["sources_ok"].append("fxtwitter")
    elif fx_err:
        rec["sources_failed"]["fxtwitter"] = fx_err
    else:
        rec["sources_failed"]["fxtwitter"] = f"code={(fx or {}).get('code')} {(fx or {}).get('message')}"

    if syn:
        rec["sources_ok"].append("syndication")
    else:
        rec["sources_failed"]["syndication"] = syn_err or "no payload"

    if not fx_tweet and not syn:
        rec["status"] = "unresolved"
        return rec

    # ---- author -------------------------------------------------------------------
    author = {}
    if fx_tweet:
        a = fx_tweet.get("author") or {}
        author = {
            "handle": a.get("screen_name"),
            "name": a.get("name"),
            "id": a.get("id"),
            "avatar_url": a.get("avatar_url"),
            "followers_at_capture": a.get("followers"),
        }
    elif syn:
        u = syn.get("user") or {}
        author = {"handle": u.get("screen_name"), "name": u.get("name"), "id": u.get("id_str")}
    rec["author"] = author

    expected = meta.get("expected_author")
    if expected and author.get("handle") and expected.lower() != str(author["handle"]).lower():
        rec["author_mismatch"] = {"expected": expected, "actual": author["handle"]}

    # ---- text ---------------------------------------------------------------------
    syn_text = (syn or {}).get("text") or ""
    fx_text = (fx_tweet or {}).get("text") or ""
    is_note = bool((syn or {}).get("note_tweet")) or len(fx_text) > 280

    text = fx_text or syn_text
    text_source = "fxtwitter" if fx_text else "syndication"
    integrity = "single_source"
    vx_text = None

    # Cross-validate anything long or note-flagged: that is exactly where the previous
    # fabrication happened, and where syndication silently truncates.
    if is_note or len(text) > 280:
        vx, vx_err = fetch_vx(tweet_id)
        vx_text = (vx or {}).get("text") if isinstance(vx, dict) else None
        if vx_text:
            rec["sources_ok"].append("vxtwitter")
            integrity = "verified_identical" if vx_text == text else "MISMATCH"
            if vx_text != text:
                rec["text_mismatch"] = {
                    "fxtwitter_len": len(text),
                    "vxtwitter_len": len(vx_text),
                    "vxtwitter_sha256": hashlib.sha256(vx_text.encode()).hexdigest(),
                }
                # Prefer the longer rendering; never silently keep a shorter one.
                if len(vx_text) > len(text):
                    text, text_source = vx_text, "vxtwitter"
        elif vx_err:
            rec["sources_failed"]["vxtwitter"] = vx_err

    rec["text"] = text
    rec["text_len"] = len(text)
    rec["text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    rec["text_source"] = text_source
    rec["text_integrity"] = integrity
    rec["is_note_tweet"] = is_note
    if is_note and syn_text:
        rec["syndication_truncated_len"] = len(syn_text)
        rec["truncation_avoided_chars"] = max(0, len(text) - len(syn_text))

    # ---- timing -------------------------------------------------------------------
    rec["created_at"] = (_iso_from_twitter_date((fx_tweet or {}).get("created_at"))
                         or _iso_from_twitter_date((syn or {}).get("created_at")))

    # ---- metrics (fxtwitter only; every one stamped) --------------------------------
    if fx_tweet:
        rec["metrics"] = {
            "likes": fx_tweet.get("likes"),
            "retweets": fx_tweet.get("retweets"),
            "replies": fx_tweet.get("replies"),
            "quotes": fx_tweet.get("quotes"),
            "bookmarks": fx_tweet.get("bookmarks"),
            "views": fx_tweet.get("views"),
            "captured_at": captured,
            "source": "fxtwitter",
        }
    if syn:
        # Independent second opinion on likes/replies — useful drift check.
        rec["metrics_syndication"] = {
            "likes": syn.get("favorite_count"),
            "replies": syn.get("conversation_count"),
            "captured_at": captured,
            "source": "cdn.syndication.twimg.com",
        }

    # ---- media --------------------------------------------------------------------
    media = []
    for m in ((fx_tweet or {}).get("media") or {}).get("all") or []:
        media.append({"type": m.get("type"), "url": m.get("url"),
                      "thumbnail_url": m.get("thumbnail_url"),
                      "width": m.get("width"), "height": m.get("height")})
    if not media and syn:
        for m in (syn.get("entities") or {}).get("media") or []:
            media.append({"type": m.get("type"), "url": m.get("media_url_https")})
    rec["media"] = media
    rec["has_media"] = bool(media)

    handle = author.get("handle") or "i"
    rec["url"] = f"https://x.com/{handle}/status/{tweet_id}"
    rec["lang"] = (fx_tweet or {}).get("lang") or (syn or {}).get("lang")
    rec["conversation_id"] = (syn or {}).get("conversation_id") or (fx_tweet or {}).get("conversation_id")

    # The pledge tweet literally contains the mint address. When it does, that is an
    # independent on-tweet corroboration of the on-chain constant — worth surfacing.
    rec["mentions_mint"] = MINT.lower() in text.lower()
    rec["status"] = "ok"
    return rec


def hydrate_many(items: list[dict], workers: int = 6) -> list[dict]:
    """Hydrate in parallel. Concurrency 6 — measured safe across all three keyless hosts."""
    out: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(hydrate_tweet, it["id"], it): it["id"] for it in items}
        for fut in concurrent.futures.as_completed(futs):
            tid = futs[fut]
            try:
                out[tid] = fut.result()
            except Exception as e:
                out[tid] = {"id": tid, "status": "error", "error": f"{type(e).__name__}: {e}",
                            "captured_at": now_iso()}
    # preserve caller order
    return [out[it["id"]] for it in items if it["id"] in out]


def fetch_profile(handle: str) -> dict:
    payload, err = keyless_get(f"https://api.fxtwitter.com/{handle}")
    captured = now_iso()
    u = (payload or {}).get("user") if isinstance(payload, dict) else None
    if not u:
        return {"handle": handle, "status": "unresolved", "error": err or "no user object",
                "captured_at": captured}
    return {
        "handle": u.get("screen_name"),
        "name": u.get("name"),
        "id": u.get("id"),
        "description": u.get("description"),
        "avatar_url": u.get("avatar_url"),
        "banner_url": u.get("banner_url"),
        "joined": _iso_from_twitter_date(u.get("joined")),
        "website": (u.get("website") or {}).get("url") if isinstance(u.get("website"), dict) else u.get("website"),
        "metrics": {
            "followers": u.get("followers"),
            "following": u.get("following"),
            "tweets": u.get("tweets"),
            "likes": u.get("likes"),
            "media_count": u.get("media_count"),
            "captured_at": captured,
            "source": "fxtwitter",
        },
        "status": "ok",
    }


# --------------------------------------------------------------------------------------
# Enumeration (token required)
# --------------------------------------------------------------------------------------

def is_toad_relevant(text: str) -> bool:
    t = (text or "").lower()
    return any(term in t for term in TOAD_TERMS)


def day_windows(start_iso: str, end_iso: str):
    """Yield UTC day-sized [start, end) windows.

    /2/tweets/search/all is known to return next_token=null prematurely on wide windows,
    silently truncating results. Day slicing bounds the damage and makes any remaining
    truncation visible as an implausible per-day count.
    """
    start = datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=1), end)
        yield (cur.strftime("%Y-%m-%dT%H:%M:%SZ"), nxt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        cur = nxt


def collect_timeline(api: XAPI, handle: str, user_id: str, start_time: str) -> dict:
    captured = now_iso()
    if not api.usable:
        return {"handle": handle, "user_id": user_id, "status": "refused",
                "reason": f"x_api_{api.status}", "detail": api.detail,
                "tweets": [], "captured_at": captured}
    log(f"  timeline @{handle} since {start_time}")
    rows, (users, media, ref_tweets), pages, err = api.paginate(
        f"/users/{user_id}/tweets",
        {"max_results": "100", "start_time": start_time, "exclude": "retweets",
         "tweet.fields": TWEET_FIELDS, "expansions": EXPANSIONS,
         "media.fields": MEDIA_FIELDS, "user.fields": USER_FIELDS},
        cursor_key="next_token", cursor_param="pagination_token", max_pages=20,
    )
    REF_POOL.update(ref_tweets or {})
    USER_POOL.update(users or {})
    tweets = []
    for r in rows:
        text = (r.get("note_tweet") or {}).get("text") or r.get("text") or ""
        pm = r.get("public_metrics") or {}
        tweets.append({
            "id": r.get("id"),
            "created_at": r.get("created_at"),
            "text": text,
            "text_len": len(text),
            "is_note_tweet": bool(r.get("note_tweet")),
            "toad_relevant": is_toad_relevant(text),
            "conversation_id": r.get("conversation_id"),
            "author_id": r.get("author_id"),
            "in_reply_to_user_id": r.get("in_reply_to_user_id"),
            # [{"type":"replied_to"|"quoted"|"retweeted","id":"..."}] -- the graph edges
            "referenced_tweets": r.get("referenced_tweets") or [],
            "metrics": {
                "likes": pm.get("like_count"), "retweets": pm.get("retweet_count"),
                "replies": pm.get("reply_count"), "quotes": pm.get("quote_count"),
                "bookmarks": pm.get("bookmark_count"), "views": pm.get("impression_count"),
                "captured_at": captured, "source": "x_api_v2",
            },
        })
    relevant = [t for t in tweets if t["toad_relevant"]]
    return {
        "handle": handle, "user_id": user_id,
        "status": "partial" if err else "ok",
        "error": err,
        "pages_fetched": pages,
        "total_tweets": len(tweets),
        "toad_relevant_count": len(relevant),
        "start_time": start_time,
        "captured_at": captured,
        "tweets": tweets,
    }


def collect_search(api: XAPI, start_time: str, end_time: str) -> dict:
    captured = now_iso()
    if not api.usable:
        return {"status": "refused", "reason": f"x_api_{api.status}", "detail": api.detail,
                "query": SEARCH_QUERY, "per_day": [], "top_by_views": [],
                "captured_at": captured}

    per_day, all_rows, users = [], [], {}
    for w_start, w_end in day_windows(start_time, end_time):
        log(f"  search window {w_start[:10]}")
        rows, (u, _m, _rt), pages, err = api.paginate(
            "/tweets/search/all",
            {"query": SEARCH_QUERY, "max_results": "100",
             "start_time": w_start, "end_time": w_end,
             "tweet.fields": TWEET_FIELDS, "expansions": EXPANSIONS,
             "media.fields": MEDIA_FIELDS, "user.fields": USER_FIELDS},
            max_pages=30,
        )
        REF_POOL.update(_rt or {})
        USER_POOL.update(u or {})
        users.update(u)
        all_rows.extend(rows)
        per_day.append({"date": w_start[:10], "count": len(rows), "pages": pages,
                        "error": err,
                        # A full page count with no cursor is the truncation signature.
                        # A day is truncated if pagination stopped on a full page OR if the
                        # window errored at all. The old test required `not err`, so a day that
                        # died on HTTP 402 mid-run reported possible_truncation=False and the
                        # whole search claimed to be complete while missing entire days.
                        "possible_truncation": bool(err) or (bool(rows) and len(rows) % 100 == 0)})
        time.sleep(1.0)

    # Official per-day counts — an independent check on what search actually returned.
    counts, counts_err = api.get("/tweets/counts/all",
                                 {"query": SEARCH_QUERY, "start_time": start_time,
                                  "end_time": end_time, "granularity": "day"})
    official = []
    if counts:
        for b in counts.get("data") or []:
            official.append({"date": (b.get("start") or "")[:10], "count": b.get("tweet_count")})

    seen, deduped = set(), []
    for r in all_rows:
        if r.get("id") in seen:
            continue
        seen.add(r["id"])
        pm = r.get("public_metrics") or {}
        au = users.get(r.get("author_id")) or {}
        text = (r.get("note_tweet") or {}).get("text") or r.get("text") or ""
        deduped.append({
            "id": r.get("id"),
            "created_at": r.get("created_at"),
            "author": {"handle": au.get("username"), "name": au.get("name"),
                       "id": r.get("author_id"),
                       "followers_at_capture": (au.get("public_metrics") or {}).get("followers_count")},
            "text": text,
            "url": f"https://x.com/{au.get('username','i')}/status/{r.get('id')}",
            "metrics": {
                "likes": pm.get("like_count"), "retweets": pm.get("retweet_count"),
                "replies": pm.get("reply_count"), "quotes": pm.get("quote_count"),
                "views": pm.get("impression_count"),
                "captured_at": captured, "source": "x_api_v2",
            },
        })

    top = sorted(deduped, key=lambda t: (t["metrics"]["views"] or 0), reverse=True)[:50]
    return {
        "status": "ok",
        "query": SEARCH_QUERY,
        "window": {"start": start_time, "end": end_time},
        "captured_at": captured,
        "unique_tweets": len(deduped),
        "per_day_returned": per_day,
        "per_day_official_counts": official,
        "counts_error": counts_err,
        "truncation_suspected": any(d["possible_truncation"] for d in per_day),
        "days_errored": [d["date"] for d in per_day if d.get("error")],
        "days_complete": [d["date"] for d in per_day if not d.get("error") and not d["possible_truncation"]],
        "coverage_note": (
            "Ranking over a window with errored days is biased toward the days that "
            "did return. Do not publish a leaderboard unless days_errored is empty."
        ),
        "top_by_views": top,
        "tweets": deduped,
    }


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------

def write_json(name: str, payload) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect X/social evidence for toad-wiki")
    ap.add_argument("--no-api", action="store_true", help="skip the X API entirely")
    ap.add_argument("--no-search", action="store_true",
                    help="skip the $TOAD full-archive search (the expensive part: billed "
                         "per post returned). Timelines, key moments and the conversation "
                         "graph still run.")
    ap.add_argument("--refresh", action="store_true",
                    help="keyless metric refresh only (free; no enumeration)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--start", default=CAMPAIGN_START)
    ap.add_argument("--max-sleep", type=int, default=900,
                    help="cap on a single 429 sleep, seconds")
    args = ap.parse_args()

    started = now_iso()
    log(f"toad-wiki social collector  start={started}")
    log(f"output -> {OUT}")

    # ---- STEP 1: probe the token before choosing a path ------------------------------
    token = os.environ.get("X_BEARER_TOKEN")
    api = XAPI(None if (args.no_api or args.refresh) else token, max_sleep=args.max_sleep)
    if args.no_api or args.refresh:
        api.status = "skipped"
        api.detail = "--no-api/--refresh: keyless path forced"
        probe = {"status": api.status, "detail": api.detail, "checked_at": started}
        log(f"[1/5] X API: skipped ({api.detail})")
    else:
        log("[1/5] probing X API...")
        probe = api.probe()
        log(f"      status={api.status} :: {api.detail}")
        for name, r in (probe.get("endpoints") or {}).items():
            log(f"      {name:20s} HTTP {r.get('http')}")

    # ---- key moments (keyless, always works) -----------------------------------------
    log(f"[2/5] hydrating {len(KEY_MOMENTS)} key moments (keyless)...")
    moments = hydrate_many(KEY_MOMENTS, workers=args.workers)
    ok = [m for m in moments if m.get("status") == "ok"]
    notes = [m for m in ok if m.get("is_note_tweet")]
    verified = [m for m in ok if m.get("text_integrity") == "verified_identical"]
    mismatches = [m for m in ok if m.get("text_integrity") == "MISMATCH"]
    log(f"      resolved {len(ok)}/{len(KEY_MOMENTS)}; note tweets {len(notes)}; "
        f"text cross-verified {len(verified)}; mismatches {len(mismatches)}")
    for m in ok:
        flag = " [MINT]" if m.get("mentions_mint") else ""
        mm = " [AUTHOR MISMATCH]" if m.get("author_mismatch") else ""
        log(f"      {m['id']} @{(m.get('author') or {}).get('handle'):<12} "
            f"len={m['text_len']:<5} views={(m.get('metrics') or {}).get('views')}{flag}{mm}")

    write_json("key_moments.json", {
        "generated_at": started,
        "method": "keyless hydration: fxtwitter (text+metrics incl. views), "
                  "vxtwitter (text cross-validation), syndication (entities/media/note flag)",
        "resolved": len(ok),
        "requested": len(KEY_MOMENTS),
        "note_tweets": len(notes),
        "text_cross_verified": len(verified),
        "text_mismatches": len(mismatches),
        "moments": moments,
    })

    # ---- profiles (keyless) -----------------------------------------------------------
    log("[3/5] fetching profiles (keyless)...")
    profiles = [fetch_profile(h) for h in ACCOUNTS]
    for p in profiles:
        log(f"      @{p.get('handle') or '?':<12} followers="
            f"{(p.get('metrics') or {}).get('followers')}")
    write_json("profiles.json", {"generated_at": started, "profiles": profiles})

    # ---- timelines (token required) ---------------------------------------------------
    log("[4/5] timelines...")
    timelines = {h: collect_timeline(api, h, uid, args.start) for h, uid in ACCOUNTS.items()}
    for h, t in timelines.items():
        if t["status"] == "refused":
            log(f"      @{h:<12} REFUSED ({t['reason']})")
        else:
            log(f"      @{h:<12} {t['total_tweets']} tweets, "
                f"{t['toad_relevant_count']} TOAD-relevant")
    write_json("timelines.json", {"generated_at": started, "timelines": timelines})

    # ---- search (token required) ------------------------------------------------------
    log("[5/5] $TOAD full-archive search...")
    if args.no_search:
        log("[5/6] $TOAD search SKIPPED (--no-search)")
        search = {"status": "skipped", "reason": "--no-search", "unique_tweets": 0,
                  "tweets": [], "top_by_views": [], "per_day_returned": [],
                  "days_errored": [], "days_complete": [],
                  "truncation_suspected": False,
                  "window": {"start": args.start, "end": started},
                  "captured_at": started,
                  "note": "Not collected this run. Empty means unsearched, not inactive."}
    else:
        search = collect_search(api, args.start, started)
    if search["status"] == "refused":
        log(f"      REFUSED ({search['reason']})")
    else:
        log(f"      {search['unique_tweets']} unique tweets; "
            f"truncation_suspected={search['truncation_suspected']}")
    write_json("search_toad.json", search)

    # ---- conversation graph -------------------------------------------------
    # Built from what we already fetched: no extra API calls, no extra spend.
    log("[6/6] conversation graph...")
    graph = build_graph(
        {"timelines": timelines},
        {"moments": moments},
        search,
        REF_POOL,
        started,
        USER_POOL,
    )
    write_json("graph.json", graph)
    g = graph["stats"]
    log(f"      {g['node_count']} nodes ({g['resolved_nodes']} resolved), "
        f"{g['edge_count']} edges ({g['dangling_edges']} dangling) :: {g['by_kind']}")

    # ---- manifest ---------------------------------------------------------------------
    unresolved = [m["id"] for m in moments if m.get("status") != "ok"]
    manifest = {
        "generated_at": started,
        "completed_at": now_iso(),
        "x_api": {
            "probe": probe,
            "status": api.status,
            "detail": api.detail,
            "calls_made": api.calls,
            "token_present": bool(token),
        },
        "keyless": {
            "hydration_source": "api.fxtwitter.com",
            "text_validator": "api.vxtwitter.com",
            "entity_source": "cdn.syndication.twimg.com",
            "views_available_keylessly": True,
            "note": "fxtwitter exposes `views`, so impression counts do NOT require paid "
                    "API quota. Engagement can be refreshed forever at zero cost.",
        },
        "collected": {
            "key_moments_requested": len(KEY_MOMENTS),
            "key_moments_resolved": len(ok),
            "key_moments_unresolved": unresolved,
            "text_cross_verified": len(verified),
            "text_mismatches": [m["id"] for m in mismatches],
            "author_mismatches": [
                {"id": m["id"], **m["author_mismatch"]} for m in ok if m.get("author_mismatch")
            ],
            "profiles": len([p for p in profiles if p.get("status") == "ok"]),
        },
        "refused": {
            "timelines": {h: t.get("reason") for h, t in timelines.items()
                          if t["status"] == "refused"},
            "search": search.get("reason") if search["status"] == "refused" else None,
            "impact": None if api.usable else (
                "Enumeration (timeline walks, $TOAD full-archive search, per-day counts, "
                "and discovery of the viral board) is unavailable without a working "
                "bearer token. There is NO keyless substitute for enumeration. "
                "Hydration of known ids — including view counts — is unaffected."
            ),
        },
        "files": sorted(p.name for p in OUT.glob("*.json")),
    }
    write_json("_manifest.json", manifest)

    log("")
    log(f"done. wrote {len(manifest['files'])} files to {OUT}")
    if not api.usable:
        log(f"NOTE: enumeration refused ({api.status}). Key moments + profiles + metrics "
            f"collected keylessly.")
    return 0


# --------------------------------------------------------------------------------------
# Conversation graph
# --------------------------------------------------------------------------------------
#
# The site renders a board of posts and how they connect. That needs explicit
# nodes and edges, not just a conversation_id, because:
#
#   * conversation_id groups a thread but says nothing about WHO replied to WHOM;
#   * a quote-tweet is a different relationship from a reply and must not be
#     collapsed into one;
#   * an edge whose target we never fetched is a dangling edge -- rendering it as
#     a connection would imply we saw a post we never saw.
#
# So every edge is emitted with a `resolved` flag, and the renderer must not draw
# an unresolved edge as if it were verified.

EDGE_KIND = {"replied_to": "reply", "quoted": "quote", "retweeted": "retweet"}


def build_graph(timelines_doc: dict, moments_doc: dict, search_doc: dict,
                ref_pool: dict, captured: str, user_pool: dict | None = None) -> dict:
    user_pool = user_pool or {}
    def handle_of(aid):
        u = user_pool.get(aid) or {}
        return u.get("username")
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def put(tid, *, author=None, handle=None, created=None, text=None,
            metrics=None, source=None, is_moment=False, label=None):
        if not tid:
            return
        n = nodes.setdefault(tid, {
            "id": tid, "author_handle": None, "author_id": None, "created_at": None,
            "text": None, "metrics": None, "sources": [], "is_key_moment": False,
            "label": None, "resolved": False,
        })
        if handle and not n["author_handle"]: n["author_handle"] = handle
        if author and not n["author_id"]: n["author_id"] = author
        if created and not n["created_at"]: n["created_at"] = created
        if text is not None and n["text"] is None: n["text"] = text
        if metrics and not n["metrics"]: n["metrics"] = metrics
        if source and source not in n["sources"]: n["sources"].append(source)
        if is_moment: n["is_key_moment"] = True
        if label and not n["label"]: n["label"] = label
        if n["text"] is not None: n["resolved"] = True
        return n

    # --- nodes from the three timelines -------------------------------------
    for handle, tl in (timelines_doc.get("timelines") or {}).items():
        for r in tl.get("tweets") or []:
            put(r.get("id"), author=r.get("author_id"), handle=handle,
                created=r.get("created_at"), text=r.get("text"),
                metrics=r.get("metrics"), source="timeline")
            for ref in r.get("referenced_tweets") or []:
                edges.append({
                    "from": r.get("id"), "to": ref.get("id"),
                    "kind": EDGE_KIND.get(ref.get("type"), ref.get("type")),
                    "from_handle": handle,
                })

    # --- curated key moments -------------------------------------------------
    for m in (moments_doc.get("moments") or moments_doc.get("tweets") or []):
        put(m.get("id"), handle=(m.get("author") or {}).get("handle"),
            created=m.get("created_at"), text=m.get("text"),
            metrics=m.get("metrics"), source="key_moment",
            is_moment=True, label=m.get("label"))

    # --- anything the referenced_tweets expansion already paid for -----------
    for tid, tw in (ref_pool or {}).items():
        put(tid, author=tw.get("author_id"), handle=handle_of(tw.get("author_id")),
            created=tw.get("created_at"),
            text=(tw.get("note_tweet") or {}).get("text") or tw.get("text"),
            metrics=tw.get("public_metrics"), source="expansion")

    for n in nodes.values():
        if not n["author_handle"] and n["author_id"]:
            n["author_handle"] = handle_of(n["author_id"])

    # --- de-dup edges, mark resolution ---------------------------------------
    seen, out = set(), []
    for e in edges:
        k = (e["from"], e["to"], e["kind"])
        if k in seen or not e["to"] or not e["from"]:
            continue
        seen.add(k)
        e["resolved"] = e["to"] in nodes and nodes[e["to"]]["resolved"]
        e["to_handle"] = (nodes.get(e["to"]) or {}).get("author_handle")
        out.append(e)

    dangling = [e for e in out if not e["resolved"]]
    from collections import Counter
    return {
        "generated_at": captured,
        "nodes": list(nodes.values()),
        "edges": out,
        "stats": {
            "node_count": len(nodes),
            "resolved_nodes": sum(1 for n in nodes.values() if n["resolved"]),
            "edge_count": len(out),
            "resolved_edges": len(out) - len(dangling),
            "dangling_edges": len(dangling),
            "by_kind": dict(Counter(e["kind"] for e in out)),
            "key_moments": sum(1 for n in nodes.values() if n["is_key_moment"]),
        },
        "note": (
            "An edge with resolved=false points at a post we did not fetch. Render it as "
            "an open/unknown endpoint, never as a verified connection. Nodes with "
            "resolved=false are ids only -- we know they were referenced, not what they said."
        ),
    }


if __name__ == "__main__":
    sys.exit(main())


