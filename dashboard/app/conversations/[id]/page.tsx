"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";

import { HandoverBadge } from "../../components/handover-badge";
import {
  ApiFailure,
  exportUrl,
  getConversation,
  type ConversationDetail,
} from "@/lib/api";

export default function TranscriptPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await getConversation(id));
    } catch (failure) {
      if (failure instanceof ApiFailure && failure.status === 404) {
        // The API answers 404 for "belongs to another tenant" and "does not exist"
        // alike, so the screen must not claim which one it was.
        setError("This conversation was not found.");
      } else if (failure instanceof ApiFailure && failure.status === 401) {
        setError("Log in to view this conversation.");
      } else if (failure instanceof ApiFailure) {
        setError(`${failure.code}: ${failure.message}`);
      } else {
        setError("Could not load this conversation.");
      }
      setDetail(null);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) return <main style={styles.page}>Loading…</main>;
  if (error)
    return (
      <main style={styles.page}>
        <p role="alert" style={styles.error}>
          {error}
        </p>
        <Link href="/conversations" style={styles.link}>
          Back to conversations
        </Link>
      </main>
    );
  if (!detail) return null;

  const { summary, turns } = detail;

  return (
    <main style={styles.page}>
      <Link href="/conversations" style={styles.link}>
        ← Conversations
      </Link>
      <h1 style={styles.h1}>
        Conversation {summary.id.slice(0, 8)}
        <HandoverBadge badge={summary.handover} />
      </h1>

      <dl style={styles.meta}>
        <dt style={styles.dt}>Visitor</dt>
        <dd style={styles.dd}>{summary.visitor_name ?? "—"}</dd>
        <dt style={styles.dt}>Email</dt>
        <dd style={styles.dd}>{summary.visitor_email ?? "—"}</dd>
        <dt style={styles.dt}>Started</dt>
        <dd style={styles.dd}>{summary.started_at}</dd>
        <dt style={styles.dt}>Status</dt>
        <dd style={styles.dd}>{summary.status}</dd>
        <dt style={styles.dt}>CSAT</dt>
        <dd style={styles.dd}>
          {summary.csat_up} up / {summary.csat_down} down
        </dd>
        <dt style={styles.dt}>Page</dt>
        <dd style={styles.dd}>{summary.page_url ?? "—"}</dd>
      </dl>

      <p style={styles.actions}>
        <a href={exportUrl(summary.id, "txt")} style={styles.button}>
          Export .txt
        </a>
        <a href={exportUrl(summary.id, "json")} style={styles.button}>
          Export .json
        </a>
        <span style={styles.note}>
          The export contains the full record, not just this transcript.
        </span>
      </p>

      <ol style={styles.turns}>
        {turns.map((turn) => (
          <li key={turn.id} style={styles.turn}>
            <div style={styles.turnHead}>
              <strong>{turn.role}</strong>
              <span style={styles.muted}>{turn.created_at}</span>
              {turn.confidence !== null ? (
                <span style={styles.muted}>
                  confidence {turn.confidence.toFixed(2)}
                </span>
              ) : null}
              {turn.latency_ms !== null ? (
                <span style={styles.muted}>{turn.latency_ms} ms</span>
              ) : null}
              {turn.feedback ? (
                <span
                  style={turn.feedback === "down" ? styles.voteDown : styles.voteUp}
                  title={turn.feedback_comment ?? undefined}
                >
                  {turn.feedback === "up" ? "👍" : "👎"}
                </span>
              ) : null}
            </div>
            <p style={styles.content}>{turn.content}</p>
            {turn.citations.length > 0 ? (
              <p style={styles.muted}>
                Sources:{" "}
                {turn.citations
                  .map((citation) =>
                    typeof citation === "object" && citation !== null
                      ? String(
                          (citation as Record<string, unknown>).title ??
                            (citation as Record<string, unknown>).document_title ??
                            "untitled",
                        )
                      : String(citation),
                  )
                  .join(", ")}
              </p>
            ) : null}
            {turn.feedback_comment ? (
              <p style={styles.comment}>Visitor comment: {turn.feedback_comment}</p>
            ) : null}
          </li>
        ))}
      </ol>
    </main>
  );
}

const styles: Record<string, React.CSSProperties> = {
  page: { fontFamily: "system-ui, sans-serif", padding: "1.5rem 2rem", maxWidth: 900 },
  h1: {
    fontSize: "1.35rem",
    margin: "0.75rem 0",
    display: "flex",
    gap: "0.6rem",
    alignItems: "center",
  },
  meta: {
    display: "grid",
    gridTemplateColumns: "6rem 1fr",
    gap: "0.25rem 1rem",
    fontSize: "0.875rem",
    margin: "0 0 1rem",
  },
  dt: { color: "#6b7280" },
  dd: { margin: 0 },
  actions: { display: "flex", gap: "0.6rem", alignItems: "center", margin: "0 0 1.5rem" },
  button: {
    padding: "0.35rem 0.75rem",
    border: "1px solid #d1d5db",
    borderRadius: 4,
    background: "#f9fafb",
    color: "#111827",
    textDecoration: "none",
    fontSize: "0.85rem",
  },
  turns: { listStyle: "none", padding: 0, margin: 0 },
  turn: { borderTop: "1px solid #f3f4f6", padding: "0.75rem 0" },
  turnHead: { display: "flex", gap: "0.75rem", alignItems: "center", fontSize: "0.85rem" },
  content: { margin: "0.4rem 0", whiteSpace: "pre-wrap" },
  muted: { color: "#6b7280", fontSize: "0.8rem" },
  voteUp: { color: "#166534" },
  voteDown: { color: "#991b1b" },
  comment: { margin: "0.3rem 0 0", fontSize: "0.85rem", color: "#92400e" },
  note: { color: "#6b7280", fontSize: "0.8rem" },
  link: { color: "#1d4ed8", textDecoration: "none", fontSize: "0.85rem" },
  error: {
    color: "#991b1b",
    background: "#fee2e2",
    padding: "0.6rem 0.8rem",
    borderRadius: 4,
  },
};
