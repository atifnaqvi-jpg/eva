/**
 * The chat app (WIDG-02/06/07/08/11, ARCHITECTURE §12.2).
 *
 * Runs inside the iframe with a Preact island of its own, so the host page's CSS,
 * its framework and its versions cannot reach it. It never talks to the API: every
 * request goes through the loader via postMessage, because a fetch from this origin
 * would carry the platform's `Origin` and fail §12.3's domain check.
 *
 * Accessibility is part of the structure rather than a later pass:
 * transcript is `role="log"` with `aria-live="polite"` so a streamed answer is
 * announced as it arrives; the composer is a real `<form>`; the close control is a
 * `<button>`; focus is trapped while the panel is open and returned on close.
 */

import { render, Fragment } from "preact";
import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import type { Citation, Handover, ToApp } from "../shared/protocol";
import { PROTOCOL_VERSION } from "../shared/protocol";
import { parseBlocks } from "../shared/markdown";
import type { Block, Inline } from "../shared/markdown";

interface Appearance {
  primaryColor?: string;
  avatarUrl?: string;
  headerText?: string;
}

interface Bot {
  name: string;
  welcome: string | null;
  suggestions: string[];
  appearance: Appearance;
  public_id: string;
}

interface Message {
  /** Local React key. Never sent to the server. */
  id: string;
  /**
   * The server's message id, available once the turn's `final` event arrives.
   * CSAT must use this: the local key is a timestamp string, not a UUID, so posting
   * it as a message id made every vote fail.
   */
  serverId?: string;
  role: "visitor" | "bot";
  text: string;
  citations: Citation[];
  declined: boolean;
  /** Set once the turn this message belongs to has finished. */
  complete: boolean;
  /** Feedback already given, so the buttons reflect it. */
  rating?: "up" | "down";
}

