import { publicAskConfig } from "@/lib/ask-config";
import { SuggestForm } from "@/components/suggest-form";
import { MINT } from "@/lib/constants";

/**
 * Dynamic on purpose: the wallet, the amounts and whether the paid features are
 * switched on all come from Vercel environment variables, so the owner can change
 * any of them without a rebuild — and the page never shows a stale address, which
 * on a page that asks people to send money is the one mistake that actually costs
 * somebody something.
 */
export const dynamic = "force-dynamic";

export const metadata = {
  title: "Support · toadwiki.xyz",
  description:
    "What it costs to run an independent $TOAD ledger, how to keep it running, and how to get a suggestion onto the list.",
};

export default function SupportPage() {
  const cfg = publicAskConfig();

  return (
    <>
      <section className="page-hero">
        <div className="wrap">
          <div className="kicker">Support</div>
          <h1>Keeping the toad fed</h1>
          <p>
            This site has no ads, no token of its own, no affiliate links and nothing to sell. It
            costs real money to run, and this page is the whole of how that gets paid for.
          </p>
        </div>
      </section>

      <div className="page-body">
        <div className="wrap">
          <ul className="glance">
            <li>
              <b>What it costs</b>
              <span>
                Every answer the assistant gives is a paid API call. Archiving posts is metered per
                post. The chain data is a metered RPC key.
              </span>
            </li>
            <li>
              <b>What you get for nothing</b>
              <span>
                Every page, every figure, the whole ledger, and one question a day. That part is not
                going behind anything.
              </span>
            </li>
            <li>
              <b>What $TOAD buys</b>
              <span>
                More questions, and a suggestion that lands on the public list instead of vanishing
                into a DM.
              </span>
            </li>
            <li>
              <b>What it does not buy</b>
              <span>
                A favourable entry in the ledger. Numbers are derived from the chain and no payment
                changes one.
              </span>
            </li>
          </ul>

          <div className="sg-grid">
            <div>
              <div className="section-head">
                <div className="kicker">Send something</div>
                <h2>Where to send $TOAD</h2>
              </div>
              {cfg.tipWallet ? (
                <>
                  <p className="sg-addr-label">Any amount. Solana, $TOAD only.</p>
                  <code className="ask-addr sg-addr">{cfg.tipWallet}</code>
                  <p className="sg-foot">
                    On pump.fun this wallet is <b>@{cfg.tipHandle}</b>. The mint is{" "}
                    <code>{MINT}</code> — match it in full before you send anything.
                  </p>
                </>
              ) : (
                <p className="sg-off">
                  No receiving wallet is configured yet, so there is nowhere to send anything. That
                  is deliberate — better an empty page than a wrong address.
                </p>
              )}

              <div className="section-head" style={{ marginTop: "var(--s8)" }}>
                <div className="kicker">Honest accounting</div>
                <h2>What this does not do</h2>
              </div>
              <p className="note">
                Sending $TOAD is <strong>not an investment</strong> and buys no stake in anything. It
                does not change a single number on this site — those come from the chain, and a
                payment cannot move them. It does not buy a mention, a flattering write-up, or the
                removal of a criticism. If that is what you want, keep your tokens.
              </p>
              <p className="note">
                This project is independent and not affiliated with @mdudas, the deployer,
                @eltoadpepe or pump.fun. Nothing here is financial advice.
              </p>
            </div>

            <div>
              <div className="section-head">
                <div className="kicker">Have an opinion</div>
                <h2>Tell me what to build</h2>
              </div>
              <p className="sg-lede">
                Suggestions cost {cfg.suggestToad.toLocaleString("en-US")} $TOAD. Not to be greedy —
                a box that costs nothing fills up with nothing. Paying puts your idea on a public
                list I have to answer for.
              </p>
              <SuggestForm
                enabled={cfg.suggestEnabled}
                wallet={cfg.tipWallet}
                amount={cfg.suggestToad}
              />
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
