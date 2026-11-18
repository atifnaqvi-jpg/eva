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
  // The window count says nothing about this being the first ever conversation: a tenant
  // that was busy last month reads as empty today. §18.2's checklist runs "until the first
  // live conversation", so it is gated on the unfiltered list, not the window.
  const neverLive = recent.length === 0;

  return (
    <main style={styles.page}>
      <h1 style={styles.h1}>
        {bot ? bot.name : "EVA dashboard"}
      </h1>

      {neverLive ? (
        <Onboarding bot={bot} documents={documents} />
      ) : isEmpty ? (
        <p style={styles.hint}>
          No conversations in the last {summary.window.days} days. Nothing is broken —
          the numbers below just cover a quiet window.
        </p>
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

/**
 * Onboarding checklist (ADMN-11, §18.2).
 *
 * §18.2 draws seven steps. Only some of them leave a trace the server can see, and this
 * checklist only ticks a step it can actually observe — a checklist that guesses is worse
 * than none, because it tells an owner they are finished when they are not.
 *
 * - **Observable:** sign up (you are signed in, or there is no page), a chatbot exists
 *   (every tenant gets one at registration), knowledge added (documents exist), knowledge
 *   published (the bot has an active version), the bot customised (it has saved overrides).
 * - **Not observable:** "test in preview" and "copy the snippet". Both happen entirely in
 *   the browser and notify nobody, so they are linked but never ticked. Marking them done
 *   would be a lie; leaving them out would hide the step the owner most needs.
 * - **Not possible yet:** "verify domain and go live". Embedding is blocked on HALT-06 —
 *   no origin is accepted — so the last step is a statement of fact, not a checkbox.
 *
 * "Sign up" and "chatbot exists" tick on every tenant by construction. They stay in the
 * list because §18.2's path starts there and an owner reading it wants the whole path, not
 * the two thirds that can still fail.
 *
 * A step whose data failed to load is `null` (unknown), not `false`: an owner whose
 * documents request failed must not be told they have added nothing.
 *
 * The checklist disappears at the first conversation, which is the condition §18.2 gives.
 */
function Onboarding({
  bot,
  documents,
}: {
  bot: BotConfig | null;
  documents: KnowledgeDocument[] | null;
}) {
  const hasKnowledge = documents === null ? null : documents.length > 0;
  // A set, not one version (RAG-11): a bot with five published documents has a null
  // `active_knowledge_version_id` and is very much published.
  const published =
    bot === null ? null : bot.active_knowledge_version_ids.length > 0;
  const customised =
    bot === null
      ? null
      : bot.welcome_message !== null || Object.keys(bot.appearance).length > 0;

  const steps: { done: boolean | null; label: React.ReactNode }[] = [
    { done: true, label: <>Sign up. Done — you are reading this signed in.</> },
    {
      done: bot !== null,
      label: <>Your chatbot exists, named after your workspace.</>,
    },
    {
      done: hasKnowledge,
      label: (
        <>
          <Link href="/knowledge" style={styles.link}>
            Add something for it to answer from
          </Link>{" "}
          — upload a file, crawl your site, or paste a Q&amp;A.
        </>
      ),
    },
    {
      done: published,
      label: (
        <>
          Publish it. Knowledge stays in draft, and the bot answers only from what is
          published, so this is the step that makes it able to answer at all.
        </>
      ),
    },
    {
      done: false,
      label: (
        <>
          Ask it something on the <Link href="/knowledge" style={styles.link}>Knowledge</Link>{" "}
          page and check the answer. Nothing reports this back, so it never ticks.
        </>
      ),
    },
    {
      done: customised,
      label: (
        <>
          <Link href="/bot" style={styles.link}>
            Make it yours
          </Link>{" "}
          — name, greeting, colour.
        </>
      ),
    },
    {
      done: false,
      label: (
        <>
          Put it on your site. <strong>Not available yet:</strong> no way to verify a site
          is yours has been chosen (HALT-06), so the widget refuses every origin. The
          snippet is ready to copy, but installing it will not work until that is decided.
        </>
      ),
    },
  ];

  const done = steps.filter((step) => step.done === true).length;
  const unknown = steps.filter((step) => step.done === null).length;

  return (
    <section style={styles.setup}>
      <h2 style={styles.h2}>Getting set up</h2>
      <p style={styles.hint}>
        {unknown > 0
          ? `Could not load ${unknown === 1 ? "part of your workspace" : "parts of your workspace"}, so some steps below are marked unknown rather than guessed.`
          : done === 0
            ? "Nothing is configured yet, and the widget has not been used."
            : `${done} of ${steps.length} done. This disappears once the widget is used.`}
      </p>
      <ol style={styles.steps}>
        {steps.map((step, index) => (
          <li key={index} style={step.done === true ? styles.stepDone : styles.step}>
            <span aria-hidden="true">
              {step.done === true ? "✓" : step.done === null ? "?" : "•"}
            </span>{" "}
            {step.label}
            {step.done === null ? (
              <span style={styles.hint}> (unknown — could not load)</span>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
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
  steps: {
    margin: "0.6rem 0 0",
    paddingLeft: "1.1rem",
    fontSize: "0.85rem",
    lineHeight: 1.6,
    color: "#374151",
  },
  step: { marginBottom: "0.25rem" },
  stepDone: { marginBottom: "0.25rem", color: "#6b7280", textDecoration: "line-through" },
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
