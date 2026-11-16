/**
 * Visitor identity and session scope (WIDG-04, WIDG-12, WIDG-13, SEC-08).
 *
 * The rules being pinned are privacy rules, so they are stated as behaviour rather
 * than as implementation: nothing is written before `id()` is called, a reload
 * resumes within the tab, a durable id exists only after explicit opt-in, and
 * revoking it actually removes the stored value.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const HERE = dirname(fileURLToPath(import.meta.url));

async function loadIdentity() {
  const result = await build({
    entryPoints: [join(HERE, "..", "src", "shared", "identity.ts")],
    bundle: true,
    format: "esm",
    write: false,
    target: ["es2019"],
  });
  const code = result.outputFiles[0].text;
  return import(
    `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`
  );
}

/** A Window stub with independent localStorage and sessionStorage. */
function fakeWindow({ throws = false } = {}) {
  const make = () => {
    const data = new Map();
    return {
      data,
      getItem: (k) => (data.has(k) ? data.get(k) : null),
      setItem: (k, v) => {
        if (throws) throw new Error("storage disabled");
        data.set(k, String(v));
      },
      removeItem: (k) => {
        if (throws) throw new Error("storage disabled");
        data.delete(k);
      },
    };
  };
  return { localStorage: make(), sessionStorage: make() };
}

let counter = 0;
const makeId = () => `id-${++counter}`;

test("nothing is stored before the identity is used (SEC-08/WIDG-04)", async () => {
  const { createIdentity } = await loadIdentity();
  const win = fakeWindow();
  createIdentity(win, makeId);

  assert.equal(win.localStorage.data.size, 0);
  assert.equal(win.sessionStorage.data.size, 0);
});

test("the first id() writes to session storage, not local (WIDG-12)", async () => {
  const { createIdentity } = await loadIdentity();
  const win = fakeWindow();
  const identity = createIdentity(win, makeId);

  const id = identity.id();
  assert.ok(id);
  assert.equal(win.sessionStorage.data.size, 1);
  assert.equal(win.localStorage.data.size, 0, "local storage implies durability");
});

test("the id is stable across calls and across a reload in the same tab", async () => {
  const { createIdentity } = await loadIdentity();
  const win = fakeWindow();
  const first = createIdentity(win, makeId).id();
  assert.equal(createIdentity(win, makeId).id(), first);

  // A reload is a new page with the same sessionStorage.
  const afterReload = createIdentity(win, makeId).id();
  assert.equal(afterReload, first);
});

test("a new tab starts a new conversation (R37: tab close = new conversation)", async () => {
  const { createIdentity } = await loadIdentity();
  const tabOne = createIdentity(fakeWindow(), makeId).id();
  const tabTwo = createIdentity(fakeWindow(), makeId).id();
  assert.notEqual(tabOne, tabTwo);
});

test("opt-in moves the id to durable storage and keeps it there", async () => {
  const { createIdentity } = await loadIdentity();
  const win = fakeWindow();
  const identity = createIdentity(win, makeId);
  const id = identity.id();

  assert.equal(identity.remembered(), false);
  identity.remember(true);
  assert.equal(identity.remembered(), true);
  assert.equal(win.localStorage.getItem("eva_visitor_durable"), id);

  // A new tab still sees the durable id: that is the point of opting in.
  assert.equal(createIdentity(win, makeId).id(), id);
});

test("revoking opt-in deletes the durable id rather than ignoring it (WIDG-13)", async () => {
  const { createIdentity } = await loadIdentity();
  const win = fakeWindow();
  const identity = createIdentity(win, makeId);
  identity.id();
  identity.remember(true);
  identity.remember(false);

  assert.equal(identity.remembered(), false);
  assert.equal(
    win.localStorage.getItem("eva_visitor_durable"),
    null,
    "an identifier left on disk after revocation is not revoked",
  );
  // The tab keeps working: only durability is withdrawn.
  assert.ok(identity.id());
});

test("the conversation id is session-scoped and written only when set", async () => {
  const { createIdentity } = await loadIdentity();
  const win = fakeWindow();
  const identity = createIdentity(win, makeId);

  identity.id();
  assert.equal(identity.conversation(), null, "no conversation before one exists");

  identity.setConversation("conv-1");
  assert.equal(identity.conversation(), "conv-1");
  // A different tab does not resume this conversation.
  assert.equal(createIdentity(fakeWindow(), makeId).conversation(), null);
});

test("storage that throws degrades to an in-memory id, not a crash", async () => {
  // Safari private mode and any browser with storage blocked raise on access.
  const { createIdentity } = await loadIdentity();
  const win = fakeWindow({ throws: true });
  const identity = createIdentity(win, makeId);

  const id = identity.id();
  assert.ok(id);
  assert.equal(identity.id(), id, "the in-memory id is stable for the page's life");
  // Remembering cannot persist, but it must not throw either.
  identity.remember(true);
  identity.remember(false);
});
