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
            rejected_log_path=os.getenv("PG_REJECTED_LOG_PATH", cls.rejected_log_path),
        )
