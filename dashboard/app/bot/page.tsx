"use client";

import { useEffect, useState } from "react";

import {
  ApiFailure,
  embedSnippet,
  getBot,
  listBots,
  updateBot,
  type BotConfig,
  type BotPatch,
  type EmbedSnippet,
} from "@/lib/api";

/**
 * Bot configuration (ADMN-03, §17.2).
 *
 * Two things this screen deliberately does not do:
 *
 * - It does not offer a free-text colour field. `primaryColor` is validated against an
 *   allowlist that rejects named colours and `var()`, because the value renders inside
 *   a third party's page. A hex input plus a swatch keeps the owner inside the set of
 *   values that will actually be accepted, instead of letting them type something that
 *   is silently replaced on save.
 * - It does not claim the saved appearance is what visitors see. The API returns
 *   `appearance_defaults` separately because `normalise` drops rejected keys rather than
 *   storing a default; the screen shows the effective value and says where it came from.
 */
const HEX = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

export default function BotPage() {
  const [bot, setBot] = useState<BotConfig | null>(null);
  const [embed, setEmbed] = useState<EmbedSnippet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [name, setName] = useState("");
  const [welcome, setWelcome] = useState("");
  const [threshold, setThreshold] = useState(0.55);
  const [colour, setColour] = useState("");
  const [position, setPosition] = useState("bottom-right");
  const [poweredBy, setPoweredBy] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const bots = await listBots();
        if (cancelled) return;
        if (bots.length === 0) {
          setError("This workspace has no bot yet.");
          return;
        }
        const full = await getBot(bots[0].id);
        if (cancelled) return;
        apply(full);
        const snippet = await embedSnippet(full.id);
        if (!cancelled) setEmbed(snippet);
      } catch (failure) {
        if (!cancelled) setError(describe(failure));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  function apply(value: BotConfig) {
    setBot(value);
    setName(value.name);
    setWelcome(value.welcome_message ?? "");
    setThreshold(value.confidence_threshold);
    setColour(value.appearance.primaryColor ?? "");
    setPosition(value.appearance.position ?? value.appearance_defaults.position ?? "bottom-right");
    setPoweredBy(value.powered_by_visible);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!bot) return;
    setBusy(true);
    setError(null);
    setNotice(null);

    // The server normalises and would drop a bad colour, returning 200 with the key
    // absent -- so the screen would say "Saved." while the value silently reverted.
    // Caught here instead, where it can be explained. The server check stays: this is
    // for the owner, not for safety.
    const trimmedColour = colour.trim();
    if (trimmedColour && !HEX.test(trimmedColour)) {
      setError("Colour must be #rgb or #rrggbb — for example #1f6feb.");
      setBusy(false);
      return;
    }

    try {
      const patch: BotPatch = {
        name,
        welcome_message: welcome,
        confidence_threshold: threshold,
        powered_by_visible: poweredBy,
        appearance: {
          // Omitting primaryColor when blank is deliberate: `normalise` would drop an
          // empty string anyway, and sending it makes the intent "I cleared this" rather
          // than "I forgot". Blank means "use the default".
          ...(trimmedColour ? { primaryColor: trimmedColour } : {}),
          position,
        },
      };
      const saved = await updateBot(bot.id, patch);
      apply(saved);
      setNotice("Saved.");
    } catch (failure) {
      setError(describe(failure));
    } finally {
      setBusy(false);
    }
  }

  if (error && !bot) {
    return (
      <main style={styles.page}>
        <h1 style={styles.h1}>Chatbot</h1>
        <p role="alert" style={styles.error}>{error}</p>
      </main>
    );
  }

  if (!bot) {
    return <main style={styles.page}>Loading…</main>;
  }

  const effectiveColour = bot.appearance.primaryColor ?? bot.appearance_defaults.primaryColor;
  const colourIsDefault = bot.appearance.primaryColor === undefined;

  return (
    <main style={styles.page}>
      <h1 style={styles.h1}>Chatbot</h1>
      <p style={styles.hint}>
        These values are served to the widget. Saved settings can take effect on the next
        page load of any site with the widget embedded.
      </p>

      <form onSubmit={save} style={styles.form}>
        <label style={styles.field}>
          Bot name
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={80}
            style={styles.input}
          />
        </label>

        <label style={styles.field}>
          Welcome message
          <textarea
            value={welcome}
            onChange={(event) => setWelcome(event.target.value)}
            maxLength={500}
            rows={3}
            style={styles.input}
          />
          <span style={styles.sub}>Shown before the visitor types. Blank means no greeting.</span>
        </label>

        <label style={styles.field}>
          Confidence threshold: {threshold.toFixed(2)}
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={threshold}
            onChange={(event) => setThreshold(Number(event.target.value))}
            style={styles.range}
          />
          <span style={styles.sub}>
            Below this, the bot refuses rather than guessing. Lower means more answers and
            more wrong ones.
          </span>
        </label>

        <label style={styles.field}>
          Colour
          <span style={styles.row}>
            <input
              value={colour}
              onChange={(event) => setColour(event.target.value)}
              placeholder={bot.appearance_defaults.primaryColor}
              maxLength={7}
              style={{ ...styles.input, maxWidth: 140 }}
            />
            <span style={{ ...styles.swatch, background: effectiveColour }} aria-hidden />
            <span style={styles.sub}>
              #rgb or #rrggbb. Currently using {effectiveColour}
              {colourIsDefault ? " (the default)" : ""}.
            </span>
          </span>
        </label>

        <label style={styles.field}>
          Position
          <select
            value={position}
            onChange={(event) => setPosition(event.target.value)}
            style={{ ...styles.input, maxWidth: 200 }}
          >
            <option value="bottom-right">Bottom right</option>
            <option value="bottom-left">Bottom left</option>
          </select>
        </label>

        <label style={styles.checkboxRow}>
          <input
            type="checkbox"
            checked={poweredBy}
            onChange={(event) => setPoweredBy(event.target.checked)}
          />
          Show &ldquo;Powered by EVA&rdquo;
        </label>

        {error ? <p role="alert" style={styles.error}>{error}</p> : null}
        {notice ? <p style={styles.notice}>{notice}</p> : null}

        <button type="submit" disabled={busy} style={styles.button}>
          {busy ? "Saving…" : "Save"}
        </button>
      </form>

      <section style={styles.install}>
        <h2 style={styles.h2}>Put it on your site</h2>
        <p style={styles.hint}>
          Paste this just before <code>&lt;/body&gt;</code> on the site you want the
          chat on.
        </p>
        {embed ? (
          <>
            <pre style={styles.pre}>
              <code>{embed.snippet}</code>
            </pre>
            <CopyButton text={embed.snippet} />
            <p style={styles.sub}>
              The <code>data-bot</code> value identifies your bot. It is public — anyone
              who views your page can read it — and is not a credential.
            </p>
            {/* The widget only serves domains marked verified, and domain
                registration/verification is not built yet, so a snippet pasted today is
                refused with origin_not_allowed. Saying "just paste this" without that
                would send an owner to debug a problem they cannot fix from here. */}
            <p style={styles.warning}>
              <strong>Not yet active:</strong> the widget only answers on domains that
              are registered and verified for this bot, and domain setup is not available
              in this build. Pasting the snippet now produces
              <code> origin_not_allowed</code> in the browser console. The snippet is
              shown so the shape is known; it will not serve a chat until domain
              verification ships.
            </p>
          </>
        ) : (
          <p style={styles.sub}>Loading snippet…</p>
        )}
      </section>
    </main>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      style={styles.copy}
      onClick={async () => {
        // Clipboard access needs a secure context and permission; both can be absent.
        // Falling back to selecting the text is better than a button that silently
        // does nothing, because the user can complete the copy themselves.
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 2000);
        } catch {
          setCopied(false);
          window.alert("Could not copy automatically — select the snippet and copy it.");
        }
      }}
    >
      {copied ? "Copied" : "Copy snippet"}
    </button>
  );
}

