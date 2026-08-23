# 0001 — Local model transport: OpenAI-compatible gateway for extraction, Ollama bge-m3 for embeddings

The prototype stack named Claude via the Anthropic API for extraction and Voyage `voyage-3` cloud embeddings. We instead run extraction through the user's OpenAI-compatible LLM gateway (Aperture, default model `z-ai/glm-5.2`, env-configurable) and embeddings through a local Ollama `bge-m3` service. Rationale: provider portability (any OpenAI-compatible endpoint serves the extractor — no vendor SDK lock-in), and embeddings become local, free, and offline-capable. `bge-m3` outputs 1024 dimensions — identical to `voyage-3` — so the Neo4j vector index schema is untouched, and its multilingual training preserves headroom for non-English sources.

## Considered Options

- **Anthropic-protocol gateway + Voyage cloud embeddings** (original stack): rejected — couples the pipeline to one vendor protocol and keeps a paid cloud dependency for every embedded entity.
- **Ollama `nomic-embed-text` / `nomic-embed-text-v2-moe`** (already installed): rejected — 768-dim output would force a vector-index dimension change across schema and docs for no additional benefit.

## Consequences

- Extraction quality now depends on the gateway's served models; swapping models is an env change, and the extraction contract tests are model-agnostic (fake transport).
- If Ollama is down, ingestion degrades gracefully (entities stored without embeddings; semantic resolution layer skips) rather than failing.

## Measured results

Promoted from PRD #35 Results (live smoke captured by PR #48 on a fresh Neo4j after `pg init`, scripted Mode-2 approval):

- **Source:** local demo Markdown fixture, 3 sequential chunks.
- **Models:** extraction via OpenAI-compatible gateway (`z-ai/glm-5.2` served by OpenRouter at `https://ai.tailbac57a.ts.net/v1`); embeddings via local Ollama `bge-m3` (1024-dim).
- **Counts:** 3 extraction requests, 2 embedding requests, 0 ambiguity-queued candidates, 2 entities + 1 edge committed (`USED_BY`, confidence `0.80`), 0 rejected.
- **Latency:** 23.795 seconds elapsed end-to-end.
- **Configuration:** env-overridable per `Settings` / `.env.example` (`APERTURE_BASE_URL`, `LLM_MODEL` / `APERTURE_MODEL`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `PG_REJECTED_LOG_PATH`); gateway and Ollama are intentionally not pre-flighted so failures surface on first call rather than blocking ingest.
- **Degraded mode:** when Ollama is unreachable, entities are stored without embeddings, the semantic resolution layer skips, and alias + structural layers still run.

These numbers are the first production-like telemetry for the transport decision; subsequent runs are expected to scale linearly in chunk count and request counts until the gateway model or Neo4j writer becomes the bottleneck.