const CSS = `
:root{color-scheme:light}
*{box-sizing:border-box}
html,body{margin:0;height:100%;font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#111827;background:#fff}
#root{height:100%}
.wrap{display:flex;flex-direction:column;height:100%;background:#fff}
header{display:flex;align-items:center;gap:10px;padding:12px 14px;background:var(--brand);color:#fff}
header h1{font-size:15px;margin:0;font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.avatar{width:28px;height:28px;border-radius:50%;object-fit:cover;background:rgba(255,255,255,.25)}
.close{background:transparent;border:0;color:#fff;cursor:pointer;min-width:44px;min-height:44px;border-radius:8px;font-size:18px;line-height:1}
.close:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid #111827;outline-offset:2px}
.log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:88%;border-radius:14px;padding:9px 12px;word-wrap:break-word;overflow-wrap:anywhere}
.msg.bot{background:#f1f3f5;align-self:flex-start;border-top-left-radius:4px}
.msg.visitor{background:var(--brand);color:#fff;align-self:flex-end;border-top-right-radius:4px}
.msg p{margin:0 0 6px}.msg p:last-child{margin-bottom:0}
.msg pre{background:rgba(0,0,0,.06);padding:8px;border-radius:8px;overflow-x:auto;margin:6px 0}
.msg code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
.msg ul,.msg ol{margin:4px 0;padding-left:20px}
.cites{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{display:inline-block;font-size:12px;padding:3px 9px;border-radius:999px;background:#fff;border:1px solid #d1d5db;color:#374151;text-decoration:none;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
a.chip:hover{background:#f3f4f6}
.rate{display:flex;gap:4px;margin-top:6px}
.rate button{background:transparent;border:1px solid #d1d5db;border-radius:8px;cursor:pointer;min-width:44px;min-height:44px;font-size:15px;line-height:1}
.rate button[aria-pressed=true]{background:#e5e7eb;border-color:#9ca3af}
/* Suggestion rail, rendered inside .log so it scrolls with the conversation instead of
   reserving height above the composer. As a sibling band it ate ~110px of a 560px frame
   on every turn, including after an answer, which is when the conversation matters most. */
.sugg{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
/* Wrapped, capped at half the rail: three chips take two rows and all stay visible.
   A single sideways-scrolling row hid the third chip behind an invisible edge. */
.sugg button{background:#fff;border:1px solid #d1d5db;border-radius:999px;
      padding:9px 12px;font-size:13px;cursor:pointer;min-height:44px;
      flex:1 1 calc(50% - 6px);max-width:calc(50% - 3px);
      text-align:left;line-height:1.25;
      overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;
      -webkit-box-orient:vertical}
.sugg button:hover{background:#f3f4f6}
form{display:flex;gap:8px;padding:10px 14px;border-top:1px solid #e5e7eb;align-items:flex-end}
textarea{flex:1;resize:none;border:1px solid #d1d5db;border-radius:10px;padding:9px 11px;font:inherit;max-height:96px;min-height:44px}
footer{padding:0 14px 8px;font-size:11px;color:#6b7280;display:flex;justify-content:space-between;gap:8px}
.foot-link{color:#6b7280}
.typing{display:inline-flex;gap:3px;align-items:center;padding:4px 2px}
.typing i{width:6px;height:6px;border-radius:50%;background:#9ca3af;animation:b 1.2s infinite}
.typing i:nth-child(2){animation-delay:.2s}.typing i:nth-child(3){animation-delay:.4s}
@keyframes b{0%,60%,100%{opacity:.3}30%{opacity:1}}
.notice{background:#fef3c7;color:#92400e;padding:8px 12px;font-size:13px;border-radius:8px;margin:8px 14px}
.offline{background:#eef2ff;color:#3730a3;padding:8px 12px;font-size:13px;border-radius:8px;margin:8px 14px}
.human{background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.4);color:#fff;border-radius:8px;padding:6px 10px;font-size:12px;cursor:pointer;white-space:nowrap}
.human:hover{background:rgba(255,255,255,.26)}
/* The composer flex rule applies to every form in the panel, so
   this one has to state its layout explicitly or its labels and inputs lay out as
   one overlapping row. */
.form{display:block;margin:8px 14px;padding:12px;border:1px solid #d1d5db;border-radius:12px;background:#f9fafb}
.form h2{font-size:13px;margin:0 0 8px;font-weight:600}
.form label{display:block;font-size:12px;color:#374151;margin-bottom:8px}
.form input,.form textarea{display:block;width:100%;border:1px solid #d1d5db;border-radius:8px;padding:9px 10px;font:inherit;background:#fff}
.form textarea{min-height:44px;max-height:96px;resize:vertical}
.form label{margin-bottom:6px}
.form .form-error{margin:0 0 10px;font-size:13px;color:#b42318}
.form{flex:1 1 auto;overflow-y:auto;min-height:0}
/* hidden loses to a display declaration, and the composer rule sets one. */
[hidden]{display:none !important}
/* Pinned to the bottom of the scrolling form: the panel is only 380px tall, so
   the submit button must never be the thing that scrolls out of reach. */
.form .actions{position:sticky;bottom:0;display:flex;gap:8px;justify-content:flex-end;background:#f9fafb;padding:8px 0 2px}
.form button{border-radius:8px;padding:10px 14px;font-size:13px;cursor:pointer;min-height:44px}
.form .submit{background:var(--brand);color:#fff;border:0;font-weight:600}
.form .cancel{background:#fff;border:1px solid #d1d5db}
.form .err{color:#b91c1c;font-size:12px;margin:0 0 8px}
@media (prefers-reduced-motion:reduce){.typing i{animation:none}}
/* Mobile sheet (R38): below 480px the panel is the whole viewport, which is what
   a phone keyboard leaves usable. dvh accounts for the collapsing browser chrome;
   iOS Safari otherwise puts the composer under the keyboard. */
@media (max-width:480px){
  html,body,#root{height:100dvh}
  header{padding:10px 12px;padding-top:max(10px,env(safe-area-inset-top))}
  .log{padding:12px}
  form{padding:8px 12px;padding-bottom:max(8px,env(safe-area-inset-bottom))}
  .msg{max-width:92%}
}
/* Touch targets stay ≥44px regardless of pointer type (WCAG 2.5.5 AAA / 2.1 AA
   practical target), so a phone-width viewport on a desktop is still operable. */
@media (pointer:coarse){.close,.rate button,.sugg button,form button{min-width:44px;min-height:44px}}
`;

