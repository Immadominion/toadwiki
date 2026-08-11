"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { useLive } from "./live";
import { fmtPrice, short } from "@/lib/format";

const MINT = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump";

const LINKS = [
  { href: "/", label: "Overview", m: "Story" },
  { href: "/ledger/", label: "The Ledger", m: "Ledger" },
  { href: "/receipts/", label: "The Receipts", m: "Receipts" },
  { href: "/methodology/", label: "Methodology", m: "Method" },
];

export function Nav() {
  const live = useLive();
  const path = usePathname();
  const [copied, setCopied] = useState(false);
  const p = fmtPrice(live?.price ?? null);

  const copyMint = () => {
    navigator.clipboard?.writeText(MINT).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    });
  };

  return (
    <>
      <header className="nav">
        <div className="wrap nav-in">
          <Link href="/" className="brand">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/toad-96.webp" width={96} height={96} alt="" className="brand-toad" />
            <span className="brand-name">
              toadwiki<em>.xyz</em>
            </span>
          </Link>
          <nav className="nav-links" aria-label="Primary">
            {LINKS.map((l) => (
              <Link
                key={l.href}
                href={l.href}
                className={`nav-link ${
                  path === l.href ||
                  (l.href !== "/" && path?.startsWith(l.href.replace(/\/$/, "")))
                    ? "on"
                    : ""
                }`}
              >
                {l.label}
              </Link>
            ))}
          </nav>
          <div className="nav-right">
            <span className={`price-tag mono ${live ? "is-live" : ""}`} aria-label={p.aria || p.text}>
              {live && <i className="live-dot" aria-hidden />}
              {p.text}
              {live?.chg24 !== null && live?.chg24 !== undefined && (
                <b className={`price-chg ${live.chg24 >= 0 ? "up" : "down"}`}>
                  {live.chg24 >= 0 ? "+" : ""}
                  {live.chg24.toFixed(1)}%
                </b>
              )}
            </span>
            <button className="mint-tag mono" onClick={copyMint} title={`copy mint: ${MINT}`} type="button">
              {copied ? "copied" : short(MINT)}
            </button>
            <Link href="/ledger/" className="btn btn-cta nav-cta">
              Open ledger
            </Link>
          </div>
        </div>
      </header>
      <nav className="mobile-tabs" aria-label="Mobile">
        {LINKS.map((l) => (
          <Link
            key={l.href}
            href={l.href}
            className={
              path === l.href ||
              (l.href !== "/" && path?.startsWith(l.href.replace(/\/$/, "")))
                ? "on"
                : ""
            }
          >
            {l.m}
          </Link>
        ))}
      </nav>
    </>
  );
}
