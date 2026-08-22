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


def chunk_markdown(source: str) -> list[Chunk]:
    """Split Markdown at headings, retaining each heading with its section."""
    lines = source.splitlines()
    chunks: list[Chunk] = []
    current: list[str] = []
    path: list[str] = []
    ordinal = 0
    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and current:
            text = "\n".join(current).strip()
            if text:
                ordinal += 1
                chunks.append(Chunk(f"section-{ordinal}", text, tuple(path)))
            current = []
        if match:
            level, heading = len(match.group(1)), match.group(2)
            path = path[: level - 1] + [heading]
        current.append(line)
    text = "\n".join(current).strip()
    if text:
        ordinal += 1
        chunks.append(Chunk(f"section-{ordinal}", text, tuple(path)))
    return chunks


def chunk_pdf_pages(pages: list[str]) -> list[Chunk]:
    """Represent PDF paragraphs while retaining page provenance."""
    chunks: list[Chunk] = []
    for page_number, page in enumerate(pages, 1):
        for paragraph in re.split(r"\n\s*\n", page.strip()):
            text = paragraph.strip()
            if text:
                chunks.append(Chunk(f"page-{page_number}-{len(chunks) + 1}", text, (), (page_number,)))
    return chunks
