/**
 * Session-scoped visitor identity (WIDG-12 / SEC-08, ARCHITECTURE §12.2 R37/R41).
 *
 * Three rules, in order of importance:
 *
 * 1. **Nothing is written before the visitor opens the chat.** Not a cookie, not a
 *    storage key, not a random id. The id is created on first use.
 * 2. **Session-scoped storage only.** `sessionStorage` is per-tab and cleared when
 *    the tab closes, which is exactly the "tab close = new conversation" rule. A
 *    cookie would outlive the tab; `localStorage` would outlive the browser.
 * 3. **A durable identifier requires explicit opt-in** (R42). Only then does the
 *    id move to `localStorage`, and the visitor can revoke it.
 *
 * `sessionStorage` can throw: Safari in private mode and any browser with storage
 * blocked raise on *access*, not just on write. The fallback is an in-memory id for
 * the lifetime of the page, which degrades to "new conversation per reload" rather
 * than to a broken widget.
 */

const SESSION_KEY = "eva_visitor";
const DURABLE_KEY = "eva_visitor_durable";

function safeGet(store: Storage | undefined, key: string): string | null {
  try {
    return store ? store.getItem(key) : null;
  } catch {
    return null;
  }
}

function safeSet(store: Storage | undefined, key: string, value: string): void {
  try {
    store?.setItem(key, value);
  } catch {
    /* storage blocked: the in-memory fallback covers it */
  }
}

function safeRemove(store: Storage | undefined, key: string): void {
  try {
    store?.removeItem(key);
  } catch {
    /* nothing to do */
  }
}

export interface VisitorIdentity {
  /** The id to send with `POST /widget/conversations`. Creates one if absent. */
  id(): string;
  /** Whether a durable (opted-in) id exists. */
  remembered(): boolean;
  /**
   * Move the id to durable storage, or destroy the durable copy.
   *
   * Turning it off also deletes the durable id rather than merely ignoring it:
   * "stop remembering me" that leaves the identifier on disk is not what the
   * visitor asked for.
   */
  remember(enabled: boolean): void;
}

export function createIdentity(
  win: Window,
  makeId: () => string,
): VisitorIdentity {
  let memory: string | null = null;

  return {
    id(): string {
      if (memory) return memory;

      const durable = safeGet(win.localStorage, DURABLE_KEY);
      if (durable) {
        memory = durable;
        return durable;
      }

      const session = safeGet(win.sessionStorage, SESSION_KEY);
      if (session) {
        memory = session;
        return session;
      }

      memory = makeId();
      safeSet(win.sessionStorage, SESSION_KEY, memory);
      return memory;
    },

    remembered(): boolean {
      return safeGet(win.localStorage, DURABLE_KEY) !== null;
    },

    remember(enabled: boolean): void {
      const current = this.id();
      if (enabled) {
        safeSet(win.localStorage, DURABLE_KEY, current);
        safeSet(win.sessionStorage, SESSION_KEY, current);
      } else {
        safeRemove(win.localStorage, DURABLE_KEY);
        safeSet(win.sessionStorage, SESSION_KEY, current);
      }
    },
  };
}
