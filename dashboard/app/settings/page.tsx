"use client";

import { useCallback, useEffect, useState } from "react";

import {
  ApiFailure,
  changePassword,
  createWebhook,
  CREDENTIAL_PROVIDERS,
  deleteCredential,
  deleteWebhook,
  getProfile,
  getTenantSettings,
  listAuditEntries,
  listCredentials,
  listWebhooks,
  setCredential,
  testWebhook,
  updateTenantSettings,
  WEBHOOK_EVENTS,
  type AuditEntry,
  type Credential,
  type Profile,
  type WebhookSummary,
} from "@/lib/api";

/**
 * Settings (ADMN-10, §18.1).
 *
 * Two deliberate omissions, both because the backend behind them does not exist yet and
 * a control that does nothing is worse than no control:
 *
 * - **Data retention** is listed in §18.1, but nothing enforces it: there is no scheduler
 *   in the worker and no deletion path anywhere. A "keep transcripts for 30 days" dropdown
 *   would promise a deletion that never happens, which is a compliance claim rather than a
 *   UI gap. The screen says so instead.
 * - **Domains** are blocked on HALT-06 (no verification mechanism is specified), so the
 *   screen explains why the widget refuses every origin rather than offering an
 *   add-domain form whose output the widget would ignore.
 *
 * The BYOK list shows `••••` + last-4 and nothing else because that is all the API can
 * return; no endpoint could echo a key even if this page asked for one.
 */
