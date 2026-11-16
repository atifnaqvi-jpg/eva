# Retrieval: fusion order and decline contract

This document is the normative description of how EVA turns a question into
ranked chunks, and when it refuses to answer at all. It exists because the fusion
order is a series of small, individually reasonable choices that are nearly
impossible to reconstruct from code later — and because getting the order wrong
produces answers that look fine while being ranked by accident.

Written before the WP-09 implementation, deliberately. Where this document and
the code disagree, this document is wrong only if it is amended first.

Sources: ARCHITECTURE §10.3 (retrieval), §10.4 (grounding), §10.5 (versioning);
R19 (handover), R18 (knowledge priority — **not applicable yet, see §7**).

---

## 1. The pipeline

```
question
  → [1] embed (one batch, via the embed service)
  → [2] dense candidates   (pgvector HNSW, cosine)   ─┐  each list is already
  → [3] sparse candidates  (tsvector FTS, ts_rank_cd) ─┘  tenant+version filtered
  → [4] RRF merge                       (k = 60)
  → [5] rerank top-20 → top-5           (optional, on by default)
  → [6] confidence floor                → DECLINE below threshold
  → [7] citations
```

Every stage is pure and deterministic given its inputs, so the fusion order can be
tested without a model and replayed from a fixture.

## 2. Filtering — the invariant that is never optional

Dense and sparse retrieval both run inside **one** SQL statement each, and both
carry the same two predicates:

```sql
WHERE tenant_id = :tenant_id
  AND document_version_id = ANY(:version_ids)
```

`version_ids` is resolved **by the caller** from the bot's
`active_knowledge_version_id` pointer. There is no code path that searches without
`version_ids`: an empty list returns no rows rather than falling back to
"everything", and the store raises on an empty list instead of silently widening
the query.

Two consequences worth stating because they are the point:

- **A draft version is unreachable through the published path.** Drafts are only
  visible to the owner's preview endpoint, which passes draft ids explicitly.
- **Cross-tenant leakage is structurally impossible**, not merely discouraged.
  RLS enforces it, and the explicit predicate keeps the query plan honest (the
  HNSW index is only usable when the tenant predicate is present).

## 3. Dense retrieval

pgvector HNSW, cosine distance, `ORDER BY embedding <=> :query`.

`LIMIT :candidates` (default 20 per list, configurable, hard-capped at 100). Chunks
with a `NULL` embedding are excluded — they exist between parse and embed, and
scoring them as zero would rank noise.

Score reported to callers is **similarity** (`1 - distance`), in `[0, 1]` for
cosine on unit vectors. Distance is what the index orders by; similarity is what
humans and thresholds reason about. Both are computed in one place so they cannot
drift apart.

## 4. Sparse retrieval

PostgreSQL full-text search on `chunks.fts`, which is a generated
`to_tsvector('english', text)` column with a GIN index. Ranking uses
`ts_rank_cd`.

Chosen over an external BM25 engine because it is the same table, the same
transaction and the same RLS policies — no second source of truth, no sync lag, no
new service. `ts_rank_cd` is not identical to BM25, but RRF (§5) only consumes
*ranks*, so the difference in score scale is irrelevant; only ordering quality
matters.

Query construction: the question is passed through `websearch_to_tsquery`, which
tolerates quoted phrases, `or`, and stray punctuation from real user input without
raising. An empty tsquery (stopword-only question) yields an empty sparse list,
which RRF handles as a one-list fusion.

## 5. Fusion — Reciprocal Rank Fusion

```
RRF(d) = Σ over lists L containing d of  1 / (k + rank_L(d))        k = 60
```

Properties, and why this rather than score normalisation:

- **Scale-free.** Cosine similarity and `ts_rank_cd` live on incomparable scales;
  RRF only uses rank position, so no calibration step exists to get wrong.
- **Consensus-seeking.** A chunk ranked highly by both retrievers outranks one
  ranked highly by either alone.
- **Rank-1 heavy.** The `k` constant bounds the advantage of a single top hit, so
  a fluke exact match cannot dominate a document that both lists agree on.

