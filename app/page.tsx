import Link from "next/link";
import { loadModel } from "@/lib/model";
import { fmtAmt, fmtInt, fmtPct, fmtTs, fmtUsd, short } from "@/lib/format";
import { MINT, solscanAccount, solscanTx } from "@/lib/constants";

export const dynamic = "force-static";

const utc = (iso: string) => `${iso.slice(0, 10)} ${iso.slice(11, 16)}Z`;

export default function Overview() {
  const model = loadModel();
  const s = model.stats;
  const rec = model.reconciliation;
  const mkt = model.market ?? {};
  const snap = model.market_snapshot ?? {};
  const proof = model.wallet_proof ?? {};
  const dep = model.deployer_conduct ?? {};

  const ath = mkt.ath ?? {};
  const drawdown =
    ath.price_usd && snap.price_usd ? snap.price_usd / ath.price_usd - 1 : null;

  // Runway at the current burn-down rate. Uses the last full day, not an average
  // across the whole campaign — the rate is falling fast and an average flatters it.
  const daily = model.airdrop_daily ?? [];
  const lastFull = daily.length >= 2 ? daily[daily.length - 2] : null;
  const runwayDays =
    lastFull && lastFull.amount > 0 ? Math.round(rec.held / lastFull.amount) : null;

  const hoursSinceLastDrop = s.last_drop_ts
    ? Math.floor((Date.now() / 1000 - s.last_drop_ts) / 3600)
    : null;

  const onchainTimeline = model.timeline.filter(
    (t) => t.kind === "onchain" || t.kind === "social" || t.kind === "attested"
  );
  const loreTimeline = model.timeline.filter((t) => t.kind === "lore");
  const clones = model.copycats.filter((c) => c.severity === "exact_name_clone");

  return (
    <>
      {/* ---------- HERO ---------- */}
      <section className="hero">
        <div className="wrap hero-in">
          <div className="rise">
            <div className="kicker">Independent · not affiliated</div>
            <h1>
              The first Pepe was a <em>toad</em>
            </h1>
            <p className="lede">
              El Sapo Pepe hopped Argentine TV in 1988. Today a Solana wallet holding{" "}
              {fmtAmt(rec.held)} $TOAD is giving it away, and every base unit of it is
              accounted for on this page.
            </p>
            <div className="hero-cta">
              <Link href="/ledger/" className="btn btn-cta">
                Open the ledger
              </Link>
            </div>

            {/* Figures as typography, not tiles. Four capsules in a row is the
                single most recognisable AI-dashboard pattern, and it turns four
                real numbers into decoration. */}
            <p className="figures rise rise-1">
              <b>{fmtInt(s.transfers)}</b> drops to <b>{fmtInt(s.recipients)}</b> wallets ·{" "}
              <b>{fmtAmt(s.total_amount)}</b> $TOAD · worth{" "}
              <b>{fmtUsd(s.total_usd_at_drop)}</b> the minute they landed
            </p>
          </div>
          <div className="hero-mascot rise rise-2">
            <div className="mascot-glow" aria-hidden />
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/toad-768.webp"
              width={768}
              height={768}
              alt="El Sapo Pepe, the 1988 Argentine children's TV puppet the token is named after"
              className="floaty"
            />
          </div>
        </div>
      </section>

      {/* ---------- THE RECONCILIATION — the site's whole argument ---------- */}
      <section className="section">
        <div className="wrap">
          <div className="section-head">
            <div className="kicker">The proof</div>
            <h2>The books close to zero</h2>
          </div>

          <div className="recon">
            <table className="recon-eq">
              <caption className="sr-only">
                Campaign wallet reconciliation: received minus distributed equals the live
                on-chain balance.
              </caption>
              <tbody>
                <tr>
                  <td className="mono num">{rec.received.toLocaleString("en-US", { minimumFractionDigits: 6 })}</td>
                  <td>
                    received
                    <span className="sub">{rec.received_tx_count} inbound transfers</span>
                  </td>
                </tr>
                <tr>
                  <td className="mono num">
                    − {rec.distributed.toLocaleString("en-US", { minimumFractionDigits: 6 })}
                  </td>
                  <td>
                    distributed
                    <span className="sub">
                      {rec.distributed_transfer_count} transfers to{" "}
                      {rec.distributed_recipient_count} wallets
                    </span>
                  </td>
                </tr>
                <tr className="recon-total">
                  <td className="mono num">
                    = {rec.held.toLocaleString("en-US", { minimumFractionDigits: 6 })}
                  </td>
                  <td>
                    still held
                    <span className="sub">matches the live on-chain balance exactly</span>
                  </td>
                </tr>
              </tbody>
            </table>

            <div className="recon-side">
              <div className="recon-stat">
                <div className="label">Residual</div>
                <div className="val mono big">{rec.residual_raw}</div>
                <p>
                  Base units unaccounted for. If this were anything other than zero the
                  build would fail and this page would not exist.
                </p>
              </div>
              <div className="recon-stat">
                <div className="label">Sells detected</div>
                <div className="val mono big">{rec.sells_detected}</div>
                <p>
                  No outbound transfer reached a liquidity pool or invoked a swap program,
                  at any call depth.
                </p>
              </div>
              <p className="recon-meta mono">
                verified live at slot {rec.checked_at_slot.toLocaleString("en-US")} ·{" "}
                <Link href="/methodology/">how this is checked →</Link>
              </p>
            </div>
          </div>

          <p className="note">
            A statement about <strong>a wallet</strong> — not a person. It cannot see tokens
            held anywhere else.
          </p>
        </div>
      </section>

      {/* ---------- LIVE STATE ---------- */}
      <section className="section section-alt">
        <div className="wrap">
          <div className="section-head">
            <div className="kicker">Where things stand</div>
            <h2>Live state</h2>
            <span className="tail mono">as of {utc(snap.as_of ?? model.generated_at)}</span>
          </div>
          <div className="kpi-grid">
            <div className="kpi">
              <div className="label">Market cap</div>
              <div className="val mono">{fmtUsd(snap.mcap_usd)}</div>
              <div className="sub">live supply × pool close</div>
            </div>
            <div className="kpi">
              <div className="label">All-time high</div>
              <div className="val mono">{fmtUsd((ath.price_usd ?? 0) * (model.token.supply ?? 0))}</div>
              <div className="sub">{ath.iso ? utc(ath.iso) : "—"}</div>
            </div>
            <div className="kpi">
              <div className="label">From ATH</div>
              <div className={`val mono ${drawdown !== null && drawdown < 0 ? "neg" : ""}`}>
                {drawdown !== null ? fmtPct(drawdown, 1) : "--"}
              </div>
              <div className="sub">on price</div>
            </div>
            <div className="kpi">
              <div className="label">Holders</div>
              <div className="val mono">{fmtInt(model.holders?.count)}</div>
              <div className="sub">non-zero balances</div>
            </div>
            <div className="kpi">
              <div className="label">Since last drop</div>
              <div className="val mono">
                {hoursSinceLastDrop !== null ? `${hoursSinceLastDrop}h` : "--"}
              </div>
              <div className="sub">
                {s.last_drop_ts ? fmtTs(s.last_drop_ts) : "—"}
              </div>
            </div>
            <div className="kpi">
              <div className="label">Still to give</div>
              <div className="val mono">{fmtAmt(rec.held)}</div>
              <div className="sub">
                {runwayDays ? `~${runwayDays} days at the last full day's rate` : "—"}
              </div>
            </div>
          </div>

          <div className="burn">
            <h3>Distribution is decelerating</h3>
            {/* The wrapper is not decoration: .burn-table carries min-width:560px,
                and without a scroll container it widened the whole document to
                600px on a 390px phone — which makes mobile Safari zoom the entire
                page out, shrinking every other element on the site. */}
            <div className="table-wrap">
            <table className="table burn-table">
              <thead>
                <tr>
                  <th scope="col">Day</th>
                  <th scope="col">Transfers</th>
                  <th scope="col">$TOAD</th>
                  <th scope="col">USD at drop</th>
                  <th scope="col" className="bar-col">Relative volume</th>
                </tr>
              </thead>
              <tbody>
                {daily.map((d) => {
                  const max = Math.max(...daily.map((x) => x.amount));
                  return (
                    <tr key={d.date}>
                      <td className="mono">{d.date}</td>
                      <td className="mono">{d.count}</td>
                      <td className="mono">{fmtAmt(d.amount)}</td>
                      <td className="mono">{d.usd_display ?? fmtUsd(d.usd)}</td>
                      <td className="bar-col">
                        <span className="bar" style={{ width: `${(d.amount / max) * 100}%` }} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </div>
            <p className="note">
              Drops per day rose while tokens per day halved. <strong>Tokens</strong> is the
              honest metric — counting transfers tells the opposite story.
            </p>
          </div>
        </div>
      </section>

      {/* ---------- WALLET EVIDENCE ---------- */}
      <section className="section">
        <div className="wrap">
          <div className="section-head">
            <div className="kicker">Attribution</div>
            <h2>How we know whose wallet this is</h2>
          </div>

          <div className="evidence">
            <div className="evidence-claim">
              <p className="lede-dark">{proof.claim}</p>
              <p className="warn">
                <strong>Nobody ever posted this address.</strong> What follows is inference
                from public evidence — not a disclosure.
              </p>
            </div>

            <ol className="evidence-chain">
              <li>
                <div className="step-label mono">1 · the transfer</div>
                <p>
                  {proof.tx?.amount ? fmtAmt(Number(proof.tx.amount)) : "70,000"} $TOAD left
                  the campaign wallet at {proof.tx?.time ? utc(proof.tx.time) : "—"}.
                </p>
                {proof.tx?.sig && (
                  <a className="mono wallet-link" href={solscanTx(proof.tx.sig)} target="_blank" rel="noopener noreferrer">
                    {short(proof.tx.sig)} ↗
                  </a>
                )}
              </li>
              <li>
                <div className="step-label mono">2 · the receiving account</div>
                <p>
                  The recipient&rsquo;s token account was <strong>created by that very
                  transaction</strong> and has{" "}
                  {proof.recipient_token_account?.signature_count_ever ?? 1} signature in its
                  entire history. The balance has no other possible source.
                </p>
                {proof.recipient_token_account?.address && (
                  <a
                    className="mono wallet-link"
                    href={solscanAccount(proof.recipient_token_account.address)}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {short(proof.recipient_token_account.address)} ↗
                  </a>
                )}
              </li>
              <li>
                <div className="step-label mono">3 · the post, 63 seconds later</div>
                <p>
                  @mdudas replied to that recipient with a screenshot of their portfolio
                  showing exactly that balance.
                </p>
              </li>
            </ol>
          </div>

          {model.caveats && model.caveats.length > 0 && (
            <details className="caveats">
              <summary>What weakens this ({model.caveats.length})</summary>
              <ul>
                {model.caveats.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
      </section>

      {/* ---------- ORIGIN ---------- */}
      <section className="section section-alt">
        <div className="wrap">
          <div className="section-head">
            <div className="kicker">Origin</div>
            <h2>The first four hours</h2>
            <span className="tail mono">every row has a signature or a post</span>
          </div>
          <ol className="tick-tock">
            {onchainTimeline.map((t, i) => (
              /* When and what-kind both belong to the gutter. Keeping the kind
                 badge on its own row under the event cost ~33px on every one of
                 the eleven rows and bought nothing — the gutter had the space. */
              <li key={i} className={`tt-${t.kind}`}>
                <div className="tt-when">
                  <span className="mono">{t.date}</span>
                  <span className={`badge badge-${t.kind === "onchain" ? "hold" : t.kind === "attested" ? "partial" : "closed"}`}>
                    {t.kind}
                  </span>
                </div>
                <div className="tt-what">
                  <p>{t.event}</p>
                  {t.caveat && <p className="tt-caveat">{t.caveat}</p>}
                  {t.source && (
                    <div className="tt-meta">
                      <a href={t.source} target="_blank" rel="noopener noreferrer" className="mono">
                        source ↗
                      </a>
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>
          {loreTimeline.length > 0 && (
            <details className="caveats">
              <summary>The 1988 lore ({loreTimeline.length} entries — uncited history)</summary>
              <ul>
                {loreTimeline.map((t, i) => (
                  <li key={i}>
                    <strong className="mono">{t.date}</strong> — {t.event}
                  </li>
                ))}
              </ul>
              <p className="note">
                We have not independently sourced a 1988 broadcast. What is verifiable is that
                the token&rsquo;s own on-chain metadata, immutable since mint, makes this
                claim.
              </p>
            </details>
          )}
        </div>
      </section>

      {/* ---------- CRITICISM ---------- */}
      <section className="section">
        <div className="wrap">
          <div className="section-head">
            <div className="kicker">The other side</div>
            <h2>What critics say</h2>
          </div>
          <div className="crit-grid">
            <div className="crit">
              <h3>The conflict of interest</h3>
              <p>
                Mike Dudas is a partner at 6th Man Ventures, <strong>a seed investor in
                pump.fun</strong>. He was personally asked by pump.fun&rsquo;s co-founder to
                test the new mobile app, bought $TOAD{" "}
                <strong>17 minutes and 43 seconds after the mint</strong> at a fully-diluted
                value under $50,000, and the resulting trade became a marketing story for the
                platform he is invested in.
              </p>
              <p className="note">
                All verifiable, none of it alleged wrongdoing. A ledger that omits it is a fan
                page.
              </p>
            </div>
            <div className="crit">
              <h3>The deployer took 20% in the mint transaction</h3>
              <p>
                The deployer bought <strong>200,963,210.66 $TOAD — 20.1% of supply</strong> for
                6.91 SOL <em>inside the same transaction that created the token</em>, then 88
                minutes later sent 180,963,210.66 of it onward and kept exactly 20,000,000.
              </p>
              <p className="note">
                Against the usual assumption: <strong>{dep.sells ?? 0} sells</strong>,{" "}
                {fmtInt(dep.buys)} buys. Its pool transfers were liquidity deposits
                ({fmtInt(dep.lp_deposits)}), not sales.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* ---------- REAL VS FAKE ---------- */}
      <section className="section section-alt">
        <div className="wrap">
          <div className="section-head">
            <div className="kicker">Safety</div>
            <h2>One real mint, {model.copycats.length} imitators</h2>
          </div>

          <div className="mint-real">
            <div className="mint-flag ok">REAL</div>
            <div>
              <div className="mono mint-addr">{MINT}</div>
              <div className="sub">
                {model.token.name} · {model.token.symbol} · {model.token.token_program_name ?? "Token-2022"}
              </div>
            </div>
            <a className="btn btn-outline" href={`https://solscan.io/token/${MINT}`} target="_blank" rel="noopener noreferrer">
              Verify ↗
            </a>
          </div>

          {clones.length > 0 && (
            <>
              <h3 className="sub-head">Exact-name clones</h3>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Mint</th>
                      <th scope="col">Name</th>
                      <th scope="col">Liquidity</th>
                      <th scope="col">Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {clones.map((c) => (
                      <tr key={c.mint}>
                        <td className="mono danger">{short(c.mint)}</td>
                        <td>{c.name} · {c.symbol}</td>
                        <td className="mono">{fmtUsd(c.liquidity_usd)}</td>
                        <td className="mono">{c.created_at ? c.created_at.slice(0, 10) : "--"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          <p className="note">
            <strong>There is no claim portal for $TOAD.</strong> Nothing calling itself one is
            real. Match the contract address, every time.
          </p>
        </div>
      </section>

      {/* ---------- OPEN QUESTIONS ---------- */}
      {model.open_questions?.length > 0 && (
        <section className="section">
          <div className="wrap">
            <div className="section-head">
              <div className="kicker">Not settled</div>
              <h2>Open questions</h2>
            </div>
            <ul className="oq">
              {model.open_questions.map((q, i) => (
                <li key={i}>{q}</li>
              ))}
            </ul>
          </div>
        </section>
      )}
    </>
  );
}
