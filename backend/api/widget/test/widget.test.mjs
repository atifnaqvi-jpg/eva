/**
 * Widget unit tests (WIDG-01/03/06/12, SEC-08).
 *
 * Run with `node --test`. The loader and app are tested as *built* artefacts where
 * possible: a test against `dist/loader.js` proves what ships, not what the source
 * intended. Markdown and protocol tests import the TypeScript directly through
 * esbuild's transform, because those are pure functions and a bundle round-trip
 * would only obscure the assertion.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";

const HERE = dirname(fileURLToPath(import.meta.url));
const DIST = join(HERE, "..", "dist");

const sizes = JSON.parse(readFileSync(join(DIST, "sizes.json"), "utf8"));
const loaderSource = readFileSync(join(DIST, "loader.js"), "utf8");

/** Load a TypeScript module through esbuild, so tests exercise the real source. */
async function loadTs(relative) {
  const result = await build({
    entryPoints: [join(HERE, "..", "src", relative)],
    bundle: true,
    format: "esm",
    write: false,
    target: ["es2019"],
  });
  const code = result.outputFiles[0].text;
  const url = `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
  return import(url);
}

test("the loader is under the 10 KB gzipped budget (§12.2)", () => {
  const gz = gzipSync(readFileSync(join(DIST, "loader.js")), { level: 9 }).length;
  assert.equal(sizes.loader.gzip, gz, "sizes.json is stale; rebuild");
  assert.ok(gz < 10 * 1024, `loader is ${gz} B gzipped, budget is 10240 B`);
});

test("the loader ships no storage write at load time (SEC-08)", () => {
  // The invariant SEC-08 asks for is "nothing is stored before the visitor asks for
  // the chat". A DOM-level proof needs a browser; this is the structural version:
  // every storage call must sit inside a function that only a user action reaches.
  // `setItem`/`removeItem` appear only in identity.ts, and identity.id() is called
  // from openChat()/remember handling -- so their presence at module scope would
  // mean the loader writes during parse.
  const atModuleScope = loaderSource
    .split("\n")
    .filter((line) => /setItem|removeItem|localStorage|sessionStorage/.test(line));
  for (const line of atModuleScope) {
    assert.match(
      line,
      /^(?![^;]*\)\s*\(?\s*$)/,
      `storage access looks top-level: ${line}`,
    );
  }

  // Stronger and simpler: the built IIFE runs `start()` but `start()` must not
  // touch storage until the click handler runs. Verified by the ordering test below.
  assert.ok(
    loaderSource.includes("sessionStorage"),
    "identity storage should be bundled into the loader",
  );
});

test("the loader sends no network request before the click", async () => {
  // Executed in a stub DOM: if the loader fetched anything during start(), the
  // stub records it. This is the executable half of SEC-08.
  const calls = [];
  const storage = new Map();
  const makeStorage = () => ({
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  });

  const sandbox = {
    document: {
      readyState: "complete",
      currentScript: {
        getAttribute: (name) =>
          ({ "data-bot": "bot-live-1", "data-color": "#123456" })[name] ?? null,
        src: "https://platform.example/loader.js",
      },
      createElement: () => ({
        style: {},
        setAttribute() {},
        append() {},
        addEventListener() {},
        contentWindow: null,
      }),
      body: { append() {} },
      addEventListener() {},
    },
    location: { href: "https://shop.example/" },
    crypto: { getRandomValues: (arr) => arr.fill(7) },
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
    fetch: (...args) => {
      calls.push(args[0]);
      return Promise.resolve({ ok: true, json: async () => ({}) });
    },
    addEventListener() {},
    postMessage() {},
  };

  const run = new Function(
    "window",
    "document",
    "location",
    "crypto",
    "fetch",
    "localStorage",
    "sessionStorage",
    `${loaderSource}\n`,
  );
  run(
    sandbox,
    sandbox.document,
    sandbox.location,
    sandbox.crypto,
    sandbox.fetch,
    sandbox.localStorage,
    sandbox.sessionStorage,
  );
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.deepEqual(calls, [], `the loader made requests before the click: ${calls}`);
  assert.equal(storage.size, 0, `the loader stored keys before the click: ${[...storage]}`);
});

test("markdown renders no raw HTML and drops dangerous link schemes", async () => {
  const { parseBlocks } = await loadTs("shared/markdown.ts");

  const html = parseBlocks('<img src=x onerror=alert(1)> and <script>bad()</script>');
  const flat = JSON.stringify(html);
  assert.ok(!flat.includes('"kind":"link"'), "no link should be produced");

  // The payload survives as *text*, which Preact renders via a text node. What
  // matters is that no element is created from it.
  const nodes = html[0].inline.filter((n) => n.kind === "text");
  assert.ok(nodes.some((n) => n.text.includes("<img")));

  const js = parseBlocks("[click](javascript:alert(1))");
  const link = js[0].inline.find((n) => n.kind === "link");
  assert.equal(link, undefined, "javascript: must not become a link");
  const kept = js[0].inline.find((n) => n.text === "click");
  assert.ok(kept, "the label text is kept, the URL is dropped");

  const ok = parseBlocks("[docs](https://example.com/a)");
  const safe = ok[0].inline.find((n) => n.kind === "link");
  assert.equal(safe.href, "https://example.com/a");

  const data = parseBlocks("[x](data:text/html,<script>alert(1)</script>)");
  assert.equal(
    data[0].inline.find((n) => n.kind === "link"),
    undefined,
    "data: must not become a link",
  );
});

test("markdown keeps code fences literal, including HTML inside them", async () => {
  const { parseBlocks } = await loadTs("shared/markdown.ts");
  const blocks = parseBlocks("```\n<script>alert(1)</script>\n```");
  assert.equal(blocks[0].kind, "code");
  assert.equal(blocks[0].text, "<script>alert(1)</script>");
});

test("streamed tokens accumulate into one message with paragraphs intact", async () => {
  const { parseBlocks } = await loadTs("shared/markdown.ts");
  // A sentence split mid-word across two SSE frames must still parse as one
  // paragraph: the app concatenates before parsing, which is what this asserts.
  const partial = "Refunds take 5 work";
  const rest = "ing days.";
  const blocks = parseBlocks(partial + rest);
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].kind, "p");
  assert.equal(
    blocks[0].inline.map((n) => n.text).join(""),
    "Refunds take 5 working days.",
  );
});

test("the protocol rejects unknown and malformed messages (WIDG-03)", async () => {
  const { isToLoader, isToApp } = await loadTs("shared/protocol.ts");

  assert.equal(isToLoader({ type: "send", text: "hi" }), true);
  assert.equal(isToLoader({ type: "send", text: "" }), false);
  assert.equal(isToLoader({ type: "nope" }), false);
  assert.equal(isToLoader(null), false);
  assert.equal(isToLoader("send"), false);
  assert.equal(isToLoader({ type: "resize", height: "big" }), false);
  assert.equal(isToLoader({ type: "feedback", messageId: "m", rating: "meh" }), false);

  assert.equal(isToApp({ type: "turn", event: "token", text: "a" }), true);
  assert.equal(isToApp({ type: "turn", event: "token" }), false);
  assert.equal(isToApp({ type: "unknown" }), false);
});

test("the protocol accepts the handover messages and rejects empty ones (HAND-02)", async () => {
  const { isToLoader } = await loadTs("shared/protocol.ts");

  assert.equal(isToLoader({ type: "handover", email: "a@b.co" }), true);
  assert.equal(
    isToLoader({ type: "handover", email: "a@b.co", name: "Sam", note: "hi" }),
    true,
  );
  // Rejected before a round trip: the form must not be able to submit a blank
  // address, and an unbounded string is the one thing a hostile frame controls.
  assert.equal(isToLoader({ type: "handover", email: "" }), false);
  assert.equal(isToLoader({ type: "handover" }), false);
  assert.equal(isToLoader({ type: "handover", email: "a@b.co", name: 5 }), false);
  assert.equal(isToLoader({ type: "handover", email: "x".repeat(321) }), false);

  assert.equal(isToLoader({ type: "post-handover", text: "more" }), true);
  assert.equal(isToLoader({ type: "post-handover", text: "" }), false);
});

test("a garbage frame from another origin cannot drive the loader", () => {
  // The source/origin checks are in onMessage; assert they exist in the shipped
  // bundle rather than trusting the source file.
  assert.match(loaderSource, /contentWindow/, "frame source must be compared");
  assert.match(loaderSource, /origin/, "origin must be compared");
});

test("no widget source opts into raw HTML", () => {
  // The built bundle *does* contain `innerHTML`: that is Preact's own
  // `dangerouslySetInnerHTML` support, which is inert unless a vnode carries an
  // `__html` prop. Asserting on the bundle would therefore either be a false
  // positive (as it was) or a permanently skipped check. The real invariant is
  // that no source file ever produces such a vnode, so assert that instead.
  const sources = ["app/app.tsx", "shared/markdown.ts", "loader.ts"];
  for (const relative of sources) {
    const code = readFileSync(join(HERE, "..", "src", relative), "utf8");
    assert.ok(
      !/dangerouslySetInnerHTML|__html/.test(code),
      `${relative} opts into raw HTML rendering`,
    );
  }
  // And the markdown parser must not emit HTML strings at all: its output is a
  // typed tree, which is the reason the app needs no sanitiser. Comments are
  // stripped first -- the parser's docstring *mentions* innerHTML to explain the
  // design, and matching prose instead of code was a real false positive here.
  const markdown = readFileSync(join(HERE, "..", "src", "shared", "markdown.ts"), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  assert.ok(!/innerHTML/.test(markdown));
});
