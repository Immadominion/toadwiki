import { fmtInt } from "@/lib/format";

/**
 * A post rendered with real X chrome — avatar, handle, the bird-cage icons,
 * engagement row — rather than as a generic bordered card with a quote in it.
 * If the page is going to show someone's post, it should look like their post.
 *
 * Metrics are stamped: they were true at capture time, not now. The caller
 * passes `capturedAt` so the card can say so on hover rather than implying live.
 */

type Author = { name: string; handle: string; avatar: string | null; followers?: number | null };

export type TweetCardProps = {
  id: string;
  url?: string;
  author: Author;
  date: string;
  text: string;
  label?: string | null;
  likes?: number | null;
  replies?: number | null;
  rts?: number | null;
  views?: number | null;
  photo?: string | null;
  capturedAt?: string | null;
  /** Marks this as one of the curated moments rather than ambient chatter. */
  highlight?: boolean;
};

const Icon = ({ d, label }: { d: string; label: string }) => (
  <svg viewBox="0 0 24 24" aria-label={label} role="img" className="tw-ic">
    <path d={d} />
  </svg>
);

const REPLY =
  "M1.751 10c0-4.42 3.584-8 8.005-8h4.366c4.49 0 8.129 3.64 8.129 8.13 0 2.96-1.607 5.68-4.196 7.11l-8.054 4.46v-3.69h-.067c-4.49.1-8.183-3.51-8.183-8.01zm8.005-6c-3.317 0-6.005 2.69-6.005 6 0 3.37 2.77 6.08 6.138 6.01l.351-.01h1.761v2.3l5.087-2.81c1.951-1.08 3.163-3.13 3.163-5.36 0-3.39-2.744-6.13-6.129-6.13H9.756z";
const RT =
  "M4.5 3.88l4.432 4.14-1.364 1.46L5.5 7.55V16c0 1.1.896 2 2 2H13v2H7.5c-2.209 0-4-1.79-4-4V7.55L1.432 9.48.068 8.02 4.5 3.88zM16.5 6H11V4h5.5c2.209 0 4 1.79 4 4v8.45l2.068-1.93 1.364 1.46-4.432 4.14-4.432-4.14 1.364-1.46 2.068 1.93V8c0-1.1-.896-2-2-2z";
const LIKE =
  "M16.697 5.5c-1.222-.06-2.679.51-3.89 2.16l-.805 1.09-.806-1.09C9.984 6.01 8.526 5.44 7.304 5.5c-1.243.07-2.349.78-2.91 1.91-.552 1.12-.633 2.78.479 4.82 1.074 1.97 3.257 4.27 7.129 6.61 3.87-2.34 6.052-4.64 7.126-6.61 1.111-2.04 1.03-3.7.477-4.82-.561-1.13-1.666-1.84-2.908-1.91zm4.187 7.69c-1.351 2.48-4.001 5.12-8.379 7.67l-.503.3-.504-.3c-4.379-2.55-7.029-5.19-8.382-7.67-1.36-2.5-1.41-4.86-.514-6.67.887-1.79 2.647-2.91 4.601-3.01 1.651-.09 3.368.56 4.798 2.01 1.429-1.45 3.146-2.1 4.796-2.01 1.954.1 3.714 1.22 4.601 3.01.896 1.81.846 4.17-.514 6.67z";
const VIEWS = "M8.75 21V3h2v18h-2zM18 21V8.5h2V21h-2zM4 21l.004-10h2L6 21H4zm9.248 0v-7h2v7h-2z";

export function TweetCard(p: TweetCardProps) {
  const url = p.url || `https://x.com/${p.author.handle}/status/${p.id}`;
  const d = new Date(p.date);
  const stamp = Number.isNaN(d.getTime())
    ? p.date
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) +
      " · " +
      d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" }) +
      "Z";

  return (
    <article className={`tw${p.highlight ? " tw-hi" : ""}`}>
      {p.label && <div className="tw-label">{p.label}</div>}

      <header className="tw-head">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        {p.author.avatar ? (
          <img className="tw-av" src={p.author.avatar} alt="" width={48} height={48} loading="lazy" />
        ) : (
          <div className="tw-av tw-av-none" aria-hidden />
        )}
        <div className="tw-who">
          <span className="tw-name">{p.author.name}</span>
          <span className="tw-handle">@{p.author.handle}</span>
        </div>
        <a className="tw-x" href={url} target="_blank" rel="noopener noreferrer" aria-label="View on X">
          <svg viewBox="0 0 24 24" aria-hidden>
            <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
          </svg>
        </a>
      </header>

      <p className="tw-text">{p.text}</p>

      {p.photo && (
        // eslint-disable-next-line @next/next/no-img-element
        <img className="tw-media" src={p.photo} alt="" loading="lazy" />
      )}

      <div className="tw-time">{stamp}</div>

      <footer className="tw-metrics" title={p.capturedAt ? `captured ${p.capturedAt}` : undefined}>
        <span className="tw-m">
          <Icon d={REPLY} label="replies" />
          {fmtInt(p.replies ?? null)}
        </span>
        <span className="tw-m tw-rt">
          <Icon d={RT} label="reposts" />
          {fmtInt(p.rts ?? null)}
        </span>
        <span className="tw-m tw-like">
          <Icon d={LIKE} label="likes" />
          {fmtInt(p.likes ?? null)}
        </span>
        {p.views != null && (
          <span className="tw-m">
            <Icon d={VIEWS} label="views" />
            {fmtInt(p.views)}
          </span>
        )}
      </footer>
    </article>
  );
}