/** Inline nodes to Preact elements. Text is always a text child, never innerHTML. */
function renderInline(nodes: Inline[]): preact.VNode[] {
  return nodes.map((node, index) => {
    switch (node.kind) {
      case "bold":
        return <strong key={index}>{node.text}</strong>;
      case "italic":
        return <em key={index}>{node.text}</em>;
      case "code":
        return <code key={index}>{node.text}</code>;
      case "link":
        return (
          <a key={index} href={node.href} target="_blank" rel="noopener noreferrer nofollow">
            {node.text}
          </a>
        );
      default:
        return <Fragment key={index}>{node.text}</Fragment>;
    }
  });
}

function renderBlocks(blocks: Block[]): preact.VNode[] {
  return blocks.map((block, index) => {
    switch (block.kind) {
      case "code":
        return (
          <pre key={index}>
            <code>{block.text}</code>
          </pre>
        );
      case "ul":
        return (
          <ul key={index}>
            {block.items.map((item, i) => (
              <li key={i}>{renderInline(item)}</li>
            ))}
          </ul>
        );
      case "ol":
        return (
          <ol key={index}>
            {block.items.map((item, i) => (
              <li key={i}>{renderInline(item)}</li>
            ))}
          </ol>
        );
      default:
        return <p key={index}>{renderInline(block.inline)}</p>;
    }
  });
}

function CitationChips({ citations }: { citations: Citation[] }): preact.VNode {
  return (
    <div class="cites" aria-label="Sources">
      {citations.map((citation, index) => {
        const label = citation.document ?? citation.section ?? "Source";
        return citation.uri ? (
          <a
            key={index}
            class="chip"
            href={citation.uri}
            target="_blank"
            rel="noopener noreferrer"
            title={label}
          >
            {label}
          </a>
        ) : (
          <span key={index} class="chip" title={label}>
            {label}
          </span>
        );
      })}
    </div>
  );
}

