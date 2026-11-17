"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  ApiFailure,
  analyticsSummary,
  listBots,
  listConversations,
  listDocuments,
  type AnalyticsSummary,
  type BotConfig,
  type ConversationSummary,
  type KnowledgeDocument,
} from "@/lib/api";

/**
 * Dashboard home (ADMN-02).
 *
 * §18.1 asks for four blocks: usage overview, recent conversations, knowledge status
 * and CSAT. All four now report from real data.
 *
 * The knowledge block counts *documents* and names how many are still processing or
 * have failed, rather than showing a single "documents indexed" number. A count alone
 * says nothing about whether the bot can answer: a tenant with five documents that all
 * failed to process and a tenant with none both read as "5" and "0" while being the
 * same problem.
 */
export default function Page() {
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);
  const [recent, setRecent] = useState<ConversationSummary[] | null>(null);
  const [bot, setBot] = useState<BotConfig | null>(null);
  const [documents, setDocuments] = useState<KnowledgeDocument[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [signedOut, setSignedOut] = useState(false);

  const failed = documents?.filter((doc) => doc.error).length ?? 0;
  const processing = documents?.filter((doc) => doc.status === "processing").length ?? 0;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [analytics, conversations, bots, docs] = await Promise.all([
          analyticsSummary(30),
          listConversations({ limit: 5 }),
          listBots(),
          // A failure here must not blank the page: the rest of the home is still
          // worth showing, so this resolves to null and the block says it is unknown.
          listDocuments().catch(() => null),
        ]);
        if (cancelled) return;
        setSummary(analytics);
        setRecent(conversations.items);
        setBot(bots[0] ?? null);
        setDocuments(docs);
      } catch (failure) {
        if (cancelled) return;
        if (failure instanceof ApiFailure && failure.status === 401) {
          setSignedOut(true);
        } else {
          setError(describe(failure));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (signedOut) {
    return (
      <main style={styles.page}>
        <h1 style={styles.h1}>EVA dashboard</h1>
        <p style={styles.hint}>
          <Link href="/login">Sign in</Link> to see your workspace.
        </p>
      </main>
    );
  }

  if (error) {
    return (
      <main style={styles.page}>
        <h1 style={styles.h1}>EVA dashboard</h1>
        <p role="alert" style={styles.error}>{error}</p>
      </main>
    );
  }

  if (!summary || !recent) {
    return <main style={styles.page}>Loading…</main>;
  }

  const isEmpty = summary.counts.conversations === 0;

  return (
    <main style={styles.page}>
      <h1 style={styles.h1}>
        {bot ? bot.name : "EVA dashboard"}
      </h1>

      {isEmpty ? (
        <section style={styles.setup}>
          <h2 style={styles.h2}>Nothing here yet</h2>
          <p style={styles.hint}>
            No conversations in the last {summary.window.days} days. The bot is live —
            once someone opens the widget, this page fills in.
          </p>
          <p style={styles.hint}>
            Next: <Link href="/bot">configure the chatbot</Link> and copy its embed
            snippet, then <Link href="/knowledge">add something for it to answer from</Link>.
          </p>
        </section>
      ) : null}

      <section style={styles.section}>
        <h2 style={styles.h2}>Last {summary.window.days} days</h2>
        <div style={styles.cards}>
          <Card label="Conversations" value={summary.counts.conversations} />
          <Card
            label="Visitor messages"
            value={summary.counts.visitor_messages}
          />
          <Card
            label="Handed to a human"
            value={summary.counts.handed_over_conversations}
            note="of which the bot did not resolve"
          />
          <Card
            label="Satisfaction"
            value={
              summary.csat.votes === 0
                ? "—"
                : `${Math.round((summary.csat.up / summary.csat.votes) * 100)}%`
            }
            note={
              summary.csat.votes === 0
                ? "no votes yet"
                : `${summary.csat.votes} vote${summary.csat.votes === 1 ? "" : "s"}`
            }
          />
        </div>
        <p style={styles.sub}>
          Definitions travel with these numbers on the{" "}
          <Link href="/analytics">Analytics</Link> page.
        </p>
      </section>

      <section style={styles.section}>
        <h2 style={styles.h2}>Recent conversations</h2>
        {recent.length === 0 ? (
          <p style={styles.hint}>None in this window.</p>
        ) : (
          <ul style={styles.list}>
            {recent.map((conversation) => (
              <li key={conversation.id} style={styles.listItem}>
                <Link href={`/conversations/${conversation.id}`} style={styles.link}>
                  {/* A visitor who never gave a name is not "Anonymous" — that is a
                      claim about them. The id prefix says only "this is a distinct
                      conversation", which is all the record supports. */}
                  {conversation.visitor_name ?? `Conversation ${conversation.id.slice(0, 8)}`}
                </Link>
                <span style={styles.sub}>
                  {conversation.status.replace("_", " ")} ·{" "}
                  {new Date(conversation.started_at).toLocaleString()}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p style={styles.sub}>
          <Link href="/conversations">All conversations</Link>
        </p>
      </section>

      <section style={styles.section}>
        <h2 style={styles.h2}>Knowledge</h2>
        {documents === null ? (
          <p style={styles.hint}>Could not read knowledge status.</p>
        ) : documents.length === 0 ? (
          <p style={styles.hint}>
            Nothing added yet. <Link href="/knowledge" style={styles.link}>Add a document</Link> so the
            bot can answer from your own content instead of only the greeting.
          </p>
        ) : (
          <p style={styles.hint}>
            {documents.length} document{documents.length === 1 ? "" : "s"}
            {failed > 0 ? `, ${failed} with a processing error` : ""}
            {processing > 0 ? `, ${processing} still processing` : ""}.{" "}
            <Link href="/knowledge" style={styles.link}>Manage</Link>
          </p>
        )}
      </section>
    </main>
  );
}

function Card({
  label,
  value,
  note,
}: {
  label: string;
  value: number | string;
  note?: string;
}) {
  return (
    <div style={styles.card}>
      <span style={styles.cardValue}>{value}</span>
      <span style={styles.cardLabel}>{label}</span>
      {note ? <span style={styles.cardNote}>{note}</span> : null}
    </div>
  );
}

function describe(failure: unknown): string {
  if (failure instanceof ApiFailure) return `${failure.code}: ${failure.message}`;
  return "Could not reach the server.";
}

const styles: Record<string, React.CSSProperties> = {
  page: { fontFamily: "system-ui, sans-serif", padding: "1.5rem 2rem", maxWidth: 900 },
  h1: { fontSize: "1.4rem", margin: "0 0 1.25rem" },
  h2: { fontSize: "1rem", margin: "0 0 0.75rem", color: "#111827" },
  section: { marginBottom: "2rem" },
  setup: {
    border: "1px solid #e5e7eb",
    borderRadius: 6,
    padding: "1rem 1.25rem",
    marginBottom: "1.75rem",
    background: "#f9fafb",
  },
  cards: { display: "flex", gap: "0.75rem", flexWrap: "wrap" },
  card: {
    display: "flex",
    flexDirection: "column",
    gap: "0.15rem",
    border: "1px solid #e5e7eb",
    borderRadius: 6,
    padding: "0.75rem 1rem",
    minWidth: 150,
  },
  cardValue: { fontSize: "1.5rem", fontWeight: 600, lineHeight: 1.1 },
  cardLabel: { fontSize: "0.78rem", color: "#374151" },
  cardNote: { fontSize: "0.72rem", color: "#6b7280" },
  list: { listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: "0.5rem" },
  listItem: { display: "flex", flexDirection: "column", gap: "0.1rem" },
  link: { color: "#1d4ed8", textDecoration: "none", fontSize: "0.9rem" },
  hint: { color: "#4b5563", fontSize: "0.85rem", margin: "0 0 0.5rem" },
  sub: { color: "#6b7280", fontSize: "0.78rem", margin: "0.5rem 0 0" },
  error: { color: "#991b1b", background: "#fee2e2", padding: "0.5rem 0.7rem", borderRadius: 4, fontSize: "0.85rem" },
};
