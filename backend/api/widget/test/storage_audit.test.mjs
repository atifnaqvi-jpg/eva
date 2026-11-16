/**
 * SEC-08: a storage audit of the *shipped* loader, in a stub DOM.
 *
 * SEC-08 asks for a browser storage audit proving nothing is stored before the chat
 * is opened. No browser is available here (see the M3 report), so this runs the
 * built `dist/loader.js` -- the exact bytes served to host pages, not the source --
 * against a DOM stub that records every storage write, every `fetch`, and every
 * element created.
 *
 * This is weaker than a real browser in one way that matters and is stated plainly:
 * it cannot catch something a browser engine does that the stub does not model. It is
 * stronger than a grep in the way that matters most: the code under test is the
 * artefact, so a build-config change that inlined a storage write would still fail.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const BUNDLE = join(HERE, "..", "dist", "loader.js");

/**
 * A DOM stub just large enough for the loader's start-up path.
 *
 * Deliberately not jsdom: adding a multi-megabyte DOM implementation to prove that
 * six lines of code touch nothing would be a heavier dependency than the widget. The
 * stub records operations, which is what the audit needs.
 */
function makeDom({ botId = "abc123", withCurrentScript = true } = {}) {
  const writes = [];
  const fetches = [];
  const created = [];

  const makeStorage = (name) => ({
    getItem: () => null,
    setItem: (key, value) => writes.push({ name, key, value }),
    removeItem: (key) => writes.push({ name, key, value: null }),
  });

  const makeElement = (tag) => {
    const el = {
      tagName: tag.toUpperCase(),
      style: { cssText: "" },
      className: "",
      textContent: "",
      children: [],
      attributes: {},
      setAttribute(name, value) {
        this.attributes[name] = value;
      },
      getAttribute(name) {
        return name in this.attributes ? this.attributes[name] : null;
      },
      append(...kids) {
        this.children.push(...kids);
      },
      appendChild(kid) {
        this.children.push(kid);
        return kid;
      },
      remove() {},
      focus() {},
      addEventListener() {},
      removeEventListener() {},
      querySelectorAll: () => [],
    };
    created.push(el);
    return el;
  };

  const head = makeElement("head");
  const body = makeElement("body");

  const script = makeElement("script");
  script.attributes["data-bot"] = botId;
  script.attributes["data-api"] = "https://api.example.test";
  script.src = "https://api.example.test/widget/loader.js";

  const document = {
    currentScript: withCurrentScript ? script : null,
    readyState: "complete",
    head,
    body,
    createElement: makeElement,
    createTextNode: (text) => ({ textContent: text }),
    addEventListener() {},
    removeEventListener() {},
    querySelectorAll: () => [],
    getElementById: () => null,
  };

  const window = {
    document,
    location: {
      href: "https://shop.example.test/products/42",
      origin: "https://shop.example.test",
    },
    localStorage: makeStorage("localStorage"),
    sessionStorage: makeStorage("sessionStorage"),
    addEventListener() {},
    removeEventListener() {},
    setTimeout: (fn) => 0,
    clearTimeout() {},
    fetch: (url, options) => {
      fetches.push({ url: String(url), options });
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ token: "t", expires_in: 1800, bot: { name: "n" } }),
        text: async () => "",
        headers: { get: () => null },
        body: null,
      });
    },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    innerWidth: 1280,
    innerHeight: 800,
  };
  window.window = window;
  window.self = window;
  window.globalThis = window;

  return { window, document, writes, fetches, created, script };
}

function runLoader(dom) {
  const code = readFileSync(BUNDLE, "utf8");
  const context = vm.createContext({
    ...dom.window,
    console,
    URL,
    Promise,
    JSON,
    Math,
    Object,
    Array,
    String,
    Number,
    Error,
    TextEncoder,
  });
  context.window = context;
  context.document = dom.document;
  context.document.currentScript = dom.script;
  vm.runInContext(code, context, { filename: "loader.js" });
  return context;
}

test("the shipped bundle writes nothing to storage at load (SEC-08)", () => {
  const dom = makeDom();
  runLoader(dom);
  assert.deepEqual(
    dom.writes,
    [],
    "a GDPR-relevant write happened before the visitor interacted",
  );
});

test("the shipped bundle makes no network request at load (SEC-08)", () => {
  const dom = makeDom();
  runLoader(dom);
  assert.deepEqual(dom.fetches, [], "the loader called out before being opened");
});

test("the shipped bundle creates only the launcher, not the iframe (WIDG-04)", () => {
  const dom = makeDom();
  runLoader(dom);
  const tags = dom.created.map((el) => el.tagName);
  assert.ok(tags.includes("BUTTON"), "the launcher should exist");
  assert.ok(!tags.includes("IFRAME"), "the frame must wait for a click");
});

test("the launcher carries an accessible name and an expanded state", () => {
  const dom = makeDom();
  runLoader(dom);
  const button = dom.created.find((el) => el.tagName === "BUTTON");
  assert.equal(button.getAttribute("aria-label"), "Open chat");
  assert.equal(button.getAttribute("aria-expanded"), "false");
});

test("a page with no data-bot attribute gets nothing at all", () => {
  const dom = makeDom({ botId: null });
  // `readConfig` returns null without the attribute, so the loader must exit before
  // creating a launcher, writing storage, or fetching.
  const domNoScript = makeDom();
  domNoScript.document.currentScript = null;
  domNoScript.script.attributes["data-bot"] = "";

  runLoader(domNoScript);
  assert.deepEqual(domNoScript.writes, []);
  assert.deepEqual(domNoScript.fetches, []);
  assert.ok(!domNoScript.created.some((el) => el.tagName === "BUTTON"));
});

test("consent is required before a durable identifier exists (WIDG-13)", () => {
  const dom = makeDom();
  runLoader(dom);
  // Load alone must not create the durable id; only the opt-in path may.
  assert.ok(!dom.writes.some((w) => w.name === "localStorage"));
});

test("the loader's own size stays inside the §12.2 budget", () => {
  const bytes = readFileSync(BUNDLE).byteLength;
  assert.ok(bytes < 10_240, `loader.js is ${bytes} B, over the 10 KB budget`);
});
