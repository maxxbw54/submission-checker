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


def test_check_file_retries_transient_empty_extraction(tmp_path, monkeypatch):
    pdf_path = tmp_path / "transient_empty.pdf"
    make_pdf(["page1"], pdf_path)

    calls = {"count": 0}

    def fake_extract_with_timeout(_path, timeout=10):
        calls["count"] += 1
        if calls["count"] == 1:
            return []
        return ["Body text on retry"]

    monkeypatch.setattr(checker, "extract_text_with_timeout", fake_extract_with_timeout)

    warns = checker.check_file(str(pdf_path), max_pages=2)
    assert warns == [], f"Expected no warnings after retry succeeds, got: {warns}"
    assert calls["count"] == 2


def test_check_folder_parallel_preserves_order_and_counts(tmp_path, monkeypatch):
    folder = tmp_path / "papers"
    folder.mkdir()
    nested = folder / "nested"
    nested.mkdir()

    file_a = folder / "paperA.pdf"
    file_b = nested / "paperB.pdf"
    file_c = folder / "paperC.pdf"
    for path in (file_a, file_b, file_c):
        path.write_bytes(b"%PDF-1.4\n%EOF\n")

    def fake_check_file(file_path, **kwargs):
        name = Path(file_path).name
        if name == "paperB.pdf":
            return ["warning"]
        return []

    monkeypatch.setattr(checker, "check_file", fake_check_file)

    result = checker.check_folder(str(folder), workers=3)

    assert result["passed"] == 2
    assert result["failed"] == 1
    assert [filename for filename, _ in result["results"]] == [
        "nested/paperB.pdf",
        "paperA.pdf",
        "paperC.pdf",
    ]


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
    assert any("Figures/tables appear" in w for w in warns)


def test_appendix_outside_main_pages_detected_without_references(tmp_path, monkeypatch):
    pdf_path = tmp_path / "appendix_after_main_no_refs.pdf"
    texts = [
        "Body page 1",
        "Body page 2",
        "Body page 3",
        "Appendix A: Extra material",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), main_pages=3)
    assert any("Appendix content appears outside main pages" in w for w in warns), f"Expected appendix warning, got: {warns}"


def test_appendix_within_main_pages_not_flagged(tmp_path, monkeypatch):
    pdf_path = tmp_path / "appendix_within_main.pdf"
    texts = [
        "Body page 1",
        "Appendix A: Embedded section",
        "References",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), main_pages=3)
    assert not any("Appendix content appears outside main pages" in w for w in warns), f"Unexpected appendix warning: {warns}"


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
    assert not any("non-standard references format" in w.lower() for w in warns), f"Unexpected non-standard format warning: {warns}"


def test_nonstandard_reference_bullet_points(tmp_path, monkeypatch):
    pdf_path = tmp_path / "bullet_refs.pdf"
    texts = [
        "Intro text",
        "REFERENCES",
        "- John Smith, Great Paper, ICSE, 2024",
        "* Jane Doe, Another Paper, FSE, 2023",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path))
    assert any("Non-standard references format detected" in w and "bullet points" in w for w in warns), f"Expected bullet-points warning, got: {warns}"


def test_nonstandard_reference_plain_numbering(tmp_path, monkeypatch):
    pdf_path = tmp_path / "plain_number_refs.pdf"
    texts = [
        "Intro text",
        "References",
        "1. John Smith, Great Paper, ICSE, 2024",
        "2) Jane Doe, Another Paper, FSE, 2023",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path))
    assert any(
        "Non-standard references format detected" in w
        and "plain numbering without brackets" in w
        for w in warns
    ), f"Expected plain-numbering warning, got: {warns}"