export function App(): preact.VNode {
  const [bot, setBot] = useState<Bot | null>(null);
  const [poweredBy, setPoweredBy] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [thinking, setThinking] = useState(false);
  const [draft, setDraft] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [remember, setRemember] = useState(false);
  // R43: the server decides whether the business is open; the app only renders it.
  const [online, setOnline] = useState(true);
  const [nextOpen, setNextOpen] = useState<string | null>(null);
  // HAND-02: the contact form is shown when a handover needs an address, or when
  // the visitor asked for a human directly.
  const [handover, setHandover] = useState<Handover | null>(null);
  // Offline shows the form unprompted, so it also needs its own dismissal; the
  // "Not now" button used to clear `handover` and nothing else, which left the
  // form on screen forever because being offline re-opened it.
  const [dismissedForm, setDismissedForm] = useState(false);
  // The form covers the transcript, so an error appended there is invisible.
  const [formError, setFormError] = useState<string | null>(null);
  const [formEmail, setFormEmail] = useState("");
  const [formName, setFormName] = useState("");
  const [formNote, setFormNote] = useState("");
  // True once a handover exists: the composer still works, but it appends to the
  // transcript instead of asking the AI anything.
  const [handedOver, setHandedOver] = useState(false);

  const logRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const streamingId = useRef<string | null>(null);

  const post = useCallback((message: unknown) => {
    parent.postMessage(message, "*");
  }, []);

  const appendToken = useCallback((text: string) => {
    setMessages((current) => {
      const id = streamingId.current;
      if (!id) return current;
      return current.map((message) =>
        message.id === id ? { ...message, text: message.text + text } : message,
      );
    });
  }, []);

  const finishTurn = useCallback(
    (patch: Partial<Message>) => {
      setMessages((current) => {
        const id = streamingId.current;
        if (!id) return current;
        return current.map((message) =>
          message.id === id ? { ...message, ...patch, complete: true } : message,
        );
      });
      streamingId.current = null;
      setThinking(false);
    },
    [],
  );

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || thinking) return;
      const visitor: Message = {
        id: `v-${Date.now()}`,
        role: "visitor",
        text: trimmed,
        citations: [],
        declined: false,
        complete: true,
      };
      const botMessage: Message = {
        id: `b-${Date.now()}`,
        role: "bot",
        text: "",
        citations: [],
        declined: false,
        complete: false,
      };
      if (handedOver) {
        // The AI has stepped aside; the message is recorded for the human instead
        // of being answered (§13.3). No placeholder bubble: nothing is coming.
        setMessages((current) => [
          ...current,
          visitor,
          {
            ...botMessage,
            text: "Thanks — that's been added to the conversation for the team.",
            complete: true,
          },
        ]);
        setDraft("");
        post({ type: "post-handover", text: trimmed });
        return;
      }
      streamingId.current = botMessage.id;
      setMessages((current) => [...current, visitor, botMessage]);
      setDraft("");
      setSuggestions([]);
      setThinking(true);
      post({ type: "send", text: trimmed });
    },
    [post, thinking, handedOver],
  );

  useEffect(() => {
    const onMessage = (event: MessageEvent): void => {
      const data = event.data as ToApp;
      if (!data || typeof data !== "object" || typeof data.type !== "string") return;
      if (data.type === "session") {
        if (data.error) {
          setNotice(data.error.message);
          return;
        }
        setBot(data.bot);
        setPoweredBy(data.poweredByVisible);
        setSuggestions(data.bot.suggestions ?? []);
        setOnline(data.online !== false);
        setNextOpen(data.nextOpen ?? null);
        return;
      }
      if (data.type === "turn") {
        if (data.event === "token") appendToken(data.text);
        else if (data.event === "final") {
          finishTurn({
            serverId: data.messageId,
            citations: data.citations ?? [],
            declined: data.declined,
          });
          // A form submission has no streaming bubble to finish; acknowledge it in
          // the log so the visitor sees their details went somewhere.
          if (data.handover && !streamingId.current) {
            setMessages((current) => [
              ...current,
              {
                id: `b-${Date.now()}`,
                role: "bot",
                text: "Thanks — the team has your details and this conversation. They'll follow up by email.",
                citations: [],
                declined: false,
                complete: true,
              },
            ]);
          }
          setSuggestions((data.suggestions ?? []).slice(0, 3));
          if (data.handover) {
            setHandover(data.handover);
            setHandedOver(true);
          }
        } else if (data.event === "error") {
          // A form submission has no bubble of its own, so an error would land
          // nowhere and leave the visitor staring at a form that did nothing.
          if (!streamingId.current) {
            setFormError(
              data.code === "invalid_email"
                ? "That email address doesn't look right. Could you check it?"
                : "Sorry — we couldn't send your details. Please try again.",
            );
            setThinking(false);
            return;
          }
          finishTurn({ text: "Sorry, something went wrong. Please try again." });
        } else if (data.event === "done") {
          // The stream is over. Without this the composer stays in its thinking
          // state whenever a turn ends without a `final` (post-handover appends).
          setThinking(false);
          streamingId.current = null;
        }
      }
    };
    window.addEventListener("message", onMessage);
    post({ type: "ready", version: PROTOCOL_VERSION });
    return () => window.removeEventListener("message", onMessage);
  }, [appendToken, finishTurn, post]);

  // Keep the newest message in view as tokens arrive.
  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [messages, thinking]);

  // Focus trap (WIDG-11): the panel is the only focusable region while open.
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const selector =
      'a[href],button:not([disabled]),textarea,input,select,[tabindex]:not([tabindex="-1"])';
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        post({ type: "close" });
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(selector)).filter(
        (element) => element.offsetParent !== null,
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    // Bound on the document only (WIDG-17). Escape must work when focus sits on the
    // body as well as inside the panel — Firefox does not focus the frame's content on
    // load the way Chromium does, so a keydown targeting `body` never reaches a
    // panel-scoped listener. Binding both is what made one press post `close` twice:
    // a keydown in the panel bubbles to the document, so two listeners ran for the one
    // event. One listener at the document owns the key and sees every focused element
    // inside it; the panel is the frame's only content, so the Tab trap is unchanged.
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [post]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // §18.3: outside business hours the contact form comes first, without waiting to
  // be asked. Open hours only show it once a handover actually needs an address.
  const showContactForm =
    !handedOver && (Boolean(handover?.requires_contact) || (!online && !dismissedForm));

  const brand = bot?.appearance.primaryColor ?? "#1f6feb";
  const welcome = useMemo(
    () => bot?.welcome ?? "Hi! Ask me anything about our products and services.",
    [bot],
  );

  return (
    <div class="wrap" style={{ ["--brand" as string]: brand }} ref={panelRef}>
      <style>{CSS}</style>
      <header>
        {bot?.appearance.avatarUrl ? (
          <img class="avatar" src={bot.appearance.avatarUrl} alt="" />
        ) : null}
        <h1 id="title">{bot?.appearance.headerText ?? bot?.name ?? "Chat"}</h1>
        {/* §18.3: always visible, and the direct route to HAND-02 when the AI is
            not what the visitor wants. Hidden once a handover exists — offering it
            twice would suggest the first request did not work. */}
        {!handedOver ? (
          <button
            class="human"
            type="button"
            onClick={() =>
              setHandover({
                id: "",
                reason: "explicit",
                requires_contact: true,
              })
            }
          >
            Talk to a human
          </button>
        ) : null}
        <button
          class="close"
          type="button"
          aria-label="Close chat"
          onClick={() => post({ type: "close" })}
        >
          ✕
        </button>
      </header>

      {notice ? (
        <p class="notice" role="alert">
          {notice}
        </p>
      ) : null}

      {/* R43: outside business hours the visitor is told before they type, and the
          contact form is the primary path rather than the last resort. */}
      {!online ? (
        <p class="offline" role="status">
          {handedOver
            ? "You're in the queue — the team has this conversation."
            : `We're offline right now${nextOpen ? ` — back ${nextOpen}` : ""}. Leave your details and the team will follow up.`}
        </p>
      ) : null}

      {showContactForm ? (
        <form
          class="form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!formEmail.trim()) return;
            setFormError(null);
            post({
              type: "handover",
              email: formEmail.trim(),
              name: formName.trim() || undefined,
              note: formNote.trim() || undefined,
            });
          }}
        >
          <h2>Leave your details</h2>
          {formError ? (
            <p class="form-error" role="alert">
              {formError}
            </p>
          ) : null}
          <label>
            Email
            <input
              type="email"
              required
              maxLength={320}
              value={formEmail}
              onInput={(event) => setFormEmail((event.target as HTMLInputElement).value)}
              placeholder="you@example.com"
            />
          </label>
          <label>
            Name (optional)
            <input
              type="text"
              maxLength={200}
              value={formName}
              onInput={(event) => setFormName((event.target as HTMLInputElement).value)}
            />
          </label>
          <label>
            Anything to add? (optional)
            <textarea
              maxLength={2000}
              value={formNote}
              onInput={(event) => setFormNote((event.target as HTMLTextAreaElement).value)}
            />
          </label>
          <div class="actions">
            <button
              type="button"
              class="cancel"
              onClick={() => {
                setHandover(null);
                setDismissedForm(true);
              }}
            >
              Not now
            </button>
            <button type="submit" class="submit">
              Send to the team
            </button>
          </div>
        </form>
      ) : null}

      <div
        class="log"
        ref={logRef}
        role="log"
        aria-live="polite"
        aria-relevant="additions text"
        aria-labelledby="title"
      >
        {messages.length === 0 ? (
          <div class="msg bot">
            <p>{welcome}</p>
          </div>
        ) : null}
        {messages.map((message) => (
          <div key={message.id} class={`msg ${message.role}`}>
            {message.role === "bot" ? (
              message.text ? (
                renderBlocks(parseBlocks(message.text))
              ) : thinking && !message.complete ? (
                <span class="typing" aria-label="Assistant is typing">
                  <i /> <i /> <i />
                </span>
              ) : null
            ) : (
              <p>{message.text}</p>
            )}
            {message.complete && message.role === "bot" && message.citations.length ? (
              <CitationChips citations={message.citations} />
            ) : null}
            {message.complete &&
            message.role === "bot" &&
            !message.declined &&
            message.serverId ? (
              <div class="rate" role="group" aria-label="Was this answer helpful?">
                <button
                  type="button"
                  aria-pressed={message.rating === "up"}
                  aria-label="Helpful"
                  onClick={() => {
                    setMessages((current) =>
                      current.map((m) =>
                        m.id === message.id ? { ...m, rating: "up" } : m,
                      ),
                    );
                    post({
                      type: "feedback",
                      messageId: message.serverId ?? "",
                      rating: "up",
                    });
                  }}
                >
                  👍
                </button>
                <button
                  type="button"
                  aria-pressed={message.rating === "down"}
                  aria-label="Not helpful"
                  onClick={() => {
                    setMessages((current) =>
                      current.map((m) =>
                        m.id === message.id ? { ...m, rating: "down" } : m,
                      ),
                    );
                    post({
                      type: "feedback",
                      messageId: message.serverId ?? "",
                      rating: "down",
                    });
                  }}
                >
                  👎
                </button>
              </div>
            ) : null}
          </div>
        ))}

        {/* The rail lives INSIDE the log, not as a band between log and composer.
            As a sibling it reserved its height on every turn: after an answer the
            message a visitor wants to read was squeezed into what was left. Inside,
            it scrolls with the conversation, so the log always owns the full frame
            and the chips are simply the last thing in it. */}
        {suggestions.length ? (
          <div class="sugg" aria-label="Suggested questions">
            {suggestions.map((suggestion, index) => (
              <button key={index} type="button" onClick={() => send(suggestion)}>
                {suggestion}
              </button>
            ))}
          </div>
        ) : null}
      </div>

      <form
        hidden={showContactForm}
        onSubmit={(event) => {
          event.preventDefault();
          send(draft);
        }}
      >
        <textarea
          id="composer"
          ref={inputRef}
          value={draft}
          rows={1}
          maxLength={4000}
          placeholder="Type your message…"
          aria-label="Message"
          onInput={(event) => setDraft((event.target as HTMLTextAreaElement).value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              send(draft);
            }
          }}
        />
        <button
          type="submit"
          aria-label="Send message"
          disabled={thinking || !draft.trim()}
          style={`min-width:44px;min-height:44px;border-radius:10px;border:0;background:${brand};color:#fff;cursor:pointer`}
        >
          ➤
        </button>
      </form>

      <footer hidden={showContactForm}>
        {/* AI disclosure, present from day one (§19.5, SEC-14). */}
        <span>You are chatting with an AI assistant.</span>
        {poweredBy ? <span class="foot-link">Powered by EVA</span> : <span />}
      </footer>
      <div class="sugg" hidden={showContactForm} style="padding:0 14px 8px">
        <label style="font-size:12px;color:#6b7280;display:flex;gap:6px;align-items:center">
          <input
            type="checkbox"
            checked={remember}
            onChange={(event) => {
              const enabled = (event.target as HTMLInputElement).checked;
              setRemember(enabled);
              post({ type: "remember", enabled });
            }}
          />
          Remember this conversation on this device
        </label>
      </div>
    </div>
  );
}

const root = document.getElementById("root");
if (root) render(<App />, root);
