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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            uri=os.getenv("NEO4J_URI", cls.uri),
            user=os.getenv("NEO4J_USER", cls.user),
            password=os.getenv("NEO4J_PASSWORD", cls.password),
            database=os.getenv("NEO4J_DATABASE", cls.database),
        )
