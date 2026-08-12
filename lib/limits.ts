/**
 * Rate limiting and the daily spend cap.
 *
 * HONEST DESCRIPTION OF WHAT THIS DOES AND DOES NOT DO:
 *
 * State lives in the memory of one serverless instance. Vercel runs several,
 * and recycles them, so the per-IP counter is BEST EFFORT — a cold start resets
 * it, and two instances do not share counts. A determined visitor can get more
 * than their free allowance by waiting out a recycle. That is a deliberate,
 * documented trade: the alternative is a Redis dependency, another account and
 * another credential.
 *
 * The number that actually bounds the bill is the daily cap, which is checked
 * per instance too — but every instance stops at the same ceiling, so the true
 * worst case is (cap x instances) rather than unbounded. Set the cap with that
 * in mind; the default is deliberately conservative.
 *
 * Consequence worth knowing: `spentSignatures` is memory too, so a payment
 * signature could in principle be replayed after a recycle. Cost of that abuse
 * is a handful of Haiku calls, and the daily cap still holds the line.
 */

const DAY_MS = 24 * 60 * 60 * 1000;

type Bucket = { used: number; resetAt: number; unlocked: number };

const ipBuckets = new Map<string, Bucket>();
const spentSignatures = new Map<string, number>();

let dayKey = "";
let dayCount = 0;

/** Keeps the maps from growing without bound on a long-lived instance. */
function sweep(now: number) {
  if (ipBuckets.size > 5000) {
    for (const [k, v] of ipBuckets) if (v.resetAt <= now) ipBuckets.delete(k);
  }
  if (spentSignatures.size > 5000) {
    for (const [k, v] of spentSignatures) if (v <= now) spentSignatures.delete(k);
  }
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

/**
 * The client's IP as seen through Vercel's proxy. `x-forwarded-for` is a list;
 * the ORIGINAL client is the first entry — later ones are proxies, and reading
 * the last would bucket every visitor into one.
 */
export function clientIp(headers: Headers): string {
  const xff = headers.get("x-forwarded-for");
  if (xff) {
    const first = xff.split(",")[0]?.trim();
    if (first) return first;
  }
  return headers.get("x-real-ip")?.trim() || "unknown";
}

export function dailyCapReached(cap: number): boolean {
  const k = today();
  if (k !== dayKey) {
    dayKey = k;
    dayCount = 0;
  }
  return dayCount >= cap;
}

export function recordSpend() {
  const k = today();
  if (k !== dayKey) {
    dayKey = k;
    dayCount = 0;
  }
  dayCount += 1;
}

export function dailyRemaining(cap: number): number {
  if (today() !== dayKey) return cap;
  return Math.max(0, cap - dayCount);
}

/** Reads the caller's allowance without consuming any of it. */
export function checkAllowance(ip: string, free: number) {
  const now = Date.now();
  sweep(now);
  const b = ipBuckets.get(ip);
  if (!b || b.resetAt <= now) return { used: 0, allowed: free, remaining: free };
  const allowed = free + b.unlocked;
  return { used: b.used, allowed, remaining: Math.max(0, allowed - b.used) };
}

/** Consumes one question. Returns false when the caller has none left. */
export function consume(ip: string, free: number): boolean {
  const now = Date.now();
  let b = ipBuckets.get(ip);
  if (!b || b.resetAt <= now) {
    b = { used: 0, resetAt: now + DAY_MS, unlocked: 0 };
    ipBuckets.set(ip, b);
  }
  if (b.used >= free + b.unlocked) return false;
  b.used += 1;
  return true;
}

/**
 * Gives a consumed question back. Called whenever the answer never arrived —
 * a rejected API key, a timeout, an empty response. Charging someone their one
 * free question of the day for the site's own outage is indefensible.
 */
export function refund(ip: string) {
  const b = ipBuckets.get(ip);
  if (b && b.resetAt > Date.now() && b.used > 0) b.used -= 1;
}

/** Grants extra questions to an IP after a verified payment. */
export function grantUnlock(ip: string, questions: number) {
  const now = Date.now();
  let b = ipBuckets.get(ip);
  if (!b || b.resetAt <= now) {
    b = { used: 0, resetAt: now + DAY_MS, unlocked: 0 };
    ipBuckets.set(ip, b);
  }
  b.unlocked += questions;
}

/**
 * One payment, one unlock. Returns false if this signature was already redeemed
 * on this instance.
 */
export function claimSignature(sig: string): boolean {
  const now = Date.now();
  const seen = spentSignatures.get(sig);
  if (seen && seen > now) return false;
  spentSignatures.set(sig, now + 7 * DAY_MS);
  return true;
}
