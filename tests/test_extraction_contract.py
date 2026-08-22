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
    assert [chunk.id for chunk in chunks] == ["section-1", "section-2"]


def test_pdf_chunks_retain_page_numbers_and_paragraph_boundaries():
    chunks = chunk_pdf_pages(["first paragraph\n\nsecond paragraph", "third"])
    assert [chunk.text for chunk in chunks] == ["first paragraph", "second paragraph", "third"]
    assert [chunk.pages for chunk in chunks] == [(1,), (1,), (2,)]
