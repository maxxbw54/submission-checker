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


def test_our_previous_work_detection(tmp_path, monkeypatch):
    """Test detection of 'Our previous work' phrase in Conclusion section."""
    pdf_path = tmp_path / "self_citation.pdf"
    texts = [
        "Title and Introduction",
        "Related Work and Methods",
        "Evaluation Results Here",
        "Conclusion: Our previous work demonstrated a reference architecture to present an overview of agent design [27], while this work extends it.",
        "REFERENCES",
        "[27] Smith, J., et al. 2023. A Reference Architecture...",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    
    # Check - should detect suspicious phrase "our previous work"
    warns = checker.check_file(str(pdf_path))
    assert any("Suspicious wording" in w and "our previous work" in w.lower() for w in warns), f"Expected 'our previous work' detection, got: {warns}"


def test_anonymous_placeholder_email_not_flagged(tmp_path, monkeypatch):
    """Ensure common anonymized placeholder email does not trigger warning."""
    pdf_path = tmp_path / "anonymous_email.pdf"
    texts = [
        "Title\nAnonymous submission\nanonymous@example.com",
        "Body text",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path))
    assert not any("Non-anonymous email" in w for w in warns), f"Unexpected email warning: {warns}"


def test_font_decrease_detected_for_real_body_shrink(tmp_path, monkeypatch):
    pdf_path = tmp_path / "font_drop.pdf"
    texts = ["p1", "p2", "p3", "p4", "References"]
    make_pdf(texts, pdf_path)

    # Page 4 genuinely shrinks from ~9.6 to ~8.1.
    page_samples = [
        [9.6] * 120,
        [9.7] * 120,
        [9.5] * 120,
        [8.1] * 120,
        [8.1] * 60,
    ]

    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    monkeypatch.setattr(checker, "extract_font_size_samples_per_page", lambda p: page_samples)

    warns = checker.check_file(str(pdf_path), main_pages=10)
    assert any("Font size decreases" in w for w in warns), f"Expected font-size warning, got: {warns}"


def test_font_decrease_not_flagged_for_figure_heavy_page(tmp_path, monkeypatch):
    pdf_path = tmp_path / "figure_heavy.pdf"
    texts = ["p1", "p2", "p3", "p4", "References"]
    make_pdf(texts, pdf_path)

    # Page 4 has many small labels but still enough baseline-sized body text.
    page_samples = [
        [9.6] * 120,
        [9.7] * 120,
        [9.5] * 120,
        [6.2] * 240 + [9.6] * 24,
        [9.6] * 60,
    ]

    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    monkeypatch.setattr(checker, "extract_font_size_samples_per_page", lambda p: page_samples)

    warns = checker.check_file(str(pdf_path), main_pages=10)
    assert not any("Font size decreases" in w for w in warns), f"Unexpected font-size warning: {warns}"


def test_font_decrease_not_flagged_for_short_table_page(tmp_path, monkeypatch):
    pdf_path = tmp_path / "table_page.pdf"
    texts = [
        "Body text " * 600,
        "Body text " * 600,
        "Body text " * 600,
        "TABLE II Benchmarks N speedup values",  # short, table-heavy page
        "Body text " * 600,
        "References",
    ]
    make_pdf(texts, pdf_path)

    page_samples = [
        [9.96] * 200,
        [9.96] * 200,
        [9.96] * 200,
        [7.97] * 220,
        [9.96] * 200,
        [7.97] * 50,
    ]

    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    monkeypatch.setattr(checker, "extract_font_size_samples_per_page", lambda p: page_samples)

    warns = checker.check_file(str(pdf_path), main_pages=10)
    assert not any("Font size decreases" in w for w in warns), f"Unexpected font-size warning: {warns}"


def test_font_decrease_not_flagged_for_recurring_alternate_baseline_size(tmp_path, monkeypatch):
    pdf_path = tmp_path / "alternate_baseline_size.pdf"
    texts = ["Body text " * 700] * 6
    make_pdf(texts, pdf_path)

    # First 3 pages establish two recurring text-size buckets (10pt and 8pt).
    # Later pages using the 8pt bucket should not be treated as a new decrease.
    page_samples = [
        [9.96] * 120 + [7.97] * 40,
        [9.96] * 80 + [7.97] * 80,
        [9.96] * 110 + [7.97] * 30,
        [7.97] * 140,
        [9.96] * 140,
        [7.97] * 120,
    ]

    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    monkeypatch.setattr(checker, "extract_font_size_samples_per_page", lambda p: page_samples)

    warns = checker.check_file(str(pdf_path), main_pages=10)
    assert not any("Font size decreases" in w for w in warns), f"Unexpected font-size warning: {warns}"


