# ADR-007: Provider Abstraction & Selection

- **Status:** Accepted
- **Date:** 2026-09-30
- **Ticket:** #7 (Provider Abstraction for LLMs & Embeddings)
- **Requirements:** BRD AR-2 (Provider Abstraction — AR-2a chat interface with ≥2 implementations, AR-2b separate embedding interface, AR-2c per-capability fallback), AR-1 (no LLM/SDK imports in domain/application), AR-3 (composition root wires dependencies from config); SYSTEM-DESIGN §A.5.6 (LLM Provider Architecture), §A.5.2 (transient-failure handling); constraint C6 (no secrets in the repo)

## Context

ADR-005 established the layered skeleton and ADR-006 the configuration surface,
including stub `llm`/`embedding` config groups whose *adapters* were explicitly
deferred to this ticket. AR-2 requires a real provider abstraction: a chat
interface with at least two working implementations, a **separate** embedding
interface, and fallback defined **per capability** rather than as one global
chain. The inner layers must remain free of any vendor SDK (AR-1), and which
provider is used must be chosen by configuration, not hard-coded (AR-3).

The frozen SYSTEM-DESIGN §A.5.6 sketches these interfaces with illustrative
method names (`tool_call()`, `embed()`/`embed_batch()`, a `FallbackChain` with
`with_fallback()`, and an `AllProvidersExhaustedError`). The assessment states
the requirement in terms of *capabilities* — "completion, streaming, and
tool-calling", "text-to-vector embedding", "fallback chain per capability". This
ADR records how the implementation realises those capabilities, and resolves the
wording difference between the design sketch and the built interfaces without
editing the frozen SDD.

## Decision

1. **Segregated ports, provider-neutral DTOs (AR-2a/AR-2b, AR-1).** Two
   independent ports live in the application layer and import nothing beyond the
   standard library:
   - `app/application/ports/llm.py` — `ILLMProvider` with `complete()` and
     `stream()`, plus neutral DTOs (`CompletionRequest`, `CompletionResponse`,
     `StreamChunk`, `ModelOptions`, `ToolDefinition`, `ToolCall`, `ToolResult`).
   - `app/application/ports/embeddings.py` — `IEmbeddingProvider` with
     `generate_embeddings()` and the `EmbeddingResult` DTO.

   Keeping them separate is a direct application of Interface Segregation (SDD
   Decision 5): a provider may support chat but not embeddings, or vice versa, so
   no adapter is forced to implement a capability it lacks.

2. **Tool-calling is a per-request capability of the chat port, not a separate
   method.** Rather than a standalone `tool_call()`, tools are supplied on
   `CompletionRequest.tools` and any calls the model requests come back on
   `CompletionResponse.tool_calls` / `StreamChunk.tool_calls`. This matches the
   actual Groq and Ollama wire protocols (tools are passed per request; tool
   calls are returned in the response) and satisfies the assessment's
   "completion, streaming, **and tool-calling**" as three capabilities of one
   interface. **The provider only parses and returns tool calls; it never
   executes a tool** — execution belongs to orchestration (later tickets), which
   keeps the boundary clean and side-effect-free.

3. **Two complete chat adapters (AR-2a, ≥2 implementations).** Both fully
   implement `complete()` and `stream()`, including tool-call parsing/return and
   token-usage extraction:
   - `GroqAdapter` (`app/infrastructure/llm/groq_adapter.py`) — OpenAI-compatible
     hosted provider via the `groq` SDK.
   - `OllamaAdapter` (`app/infrastructure/llm/ollama_adapter.py`) — local provider
     over `httpx` against `/api/chat`.

   Each maps vendor errors into the typed taxonomy (§A.5.1/ADR-006):
   connection/timeout → `ProviderUnavailableError` (transient), HTTP 429 →
   `ProviderRateLimitError` (transient), 401/403 → `ProviderAuthenticationError`,
   context-length 400 → `ContextWindowExceededError`, other 400 →
   `ProviderInvalidRequestError`.

4. **One local embedding adapter (AR-2b, ≥1 implementation).**
   `LocalEmbeddingAdapter` (`app/infrastructure/embeddings/local_adapter.py`)
   wraps sentence-transformers (`all-MiniLM-L6-v2`, 384-dim). The model is loaded
   lazily so importing the module — and constructing the container in tests — does
   not pull the weights. `generate_embeddings(texts: list[str])` is a single
   batch method that covers both the single-text (`embed()`) and batch
   (`embed_batch()`) cases from the SDD sketch: embedding one string is a
   one-element list. Chat runs hosted while embeddings run local, exactly the
   split AR-2b anticipates.

