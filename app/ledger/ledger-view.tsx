"use client";

import { useMemo, useState } from "react";
import type { Recipient, Status } from "@/lib/model";
import { fmtAmt, fmtInt, fmtPct, fmtTs, fmtUsd, short } from "@/lib/format";
import { solscanAccount, solscanTx } from "@/lib/constants";

type Props = {
  recipients: Recipient[];
  stats: Record<string, number | null>;
  airdropWallet: string | null;
};

type Filter = "all" | Status;

/** Filters mirror the statuses the pipeline actually emits. The old set included
 *  "sold", which no recipient can ever match — nothing is proven sold — so three
 *  of four chips rendered an empty table with no explanation. */
const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "holding", label: "Holding" },
  { key: "partial", label: "Partial" },
  { key: "zero_balance", label: "Zero balance" },
  { key: "account_closed", label: "Closed account" },
];

const BADGE: Record<Status, { cls: string; label: string }> = {
  holding: { cls: "badge-hold", label: "holding" },
  partial: { cls: "badge-partial", label: "partial" },
  zero_balance: { cls: "badge-zero", label: "zero balance" },
  account_closed: { cls: "badge-closed", label: "account closed" },
};

export function LedgerView({ recipients, stats, airdropWallet }: Props) {
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [open, setOpen] = useState<string | null>(null);

  const rows = useMemo(() => {
    const qq = q.trim().toLowerCase();
    return recipients.filter((r) => {
      if (filter !== "all" && r.status !== filter) return false;
      if (!qq) return true;
      return (
        r.wallet.toLowerCase().includes(qq) ||
        (r.known_label || "").toLowerCase().includes(qq) ||
        (r.identity?.name || "").toLowerCase().includes(qq)
      );
    });
  }, [recipients, q, filter]);

  if (!recipients.length) {
    return (
      <div className="empty-panel">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/toad-256.webp" width={256} height={256} alt="" aria-hidden />
        <h3>No transfers collected</h3>
        <p>
          The collector produced no rows for
          {airdropWallet ? ` ${short(airdropWallet)}` : " the campaign wallet"}. That is a
          pipeline failure, not an empty campaign — the ledger is never published without a
          closing balance check.
        </p>
      </div>
    );
  }

  return (
    <>
      {/* The same four capsules the overview lost. Stacked one-per-row on a
          phone they cost four screens of scrolling to deliver four numbers. */}
      <p className="figures figures-dark">
        <b>{fmtInt(stats.transfers)}</b> drops to <b>{fmtInt(stats.recipients)}</b> wallets ·{" "}
        <b>{fmtAmt(stats.total_amount)}</b> $TOAD · worth{" "}
        <b>{fmtUsd(stats.total_usd_at_drop)}</b> the minute they landed
      </p>

      <div className="ledger-toolbar">
        <input
          type="search"
          placeholder="Search wallet, name, label…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="Search recipients"
        />
        {FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            className={`filter-chip ${filter === f.key ? "on" : ""}`}
            aria-pressed={filter === f.key}
            onClick={() => setFilter(f.key)}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="table-wrap">
        <table className="table">
          <caption className="sr-only">
            $TOAD airdrop recipients, ranked by amount received, with per-transfer signatures.
          </caption>
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">Wallet</th>
              <th scope="col">Amount</th>
              <th scope="col">USD at drop</th>
              <th scope="col">Status</th>
              <th scope="col">Held&nbsp;%</th>
              <th scope="col">Txs</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="table-empty">
                  No recipients match{q ? ` “${q}”` : ""}
                  {filter !== "all" ? ` with status “${FILTERS.find((f) => f.key === filter)?.label}”` : ""}.
                  <button type="button" className="linkish" onClick={() => { setQ(""); setFilter("all"); }}>
                    Clear filters
                  </button>
                </td>
              </tr>
            )}
            {rows.map((r) => {
              const isOpen = open === r.wallet;
              const badge = BADGE[r.status];
              return [
                <tr key={r.wallet}>
                  <td className="mono">{r.rank}</td>
                  <td>
                    <a
                      href={solscanAccount(r.wallet)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="mono wallet-link"
                    >
                      {r.known_label || r.identity?.name || short(r.wallet)}
                    </a>
                  </td>
                  <td className="mono">{fmtAmt(r.total)}</td>
                  <td className="mono">{fmtUsd(r.usd_at_drop)}</td>
                  <td>
                    <span className={`badge ${badge.cls}`}>{badge.label}</span>
                  </td>
                  {/* fmtPct renders "--" for null. Never Math.round() a possibly-null
                      value into a confident-looking 0%.
                      1 decimal, not 0: at 0dp, toFixed collapsed 0.995654 to "100%"
                      and 0.00134 to "0%", so four "partial" rows contradicted their
                      own badge and were indistinguishable from true zero balances. */}
                  <td className="mono">{fmtPct(r.held_pct, 1)}</td>
                  <td>
                    <button
                      type="button"
                      className="tx-toggle mono"
                      aria-expanded={isOpen}
                      onClick={() => setOpen(isOpen ? null : r.wallet)}
                    >
                      {r.tx_count} <span aria-hidden>{isOpen ? "▾" : "▸"}</span>
                    </button>
                  </td>
                </tr>,
                isOpen && (
                  <tr key={r.wallet + "-txs"} className="tx-row">
                    <td colSpan={7}>
                      <table className="tx-table">
                        <thead>
                          <tr>
                            <th scope="col">When (UTC)</th>
                            <th scope="col">Amount</th>
                            <th scope="col">USD at drop</th>
                            <th scope="col">Signature</th>
                          </tr>
                        </thead>
                        <tbody>
                          {r.txs.map((t) => (
                            <tr key={t.sig}>
                              <td className="mono">{fmtTs(t.ts)}</td>
                              <td className="mono">{fmtAmt(t.amount)}</td>
                              <td className="mono">
                                {fmtUsd(t.usd)}
                                {t.price_minute_offset !== 0 && t.price_minute_offset !== null && (
                                  <span className="px-note" title="No candle for that exact minute; nearest used">
                                    ~
                                  </span>
                                )}
                              </td>
                              <td>
                                <a
                                  href={solscanTx(t.sig)}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="mono wallet-link"
                                >
                                  {short(t.sig)} ↗
                                </a>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </td>
                  </tr>
                ),
              ];
            })}
          </tbody>
        </table>
      </div>

      <p className="table-note">
        Showing {rows.length} of {recipients.length} recipients. Amounts are exact; USD is the
        value at the minute each transfer landed, so it will not match today&rsquo;s price.{" "}
        <a href="/methodology/">How this is computed →</a>
      </p>
    </>
  );
}
