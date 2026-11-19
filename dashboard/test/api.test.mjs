/**
 * The dashboard's API client, exercised against a stubbed `fetch`.
 *
 * `fetch` is the seam: the client's whole job is turning a URL and a response into a
 * typed value or an `ApiFailure`, so substituting the transport tests the real parsing
 * and error-mapping code without a server. Node's built-in runner and type-stripping
 * mean this needs no new dependencies.
 *
 * The cases are the ones that were actually wrong or would be silently wrong: a 204
 * with no body (logout), an error body in the API's shape, a non-JSON error page from a
 * proxy, and the naive-timestamp query the list screen must build.
 */

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

const {
  ApiFailure,
  listConversations,
  logout,
  exportUrl,
  getConversation,
  analyticsKnowledgeGaps,
  rollbackKnowledge,
} =
  await import("../lib/api.ts");

const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
});

function stub(response) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return response;
  };
  return calls;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("logout tolerates a 204 with no body", async () => {
  stub(new Response(null, { status: 204 }));
  // The bug this pins: `.json()` on an empty 204 throws, which surfaced as a failed
  // logout even though the API had already cleared the cookie.
  await logout();
});

test("a 401 becomes an ApiFailure carrying the API's code", async () => {
  stub(
    json(
      { detail: { error: { code: "not_authenticated", message: "Sign in first" } } },
      401,
    ),
  );
  await assert.rejects(
    () => listConversations({}),
    (failure) => {
      assert.ok(failure instanceof ApiFailure);
      assert.equal(failure.status, 401);
      assert.equal(failure.code, "not_authenticated");
      assert.equal(failure.message, "Sign in first");
      return true;
    },
  );
});

test("a non-JSON error body keeps the status rather than throwing a parse error", async () => {
  stub(new Response("<html>502 Bad Gateway</html>", { status: 502 }));
  await assert.rejects(
    () => listConversations({}),
    (failure) => {
      assert.ok(failure instanceof ApiFailure);
      assert.equal(failure.status, 502);
      assert.equal(failure.code, "http_502");
      return true;
    },
  );
});

test("requests are same-origin and uncached", async () => {
  const calls = stub(json({ items: [], total: 0, limit: 50, offset: 0 }));
  await listConversations({});
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/v1/conversations");
  assert.equal(calls[0].init.credentials, "same-origin");
  assert.equal(calls[0].init.cache, "no-store");
});

test("filters are omitted when unset and encoded when set", async () => {
  const calls = stub(json({ items: [], total: 0, limit: 50, offset: 0 }));
  await listConversations({
    status: "handed_over",
    from: "2026-11-01T00:00:00Z",
    limit: 25,
    offset: 25,
  });
  const url = new URL(calls[0].url, "https://example.test");
  assert.equal(url.pathname, "/api/v1/conversations");
  assert.equal(url.searchParams.get("status"), "handed_over");
  assert.equal(url.searchParams.get("from"), "2026-11-01T00:00:00Z");
  assert.equal(url.searchParams.get("limit"), "25");
  assert.equal(url.searchParams.get("offset"), "25");
  // A blank status must not become `status=`; the API would treat it as a filter.
  assert.equal(url.searchParams.has("to"), false);
});

test("conversation ids are path-encoded", async () => {
  // Ids are UUIDs today, so this is defensive — but a route that interpolates an
  // unencoded path segment is the shape that becomes traversal if ids ever change.
  assert.equal(
    exportUrl("../admin", "json"),
    "/api/v1/conversations/..%2Fadmin/export?format=json",
  );

  const calls = stub(json({ summary: {}, transcript: "", turns: [] }));
  await getConversation("a b");
  assert.equal(calls[0].url, "/api/v1/conversations/a%20b");
});

test("a 404 from a foreign conversation surfaces as a not-found the screen can name", async () => {
  stub(
    json(
      { detail: { error: { code: "conversation_not_found", message: "Not found" } } },
      404,
    ),
  );
  await assert.rejects(
    () => getConversation("00000000-0000-4000-8000-000000000000"),
    (failure) => {
      assert.ok(failure instanceof ApiFailure);
      assert.equal(failure.status, 404);
      assert.equal(failure.code, "conversation_not_found");
      return true;
    },
  );
});

test("the knowledge-gap report is fetched from its own route, not the summary", async () => {
  // OPS-05: the gap list waits on the embedding service, so it must be a separate
  // request. Folding it into /analytics/summary would make every analytics page load
  // wait on the embedder and would let an embedder outage blank the summary.
  const calls = stub(
    json({
      window: { from: "2026-11-01T00:00:00Z", to: "2026-11-19T00:00:00Z", days: 30, timezone: "UTC" },
      declined_turns: 2,
      distinct_questions: 2,
      gap_count: 1,
      clustered: true,
      truncated: false,
      gaps: [
        {
          question: "do you ship to canada",
          occurrences: 2,
          distinct_phrasings: 2,
          first_seen: "2026-11-18T00:00:00Z",
          last_seen: "2026-11-18T00:01:00Z",
          score: 1.9,
          phrasings: [
            { question: "do you ship to canada", occurrences: 1, last_seen: "2026-11-18T00:00:00Z" },
            { question: "can you deliver to canadian addresses", occurrences: 1, last_seen: "2026-11-18T00:01:00Z" },
          ],
        },
      ],
      note: null,
      definitions: {},
    }),
  );

  const report = await analyticsKnowledgeGaps(7);
  assert.equal(calls[0].url, "/api/v1/analytics/knowledge-gaps?days=7");
  assert.equal(report.clustered, true);
  assert.equal(report.gaps[0].distinct_phrasings, 2);
});

test("rollback posts only the bot id: the API refuses a version id", async () => {
  // ADMN-17 / HALT-10: rollback restores the previous *set*. A client that still sent
  // `version_id` would be answered 422 `version_id_not_supported` — the old
  // serve-this-document-alone semantics, which silently unpublished the rest.
  const calls = stub(json({ active_knowledge_set_id: "s1", active_knowledge_version_ids: ["v1"] }));
  await rollbackKnowledge("bot 1");
  assert.equal(calls[0].url, "/api/v1/knowledge/rollback");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].init.body), { bot_id: "bot 1" });
});

test("a refused rollback surfaces the API's own code and message", async () => {
  // `no_previous_set` and `nothing_published` send the owner to different places, so the
  // code has to survive the client rather than collapsing into a generic failure.
  stub(
    json(
      {
        detail: {
          error: {
            code: "no_previous_set",
            message: "this bot's published set has no earlier set recorded",
          },
        },
      },
      409,
    ),
  );
  await assert.rejects(
    () => rollbackKnowledge("b1"),
    (failure) => {
      assert.ok(failure instanceof ApiFailure);
      assert.equal(failure.status, 409);
      assert.equal(failure.code, "no_previous_set");
      assert.match(failure.message, /no earlier set recorded/);
      return true;
    },
  );
});
