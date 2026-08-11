import { loadModel } from "@/lib/model";
import { fmtInt } from "@/lib/format";
import { ConversationBoardView } from "@/components/conversation-board";

export const dynamic = "force-static";

export default function ReceiptsPage() {
  const model = loadModel();

  return (
    <>
      <section className="page-hero">
        <div className="wrap">
          <div className="kicker">
            The Receipts
          </div>
          <h1>Tweets that made the toad hop</h1>
          <p>
            Key moments from @mdudas, the deployer and @eltoadpepe, and how the posts connect
            to one another. Connections are X&rsquo;s own reply and quote relationships, not
            inferred from text.
          </p>
        </div>
      </section>
      <div className="page-body">
        <div className="wrap">
          <div className="section-head">
            <div className="kicker">Key moments</div>
            <h2>The story in posts</h2>
          </div>
          <div className="tweet-grid">
            {model.tweets.map((t) => (
              <a
                key={t.id}
                href={t.url}
                target="_blank"
                rel="noopener noreferrer"
                className="tweet-card"
              >
                <div className="meta">
                  <span className="who">
                    {t.author.name} · @{t.author.handle}
                  </span>
                  <span className="when mono">{t.date.slice(0, 10)}</span>
                </div>
                <div className="body">{t.text}</div>
                <div className="eng">
                  <span>♥ {fmtInt(t.likes)}</span>
                  <span>💬 {fmtInt(t.replies)}</span>
                </div>
              </a>
            ))}
          </div>

          {model.conversation_board && model.conversation_board.nodes.length > 0 && (
            <div style={{ marginTop: "var(--s8)" }}>
              <div className="section-head">
                <div className="kicker">The board</div>
                <h2>How the posts connect</h2>
              </div>
              <ConversationBoardView board={model.conversation_board} />
            </div>
          )}

          {model.viral_status && model.viral.length === 0 && (
            <div className="caveats" style={{ marginTop: "var(--s7)" }}>
              <summary style={{ listStyle: "none", fontWeight: 700 }}>
                Why there is no viral leaderboard yet
              </summary>
              <p style={{ marginTop: "var(--s3)", fontSize: "var(--t-meta)", color: "var(--muted)", lineHeight: 1.65 }}>
                {model.viral_status.note}
              </p>
            </div>
          )}

          {model.quotes.length > 0 && (
            <div style={{ marginTop: 48 }}>
              <div className="section-head">
                <div className="kicker">Pull quotes</div>
                <h2>Words that stuck</h2>
              </div>
              {model.quotes.map((q, i) => (
                <div className="q" key={i}>
                  &ldquo;{q.text}&rdquo;
                  <b>
                    {q.who}
                    {q.context ? ` · ${q.context}` : ""}
                  </b>
                </div>
              ))}
            </div>
          )}

          {model.open_questions.length > 0 && (
            <div style={{ marginTop: 48 }}>
              <div className="section-head">
                <div className="kicker">Open questions</div>
                <h2>Still tracing</h2>
              </div>
              <ul style={{ paddingLeft: 20, color: "var(--muted)", lineHeight: 1.8 }}>
                {model.open_questions.map((q, i) => (
                  <li key={i}>{q}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
