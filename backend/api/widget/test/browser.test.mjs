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

  // The served loader, not the source, is what the browser ran. `loader.ts` has no
  // `keydown` string unless the host-document Escape binding is present, so this
  // separates "the key was routed somewhere unexpected" from "the bundle is stale".
  const served = await page.request.get(`${BASE_URL}/widget/loader.js`);
  assert.equal(served.status(), 200, "the widget loader was not served");
  const loaderSource = await served.text();
  assert.ok(
    loaderSource.includes("keydown"),
    "the served loader binds no keydown handler, so Escape cannot close the panel " +
      "from the host document; the bundle is stale or the fix is not in this build",
  );

  const launcher = page.getByRole("button", { name: "Open chat" });
  await launcher.click();
  const frame = page.frameLocator("iframe");
  await frame.getByLabel("Message", { exact: true }).waitFor({ timeout: 15_000 });

  // Focus is the whole point of this case, so it is set rather than inherited. Escape is
  // handled inside the frame *and* on the host document; which one receives the keydown
  // depends on where focus sits, and that differs by engine and between runs. Testing
  // only the inherited position is what let a host-document gap through: chromium and
  // firefox leave focus on the launcher, so the panel could not be closed by keyboard at
  // all, while the same code passed locally where focus had landed in the frame.
  const launcherNow = page.getByRole("button", { name: "Close chat" });
  await launcherNow.focus(); // host document, the position a real click leaves behind
  // Record the key at the document's capture phase, before any handler can act, so a
  // failure says whether the key arrived at all or arrived and was not acted on.
  await page.evaluate(() => {
    window.__hostKeys = [];
    window.__expandedLog = [];
    document.addEventListener(
      "keydown",
      (event) => window.__hostKeys.push(event.key),
      { capture: true, once: false },
    );
    // The button's `aria-expanded` is the panel's visible state, written by both
    // `openChat` and `closeChat`. Logging its transitions separates the two ways this
    // can fail: the handler never ran (no transition) or it ran and the panel was
    // reopened afterwards (false, then true again).
    const button = document.querySelector("button[aria-expanded]");
    if (button) {
      new MutationObserver((records) => {
        for (const record of records) {
          window.__expandedLog.push(
            `${button.getAttribute("aria-expanded")}@${Math.round(performance.now())}`,
          );
        }
      }).observe(button, { attributes: true, attributeFilter: ["aria-expanded"] });
    }
  });
  const focusBefore = await page.evaluate(
    () => `${document.activeElement?.tagName}:${document.activeElement?.getAttribute("aria-label") ?? ""}`,
  );
  await page.keyboard.press("Escape");
  await page.waitForTimeout(400);
  const hostState = await page.evaluate(() => {
    const frames = Array.from(document.querySelectorAll("iframe"));
    return {
      frames: frames.length,
      hidden: frames.map((f) => f.hidden),
      expanded: document.activeElement?.getAttribute("aria-expanded") ?? null,
    };
  });
  const visibleFromHost = await page
    .locator("iframe")
    .first()
    .isVisible()
    .catch(() => false);
  const keysSeen = await page.evaluate(() => window.__hostKeys ?? []);
  const expandedLog = await page.evaluate(() => window.__expandedLog ?? []);
  const states = expandedLog.map((entry) => String(entry).split("@")[0]);
  const firstClose = states.indexOf("false");
  assert.ok(firstClose !== -1, `Escape never closed the panel (expanded=${JSON.stringify(expandedLog)})`);
  assert.equal(
    states.slice(firstClose).includes("true"),
    false,
    `the panel closed and reopened within the settle window (WIDG-17): ` +
      `expanded=${JSON.stringify(expandedLog)}`,
  );
  assert.equal(
    visibleFromHost,
    false,
    `Escape from the host page left the panel open (focus=${focusBefore}, ` +
      `frames=${JSON.stringify(hostState)}, hostKeys=${JSON.stringify(keysSeen)}, ` +
      `expanded=${JSON.stringify(expandedLog)})`,
  );

  // And the in-frame path must still work after a reopen.
  await launcher.click();
  await frame.getByLabel("Message", { exact: true }).waitFor({ timeout: 15_000 });
  await frame.getByLabel("Message", { exact: true }).click();
  await page.evaluate(() => {
    window.__expandedLog = [];
  });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(400);
  const visibleFromFrame = await page
    .locator("iframe")
    .first()
    .isVisible()
    .catch(() => false);
  const frameLog = await page.evaluate(() => window.__expandedLog ?? []);
  const frameStates = frameLog.map((entry) => String(entry).split("@")[0]);
  const frameClose = frameStates.indexOf("false");
  assert.ok(frameClose !== -1, `Escape inside the frame never closed the panel (expanded=${JSON.stringify(frameLog)})`);
  assert.equal(
    frameStates.slice(frameClose).includes("true"),
    false,
    `the panel closed from inside the frame and reopened within the settle window ` +
      `(WIDG-17): expanded=${JSON.stringify(frameLog)}`,
  );
  assert.equal(visibleFromFrame, false, "Escape inside the frame left the panel open");

  await context.close();
});

