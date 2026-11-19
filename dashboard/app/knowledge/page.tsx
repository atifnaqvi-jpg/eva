"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiFailure,
  createCrawl,
  createFaq,
  listBots,
  listDocuments,
  previewAnswer,
  publishVersion,
  rollbackKnowledge,
  uploadDocument,
  type BotConfig,
  type KnowledgeDocument,
  type PreviewResult,
} from "@/lib/api";

type Mode = "upload" | "crawl" | "faq";

const MODES: { id: Mode; label: string }[] = [
  { id: "upload", label: "Upload a file" },
  { id: "crawl", label: "Crawl a site" },
  { id: "faq", label: "Add a Q&A" },
];

export default function KnowledgePage() {
  const [mode, setMode] = useState<Mode>("upload");
  const [documents, setDocuments] = useState<KnowledgeDocument[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [bot, setBot] = useState<BotConfig | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const fileInput = useRef<HTMLInputElement | null>(null);

  const [testQuestion, setTestQuestion] = useState("");
  const [draft, setDraft] = useState<PreviewResult | null>(null);
  const [live, setLive] = useState<PreviewResult | null>(null);
  const [testing, setTesting] = useState(false);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setError(null);
    try {
      // Both are needed to render the table: the documents say what could be live,
      // the bot says which version actually is.
      const [docs, bots] = await Promise.all([listDocuments(), listBots()]);
      setDocuments(docs);
      setBot(bots[0] ?? null);
    } catch (failure) {
      if (quiet) throw failure;
      if (failure instanceof ApiFailure && failure.status === 401) {
        setError("Log in to see your knowledge sources.");
      } else if (failure instanceof ApiFailure) {
        setError(`${failure.code}: ${failure.message}`);
      } else {
        setError("Could not load documents.");
      }
      setDocuments(null);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Processing happens in the worker, so the table has no event to wait on. Without
  // this the row sits on "processing" forever and an owner reloads to find out — or
  // believes an upload failed when it did not. Poll only while something is actually
  // processing, and quietly: a failed poll must not replace a good table with an error.
  const pending = documents?.some((doc) => doc.status === "processing") ?? false;
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => {
      void load(true).catch(() => {});
    }, 4000);
    return () => clearInterval(timer);
  }, [pending, load]);

  async function publish(doc: KnowledgeDocument) {
    if (!bot || !doc.version_id) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await publishVersion(bot.id, doc.version_id);
      // Publishing is additive (RAG-11): the bot serves its whole knowledge set, so the
      // honest thing to report is how many documents that now covers. Saying "only
      // document" here was true before the set pointer and would now be a lie.
      const served = result.active_knowledge_version_ids.length;
      setNotice(
        served > 1
          ? `“${doc.title}” is live. The bot now answers from ${served} documents.`
          : `“${doc.title}” is live. Visitors can be answered from it now.`,
      );
      await load();
    } catch (failure) {
      if (failure instanceof ApiFailure) {
        setError(
          failure.code === "version_not_ready"
            ? `${failure.message} — wait for processing to finish, then publish.`
            : `${failure.code}: ${failure.message}`,
        );
      } else {
        setError("Could not publish that version.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function restorePrevious() {
    if (!bot) return;
    // The control is only rendered when the API reports an earlier set, but the click
    // can outlive that state (a publish in another tab). Re-check here so the request
    // is not sent on a stale screen; the API refuses it either way, and the refusal is
    // shown verbatim rather than swallowed (§8.45's lesson about silent refusals).
    if (!bot.previous_knowledge_set_id) {
      setError("There is no earlier published version to restore yet.");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await rollbackKnowledge(bot.id);
      const restored = result.active_knowledge_version_ids.length;
      // Say what actually happened: the whole earlier set came back, and how many
      // documents that is. "Restored the previous version" alone would leave an owner
      // unable to tell whether the other documents survived — the doubt HALT-10 raised.
      setNotice(
        restored > 1
          ? `Restored the previous version. The bot now answers from ${restored} documents again.`
          : "Restored the previous version. The bot is answering from that one document again.",
      );
      await load();
    } catch (failure) {
      if (failure instanceof ApiFailure) {
        // no_previous_set / nothing_published / previous_set_empty each name a
        // different state, and the server's sentence says which. Replacing them with
        // one generic message would send the owner to the wrong place.
        setError(`${failure.code}: ${failure.message}`);
      } else {
        setError("Could not restore the previous version.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function test(event: React.FormEvent) {
    event.preventDefault();
    setTesting(true);
    setError(null);
    setDraft(null);
    setLive(null);
    try {
      // Both queries are asked in parallel against the same wording, because the
      // point of the screen is the difference between them: the draft is everything
      // indexed, the live side is the set of versions visitors are answered from
      // (RAG-11 -- a set, not one version).
      const liveVersions = bot?.active_knowledge_version_ids ?? [];
      const [draftResult, liveResult] = await Promise.all([
        previewAnswer(testQuestion.trim()),
        liveVersions.length > 0
          ? previewAnswer(testQuestion.trim(), liveVersions)
          : Promise.resolve(null),
      ]);
      setDraft(draftResult);
      setLive(liveResult);
    } catch (failure) {
      setError(
        failure instanceof ApiFailure
          ? `${failure.code}: ${failure.message}`
          : "Could not run the test.",
      );
    } finally {
      setTesting(false);
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === "upload") {
        if (!file) {
          setError("Choose a file first.");
          setBusy(false);
          return;
        }
        const result = await uploadDocument(file.name, file);
        setNotice(
          `Uploaded ${file.name} — ${result.chunks} chunk${result.chunks === 1 ? "" : "s"}, now processing.`,
        );
        setFile(null);
        if (fileInput.current) fileInput.current.value = "";
      } else if (mode === "crawl") {
        // "manual", not "weekly": nothing re-runs a crawl on a timer (the worker has no
        // cron), so recording "weekly" would store a promise no code keeps. The crawl
        // happens once, now, and the schedule can follow when there is something to
        // honour it.
        await createCrawl(url.trim(), "manual");
        // The crawl is queued, not finished: saying "crawled" here would be a claim
        // about work that has not happened yet.
        setNotice(
          "Crawl started. Pages appear here as they are fetched. Nothing re-crawls this site automatically yet, so re-run it when the site changes.",
        );
        setUrl("");
      } else {
        const result = await createFaq(question.trim(), answer.trim());
        setNotice(`Added Q&A — ${result.chunks} chunk, ready to publish.`);
        setQuestion("");
        setAnswer("");
      }
      await load();
    } catch (failure) {
      if (failure instanceof ApiFailure) {
        setError(`${failure.code}: ${failure.message}`);
      } else {
        setError("That did not work. Try again.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={styles.page}>
      <h1 style={styles.h1}>Knowledge</h1>
      <p style={styles.lead}>
        What the bot is allowed to answer from. Uploads and crawls are processed in the
        background; until a version is published the bot keeps answering from the last
        published one.
      </p>

      <section style={styles.card}>
        <div style={styles.tabs} role="tablist" aria-label="Add a source">
          {MODES.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={mode === item.id}
              onClick={() => {
                setMode(item.id);
                setError(null);
                setNotice(null);
              }}
              style={{ ...styles.tab, ...(mode === item.id ? styles.tabActive : {}) }}
            >
              {item.label}
            </button>
          ))}
        </div>

        <form onSubmit={submit} style={styles.form}>
          {mode === "upload" ? (
            <label style={styles.label}>
              File (PDF, DOCX, TXT, Markdown or HTML)
              <input
                ref={fileInput}
                type="file"
                accept=".pdf,.docx,.txt,.md,.markdown,.html,.htm"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                style={styles.input}
              />
            </label>
          ) : null}

          {mode === "crawl" ? (
            <label style={styles.label}>
              Start URL
              <input
                type="url"
                required
                placeholder="https://example.com/docs"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                style={styles.input}
              />
              <span style={styles.hint}>
                Only pages on this host are crawled, and only if its robots.txt allows it.
              </span>
            </label>
          ) : null}

          {mode === "faq" ? (
            <>
              <label style={styles.label}>
                Question
                <input
                  type="text"
                  required
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                  style={styles.input}
                />
              </label>
              <label style={styles.label}>
                Answer
                <textarea
                  required
                  rows={4}
                  value={answer}
                  onChange={(event) => setAnswer(event.target.value)}
                  style={{ ...styles.input, resize: "vertical" }}
                />
              </label>
            </>
          ) : null}

          {error ? <p style={styles.error}>{error}</p> : null}
          {notice ? <p style={styles.notice}>{notice}</p> : null}

          <button type="submit" disabled={busy} style={styles.button}>
            {busy
              ? "Working…"
              : mode === "crawl"
                ? "Start crawl"
                : mode === "faq"
                  ? "Add Q&A"
                  : "Upload"}
          </button>
        </form>
      </section>

      <section style={styles.card}>
        <h2 style={styles.h2}>Test an answer</h2>
        <p style={styles.hint}>
          Asks your indexed content directly, without the assistant writing prose. The
          left column is everything indexed; the right is only what is live, so a
          difference between them is what publishing would change.
        </p>
        <form onSubmit={test} style={{ ...styles.form, flexDirection: "row", alignItems: "flex-end" }}>
          <label style={{ ...styles.label, flex: 1 }}>
            Question
            <input
              type="text"
              required
              value={testQuestion}
              onChange={(event) => setTestQuestion(event.target.value)}
              style={styles.input}
            />
          </label>
          <button type="submit" disabled={testing} style={styles.button}>
            {testing ? "Testing…" : "Test"}
          </button>
        </form>

        {draft ? (
          <div style={styles.columns}>
            <PreviewColumn title="Draft (everything indexed)" result={draft} />
            <PreviewColumn
              title="Live (what visitors get)"
              result={live}
              missing="Nothing is published yet, so visitors are answered from the greeting only."
            />
          </div>
        ) : null}
      </section>

      <section style={styles.card}>
        <h2 style={styles.h2}>Documents</h2>
        <p style={styles.hint}>
          Status is the version being processed, not whether the bot can use it — that is
          decided by publishing. <strong>The bot answers from every document you have
          published</strong>, so publishing adds a document to what visitors are answered
          from rather than replacing it. Publishing a newer version of a document you
          already serve replaces just that document.
        </p>

        {/* ADMN-17 / HALT-10: restore the whole set the bot served before the current
            one. Offered only when the API reports a recorded earlier set — a button
            that always shows and sometimes 409s is a control the owner cannot trust. */}
        <div style={styles.restoreRow}>
          <button
            type="button"
            disabled={busy || !bot || !bot.previous_knowledge_set_id}
            onClick={() => void restorePrevious()}
            style={styles.smallButton}
          >
            Restore previous version
          </button>
          <span style={styles.hint}>
            {bot?.previous_knowledge_set_id
              ? `Goes back to the set served before the current one — ${bot.previous_knowledge_version_ids.length} document${
                  bot.previous_knowledge_version_ids.length === 1 ? "" : "s"
                }, all of it. Nothing is un-published.`
              : "Nothing to restore yet. This appears once you publish a second time."}
          </span>
        </div>

        {documents === null ? (
          <p style={styles.sub}>Loading…</p>
        ) : documents.length === 0 ? (
          <p style={styles.sub}>
            Nothing yet. The bot cannot answer from your own content until something is
            added here.
          </p>
        ) : (
          <table style={styles.table}>
            <thead>
              <tr>
                <th style={styles.th}>Document</th>
                <th style={styles.th}>Version</th>
                <th style={styles.th}>Status</th>
                <th style={styles.th}>Chunks</th>
                <th style={styles.th}>Served</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <tr key={doc.id}>
                  <td style={styles.td}>
                    {doc.title}
                    {doc.uri && doc.uri !== doc.title ? (
                      // The title comes from the document's own heading, so the filename
                      // an owner recognises is shown alongside rather than instead.
                      <span style={styles.hint}> · {doc.uri}</span>
                    ) : null}
                  </td>
                  <td style={styles.td}>{doc.version_no ?? "—"}</td>
                  <td style={styles.td}>
                    <StatusLabel status={doc.status} error={doc.error} />
                  </td>
                  <td style={styles.td}>{doc.chunk_count}</td>
                  <td style={styles.td}>
                    {doc.version_id &&
                    (bot?.active_knowledge_version_ids ?? []).includes(doc.version_id) ? (
                      <span style={styles.live}>live</span>
                    ) : doc.status === "ready" && doc.version_id ? (
                      <button
                        type="button"
                        disabled={busy || !bot}
                        onClick={() => void publish(doc)}
                        style={styles.smallButton}
                      >
                        Make live
                      </button>
                    ) : (
                      // Not ready yet, so there is nothing to publish -- offering the
                      // button would only produce a 409 the owner cannot act on.
                      <span style={styles.hint}>—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}

function PreviewColumn({
  title,
  result,
  missing,
}: {
  title: string;
  result: PreviewResult | null;
  missing?: string;
}) {
  return (
    <div style={styles.column}>
      <h3 style={styles.h3}>{title}</h3>
      {result === null ? (
        <p style={styles.hint}>{missing ?? "Not run."}</p>
      ) : result.hits.length === 0 ? (
        <p style={styles.hint}>
          {result.reason === "below_confidence_floor"
            ? `Nothing scored above the ${result.threshold} threshold (best ${result.best_score}).`
            : result.reason === "no_candidates"
              ? "Nothing indexed matches those words."
              : result.reason === "no_versions"
                ? "No processed content to search."
                : `No match (${result.reason ?? "no hits"}).`}
        </p>
      ) : (
        <ul style={styles.hits}>
          {result.hits.map((hit, index) => (
            <li key={index} style={styles.hit}>
              <span style={styles.score}>{hit.score}</span> {hit.text}
              {/* The citation is the point of a preview: "these words came from this
                  document, this section". A name alone cannot be checked against the
                  source, so the section and URI travel with it. */}
              {hit.document || hit.uri || hit.section ? (
                <span style={styles.hint}>
                  {" — "}
                  {hit.document ?? "untitled"}
                  {/* For a FAQ the section *is* the question, which is also the document
                      title, so printing both yields "Q: Do you ship…, Do you ship…". */}
                  {hit.section && hit.section !== hit.document ? `, ${hit.section}` : ""}
                  {hit.uri && hit.uri !== hit.document && hit.uri !== hit.section
                    ? ` (${hit.uri})`
                    : ""}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function StatusLabel({ status, error }: { status: string | null; error: string | null }) {
  if (error) {
    // The failure text is the only thing that tells an owner whether to retry or fix
    // the file, so it is shown rather than collapsed into "failed".
    return (
      <span style={styles.failed} title={error}>
        failed — {error}
      </span>
    );
  }
  if (!status) return <span>—</span>;
  return <span style={status === "ready" ? styles.ready : styles.pending}>{status}</span>;
}

const styles: Record<string, React.CSSProperties> = {
  page: { padding: "1.5rem", fontFamily: "system-ui, sans-serif", maxWidth: 900 },
  h1: { fontSize: "1.35rem", marginTop: 0 },
  h2: { fontSize: "1rem", margin: "0 0 0.35rem" },
  lead: { color: "#4b5563", fontSize: "0.9rem", marginTop: 0, maxWidth: "60ch" },
  card: {
    border: "1px solid #e5e7eb",
    borderRadius: 8,
    padding: "1rem 1.1rem",
    marginBottom: "1.25rem",
  },
  tabs: { display: "flex", gap: "0.4rem", marginBottom: "0.9rem", flexWrap: "wrap" },
  tab: {
    padding: "0.35rem 0.7rem",
    border: "1px solid #e5e7eb",
    background: "#ffffff",
    borderRadius: 999,
    cursor: "pointer",
    fontSize: "0.82rem",
  },
  tabActive: { borderColor: "#1f6feb", color: "#1f6feb", fontWeight: 600 },
  form: { display: "flex", flexDirection: "column", gap: "0.75rem", maxWidth: 520 },
  label: {
    display: "flex",
    flexDirection: "column",
    gap: "0.25rem",
    fontSize: "0.85rem",
    color: "#374151",
  },
  input: {
    padding: "0.45rem 0.55rem",
    border: "1px solid #d1d5db",
    borderRadius: 4,
    fontSize: "0.9rem",
    fontFamily: "inherit",
  },
  hint: { color: "#6b7280", fontSize: "0.78rem" },
  sub: { color: "#6b7280", fontSize: "0.85rem" },
  error: { color: "#b91c1c", fontSize: "0.85rem", margin: 0 },
  notice: { color: "#065f46", fontSize: "0.85rem", margin: 0 },
  button: {
    alignSelf: "flex-start",
    padding: "0.45rem 1rem",
    background: "#1f6feb",
    color: "#ffffff",
    border: "none",
    borderRadius: 4,
    cursor: "pointer",
    fontSize: "0.88rem",
  },
  table: { width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" },
  th: {
    textAlign: "left",
    borderBottom: "1px solid #e5e7eb",
    padding: "0.4rem 0.5rem 0.4rem 0",
    color: "#6b7280",
    fontWeight: 500,
  },
  td: {
    borderBottom: "1px solid #f3f4f6",
    padding: "0.45rem 0.5rem 0.45rem 0",
    verticalAlign: "top",
  },
  ready: { color: "#065f46" },
  live: { color: "#065f46", fontWeight: 600 },
  columns: { display: "flex", gap: "1.25rem", marginTop: "1rem", flexWrap: "wrap" },
  column: { flex: "1 1 260px", minWidth: 240 },
  h3: { fontSize: "0.85rem", margin: "0 0 0.4rem", color: "#374151" },
  hits: { margin: 0, paddingLeft: "1.1rem", fontSize: "0.82rem", color: "#374151" },
  hit: { marginBottom: "0.5rem" },
  score: { color: "#6b7280", fontVariantNumeric: "tabular-nums" },
  smallButton: {
    padding: "0.2rem 0.55rem",
    background: "#ffffff",
    border: "1px solid #d1d5db",
    borderRadius: 4,
    cursor: "pointer",
    fontSize: "0.8rem",
  },
  restoreRow: {
    display: "flex",
    alignItems: "center",
    gap: "0.6rem",
    flexWrap: "wrap",
    margin: "0 0 0.9rem",
  },
  pending: { color: "#92400e" },
  failed: { color: "#b91c1c" },
};
