import os
from pathlib import Path
import sys

# ensure workspace src is on path for tests
sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

from submission_checker import checker
from pypdf import PdfWriter


def make_pdf(pages_text, path):
    writer = PdfWriter()
    for txt in pages_text:
        writer.add_blank_page(width=72, height=72)
        # pypdf currently doesn't allow adding text easily; we'll ignore actual text content and monkeypatch extraction
    with open(path, "wb") as f:
        writer.write(f)


def test_empty_pdf(tmp_path, monkeypatch):
    pdf_path = tmp_path / "empty.pdf"
    print(f"PDF path: {pdf_path}")
    make_pdf([], pdf_path)
    # override extractor to return manual text
    monkeypatch.setattr(checker, "extract_text_per_page", lambda p: [])
    warns = checker.check_file(str(pdf_path), max_pages=1)
    assert "Number of pages" not in " ".join(warns)


def test_warnings(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    print(f"PDF path: {pdf_path}")
    texts = [
        "Title\nAuthor\nemail@example.com",
        "Content\nour previous paper [3]",
        "References",
        "Figure 1 after refs",
    ]
    make_pdf(texts, pdf_path)
    # Avoid calling the slow PDF extractor during unit tests
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    warns = checker.check_file(str(pdf_path), max_pages=2, style="acm")
    assert any("Number of pages" in w for w in warns)
    assert any("References start" in w for w in warns)
    assert any("Non-anonymous email" in w for w in warns)
    assert any("Suspicious wording" in w for w in warns)
    assert any("Figures/tables/appendix" in w for w in warns)


def test_min_pages_warning(tmp_path, monkeypatch):
    pdf_path = tmp_path / "short.pdf"
    texts = ["Page 1 text", "Page 2 text"]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), min_pages=3)
    assert any("less than minimum required" in w for w in warns)


def test_reference_format_warning(tmp_path, monkeypatch):
    """Test detection of author-style citations with IEEE style."""
    pdf_path = tmp_path / "author_refs.pdf"
    texts = [
        "Title and Introduction",
        "Some content with IEEE reference",
        "More content here",
        "REFERENCES",
        "[Smith et al.(2020)] John Smith and Jane Doe. 2020. Title of Paper. In Proceedings.",
        "[Johnson et al.(2019)] Bob Johnson. 2019. Another Paper. In Conference Proceedings.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    
    # Check with IEEE style - should detect author citations
    warns = checker.check_file(str(pdf_path), style="ieee")
    assert any("author citations" in w.lower() for w in warns), f"Expected author citations warning, got: {warns}"


def test_numeric_reference_format(tmp_path, monkeypatch):
    """Test that numeric citations don't trigger warning with IEEE style."""
    pdf_path = tmp_path / "numeric_refs.pdf"
    texts = [
        "Title and Introduction with refs [1][2]",
        "Some content here",
        "More content",
        "REFERENCES",
        "[1] John Smith and Jane Doe. 2020. Title of Paper. In Proceedings.",
        "[2] Bob Johnson. 2019. Another Paper. In Conference Proceedings.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    
    # Check with IEEE style - should NOT warn about citations format
    warns = checker.check_file(str(pdf_path), style="ieee")
    assert not any("author citations" in w.lower() or "numeric" in w.lower() for w in warns), f"Unexpected citation warning for numeric refs: {warns}"


