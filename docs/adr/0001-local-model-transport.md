# 0001 — Local model transport: OpenAI-compatible gateway for extraction, Ollama bge-m3 for embeddings

The prototype stack named Claude via the Anthropic API for extraction and Voyage `voyage-3` cloud embeddings. We instead run extraction through the user's OpenAI-compatible LLM gateway (Aperture, default model `z-ai/glm-5.2`, env-configurable) and embeddings through a local Ollama `bge-m3` service. Rationale: provider portability (any OpenAI-compatible endpoint serves the extractor — no vendor SDK lock-in), and embeddings become local, free, and offline-capable. `bge-m3` outputs 1024 dimensions — identical to `voyage-3` — so the Neo4j vector index schema is untouched, and its multilingual training preserves headroom for non-English sources.

## Considered Options

- **Anthropic-protocol gateway + Voyage cloud embeddings** (original stack): rejected — couples the pipeline to one vendor protocol and keeps a paid cloud dependency for every embedded entity.
- **Ollama `nomic-embed-text` / `nomic-embed-text-v2-moe`** (already installed): rejected — 768-dim output would force a vector-index dimension change across schema and docs for no additional benefit.

## Consequences

- Extraction quality now depends on the gateway's served models; swapping models is an env change, and the extraction contract tests are model-agnostic (fake transport).
- If Ollama is down, ingestion degrades gracefully (entities stored without embeddings; semantic resolution layer skips) rather than failing.

## Measured results

*(Filled at initiative close from PRD #35's Results section — live smoke stats: request counts, latency, committed graph counts.)*
