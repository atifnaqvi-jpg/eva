import fs from "node:fs";
import { createRequire } from "node:module";

// Resolved from the widget package rather than this file's directory, so the
// gate does not depend on which node_modules happens to sit next to scripts/.
const { chromium } = createRequire(`${process.env.M5_WIDGET}/package.json`)("playwright");

const BASE = process.env.M5_BASE;
const PHASE = process.env.M5_PHASE;
const STATE = process.env.M5_STATE;
const QUESTION = process.env.M5_QUESTION;
const ANSWER = process.env.M5_ANSWER;

const out = { steps: [], errors: [] };
const say = (name, detail) => out.steps.push({ name, detail });

// The assistant writes prose around the retrieved chunk, so punctuation is not
// stable between runs: the same answer came back with a hyphen once and an en
// dash the next time. Compare meaning, not code points, or the gate reports a
// defect every time the model chooses a different dash.
const norm = (value) =>
  String(value ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
const shot = (page, name) =>
  page.screenshot({ path: `${process.env.M5_SHOTS}/${name}.png`, fullPage: true }).catch(() => {});

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1280, height: 900 },
  acceptDownloads: true,
  storageState: PHASE === "two" ? STATE : undefined,
});
const page = await context.newPage();
page.on("pageerror", (error) => out.errors.push(String(error)));