matrixTest("one Escape press posts exactly one close (WIDG-17)", async () => {
  // The panel-scoped and document-scoped listeners both fired for a single keydown,
  // because a keydown that starts in the panel bubbles to the document. Two `close`
  // posts for one press is the other half of the WIDG-11 race: whichever handler
  // arrives second can land after the panel has been reopened. Counted at the frame's
  // own `postMessage`, so a source-level "there is one listener" reading cannot pass
  // this — the assertion is the number of messages that actually left the frame.
  const { context, page } = await newPageRecordingRequests({ width: 1280, height: 800 });
  await openHost(page);
  await page.getByRole("button", { name: "Open chat" }).click();

  const frame = page.frameLocator("iframe");
  const composer = frame.getByLabel("Message", { exact: true });
  await composer.waitFor({ timeout: 15_000 });

  const appFrame = page.frames().find((f) => f.url().includes("/widget/app"));
  assert.ok(appFrame, "the widget app frame was not found");
  await appFrame.evaluate(() => {
    window.__closePosts = 0;
    const original = window.parent.postMessage.bind(window.parent);
    window.parent.postMessage = (message, target) => {
      if (message && message.type === "close") window.__closePosts += 1;
      original(message, target);
    };
  });

  await composer.click(); // focus inside the panel, so the keydown bubbles through it
  await page.keyboard.press("Escape");
  await page.waitForTimeout(400);
  const posts = await appFrame.evaluate(() => window.__closePosts ?? 0);
  assert.equal(
    posts,
    1,
    `one Escape press posted close ${posts} time(s); two handlers own the same key (WIDG-17)`,
  );

  await context.close();
});

matrixTest("a late frame ready cannot reopen a panel the visitor closed (WIDG-17)", async () => {
  // The CI flake in full: Escape lands while the app bundle is still loading, and the
  // frame's `ready` post arrives afterwards. Holding `app.js` makes that ordering
  // deterministic instead of a race, so this fails every run against the defect rather
  // than at the ~50% rate the flake fired at.
  const { context, page } = await newPageRecordingRequests({ width: 1280, height: 800 });

  let release;
  const held = new Promise((resolve) => (release = resolve));
  await page.route("**/widget/app.js", async (route) => {
    await held;
    await route.continue();
  });

  await openHost(page);
  await page.getByRole("button", { name: "Open chat" }).click();
  await page.waitForSelector("iframe");

  await page.evaluate(() => {
    window.__expandedLog = [];
    const button = document.querySelector("button[aria-expanded]");
    new MutationObserver(() => {
      window.__expandedLog.push(
        `${button.getAttribute("aria-expanded")}@${Math.round(performance.now())}`,
      );
    }).observe(button, { attributes: true, attributeFilter: ["aria-expanded"] });
  });

  const launcherNow = page.getByRole("button", { name: "Close chat" });
  await launcherNow.focus();
  await page.keyboard.press("Escape");
  await page.waitForTimeout(100);
  assert.equal(
    await page.locator("iframe").first().evaluate((f) => f.hidden),
    true,
    "Escape did not close the panel before the app loaded",
  );

  release();
  await page.waitForTimeout(1500);
  const expandedLog = await page.evaluate(() => window.__expandedLog ?? []);
  assert.equal(
    await page.locator("iframe").first().evaluate((f) => f.hidden),
    true,
    `the frame's ready post reopened the panel after the visitor closed it (WIDG-17): ` +
      `expanded=${JSON.stringify(expandedLog)}`,
  );

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
