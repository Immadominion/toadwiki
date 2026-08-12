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
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const answerRef = useRef<HTMLDivElement>(null);
  const lastQuestion = useRef("");

  /**
   * Grow to fit, up to three lines, then scroll. A single-line input pushed the
   * start of a long question off to the left where the writer could not see it,
   * which is a bad way to treat the one thing this page asks people to do.
   * Height is measured from scrollHeight rather than counting characters, so it
   * is correct at any width and any font size.
   */
  const fit = useCallback((el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.style.height = "auto";
    const cs = getComputedStyle(el);
    // scrollHeight is content + padding, but box-sizing is border-box, so
    // assigning it directly loses the border and leaves every multi-line
    // message overflowing by exactly 4px with its last line clipped.
    const border = (parseFloat(cs.borderTopWidth) || 0) + (parseFloat(cs.borderBottomWidth) || 0);
    const max = parseFloat(cs.maxHeight);
    const want = el.scrollHeight + border;
    el.style.height = `${Number.isFinite(max) ? Math.min(want, max) : want}px`;
  }, []);

  useEffect(() => { fit(inputRef.current); }, [q, fit]);

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

  const fillHint = () => {
    setQ(HINT);
    requestAnimationFrame(() => {
      const el = inputRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(HINT.length, HINT.length);
    });
  };

  /**
   * Tab completes the example, the way a shell completes a path. It only fires
   * on an EMPTY field, so the moment there is anything to keep, Tab goes back to
   * being Tab and moves focus to the Ask button. Shift+Tab is never intercepted,
   * so a keyboard user is never trapped. Space does the same on an empty field,
   * which is the reachable gesture on a phone keyboard that has no Tab key.
   */
  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const empty = q.length === 0;
    if (empty && !e.shiftKey && (e.key === "Tab" || e.key === " ")) {
      e.preventDefault();
      fillHint();
      return;
    }
    // Enter sends, Shift+Enter makes a new line. Standard everywhere people type
    // a message, and the reason the field can afford to be multi-line at all.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(q);
    }
  };

  const form = (
    <form className="ask-form" onSubmit={submit}>
      <div className="ask-field">
        <textarea
          ref={inputRef}
          className="ask-q"
          rows={1}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKey}
          placeholder={HINT}
          aria-label="Ask anything about $TOAD"
          aria-describedby={`ask-hint-${variant}`}
          maxLength={400}
          disabled={state.k === "loading"}
          spellCheck
        />
        {q.length === 0 && state.k !== "loading" && (
          <button
            type="button"
            className="ask-fill"
            onClick={fillHint}
            /* Out of the tab order on purpose: Tab is what fills the field, so
               Tab must never land here. Pointer and touch still reach it. */
            tabIndex={-1}
            aria-label={`Use the example question: ${HINT}`}
          >
            <span className="k-fine">tab</span>
            <span className="k-touch">tap</span>
          </button>
        )}
      </div>
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

      {/* A shortcut nobody is told about is not a shortcut. The badge shows the
          key; this says what it does, and says the true thing on each device. */}
      {state.k === "idle" && (
        <p className="ask-hint" id={`ask-hint-${variant}`}>
          <span className="k-fine">
            Press <kbd>Tab</kbd> to use the example, or just type.
          </span>
          <span className="k-touch">Tap the hint to use it, or just type.</span>{" "}
          It answers from the verified ledger, or says it doesn&rsquo;t know.
        </p>
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