try {
  if (PHASE === "one") {
    // 1. sign up, as somebody with no account and no instructions
    await page.goto(`${BASE}/login`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Create a workspace" }).click();
    await page.getByLabel("Email").fill(process.env.M5_EMAIL);
    await page.getByLabel("Password").fill(process.env.M5_PASSWORD);
    await page.getByLabel("Workspace name").fill(process.env.M5_WORKSPACE);
    await page.getByRole("button", { name: "Create workspace" }).click();
    await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 30000 });
    say("sign up", `landed on ${new URL(page.url()).pathname}`);
    await shot(page, "1-signup");

    // 2. create the bot, and take the snippet from the screen itself
    await page.goto(`${BASE}/bot`, { waitUntil: "networkidle" });
    await page.getByLabel("Bot name").fill(process.env.M5_BOT);
    await page.getByRole("button", { name: /^Save$/ }).first().click();
    await page.waitForTimeout(2500);
    const html = await page.content();
    out.snippet = (/data-bot="([^"]+)"/.exec(html) || [])[1] || null;
    out.snippetIsOnScreen = (await page.locator("pre").count()) > 0;
    say("create bot", `public id ${out.snippet}, snippet on screen: ${out.snippetIsOnScreen}`);
    await shot(page, "2-bot");

    // 3. add knowledge as a Q&A, then publish it
    await page.goto(`${BASE}/knowledge`, { waitUntil: "networkidle" });
    await page.getByRole("tab", { name: "Add a Q&A" }).click();
    await page.getByLabel("Question").first().fill(QUESTION);
    await page.getByLabel("Answer").fill(ANSWER);
    await page.getByRole("button", { name: "Add Q&A" }).click();
    await page.waitForTimeout(6000);
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForTimeout(2000);
    out.knowledgeRows = await page.locator("table tbody tr").count();
    await page.getByRole("button", { name: "Make live" }).first().click();
    await page.waitForTimeout(4000);
    out.publishSaidLive = /is live/i.test(await page.locator("main").innerText());
    say("add knowledge", `${out.knowledgeRows} document row(s); publish reported live: ${out.publishSaidLive}`);
    await shot(page, "3-knowledge");

    // 4. test it from the screen, which is how an owner checks their own bot
    await page.getByLabel("Question").last().fill(QUESTION);
    await page.getByRole("button", { name: /^Test$/ }).click();
    await page.waitForTimeout(6000);
    const testText = (await page.locator("main").innerText()).replace(/\n+/g, " ");
    const liveColumn = testText.slice(testText.indexOf("Live (what visitors get)"));
    out.testFoundAnswer = norm(liveColumn).includes(norm(ANSWER));
    say("test", `the live column answers from published content: ${out.testFoundAnswer}`);
    await shot(page, "4-test");

    // 5. customise, and prove the values come back from the API after a reload
    await page.goto(`${BASE}/bot`, { waitUntil: "networkidle" });
    await page.getByLabel("Welcome message").fill(process.env.M5_GREETING);
    await page.getByLabel("Colour").fill("#0b7285");
    await page.getByRole("button", { name: /^Save$/ }).first().click();
    await page.waitForTimeout(2500);
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForTimeout(2000);
    out.customisationPersisted =
      (await page.getByLabel("Welcome message").inputValue()) === process.env.M5_GREETING;
    say("customise", `greeting survives a reload: ${out.customisationPersisted}`);
    await shot(page, "5-customise");

    await context.storageState({ path: STATE });
  } else {
    // 6. a visitor uses the embedded widget on the product's own host page
    await page.goto(`${BASE}/demo/host?bot=${process.env.M5_BOT_ID}`, { waitUntil: "networkidle" });
    await page.waitForTimeout(2500);
    const launcher = page
      .locator("[data-eva-launcher], #eva-launcher, button[aria-label*='chat' i]")
      .first();
    out.launcherVisible = (await launcher.count()) > 0 && (await launcher.isVisible());
    if (out.launcherVisible) {
      await launcher.click();
      await page.waitForTimeout(2500);
      const frame = page.frameLocator("iframe").first();
      const input = frame.locator("textarea, input[type='text']").first();
      await input.fill(QUESTION);
      await input.press("Enter");
      await page.waitForTimeout(10000);
      const body = await frame.locator("body").innerText();
      out.widgetAnswered = norm(body).includes(norm(ANSWER));
      out.widgetText = body.replace(/\s+/g, " ").slice(0, 300);
    }
    say("embed", `launcher visible: ${out.launcherVisible}; answered: ${out.widgetAnswered}`);
    await shot(page, "6-widget");

    // 7. the transcript export the criterion names
    await page.goto(`${BASE}/conversations`, { waitUntil: "networkidle" });
    await page.waitForTimeout(2500);
    const listText = await page.locator("main").innerText();
    out.conversationsListed = !/No conversations/i.test(listText);
    const first = page.locator("main a").filter({ hasText: /./ }).first();
    if (await first.count()) {
      await first.click();
      await page.waitForTimeout(3000);
    }
    await shot(page, "7-transcript");

    const openExport = async (label) => {
      // The export is an <a href> to the API, not a button: a real download the
      // browser handles, which is what makes it the data-export path as well.
      const link = page.getByRole("link", { name: label }).first();
      if (!(await link.count())) return null;
      const waiter = page.waitForEvent("download", { timeout: 30000 }).catch(() => null);
      await link.click();
      const file = await waiter;
      return file ? fs.readFileSync(await file.path(), "utf8") : null;
    };

    const txt = await openExport(/Export \.txt/);
    if (txt) {
      out.exportHasQuestion = norm(txt).includes(norm(QUESTION));
      out.exportHasAnswer = norm(txt).includes(norm(ANSWER));
      out.exportPath = "download";
    }
    await page.waitForTimeout(500);
    const jsonText = await openExport(/Export \.json/);
    if (jsonText) {
      out.jsonExportParses = true;
      try {
        const parsed = JSON.parse(jsonText);
        out.jsonExportMessages = (parsed.messages || []).length;
        out.jsonExportHasQuestion = norm(JSON.stringify(parsed)).includes(norm(QUESTION));
      } catch (error) {
        out.jsonExportParses = false;
        out.jsonExportError = String(error).split("\n")[0];
      }
    }
    say(
      "export",
      `txt has question: ${out.exportHasQuestion}; answer: ${out.exportHasAnswer}; ` +
        `json parses: ${out.jsonExportParses} (${out.jsonExportMessages} messages)`,
    );

    // 8. analytics, read off the screen for reconciliation against the database
    await page.goto(`${BASE}/analytics`, { waitUntil: "networkidle" });
    await page.waitForTimeout(3000);
    await page.getByRole("button", { name: /7 days/i }).first().click().catch(() => {});
    await page.waitForTimeout(2500);
    out.analyticsText = (await page.locator("main").innerText()).replace(/\s+/g, " ").slice(0, 900);
    await shot(page, "8-analytics");
  }
} catch (error) {
  out.fatal = String(error).split("\n")[0];
} finally {
  await browser.close();
}

console.log(JSON.stringify(out));