function describe(failure: unknown): string {
  if (failure instanceof ApiFailure) return `${failure.code}: ${failure.message}`;
  return "Could not reach the server.";
}

const styles: Record<string, React.CSSProperties> = {
  page: { fontFamily: "system-ui, sans-serif", padding: "1.5rem 2rem", maxWidth: 620 },
  h1: { fontSize: "1.35rem", margin: "0 0 1rem" },
  hint: { color: "#4b5563", fontSize: "0.85rem", margin: "0 0 1.25rem" },
  form: { display: "flex", flexDirection: "column", gap: "1rem" },
  field: { display: "flex", flexDirection: "column", fontSize: "0.82rem", color: "#374151" },
  row: { display: "flex", alignItems: "center", gap: "0.6rem", marginTop: "0.25rem" },
  input: {
    marginTop: "0.25rem",
    padding: "0.4rem",
    border: "1px solid #d1d5db",
    borderRadius: 4,
    fontSize: "0.9rem",
    fontFamily: "inherit",
  },
  range: { marginTop: "0.4rem" },
  sub: { color: "#6b7280", fontSize: "0.75rem", marginTop: "0.25rem" },
  swatch: { width: 22, height: 22, borderRadius: 4, border: "1px solid #d1d5db" },
  checkboxRow: { display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.85rem" },
  button: {
    alignSelf: "flex-start",
    padding: "0.45rem 1rem",
    border: "1px solid #111827",
    background: "#111827",
    color: "#ffffff",
    borderRadius: 4,
    cursor: "pointer",
    fontSize: "0.9rem",
  },
  error: { color: "#991b1b", background: "#fee2e2", padding: "0.5rem 0.7rem", borderRadius: 4, fontSize: "0.85rem" },
  notice: { color: "#065f46", background: "#d1fae5", padding: "0.5rem 0.7rem", borderRadius: 4, fontSize: "0.85rem" },
  warning: { color: "#92400e", background: "#fef3c7", padding: "0.5rem 0.7rem", borderRadius: 4, fontSize: "0.85rem" },
  install: { marginTop: "2.5rem", borderTop: "1px solid #e5e7eb", paddingTop: "1.25rem" },
  h2: { fontSize: "1rem", margin: "0 0 0.5rem" },
  pre: {
    background: "#111827",
    color: "#f9fafb",
    padding: "0.7rem 0.9rem",
    borderRadius: 4,
    fontSize: "0.78rem",
    overflowX: "auto",
    margin: "0 0 0.6rem",
  },
  copy: {
    padding: "0.35rem 0.8rem",
    border: "1px solid #d1d5db",
    background: "#ffffff",
    borderRadius: 4,
    cursor: "pointer",
    fontSize: "0.8rem",
  },
};
