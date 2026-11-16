/**
 * EVA widget loader (ARCHITECTURE §9.3, §12.1, §12.2 — WIDG-01/03/04).
 *
 * This file is the *only* code that runs in the host page. It must stay under
 * 10 KB gzipped (§12.2), so it is deliberately procedural: no framework, no
 * polyfills, no CSS framework, and the chat UI lives in the iframe app.
 *
 * Two architectural decisions are load-bearing:
 *
 * **Nothing is created before the click** (R41 / §19.3). The launcher button is one
 * element; the iframe, the session request and every storage write happen inside
 * the click handler. A pageview that never opens the chat leaves no trace — that is
 * what the browser-storage audit (SEC-08) measures, and it is why the config fetch
 * happens at click time instead of on load.
 *
 * **API calls happen here, never in the iframe.** The iframe is served from the
 * platform origin, so a request from inside it carries the platform's `Origin`
 * header and would fail §12.3's "Origin must match bot_domains" check. Calling from
 * the host page keeps the platform's own domain on the allowlist requirement honest
 * and keeps the token in the host page.
 */

import { isToLoader, PROTOCOL_VERSION } from "./shared/protocol";
import type { Citation, Handover } from "./shared/protocol";
import { createIdentity } from "./shared/identity";

interface LoaderConfig {
  apiBase: string;
  appUrl: string;
  botId: string;
  primaryColor: string;
  position: "bottom-right" | "bottom-left";
  avatarUrl: string | null;
}

const Z = 2147483000;
const BTN = 56;
const MARGIN = 20;
const MAX_HEIGHT = 640;
const MIN_HEIGHT = 380;

function readConfig(): LoaderConfig | null {
  const script = document.currentScript as HTMLScriptElement | null;
  if (!script) return null;
  const botId = script.getAttribute("data-bot");
  if (!botId) return null;
  const base = script.getAttribute("data-api") ?? new URL(script.src).origin;
  return {
    apiBase: base.replace(/\/$/, ""),
    // The app is served by the API origin: one deployable, no second CDN to
    // provision for the MVP, and the iframe is same-site with its own API.
    appUrl: `${base.replace(/\/$/, "")}/widget/app`,
    botId,
    primaryColor: script.getAttribute("data-color") ?? "#1f6feb",
    position:
      script.getAttribute("data-position") === "left" ? "bottom-left" : "bottom-right",
    avatarUrl: script.getAttribute("data-avatar"),
  };
}

