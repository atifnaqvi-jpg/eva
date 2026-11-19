"use client";

import { useCallback, useEffect, useState } from "react";

import {
  addDomain,
  ApiFailure,
  changePassword,
  createWebhook,
  CREDENTIAL_PROVIDERS,
  deleteCredential,
  deleteDomain,
  deleteWebhook,
  getProfile,
  getTenantSettings,
  listAuditEntries,
  listBots,
  listCredentials,
  listDomains,
  listWebhooks,
  RETENTION_MAX_DAYS,
  RETENTION_MIN_DAYS,
  setCredential,
  testWebhook,
  updateTenantSettings,
  verifyDomain,
  WEBHOOK_EVENTS,
  type AuditEntry,
  type Credential,
  type DomainSummary,
  type Profile,
  type WebhookSummary,
} from "@/lib/api";

/**
 * Settings (ADMN-10, §18.1; controls added by ADMN-16; copy corrected by ADMN-14).
 *
 * The two cards that used to describe shipped behaviour they could not control now
 * drive it. That ordering was deliberate: the copy was corrected first (ADMN-14) so the
 * screen stopped making false claims, and the controls arrive here once each has a
 * backend that can refuse a bad value.
 *
 * - **Data retention** is enforced server-side by SEC-09/SEC-16's daily purge and on
 *   demand by SEC-10's erasure. The editor writes the per-tenant window through
 *   `PATCH /settings/tenant`, which validates with `app.retention.policy.validate` —
 *   the same module the purge reads — so the bound (1–730) is refused at the boundary
 *   rather than silently normalised to the default. The card still states the
 *   eligibility rule that makes the promise narrower than "old conversations are
 *   deleted": only finished conversations qualify, aged from `last_activity_at`.
 * - **Domains** are verified by DNS TXT record (SEC-15). Add and verify are separate
 *   actions and an add grants nothing on its own: the record name and value come from
 *   the API, and a successful verify is what flips the flag the widget checks.
 *
 * What the copy must not do, in either card: claim a mechanism does not exist when it
 * does (HALT-06 was ruled and built), or state a number the screen never read.
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
  // Null until the settings load, so the retention card can say "not read yet" instead
  // of printing a default it did not get from the API.
  const [retentionDays, setRetentionDays] = useState<number | null>(null);
  // What the owner typed, kept apart from `retentionDays` so an invalid value stays on
  // screen with the refusal beside it instead of snapping back to the stored number.
  const [retentionDraft, setRetentionDraft] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");

  // One workspace, one bot (the MVP creates exactly one, and every other screen reads
  // `bots[0]`); null means "not resolved yet", which the domain card states rather than
  // rendering an add form that would POST to an undefined id.
  const [botId, setBotId] = useState<string | null>(null);
  const [domains, setDomains] = useState<DomainSummary[]>([]);
  const [domainInput, setDomainInput] = useState("");
  // The last verify outcome, kept per domain: "no record found" is a normal setup step
  // and has to stay visible beside the values to publish, not vanish with the notice.
  const [verifyResult, setVerifyResult] = useState<{ id: string; text: string; ok: boolean } | null>(
    null,
  );

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
      const [me, tenant, creds, hooks, log, bots] = await Promise.all([
        getProfile(),
        getTenantSettings(),
        listCredentials(),
        listWebhooks(),
        listAuditEntries(20),
        listBots(),
      ]);
      setProfile(me);
      setHandoverEmail(tenant.handover.email ?? "");
      setRetentionDays(tenant.retention.conversation_days);
      setRetentionDraft(String(tenant.retention.conversation_days));
      setCredentials(creds);
      setWebhooks(hooks);
      setAudit(log);

      // Domains are per bot, so a workspace with no bot cannot have any. That is a
      // distinct state from "no domains yet" and the card says which one it is.
      const bot = bots[0] ?? null;
      setBotId(bot ? bot.id : null);
      setDomains(bot ? await listDomains(bot.id) : []);
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

  async function saveRetention(event: React.FormEvent) {
    event.preventDefault();
    const days = Number(retentionDraft);
    // Checked here as well as by the server, and deliberately not left to the input's
    // `min`/`max`: native validation shows a tooltip that vanishes on the next click,
    // leaving the bad number in the box with no explanation. This message stays. The
    // server still refuses the same range — that is the authority, and the `min`/`max`
    // attributes and these constants are asserted against `app.retention.policy`.
    if (!Number.isInteger(days) || days < RETENTION_MIN_DAYS || days > RETENTION_MAX_DAYS) {
      setError(
        `The window must be a whole number of days between ${RETENTION_MIN_DAYS} and ` +
          `${RETENTION_MAX_DAYS}. Nothing was saved, and conversations past their ` +
          "window keep being purged on the old schedule.",
      );
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await updateTenantSettings({
        retention: { conversation_days: days },
      });
      setRetentionDays(updated.retention.conversation_days);
      setRetentionDraft(String(updated.retention.conversation_days));
      setNotice(
        `Conversations will be kept for ${updated.retention.conversation_days} ${
          updated.retention.conversation_days === 1 ? "day" : "days"
        }. The purge runs daily and only removes finished conversations.`,
      );
    } catch (failure) {
      // The API's refusal message names the bound, so it is shown verbatim: a generic
      // "invalid" would leave the owner guessing which end they crossed.
      setError(
        failure instanceof ApiFailure
          ? failure.code === "invalid_retention"
            ? failure.message
            : `${failure.code}: ${failure.message}`
          : "Could not save that window.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function addDomainEntry(event: React.FormEvent) {
    event.preventDefault();
    if (!botId) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    setVerifyResult(null);
    try {
      const created = await addDomain(botId, domainInput.trim());
      setDomainInput("");
      setDomains(await listDomains(botId));
      // Verbatim, because "added" and "allowed" are different and an owner who reads
      // only the first word will wait for a widget that still refuses the origin.
      setNotice(
        `Added ${created.domain}. It is not allowed yet: publish the TXT record below and verify it.`,
      );
    } catch (failure) {
      setError(
        failure instanceof ApiFailure
          ? failure.code === "invalid_domain"
            ? failure.message
            : `${failure.code}: ${failure.message}`
          : "Could not add that domain.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function checkDomain(target: DomainSummary) {
    if (!botId) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await verifyDomain(botId, target.id);
      setVerifyResult({
        id: target.id,
        ok: result.verified,
        text: result.verified
          ? `${result.domain.domain} is verified and may now serve the widget.`
          : result.detail,
      });
      if (result.verified) {
        setNotice(`${result.domain.domain} is verified.`);
      }
      setDomains(await listDomains(botId));
    } catch (failure) {
      // 503 `lookup_failed` means the resolver could not answer, which is not the same
      // as "your record is missing" — saying otherwise would send the owner to fix a
      // record that may already be correct.
      setError(
        failure instanceof ApiFailure
          ? failure.code === "lookup_failed"
            ? `Could not check the record right now: ${failure.message}`
            : `${failure.code}: ${failure.message}`
          : "Could not check that domain.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function removeDomainEntry(target: DomainSummary) {
    if (!botId) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await deleteDomain(botId, target.id);
      setVerifyResult(null);
      setDomains(await listDomains(botId));
      setNotice(`Removed ${target.domain}. The widget refuses that origin from now on.`);
    } catch (failure) {
      setError(
        failure instanceof ApiFailure
          ? `${failure.code}: ${failure.message}`
          : "Could not remove that domain.",
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
          The widget refuses any site whose domain is not verified, and verification is
          proving you control that domain: add it for the bot, then publish the DNS TXT
          record the API returns at <code>_eva-verify.&lt;your-domain&gt;</code> with the
          value it issues, and verify it. Adding a domain on its own never allows it — a
          site becomes allowed only once that lookup matches.
        </p>

        {botId === null ? (
          <p style={styles.hint}>
            This workspace has no bot yet, so there is no widget to allow a domain for.
          </p>
        ) : (
          <>
            <form onSubmit={addDomainEntry} style={styles.form}>
              <label style={styles.label}>
                Domain
                <input
                  style={styles.input}
                  value={domainInput}
                  onChange={(event) => setDomainInput(event.target.value)}
                  placeholder="www.example.com"
                  disabled={busy}
                />
              </label>
              <p style={styles.hint}>
                Enter the host only. The scheme and port are not part of the record, and
                the widget matches the origin it is loaded from.
              </p>
              <button style={styles.button} type="submit" disabled={busy || !domainInput.trim()}>
                Add domain
              </button>
            </form>

            {domains.length === 0 ? (
              <p style={styles.hint}>No domains yet. The widget serves no site until one is verified.</p>
            ) : (
              <ul style={styles.list}>
                {domains.map((entry) => (
                  <li key={entry.id} style={styles.listItem}>
                    <strong>{entry.domain}</strong>{" "}
                    <span style={styles.hint}>
                      {entry.verified ? "verified" : "not verified — the widget refuses this origin"}
                    </span>
                    <div style={styles.recordBox}>
                      {entry.record_name ? (
                        <>
                          <div style={styles.recordLine}>
                            Name: <code>{entry.record_name}</code>
                          </div>
                          <div style={styles.recordLine}>
                            Type: <code>TXT</code> Value: <code>{entry.record_value}</code>
                          </div>
                        </>
                      ) : (
                        <div style={styles.recordLine}>
                          This entry has no record name the API can produce, so it cannot
                          be verified. Remove it and add the host again.
                        </div>
                      )}
                    </div>
                    {verifyResult && verifyResult.id === entry.id ? (
                      <p style={verifyResult.ok ? styles.notice : styles.error}>
                        {verifyResult.text}
                      </p>
                    ) : null}
                    <div style={styles.row}>
                      <button
                        style={styles.smallButton}
                        onClick={() => void checkDomain(entry)}
                        disabled={busy || !entry.record_name}
                      >
                        {entry.verified ? "Check again" : "Verify"}
                      </button>
                      <button
                        style={styles.smallButton}
                        onClick={() => void removeDomainEntry(entry)}
                        disabled={busy}
                      >
                        Remove
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
            <p style={styles.hint}>
              Verification reads the record live, so it can be run again once DNS has
              propagated. If the lookup itself cannot be made, the screen says so instead
              of claiming the record is missing — those mean different things, and only
              one of them is something you can fix.
            </p>
          </>
        )}
      </section>

      <section style={styles.card}>
        <h2 style={styles.h2}>Data retention</h2>
        <p style={styles.hint}>
          {retentionDays === null
            ? "Reading your retention window…"
            : `Conversations are deleted on a schedule: your window is ${retentionDays} ${
                retentionDays === 1 ? "day" : "days"
              }, and a daily job purges conversations past it.`}{" "}
          Only finished conversations are eligible — a conversation still open is never
          purged however old it is, and the clock runs from its last activity rather than
          from when it started. Erasing a conversation on a data-subject request is done
          through the API, and applies whatever the window says; exporting one is
          available from the Conversations screen.
        </p>

        {/* noValidate on purpose: the browser's own range check would prevent submit and
            show a tooltip that disappears on the next click. The handler below renders a
            durable message, and the server is still the authority on the same range. */}
        <form onSubmit={saveRetention} style={styles.form} noValidate>
          <label style={styles.label}>
            Keep finished conversations for (days)
            <input
              style={styles.input}
              type="number"
              min={RETENTION_MIN_DAYS}
              max={RETENTION_MAX_DAYS}
              step={1}
              value={retentionDraft}
              onChange={(event) => setRetentionDraft(event.target.value)}
              disabled={busy}
            />
          </label>
          <p style={styles.hint}>
            Between {RETENTION_MIN_DAYS} and {RETENTION_MAX_DAYS} days. Shorter windows
            delete sooner and cannot be undone, so the purge is the only thing that acts
            on this number — nothing is deleted on save. A shorter window takes effect at
            the next daily run, and only for conversations that have already finished.
          </p>
          <button style={styles.button} type="submit" disabled={busy || retentionDraft === ""}>
            Save window
          </button>
        </form>
      </section>

      <section style={styles.card}>
        <h2 style={styles.h2}>Recent activity</h2>
        {audit.length === 0 ? (
          <p style={styles.hint}>Nothing recorded yet.</p>
        ) : (
          <table style={styles.table}>
            <caption style={styles.caption}>
              {audit.length < 20
                ? `All ${audit.length} recorded ${audit.length === 1 ? "entry" : "entries"}, newest first.`
                : "The most recent 20 entries, newest first. This view has no paging, so older entries are not shown."}
            </caption>
            <thead>
              <tr>
                <th style={styles.th}>When</th>
                <th style={styles.th}>Action</th>
                <th style={styles.th}>Subject</th>
                <th style={styles.th}>Detail</th>
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
                  {/* The API records what changed here (provider and last-4, a webhook
                      URL) and the audit trail is append-only, so this is the only place
                      it can be read. Showing the action alone loses the subject. */}
                  <td style={styles.td}>
                    {Object.entries(entry.detail ?? {})
                      .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join("/") : String(value)}`)
                      .join(", ") || "—"}
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
  listItem: { marginBottom: "0.6rem" },
  recordBox: {
    margin: "0.3rem 0",
    padding: "0.4rem 0.55rem",
    background: "#f9fafb",
    border: "1px solid #e5e7eb",
    borderRadius: 4,
    fontSize: "0.78rem",
  },
  recordLine: { lineHeight: 1.6 },
  row: { display: "flex", gap: "0.4rem", marginTop: "0.3rem" },
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
  caption: { textAlign: "left", color: "#6b7280", fontSize: "0.78rem", paddingBottom: "0.4rem" },
  th: {
    textAlign: "left",
    borderBottom: "1px solid #e5e7eb",
    padding: "0.4rem 0.5rem 0.4rem 0",
    color: "#6b7280",
    fontWeight: 500,
  },
  td: { borderBottom: "1px solid #f3f4f6", padding: "0.35rem 0.5rem 0.35rem 0" },
};
