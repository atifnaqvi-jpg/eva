"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { HandoverBadge } from "../components/handover-badge";
import {
  ApiFailure,
  listConversations,
  type ConversationList,
} from "@/lib/api";

const PAGE_SIZE = 25;

export default function ConversationsPage() {
  const [status, setStatus] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ConversationList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // A date input yields `YYYY-MM-DD`, which the API rejects as a naive
      // timestamp. Midnight UTC is the only unambiguous reading of "that day".
      const list = await listConversations({
        status: status || undefined,
        from: from ? `${from}T00:00:00Z` : undefined,
        to: to ? `${to}T23:59:59Z` : undefined,
        limit: PAGE_SIZE,
        offset,
      });
      setData(list);
    } catch (failure) {
      if (failure instanceof ApiFailure && failure.status === 401) {
        setError("Log in to view conversations.");
      } else if (failure instanceof ApiFailure) {
        setError(`${failure.code}: ${failure.message}`);
      } else {
        setError("Could not load conversations.");
      }
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [status, from, to, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <main style={styles.page}>
      <h1 style={styles.h1}>Conversations</h1>

      <form
        style={styles.filters}
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
          void load();
        }}
      >
        <label style={styles.field}>
          Status
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            style={styles.input}
          >
            <option value="">Any</option>
            <option value="active">active</option>
            <option value="handed_over">handed_over</option>
            <option value="closed">closed</option>
          </select>
        </label>
        <label style={styles.field}>
          From
          <input
            type="date"
            value={from}
            onChange={(event) => setFrom(event.target.value)}
            style={styles.input}
          />
        </label>
        <label style={styles.field}>
          To
          <input
            type="date"
            value={to}
            onChange={(event) => setTo(event.target.value)}
            style={styles.input}
          />
        </label>
        <button type="submit" style={styles.button}>
          Apply
        </button>
      </form>

      {error ? (
        <p role="alert" style={styles.error}>
          {error}
        </p>
      ) : null}

      {loading ? <p style={styles.muted}>Loading…</p> : null}

      {!loading && data && data.items.length === 0 ? (
        <p style={styles.empty}>
          No conversations in this range. Conversations appear here once a visitor
          talks to the bot on your site.
        </p>
      ) : null}

      {data && data.items.length > 0 ? (
        <>
          <table style={styles.table}>
            <thead>
              <tr>
                <th style={styles.th}>Started</th>
                <th style={styles.th}>Visitor</th>
                <th style={styles.th}>Status</th>
                <th style={styles.th}>Handover</th>
                <th style={styles.th}>Messages</th>
                <th style={styles.th}>CSAT</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
                <tr key={item.id}>
                  <td style={styles.td}>
                    <Link href={`/conversations/${item.id}`} style={styles.link}>
                      {new Date(item.started_at).toISOString().replace("T", " ").slice(0, 16)}
                    </Link>
                  </td>
                  <td style={styles.td}>{item.visitor_name ?? item.visitor_email ?? "—"}</td>
                  <td style={styles.td}>{item.status}</td>
                  <td style={styles.td}>
                    <HandoverBadge badge={item.handover} />
                  </td>
                  <td style={styles.td}>{item.message_count}</td>
                  <td style={styles.td}>
                    <span title="thumbs up / thumbs down">
                      {item.csat_up} / {item.csat_down}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div style={styles.pager}>
            <button
              type="button"
              style={styles.button}
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </button>
            <span style={styles.muted}>
              {offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} of {data.total}
            </span>
            <button
              type="button"
              style={styles.button}
              disabled={offset + PAGE_SIZE >= data.total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </button>
          </div>
        </>
      ) : null}
    </main>
  );
}

const styles: Record<string, React.CSSProperties> = {
  page: { fontFamily: "system-ui, sans-serif", padding: "1.5rem 2rem", maxWidth: 1100 },
  h1: { fontSize: "1.35rem", margin: "0 0 1rem" },
  filters: { display: "flex", gap: "0.75rem", alignItems: "flex-end", marginBottom: "1rem" },
  field: { display: "flex", flexDirection: "column", fontSize: "0.8rem", color: "#374151" },
  input: { marginTop: "0.2rem", padding: "0.35rem", border: "1px solid #d1d5db", borderRadius: 4 },
  button: {
    padding: "0.4rem 0.8rem",
    border: "1px solid #d1d5db",
    background: "#f9fafb",
    borderRadius: 4,
    cursor: "pointer",
  },
  table: { width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" },
  th: {
    textAlign: "left",
    borderBottom: "1px solid #e5e7eb",
    padding: "0.5rem 0.4rem",
    fontWeight: 600,
    color: "#374151",
  },
  td: { borderBottom: "1px solid #f3f4f6", padding: "0.5rem 0.4rem" },
  link: { color: "#1d4ed8", textDecoration: "none" },
  pager: { display: "flex", gap: "0.75rem", alignItems: "center", marginTop: "1rem" },
  muted: { color: "#6b7280", fontSize: "0.85rem" },
  empty: { color: "#374151", padding: "1.5rem 0" },
  error: { color: "#991b1b", background: "#fee2e2", padding: "0.6rem 0.8rem", borderRadius: 4 },
};
