"use client";

import { useCallback, useEffect, useState } from "react";

import {
  analyticsKnowledgeGaps,
  analyticsSummary,
  ApiFailure,
  type AnalyticsSummary,
  type KnowledgeGapReport,
} from "@/lib/api";

const WINDOWS = [7, 30, 90];

export default function AnalyticsPage() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<AnalyticsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Loaded separately: the gap list waits on the embedding service, so a slow or
  // unavailable embedder must not blank the summary that is already on screen.
  const [gapData, setGapData] = useState<KnowledgeGapReport | null>(null);
  const [gapsError, setGapsError] = useState<string | null>(null);
  const [gapsLoading, setGapsLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await analyticsSummary(days));
    } catch (failure) {
      if (failure instanceof ApiFailure && failure.status === 401) {
        setError("Log in to view analytics.");
      } else if (failure instanceof ApiFailure) {
        setError(`${failure.code}: ${failure.message}`);
      } else {
        setError("Could not load analytics.");
      }
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [days]);

  const loadGaps = useCallback(async () => {
    setGapsLoading(true);
    setGapsError(null);
    try {
      setGapData(await analyticsKnowledgeGaps(days));
    } catch (failure) {
      if (failure instanceof ApiFailure && failure.status === 401) {
        setGapsError("Log in to view knowledge gaps.");
      } else if (failure instanceof ApiFailure) {
        setGapsError(`${failure.code}: ${failure.message}`);
      } else {
        setGapsError("Could not load knowledge gaps.");
      }
      setGapData(null);
    } finally {
      setGapsLoading(false);
    }
  }, [days]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void loadGaps();
  }, [loadGaps]);

  return (
    <main style={styles.page}>
      <h1 style={styles.h1}>Analytics</h1>

      <div style={styles.windows}>
        {WINDOWS.map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => setDays(option)}
            style={{ ...styles.button, ...(option === days ? styles.buttonActive : {}) }}
          >
            {option} days
          </button>
        ))}
      </div>

      {error ? (
        <p role="alert" style={styles.error}>
          {error}
        </p>
      ) : null}
      {loading ? <p style={styles.muted}>Loading…</p> : null}

      {data ? (
        <>
          <p style={styles.muted}>
            {new Date(data.window.from).toISOString().slice(0, 10)} →{" "}
            {new Date(data.window.to).toISOString().slice(0, 10)} · {data.window.timezone} ·{" "}
            {data.window.basis}
          </p>

          <section style={styles.cards}>
            <Card label="Conversations" value={data.counts.conversations} />
            <Card label="Visitor messages" value={data.counts.visitor_messages} />
            <Card label="Assistant messages" value={data.counts.assistant_messages} />
            <Card label="Handed over" value={data.counts.handed_over_conversations} />
          </section>

          <section style={styles.section}>
            <h2 style={styles.h2}>Resolution proxies</h2>
            <p style={styles.caveat}>
              This product has no ticket-close signal, so there is no true resolution
              rate. These are proxies:
            </p>
            <table style={styles.table}>
              <tbody>
                <tr>
                  <td style={styles.td}>Answered turn rate</td>
                  <td style={styles.td}>
                    <strong>{formatPct(data.resolution_proxies.answered_turn_rate)}</strong>
                  </td>
                  <td style={styles.definition}>
                    {data.resolution_proxies.definitions.answered_turn_rate}
                  </td>
                </tr>
                <tr>
                  <td style={styles.td}>Handover rate</td>
                  <td style={styles.td}>
                    <strong>{formatPct(data.resolution_proxies.handover_rate)}</strong>
                  </td>
                  <td style={styles.definition}>
                    {data.resolution_proxies.definitions.handover_rate}
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          <section style={styles.section}>
            <h2 style={styles.h2}>Latency</h2>
            <p style={styles.caveat}>{data.latency_ms.definition}</p>
            <section style={styles.cards}>
              <Card label="p50 (ms)" value={formatNumber(data.latency_ms.p50, 0)} />
              <Card label="p95 (ms)" value={formatNumber(data.latency_ms.p95, 0)} />
              <Card label="Samples" value={data.latency_ms.samples} />
            </section>
          </section>

          <section style={styles.section}>
            <h2 style={styles.h2}>Tokens</h2>
            <section style={styles.cards}>
              <Card label="Prompt" value={data.tokens.prompt.toLocaleString()} />
              <Card label="Completion" value={data.tokens.completion.toLocaleString()} />
              <Card label="Total" value={data.tokens.total.toLocaleString()} />
            </section>
          </section>

          <section style={styles.section}>
            <h2 style={styles.h2}>CSAT</h2>
            <section style={styles.cards}>
              <Card label="Positive" value={formatPct(data.csat.positive_pct)} />
              <Card label="Thumbs up" value={data.csat.up} />
              <Card label="Thumbs down" value={data.csat.down} />
              <Card label="Votes" value={data.csat.votes} />
            </section>
          </section>

          <section style={styles.section}>
            <h2 style={styles.h2}>Top questions</h2>
            <p style={styles.caveat}>{data.definitions.top_questions}</p>
            {data.top_questions.length === 0 ? (
              <p style={styles.muted}>No visitor questions in this window.</p>
            ) : (
              <table style={styles.table}>
                <tbody>
                  {data.top_questions.map((row) => (
                    <tr key={row.question}>
                      <td style={styles.td}>{row.question}</td>
                      <td style={styles.td}>{row.occurrences}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section style={styles.section}>
            <h2 style={styles.h2}>Knowledge gaps</h2>
            <p style={styles.caveat}>
              Questions the bot declined, merged into topics. This is the list to write
              content for.
            </p>
            {gapsError ? (
              <p role="alert" style={styles.error}>
                {gapsError}
              </p>
            ) : null}
            {gapsLoading ? <p style={styles.muted}>Loading gaps…</p> : null}
            {gapData ? (
              <>
                <section style={styles.cards}>
                  <Card label="Declined turns" value={gapData.declined_turns} />
                  <Card label="Distinct questions" value={gapData.distinct_questions} />
                  <Card label="Gap topics" value={gapData.gap_count} />
                </section>
                <p style={styles.caveat}>{gapData.definitions.clustering}</p>
                <p style={styles.caveat}>{gapData.definitions.ranking}</p>
                {gapData.note ? (
                  <p style={styles.caveat}>{gapData.note}</p>
                ) : null}
                {gapData.gaps.length === 0 ? (
                  <p style={styles.muted}>
                    No declined questions in this window — either everything was
                    answered, or nothing was asked.
                  </p>
                ) : (
                  <table style={styles.table}>
                    <tbody>
                      {gapData.gaps.map((gap) => (
                        <tr key={gap.question}>
                          <td style={styles.td}>
                            {gap.question}
                            {gap.distinct_phrasings > 1 ? (
                              <span style={styles.definition}>
                                {" "}
                                +{gap.distinct_phrasings - 1} other phrasing
                                {gap.distinct_phrasings > 2 ? "s" : ""}
                              </span>
                            ) : null}
                          </td>
                          <td style={styles.td}>{gap.occurrences}</td>
                          <td style={styles.definition}>
                            last {new Date(gap.last_seen).toISOString().slice(0, 10)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            ) : null}
          </section>
        </>
      ) : null}
    </main>
  );
}

function Card({ label, value }: { label: string; value: string | number }) {
  return (
    <div style={styles.card}>
      <div style={styles.cardLabel}>{label}</div>
      <div style={styles.cardValue}>{value}</div>
    </div>
  );
}

function formatPct(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function formatNumber(value: number | null, digits: number): string {
  return value === null ? "—" : value.toFixed(digits);
}

const styles: Record<string, React.CSSProperties> = {
  page: { fontFamily: "system-ui, sans-serif", padding: "1.5rem 2rem", maxWidth: 1000 },
  h1: { fontSize: "1.35rem", margin: "0 0 1rem" },
  h2: { fontSize: "1rem", margin: "0 0 0.35rem" },
  windows: { display: "flex", gap: "0.5rem", marginBottom: "0.75rem" },
  button: {
    padding: "0.35rem 0.7rem",
    border: "1px solid #d1d5db",
    background: "#f9fafb",
    borderRadius: 4,
    cursor: "pointer",
    fontSize: "0.85rem",
  },
  buttonActive: { background: "#111827", color: "#ffffff", borderColor: "#111827" },
  cards: { display: "flex", gap: "0.75rem", flexWrap: "wrap", margin: "0.5rem 0 0" },
  card: {
    border: "1px solid #e5e7eb",
    borderRadius: 6,
    padding: "0.6rem 0.9rem",
    minWidth: 120,
  },
  cardLabel: { fontSize: "0.75rem", color: "#6b7280" },
  cardValue: { fontSize: "1.25rem", fontWeight: 600 },
  section: { marginTop: "1.75rem" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" },
  td: { padding: "0.35rem 0.4rem", borderBottom: "1px solid #f3f4f6", verticalAlign: "top" },
  definition: { color: "#6b7280", fontSize: "0.78rem" },
  caveat: { color: "#6b7280", fontSize: "0.8rem", margin: "0.25rem 0" },
  muted: { color: "#6b7280", fontSize: "0.85rem" },
  error: {
    color: "#991b1b",
    background: "#fee2e2",
    padding: "0.6rem 0.8rem",
    borderRadius: 4,
  },
};