function randomId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function start(): void {
  const found = readConfig();
  if (!found) return;
  // Bound to a `const` after the guard: `cfg` is captured by the closures below and
  // a mutable `let` would need a non-null assertion in each of them.
  const cfg: LoaderConfig = found;

  const identity = createIdentity(window, randomId);
  const side = cfg.position === "bottom-left" ? "left" : "right";

  const button = document.createElement("button");
  button.type = "button";
  button.setAttribute("aria-label", "Open chat");
  button.setAttribute("aria-expanded", "false");
  button.style.cssText = [
    "position:fixed",
    `bottom:${MARGIN}px`,
    `${side}:${MARGIN}px`,
    `width:${BTN}px`,
    `height:${BTN}px`,
    "border-radius:50%",
    "border:0",
    `background:${cfg.primaryColor}`,
    "color:#fff",
    "cursor:pointer",
    "box-shadow:0 4px 16px rgba(0,0,0,.24)",
    `z-index:${Z}`,
    "display:flex",
    "align-items:center",
    "justify-content:center",
    "padding:0",
  ].join(";");

  if (cfg.avatarUrl) {
    const img = document.createElement("img");
    img.src = cfg.avatarUrl;
    img.alt = "";
    img.style.cssText = "width:100%;height:100%;border-radius:50%;object-fit:cover";
    button.append(img);
  } else {
    // Inline SVG rather than a font or an image request: one fewer network call
    // before the visitor has consented to anything.
    button.innerHTML =
      '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7A2.5 2.5 0 0 1 17.5 15H9l-4 4v-4.2A2.5 2.5 0 0 1 4 12.5v-7Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>';
  }

  let frame: HTMLIFrameElement | null = null;
  let token: string | null = null;
  let session: unknown = null;
  let conversationId: string | null = null;
  let open = false;
  let connecting = false;
  // Set once a handover exists, so later messages take the append path instead of
  // the streaming one.
  let handedOver = false;

  function sizeFrame(height: number): void {
    if (!frame) return;
    const h = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, Math.round(height)));
    frame.style.height = `${h}px`;
  }

  function sendApp(message: unknown): void {
    frame?.contentWindow?.postMessage(message, cfg.appUrl);
  }

  function createFrame(): void {
    frame = document.createElement("iframe");
    frame.src = cfg.appUrl;
    frame.title = "Chat";
    frame.setAttribute("allow", "clipboard-write");
    // No `allow-same-origin`: the app needs scripts and its own storage, and
    // granting the platform origin to host-page-controlled code would let the
    // widget's storage be read by anything else that framed it.
    frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms");
    // Mobile (R38): the frame becomes the viewport, not a 380px card. Done with a
    // media query in a stylesheet rather than inline styles, because the inline
    // `bottom`/`height` above would otherwise have to be recomputed on every
    // orientation change and on the keyboard opening.
    const rule = document.createElement("style");
    rule.textContent =
      "@media (max-width:480px){.eva-frame{inset:0 !important;width:100vw !important;" +
      "height:100dvh !important;border-radius:0 !important}}";
    document.head.append(rule);
    frame.className = "eva-frame";
    frame.style.cssText = [
      "position:fixed",
      `bottom:${MARGIN + BTN + 12}px`,
      `${side}:${MARGIN}px`,
      "width:min(380px, calc(100vw - 32px))",
      `height:${MIN_HEIGHT}px`,
      "border:0",
      "border-radius:16px",
      "box-shadow:0 12px 40px rgba(0,0,0,.28)",
      `z-index:${Z}`,
      "background:transparent",
      "color-scheme:normal",
    ].join(";");
    frame.hidden = true;
    document.body.append(frame);
  }

  async function openChat(announce = true): Promise<void> {
    open = true;
    button.setAttribute("aria-expanded", "true");
    button.setAttribute("aria-label", "Close chat");
    if (!frame) createFrame();
    if (frame) frame.hidden = false;

    // First act of the visitor: now, and only now, does anything leave the page.
    //
    // `connecting` guards the double-start: a visitor can click before the frame
    // has announced itself, and without the flag the click and the `ready` message
    // would both mint a session, creating two conversations for one greeting.
    if (token && session) {
      if (announce) sendApp(session);
      return;
    }
    if (connecting) return;
    if (!token) {
      connecting = true;
      try {
        const response = await fetch(`${cfg.apiBase}/api/v1/widget/session`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "omit",
          body: JSON.stringify({ bot_public_id: cfg.botId }),
        });
        const body = await response.json();
        if (!response.ok) {
          sendApp({
            type: "session",
            token: "",
            bot: null,
            poweredByVisible: true,
            error: {
              code: body?.detail?.error?.code ?? "session_failed",
              message: body?.detail?.error?.message ?? "chat is unavailable",
            },
          });
          connecting = false;
          return;
        }
        token = body.token;
        session = {
          type: "session",
          token,
          bot: body.bot,
          poweredByVisible: body.powered_by_visible,
          // R43. Carried on the session so the app can lead with the offline
          // banner before the visitor types, rather than after a wasted turn.
          online: body.online !== false,
          nextOpen: body.next_open ?? null,
        };
        sendApp(session);
      } catch {
        sendApp({
          type: "session",
          token: "",
          bot: null,
          poweredByVisible: true,
          error: { code: "network", message: "chat is unavailable" },
        });
      } finally {
        connecting = false;
      }
    } else {
      sendApp({ type: "hello", version: PROTOCOL_VERSION });
    }
  }

  function closeChat(): void {
    open = false;
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-label", "Open chat");
    if (frame) frame.hidden = true;
    frame?.contentWindow?.focus();
    button.focus();
  }

  async function ensureConversation(): Promise<string | null> {
    if (conversationId) return conversationId;
    if (!token) return null;

    // Resume within the tab session (R37). Session-scoped, so closing the tab still
    // means a new conversation; a reload does not.
    const resumed = identity.conversation();
    if (resumed) {
      conversationId = resumed;
      return resumed;
    }
    try {
      const response = await fetch(`${cfg.apiBase}/api/v1/widget/conversations`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        credentials: "omit",
        body: JSON.stringify({
          visitor_session_id: identity.id(),
          page_url: location.href.slice(0, 2000),
        }),
      });
      if (!response.ok) return null;
      const body = await response.json();
      const created: string = body.conversation_id;
      conversationId = created;
      identity.setConversation(created);
      return created;
    } catch {
      return null;
    }
  }

  /**
   * Read an SSE stream from a POST response.
   *
   * `EventSource` cannot be used: it only issues GET without headers, and the token
   * travels in `Authorization`. The frames are parsed by hand, which is why the
   * parsing tolerates a chunk boundary anywhere — the tests feed it one byte at a
   * time.
   */
  async function streamTurn(text: string): Promise<void> {
    const cid = await ensureConversation();
    if (!cid || !token) {
      sendApp({ type: "turn", event: "error", code: "no_conversation" });
      return;
    }
    let response: Response;
    try {
      response = await fetch(
        `${cfg.apiBase}/api/v1/widget/conversations/${cid}/messages`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
            Accept: "text/event-stream",
          },
          credentials: "omit",
          body: JSON.stringify({ text }),
        },
      );
    } catch {
      sendApp({ type: "turn", event: "error", code: "network" });
      return;
    }
    if (!response.ok || !response.body) {
      sendApp({ type: "turn", event: "error", code: `http_${response.status}` });
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let split: number;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const frameText = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        dispatch(frameText);
      }
    }
    sendApp({ type: "turn", event: "done" });
  }

  function dispatch(frameText: string): void {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of frameText.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(dataLines.join("\n"));
    } catch {
      return;
    }
    if (event === "token") {
      sendApp({ type: "turn", event: "token", text: String(payload.text ?? "") });
    } else if (event === "final") {
      sendApp({
        type: "turn",
        event: "final",
        messageId: String(payload.message_id ?? ""),
        citations: (payload.citations ?? []) as Citation[],
        suggestions: (payload.suggestions ?? []) as string[],
        confidence: typeof payload.confidence === "number" ? payload.confidence : null,
        declined: payload.declined === true,
        handover: (payload.handover as Handover | null | undefined) ?? null,
      });
    } else if (event === "error") {
      sendApp({
        type: "turn",
        event: "error",
        code: String(payload.code ?? "turn_failed"),
      });
    }
  }

  /**
   * Submit the contact form (HAND-02).
   *
   * The visitor's answer is reported back as a normal `turn final` so the app has
   * one code path for "the conversation moved on", rather than a second event type
   * that only this feature emits.
   */
  async function submitHandover(payload: {
    email: string;
    name?: string | undefined;
    note?: string | undefined;
  }): Promise<void> {
    const cid = await ensureConversation();
    if (!cid || !token) {
      sendApp({ type: "turn", event: "error", code: "no_conversation" });
      return;
    }
    let response: Response;
    try {
      response = await fetch(
        `${cfg.apiBase}/api/v1/widget/conversations/${cid}/handover`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
          },
          credentials: "omit",
          body: JSON.stringify(payload),
        },
      );
    } catch {
      sendApp({ type: "turn", event: "error", code: "network" });
      return;
    }
    if (!response.ok) {
      // The server's reason is worth surfacing: "invalid_email" is the visitor's
      // to fix, "origin_not_allowed" is not.
      let code = `http_${response.status}`;
      try {
        const body = await response.json();
        code = body?.detail?.error?.code ?? code;
      } catch {
        /* body was not JSON; the status is all we have */
      }
      sendApp({ type: "turn", event: "error", code });
      return;
    }
    handedOver = true;
    let reason = "explicit";
    try {
      const body = await response.json();
      reason = body?.reason ?? reason;
    } catch {
      /* the handover exists either way; the reason is cosmetic */
    }
    // The `handover` payload is what tells the app to close the form and stop
    // offering "Talk to a human". Sending a bare `final` left the form on screen
    // and every later message spinning, because the app never learned it worked.
    sendApp({
      type: "turn",
      event: "final",
      messageId: "",
      citations: [],
      suggestions: [],
      confidence: null,
      declined: false,
      handover: { id: "", reason, requires_contact: false },
    });
  }

  /**
   * Append a message after the AI has stepped aside (HAND-05).
   *
   * Deliberately not `streamTurn`: a handed-over conversation answers with an error
   * event, which the app would render as "something went wrong" — the opposite of
   * what the visitor was just promised.
   */
  async function appendAfterHandover(text: string): Promise<void> {
    const cid = await ensureConversation();
    if (!cid || !token) {
      sendApp({ type: "turn", event: "error", code: "no_conversation" });
      return;
    }
    try {
      await fetch(
        `${cfg.apiBase}/api/v1/widget/conversations/${cid}/messages/after-handover`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
          },
          credentials: "omit",
          body: JSON.stringify({ text }),
        },
      );
    } catch {
      sendApp({ type: "turn", event: "error", code: "network" });
      return;
    }
    sendApp({ type: "turn", event: "done" });
  }

  function onMessage(event: MessageEvent): void {
    if (!frame || event.source !== frame.contentWindow) return;
    if (event.origin !== new URL(cfg.appUrl).origin) return;
    if (!isToLoader(event.data)) return;
    const message = event.data;
    switch (message.type) {
      case "ready":
        if (message.version !== PROTOCOL_VERSION) return;
        void openChat();
        break;
      case "send":
        // After a handover the AI must not answer, but the visitor's words still
        // have to reach the transcript the owner is reading (§13.3).
        if (handedOver) void appendAfterHandover(message.text.slice(0, 4000));
        else void streamTurn(message.text.slice(0, 4000));
        break;
      case "handover":
        void submitHandover({
          email: message.email,
          name: message.name,
          note: message.note,
        });
        break;
      case "post-handover":
        handedOver = true;
        void appendAfterHandover(message.text.slice(0, 4000));
        break;
      case "resize":
        sizeFrame(message.height);
        break;
      case "close":
        closeChat();
        break;
      case "remember":
        identity.remember(message.enabled);
        break;
      case "feedback":
        void sendFeedback(message.messageId, message.rating);
        break;
    }
  }

  async function sendFeedback(
    messageId: string,
    rating: "up" | "down",
  ): Promise<void> {
    if (!token) return;
    try {
      await fetch(`${cfg.apiBase}/api/v1/widget/messages/${messageId}/feedback`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        credentials: "omit",
        body: JSON.stringify({ rating }),
      });
    } catch {
      /* a lost vote is not worth an error in the visitor's face */
    }
  }

  button.addEventListener("click", () => {
    if (open) closeChat();
    else void openChat();
  });

  window.addEventListener("message", onMessage);
  document.body.append(button);
  void identity;
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start, { once: true });
} else {
  start();
}
