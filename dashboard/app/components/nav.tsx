"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { currentSession, logout, type Session } from "@/lib/api";

/**
 * The dashboard navigation shell (ADMN-01).
 *
 * Only the destinations that exist are listed. §18.1 names six sections, but Bot,
 * Knowledge and Settings are ADMN-03/04/10 — still backlog — and linking a nav item
 * to a 404 is worse than not linking it. Each is added when its screen lands.
 */
const NAV = [
  { href: "/", label: "Home" },
  { href: "/conversations", label: "Conversations" },
  { href: "/analytics", label: "Analytics" },
];

export function Nav() {
  const pathname = usePathname();
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);

  useEffect(() => {
    let cancelled = false;
    currentSession()
      .then((value) => {
        if (!cancelled) setSession(value);
      })
      .catch(() => {
        // 401 is the normal signed-out state, not something to surface.
        if (!cancelled) setSession(null);
      });
    return () => {
      cancelled = true;
    };
  }, [pathname]);

  return (
    <nav style={styles.nav} aria-label="Main">
      <span style={styles.brand}>EVA</span>
      {NAV.map((item) => {
        const active =
          item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            style={{ ...styles.link, ...(active ? styles.active : {}) }}
          >
            {item.label}
          </Link>
        );
      })}

      <span style={styles.spacer} />

      {session ? (
        <>
          <span style={styles.email}>{session.email}</span>
          <button
            type="button"
            style={styles.linkButton}
            onClick={async () => {
              await logout().catch(() => undefined);
              setSession(null);
              router.push("/login");
              router.refresh();
            }}
          >
            Sign out
          </button>
        </>
      ) : (
        <Link href="/login" style={styles.link}>
          Sign in
        </Link>
      )}
    </nav>
  );
}

const styles: Record<string, React.CSSProperties> = {
  nav: {
    display: "flex",
    alignItems: "center",
    gap: "1.25rem",
    padding: "0.75rem 1.5rem",
    borderBottom: "1px solid #e5e7eb",
    fontFamily: "system-ui, sans-serif",
  },
  brand: { fontWeight: 700, letterSpacing: "0.05em", marginRight: "0.5rem" },
  link: { color: "#374151", textDecoration: "none", fontSize: "0.9rem" },
  active: { color: "#111827", fontWeight: 600, textDecoration: "underline" },
  spacer: { flex: 1 },
  email: { color: "#6b7280", fontSize: "0.8rem" },
  linkButton: {
    border: "none",
    background: "none",
    color: "#374151",
    fontSize: "0.9rem",
    cursor: "pointer",
    padding: 0,
  },
};
