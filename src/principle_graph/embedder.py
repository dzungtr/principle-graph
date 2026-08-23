"""Embedding providers used by the graph pipeline."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any
from urllib import request

from .config import Settings


class OllamaEmbedder:
    """Fetch bge-m3 embeddings from a local Ollama service.

    Ollama is an optional semantic-resolution aid. A failed request is therefore
    converted to ``None`` rather than escaping through the embedder seam.
    """

    def __init__(self, settings: Settings | None = None, *, base_url: str | None = None,
                 model: str | None = None, timeout: float = 10.0,
                 http_client: Callable[..., Any] | None = None) -> None:
        settings = settings or Settings.from_env()
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout = timeout
        self._http_client = http_client
        self._warned = False

    def embed(self, name: str) -> Sequence[float] | None:
        """Return the 1024-dimensional embedding for *name*, or ``None`` on error."""
        payload = json.dumps({"model": self.model, "prompt": name}).encode("utf-8")
        req = request.Request(f"{self.base_url}/api/embeddings", data=payload,
                              headers={"Content-Type": "application/json"}, method="POST")
        try:
            opener = self._http_client or request.urlopen
            response = opener(req, timeout=self.timeout)
            if hasattr(response, "__enter__"):
                with response as active:
                    body = active.read()
            else:
                body = response.read()
            vector = json.loads(body)["embedding"]
            if (not isinstance(vector, list) or len(vector) != 1024 or
                    not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                            for value in vector)):
                raise ValueError("Ollama returned an invalid embedding")
            return [float(value) for value in vector]
        except Exception as exc:  # the seam must never propagate Ollama failures
            self._warn_once(exc)
            return None

    def _warn_once(self, error: Exception) -> None:
        if not self._warned:
            logging.getLogger(__name__).warning("Ollama embedding unavailable: %s", error)
            self._warned = True