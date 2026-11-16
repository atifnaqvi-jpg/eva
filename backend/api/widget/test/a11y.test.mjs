/**
 * Accessibility and mobile contracts (WIDG-10, WIDG-11).
 *
 * No browser is available in this environment, so these assert the *contract* in the
 * code that ships rather than the rendered result: live regions exist, every control
 * has an accessible name, the focus trap and Escape handling are present, and no
 * touch target is defined below 44px. A real screen-reader pass is still owed and is
 * recorded as a gap in the M3 report -- claiming WCAG conformance from static checks
 * would be overstating what was verified.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = readFileSync(join(HERE, "..", "src", "app", "app.tsx"), "utf8");
const LOADER = readFileSync(join(HERE, "..", "src", "loader.ts"), "utf8");

test("the transcript is a polite live region (WIDG-11)", () => {
  assert.match(APP, /role="log"/);
  assert.match(APP, /aria-live="polite"/);
  // `additions text` so tokens appended to the streaming message are announced
  // without re-reading the whole message on every token.
  assert.match(APP, /aria-relevant="additions text"/);
});

test("the live region is labelled by the conversation title", () => {
  assert.match(APP, /aria-labelledby="title"/);
  assert.match(APP, /id="title"/);
});

test("every icon-only control carries an accessible name (WCAG 4.1.2)", () => {
  // The send, close and rating controls show glyphs, not words.
  for (const name of [
    'aria-label="Close chat"',
    'aria-label="Send message"',
    'aria-label="Helpful"',
    'aria-label="Not helpful"',
    'aria-label="Assistant is typing"',
    'aria-label="Message"',
    'aria-label="Sources"',
    'aria-label="Suggested questions"',
  ]) {
    assert.ok(APP.includes(name), `missing accessible name: ${name}`);
  }
});

test("the CSAT controls expose their pressed state", () => {
  assert.match(APP, /aria-pressed=\{message\.rating === "up"\}/);
  assert.match(APP, /aria-pressed=\{message\.rating === "down"\}/);
  assert.match(APP, /role="group" aria-label="Was this answer helpful\?"/);
});

test("Escape closes the panel and focus is trapped while open (WIDG-11)", () => {
  assert.match(APP, /event\.key === "Escape"/);
  assert.match(APP, /event\.key !== "Tab"/);
  // The trap must wrap in both directions, or Shift+Tab escapes on the first press.
  assert.match(APP, /event\.shiftKey/);
  assert.match(APP, /last\.focus\(\)/);
  assert.match(APP, /first\.focus\(\)/);
});

test("focus moves into the composer when the panel opens", () => {
  assert.match(APP, /inputRef\.current\?\.focus\(\)/);
});

test("the composer is a form, so Enter and assistive tech both submit", () => {
  assert.match(APP, /<form/);
  assert.match(APP, /onSubmit=/);
  assert.match(APP, /type="submit"/);
});

test("the textarea is labelled and length-capped", () => {
  assert.match(APP, /aria-label="Message"/);
  assert.match(APP, /maxLength=\{4000\}/);
});

test("no touch target is defined under 44px (WIDG-10)", () => {
  // Catches the regression that existed here: suggestion chips were 36px.
  const sizes = [...APP.matchAll(/min-(?:width|height):\s*(\d+)px/g)].map((m) =>
    Number(m[1]),
  );
  assert.ok(sizes.length > 0);
  for (const size of sizes) {
    assert.ok(size >= 44, `a touch target is ${size}px, below the 44px minimum`);
  }
});

test("a mobile sheet is defined for small viewports (WIDG-10)", () => {
  assert.match(APP, /@media \(max-width:480px\)/);
  assert.match(APP, /100dvh/);
  // Safe-area insets: without them the composer sits under the home indicator.
  assert.match(APP, /env\(safe-area-inset-bottom\)/);
  assert.match(LOADER, /max-width:480px/);
  assert.match(LOADER, /100dvh/);
});

test("the launcher is a real button with an expanded state", () => {
  assert.match(LOADER, /createElement\("button"\)/);
  assert.match(LOADER, /aria-expanded/);
  assert.match(LOADER, /aria-label", "Open chat"/);
});

test("reduced motion is respected", () => {
  assert.match(APP, /prefers-reduced-motion:reduce/);
});

test("CSAT posts the server message id, not the local React key", () => {
  // Regression: the vote posted `message.id`, which is a `b-<timestamp>` key used as
  // a React list identity. The endpoint expects a UUID, so every vote 404'd.
  assert.match(APP, /serverId\?: string/);
  assert.match(APP, /serverId: data\.messageId/);
  assert.ok(
    !/messageId: message\.id/.test(APP),
    "feedback must not send the local key as a message id",
  );
});

test("the AI disclosure is rendered unconditionally (SEC-14)", () => {
  // Not behind a flag and not conditional on configuration: §19.5 requires it from
  // day one, and a toggle that can hide it defeats the purpose.
  assert.match(APP, /You are chatting with an AI assistant\./);
});
