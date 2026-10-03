# Hybrid retrieval and grounded Q&A

Ticket #10 adds synchronous `POST /api/v1/retrieve` and `POST /api/v1/ask`.
Both require a bearer token and the existing `ASK_QUESTION` permission. Analyst,
reviewer and admin roles have this permission. The indexed corpus is shared;
the trace belongs to the authenticated requester.

## Retrieval contract

The query is embedded through #7's `IEmbeddingProvider`. #9's `IRetrievalStore`
performs independent pgvector cosine and PostgreSQL full-text searches. The
application fuses their rankings using **Reciprocal Rank Fusion**:

```text
rrf(chunk) = sum(1 / (k + rank_in_source))
k = 60 by default; ranks start at 1
```

A chunk contributes once per source. An absent source contributes zero. Raw
cosine and `ts_rank` scores are never combined. Fused-score ties use chunk UUID;
the database also orders equal dense/keyword scores by UUID. After reranking,
ties use RRF score and then UUID. Configuration controls deployment-wide limits,
not request-supplied weights or alternative fusion methods.

Every fused candidate is scored by the local
**`BAAI/bge-reranker-v2-m3`** cross-encoder, pinned to revision
`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`. It is accessed through `IReranker`;
all model and PyTorch imports stay in infrastructure. The adapter loads lazily,
disables remote model code, and runs inference on a dedicated thread. A busy
adapter rejects additional inference rather than accumulating an unbounded
queue. A request timeout does not forcibly terminate an active model thread.

### Score meaning and limits

**`relevance_score` is `sigmoid(z)`, where `z` is BGE's cross-encoder relevance
logit for the question/chunk pair.** It is in `[0, 1]`. It is neither an RRF
score nor a calibrated probability that a clinical statement is correct.
Reranker failure produces a safe HTTP 503; this pipeline never substitutes an
RRF score in an answered response.

| Setting | Default | Meaning |
| --- | --- | --- |
| `RETRIEVAL__CANDIDATE_K` | 20 | Candidates per dense/keyword branch; at most twice this many are reranked |
| `RETRIEVAL__RRF_K` | 60 | RRF rank constant |
| `RETRIEVAL__TOP_K` | 5 | Maximum selected chunks |
| `RETRIEVAL__SCORE_THRESHOLD` | 0.5 | Minimum normalized cross-encoder score, inclusive |
| `RETRIEVAL__MAX_CONTEXT_CHUNKS` | 12 | Additional hard ceiling on selected chunks |
| `RETRIEVAL__MAX_CONTEXT_CHARACTERS` | 24000 | Combined selected text budget; oversized chunks are omitted whole |
| `RETRIEVAL__TIMEOUT_SECONDS` | 180 | Embedding/search/fusion/reranking deadline |
| `RERANKER__DEVICE` | cpu | Local CPU or CUDA execution |
| `RERANKER__BATCH_SIZE` | 4 | Inference batch size |
| `RERANKER__MAX_LENGTH` | 1024 | Token limit for each question/chunk pair during scoring |
| `RERANKER__CACHE_DIRECTORY` | unset | Optional Hugging Face model-cache directory |

Queries have a 2000-character limit. The scorer may truncate long token pairs;
answers and citations retain the complete indexed text. Threshold calibration
and golden-set measurement belong to #12.

## Grounding and refusal

`AskUseCase` retrieves evidence and calls the existing #7 `ILLMProvider.complete`
with the versioned `grounded_answer` v2 policy in the system message. The question
and retrieved text are serialized as untrusted data in a separate user message.
No tools are supplied.

The model assesses whether the complete question is supported, including drug,
population, route and condition. It returns a status and selected chunk IDs.
The application accepts only the declared JSON shape and IDs from the selected
evidence. Answers quote those complete chunks in reranker order, with `[1]`,
`[2]`, etc. referring to the corresponding citation array entries. This keeps
qualifiers and negations intact and prevents model-written clinical prose or
metadata from entering the answer.

Empty or low-score retrieval, missing explicit high-risk information, directly
contradictory source statements, model-reported conflict, malformed output and
unresolvable IDs all return HTTP 200 with:

```json
{
  "answer": "Not enough information in the corpus",
  "citations": [],
  "refused": true,
  "trace_id": "<request trace UUID>"
}
```

The refusal string has no added punctuation. It is the Ticket #10 / AC-2.6
contract, including dosage questions; the older BR-01 example wording is not
used by this endpoint.

