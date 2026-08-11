import { CAMPAIGN_ATA, CAMPAIGN_WALLET, MINT, PRICING_POOL, TOKEN_PROGRAM, solscanAccount } from "@/lib/constants";

export const dynamic = "force-static";

export const metadata = {
  title: "Methodology · toadwiki.xyz",
  description:
    "How every number on toadwiki.xyz is derived, which checks run at build time, and what this site cannot know.",
};

export default function MethodologyPage() {
  return (
    <>
      <section className="page-hero">
        <div className="wrap">
          <div className="kicker">Methodology</div>
          <h1>How we know</h1>
          <p>
            Every figure on this site is derived from Solana mainnet or archived from X.
            Nothing is hand-entered. This page explains exactly how, so you can reproduce it
            or prove us wrong.
          </p>
        </div>
      </section>

      <div className="page-body">
        <div className="wrap prose">
          <h2>1. Transfers</h2>
          <p>
            We paginate the signature history of the campaign wallet&rsquo;s{" "}
            <strong>associated token account</strong>{" "}
            <a href={solscanAccount(CAMPAIGN_ATA)} target="_blank" rel="noopener noreferrer">
              <code>{CAMPAIGN_ATA}</code>
            </a>{" "}
            &mdash; not the owner wallet{" "}
            <a href={solscanAccount(CAMPAIGN_WALLET)} target="_blank" rel="noopener noreferrer">
              <code>{CAMPAIGN_WALLET}</code>
            </a>
            . This distinction matters: the owner wallet&rsquo;s history is padded with
            unsolicited memecoin airdrops sent by strangers hoping to be noticed, while the
            token account contains only $TOAD activity.
          </p>
          <p>Three rules govern the parse:</p>
          <ul>
            <li>
              <strong>Pagination runs to exhaustion.</strong> There is no signature cap. An
              earlier version of this collector capped at 150 signatures while paginating
              newest-first, which silently discarded the oldest drops and lost the entire
              launch day &mdash; 55% of all tokens distributed.
            </li>
            <li>
              <strong>Amounts are integers.</strong> We read the raw base-unit{" "}
              <code>amount</code> string and divide by 10<sup>{6}</sup> once, at render.
              Reading the floating-point <code>uiAmount</code> produces artefacts like{" "}
              <code>39999.99999999999</code>.
            </li>
            <li>
              <strong>Deltas come from balances, not instruction parsing.</strong> We diff{" "}
              <code>preTokenBalances</code> against <code>postTokenBalances</code>, which is
              independent of how any given transaction was constructed.
            </li>
          </ul>
          <p>
            $TOAD is a <strong>Token-2022</strong> mint (
            <code>{TOKEN_PROGRAM}</code>), not classic SPL. A parser that assumes SPL returns
            nothing at all, silently &mdash; which is worth knowing if you are reproducing
            this yourself.
          </p>

          <h2>2. The invariant</h2>
          <p>
            The build fails if this does not hold to the exact base unit &mdash; not
            &ldquo;close&rdquo;, but a residual of zero:
          </p>
          <pre className="method-eq">
            <code>sum(inbound) − sum(outbound) == getAccountInfo(ATA).amount</code>
          </pre>
          <p>
            This is both the site&rsquo;s correctness test and its headline exhibit. If a
            single transfer were missed, the residual would be non-zero and no page would be
            published. It is also why we can state the distribution total without hedging:
            arithmetic closure means the transfer set is neither incomplete nor
            double-counted.
          </p>

          <h2>3. USD at drop</h2>
          <p>
            We value each transfer at the price of the <strong>minute it landed</strong>, not
            at today&rsquo;s price. Candles come from the PumpSwap pool{" "}
            <a href={solscanAccount(PRICING_POOL)} target="_blank" rel="noopener noreferrer">
              <code>{PRICING_POOL}</code>
            </a>
            , which is constant-product and sets spot. We deliberately do{" "}
            <em>not</em> price off the Meteora DLMM pool: it is concentrated-liquidity and its
            reserve ratio sits far from true spot, so a series built from it would be quietly
            wrong.
          </p>
          <p>
            Minute granularity is not fussiness. The distribution window spans a{" "}
            <strong>2.83&times; price swing</strong>. Bucketing by hour instead of minute
            introduces a median error of 5.55% and a worst case of 70.79% inside that window.
          </p>
          <p>
            Prices are cross-checked against an independent pump.fun candle feed; the build
            asserts the two sources agree to within 2% median divergence before publishing.
          </p>
          <p className="method-caveat">
            <strong>Caveat we will not bury:</strong> sub-minute precision does not exist.
            Transfers land partway into their candle, so a true fill can differ from our
            figure by that candle&rsquo;s range. We therefore publish rounded totals
            (&ldquo;~$281,000&rdquo;), never false precision (&ldquo;$281,311&rdquo;).
          </p>

          <h2>4. Held, reduced, and sold</h2>
          <p>
            We compare each recipient&rsquo;s current balance against what they received. The
            distinction between these states is deliberate:
          </p>
          <ul>
            <li>
              <strong>Sold</strong> requires evidence &mdash; the destination is a pool token
              account, or a DEX program appears in the transaction.
            </li>
            <li>
              <strong>Reduced</strong> means tokens left the wallet without any of that
              evidence. Roughly a fifth of outbound recipient volume is plain wallet-to-wallet
              movement, which is not a sale.
            </li>
            <li>
              <strong>Account closed</strong> is tracked separately from a zero balance. They
              are not the same event.
            </li>
          </ul>
          <p>
            Where a value is genuinely unknown we render <code>&mdash;</code>. We never
            display a computed-looking <code>0</code> in place of missing data. An earlier
            version of this site rendered &ldquo;Held: 0%&rdquo; for every recipient because
            the field was a hardcoded constant, which read as though all of them had dumped.
            That was wrong and it has been removed.
          </p>

          <h2>5. Whose wallet is it?</h2>
          <p>
            The campaign wallet is <strong>attributed</strong> to @mdudas at high confidence.
            It is not self-disclosed &mdash; he has never posted the address, and we will not
            claim otherwise. The attribution rests on a tweet-to-transaction match: a transfer
            landed in a token account created by that very transaction, giving the balance no
            other possible source, and the corresponding post appeared 63 seconds later
            showing exactly that balance. The evidence panel on the overview page lays out
            each link.
          </p>
          <p>
            The pump.fun profile for this address currently exposes no verified X account. We
            render that null rather than hiding it.
          </p>

          <h2>6. What this site cannot know</h2>
          <ul>
            <li>
              Whether the person behind the wallet has sold elsewhere. Every claim here is
              scoped to <em>this wallet</em>, never to a person&rsquo;s total position.
            </li>
            <li>
              What happened downstream once tokens left a recipient. The redistribution tree
              is sampled, not complete.
            </li>
            <li>
              Whether the wallet is self-custodied or app-custodied. The first buy was
              relayer-paid; later transfers are self-signed. We treat this as unknown.
            </li>
            <li>
              That no phishing site exists. We can only list the copycat mints we have
              actually found. Absence of evidence is not evidence of absence.
            </li>
          </ul>

          <h2>7. Freshness</h2>
          <p>
            The campaign is live and every number moves. Each figure carries an
            as-of timestamp, and the footer stamps three separate capture times &mdash; chain,
            price, and posts &mdash; because they are collected independently and go stale at
            different rates. Treat every number as a snapshot, never a standing fact.
          </p>

          <h2>8. Reproduce it</h2>
          <p>
            The collectors live in <code>scripts/collect/</code> and write their raw captures
            to <code>data/raw/</code> before anything is derived. The mint is{" "}
            <a href={`https://solscan.io/token/${MINT}`} target="_blank" rel="noopener noreferrer">
              <code>{MINT}</code>
            </a>
            . Everything above can be re-derived from a public RPC endpoint and free candle
            data &mdash; no paid key is required for any on-chain figure on this site.
          </p>
        </div>
      </div>
    </>
  );
}
