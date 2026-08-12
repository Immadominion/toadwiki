/**
 * Server-only configuration for the ask + suggest endpoints.
 *
 * Everything here is read from the environment so no key, wallet or token ever
 * reaches the client bundle. Every feature FAILS CLOSED: if a required variable
 * is missing the endpoint reports "not configured" rather than half-working, and
 * the UI degrades to a static notice instead of taking someone's money for a
 * feature that cannot run.
 */

/** Haiku only — the cheapest model that can hold this brief and stay accurate. */
export const ASK_MODEL = "claude-haiku-4-5-20251001";

/** Hard ceiling on the answer. Also the main lever on cost per question. */
export const MAX_ANSWER_TOKENS = 420;

/** Longest question we will pay to process. */
export const MAX_QUESTION_CHARS = 400;
export const MAX_SUGGESTION_CHARS = 1200;

const int = (v: string | undefined, fallback: number) => {
  const n = Number.parseInt(v ?? "", 10);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
};

/**
 * The real spend guard. Per-IP limiting is best-effort on serverless (see
 * lib/limits.ts); THIS is the number that bounds the bill. At roughly $0.008 a
 * question uncached, 200/day caps exposure near $1.60/day.
 */
export const DAILY_QUESTION_CAP = int(process.env.DAILY_QUESTION_CAP, 200);

/** Free questions per IP per day before payment is required. */
export const FREE_QUESTIONS_PER_IP = int(process.env.FREE_QUESTIONS_PER_IP, 1);

/** Questions unlocked by one verified payment. */
export const QUESTIONS_PER_UNLOCK = int(process.env.QUESTIONS_PER_UNLOCK, 10);

/** Whole TOAD required to unlock more questions / to file a suggestion. */
export const ASK_UNLOCK_TOAD = int(process.env.ASK_UNLOCK_TOAD, 25_000);
export const SUGGEST_TOAD = int(process.env.SUGGEST_TOAD, 50_000);

/** Where supporters send TOAD. Unset = every paid path is disabled. */
export const TIP_WALLET = process.env.TIP_WALLET?.trim() || null;
export const TIP_HANDLE = process.env.TIP_HANDLE?.trim() || "heisjoel0x";

export const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY?.trim() || null;
export const HELIUS_RPC = process.env.HELIUS_RPC?.trim() || null;

export const GITHUB_TOKEN = process.env.GITHUB_TOKEN?.trim() || null;
export const GITHUB_REPO = process.env.GITHUB_REPO?.trim() || "Immadominion/toadwiki";

/** Public config the client is allowed to know, so the UI can explain itself. */
export function publicAskConfig() {
  return {
    askEnabled: Boolean(ANTHROPIC_API_KEY),
    payEnabled: Boolean(TIP_WALLET && HELIUS_RPC),
    suggestEnabled: Boolean(TIP_WALLET && HELIUS_RPC && GITHUB_TOKEN),
    tipWallet: TIP_WALLET,
    tipHandle: TIP_HANDLE,
    freePerIp: FREE_QUESTIONS_PER_IP,
    perUnlock: QUESTIONS_PER_UNLOCK,
    unlockToad: ASK_UNLOCK_TOAD,
    suggestToad: SUGGEST_TOAD,
  };
}
