import crypto from "crypto";

/**
 * Question limits and the spend cap.
 *
 * This file was rewritten after the first version failed in the field. It kept
 * counts in one serverless instance's memory, which means two questions asked
 * seconds apart can land on two instances and each get a free answer. The daily
 * cap had the same hole: "200 a day" was really 200 per warm instance. On a site
 * whose whole reason for limiting is that the owner pays per answer, that is not
 * a limit, it is a suggestion.
 *
 * Three layers now, checked cheapest first:
 *
 *   1. COOKIE  — signed, HttpOnly, per browser. Travels with the request, so it
 *                works no matter which instance answers, with zero setup. Clearing
 *                cookies defeats it, which is what the layers below are for.
 *   2. REDIS   — exact and shared across every instance and region, including the
 *                global daily cap. Only layer that makes the cap a real ceiling.
 *                Enabled by adding the free Upstash integration on Vercel.
 *   3. MEMORY  — last resort when Redis is not configured. Per instance, leaky,
 *                and the reason layer 1 exists.
 */

const DAY_MS = 24 * 60 * 60 * 1000;
const DAY_S = 24 * 60 * 60;

export const COOKIE = "tw_ask";

/* ------------------------------------------------------------------ redis */

const REDIS_URL =
  process.env.KV_REST_API_URL?.trim() || process.env.UPSTASH_REDIS_REST_URL?.trim() || null;
const REDIS_TOKEN =
  process.env.KV_REST_API_TOKEN?.trim() || process.env.UPSTASH_REDIS_REST_TOKEN?.trim() || null;

export const redisEnabled = Boolean(REDIS_URL && REDIS_TOKEN);

async function pipeline(cmds: (string | number)[][]): Promise<(number | string | null)[] | null> {
  if (!redisEnabled) return null;
  try {
    const res = await fetch(`${REDIS_URL}/pipeline`, {
      method: "POST",
      headers: { authorization: `Bearer ${REDIS_TOKEN}`, "content-type": "application/json" },
      body: JSON.stringify(cmds),
      signal: AbortSignal.timeout(4000),
      cache: "no-store",
    });
    if (!res.ok) return null;
    const rows = (await res.json()) as { result?: number | string | null; error?: string }[];
    return rows.map((r) => (r?.error ? null : (r?.result ?? null)));
  } catch {
    // A limiter that throws must never take the whole endpoint down with it.
    return null;
  }
}

/** INCR then set a TTL only if the key is new. Returns the new value. */
async function bump(key: string, ttl: number): Promise<number | null> {
  const out = await pipeline([
    ["INCR", key],
    ["EXPIRE", key, ttl, "NX"],
  ]);
  const v = out?.[0];
  return typeof v === "number" ? v : null;
}

