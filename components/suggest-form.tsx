"use client";

import { useState } from "react";

type Props = { enabled: boolean; wallet: string | null; amount: number };

type State =
  | { k: "idle" }
  | { k: "sending" }
  | { k: "done"; url: string | null; number: number | null; paid: number }
  | { k: "error"; text: string };

/**
 * Paid suggestions. Payment is checked on-chain server-side before anything is
 * filed, so this form cannot be used as a free channel into the repo.
 *
 * The submitter is told plainly, before they type, that the result is public —
 * asking for money and then publishing someone's words without warning would be
 * a nasty surprise on a site that sells itself on candour.
 */
export function SuggestForm({ enabled, wallet, amount }: Props) {
  const [text, setText] = useState("");
  const [sig, setSig] = useState("");
  const [contact, setContact] = useState("");
  const [state, setState] = useState<State>({ k: "idle" });

  if (!enabled) {
    return (
      <p className="sg-off">
        The suggestion box isn&rsquo;t switched on yet — the owner still has to add a receiving
        wallet and a repo token. Nothing here will take your money in the meantime.
      </p>
    );
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!text.trim() || !sig.trim()) return;
    setState({ k: "sending" });
    try {
      const res = await fetch("/api/suggest/", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ suggestion: text.trim(), txSig: sig.trim(), contact: contact.trim() }),
      });
      const data = await res.json();
      if (res.ok && data.ok) {
        setState({ k: "done", url: data.url ?? null, number: data.number ?? null, paid: data.paid ?? 0 });
        setText("");
        setSig("");
        setContact("");
        return;
      }
      setState({ k: "error", text: data.error || "Something went wrong." });
    } catch {
      setState({ k: "error", text: "Couldn't reach the server. Try again." });
    }
  };

  if (state.k === "done") {
    return (
      <div className="sg-done">
        <h3>Filed.</h3>
        <p>
          Your {state.paid.toLocaleString("en-US", { maximumFractionDigits: 0 })} $TOAD was verified
          on-chain and the suggestion is now issue{" "}
          {state.url ? (
            <a href={state.url} target="_blank" rel="noopener noreferrer">
              #{state.number}
            </a>
          ) : (
            <>#{state.number}</>
          )}{" "}
          on the repo. Thank you — that genuinely keeps this running.
        </p>
        <button className="btn btn-outline" type="button" onClick={() => setState({ k: "idle" })}>
          Send another
        </button>
      </div>
    );
  }

  return (
    <form className="sg" onSubmit={submit}>
      <ol className="sg-steps">
        <li>
          Send at least <b>{amount.toLocaleString("en-US")} $TOAD</b> to{" "}
          {wallet ? <code className="ask-addr">{wallet}</code> : <span>the owner&rsquo;s wallet</span>}
        </li>
        <li>Write what you want built, changed, or taken down.</li>
        <li>Paste the signature. It gets checked on-chain, then filed.</li>
      </ol>

      <label className="sg-label" htmlFor="sg-text">
        Your suggestion
      </label>
      <textarea
        id="sg-text"
        className="sg-text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={5}
        maxLength={1200}
        placeholder="What should this site do that it doesn't?"
        required
      />
      <div className="sg-count">{text.length}/1200</div>

      <label className="sg-label" htmlFor="sg-sig">
        Transaction signature
      </label>
      <input
        id="sg-sig"
        className="sg-input"
        value={sig}
        onChange={(e) => setSig(e.target.value)}
        placeholder="the signature of your $TOAD transfer"
        spellCheck={false}
        required
      />

      <label className="sg-label" htmlFor="sg-contact">
        How to reach you <span>(optional)</span>
      </label>
      <input
        id="sg-contact"
        className="sg-input"
        value={contact}
        onChange={(e) => setContact(e.target.value)}
        placeholder="@handle, if you want a reply"
        maxLength={80}
      />

      <p className="sg-warn">
        <strong>This becomes a public GitHub issue.</strong> Your words, the amount you sent, the
        sending wallet and the transaction all appear on the repo where anyone can read them. Don&rsquo;t
        put anything private in it.
      </p>

      {state.k === "error" && (
        <p className="ask-error" role="alert">
          {state.text}
        </p>
      )}

      <button className="btn btn-cta" type="submit" disabled={state.k === "sending" || !text.trim() || !sig.trim()}>
        {state.k === "sending" ? "Checking the chain…" : "Verify and file it"}
      </button>
      <p className="sg-foot">
        Send the transfer within 2 hours of submitting. Nothing here connects to your wallet — you
        send from wherever you normally would and paste the receipt.
      </p>
    </form>
  );
}
