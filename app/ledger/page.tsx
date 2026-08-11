import { loadModel } from "@/lib/model";
import { LedgerView } from "./ledger-view";

export const dynamic = "force-static";

export default function LedgerPage() {
  const model = loadModel();
  return (
    <>
      <section className="page-hero">
        <div className="wrap">
          <div className="kicker">
            The Ledger
          </div>
          <h1>Every $TOAD airdrop, line by line</h1>
          <p>
            Recipients of outbound transfers from the campaign wallet — amount, USD at the moment of
            the drop, whether they still hold it. Updated as the airdrop trail is confirmed onchain.
          </p>
        </div>
      </section>
      <div className="page-body">
        <div className="wrap">
          <LedgerView
            recipients={model.recipients}
            stats={model.stats}
            airdropWallet={model.airdrop_wallet}
          />
        </div>
      </div>
    </>
  );
}
