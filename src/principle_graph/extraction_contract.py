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
    # Optional domain tag (PRD #76 slice #78): absent means untagged; when
    # present it must already be lowercase snake_case — canonicalization
    # against the domain registry happens at the write boundary, not here.
    if "domain" in candidate:
        domain = candidate["domain"]
        if not isinstance(domain, str) or not _LABEL.fullmatch(domain):
            raise ContractError("domain must be lowercase snake_case when present")
    return candidate


_REQUIRED_STATE = (
    "entity", "entity_type", "state_key", "value", "unit", "as_of",
    "confidence", "evidence", "scope_conditions", "source_ref",
)


def validate_state(candidate: dict[str, Any]) -> dict[str, Any]:
    """Validate a ``propose_state`` candidate and normalize its value to a string.

    Same provenance discipline as :func:`validate_triple` (issue #98): non-empty
    strings for the claim text, snake_case labels, bounded confidence, and the
    chunk's exact ``source_ref``. ``value`` may be numeric or qualitative
    (``elevated``) — both normalize to a string; ``state_key`` canonicalization
    against the state registry happens at the write boundary, not here.
    """
    missing = [key for key in _REQUIRED_STATE if key not in candidate]
    if missing:
        raise ContractError(f"missing fields: {', '.join(missing)}")
    for key in ("entity", "evidence", "source_ref", "as_of"):
        if not isinstance(candidate[key], str) or not candidate[key].strip():
            raise ContractError(f"{key} must be a non-empty string")
    for key in ("scope_conditions",):
        if not isinstance(candidate[key], str):
            raise ContractError(f"{key} must be a string (empty when unconditional)")
    for key in ("entity_type", "state_key"):
        if not isinstance(candidate[key], str) or not _LABEL.fullmatch(candidate[key]):
            raise ContractError(f"{key} must be lowercase snake_case")
    confidence = candidate["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ContractError("confidence must be a number in [0, 1]")
    value = candidate["value"]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)) \
            or not str(value).strip() or (isinstance(value, str) and not value.strip()):
        raise ContractError("value must be a non-empty string or number")
    candidate["value"] = str(value).strip()
    unit = candidate["unit"]
    if not isinstance(unit, str):
        raise ContractError("unit must be a string (empty for qualitative values)")
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


_CONTINUATION_WORDS = frozenset(
    "a an and as at by for from in of on or than that the to with".split()
)


def _looks_like_page_continuation(text: str, next_paragraph: str) -> bool:
    """Return whether a page's final paragraph probably continues on the next page.

    PDF extraction does not preserve the layout signal distinguishing a paragraph
    break from a page break.  Be conservative: punctuation, a hyphen, or a
    sentence ending in a conjunction/preposition is useful evidence of a wrapped
    sentence; an arbitrary unterminated phrase is not.
    """
    if not text or not next_paragraph or re.match(r"^(chapter\s+\d+|\d+(?:\.\d+)*\s+\S.+)$", next_paragraph, re.I):
        return False
    if text.endswith((",", ";", ":", "-", "—")):
        return True
    if re.search(r"\b(?:" + "|".join(_CONTINUATION_WORDS) + r")$", text, re.I):
        return True
    return False


def chunk_pdf_pages(pages: list[str], source_id: str = "pdf") -> list[Chunk]:
    """Chunk extracted PDF pages, joining likely paragraphs crossing page breaks."""
    chunks: list[Chunk] = []
    ordinal = 0
    for page_number, page in enumerate(pages, 1):
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", page.strip()) if p.strip()]
        for paragraph in paragraphs:
            if (chunks and chunks[-1].pages[-1] == page_number - 1
                    and _looks_like_page_continuation(chunks[-1].text, paragraph)):
                previous = chunks[-1]
                chunks[-1] = Chunk(previous.id, previous.text + "\n" + paragraph, previous.section_path, previous.pages + (page_number,), previous.source_ref)
                continue
            ordinal += 1
            chunks.append(_chunk(paragraph, ordinal, source_id, (), (page_number,)))
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
