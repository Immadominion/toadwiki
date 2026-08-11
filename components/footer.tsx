import Link from "next/link";
import type { Model } from "@/lib/model";
import { MINT } from "@/lib/constants";

/** ISO -> "2026-08-11 08:14Z". Date-only freshness is meaningless on a campaign
 *  whose numbers move hourly, so provenance always carries the time. */
function stamp(iso: string | null | undefined): string {
  if (!iso) return "not captured";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "not captured";
  return `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 16)}Z`;
}

type Provenance = {
  chain?: { collected_at?: string; slot?: number | null; sigs_scanned?: number | null };
  price?: { collected_at?: string; source?: string };
  social?: { collected_at?: string; source?: string };
};

export function Footer({ model }: { model: Model }) {
  const prov = ((model as Record<string, unknown>).provenance ?? {}) as Provenance;
  const chainAt = stamp(prov.chain?.collected_at ?? model.generated_at);
  const priceAt = stamp(prov.price?.collected_at ?? model.generated_at);
  const socialAt = stamp(prov.social?.collected_at ?? model.generated_at);

  return (
    <footer className="footer">
      <div className="wrap footer-in">
        <div>
          <div className="footer-brand">toadwiki.xyz</div>
          <p className="footer-note">
            An independent ledger of the $TOAD airdrop campaign wallet. Every figure here is
            derived from Solana mainnet or archived from X — nothing is hand-entered.{" "}
            <strong>
              Not affiliated with @mdudas, the deployer, @eltoadpepe, or pump.fun.
            </strong>{" "}
            Not financial advice. There is no claim portal and this site will never ask you to
            connect a wallet.
          </p>
        </div>

        <dl className="footer-prov">
          <dt>chain</dt>
          <dd>
            {chainAt}
            {prov.chain?.slot ? ` · slot ${prov.chain.slot.toLocaleString("en-US")}` : ""}
            {prov.chain?.sigs_scanned
              ? ` · ${prov.chain.sigs_scanned.toLocaleString("en-US")} sigs scanned`
              : ""}
          </dd>
          <dt>price</dt>
          <dd>
            {priceAt}
            {prov.price?.source ? ` · ${prov.price.source}` : ""}
          </dd>
          <dt>posts</dt>
          <dd>
            {socialAt}
            {prov.social?.source ? ` · ${prov.social.source}` : ""}
          </dd>
        </dl>

        <div className="footer-links">
          <Link href="/methodology/">Methodology</Link>
          <Link href="/receipts/">Receipts</Link>
          <a
            href={`https://solscan.io/token/${MINT}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            Verify on Solscan ↗
          </a>
          <a href="https://x.com/eltoadpepe" target="_blank" rel="noopener noreferrer">
            @eltoadpepe ↗
          </a>
          <a
            href={`https://pump.fun/coin/${MINT}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            pump.fun ↗
          </a>
        </div>
      </div>
    </footer>
  );
}
