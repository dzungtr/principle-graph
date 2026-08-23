import pytest

from principle_graph.extraction_contract import (
    ContractError,
    chunk_markdown,
    chunk_pdf_pages,
    validate_triple,
)


VALID = {
    "subject": "rates",
    "subject_type": "policy_action",
    "relation": "reduces",
    "object": "borrowing",
    "object_type": "economic_behavior",
    "confidence": 0.5,
    "evidence": "The text links higher rates to less borrowing.",
    "scope_conditions": "short run",
    "source_ref": "book:chapter-1/page-2",
}


def test_validates_contract_and_allows_new_normalized_labels():
    assert validate_triple({**VALID, "relation": "influences_demand"}) == {**VALID, "relation": "influences_demand"}


@pytest.mark.parametrize("field,value", [("subject_type", "Policy Action"), ("relation", "causes-")])
def test_rejects_non_normalized_labels(field, value):
    with pytest.raises(ContractError):
        validate_triple({**VALID, field: value})


def test_rejects_missing_fields_and_bad_confidence():
    with pytest.raises(ContractError):
        validate_triple({key: value for key, value in VALID.items() if key != "evidence"})
    with pytest.raises(ContractError):
        validate_triple({**VALID, "confidence": 1.1})


def test_markdown_chunks_retain_heading_and_source_order():
    chunks = chunk_markdown("# Chapter\nintro\n## Section\nbody")
    assert [chunk.text for chunk in chunks] == ["# Chapter\nintro", "## Section\nbody"]
    assert chunks[1].section_path == ("Chapter", "Section")
    assert [chunk.id for chunk in chunks] == ["chunk-1", "chunk-2"]
    assert chunks[0].source_ref == "markdown:chunk-1"


def test_pdf_chunks_retain_page_numbers_and_paragraph_boundaries():
    chunks = chunk_pdf_pages(["first paragraph\n\nsecond paragraph", "third"])
    assert [chunk.text for chunk in chunks] == ["first paragraph", "second paragraph", "third"]
    assert [chunk.pages for chunk in chunks] == [(1,), (1,), (2,)]
    assert [chunk.source_ref for chunk in chunks] == ["pdf:chunk-1", "pdf:chunk-2", "pdf:chunk-3"]


def test_pdf_chunk_crossing_page_break_keeps_page_range():
    chunks = chunk_pdf_pages(["Chapter 1\n\nA paragraph that", "continues on the next page."])
    assert len(chunks) == 2
    assert chunks[-1].text == "A paragraph that\ncontinues on the next page."
    assert chunks[-1].pages == (1, 2)


def test_pdf_chunk_does_not_merge_separate_page_paragraphs():
    chunks = chunk_pdf_pages(["Chapter 1\n\nfirst paragraph", "a separate new paragraph"])
    assert [chunk.text for chunk in chunks] == [
        "Chapter 1", "first paragraph", "a separate new paragraph"
    ]
    assert [chunk.pages for chunk in chunks] == [(1,), (1,), (2,)]


def test_pdf_chunk_merges_unterminated_paragraph_without_heading():
    chunks = chunk_pdf_pages(["the long paragraph continues onto the", "next page mid-sentence"])
    assert len(chunks) == 1
    assert chunks[0].text == "the long paragraph continues onto the\nnext page mid-sentence"
    assert chunks[0].pages == (1, 2)


def test_markdown_source_id_is_traceable():
    chunk = chunk_markdown("# Intro\ntext", source_id="book-1")[0]
    assert chunk.source_ref == "book-1:chunk-1"
