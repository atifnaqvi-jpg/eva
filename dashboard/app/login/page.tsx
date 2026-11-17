"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiFailure, login, register } from "@/lib/api";

/**
 * Sign in / create a workspace.
 *
 * This screen exists because the other screens could only say "log in to view this"
 * with no way to do it — the dashboard was unreachable in-product even though the API
 * had `/auth/login` all along.
 *
 * Registration is offered alongside login rather than hidden behind a separate flow:
 * M5's owner critical path starts at "sign up", and a product whose first screen is a
 * login box with no account is a dead end for its only user.
 */
export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantName, setTenantName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") {
        await login(email, password);
      } else {
        await register(email, password, tenantName || "My workspace");
      }
      router.push("/conversations");
      router.refresh();
    } catch (failure) {
      if (failure instanceof ApiFailure) {
        // The API already distinguishes these; showing its code alongside the message
        // is what lets a user tell "wrong password" from "already registered".
        setError(`${failure.code}: ${failure.message}`);
      } else {
        setError("Could not reach the server.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={styles.page}>
      <h1 style={styles.h1}>
        {mode === "login" ? "Sign in" : "Create a workspace"}
      </h1>

      <form onSubmit={submit} style={styles.form}>
        <label style={styles.field}>
          Email
          <input
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            style={styles.input}
          />
        </label>

        <label style={styles.field}>
          Password
          <input
            type="password"
            required
            minLength={12}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            style={styles.input}
          />
        </label>

        {mode === "register" ? (
          <label style={styles.field}>
            Workspace name
            <input
              type="text"
              value={tenantName}
              onChange={(event) => setTenantName(event.target.value)}
              placeholder="My workspace"
              style={styles.input}
            />
          </label>
        ) : null}

        {error ? (
          <p role="alert" style={styles.error}>
            {error}
          </p>
        ) : null}

        <button type="submit" disabled={busy} style={styles.button}>
          {busy ? "Working…" : mode === "login" ? "Sign in" : "Create workspace"}
        </button>
      </form>

      <p style={styles.switchRow}>
        {mode === "login" ? "No account yet?" : "Already have an account?"}{" "}
        <button
          type="button"
          style={styles.linkButton}
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setError(null);
          }}
        >
          {mode === "login" ? "Create a workspace" : "Sign in"}
        </button>
      </p>
    </main>
  );
}

const styles: Record<string, React.CSSProperties> = {
  page: { fontFamily: "system-ui, sans-serif", padding: "3rem 2rem", maxWidth: 380 },
  h1: { fontSize: "1.3rem", margin: "0 0 1.25rem" },
  form: { display: "flex", flexDirection: "column", gap: "0.75rem" },
  field: { display: "flex", flexDirection: "column", fontSize: "0.8rem", color: "#374151" },
  input: {
    marginTop: "0.2rem",
    padding: "0.45rem",
    border: "1px solid #d1d5db",
    borderRadius: 4,
    fontSize: "0.95rem",
  },
  button: {
    padding: "0.5rem 0.9rem",
    border: "1px solid #111827",
    background: "#111827",
    color: "#ffffff",
    borderRadius: 4,
    cursor: "pointer",
    fontSize: "0.9rem",
  },
  error: {
    color: "#991b1b",
    background: "#fee2e2",
    padding: "0.5rem 0.7rem",
    borderRadius: 4,
    fontSize: "0.85rem",
    margin: 0,
  },
  switchRow: { marginTop: "1.25rem", fontSize: "0.85rem", color: "#374151" },
  linkButton: {
    border: "none",
    background: "none",
    color: "#1d4ed8",
    cursor: "pointer",
    padding: 0,
    fontSize: "0.85rem",
    textDecoration: "underline",
  },
};