5. **Per-capability, transient-only fallback (AR-2c).** Fallback is realised as a
   **composite adapter**, `FallbackLLMProvider(ILLMProvider)`
   (`app/infrastructure/llm/fallback.py`), that holds a `primary` and a
   `secondary` and itself satisfies `ILLMProvider`. This is the Decorator/Composite
   form of the SDD's `FallbackChain.with_fallback()`: because the composite *is* a
   provider, it is injected wherever an `ILLMProvider` is expected, and the port
   stays free of any fallback-specific method. Fallback is a **chat-only** concern
   here (see §6); there is no global cross-capability chain.
   - It falls back **only on transient errors** (`ProviderUnavailableError`,
     `ProviderRateLimitError`). Non-transient faults (auth, invalid request,
     context-window) propagate immediately — retrying a different provider cannot
     fix a malformed request and would only add latency.
   - The exact `CompletionRequest` is forwarded unchanged to the secondary. For
     `stream()`, the primary's first chunk is awaited before yielding, so a
     transient failure that surfaces on connection still fails over cleanly
     without emitting a partial stream.

6. **Embedding fallback is out of scope now, by design.** AR-2c names embedding
   fallback (`local → [optional]`) as an *example*, and §A.5.6(6) states a
   provider joins a chain only when its adapter and credentials are configured,
   with the minimum viable set being **Groq (chat) + sentence-transformers
   (embed)**. Only one embedding implementation exists (the local one) and no
   optional hosted embedder (e.g. Gemini) is configured, so there is nothing to
   fall back *to*; wrapping a single adapter in a one-element chain would add
   indirection with no behavioural change. When a second embedder lands, the same
   composite pattern applies to embeddings.

7. **Provider-exhaustion behaviour (honest scope).** With the current two-node
   chain, if the primary fails transiently and the secondary also fails, the
   secondary's typed error propagates to the caller. The SDD's named
   `AllProvidersExhaustedError` (§A.5.6, step 4) is **not** introduced yet: with
   exactly two nodes the last typed provider error already conveys the cause, and
   a dedicated exhaustion type earns its place only once N-node chains exist. This
   is recorded as a deliberate, documented deferral rather than an omission.

8. **Config-driven selection in the single composition root (AR-3).**
   `app/core/config.py` `LLMSettings` gains `fallback: str | None = "ollama"`
   alongside the existing `provider: str = "groq"`. `app/core/container.py`
   builds the chat provider from those values via `build_llm_provider(settings)`:
   `provider` selects the primary adapter, `fallback` the secondary; a distinct
   fallback yields a `FallbackLLMProvider`, while a blank/None fallback — or one
   equal to the primary — yields the bare primary. An unrecognised name raises the
   typed **`ProviderConfigurationError`** (a non-transient server fault from the
   ADR-006 taxonomy), so misconfiguration fails the app safely at startup instead
   of silently selecting the wrong provider. Selection lives only here — the
   composition root — so no second wiring site is introduced, and swapping
   `provider`/`fallback` in configuration is the entire cost of changing providers.

## Enforcement

| Mechanism | What it adds this ticket | Where it runs |
| --------- | ------------------------ | ------------- |
| **import-linter** | `groq`, `sentence_transformers`, `httpx` already forbidden in the domain-pure and application contracts — the SDKs stay inside the adapters. | CI step `lint-imports` |
| **AST boundary test** | Same third-party names in `_FORBIDDEN_THIRD_PARTY`; inner layers importing a vendor SDK fails the scan. | Normal `pytest` step |
| **Provider contract tests** | `tests/infrastructure/test_llm_providers.py` / `test_embedding_providers.py` assert each adapter satisfies its port and cover complete/stream/tool-calls, error mapping, and transient-only fallback (with a non-transient case that must *not* fall back). | Normal `pytest` step |
| **Selection tests** | `tests/unit/core/test_container.py` proves configuration changes the primary and the fallback ordering, blank/self fallback yields the bare primary, and an unknown provider raises `ProviderConfigurationError`. | Normal `pytest` step |

## Consequences

**Positive**

- Chat and embeddings sit behind small, segregated ports; the domain and
  application layers never see a vendor SDK (AR-1), verified mechanically.
- Two complete chat adapters plus a local embedder satisfy AR-2a/AR-2b, and the
  provider used is a pure configuration choice (AR-3) with a fail-safe on bad
  config.
- Fallback is per-capability and transient-only: resilience where retrying helps,
  fast failure where it cannot, with the exact request preserved.

**Negative / accepted trade-offs**

- The built interfaces differ in shape from the SDD §A.5.6 sketch (`tool_call()`,
  `embed()`/`embed_batch()`, `with_fallback()`, `AllProvidersExhaustedError`).
  This ADR is the record that the *capabilities* are met while the method shapes
  follow the real provider APIs and Python idiom; the frozen SDD is left
  unchanged (decisions are captured in ADRs, not by editing the baseline).
- No embedding fallback and no named exhaustion error yet — both deferred to when
  a second embedder / an N-node chain actually exists (§6, §7), rather than built
  speculatively.
- Fallback is wired as primary + one secondary. A longer chain would need a small
  generalisation of the composite; the two-node form covers the minimum-viable
  chat chain (Groq → Ollama).

## Notes

This ADR records interpretation only; the frozen SYSTEM-DESIGN.md is not edited.
ADR-001–004 remain reserved for the decisions they name (see
[`README.md`](./README.md)).
