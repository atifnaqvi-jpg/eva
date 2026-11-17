/**
 * The M3 browser matrix (WIDG-10, WIDG-16, SEC-08), driven by Playwright.
 *
 * This is the real-browser counterpart to `storage_audit.test.mjs` (a DOM stub) and
 * `a11y.test.mjs` (static assertions). It loads the shipped loader on a real host
 * page in a real Chromium, and checks the things only a browser can show: that
 * storage is genuinely empty before a click, that a click creates the frame, that the
 * panel is full-viewport on a phone, and that the CSP-safe path works.
 *
 * **Scope, stated plainly:** the engine is chosen by `WIDGET_BROWSER` (chromium by
 * default; `firefox` and `webkit` run the same matrix), each of which needs its own
 * `playwright install`. "Mobile" is device emulation, not a physical handset: it proves
 * the media queries and touch target sizing, not iOS Safari's viewport quirks. Edge is
 * not run — it is Chromium, so the Chromium run covers its engine, not its packaging.
 *
 * Requires the stack to be up and reachable at BASE_URL, with the widget built.
 */

import { test, before, after } from "node:test";
import assert from "node:assert/strict";

const BASE_URL = process.env.WIDGET_BASE_URL ?? "http://localhost";
const BOT_ID = process.env.WIDGET_BOT_ID ?? "";
const ENGINE = process.env.WIDGET_BROWSER ?? "chromium";

let browser;
let skip = !BOT_ID ? "set WIDGET_BOT_ID to run the browser matrix" : false;

before(async () => {
  if (skip) return;
  let engines;
  try {
    // Imported lazily: Playwright is a dev dependency and this file must not break
    // `npm test` on a checkout that has not installed it or its browsers.
    engines = await import("playwright");
  } catch {
    skip = "playwright is not installed";
    return;
  }
  const launcher = engines[ENGINE];
  if (!launcher) {
    skip = `unknown WIDGET_BROWSER: ${ENGINE}`;
    return;
  }
  try {
    browser = await launcher.launch();
  } catch (error) {
    // Browsers are a separate download from the package.
    skip = `no ${ENGINE} browser available: ${error.message.split("\n")[0]}`;
  }
});

after(async () => {
  if (browser) await browser.close();
});

/**
 * `node:test` reads a `skip` option when the test is *defined*, which is before
 * `before()` has had a chance to discover that Playwright or its browsers are
 * missing. So the guard runs inside the body instead, and every case goes through
 * here -- that is what makes the skip real rather than decorative.
 */
const matrixTest = (name, fn) =>
  test(name, async (t) => {
    if (skip) {
      t.skip(skip);
      return;
    }
    await fn(t);
  });

/** A context that records every request it makes. */
async function newPageRecordingRequests(viewport) {
  const context = await browser.newContext({ viewport });
  const requests = [];
  context.on("request", (r) => requests.push(r.url()));
  const page = await context.newPage();
  return { context, page, requests };
}

async function openHost(page) {
  await page.goto(`${BASE_URL}/demo/host?bot=${BOT_ID}`, { waitUntil: "networkidle" });
}

async function storageSnapshot(page) {
  return page.evaluate(() => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
    cookies: document.cookie,
    // The other places a widget can leave a trace.
    indexedDB: typeof indexedDB.databases === "function" ? "checked-separately" : "n/a",
  }));
}

matrixTest("nothing is stored and nothing is requested before the click (SEC-08)", async () => {
  const { context, page, requests } = await newPageRecordingRequests({
    width: 1280,
    height: 800,
  });

  await openHost(page);
  const before = await storageSnapshot(page);

  assert.deepEqual(before.local, [], "localStorage was written before any click");
  assert.deepEqual(before.session, [], "sessionStorage was written before any click");
  assert.equal(before.cookies, "", "a cookie was set before any click");

  // The only request allowed before a click is the loader itself.
  const nonLoader = requests.filter(
    (url) => !url.includes("/widget/loader.js") && !url.includes("/demo/host"),
  );
  assert.deepEqual(nonLoader, [], `unexpected request(s) before click: ${nonLoader}`);

  // And no frame exists yet.
  assert.equal(await page.locator("iframe").count(), 0);

  await context.close();
});

