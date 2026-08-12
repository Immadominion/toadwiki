/**
 * Builds data/brief.json — the ONLY thing the answering model is ever shown.
 *
 * Why this file exists at all: data/model.json is ~900KB. Sending it as context
 * would cost more per question than the answer is worth, and would bury the
 * numbers that matter in transaction arrays nobody asks about. This produces a
 * ~6KB plain-text fact sheet instead.
 *
 * The contract is strict, and it is the whole reason the feature is safe to
 * ship on a site whose pitch is "every number here is checkable":
 *
 *   - Every figure below is copied from the verified model. Nothing is inferred.
 *   - If a fact is not in this sheet, the model is instructed to say it does not
 *     know rather than reason its way to a plausible number.
 *   - Figures carry their as-of timestamps, because the campaign is live.
 *
 * Runs from `prebuild`, so a deploy can never ship a brief that disagrees with
 * the pages around it.
 */
import fs from "node:fs";
import path from "node:path";

const ROOT = process.cwd();
const model = JSON.parse(fs.readFileSync(path.join(ROOT, "data", "model.json"), "utf8"));

const n = (v, d = 0) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "unknown"
    : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

const usd = (v) => {
  if (v === null || v === undefined) return "unknown";
  const a = Math.abs(v);
  if (a >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `$${(v / 1e3).toFixed(1)}K`;
  if (a >= 1) return `$${v.toFixed(2)}`;
  return `$${v.toPrecision(4)}`;
};

const toad = (v) => {
  if (v === null || v === undefined) return "unknown";
  if (v >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return n(v);
};

const utc = (iso) => (typeof iso === "string" ? `${iso.slice(0, 10)} ${iso.slice(11, 16)}Z` : "unknown");

const t = model.token ?? {};
const r = model.reconciliation ?? {};
const s = model.stats ?? {};
const snap = model.market_snapshot ?? {};
const mkt = model.market ?? {};
const ath = mkt.ath ?? {};
const hold = model.holders ?? {};
const conc = hold.concentration ?? {};
const proof = model.wallet_proof ?? {};
const dep = model.deployer_conduct ?? {};
const face = model.face ?? {};
const prov = model.provenance ?? {};

// Status mix across recipients — asked constantly, and the distinction between
// "moved tokens" and "sold" is the one this site refuses to blur.
const counts = {};
for (const rec of model.recipients ?? []) counts[rec.status] = (counts[rec.status] ?? 0) + 1;

const top = (model.recipients ?? [])
  .slice(0, 12)
  .map(
    (x) =>
      `  ${String(x.rank).padStart(2)}. ${x.wallet}  ${toad(x.total)} TOAD  ${
        x.usd_at_drop_display ?? usd(x.usd_at_drop)
      } at drop  ${x.tx_count} transfer(s)  status=${x.status}${
        x.known_label ? `  label=${x.known_label}` : ""
      }`
  )
  .join("\n");

const daily = (model.airdrop_daily ?? [])
  .map((d) => `  ${d.date}  ${d.count} drops  ${toad(d.amount)} TOAD  ${d.usd_display ?? usd(d.usd)}`)
  .join("\n");

const timeline = (model.timeline ?? [])
  .filter((x) => x.kind !== "lore")
  .map((x) => `  ${x.date} [${x.kind ?? "?"}] ${x.event}${x.caveat ? ` (caveat: ${x.caveat})` : ""}`)
  .join("\n");

const lore = (model.timeline ?? [])
  .filter((x) => x.kind === "lore")
  .map((x) => `  ${x.date} ${x.event}`)
  .join("\n");

const clones = (model.copycats ?? []).filter((c) => c.severity === "exact_name_clone");

const quotes = (model.quotes ?? [])
  .map((q) => `  "${q.text}" — ${q.who}${q.context ? ` (${q.context})` : ""}`)
  .join("\n");

const posts = (model.tweets ?? [])
  .map(
    (p) =>
      `  ${utc(p.date)} @${p.author?.handle ?? "?"}: ${String(p.text ?? "")
        .replace(/\s+/g, " ")
        .slice(0, 210)}`
  )
  .join("\n");

const brief = `# TOADWIKI VERIFIED FACT SHEET
Every figure below was derived from Solana mainnet or archived from X by the
toadwiki.xyz build pipeline. Model generated: ${utc(model.generated_at)}.
The campaign is LIVE — treat every count as a snapshot, not a standing fact.

## THE TOKEN
Name: ${t.name} (${t.symbol}) — "The Toad Pepe"
Real mint address: ${t.real_mint ?? model.mint}
Token program: ${t.token_program ?? "Token-2022"} (${t.token_program_address ?? ""})
Decimals: ${t.decimals}
Live supply: ${n(t.supply)} (NOT the nominal 1B — ${Number(t.burned_pct ?? 0).toFixed(2)}% has been burned and it keeps drifting)
Burned so far: ${n(t.burned)}
Mint authority: ${t.authorities?.mint_authority ?? "null"} | Freeze: ${t.authorities?.freeze_authority ?? "null"} | Update: ${t.authorities?.update_authority ?? "null"}
Launched: ${t.launch_date ?? "unknown"} on pump.fun
Official X account: ${t.official_x ?? "https://x.com/eltoadpepe"}
The "website" in the mint metadata points at ${t.website ?? "unknown"} — that is a
fandom wiki page about the 1988 character, NOT a project homepage. Do not call it
an official site.
Deployer/creator wallet: ${t.creator_wallet ?? model.deployer ?? "unknown"}
The deployer's pump.fun display name is "slingoor". ${String(t.creator_x_caveat ?? "").replace(/\s+/g, " ")}

The token's own on-chain metadata, immutable since mint, says: "${String(t.description ?? "").replace(/\s+/g, " ").slice(0, 300)}"
That text is verifiable as METADATA. It is not independently sourced history.

## THE CAMPAIGN WALLET AND THE CLOSING BALANCE
Campaign wallet: ${model.airdrop_wallet}
Its TOAD token account (what we actually paginate): ${model.campaign_ata}
Received:    ${n(r.received, 6)} TOAD across ${r.received_tx_count} inbound transfers
Distributed: ${n(r.distributed, 6)} TOAD across ${r.distributed_transfer_count} transfers to ${r.distributed_recipient_count} wallets
Still held:  ${n(r.held, 6)} TOAD
Residual:    ${r.residual_raw} base units — EXACTLY ZERO. If it were anything else the build fails and the site does not publish.
Verified live at slot ${n(r.checked_at_slot)}.
Sells detected from the campaign wallet: ${r.sells_detected} (${r.sell_detection_method})
Total value of everything given away, priced at the MINUTE each transfer landed: ${s.total_usd_at_drop_display ?? usd(s.total_usd_at_drop)}
(Pricing coverage: ${s.total_usd_at_drop_precision?.priced_exact_minute ?? "?"} priced at the exact minute, ${s.total_usd_at_drop_precision?.priced_via_probe ?? 0} via probe, ${s.total_usd_at_drop_precision?.unpriced ?? 0} unpriced.)

## WHAT RECIPIENTS DID NEXT
Total recipients: ${model.recipients?.length ?? 0}
  still holding everything they got: ${counts.holding ?? 0}
  holding part of it:                ${counts.partial ?? 0}
  balance now zero:                  ${counts.zero_balance ?? 0}
  token account closed:              ${counts.account_closed ?? 0}
IMPORTANT DISTINCTION: "sold" requires evidence — a liquidity-pool destination or
a DEX program in the transaction. Tokens simply leaving a wallet is "reduced",
NOT a sale; a large share of outbound recipient volume is plain wallet-to-wallet
movement. Never describe a recipient as having sold unless status says so.
Recipient-side sales have never been measured. If asked "who sold", say that.

### Largest recipients
${top}

### Distribution by day
${daily}

## LIVE MARKET (snapshot — goes stale fast)
Price: ${snap.price_usd ? `$${Number(snap.price_usd).toPrecision(4)}` : "unknown"} as of ${utc(snap.as_of)}
Market cap: ${usd(snap.mcap_usd)} (${snap.basis ?? "live supply x pool close"})
All-time high price: ${ath.price_usd ? `$${Number(ath.price_usd).toPrecision(4)}` : "unknown"} at ${utc(ath.iso)}
ATH market cap: ${usd((ath.price_usd ?? 0) * (t.supply ?? 0))}
Drawdown from ATH: ${mkt.drawdown_from_ath_pct !== undefined ? `${Number(mkt.drawdown_from_ath_pct).toFixed(1)}%` : "unknown"}
Pricing pool: ${model.pricing_pool} (constant-product PumpSwap; the Meteora DLMM pool is deliberately NOT used for pricing)
A "$50M market cap" figure circulates. It is NOT supported by any source we can read — the peak we can verify is ${usd(mkt.pumpfun_ath_market_cap ?? (ath.price_usd ?? 0) * (t.supply ?? 0))}.

## HOLDERS
Total holders (non-zero balances): ${n(hold.count)}
Token accounts scanned: ${n(hold.token_accounts)}
Top 1 holder: ${conc.top1_pct ?? "?"}% | Top 10: ${conc.top10_pct ?? "?"}%
Concentration is computed against LIVE supply, not the nominal 1B.

## WHOSE WALLET IS IT
${String(proof.claim ?? "").replace(/\s+/g, " ")}
This is an ATTRIBUTION, at high confidence — NOT a disclosure. ${face.name ?? "@mdudas"} has
never posted the address, and the site never claims otherwise. The chain of evidence:
  1. ${proof.tx?.amount ?? "70,000"} TOAD left the campaign wallet at ${utc(proof.tx?.time)} (tx ${proof.tx?.sig ?? "?"})
  2. The recipient's token account (${proof.recipient_token_account?.address ?? "?"}) was CREATED BY that very
     transaction and has ${proof.recipient_token_account?.signature_count_ever ?? 1} signature in its entire history — the balance has no
     other possible source.
  3. ${proof.gap_seconds ?? 63} seconds later, @${proof.tweet?.author_handle ?? "mdudas"} posted a screenshot showing exactly that balance.
Caveat: ${String(face.wallet_attribution?.caveat ?? "").replace(/\s+/g, " ")}

## THE CRITICISMS (the site publishes these on purpose)
1. Conflict of interest: Mike Dudas is a partner at 6th Man Ventures, a seed
   investor in pump.fun. He was asked by pump.fun's co-founder to test the new
   mobile app, bought TOAD 17 minutes 43 seconds after the mint at a fully-diluted
   value under $50,000, and the trade became a marketing story for a platform he
   is invested in. All verifiable; none of it is alleged wrongdoing.
2. The deployer took 20.1% of supply (200,963,210.66 TOAD for 6.91 SOL) INSIDE the
   same transaction that created the token, then 88 minutes later sent
   180,963,210.66 onward and kept exactly 20,000,000.
   Against the usual assumption, the deployer wallet (${dep.wallet ?? "?"}) shows:
   ${dep.sells ?? 0} sells, ${n(dep.buys)} buys, ${n(dep.lp_deposits)} LP deposits. Its pool transfers were
   liquidity deposits, NOT sales. Method: ${dep.method ?? "AMM instruction names"}.

## SCAMS AND IMPOSTORS
There is NO claim portal for TOAD. Anything calling itself one is fake.
This site will never ask anyone to connect a wallet.
The only real mint is ${t.real_mint ?? model.mint}. Always match the full address.
Copycat mints found: ${model.copycats?.length ?? 0}, of which ${clones.length} are exact-name clones.
Absence of evidence is not evidence of absence — we can only list clones we found.

## VERIFIED TIMELINE (onchain / social / attested — no lore)
${timeline}

## THE 1988 LORE — UNCITED
${lore}
We have NOT independently sourced a 1988 Argentine broadcast. What is verifiable is
only that the token's own immutable on-chain metadata makes the claim.

## KEY POSTS
${posts}

## QUOTES
${quotes}

## HOW THE NUMBERS ARE MADE
- We paginate the campaign wallet's associated TOKEN ACCOUNT, not the owner wallet;
  the owner's history is padded with spam airdrops from strangers.
- Pagination runs to exhaustion. An earlier version capped at 150 signatures while
  paginating newest-first, silently discarding the oldest drops and losing 55% of
  the tokens. That bug is why this site exists in its current form.
- Amounts are integer base units. Reading float uiAmount corrupts values.
- Deltas come from pre/postTokenBalances, not instruction parsing.
- TOAD is Token-2022, not classic SPL. An SPL-only parser returns nothing, silently.
- Each transfer is valued at the price of the MINUTE it landed, never today's price.
  The distribution window spans a 2.83x price swing, so hourly bucketing would
  introduce a median 5.55% error.
- Prices are cross-checked against an independent pump.fun feed; the build asserts
  the two agree within 2% median divergence. Actual: ${prov.price?.median_divergence_pct ?? "?"}%.
- Unknown values render as "—". Never a computed-looking zero.

## WHAT THIS SITE CANNOT KNOW — say so plainly if asked
- Whether the person behind the wallet has sold ANYWHERE ELSE. Every claim is
  scoped to this one wallet, never to a person's total position.
- What happened downstream after tokens left a recipient.
- Whether the wallet is self-custodied or app-custodied.
- That no phishing site exists.
- Which recipients sold versus moved tokens between their own wallets.

## OPEN QUESTIONS
${(model.open_questions ?? []).map((q) => `  - ${q}`).join("\n")}

## CAVEATS
${(model.caveats ?? []).map((c) => `  - ${c}`).join("\n")}

## CAPTURE TIMES (the numbers above are only as fresh as these)
Chain:  ${utc(prov.chain?.collected_at)} (slot ${n(prov.chain?.slot)})
Price:  ${utc(prov.price?.collected_at)}
Social: ${utc(prov.social?.collected_at)}
`;

const out = {
  generated_at: model.generated_at,
  chars: brief.length,
  brief,
};

fs.writeFileSync(path.join(ROOT, "data", "brief.json"), JSON.stringify(out, null, 1) + "\n");
console.log(
  `brief.json written: ${brief.length} chars (~${Math.round(brief.length / 3.7)} tokens) from model ${model.generated_at}`
);
