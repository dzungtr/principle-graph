"""Environment-backed configuration for the local graph service."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    """Parse a float env var, falling back to *default* when unset or invalid."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if 0.0 < value <= 1.0 else default


@dataclass(frozen=True)
class Settings:
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = "principlegraph"
    database: str = "neo4j"
    llm_base_url: str = "http://localhost:8000"
    llm_model: str = "z-ai/glm-5.2"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "bge-m3"
    # Mirrors fanout.QUERY_SEED_SIMILARITY; pinned equal by test.
    query_seed_similarity: float = 0.60
    rejected_log_path: str = ".pg/rejected.jsonl"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            uri=os.getenv("NEO4J_URI", cls.uri),
            user=os.getenv("NEO4J_USER", cls.user),
            password=os.getenv("NEO4J_PASSWORD", cls.password),
            database=os.getenv("NEO4J_DATABASE", cls.database),
            llm_base_url=os.getenv("APERTURE_BASE_URL", cls.llm_base_url),
            llm_model=os.getenv("LLM_MODEL", os.getenv("APERTURE_MODEL", cls.llm_model)),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", cls.ollama_base_url),
            ollama_model=os.getenv("OLLAMA_MODEL", cls.ollama_model),
            query_seed_similarity=_env_float("PG_QUERY_SEED_SIMILARITY", cls.query_seed_similarity),
            rejected_log_path=os.getenv("PG_REJECTED_LOG_PATH", cls.rejected_log_path),
        )