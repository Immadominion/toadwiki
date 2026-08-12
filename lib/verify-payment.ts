import { MINT } from "@/lib/constants";
import { HELIUS_RPC } from "@/lib/ask-config";

/**
 * Verifies that a transaction really moved TOAD to the tip wallet.
 *
 * The balance delta is read from pre/postTokenBalances rather than by parsing
 * instructions — the same method the ledger itself uses, and the reason it is
 * independent of how the sender's wallet constructed the transfer.
 *
 * KNOWN LIMIT, stated rather than hidden: a signature is public the moment it
 * lands, so someone watching the tip wallet could copy a stranger's signature
 * and redeem it first. Two things keep that pointless rather than impossible —
 * a signature can only be redeemed once, and it must be under RECENCY_MS old.
 * The prize is a handful of Haiku questions, and the daily cap bounds the loss
 * either way. Closing it properly needs a per-visitor nonce in a memo, which is
 * a worse trade for the amount of money involved.
 */

const RECENCY_MS = 2 * 60 * 60 * 1000;
const BASE58 = /^[1-9A-HJ-NP-Za-km-z]{64,90}$/;

export type PaymentResult =
  | { ok: true; amount: number; from: string | null; sig: string }
  | { ok: false; reason: string };

export async function verifyToadPayment(
  sig: string,
  toWallet: string,
  minWholeToad: number
): Promise<PaymentResult> {
  if (!HELIUS_RPC) return { ok: false, reason: "Payment checking is not configured on this site." };
  if (!BASE58.test(sig)) return { ok: false, reason: "That does not look like a Solana transaction signature." };

  let body: {
    result?: {
      blockTime?: number | null;
      meta?: {
        err?: unknown;
        preTokenBalances?: TokenBalance[];
        postTokenBalances?: TokenBalance[];
      } | null;
    } | null;
    error?: { message?: string };
  };

  try {
    const res = await fetch(HELIUS_RPC, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "getTransaction",
        params: [sig, { encoding: "jsonParsed", maxSupportedTransactionVersion: 0, commitment: "confirmed" }],
      }),
      signal: AbortSignal.timeout(12_000),
    });
    if (!res.ok) return { ok: false, reason: "Could not reach Solana to check that transaction. Try again shortly." };
    body = await res.json();
  } catch {
    return { ok: false, reason: "Could not reach Solana to check that transaction. Try again shortly." };
  }

  if (body.error) return { ok: false, reason: "Solana rejected that signature." };
  const tx = body.result;
  if (!tx) return { ok: false, reason: "No transaction with that signature has confirmed yet." };
  if (tx.meta?.err) return { ok: false, reason: "That transaction failed on-chain, so nothing moved." };

  const when = (tx.blockTime ?? 0) * 1000;
  if (!when) return { ok: false, reason: "That transaction has no block time yet. Wait for it to confirm." };
  if (Date.now() - when > RECENCY_MS) {
    return { ok: false, reason: "That transaction is more than 2 hours old. Send a fresh one." };
  }

  // Delta for the destination owner, in integer base units.
  const pre = index(tx.meta?.preTokenBalances ?? [], toWallet);
  const post = index(tx.meta?.postTokenBalances ?? [], toWallet);
  // BigInt(0) rather than the 0n literal: the literal needs an ES2020 target and
  // Next re-normalises tsconfig on build, so the constructor form is the one that
  // survives. Base units stay exact integers either way.
  const ZERO = BigInt(0);
  let delta = ZERO;
  for (const [acct, after] of post) delta += after - (pre.get(acct) ?? ZERO);
  if (delta <= ZERO) {
    return { ok: false, reason: `That transaction did not send $TOAD to ${short(toWallet)}.` };
  }

  const decimals = firstDecimals(tx.meta?.postTokenBalances ?? []) ?? 6;
  const whole = Number(delta) / 10 ** decimals;
  if (whole + 1e-9 < minWholeToad) {
    return {
      ok: false,
      reason: `That transaction sent ${fmt(whole)} $TOAD. This needs at least ${fmt(minWholeToad)}.`,
    };
  }

  return { ok: true, amount: whole, from: senderOf(tx.meta?.preTokenBalances ?? [], toWallet), sig };
}

type TokenBalance = {
  accountIndex: number;
  mint: string;
  owner?: string;
  uiTokenAmount: { amount: string; decimals: number };
};

function index(rows: TokenBalance[], owner: string) {
  const m = new Map<number, bigint>();
  for (const r of rows) {
    if (r.mint !== MINT || r.owner !== owner) continue;
    m.set(r.accountIndex, BigInt(r.uiTokenAmount.amount));
  }
  return m;
}

function firstDecimals(rows: TokenBalance[]) {
  for (const r of rows) if (r.mint === MINT) return r.uiTokenAmount.decimals;
  return null;
}

/** Best-effort: the TOAD holder in the transaction that is not the recipient. */
function senderOf(pre: TokenBalance[], toWallet: string) {
  for (const r of pre) {
    if (r.mint === MINT && r.owner && r.owner !== toWallet) return r.owner;
  }
  return null;
}

const short = (a: string) => `${a.slice(0, 4)}…${a.slice(-4)}`;
// A 0.1 TOAD transfer rendered as "sent 0 $TOAD", which reads like the check
// is broken rather than like the amount was too small.
const fmt = (v: number) =>
  v.toLocaleString("en-US", { maximumFractionDigits: v < 100 ? 2 : 0 });
