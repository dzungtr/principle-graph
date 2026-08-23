"""Environment-backed configuration for the local graph service."""
from __future__ import annotations

import os
from dataclasses import dataclass


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
            rejected_log_path=os.getenv("PG_REJECTED_LOG_PATH", cls.rejected_log_path),
        )