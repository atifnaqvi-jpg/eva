/**
 * ADMN-17 browser driver: the restore control on /knowledge, in real Chromium.
 *
 * Emits one JSON report on stdout. The verdict is `admn17_walk.py`'s `clauses()` — a
 * pure function over this report — so every clause can be shown to fail independently.
 * This file only *observes*; it does not decide.
 *
 * The walk is the defect's own shape: publish four documents one at a time through the
 * screen's "Make live" button, then press "Restore previous version" three times. The
 * old operation served the named document *alone*, so a restore from four documents
 * produced one; here each restore must serve the whole set it replaced (3, then 2,
 * then 1) and the fourth press must be unavailable.
 *
 * Run through `scripts/admn17_walk.py`, which supplies the credentials.
 */

import { createRequire } from "node:module";

const { chromium } = createRequire(
  `${process.env.EVA_WIDGET ?? "../backend/api/widget"}/package.json`,
)("playwright");

const BASE = process.env.EVA_BASE_URL ?? "http://localhost";
const EMAIL = process.env.EVA_EMAIL;
const PASSWORD = process.env.EVA_PASSWORD;
const WORKSPACE = process.env.EVA_WORKSPACE ?? `ADMN17 ${Date.now()}`;
const RUN = process.env.EVA_RUN ?? Date.now().toString(36);

const QUESTIONS = [
  `How long does standard shipping take ${RUN}`,
  `Do you ship to the EU ${RUN}`,
  `What is the refund window ${RUN}`,
  `Do you offer a warranty ${RUN}`,
];

const report = {
  control_before_publish: {},
  after_each_publish: [],
  restores: [],
  control_after_exhausted: {},
  fatal: null,
  steps: [],
};

/** What the API says the bot serves, and what the screen says it serves. */
async function servedState(page) {
  const served = await page.evaluate(async () => {
    const bots = await (await fetch("/api/v1/bots", { credentials: "same-origin" })).json();
    const bot = bots[0] ?? {};
    return {
      botId: bot.id ?? null,
      versions: bot.active_knowledge_version_ids ?? [],
      previous_set_id: bot.previous_knowledge_set_id ?? null,
      previous_versions: bot.previous_knowledge_version_ids ?? [],
    };
  });
  const liveRows = await page
    .locator("table tbody tr", { has: page.locator("span", { hasText: /^live$/ }) })
    .count()
    .catch(() => -1);
  return { ...served, live_rows: liveRows };
}

function restoreControl(page) {
  return page.locator("button", { hasText: "Restore previous version" }).first();
}

async function controlState(page) {
  const button = restoreControl(page);
  const exists = (await button.count()) > 0;
  if (!exists) return { rendered: false };
  const section = page
    .locator("section", { has: page.locator("h2", { hasText: "Documents" }) })
    .first();
  return {
    rendered: true,
    disabled: await button.isDisabled(),
    hint: (await section.innerText()).split("\n").find((line) =>
      /document|restore/i.test(line),
    ) ?? "",
  };
}

async function main() {
  const browser = await chromium.launch();
  const context = await browser.newContext();
  const page = await context.newPage();

  try {
    // A fresh tenant through the page's own registration path: the state this row must
    // work in is "an owner who has just signed up", not a pre-seeded account.
    await page.goto(`${BASE}/login`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Create a workspace" }).click();
    await page.fill('input[type="email"]', EMAIL);
    await page.fill('input[type="password"]', PASSWORD);
    await page.fill('input[type="text"]', WORKSPACE);
    await Promise.all([
      page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 20000 }),
      page.click('button[type="submit"]'),
    ]);
    report.steps.push({ name: "register", detail: "created a workspace through /login" });

    await page.goto(`${BASE}/knowledge`, { waitUntil: "networkidle" });
    await page.waitForTimeout(1200);
    report.control_before_publish = await controlState(page);
    report.steps.push({
      name: "control before publish",
      detail: JSON.stringify(report.control_before_publish),
    });

    for (const [index, question] of QUESTIONS.entries()) {
      // Added through the form, published through "Make live": an API call here would
      // prove the endpoint the in-process suite already covers, not the screen.
      await page.getByRole("tab", { name: "Add a Q&A" }).click();
      await page.fill('input[placeholder], input[type="text"]', question);
      await page.fill("textarea", `Answer number ${index + 1} for ${RUN}.`);
      await Promise.all([
        page.waitForResponse(
          (response) =>
            response.url().includes("/api/v1/knowledge/faqs") &&
            response.request().method() === "POST",
          { timeout: 30000 },
        ),
        page.click('button[type="submit"]'),
      ]);
      await page.waitForTimeout(1500);

      const row = page.locator("table tbody tr", { hasText: question }).first();
      await Promise.all([
        page.waitForResponse(
          (response) =>
            response.url().includes("/api/v1/knowledge/publish") &&
            response.request().method() === "POST",
          { timeout: 30000 },
        ),
        row.locator("button", { hasText: "Make live" }).click(),
      ]);
      await page.waitForTimeout(1200);

      const state = await servedState(page);
      report.after_each_publish.push({ index, ...state });
      report.steps.push({
        name: `publish ${index + 1}`,
        detail: `api=${state.versions.length} live_rows=${state.live_rows}`,
      });
    }

    // --------------------------------------------------------------- the restores
    for (let step = 0; step < 4; step += 1) {
      const control = await controlState(page);
      if (control.disabled) {
        report.control_after_exhausted = control;
        report.steps.push({ name: `restore ${step + 1}`, detail: "control disabled" });
        break;
      }
      await restoreControl(page).click();
      await page.waitForTimeout(1800);
      // Re-read after a reload so the count comes from the API and the rendered table,
      // never from the click's own optimistic state.
      await page.reload({ waitUntil: "networkidle" });
      await page.waitForTimeout(1200);
      const state = await servedState(page);
      const notice = await page
        .locator("p", { hasText: /Restored the previous version/i })
        .first()
        .innerText()
        .catch(() => "");
      report.restores.push({ step, notice, ...state });
      report.steps.push({
        name: `restore ${step + 1}`,
        detail: `api=${state.versions.length} live_rows=${state.live_rows} notice=${notice}`,
      });
    }
  } catch (error) {
    report.fatal = error.message;
  } finally {
    await browser.close();
  }

  process.stdout.write(JSON.stringify(report) + "\n");
}

main();