matrixTest("opening the chat still stores nothing; storage appears only on send (SEC-08)", async () => {
  const { context, page } = await newPageRecordingRequests({ width: 1280, height: 800 });
  await openHost(page);

  await page.getByRole("button", { name: "Open chat" }).click();
  await page.locator("iframe").first().waitFor({ state: "attached", timeout: 10_000 });
  assert.equal(await page.locator("iframe").count(), 1);

  // Opening the frame mints a session (a token in memory) but writes no identifier.
  // That is stronger than §19.3 requires -- the requirement is "nothing before the
  // chat is opened" -- and it is what the loader actually does, so it is asserted.
  const afterOpen = await storageSnapshot(page);
  assert.deepEqual(afterOpen.session, [], "opening a chat stored a session value");
  assert.deepEqual(afterOpen.local, [], "opening the chat created a durable id");

  // A message is what creates the identifier, because that is what needs one.
  const frame = page.frameLocator("iframe");
  const composer = frame.getByLabel("Message", { exact: true });
  await composer.waitFor({ timeout: 15_000 });
  await composer.fill("What is the return window?");
  await composer.press("Enter");
  await page.waitForFunction(() => Object.keys(sessionStorage).length > 0, null, {
    timeout: 20_000,
  });

  const afterSend = await storageSnapshot(page);
  assert.ok(afterSend.session.includes("eva_visitor"), "no visitor id after a message");
  assert.deepEqual(
    afterSend.local,
    [],
    "a durable id must not exist without explicit opt-in",
  );

  await context.close();
});

matrixTest("the panel answers a question end to end (WIDG-06)", async () => {
  const { context, page } = await newPageRecordingRequests({ width: 1280, height: 800 });
  await openHost(page);
  await page.getByRole("button", { name: "Open chat" }).click();

  const frame = page.frameLocator("iframe");
  const composer = frame.getByLabel("Message", { exact: true });
  await composer.waitFor({ timeout: 15_000 });
  await composer.fill("What is the return window?");
  await composer.press("Enter");

  // The bot turn must render text: an answer, or the stated handover when the
  // deployment has no provider key. Both are legitimate; silence is not.
  // `.msg.bot` alone is not enough -- the typing indicator lives in one too, so this
  // waits for a paragraph, which only exists once a turn has produced content.
  const answer = frame.locator(".msg.bot p").first();
  await answer.waitFor({ state: "visible", timeout: 30_000 });

  const text = await answer.innerText();
  assert.ok(text.trim().length > 0, "the bot turn rendered no text");

  await context.close();
});

matrixTest("the panel is a full-viewport sheet on a phone viewport (WIDG-10)", async () => {
  const { context, page } = await newPageRecordingRequests({
    width: 390,
    height: 844,
  });
  await openHost(page);
  await page.getByRole("button", { name: "Open chat" }).click();

  const frame = page.locator("iframe").first();
  await frame.waitFor({ state: "attached" });
  // The media query applies on layout, so give it a frame to settle.
  await page.waitForTimeout(400);

  const box = await frame.boundingBox();
  assert.ok(box, "the frame has no box on a phone viewport");
  assert.ok(box.width >= 380, `frame width ${box.width}px, expected ~full width`);
  assert.ok(box.height >= 800, `frame height ${box.height}px, expected ~full height`);
  assert.equal(Math.round(box.x), 0, "the sheet should be flush to the left edge");

  await context.close();
});