async function readInt(key: string): Promise<number | null> {
  const out = await pipeline([["GET", key]]);
  const v = out?.[0];
  if (v === null || v === undefined) return 0;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

/* ----------------------------------------------------------------- memory */

type Bucket = { used: number; resetAt: number; unlocked: number };
const ipBuckets = new Map<string, Bucket>();
const spentSignatures = new Map<string, number>();
let memDayKey = "";
let memDayCount = 0;

function sweep(now: number) {
  if (ipBuckets.size > 5000) for (const [k, v] of ipBuckets) if (v.resetAt <= now) ipBuckets.delete(k);
  if (spentSignatures.size > 5000) for (const [k, v] of spentSignatures) if (v <= now) spentSignatures.delete(k);
}

/* ------------------------------------------------------------------ shared */

export function today() {
  return new Date().toISOString().slice(0, 10);
}

/**
 * The client's IP through Vercel's proxy. `x-forwarded-for` is a list and the
 * ORIGINAL client is the first entry; reading the last would bucket every
 * visitor behind the same proxy into one.
 */
export function clientIp(headers: Headers): string {
  const xff = headers.get("x-forwarded-for");
  if (xff) {
    const first = xff.split(",")[0]?.trim();
    if (first) return first;
  }
  return headers.get("x-real-ip")?.trim() || "unknown";
}

/* ------------------------------------------------------------------ cookie */

/**
 * Signed so a visitor cannot simply edit the count to zero. The secret is
 * whatever is configured, falling back to a hash of the API key, which is always
 * present when the feature is live and never leaves the server.
 */
function secret() {
  return (
    process.env.ASK_COOKIE_SECRET?.trim() ||
    process.env.ANTHROPIC_API_KEY?.trim() ||
    "toadwiki-fallback-secret"
  );
}

function sign(payload: string) {
  return crypto.createHmac("sha256", secret()).update(payload).digest("base64url").slice(0, 16);
}

export type Visitor = { day: string; used: number; unlocked: number };

export function readCookie(header: string | null): Visitor {
  const fresh: Visitor = { day: today(), used: 0, unlocked: 0 };
  if (!header) return fresh;
  const raw = header
    .split(";")
    .map((c) => c.trim())
    .find((c) => c.startsWith(`${COOKIE}=`))
    ?.slice(COOKIE.length + 1);
  if (!raw) return fresh;

  const [payload, mac] = decodeURIComponent(raw).split("~");
  if (!payload || !mac || sign(payload) !== mac) return fresh;

  const [day, used, unlocked] = payload.split(".");
  if (day !== today()) return fresh; // yesterday's allowance is not today's
  return {
    day,
    used: Math.max(0, Number(used) || 0),
    unlocked: Math.max(0, Number(unlocked) || 0),
  };
}

export function writeCookie(v: Visitor): string {
  const payload = `${v.day}.${v.used}.${v.unlocked}`;
  const value = encodeURIComponent(`${payload}~${sign(payload)}`);
  return `${COOKIE}=${value}; Path=/; Max-Age=${DAY_S}; HttpOnly; SameSite=Lax; Secure`;
}

/* ------------------------------------------------------- allowance + cap */

/**
 * Has this visitor used everything they are entitled to?
 * Cookie first because it costs nothing and catches the common case; then the
 * shared IP counter, which catches a cleared cookie.
 */
export async function isSpent(ip: string, v: Visitor, free: number): Promise<boolean> {
  if (v.used >= free + v.unlocked) return true;

  if (redisEnabled) {
    const used = (await readInt(`ask:ip:${today()}:${ip}`)) ?? 0;
    const unlocked = (await readInt(`ask:unlock:${today()}:${ip}`)) ?? 0;
    if (used >= free + unlocked + v.unlocked) return true;
    return false;
  }

  const now = Date.now();
  sweep(now);
  const b = ipBuckets.get(ip);
  if (!b || b.resetAt <= now) return false;
  return b.used >= free + b.unlocked + v.unlocked;
}

/** Consumes one question from the shared counters. Cookie is bumped by the caller. */
export async function consume(ip: string): Promise<void> {
  if (redisEnabled) {
    await bump(`ask:ip:${today()}:${ip}`, DAY_S);
    return;
  }
  const now = Date.now();
  let b = ipBuckets.get(ip);
  if (!b || b.resetAt <= now) {
    b = { used: 0, resetAt: now + DAY_MS, unlocked: 0 };
    ipBuckets.set(ip, b);
  }
  b.used += 1;
}

/** Hands a question back when no answer was ever produced. */
export async function refund(ip: string): Promise<void> {
  if (redisEnabled) {
    await pipeline([["DECR", `ask:ip:${today()}:${ip}`]]);
    return;
  }
  const b = ipBuckets.get(ip);
  if (b && b.resetAt > Date.now() && b.used > 0) b.used -= 1;
}

export async function grantUnlock(ip: string, questions: number): Promise<void> {
  if (redisEnabled) {
    await pipeline([
      ["INCRBY", `ask:unlock:${today()}:${ip}`, questions],
      ["EXPIRE", `ask:unlock:${today()}:${ip}`, DAY_S, "NX"],
    ]);
    return;
  }
  const now = Date.now();
  let b = ipBuckets.get(ip);
  if (!b || b.resetAt <= now) {
    b = { used: 0, resetAt: now + DAY_MS, unlocked: 0 };
    ipBuckets.set(ip, b);
  }
  b.unlocked += questions;
}

/**
 * The global spend ceiling. Only a real ceiling with Redis; without it this is
 * per instance, which is exactly the hole that made the first version useless.
 */
export async function dailyCapReached(cap: number): Promise<boolean> {
  if (redisEnabled) return ((await readInt(`ask:day:${today()}`)) ?? 0) >= cap;
  if (today() !== memDayKey) {
    memDayKey = today();
    memDayCount = 0;
  }
  return memDayCount >= cap;
}

export async function recordSpend(): Promise<void> {
  if (redisEnabled) {
    await bump(`ask:day:${today()}`, DAY_S);
    return;
  }
  if (today() !== memDayKey) {
    memDayKey = today();
    memDayCount = 0;
  }
  memDayCount += 1;
}

export async function dailyRemaining(cap: number): Promise<number> {
  if (redisEnabled) return Math.max(0, cap - ((await readInt(`ask:day:${today()}`)) ?? 0));
  if (today() !== memDayKey) return cap;
  return Math.max(0, cap - memDayCount);
}

/** One payment, one unlock. */
export async function claimSignature(sig: string): Promise<boolean> {
  if (redisEnabled) {
    const out = await pipeline([
      ["SET", `ask:sig:${sig}`, "1", "NX", "EX", 7 * DAY_S],
    ]);
    // SET..NX returns null when the key already existed.
    if (out) return out[0] !== null;
    // Redis unreachable: fall through to memory rather than blocking a payer.
  }
  const now = Date.now();
  const seen = spentSignatures.get(sig);
  if (seen && seen > now) return false;
  spentSignatures.set(sig, now + 7 * DAY_MS);
  return true;
}
