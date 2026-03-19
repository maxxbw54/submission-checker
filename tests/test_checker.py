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
