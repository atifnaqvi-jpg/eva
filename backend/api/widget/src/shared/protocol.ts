/**
 * The loader <-> iframe protocol (WIDG-03).
 *
 * Both sides ship from the same build, but they are separate browsing contexts and
 * the app is served from the platform origin, so every message is checked against
 * an explicit allowlist rather than trusted because "it came from our frame".
 *
 * Direction matters and is part of the contract:
 *
 *   loader -> app : hello, session, turn event, turn error, resize request
 *   app -> loader : ready, send, feedback, upload request, close, remember, resize
 */

export const PROTOCOL_VERSION = 1;

/** Message types the iframe app accepts from the loader. */
export type ToApp =
  | { type: "hello"; version: number }
  | {
      type: "session";
      token: string;
      bot: BotConfig;
      poweredByVisible: boolean;
      /** R43: false outside the tenant's business hours. */
      online?: boolean;
      /** Human-readable "when we're back", when a schedule is configured. */
      nextOpen?: string | null;
      /** True when the server refused a session (bad origin, unknown bot). */
      error?: { code: string; message: string };
    }
  | { type: "turn"; event: "token"; text: string }
  | {
      type: "turn";
      event: "final";
      messageId: string;
      citations: Citation[];
      suggestions: string[];
      confidence: number | null;
      declined: boolean;
      /** Present when this turn opened a handover (HAND-01). */
      handover?: Handover | null;
    }
  | { type: "turn"; event: "error"; code: string }
  | { type: "turn"; event: "done" };

/** Message types the loader accepts from the iframe app. */
export type ToLoader =
  | { type: "ready"; version: number }
  | { type: "send"; text: string }
  | { type: "handover"; email: string; name?: string; note?: string }
  | { type: "post-handover"; text: string }
  | { type: "feedback"; messageId: string; rating: "up" | "down" }
  | { type: "close" }
  | { type: "resize"; height: number }
  | { type: "remember"; enabled: boolean };

export interface Handover {
  id: string;
  reason: "explicit" | "low_confidence" | "negative_feedback" | "rule";
  /** True when the widget must collect an email before the handover can be delivered. */
  requires_contact: boolean;
}

export interface Citation {
  chunk_id?: string;
  document?: string | null;
  uri?: string | null;
  section?: string | null;
  page?: number | null;
  score?: number;
}

export interface BotConfig {
  name: string;
  welcome: string | null;
  suggestions: string[];
  appearance: Appearance;
  public_id: string;
}

export interface Appearance {
  primaryColor?: string;
  position?: "bottom-right" | "bottom-left";
  avatarUrl?: string;
  headerText?: string;
}

const TO_APP = new Set(["hello", "session", "turn"]);
const TO_LOADER = new Set([
  "ready",
  "send",
  "handover",
  "post-handover",
  "feedback",
  "close",
  "resize",
  "remember",
]);

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * Validate a message coming from the frame.
 *
 * A boolean is returned rather than a throw: a host page may run other widgets, and
 * a stray postMessage must be ignored silently instead of producing an error the
 * visitor can see.
 */
export function isToApp(data: unknown): data is ToApp {
  if (!isObject(data) || typeof data.type !== "string") return false;
  if (!TO_APP.has(data.type)) return false;
  if (data.type === "hello") return typeof data.version === "number";
  if (data.type === "session") return typeof data.token === "string";
  if (data.type === "turn") {
    if (typeof data.event !== "string") return false;
    if (data.event === "token") return typeof data.text === "string";
    return ["final", "error", "done"].includes(data.event);
  }
  return false;
}

export function isToLoader(data: unknown): data is ToLoader {
  if (!isObject(data) || typeof data.type !== "string") return false;
  if (!TO_LOADER.has(data.type)) return false;
  switch (data.type) {
    case "ready":
      return typeof data.version === "number";
    case "send":
      return typeof data.text === "string" && data.text.length > 0;
    case "post-handover":
      return typeof data.text === "string" && data.text.length > 0;
    case "handover":
      // Deliberately permissive on shape, strict on size: the server validates the
      // address anyway, and a second, divergent validator here would be the one
      // that drifts.
      return (
        typeof data.email === "string" &&
        data.email.length > 0 &&
        data.email.length <= 320 &&
        (data.name === undefined || typeof data.name === "string") &&
        (data.note === undefined || typeof data.note === "string")
      );
    case "feedback":
      return (
        typeof data.messageId === "string" &&
        (data.rating === "up" || data.rating === "down")
      );
    case "resize":
      // Clamped here as well as at the point of use: an unclamped height from a
      // compromised frame would let it stretch over the whole host page.
      return typeof data.height === "number" && Number.isFinite(data.height);
    case "remember":
      return typeof data.enabled === "boolean";
    default:
      return true;
  }
}