export default function SettingsPage() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [webhooks, setWebhooks] = useState<WebhookSummary[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [handoverEmail, setHandoverEmail] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");

  const [keyProvider, setKeyProvider] = useState<string>(CREDENTIAL_PROVIDERS[0]);
  const [apiKey, setApiKey] = useState("");

  const [hookUrl, setHookUrl] = useState("");
  const [hookEvents, setHookEvents] = useState<string[]>([...WEBHOOK_EVENTS]);
  // The secret is shown exactly once, from the creation response, because no endpoint
  // can return it again. Holding it in state is the only way the owner can copy it.
  const [newSecret, setNewSecret] = useState<{ url: string; secret: string } | null>(null);
  const [hookResult, setHookResult] = useState<{ id: string; text: string; ok: boolean } | null>(
    null,
  );

  const load = useCallback(async () => {
    setError(null);
    try {
      const [me, tenant, creds, hooks, log] = await Promise.all([
        getProfile(),
        getTenantSettings(),
        listCredentials(),
        listWebhooks(),
        listAuditEntries(20),
      ]);
      setProfile(me);
      setHandoverEmail(tenant.handover.email ?? "");
      setCredentials(creds);
      setWebhooks(hooks);
      setAudit(log);
    } catch (failure) {
      if (failure instanceof ApiFailure && failure.status === 401) {
        setError("Log in to see your settings.");
      } else if (failure instanceof ApiFailure) {
        setError(`${failure.code}: ${failure.message}`);
      } else {
        setError("Could not load settings.");
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function saveHandover(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      // An empty box means "clear it", not "leave it": the owner needs a way to stop
      // escalation email, so null is sent explicitly rather than the key being omitted.
      const updated = await updateTenantSettings({
        handover: { email: handoverEmail.trim() ? handoverEmail.trim() : null },
      });
      setHandoverEmail(updated.handover.email ?? "");
      setNotice(
        updated.handover.email
          ? `Escalations will be emailed to ${updated.handover.email}.`
          : "Escalations will not be emailed anywhere.",
      );
    } catch (failure) {
      setError(
        failure instanceof ApiFailure
          ? failure.code === "invalid_email"
            ? "That is not a usable email address."
            : `${failure.code}: ${failure.message}`
          : "Could not save that.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function savePassword(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setNotice(
        "Password changed. Any other browser signed in as you has been signed out; this one is still signed in.",
      );
    } catch (failure) {
      setError(
        failure instanceof ApiFailure
          ? failure.code === "invalid_credentials"
            ? "Your current password is not correct."
            : failure.code === "weak_password"
              ? failure.message
              : `${failure.code}: ${failure.message}`
          : "Could not change the password.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function saveKey(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const saved = await setCredential(keyProvider, apiKey.trim());
      // Clear the field immediately: the key is stored, and leaving it in the DOM
      // would keep a live secret in the page for as long as the tab stays open.
      setApiKey("");
      setNotice(`Key for ${saved.provider} saved. Only its last four characters are stored in readable form.`);
      await load();
    } catch (failure) {
      setError(
        failure instanceof ApiFailure
          ? failure.code === "vault_unavailable"
            ? "The key store is not available in this deployment, so nothing was saved."
            : failure.code === "unknown_provider"
              ? "That provider is not supported."
              : `${failure.code}: ${failure.message}`
          : "Could not save that key.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function removeKey(provider: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await deleteCredential(provider);
      setNotice(`Removed the ${provider} key. The platform's own model is used until you add one.`);
      await load();
    } catch (failure) {
      setError(
        failure instanceof ApiFailure
          ? `${failure.code}: ${failure.message}`
          : "Could not remove that key.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function addWebhook(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    setHookResult(null);
    try {
      const created = await createWebhook(hookUrl.trim(), hookEvents);
      setNewSecret({ url: created.url, secret: created.secret });
      setHookUrl("");
      setHookEvents([...WEBHOOK_EVENTS]);
      await load();
    } catch (failure) {
      setError(
        failure instanceof ApiFailure
          ? failure.code === "invalid_url"
            ? `That URL cannot be used: ${failure.message}`
            : failure.code === "unknown_event"
              ? "Pick at least one event the API can send."
              : `${failure.code}: ${failure.message}`
          : "Could not add that endpoint.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function removeWebhook(hook: WebhookSummary) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await deleteWebhook(hook.id);
      // The secret is destroyed with the endpoint, so a displayed one would now be
      // unrecoverable *and* useless; clearing it avoids showing a dead credential.
      setNewSecret(null);
      setHookResult(null);
      setNotice(`Removed ${hook.url}. Its signing secret has been destroyed.`);
      await load();
    } catch (failure) {
      setError(
        failure instanceof ApiFailure
          ? `${failure.code}: ${failure.message}`
          : "Could not remove that endpoint.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function sendTest(hook: WebhookSummary) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await testWebhook(hook.id);
      // The receiver's status is reported verbatim: "ok" here means the endpoint
      // answered 2xx, not that the receiver verified the signature.
      setHookResult({
        id: hook.id,
        ok: result.ok,
        text: result.ok
          ? `Endpoint answered ${result.status}. It received a signed ping — check that your receiver accepted the signature.`
          : `Endpoint did not accept the ping${result.status ? ` (HTTP ${result.status})` : ""}: ${result.detail}`,
      });
    } catch (failure) {
      setError(
        failure instanceof ApiFailure
          ? `${failure.code}: ${failure.message}`
          : "Could not reach that endpoint.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={styles.page}>
      <h1 style={styles.h1}>Settings</h1>

      {error ? <p style={styles.error}>{error}</p> : null}
      {notice ? <p style={styles.notice}>{notice}</p> : null}

      <section style={styles.card}>
        <h2 style={styles.h2}>Account</h2>
        {profile ? (
          <dl style={styles.grid}>
            <dt style={styles.dt}>Workspace</dt>
            <dd style={styles.dd}>{profile.tenant_name}</dd>
            <dt style={styles.dt}>Signed in as</dt>
            <dd style={styles.dd}>{profile.email}</dd>
            <dt style={styles.dt}>Role</dt>
            <dd style={styles.dd}>{profile.role}</dd>
            <dt style={styles.dt}>Created</dt>
            <dd style={styles.dd}>{new Date(profile.created_at).toLocaleDateString()}</dd>
          </dl>
        ) : (
          <p style={styles.hint}>Loading…</p>
        )}
      </section>

      <section style={styles.card}>
        <h2 style={styles.h2}>Password</h2>
        <p style={styles.hint}>
          Changing your password signs out every other browser, so a password you shared
          by mistake stops working straight away.
        </p>
        <form onSubmit={savePassword} style={styles.form}>
          <label style={styles.label}>
            Current password
            <input
              type="password"
              required
              autoComplete="current-password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
              style={styles.input}
            />
          </label>
          <label style={styles.label}>
            New password
            <input
              type="password"
              required
              minLength={12}
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              style={styles.input}
            />
            <span style={styles.hint}>At least 12 characters.</span>
          </label>
          <button type="submit" disabled={busy} style={styles.button}>
            Change password
          </button>
        </form>
      </section>

      <section style={styles.card}>
        <h2 style={styles.h2}>Handover</h2>
        <p style={styles.hint}>
          Where a human should pick up a conversation the assistant could not answer.
        </p>
        <form onSubmit={saveHandover} style={styles.form}>
          <label style={styles.label}>
            Escalation email
            <input
              type="email"
              value={handoverEmail}
              onChange={(event) => setHandoverEmail(event.target.value)}
              placeholder="support@yourcompany.com"
              style={styles.input}
            />
            <span style={styles.hint}>
              Leave blank to stop emailing escalations. Handover still happens in the
              widget; only the notification stops.
            </span>
          </label>
          <button type="submit" disabled={busy} style={styles.button}>
            Save
          </button>
        </form>
      </section>

      <section style={styles.card}>
        <h2 style={styles.h2}>Your own model keys</h2>
        <p style={styles.hint}>
          Keys are write-only: this page can show the last four characters and nothing
          more, because no endpoint returns a key.
        </p>
        {credentials.length === 0 ? (
          <p style={styles.hint}>
            None configured. The platform&apos;s own model is used unless you add one.
          </p>
        ) : (
          <ul style={styles.list}>
            {credentials.map((credential) => (
              <li key={credential.provider}>
                <strong>{credential.provider}</strong>{" "}
                <span style={styles.mono}>•••• {credential.last4}</span>{" "}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void removeKey(credential.provider)}
                  style={styles.smallButton}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}

        <form onSubmit={saveKey}>
          <label style={styles.label}>
            Provider
            <select
              value={keyProvider}
              onChange={(event) => setKeyProvider(event.target.value)}
              style={styles.input}
            >
              {CREDENTIAL_PROVIDERS.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label style={styles.label}>
            API key
            <input
              type="password"
              required
              minLength={8}
              autoComplete="off"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              style={styles.input}
            />
          </label>
          <button type="submit" disabled={busy || apiKey.trim().length < 8} style={styles.button}>
            {credentials.some((item) => item.provider === keyProvider) ? "Replace key" : "Save key"}
          </button>
          <p style={styles.hint}>
            Using the same provider again replaces its key. Saved keys are encrypted with
            this deployment&apos;s vault; the dashboard never receives one back.
          </p>
        </form>
      </section>

      <section style={styles.card}>
        <h2 style={styles.h2}>Webhooks</h2>
        {webhooks.length === 0 ? (
          <p style={styles.hint}>None configured.</p>
        ) : (
          <ul style={styles.list}>
            {webhooks.map((hook) => (
              <li key={hook.id}>
                <span style={styles.mono}>{hook.url}</span>{" "}
                <span style={styles.hint}>
                  {hook.events.join(", ")}
                  {hook.active ? "" : " — paused"}
                </span>{" "}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void sendTest(hook)}
                  style={styles.smallButton}
                >
                  Send test
                </button>{" "}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void removeWebhook(hook)}
                  style={styles.smallButton}
                >
                  Remove
                </button>
                {hookResult?.id === hook.id ? (
                  <p style={hookResult.ok ? styles.notice : styles.error}>
                    {hookResult.text}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}

        {newSecret ? (
          // Shown once and never again: the API cannot return it, so if this is missed
          // the only recovery is to remove the endpoint and add it again.
          <p style={styles.notice}>
            Secret for <span style={styles.mono}>{newSecret.url}</span>:{" "}
            <span style={styles.mono}>{newSecret.secret}</span> — copy it now. It is not
            shown again; if you lose it, remove this endpoint and add it again.
          </p>
        ) : null}

        <form onSubmit={addWebhook}>
          <label style={styles.label}>
            Endpoint URL
            <input
              type="url"
              required
              placeholder="https://example.com/hooks/eva"
              value={hookUrl}
              onChange={(event) => setHookUrl(event.target.value)}
              style={styles.input}
            />
          </label>
          <fieldset style={styles.fieldset}>
            <legend style={styles.label}>Events</legend>
            {WEBHOOK_EVENTS.map((name) => (
              <label key={name} style={styles.check}>
                <input
                  type="checkbox"
                  checked={hookEvents.includes(name)}
                  onChange={(event) =>
                    setHookEvents((current) =>
                      event.target.checked
                        ? [...current, name]
                        : current.filter((item) => item !== name),
                    )
                  }
                />{" "}
                {name}
              </label>
            ))}
          </fieldset>
          <button type="submit" disabled={busy || hookEvents.length === 0} style={styles.button}>
            Add endpoint
          </button>
          <p style={styles.hint}>
            Deliveries are signed with the secret above, so your receiver can reject
            anything that did not come from here. Private and loopback addresses are
            refused unless the deployment explicitly allows them.
          </p>
        </form>
      </section>

      <section style={styles.card}>
        <h2 style={styles.h2}>Domains</h2>
        <p style={styles.hint}>
          Embedding is not available yet: no mechanism for verifying that a site belongs to
          you has been chosen, so the widget refuses every origin. This is a decision on
          record (HALT-06), not a setting you can change.
        </p>
      </section>

      <section style={styles.card}>
        <h2 style={styles.h2}>Data retention</h2>
        <p style={styles.hint}>
          Not configurable yet. Nothing in the deployment deletes conversations on a
          schedule, so a retention box here would promise a deletion that does not happen.
          Transcripts are kept until that is built; exporting a conversation is available
          from the Conversations screen.
        </p>
      </section>

      <section style={styles.card}>
        <h2 style={styles.h2}>Recent activity</h2>
        {audit.length === 0 ? (
          <p style={styles.hint}>Nothing recorded yet.</p>
        ) : (
          <table style={styles.table}>
            <thead>
              <tr>
                <th style={styles.th}>When</th>
                <th style={styles.th}>Action</th>
                <th style={styles.th}>Subject</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((entry) => (
                <tr key={entry.id}>
                  <td style={styles.td}>{new Date(entry.created_at).toLocaleString()}</td>
                  <td style={styles.td}>{entry.action}</td>
                  <td style={styles.td}>
                    {entry.entity_type}
                    {entry.entity_id ? ` ${entry.entity_id.slice(0, 8)}` : ""}
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

const styles: Record<string, React.CSSProperties> = {
  page: { padding: "1.5rem", fontFamily: "system-ui, sans-serif", maxWidth: 900 },
  h1: { fontSize: "1.35rem", marginTop: 0 },
  h2: { fontSize: "1rem", margin: "0 0 0.35rem" },
  card: {
    border: "1px solid #e5e7eb",
    borderRadius: 8,
    padding: "1rem 1.1rem",
    marginBottom: "1.25rem",
  },
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
  error: { color: "#b91c1c", fontSize: "0.85rem", margin: "0 0 0.75rem" },
  notice: { color: "#065f46", fontSize: "0.85rem", margin: "0 0 0.75rem" },
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
  grid: {
    display: "grid",
    gridTemplateColumns: "max-content 1fr",
    gap: "0.25rem 1rem",
    margin: 0,
    fontSize: "0.88rem",
  },
  dt: { color: "#6b7280" },
  dd: { margin: 0 },
  list: { margin: 0, paddingLeft: "1.1rem", fontSize: "0.85rem" },
  smallButton: {
    padding: "0.2rem 0.5rem",
    background: "none",
    border: "1px solid #d1d5db",
    borderRadius: 4,
    cursor: "pointer",
    fontSize: "0.78rem",
    fontFamily: "inherit",
  },
  fieldset: {
    border: "none",
    margin: "0.5rem 0 0",
    padding: 0,
    display: "flex",
    flexWrap: "wrap",
    gap: "0.75rem",
  },
  check: { display: "flex", alignItems: "center", gap: "0.3rem", fontSize: "0.85rem" },
  mono: { fontFamily: "ui-monospace, monospace" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: "0.82rem" },
  th: {
    textAlign: "left",
    borderBottom: "1px solid #e5e7eb",
    padding: "0.4rem 0.5rem 0.4rem 0",
    color: "#6b7280",
    fontWeight: 500,
  },
  td: { borderBottom: "1px solid #f3f4f6", padding: "0.35rem 0.5rem 0.35rem 0" },
};
