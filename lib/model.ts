import fs from "fs";
import path from "path";

export type Tx = {
  sig: string;
  ts: number;
  ts_iso: string;
  amount_raw: string;
  amount: number;
  price_usd: number | null;
  /** Which minute candle supplied the price: 0 = exact minute, -1/+1/-2 = probe. */
  price_minute_offset: number | null;
  usd: number | null;
  usd_display: string | null;
  /** True only with evidence: a pool destination or a DEX program in the tx. */
  is_sale: boolean;
};

/**
 * Recipient disposition. Note there is no bare "sold": moving tokens out is not
 * a sale unless a pool or DEX program is involved, and ~a fifth of outbound
 * recipient volume is plain wallet-to-wallet. "account_closed" is deliberately
 * distinct from "zero_balance" — they are different events.
 */
export type Status = "holding" | "partial" | "zero_balance" | "account_closed";

export type Recipient = {
  rank: number;
  wallet: string;
  known_label: string | null;
  identity: { type: string; name: string | null; twitter: string | null; url: string } | null;
  total_raw: string;
  total: number;
  usd_at_drop: number | null;
  usd_at_drop_display: string | null;
  tx_count: number;
  txs: Tx[];
  first_ts: number;
  last_ts: number;
  balance_raw: string;
  balance_now: number | null;
  /** null when unknown — render "--", never a computed-looking 0. */
  held_pct: number | null;
  moved_out: number;
  status: Status;
  disposition: string;
  sold_confirmed: boolean | null;
  sale_evidence: string[];
  balance_checked_via: string | null;
};

export type Tweet = {
  id: string;
  url: string;
  author: { name: string; handle: string; avatar: string | null };
  date: string;
  text: string;
  likes: number | null;
  replies: number | null;
  photo: string | null;
  quoted: { handle: string; text: string } | null;
};

export type ArchiveTweet = {
  id: string;
  date: string;
  text: string;
  likes: number;
  rts: number;
  replies: number;
  is_reply: boolean;
  reply_to: string | null;
  photo: string | null;
  quoted_text: string | null;
};

export type ViralTweet = {
  id: string;
  url: string;
  author: { name: string; handle: string; avatar: string | null } | null;
  date: string;
  text: string;
  views: number;
  likes: number;
  rts: number;
  replies: number;
};

export type TimelineEntry = {
  date: string;
  event: string;
  source?: string;
  sources?: string[];
  /** lore = uncited history; attested = a party asserts it; onchain/social = verifiable */
  kind?: "lore" | "social" | "onchain" | "attested";
  verified?: boolean;
  onchain?: boolean;
  sig?: string;
  tweet_id?: string;
  caveat?: string;
};

export type Copycat = {
  mint: string;
  name: string;
  symbol: string;
  severity: string;
  liquidity_usd: number | null;
  created_at: string | null;
  url: string;
};

/** The closing balance check. residual must be exactly "0" or the build fails. */
export type Reconciliation = {
  headline: string;
  formula: string;
  received: number;
  received_tx_count: number;
  distributed: number;
  distributed_transfer_count: number;
  distributed_recipient_count: number;
  held: number;
  held_source: string;
  residual_raw: string;
  residual: number;
  holds: boolean;
  checked_at_slot: number;
  verified_live_at_build: boolean;
  stale: boolean;
  sells_detected: number;
  sell_detection_method: string;
};

export type BoardNode = {
  id: string;
  author_handle: string | null;
  author_id: string | null;
  created_at: string | null;
  text: string | null;
  metrics: { likes?: number | null; retweets?: number | null; replies?: number | null; views?: number | null; captured_at?: string } | null;
  sources: string[];
  is_key_moment: boolean;
  label: string | null;
  /** false = referenced by a post we hold, but never fetched. Render as an open
   *  endpoint — we know it exists, not what it said. */
  resolved: boolean;
  toad_relevant: boolean;
};

export type BoardEdge = {
  from: string;
  to: string;
  /** X's own referenced_tweets relationship, not inferred from text. */
  kind: "reply" | "quote" | "retweet";
  from_handle: string | null;
  to_handle: string | null;
  resolved: boolean;
};

export type ConversationBoard = {
  nodes: BoardNode[];
  edges: BoardEdge[];
  stats: Record<string, any>;
  method: string;
  caveat: string;
  generated_at: string | null;
};

export type Model = {
  schema_version?: string;
  generated_at: string;
  token: Record<string, any>;
  copycats: Copycat[];
  copycats_caveat?: string;
  market_snapshot: Record<string, any>;
  market: Record<string, any>;
  holders: Record<string, any>;
  reconciliation: Reconciliation;
  wallet_proof: Record<string, any>;
  deployer_conduct: Record<string, any>;
  verification_ledger?: any[];
  caveats?: string[];
  face: Record<string, any>;
  airdrop_wallet: string | null;
  campaign_ata?: string;
  deployer?: string;
  mint: string;
  stats: Record<string, any>;
  airdrop_daily: { date: string; count: number; amount: number; usd: number; usd_display?: string }[];
  price_series: [number, number][];
  recipients: Recipient[];
  timeline: TimelineEntry[];
  tweets: Tweet[];
  conversation_board?: ConversationBoard;
  archive: ArchiveTweet[];
  viral: ViralTweet[];
  viral_status?: Record<string, any>;
  quotes: { text: string; who: string; context?: string; source?: string }[];
  open_questions: string[];
  sources: string[];
  provenance?: Record<string, any>;
};

export function loadModel(): Model {
  const p = path.join(process.cwd(), "data", "model.json");
  const raw = fs.readFileSync(p, "utf8");
  return JSON.parse(raw) as Model;
}
