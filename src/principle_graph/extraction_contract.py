"""Validation and deterministic chunk-boundary helpers for extraction."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_LABEL = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_REQUIRED = (
    "subject", "subject_type", "relation", "object", "object_type",
    "confidence", "evidence", "scope_conditions", "source_ref",
)


class ContractError(ValueError):
    """A candidate extraction does not satisfy the tool contract."""


def validate_triple(candidate: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a normalized candidate without changing its claim."""
    missing = [key for key in _REQUIRED if key not in candidate]
    if missing:
        raise ContractError(f"missing fields: {', '.join(missing)}")
    for key in ("subject", "object", "evidence", "scope_conditions", "source_ref"):
        if not isinstance(candidate[key], str) or not candidate[key].strip():
            raise ContractError(f"{key} must be a non-empty string")
    for key in ("subject_type", "relation", "object_type"):
        if not isinstance(candidate[key], str) or not _LABEL.fullmatch(candidate[key]):
            raise ContractError(f"{key} must be lowercase snake_case")
    confidence = candidate["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ContractError("confidence must be a number in [0, 1]")
    return candidate


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    section_path: tuple[str, ...]
    pages: tuple[int, ...] = ()
    source_ref: str = ""


def _chunk(text: str, ordinal: int, source_id: str, path: tuple[str, ...], pages: tuple[int, ...] = ()) -> Chunk:
    chunk_id = f"chunk-{ordinal}"
    return Chunk(chunk_id, text, path, pages, f"{source_id}:{chunk_id}")


def chunk_markdown(source: str, source_id: str = "markdown") -> list[Chunk]:
    """Split Markdown at headings, retaining headings and paragraph blocks."""
    lines = source.splitlines()
    chunks: list[Chunk] = []
    current: list[str] = []
    path: list[str] = []
    ordinal = 0

    def emit() -> None:
        nonlocal ordinal
        text = "\n".join(current).strip()
        if text:
            ordinal += 1
            chunks.append(_chunk(text, ordinal, source_id, tuple(path)))

    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and current:
            emit()
            current = []
        if match:
            level, heading = len(match.group(1)), match.group(2)
            path = path[: level - 1] + [heading]
        current.append(line)
    emit()
    return chunks


def chunk_pdf_pages(pages: list[str], source_id: str = "pdf") -> list[Chunk]:
    """Chunk extracted PDF pages, joining paragraphs that cross page breaks."""
    chunks: list[Chunk] = []
    ordinal = 0
    prior_page_had_heading = False
    for page_number, page in enumerate(pages, 1):
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", page.strip()) if p.strip()]
        page_had_heading = any(re.match(r"^(chapter\s+\d+|\d+(?:\.\d+)*\s+\S.+)$", p, re.I) for p in paragraphs)
        for paragraph in paragraphs:
            heading = re.match(r"^(chapter\s+\d+|\d+(?:\.\d+)*\s+\S.+)$", paragraph, re.I)
            if chunks and chunks[-1].pages[-1] == page_number - 1 and prior_page_had_heading and not heading and not re.match(r"^(chapter\s+\d+|\d+(?:\.\d+)*)\b", paragraph, re.I):
                previous = chunks[-1]
                chunks[-1] = Chunk(previous.id, previous.text + "\n" + paragraph, previous.section_path, previous.pages + (page_number,), previous.source_ref)
                continue
            ordinal += 1
            chunks.append(_chunk(paragraph, ordinal, source_id, (), (page_number,)))
        prior_page_had_heading = page_had_heading
    return chunks


def chunk_pdf(source: str, source_id: str = "pdf") -> list[Chunk]:
    """Extract and chunk a PDF file using PyMuPDF (``fitz``)."""
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError("PDF chunking requires PyMuPDF") from exc
    document = fitz.open(source)
    try:
        return chunk_pdf_pages([page.get_text("text") for page in document], source_id)
    finally:
        document.close()