def test_nonstandard_reference_author_year(tmp_path, monkeypatch):
    pdf_path = tmp_path / "author_year_refs.pdf"
    texts = [
        "Intro text",
        "References",
        "Smith et al. (2020) Great Paper. ICSE.",
        "Johnson (2019) Another Paper. FSE.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path))
    assert any("Non-standard references format detected" in w and "author-year citations" in w for w in warns), f"Expected author-year warning, got: {warns}"


def test_reference_continuation_lines_not_misclassified(tmp_path, monkeypatch):
    pdf_path = tmp_path / "continuation_lines.pdf"
    texts = [
        "Intro text",
        "References",
        "[1] A. Author, \"Paper Title,\" Venue, 2024.",
        "[Online]. Available: https://example.com/path/2024/",
        "30. [Online]. Available: https://example.com/reference/linewrap",
        "[2] B. Author, \"Another Title,\" Venue, 2023.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), style="ieee")
    assert not any("non-standard references format" in w.lower() for w in warns), f"Unexpected non-standard warning: {warns}"
    assert not any("mix numeric and author citations" in w.lower() for w in warns), f"Unexpected mixed-format warning: {warns}"


def test_reference_year_prefixed_continuation_not_plain_numbering(tmp_path, monkeypatch):
    pdf_path = tmp_path / "year_prefixed_continuation.pdf"
    texts = [
        "Intro text",
        "References",
        "[1] A. Author, \"Challenge title,\" Venue,",
        "2025: Satellite configuration, challenge description",
        "[2] B. Author, \"Another paper,\" Venue, 2024.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), style="ieee")
    assert not any("non-standard references format" in w.lower() for w in warns), f"Unexpected non-standard warning: {warns}"


def test_single_stray_plain_number_fragment_not_flagged(tmp_path, monkeypatch):
    pdf_path = tmp_path / "stray_plain_number_fragment.pdf"
    texts = [
        "Intro text",
        "References",
        "[1] A. Author, \"A Study of Something,\" in Proceedings of the",
        "1: Long Papers), M. Liakata, V. P. Moreira, J. Zhang, and",
        "[2] B. Author, \"Another Study,\" Venue, 2024.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), style="ieee")
    assert not any("non-standard references format" in w.lower() for w in warns), f"Unexpected non-standard warning: {warns}"


def test_numbered_list_before_references_header_not_counted(tmp_path, monkeypatch):
    pdf_path = tmp_path / "pre_header_numbered_list.pdf"
    texts = [
        "Body page",
        "Some content",
        "2) Result: Figure 7 shows the results",
        "3) Answer to RQ: additional discussion",
        "References",
        "[1] A. Author, \"Paper One,\" Venue, 2024.",
        "[2] B. Author, \"Paper Two,\" Venue, 2023.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), style="ieee")
    assert not any("non-standard references format" in w.lower() for w in warns), f"Unexpected non-standard warning: {warns}"


def test_non_header_references_mention_not_treated_as_references_start(tmp_path, monkeypatch):
    pdf_path = tmp_path / "references_mention_not_header.pdf"
    texts = [
        "References to prior work are discussed in this section.",
        "More body text",
        "References",
        "[1] A. Author, \"Paper One,\" Venue, 2024.",
        "[2] B. Author, \"Paper Two,\" Venue, 2023.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), style="ieee")
    assert not any("non-standard references format" in w.lower() for w in warns), f"Unexpected non-standard warning: {warns}"


def test_references_header_with_page_number_suffix_is_detected(tmp_path, monkeypatch):
    pdf_path = tmp_path / "references_suffix.pdf"
    texts = [
        "Body text page 1",
        "Body text page 2",
        "Body text page 3",
        "Body text page 4",
        "Body text page 5",
        "Body text page 6",
        "Body text page 7",
        "Body text page 8",
        "Body text page 9",
        "Body text page 10",
        "Body text page 11",
        "Conclusion text\nREFERENCES876\n[1] A. Author, \"Paper One,\" Venue, 2024.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), main_pages=10)
    assert not any("No references section found" in w for w in warns), f"Unexpected missing-references warning: {warns}"
    assert any("references must start no later than page 11" in w.lower() for w in warns), f"Expected late-references warning, got: {warns}"


def test_single_hyphen_reference_continuation_not_bullet_list(tmp_path, monkeypatch):
    pdf_path = tmp_path / "single_hyphen_continuation.pdf"
    texts = [
        "Body text",
        "References",
        "[1] A. Author, \"Security and Privacy",
        "- A Review,\" Venue, 2018.",
        "[2] B. Author, \"Another paper,\" Venue, 2024.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), style="ieee")
    assert not any("non-standard references format" in w.lower() for w in warns), f"Unexpected non-standard warning: {warns}"


def test_pre_reference_numbered_lists_not_counted_when_numeric_refs_exist(tmp_path, monkeypatch):
    pdf_path = tmp_path / "pre_reference_numbered_lists.pdf"
    texts = [
        "Body text",
        "References",
        "1) External environment graph: description",
        "2) Repository dependency graph: description",
        "[1] A. Author, \"Primary Reference,\" Venue, 2024.",
        "[2] B. Author, \"Another Reference,\" Venue, 2023.",
        "1: Long Papers), 2025, pp. 100-110.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), style="ieee")
    assert not any("non-standard references format" in w.lower() for w in warns), f"Unexpected non-standard warning: {warns}"


def test_two_dash_continuation_lines_not_bullet_list_in_numeric_refs(tmp_path, monkeypatch):
    pdf_path = tmp_path / "dash_continuations_numeric_refs.pdf"
    texts = [
        "Body text",
        "References",
        "[1] A. Author, \"A Topic,\" in Proceedings of the International Conference",
        "- Software Engineering for AI, CAIN 2024, Lisbon, Portugal,",
        "[2] B. Author, \"Another Topic,\" IEEE, 2025.",
        "- May 6, 2025. IEEE, 2025, pp. 1628-1639.",
        "[3] C. Author, \"Third Topic,\" Venue, 2022.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), style="ieee")
    assert not any("non-standard references format" in w.lower() for w in warns), f"Unexpected non-standard warning: {warns}"


def test_dash_continuations_and_large_volume_prefix_not_nonstandard(tmp_path, monkeypatch):
    pdf_path = tmp_path / "dash_continuations_large_volume.pdf"
    texts = [
        "Body text",
        "References",
        "[1] J. Author, \"Towards\"",
        "- Workshops - XP 2020 Workshops, Copenhagen, Denmark, June",
        "[2] N. Author, \"Another title,\" Venue, 2021.",
        "11915. Springer, 2019, pp. 417-432.",
        "[3] C. Author, \"Third title,\" Venue, 2022.",
    ]
    make_pdf(texts, pdf_path)
    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)

    warns = checker.check_file(str(pdf_path), style="ieee")
    assert not any("non-standard references format" in w.lower() for w in warns), f"Unexpected non-standard warning: {warns}"


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
        [9.96] * 50,
    ]

    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    monkeypatch.setattr(checker, "extract_font_size_samples_per_page", lambda p: page_samples)

    warns = checker.check_file(str(pdf_path), main_pages=10)
    assert not any("Font size decreases" in w for w in warns), f"Unexpected font-size warning: {warns}"


def test_font_decrease_detected_in_references_section(tmp_path, monkeypatch):
    pdf_path = tmp_path / "refs_font_drop.pdf"
    texts = [
        "Body text " * 600,
        "Body text " * 600,
        "Body text " * 600,
        "Body text " * 600,
        "References",
        "[1] Ref entry",
    ]
    make_pdf(texts, pdf_path)

    page_samples = [
        [10.0] * 200,
        [10.0] * 200,
        [10.0] * 200,
        [10.0] * 200,
        [8.0] * 200,
        [6.5] * 120,
    ]

    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    monkeypatch.setattr(checker, "extract_font_size_samples_per_page", lambda p: page_samples)

    warns = checker.check_file(str(pdf_path), main_pages=10, check_reference_font_size=True)
    assert any("Font size decreases in references" in w for w in warns), f"Expected references font-size warning, got: {warns}"


def test_font_decrease_not_flagged_for_stable_smaller_references(tmp_path, monkeypatch):
    pdf_path = tmp_path / "refs_stable_8pt.pdf"
    texts = [
        "Body text " * 600,
        "Body text " * 600,
        "Body text " * 600,
        "Body text " * 600,
        "References",
        "[1] Ref entry",
    ]
    make_pdf(texts, pdf_path)
    page_samples = [
        [10.0] * 200,
        [10.0] * 200,
        [10.0] * 200,
        [10.0] * 200,
        [8.0] * 200,
        [8.0] * 120,
    ]

    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    monkeypatch.setattr(checker, "extract_font_size_samples_per_page", lambda p: page_samples)

    warns = checker.check_file(str(pdf_path), main_pages=10, check_reference_font_size=True)
    assert not any("Font size decreases in references" in w for w in warns), f"Unexpected references font-size warning: {warns}"


def test_ieee_reference_target_8pt_not_flagged_after_9pt_transition_page(tmp_path, monkeypatch):
    pdf_path = tmp_path / "ieee_refs_transition_page.pdf"
    texts = [
        "Body text " * 600,
        "Body text " * 600,
        "Body text " * 600,
        "Body text " * 600,
        "References",
        "[1] Ref entry",
    ]
    make_pdf(texts, pdf_path)

    # The first references page can still estimate high because it mixes the
    # tail of the body section with the references header. IEEE references that
    # settle at ~8pt on the following page should not be flagged.
    page_samples = [
        [10.0] * 200,
        [10.0] * 200,
        [10.0] * 200,
        [10.0] * 200,
        [9.0] * 200,
        [8.0] * 120,
    ]

    monkeypatch.setattr(checker, "extract_text_with_timeout", lambda p, timeout=10: texts)
    monkeypatch.setattr(checker, "extract_font_size_samples_per_page", lambda p: page_samples)

    warns = checker.check_file(str(pdf_path), main_pages=10, style="ieee", check_reference_font_size=True)
    assert not any("Font size decreases in references" in w for w in warns), f"Unexpected IEEE references font-size warning: {warns}"


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


