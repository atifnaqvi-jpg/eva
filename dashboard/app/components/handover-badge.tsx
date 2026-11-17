import type { HandoverBadge as Badge } from "@/lib/api";

/**
 * Handover state as a badge.
 *
 * `pending` is rendered as **ambiguous**, not as "waiting for the visitor": the
 * database uses one status for two situations — a visitor who has not submitted the
 * contact form, and a delivery that threw before recording an outcome. Showing
 * "waiting" would be a claim the record cannot support, so the badge says what is
 * actually known.
 */
export function HandoverBadge({ badge }: { badge: Badge | null }) {
  if (!badge) return <span style={styles.none}>—</span>;

  const { status, ambiguous, reason } = badge;
  const label = ambiguous ? `${status} (ambiguous)` : status;
  const background = ambiguous
    ? "#fef3c7"
    : status === "failed"
      ? "#fee2e2"
      : status === "notified"
        ? "#dcfce7"
        : "#e5e7eb";
  const colour = ambiguous ? "#92400e" : status === "failed" ? "#991b1b" : "#166534";

  return (
    <span
      style={{ ...styles.badge, background, color: colour }}
      title={
        ambiguous
          ? `${reason}: pending means either the visitor has not submitted the contact ` +
            `form, or delivery failed before an outcome was recorded. The record cannot ` +
            `distinguish them.`
          : `${reason}: ${status}`
      }
    >
      {label}
    </span>
  );
}

const styles: Record<string, React.CSSProperties> = {
  badge: {
    display: "inline-block",
    padding: "0.1rem 0.45rem",
    borderRadius: 999,
    fontSize: "0.75rem",
    fontWeight: 600,
    whiteSpace: "nowrap",
  },
  none: { color: "#9ca3af" },
};