High-risk question checks require explicit dosage values/statements,
contraindication statements or interaction statements in the evidence actually
cited. These lexical checks can reject evidence; they do not prove clinical
correctness. Literal conflicts are rejected conservatively, and the prompt
handles contextual conflicts. Source accuracy and exhaustive semantic conflict
detection are not guaranteed. The broader Safety Checker and clinical-note
approval workflow remain #15/#17/#19.

Provider, reranker and store failures are service errors (HTTP 503 with
`KNOWLEDGE_UNAVAILABLE`), not evidence refusals. Internal provider/driver details
are not returned. Chat configuration and transient fallback use #7 unchanged.

## Exact citation shape

Every returned citation has exactly these seven fields:

```json
{
  "document_id": "<indexed document UUID>",
  "document_name": "synthetic-guide.pdf",
  "section": "Opening hours",
  "page": 3,
  "chunk_id": "<indexed chunk UUID>",
  "relevance_score": 0.9,
  "text_snippet": "<complete indexed chunk text>"
}
```

Document/name/section/page/chunk/text come directly from #9's joined search
result. `section` and `page` remain `null` when the indexed source has neither.
The model never supplies these values. Citation resolution is verified against
the actual `chunks`/`documents` rows in the database integration tests.

## Setup and requests

Follow [INGESTION.md](INGESTION.md) to start the existing Compose stack, seed the
synthetic PDF/Markdown documents and authenticate. `POST /retrieve` needs no
chat API key. Grounded answers with sufficient evidence need the existing chat
provider configured through `LLM__PROVIDER`, `LLM__MODEL`, `LLM__API_KEY` and
`LLM__FALLBACK`. Groq works in Compose with an environment-supplied key. The
existing Ollama adapter expects a server at `localhost:11434` in the API's
network namespace; merely setting `LLM__PROVIDER=ollama` does not start one.

```bash
curl -X POST http://localhost:8000/api/v1/retrieve \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"synthetic clinic opening hours"}'

curl -X POST http://localhost:8000/api/v1/ask \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"When does the synthetic clinic open?"}'
```

The first retrieval downloads roughly 2.27 GB of BGE weights. Allow several GB
of RAM for CPU inference and warm the model before a latency-sensitive demo.
Compose's API mounts the existing Hugging Face cache volume. A cold download
may exceed the request deadline; retry after the download/inference finishes.
No model weights are committed or baked into the repository.

## Observability

The existing `IAuditSink`/`LoggingAuditSink` records `retrieval.hybrid` and `qa.ask`
events with the same correlation/trace UUID. A small sink persists those events
using #6's existing `TraceModel` and `SpanModel`, sharing the application database
pool. No migration or second tracing system is introduced.

Retrieval spans contain the query, dense/keyword/fused counts, RRF k, model,
dense/keyword ranks and scores, fused and reranker scores, selected chunk IDs,
phase/total latency and refusal status. Ask spans add cited IDs, prompt version,
token usage, generation/total latency and refusal reason. The trace row preserves
request ownership; the existing trace access guard applies. Full trace viewing,
cost dashboards and general tracing administration remain separate work.

If trace persistence fails, the complete event remains in the existing audit log
and a warning is emitted. Queries are recorded as text; use only synthetic/public
non-PII data, restrict log/database access, and apply deployment retention rules.
No new PII detector or redactor is claimed. Authentication headers and provider
keys are never included in these events.

## Verification

The normal suite covers RRF math and k=60, reranker contracts, evidence/refusal
cases, prompt separation, citation resolution, JWT authorization and persisted
trace ownership. Set `TEST_DATABASE_URL` to a disposable PostgreSQL/pgvector
admin connection for database tests; the fixtures create and remove scratch
databases. Existing #7/#9 tests run unchanged apart from the deterministic SQL
tie ordering exercised by Ticket #10.

```bash
python -m pytest tests/unit/application/test_hybrid_retrieval.py tests/unit/application/test_grounded_qa.py
python -m pytest tests/infrastructure/test_reranking_provider.py tests/integration/test_knowledge_api.py

# Opt-in real MiniLM/BGE inference against the same Docker PostgreSQL service:
RUN_LOCAL_MODEL_TESTS=1 python -m pytest tests/integration/test_real_hybrid_retrieval.py -s
```

On PowerShell, set `$env:RUN_LOCAL_MODEL_TESTS = '1'` before the final command.
The real-model smoke uses synthetic sources, verifies both searches, RRF and BGE
scores, resolves citations to database rows and proves low-evidence refusal
without a chat call. Chat contract/API tests use deterministic provider doubles;
the smoke does not claim a live hosted-LLM response.

See [ADR-008](adr/ADR-008-hybrid-retrieval-and-grounded-qa.md) for the decisions.
