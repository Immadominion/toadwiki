"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

/**
 * The ask interface. One state machine, two shapes:
 *   variant="hero" — inline, the front door on the overview page
 *   variant="dock" — a launcher and panel on every other page
 *
 * No wallet address or price is hardcoded here. When the free question is used
 * up the server answers 402 and tells the client what payment it wants, so the
 * owner can change the amount or the wallet in Vercel without a redeploy, and
 * nothing sensitive sits in the bundle.
 */

/**
 * One hint, and it lives inside the field. A row of pill-shaped suggestion
 * buttons is the house style of every AI product shipped since 2023, and it
 * turned four questions into four containers under a box that already had a
 * container. The placeholder does the same job with no chrome at all.
 */
const HINT = "How much $TOAD has been given away?";

type Pay = { tipWallet: string | null; tipHandle: string; unlockToad: number; perUnlock: number };

type State =
  | { k: "idle" }
  | { k: "loading" }
  | { k: "answer"; text: string; remaining: number; asOf: string }
  | { k: "error"; text: string }
  | { k: "pay"; pay: Pay };

export function Ask({ variant }: { variant: "hero" | "dock" }) {
  const [q, setQ] = useState("");
  const [state, setState] = useState<State>({ k: "idle" });
  const [sig, setSig] = useState("");
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const answerRef = useRef<HTMLDivElement>(null);
  const lastQuestion = useRef("");

  const send = useCallback(async (question: string, txSig?: string) => {
    if (!question.trim()) return;
    lastQuestion.current = question;
    setState({ k: "loading" });
    try {
      const res = await fetch("/api/ask/", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question, ...(txSig ? { txSig } : {}) }),
      });
      const data = await res.json();
      if (res.ok && data.ok) {
        setSig("");
        setState({ k: "answer", text: data.answer, remaining: data.remaining ?? 0, asOf: data.asOf ?? "" });
        return;
      }
      if (data.needsPayment) {
        setState({
          k: "pay",
          pay: {
            tipWallet: data.tipWallet ?? null,
            tipHandle: data.tipHandle ?? "heisjoel0x",
            unlockToad: data.unlockToad ?? 0,
            perUnlock: data.perUnlock ?? 0,
          },
        });
        return;
      }
      setState({ k: "error", text: data.error || "Something went wrong." });
    } catch {
      setState({ k: "error", text: "Couldn't reach the toad. Check your connection." });
    }
  }, []);

  // Move focus to the answer when it lands, so a screen reader announces it
  // instead of leaving the user in a text field that silently changed context.
  useEffect(() => {
    if (state.k === "answer" || state.k === "error") answerRef.current?.focus();
  }, [state.k]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    send(q);
  };

  const form = (
    <form className="ask-form" onSubmit={submit}>
      <input
        ref={inputRef}
        className="ask-input"
        type="text"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder={HINT}
        aria-label="Ask anything about $TOAD"
        maxLength={400}
        disabled={state.k === "loading"}
      />
      {/* Disabled only while a request is in flight. Greying it out for an empty
          field made the primary action of the whole page look broken on arrival;
          submit already no-ops on empty. */}
      <button className="ask-send" type="submit" disabled={state.k === "loading"}>
        {state.k === "loading" ? "…" : "Ask"}
      </button>
    </form>
  );

  const body = (
    <>
      {form}

      {state.k === "idle" && (
        <p className="ask-hint">It answers from the verified ledger, or says it doesn&rsquo;t know.</p>
      )}

      {state.k === "loading" && <p className="ask-status" role="status">Reading the ledger…</p>}

      <div className="ask-out" ref={answerRef} tabIndex={-1} aria-live="polite">
        {state.k === "answer" && (
          <>
            <p className="ask-answer">{state.text}</p>
            <p className="ask-foot">
              Answered only from the verified ledger
              {state.asOf ? `, captured ${state.asOf.slice(0, 10)} ${state.asOf.slice(11, 16)}Z` : ""}.{" "}
              <Link href="/methodology/">How that&rsquo;s built →</Link>
              {state.remaining > 0 && (
                <span className="ask-left"> · {state.remaining} question{state.remaining === 1 ? "" : "s"} left</span>
              )}
            </p>
            <button className="ask-again" type="button" onClick={() => { setQ(""); setState({ k: "idle" }); }}>
              Ask another
            </button>
          </>
        )}

        {state.k === "error" && (
          <p className="ask-error" role="alert">
            {state.text}
          </p>
        )}

        {state.k === "pay" && (
          <div className="ask-pay">
            <p className="ask-pay-lede">
              That was your free question for today. Running this costs real money per answer, so
              more questions cost some $TOAD.
            </p>
            {state.pay.tipWallet ? (
              <>
                <ol className="ask-steps">
                  <li>
                    Send at least <b>{state.pay.unlockToad.toLocaleString("en-US")} $TOAD</b> to{" "}
                    <code className="ask-addr">{state.pay.tipWallet}</code>
                  </li>
                  <li>Paste the transaction signature below.</li>
                  <li>
                    That unlocks <b>{state.pay.perUnlock} more questions</b> for the next 24 hours.
                  </li>
                </ol>
                <div className="ask-form">
                  <input
                    className="ask-input"
                    value={sig}
                    onChange={(e) => setSig(e.target.value)}
                    placeholder="transaction signature…"
                    aria-label="Transaction signature"
                    spellCheck={false}
                  />
                  <button
                    className="ask-send"
                    type="button"
                    disabled={!sig.trim()}
                    onClick={() => send(lastQuestion.current || q, sig.trim())}
                  >
                    Unlock
                  </button>
                </div>
                <p className="ask-foot">
                  Verified on-chain before it counts — same check the rest of this site runs. Send it
                  within 2 hours of the transfer. Nothing connects to your wallet.
                </p>
              </>
            ) : (
              <p className="ask-foot">The owner hasn&rsquo;t set a receiving wallet yet, so paid questions are off.</p>
            )}
          </div>
        )}
      </div>
    </>
  );

  if (variant === "hero") {
    return (
      <div className="ask ask-hero">
        {body}
      </div>
    );
  }

  return (
    <>
      <button
        className={`ask-launch${open ? " is-open" : ""}`}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls="ask-panel"
      >
        {open ? (
          <span aria-hidden>×</span>
        ) : (
          <>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/toad-96.webp" alt="" width={28} height={28} />
            <span>Ask the toad</span>
          </>
        )}
      </button>
      {open && (
        <div className="ask ask-dock" id="ask-panel" role="dialog" aria-label="Ask the toad">
          <div className="ask-dock-head">
            <b>Ask the toad</b>
            <span>anything in the ledger</span>
          </div>
          {body}
        </div>
      )}
    </>
  );
}
