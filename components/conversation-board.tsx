import type { BoardEdge, BoardNode, ConversationBoard } from "@/lib/model";
import { fmtInt } from "@/lib/format";

/**
 * The board groups posts into connected clusters using X's own
 * `referenced_tweets` relationships — replies and quotes — never inferred from
 * text. A cluster is one weakly-connected component of that graph.
 *
 * Two honesty rules the markup has to keep:
 *  - a node with resolved=false was referenced but never fetched. It renders as
 *    an explicit "not fetched" endpoint, never as a post with unknown content.
 *  - edge direction matters: "replied to" and "quoted" are different acts and
 *    are labelled separately rather than collapsed into "related".
 */

const KIND_LABEL: Record<string, string> = {
  reply: "replied to",
  quote: "quoted",
  retweet: "reposted",
};

function clusters(nodes: BoardNode[], edges: BoardEdge[]) {
  const parent = new Map<string, string>();
  const find = (x: string): string => {
    let r = x;
    while (parent.get(r) && parent.get(r) !== r) r = parent.get(r)!;
    return r;
  };
  nodes.forEach((n) => parent.set(n.id, n.id));
  edges.forEach((e) => {
    const a = find(e.from);
    const b = find(e.to);
    if (a && b && a !== b) parent.set(a, b);
  });

  const groups = new Map<string, { nodes: BoardNode[]; edges: BoardEdge[] }>();
  nodes.forEach((n) => {
    const k = find(n.id);
    if (!groups.has(k)) groups.set(k, { nodes: [], edges: [] });
    groups.get(k)!.nodes.push(n);
  });
  edges.forEach((e) => {
    const k = find(e.from);
    if (groups.has(k)) groups.get(k)!.edges.push(e);
  });

  return [...groups.values()]
    .map((g) => ({
      ...g,
      nodes: [...g.nodes].sort((a, b) => (a.created_at || "").localeCompare(b.created_at || "")),
    }))
    // Clusters that carry a key moment first, then the most connected.
    .sort((a, b) => {
      const km = Number(b.nodes.some((n) => n.is_key_moment)) - Number(a.nodes.some((n) => n.is_key_moment));
      if (km) return km;
      if (b.edges.length !== a.edges.length) return b.edges.length - a.edges.length;
      return (b.nodes[0]?.created_at || "").localeCompare(a.nodes[0]?.created_at || "");
    });
}

function when(iso: string | null) {
  if (!iso) return "";
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)}Z`;
}

function Post({ n, edgeOut }: { n: BoardNode; edgeOut?: BoardEdge }) {
  if (!n.resolved) {
    return (
      <li className="cb-post cb-unresolved">
        <div className="cb-meta mono">not fetched</div>
        <p className="cb-unres-text">
          Referenced by a post above, but never retrieved. We know this post exists — not
          what it said.
        </p>
        <a
          className="mono cb-link"
          href={`https://x.com/i/status/${n.id}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          open on X ↗
        </a>
      </li>
    );
  }
  const m = n.metrics || {};
  return (
    <li className={`cb-post${n.is_key_moment ? " cb-key" : ""}`}>
      <div className="cb-meta">
        <span className="cb-who mono">@{n.author_handle || "unknown"}</span>
        <span className="cb-when mono">{when(n.created_at)}</span>
        {n.is_key_moment && <span className="badge badge-partial">key moment</span>}
        {edgeOut && (
          <span className="cb-rel mono">
            {KIND_LABEL[edgeOut.kind] || edgeOut.kind} @{edgeOut.to_handle || "—"}
          </span>
        )}
      </div>
      {/* Internal collector placeholders must never reach the page. */}
      {n.label && !/model\.json|TODO|placeholder/i.test(n.label) && (
        <div className="cb-label">{n.label}</div>
      )}
      <p className="cb-text">{n.text}</p>
      <div className="cb-foot">
        <span className="mono">♥ {fmtInt(m.likes ?? null)}</span>
        <span className="mono">↺ {fmtInt(m.retweets ?? null)}</span>
        <span className="mono">💬 {fmtInt(m.replies ?? null)}</span>
        {m.views != null && <span className="mono">👁 {fmtInt(m.views)}</span>}
        <a
          className="mono cb-link"
          href={`https://x.com/${n.author_handle || "i"}/status/${n.id}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          open ↗
        </a>
      </div>
    </li>
  );
}

/** Clusters worth showing. The one-hop expansion pulls in parent posts for
 *  context, which means a cluster can be mostly off-topic chatter hanging off a
 *  single $TOAD reply. Require a key moment, or real $TOAD substance, or an
 *  actual branching conversation — otherwise the board is long rather than
 *  informative. */
const MAX_CLUSTERS = 18;

export function ConversationBoardView({ board }: { board: ConversationBoard }) {
  const all = clusters(board.nodes, board.edges);
  const worth = all.filter((g) => {
    const km = g.nodes.some((n) => n.is_key_moment);
    const toad = g.nodes.filter((n) => n.toad_relevant).length;
    return km || toad >= 2 || g.edges.length >= 2;
  });
  const groups = worth.slice(0, MAX_CLUSTERS);
  const hidden = all.length - groups.length;
  const outFrom = new Map<string, BoardEdge>();
  board.edges.forEach((e) => {
    if (!outFrom.has(e.from)) outFrom.set(e.from, e);
  });
  const s = board.stats || {};

  return (
    <>
      <div className="cb-stats mono">
        <span>{fmtInt(s.node_count)} posts</span>
        <span>{fmtInt(s.edge_count)} connections</span>
        <span>{s.by_kind?.reply ?? 0} replies</span>
        <span>{s.by_kind?.quote ?? 0} quotes</span>
        <span>{fmtInt(s.key_moments)} key moments</span>
      </div>

      <div className="cb-grid">
        {groups.map((g, i) => (
          <section className="cb-cluster" key={g.nodes[0]?.id ?? i}>
            <header className="cb-cluster-head mono">
              {g.nodes.length} post{g.nodes.length === 1 ? "" : "s"} ·{" "}
              {g.edges.length} connection{g.edges.length === 1 ? "" : "s"}
            </header>
            <ol className="cb-thread">
              {g.nodes.map((n) => (
                <Post key={n.id} n={n} edgeOut={outFrom.get(n.id)} />
              ))}
            </ol>
          </section>
        ))}
      </div>

      <p className="note">
        <strong>How this is built:</strong> {board.method} {board.caveat}
        {hidden > 0 && (
          <>
            {" "}
            Showing {groups.length} of {all.length} clusters; {hidden} are single replies with
            no $TOAD substance and are omitted from the board, not from the data.
          </>
        )}
      </p>
    </>
  );
}
