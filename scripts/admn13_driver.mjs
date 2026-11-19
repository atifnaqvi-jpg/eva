import { createRequire } from "node:module";

// Resolved from the widget package, like m5_driver.mjs: the walk must not depend on
// which node_modules happens to sit next to scripts/.
const { chromium } = createRequire(`${process.env.ADMN13_WIDGET}/package.json`)("playwright");

const BASE = process.env.ADMN13_BASE;
const EMAIL = process.env.ADMN13_EMAIL;
const PASSWORD = process.env.ADMN13_PASSWORD;
const WORKSPACE = process.env.ADMN13_WORKSPACE;
const Q_A = process.env.ADMN13_QA_QUESTION;
const A_A = process.env.ADMN13_QA_ANSWER;
const Q_B = process.env.ADMN13_QB_QUESTION;
const A_B = process.env.ADMN13_QB_ANSWER;

const out = { steps: [], errors: [] };
const say = (name, detail) => out.steps.push({ name, detail });

// Compare meaning, not code points: the preview prints the chunk as stored and the
// prose around it varies (m5_driver.mjs learned this with an en dash).
const norm = (value) =>
  String(value ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();

const DRAFT = "Draft (everything indexed)";
const LIVE = "Live (what visitors get)";

// Column text is read from the heading's own card, not by slicing `main`: both
// columns share one card, so a slice from the draft heading to the end of the page
// would let the live assertion pass on the draft's text.
const columnText = (page, prefix) =>
  page.evaluate((wanted) => {
    const heading = Array.from(document.querySelectorAll("h3")).find((h) =>
      (h.textContent || "").trim().startsWith(wanted),
    );
    return heading?.parentElement ? heading.parentElement.innerText : null;
  }, prefix);

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
const page = await context.newPage();
page.on("pageerror", (error) => out.errors.push(String(error)));

// A preview run is two POSTs (draft and live) issued in one Promise.all. Counting the
// responses is what separates consecutive questions: waiting for a heading would
// resolve instantly on the previous run's columns.
let previewResponses = 0;
page.on("response", (response) => {
  if (response.url().includes("/api/v1/knowledge/preview")) previewResponses += 1;
});

const askPreview = async (question) => {
  const before = previewResponses;
  const form = page
    .locator("form")
    .filter({ has: page.getByRole("button", { name: /^Test$/ }) });
  await form.getByLabel("Question").fill(question);
  await page.getByRole("button", { name: /^Test$/ }).click();
  const deadline = Date.now() + 120000;
  while (previewResponses === before && Date.now() < deadline) {
    await page.waitForTimeout(100);
  }
  await page
    .waitForFunction(
      () => !(document.querySelector("main")?.innerText || "").includes("Testing…"),
      undefined,
      { timeout: 120000 },
    )
    .catch(() => {});
  return {
    question,
    draft: await columnText(page, DRAFT),
    live: await columnText(page, LIVE),
    responses: previewResponses - before,
  };
};

const rowFor = (page, text) => page.locator("table tbody tr").filter({ hasText: text });

const servedState = (page) =>
  page.evaluate(() =>
    Array.from(document.querySelectorAll("table tbody tr")).map((row) => {
      const cells = Array.from(row.querySelectorAll("td"));
      return {
        title: (cells[0]?.innerText || "").split("\n")[0].trim(),
        served: (cells[cells.length - 1]?.innerText || "").trim(),
      };
    }),
  );

// Matched on the quoted title the screen prints (“<title>” is live.), not on the
// words "is live": the panel's own hint text says "what is live", and a looser match
// returned that paragraph instead of the publish notice.
const publishedNotice = (page) =>
  page.evaluate(() => {
    const para = Array.from(document.querySelectorAll("p")).find((p) =>
      /[\u201c"].+[\u201d"]\s+is live/.test(p.innerText || ""),
    );
    return para ? para.innerText.replace(/\s+/g, " ") : null;
  });

try {
  // 1. a fresh owner, so "nothing published yet" is a real starting state
  await page.goto(`${BASE}/login`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Create a workspace" }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByLabel("Workspace name").fill(WORKSPACE);
  await page.getByRole("button", { name: "Create workspace" }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 30000 });
  say("sign up", `landed on ${new URL(page.url()).pathname}`);

  await page.goto(`${BASE}/knowledge`, { waitUntil: "networkidle" });

  // 2. two documents, added through the screen. FAQ ingestion is synchronous, so a
  //    row appearing means the document is ready to publish -- no polling needed.
  const addFaq = async (question, answer) => {
    await page.getByRole("tab", { name: "Add a Q&A" }).click();
    const form = page
      .locator("form")
      .filter({ has: page.getByRole("button", { name: "Add Q&A" }) });
    await form.getByLabel("Question").fill(question);
    await form.getByLabel("Answer").fill(answer);
    await page.getByRole("button", { name: "Add Q&A" }).click();
    await rowFor(page, question).first().waitFor({ timeout: 60000 });
  };
  await addFaq(Q_A, A_A);
  await addFaq(Q_B, A_B);
  await page.reload({ waitUntil: "networkidle" });
  out.rows_before_publish = await servedState(page);
  say("documents", `${out.rows_before_publish.length} document row(s) after two Q&As`);

  // 3. nothing is published: the draft knows B, the live set is empty. This is the
  //    control for every "live finds it" assertion below -- without it, a preview
  //    that ignored its scope and searched everything would look correct.
  out.probe_draft_only = await askPreview(Q_B);
  say(
    "draft only",
    `draft finds B: ${norm(out.probe_draft_only.draft).includes(norm(A_B))}; ` +
      `live finds B: ${norm(out.probe_draft_only.live).includes(norm(A_B))}`,
  );

  // 4. publish A from the screen ("Make live") -- the only publish path a UI walk may
  //    use; an API call here would prove the endpoint, not the screen.
  await rowFor(page, Q_A).first().getByRole("button", { name: "Make live" }).click();
  // Waits on the notice itself, not on the words "is live": the panel's hint text
  // contains them before anything is published, so that wait would resolve at once.
  await page
    .waitForFunction(
      () =>
        Array.from(document.querySelectorAll("main p")).some((p) =>
          /[\u201c"].+[\u201d"]\s+is live/.test(p.innerText || ""),
        ),
      undefined,
      { timeout: 30000 },
    )
    .catch(() => {});
  out.publish_1 = { notice: await publishedNotice(page), rows: await servedState(page) };

  out.probe_a_only = await askPreview(Q_A);
  out.probe_b_after_a = await askPreview(Q_B);
  say(
    "A only",
    `live finds A: ${norm(out.probe_a_only.live).includes(norm(A_A))}; ` +
      `live finds B: ${norm(out.probe_b_after_a.live).includes(norm(A_B))}`,
  );

  // 5. publish B from the screen. Under RAG-11 this adds to the served set; under the
  //    old single-pointer model it retires A, which is the measured §8.22 defect.
  await rowFor(page, Q_B).first().getByRole("button", { name: "Make live" }).click();
  // Waits on the notice itself, not on the words "is live": the panel's hint text
  // contains them before anything is published, so that wait would resolve at once.
  await page
    .waitForFunction(
      () =>
        Array.from(document.querySelectorAll("main p")).some((p) =>
          /[\u201c"].+[\u201d"]\s+is live/.test(p.innerText || ""),
        ),
      undefined,
      { timeout: 30000 },
    )
    .catch(() => {});
  out.publish_2 = { notice: await publishedNotice(page), rows: await servedState(page) };

  // 6. the served column must come from the API after a reload, not from the click's
  //    local state -- the ADMN-05 lesson, restated for two documents.
  await page.reload({ waitUntil: "networkidle" });
  out.rows_after_publish_2 = await servedState(page);

  // 7. both documents answer from the live set: the row's whole point
  out.probe_a_after_b = await askPreview(Q_A);
  out.probe_b_after_b = await askPreview(Q_B);
  say(
    "both published",
    `live finds A: ${norm(out.probe_a_after_b.live).includes(norm(A_A))}; ` +
      `live finds B: ${norm(out.probe_b_after_b.live).includes(norm(A_B))}`,
  );

  // 8. rollback. The screen has no rollback control (ADMN-05's outstanding gap), so
  //    the pointer is moved through the endpoint publish shares -- from inside the
  //    same authenticated browser session -- and the *effect* is then read off the
  //    screen. Adding a rollback button would be a new screen control, i.e. a scope
  //    change, not this verification row.
  const ids = await page.evaluate(async (questionA) => {
    const bots = await (await fetch("/api/v1/bots")).json();
    const docs = await (await fetch("/api/v1/knowledge/documents")).json();
    const match = (docs || []).find((doc) =>
      String(doc.title).startsWith(questionA.slice(0, 20)),
    );
    return { botId: bots?.[0]?.id ?? null, versionId: match?.version_id ?? null };
  }, Q_A);
  out.rollback_ids = ids;
  out.rollback = await page.evaluate(async ({ botId, versionId }) => {
    if (!botId || !versionId) return { status: 0, body: "missing bot or version id" };
    const response = await fetch("/api/v1/knowledge/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bot_id: botId, version_id: versionId }),
    });
    return { status: response.status, body: (await response.text()).slice(0, 400) };
  }, ids);

  await page.reload({ waitUntil: "networkidle" });
  out.rows_after_rollback = await servedState(page);
  out.probe_a_after_rollback = await askPreview(Q_A);
  out.probe_b_after_rollback = await askPreview(Q_B);
  say(
    "rollback to A",
    `live finds A: ${norm(out.probe_a_after_rollback.live).includes(norm(A_A))}; ` +
      `live finds B: ${norm(out.probe_b_after_rollback.live).includes(norm(A_B))}`,
  );

  const countServed = (rows) => rows.filter((row) => row.served === "live").length;
  out.served_counts = {
    before_publish: countServed(out.rows_before_publish),
    after_publish_2: countServed(out.rows_after_publish_2),
    after_rollback: countServed(out.rows_after_rollback),
  };
} catch (error) {
  out.fatal = String(error).split("\n")[0];
} finally {
  await browser.close();
}

console.log(JSON.stringify(out));