`k = 60` is the value from the original RRF paper and the common default; it is
config, not a tuned constant, and changing it is an eval-visible change.

Tie-break, fully specified because it must be deterministic: **equal RRF score →
higher dense rank wins → then lower `chunks.ordinal` → then lower `chunks.id`
lexicographically.** Without a total order, two runs over identical rows can return
different orders and flaky evals get blamed on the model.

RRF is computed in Python, not SQL. The candidate sets are at most a few hundred
rows, and keeping fusion out of SQL keeps it testable without a database.

## 6. Rerank

Optional, **on by default**, per-tenant configurable (RAG-06). Input: the fused
top-20. Output: top-5.

A reranker is a cross-encoder or an LLM scoring `(question, chunk)` jointly. It is
the main lever on answer quality once recall is adequate, and it is the one stage
that can be turned off to trade quality for latency and cost.

**Failure policy: a rerank error is not a retrieval failure.** Timeout, provider
error or malformed response → log, keep the RRF order, and mark the response
`reranked: false`. Silently degrading to RRF is strictly better than failing a
customer's question, and the flag makes the degradation visible to the eval
harness so it cannot become invisible drift.

**Model is not specified by the blueprint.** It is a config default
(`RERANK_MODEL`), and the eval harness (§WP-10) is what makes changing it
measurable. The default must not send tenant text to a third party without a
ruling — see §9.

## 7. Knowledge priority (R18) — deliberately absent

R18 ranks uploads > crawl > FAQ. §10.3 ranks purely by similarity. These conflict,
and the conflict is **unresolved** (PLAN §8.4.1, HALT-03).

Therefore, by explicit instruction: **source kind does not enter the score, the
sort, or any tie-break in this pipeline.** No `source_weight`, `priority`, or
`source_kind` parameter exists in any retrieval signature. When HALT-03 is ruled,
the change belongs in exactly one place — a single documented step between §5 and
§6 — and this section is where its semantics must be written down first.

Nothing in WP-09 pre-empts that ruling: RRF's tie-break (§5) is about determinism,
not authority.

## 8. Confidence floor — where the product refuses

The floor is the primary hallucination control (§10.3.5, §10.4, R19). It is a
**product decision expressed as configuration**, not an emergent property.

Default: `confidence_threshold = 0.55` on the bot row (already the schema default).

Evaluation input: the score of the best chunk **after** reranking, expressed as
similarity in `[0, 1]`. If reranking succeeded, that is the reranker's calibrated
score; if it was skipped or failed, it is the dense similarity.

Behaviour:

- `best_score < threshold` → **DECLINE**. The pipeline returns no answer. The
  caller receives a refusal with the handover affordance (R19: email notification +
  contact form). It must not "answer softly" or return the best chunk anyway.
- No candidates at all → DECLINE, with a distinct reason. A knowledge base with
  nothing relevant and one that is empty are different operator problems and are
  logged differently.

The exact threshold, the score scale it is compared against, and the decline reason
are all returned in the response envelope. A refusal that cannot be explained to
the operator is an outage nobody can debug.

## 9. Cost, latency and privacy notes

- One embed call per question (batch size 1), one dense query, one sparse query,
  plus an optional rerank. Rerank dominates latency when enabled.
- Tenant text reaches the embed service (self-hosted, Ruling 2 — no egress).
  A hosted reranker **would** be egress and needs a ruling before becoming the
  default.
- Every stage records its candidate count and timing so a slow or empty stage is
  visible in logs without re-running with debug tracing.

## 10. What is tested, and what cannot be

Tested without a model or a network:
- RRF arithmetic, including the total-order tie-break.
- Empty-list handling for either retriever.
- Decline below the floor, and the distinct no-candidates decline.
- Cross-tenant candidate leakage (tenant B's chunks must never appear for A).
- Draft-version unreachability through the published path.
- Rerank failure degrading to RRF order rather than raising.

Not visible to in-process tests, and therefore verified against the live stack in
the M2 gate report instead of being implied by green unit tests:
- HNSW index selection and behaviour at real row counts.
- Real `ts_rank_cd` ordering on real English text.
- Model-dependent score scale, and therefore threshold calibration.
