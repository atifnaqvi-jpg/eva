/**
 * ADMN-16 browser driver: the retention editor and the domain controls in real Chromium.
 *
 * Emits one JSON report on stdout. The verdict is computed by `admn16_walk.py`'s
 * `clauses()` — a pure function over this report — so each clause can be shown to fail
 * independently. This file only *observes*; it does not decide.
 *
 * Why a browser: the two ways this row can be wrong are both invisible to a server
 * test. A form can render `min`/`max` the API refuses, and a control can look like it
 * saved while the request never left. So every claim is read back from the server after
 * the click, not from the click's own state.
 *
 * Run through `scripts/admn16_walk.py`, which supplies the credentials.
 */

import { createRequire } from "node:module";

// Resolved from the widget's own node_modules (the repo's only playwright install), so
// this script stays outside a package that declares a dependency it does not use.
const { chromium } = createRequire(
  `${process.env.EVA_WIDGET ?? "../backend/api/widget"}/package.json`,
)("playwright");

const BASE = process.env.EVA_BASE_URL ?? "http://localhost";
const EMAIL = process.env.EVA_EMAIL;
const PASSWORD = process.env.EVA_PASSWORD;
const DOMAIN = process.env.EVA_TEST_DOMAIN ?? `shop.${Date.now()}.admn16.test`;
const WORKSPACE = process.env.EVA_WORKSPACE ?? `ADMN16 ${Date.now()}`;

const report = {
  retention_form: {},
  retention_after_valid_save: {},
  retention_after_rejected_save: {},
  domain_after_add: null,
  widget_status_after_add: null,
  after_remove: {},
  fatal: null,
  steps: [],
};

async function serverRetentionDays(page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/v1/settings/tenant", {
      credentials: "same-origin",
    });
    const body = await response.json();
    return body?.retention?.conversation_days ?? null;
  });
}

async function serverDomains(page) {
  return page.evaluate(async () => {
    const bots = await (
      await fetch("/api/v1/bots", { credentials: "same-origin" })
    ).json();
    if (!bots.length) return { botId: null, domains: [] };
    const domains = await (
      await fetch(`/api/v1/bots/${bots[0].id}/domains`, { credentials: "same-origin" })
    ).json();
    return { botId: bots[0].id, publicId: bots[0].public_id, domains };
  });
}

async function main() {
  const browser = await chromium.launch();
  const context = await browser.newContext();
  const page = await context.newPage();

  try {
    // The page's own registration path, not an injected cookie or a pre-seeded account:
    // getting a session is part of what a browser gate proves, and a fresh tenant is
    // the state this row must work in.
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

    await page.goto(`${BASE}/settings`, { waitUntil: "networkidle" });

    const retention = page
      .locator("section", { has: page.locator("h2", { hasText: "Data retention" }) })
      .first();
    const domains = page
      .locator("section", { has: page.locator("h2", { hasText: "Domains" }) })
      .first();

    // ---------------------------------------------------------- retention: form
    const dayInput = retention.locator('input[type="number"]');
    await dayInput.waitFor({ timeout: 15000 });
    report.retention_form = {
      min: await dayInput.getAttribute("min"),
      max: await dayInput.getAttribute("max"),
      value: await dayInput.inputValue(),
    };
    report.steps.push({
      name: "retention form",
      detail: `min=${report.retention_form.min} max=${report.retention_form.max} value=${report.retention_form.value}`,
    });

    // ------------------------------------------------- retention: a valid save
    await dayInput.fill("14");
    await retention.locator('button[type="submit"]').click();
    await page.waitForTimeout(1500);
    const savedDays = await serverRetentionDays(page);
    const cardText = await retention.innerText();
    report.retention_after_valid_save = {
      api_days: savedDays,
      card_mentions: cardText.includes("14 days"),
    };
    report.steps.push({
      name: "retention save (14)",
      detail: `api=${savedDays} card says 14 days=${cardText.includes("14 days")}`,
    });

    // ------------------------------------------- retention: out-of-range refused
    // `min`/`max` stop a spinner, not a typed value, so this types the boundary the way
    // a person would and then checks both halves: the message is shown, and the stored
    // value did not move.
    await dayInput.fill("5000");
    await retention.locator('button[type="submit"]').click();
    await page.waitForTimeout(1500);
    const rejectedDays = await serverRetentionDays(page);
    // Either the screen's own durable message or the server's refusal is acceptable —
    // both name the bound. What is not acceptable is silence, which is what native
    // validation left behind before the form owned the check.
    const refusalVisible = await page
      .locator("p", { hasText: /between 1 and 730|at most 730/i })
      .first()
      .isVisible()
      .catch(() => false);
    report.retention_after_rejected_save = {
      api_days: rejectedDays,
      refusal_visible: refusalVisible,
      // The bound is in the server's message; a generic "invalid" would leave the owner
      // guessing which end they crossed.
      message_names_bound: refusalVisible,
    };
    report.steps.push({
      name: "retention save (5000)",
      detail: `api=${rejectedDays} refusal shown=${refusalVisible}`,
    });

    // ------------------------------------------------------- domains: add only
    const domainField = domains.locator("input").first();
    await domainField.fill(DOMAIN);
    await domains.locator('button[type="submit"]').click();
    await page.waitForTimeout(1800);

    const afterAdd = await serverDomains(page);
    const added = afterAdd.domains.find((entry) => entry.domain === DOMAIN);
    const domainsText = await domains.innerText();
    report.domain_after_add = added
      ? {
          verified: added.verified,
          record_name: added.record_name,
          record_value: added.record_value,
          // The record has to be on screen: an owner who has to open devtools to find
          // the value to publish has no usable control.
          record_on_screen:
            domainsText.includes(added.record_name ?? "\u0000") &&
            domainsText.includes(added.record_value ?? "\u0000"),
        }
      : null;
    report.steps.push({
      name: "domain add",
      detail: added
        ? `verified=${added.verified} record on screen=${report.domain_after_add.record_on_screen}`
        : "domain not present after add",
    });

    // ------------------------------- domains: the widget must still refuse it
    if (afterAdd.botId) {
      report.widget_status_after_add = await page.evaluate(
        async ({ publicId, origin }) => {
          const response = await fetch("/api/v1/widget/session", {
            method: "POST",
            headers: { "Content-Type": "application/json", Origin: origin },
            body: JSON.stringify({ bot_public_id: publicId }),
          });
          return response.status;
        },
        { publicId: afterAdd.publicId, origin: `https://${DOMAIN}` },
      );
      report.steps.push({
        name: "widget after add",
        detail: `POST /widget/session -> ${report.widget_status_after_add}`,
      });
    }

    // ---------------------------------------------------------- domains: remove
    await domains
      .locator("li", { hasText: DOMAIN })
      .first()
      .locator("button", { hasText: "Remove" })
      .click();
    await page.waitForTimeout(1500);
    const afterRemove = await serverDomains(page);
    const removeText = await domains.innerText();
    report.after_remove = {
      still_in_api: afterRemove.domains.some((entry) => entry.domain === DOMAIN),
      still_on_screen: removeText.includes(DOMAIN),
    };
    report.steps.push({
      name: "domain remove",
      detail: `in api=${report.after_remove.still_in_api} on screen=${report.after_remove.still_on_screen}`,
    });

    // Restore the default so a re-run starts from the same state.
    await dayInput.fill("90");
    await retention.locator('button[type="submit"]').click();
    await page.waitForTimeout(1000);
  } catch (error) {
    report.fatal = error.message;
  } finally {
    await browser.close();
  }

  process.stdout.write(JSON.stringify(report) + "\n");
}

main();
