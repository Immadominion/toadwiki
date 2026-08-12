import {
  GITHUB_REPO,
  GITHUB_TOKEN,
  MAX_SUGGESTION_CHARS,
  SUGGEST_TOAD,
  TIP_WALLET,
  publicAskConfig,
} from "@/lib/ask-config";
import { claimSignature, clientIp } from "@/lib/limits";
import { verifyToadPayment } from "@/lib/verify-payment";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Paid suggestions. Payment is verified on-chain before anything is filed, so
 * the box cannot be used as a free megaphone into the repo.
 *
 * The suggestion text is written into a public GitHub issue, which means two
 * things have to be true and are enforced below: the submitter is told it will
 * be public, and the text is fenced and length-capped so it cannot forge issue
 * metadata or smuggle markup past the reader.
 */

const bad = (status: number, error: string, extra: Record<string, unknown> = {}) =>
  Response.json({ ok: false, error, ...extra }, { status });

export async function GET() {
  return Response.json(publicAskConfig());
}

export async function POST(req: Request) {
  if (!TIP_WALLET) return bad(503, "Suggestions aren't set up on this site yet.");
  if (!GITHUB_TOKEN) return bad(503, "Suggestions aren't set up on this site yet.");

  let body: { suggestion?: unknown; txSig?: unknown; contact?: unknown };
  try {
    body = await req.json();
  } catch {
    return bad(400, "Malformed request.");
  }

  const suggestion = typeof body.suggestion === "string" ? body.suggestion.trim() : "";
  const txSig = typeof body.txSig === "string" ? body.txSig.trim() : "";
  const contact = typeof body.contact === "string" ? body.contact.trim().slice(0, 80) : "";

  if (!suggestion) return bad(400, "Write the suggestion first.");
  if (suggestion.length > MAX_SUGGESTION_CHARS) {
    return bad(400, `Keep it under ${MAX_SUGGESTION_CHARS} characters.`);
  }
  if (!txSig) return bad(400, "Paste the transaction signature for your $TOAD transfer.");

  if (!claimSignature(txSig)) return bad(409, "That transaction has already been used.");
  const paid = await verifyToadPayment(txSig, TIP_WALLET, SUGGEST_TOAD);
  if (!paid.ok) return bad(402, paid.reason);

  // Fenced, so no amount of markup in the body can impersonate our own fields.
  const fence = "```";
  const issueBody = [
    `**Paid suggestion** — verified on-chain before filing.`,
    ``,
    `| | |`,
    `|---|---|`,
    `| Paid | ${paid.amount.toLocaleString("en-US", { maximumFractionDigits: 0 })} $TOAD |`,
    `| From | \`${paid.from ?? "unknown"}\` |`,
    `| Transaction | [\`${paid.sig}\`](https://solscan.io/tx/${paid.sig}) |`,
    `| Contact given | ${contact ? `\`${contact.replace(/`/g, "'")}\`` : "_none_"} |`,
    ``,
    `### Suggestion`,
    ``,
    fence,
    suggestion.replace(/```/g, "'''"),
    fence,
    ``,
    `<sub>Filed automatically by /api/suggest. The text above is submitted by a visitor and is not endorsed.</sub>`,
  ].join("\n");

  const title = `suggestion: ${suggestion.replace(/\s+/g, " ").slice(0, 70)}${
    suggestion.length > 70 ? "…" : ""
  }`;

  try {
    const res = await fetch(`https://api.github.com/repos/${GITHUB_REPO}/issues`, {
      method: "POST",
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${GITHUB_TOKEN}`,
        "content-type": "application/json",
        "x-github-api-version": "2022-11-28",
      },
      body: JSON.stringify({ title, body: issueBody, labels: ["suggestion", "paid"] }),
      signal: AbortSignal.timeout(15_000),
    });

    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      console.error("github issue error", res.status, detail.slice(0, 300), "ip", clientIp(req.headers));
      // The money already moved. Never imply it did not.
      return bad(
        502,
        "Your payment went through and was verified, but filing the suggestion failed. Screenshot this and send the transaction signature to the site owner — it will be filed by hand."
      );
    }

    const issue = (await res.json()) as { number?: number; html_url?: string };
    return Response.json({
      ok: true,
      number: issue.number ?? null,
      url: issue.html_url ?? null,
      paid: paid.amount,
    });
  } catch {
    return bad(
      502,
      "Your payment went through and was verified, but filing the suggestion timed out. Keep the transaction signature and send it to the site owner."
    );
  }
}