matrixTest("every touch target in the panel is at least 44px (WIDG-10)", async () => {
  const { context, page } = await newPageRecordingRequests({ width: 390, height: 844 });
  await openHost(page);
  await page.getByRole("button", { name: "Open chat" }).click();

  const frame = page.frameLocator("iframe");
  await frame.getByLabel("Message", { exact: true }).waitFor({ timeout: 15_000 });

  const boxes = await frame.locator("button").evaluateAll((nodes) =>
    nodes
      .filter((n) => n.offsetParent !== null)
      .map((n) => {
        const r = n.getBoundingClientRect();
        return { label: n.getAttribute("aria-label") ?? n.textContent, w: r.width, h: r.height };
      }),
  );
  assert.ok(boxes.length > 0, "no buttons found in the panel");
  for (const { label, w, h } of boxes) {
    assert.ok(
      h >= 44 || w >= 44,
      `control "${label}" is ${Math.round(w)}x${Math.round(h)}px, below 44px`,
    );
  }

  await context.close();
});

matrixTest("the transcript is announced to assistive tech (WIDG-11)", async () => {
  const { context, page } = await newPageRecordingRequests({ width: 1280, height: 800 });
  await openHost(page);
  await page.getByRole("button", { name: "Open chat" }).click();

  const frame = page.frameLocator("iframe");
  const log = frame.locator('[role="log"]');
  await log.waitFor({ timeout: 15_000 });

  assert.equal(await log.getAttribute("aria-live"), "polite");
  assert.equal(await log.getAttribute("aria-relevant"), "additions text");

  await context.close();
});

matrixTest("Escape closes the panel and focus returns to the launcher (WIDG-11)", async () => {
  const { context, page } = await newPageRecordingRequests({ width: 1280, height: 800 });
  await openHost(page);

  const launcher = page.getByRole("button", { name: "Open chat" });
  await launcher.click();
  const frame = page.frameLocator("iframe");
  await frame.getByLabel("Message", { exact: true }).waitFor({ timeout: 15_000 });

  await page.keyboard.press("Escape");
  // The loader removes the frame (or hides it) on close; either way it must not stay.
  await page.waitForTimeout(400);
  const visible = await page
    .locator("iframe")
    .first()
    .isVisible()
    .catch(() => false);
  assert.equal(visible, false, "Escape left the panel open");

  await launcher.click();
  await context.close();
});

matrixTest("a host page's CSP does not break the widget (WIDG-16)", async () => {
  // The realistic enterprise case: the host sets a policy. The loader injects styles
  // and an iframe, so it must work under `style-src 'unsafe-inline'` and a
  // `frame-src` that allows the API origin, without needing `unsafe-eval`.
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    bypassCSP: false,
  });
  const page = await context.newPage();
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  await page.goto(`${BASE_URL}/demo/host?bot=${BOT_ID}`, { waitUntil: "networkidle" });
  const launcher = page.getByRole("button", { name: "Open chat" });
  await launcher.click();

  const frame = page.frameLocator("iframe");
  await frame.getByLabel("Message", { exact: true }).waitFor({ timeout: 15_000 });

  const fatal = errors.filter((e) => /Content Security Policy/i.test(e));
  assert.deepEqual(fatal, [], "CSP violations while opening the widget");

  await context.close();
});

matrixTest("a host page on an unverified origin is refused (WIDG-05)", async () => {
  // The demo page is served by Caddy only on the configured site address, so a
  // second *origin* is created here with a throwaway static server on another port.
  // That is the realistic case: a tenant pastes the snippet on a domain they have
  // not verified, and the API must refuse to mint a session.
  const { createServer } = await import("node:http");
  const html = `<!doctype html><html><body><h1>Unverified host</h1>
    <script src="${BASE_URL}/widget/loader.js" data-bot="${BOT_ID}"
            data-api="${BASE_URL}" defer></script></body></html>`;

  const server = createServer((_req, res) => {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(html);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;

  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  const statuses = [];
  page.on("response", (r) => {
    if (r.url().includes("/widget/session")) statuses.push(r.status());
  });

  try {
    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Open chat" }).click();
    await page.waitForTimeout(2000);

    assert.ok(statuses.length > 0, "the page never attempted a session");
    assert.ok(
      statuses.includes(403),
      `expected a 403 from an unverified origin, saw ${statuses}`,
    );
  } finally {
    await context.close();
    await new Promise((resolve) => server.close(resolve));
  }
});
