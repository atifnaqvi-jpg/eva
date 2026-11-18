// OPS-08 / ARCHITECTURE §27 "Load test: 50 concurrent streams, no error spike".
//
// Drives 50 concurrent widget conversations end to end through Caddy: mint a
// session, open a conversation, POST a question, and read the SSE stream to its
// `final` event. A stream that opens but never reaches `final` is a failure —
// "no error spike" means every visitor got an answer, not that the socket
// accepted the connection.
//
// k6 is a test-only tool: it is not in any product image and not in the locked
// stack table, so `scripts/ops08_load.py` invokes the binary directly rather than
// adding a runtime dependency.
//
// Configuration (env, all optional):
//   BASE_URL    default http://localhost  (Caddy, not the API container)
//   ORIGIN      default http://localhost  (must match a verified bot_domains row)
//   BOT_ID      public_id of the bot to talk to; minted out of band if empty
//   DURATION    how long to keep VUs running, default 30s
//   STREAM_TIMEOUT_MS  per-stream deadline, default 180000
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { randomString } from "https://jslib.k6.io/k6-utils/1.4.0/index.js";

const BASE = __ENV.BASE_URL || 'http://localhost';
const ORIGIN = __ENV.ORIGIN || 'http://localhost';
const BOT_ID = __ENV.BOT_ID;
const STREAM_TIMEOUT_MS = Number(__ENV.STREAM_TIMEOUT_MS || 180000);

// The two numbers the launch checklist is really about: did every stream reach its
// `final` event, and did any request come back as an error. A 200 that never
// terminates would pass a status-code-only check, which is why `final` is counted.
const finals = new Counter('sse_final_events');
const streamsWithoutFinal = new Counter('sse_streams_without_final');
const turnDuration = new Trend('sse_turn_duration', true);

export const options = {
  scenarios: {
    concurrent_streams: {
      // Exactly one turn per VU, all 50 in flight at once: the checklist says "50
      // *concurrent* streams", which this reproduces precisely. A duration-based
      // executor is deliberately not used — see the IP-limit note below.
      executor: 'per-vu-iterations',
      vus: Number(__ENV.VUS || 50),
      iterations: 1,
      maxDuration: __ENV.MAX_DURATION || '10m',
    },
  },
  thresholds: {
    // §27: "no error spike". Zero tolerance on HTTP errors is the only reading that
    // can fail; a percentage would let 50 concurrent streams produce 2 errors and pass.
    http_req_failed: ['rate==0'],
    checks: ['rate==1'],
    // A turn that never reaches `final` is a hung visitor, even though no request
    // failed. This is the check that catches an SSE endpoint that opens and stalls.
    sse_streams_without_final: ['count==0'],
    // Every VU completes its single turn, so the run really did carry 50 streams.
    sse_final_events: ['count>=50'],
  },
};

// IP rate limit, and why this scenario is shaped the way it is.
//
// SEC-02 limits a single client IP to `IP_REQUESTS_PER_MINUTE` (60) per minute
// (`app/conversation/controls.py`). In this harness all VUs share one source
// address, so a *sustained* 50-VU run is throttled at request 61 with
// `ip_rate_limited` — measured: 104 of 522 requests 429'd over 60 s, which is the
// limiter working as designed, not an error spike. Fifty real visitors arrive from
// fifty addresses, so the control is correct and the harness is what must bend.
//
// Do not "fix" a 429 here by raising the limit: that would delete the control under
// test. One turn per VU stays inside 60 requests and still puts 50 streams in flight
// simultaneously, which is the actual requirement.


function jsonHeaders(token) {
  return {
    'Content-Type': 'application/json',
    Origin: ORIGIN,
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

export default function () {
  const session = http.post(
    `${BASE}/api/v1/widget/session`,
    JSON.stringify({ bot_public_id: BOT_ID }),
    { headers: jsonHeaders(null), tags: { name: 'widget_session' } },
  );
  if (!check(session, { 'session mints a token': (r) => r.status === 200 && !!r.json('token') })) {
    return;
  }
  const token = session.json('token');

  const visitor = `k6-${randomString(16)}`;
  const started = http.post(
    `${BASE}/api/v1/widget/conversations`,
    JSON.stringify({ visitor_session_id: visitor, page_url: `${ORIGIN}/k6-load` }),
    { headers: jsonHeaders(token), tags: { name: 'start_conversation' } },
  );
  if (!check(started, { 'conversation starts': (r) => r.status === 201 || r.status === 200 })) {
    return;
  }
  const conversationId = started.json('conversation_id');

  // A unique question per stream on purpose: the turn loop caches clean grounded
  // answers, so 50 VUs asking the same question would mostly hit the cache and the
  // run would not exercise retrieval, embedding or the provider at all.
  const question = `Load test ${randomString(8)}: what is the return policy?`;

  const t0 = Date.now();
  const stream = http.post(
    `${BASE}/api/v1/widget/conversations/${conversationId}/messages`,
    JSON.stringify({ text: question }),
    {
      headers: jsonHeaders(token),
      tags: { name: 'sse_turn' },
      timeout: STREAM_TIMEOUT_MS,
      // k6 buffers the whole response, which is exactly what is needed here: the
      // SSE body ends at `event: final`, so the request completing *is* the signal
      // that the turn finished.
      responseType: 'text',
    },
  );
  const elapsed = Date.now() - t0;
  turnDuration.add(elapsed);

  const body = stream.body || '';
  const sawFinal = body.indexOf('event: final') !== -1;
  if (sawFinal) {
    finals.add(1);
  } else {
    streamsWithoutFinal.add(1);
  }

  check(stream, {
    'turn returns 200': (r) => r.status === 200,
    'turn reaches final event': () => sawFinal,
  });
}
