/* Number formatting: mono tabular numerals, no scientific notation, "--" for null. */

const invalid = (n: unknown): n is null | undefined =>
  n === null || n === undefined || (typeof n === "number" && !isFinite(n));

function abbrev(n: number, digits = 2): string {
  const abs = Math.abs(n);
  if (abs >= 1e12) return (n / 1e12).toFixed(digits) + "T";
  if (abs >= 1e9) return (n / 1e9).toFixed(digits) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(digits) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(digits >= 2 ? 1 : digits) + "K";
  return n.toLocaleString("en-US", { maximumFractionDigits: digits });
}

export function fmtUsd(n: number | null | undefined, digits = 2): string {
  if (invalid(n)) return "--";
  if (n === 0) return "$0.00";
  if (Math.abs(n) < 0.01) return "<$0.01";
  if (Math.abs(n) < 1000)
    return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return "$" + abbrev(n, digits);
}

export function fmtUsdFull(n: number | null | undefined): string {
  if (invalid(n)) return "--";
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function fmtAmt(n: number | null | undefined): string {
  if (invalid(n)) return "--";
  if (n === 0) return "0";
  if (Math.abs(n) >= 1000) return abbrev(n, 2);
  return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

const SUBS = "₀₁₂₃₄₅₆₇₈₉";
export function fmtPrice(n: number | null | undefined): { text: string; aria?: string } {
  if (invalid(n)) return { text: "--" };
  if (n === 0) return { text: "$0.00" };
  if (n >= 0.001)
    return { text: "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 }) };
  const s = n.toFixed(12);
  const m = s.match(/^0\.(0+)([1-9]\d{0,3})/);
  if (!m) return { text: "$" + n.toFixed(6) };
  const sub = String(m[1].length)
    .split("")
    .map((d) => SUBS[+d])
    .join("");
  return { text: `$0.0${sub}${m[2].slice(0, 2)}`, aria: `$${s.replace(/0+$/, "")}` };
}

export function fmtPct(n: number | null | undefined, digits = 1): string {
  if (invalid(n)) return "--";
  const v = n * 100;
  if (v !== 0 && Math.abs(v) < 0.1) return "<0.1%";
  return v.toFixed(digits) + "%";
}

export function fmtInt(n: number | null | undefined): string {
  if (invalid(n)) return "--";
  return Math.round(n).toLocaleString("en-US");
}

export const short = (w: string) => `${w.slice(0, 4)}…${w.slice(-4)}`;

export function fmtTs(ts: number | null | undefined): string {
  if (invalid(ts)) return "--";
  const d = new Date(ts * 1000);
  return (
    d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) +
    " " +
    d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" })
  );
}

export function fmtDate(iso: string): string {
  const d = new Date(iso);
  if (isNaN(+d)) return iso;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

export function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(+d)) return "";
  return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" });
}
