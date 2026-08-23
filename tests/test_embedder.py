import json
import logging

from principle_graph.config import Settings
from principle_graph.embedder import OllamaEmbedder


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body


def test_ollama_request_shape_and_vector():
    calls = []

    def fake_http(request, timeout):
        calls.append((request, timeout))
        return Response(json.dumps({"embedding": [1] * 1024}).encode())

    result = OllamaEmbedder(Settings(), http_client=fake_http).embed("Marie Curie")

    assert result == [1.0] * 1024
    req, timeout = calls[0]
    assert req.full_url == "http://localhost:11434/api/embeddings"
    assert req.method == "POST"
    assert req.headers["Content-type"] == "application/json"
    assert json.loads(req.data) == {"model": "bge-m3", "prompt": "Marie Curie"}
    assert timeout == 10.0


def test_ollama_failure_warns_once_and_degrades(caplog):
    calls = 0

    def fake_http(request, timeout):
        nonlocal calls
        calls += 1
        raise OSError("offline")

    with caplog.at_level(logging.WARNING):
        embedder = OllamaEmbedder(http_client=fake_http)
        assert embedder.embed("one") is None
        assert embedder.embed("two") is None

    assert calls == 2
    assert [record for record in caplog.records if "Ollama embedding unavailable" in record.message] \
        == [caplog.records[0]]


def test_ollama_settings_are_env_overridable(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434/")
    monkeypatch.setenv("OLLAMA_MODEL", "custom-model")

    settings = Settings.from_env()
    assert settings.ollama_base_url == "http://ollama:11434/"
    assert settings.ollama_model == "custom-model"