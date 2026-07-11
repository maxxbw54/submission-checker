"""Entry point for submission checker CLI."""
import re
import sys
import csv
import os
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
from collections import Counter
from statistics import median

from pypdf import PdfReader

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Generic institutional emails that should not be flagged as anonymity issues
ALLOWED_EMAILS = {
    "authors@institutions.edu",
    "authors@instituitons.edu",
    "email@email.email",
    "anonymous@example.com",
}
SUSPICIOUS_PHRASES = [r"our previous work", r"our previous paper", r"in our previous work"]
REFERENCES_HEADER = re.compile(r"^references?\s*:?\s*$", flags=re.IGNORECASE)
APPENDIX_HEADER = re.compile(
    r"^\s*(appendix|appendices)\b(?:\s+[A-Z0-9]+)?(?:\s*[:.-]\s*.*)?\s*$",
    flags=re.IGNORECASE,
)
STYLE_KEYWORDS = {
    "acm": [r"acm", r"association for computing machinery"],
    "ieee": [r"ieee", r"institute of electrical and electronics engineers"],
}
IEEE_BODY_FONT_TARGET = 10.0
IEEE_REFERENCE_FONT_TARGET = 8.0
IEEE_FONT_TARGET_TOLERANCE = 0.75
IEEE_PAGE_WIDTH_PT = 612.0
IEEE_COLUMN_WIDTH_PT = 252.0
IEEE_COLUMN_GAP_PT = 12.0
IEEE_TEXT_WIDTH_PT = IEEE_COLUMN_WIDTH_PT * 2 + IEEE_COLUMN_GAP_PT
IEEE_COLUMN_WIDTH_RATIO = IEEE_COLUMN_WIDTH_PT / IEEE_PAGE_WIDTH_PT
IEEE_COLUMN_GAP_RATIO = IEEE_COLUMN_GAP_PT / IEEE_PAGE_WIDTH_PT
IEEE_TEXT_WIDTH_RATIO = IEEE_TEXT_WIDTH_PT / IEEE_PAGE_WIDTH_PT


from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from concurrent.futures import as_completed


def extract_text_per_page(pdf_path: Path) -> List[str]:
    try:
        reader = PdfReader(str(pdf_path))
        texts = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append("")
        return texts
    except Exception:
        # Return empty list if PDF can't be read
        return []


def extract_text_with_timeout(pdf_path: Path, timeout: int = 10) -> List[str]:
    """Attempt to extract text with a hard timeout to avoid hanging on slow network drives.

    This uses a background thread with a timeout so the main process does not hang on
    slow or locked network files. If extraction does not complete in time, we return
    an empty list to signal failure.
    """
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(extract_text_per_page, pdf_path)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        return []
    except Exception:
        return []
    finally:
        # Do not block caller on shutdown if the worker is stuck in PDF parsing.
        executor.shutdown(wait=False, cancel_futures=True)


def get_metadata(pdf_path: Path) -> dict:
    try:
        reader = PdfReader(str(pdf_path))
        return reader.metadata or {}
    except Exception:
        return {}


def extract_font_size_samples_per_page(pdf_path: Path) -> List[List[float]]:
    """Extract raw font-size samples for each page from PDF content streams.

    Returns one list per page. A page list is empty when no font sizes can be
    extracted.
    """
    try:
        reader = PdfReader(str(pdf_path))
        page_samples: List[List[float]] = []
        
        for page in reader.pages:
            try:
                page_font_sizes: List[float] = []
                
                # Access the content stream which contains font operations
                if "/Contents" in page:
                    content = page["/Contents"]
                    if content:
                        try:
                            # Get the raw data from the content stream
                            from pypdf.generic import IndirectObject, ArrayObject
                            
                            if isinstance(content, IndirectObject):
                                content_data = content.get_object()
                            else:
                                content_data = content
                            
                            if hasattr(content_data, 'get_data'):
                                raw_data = content_data.get_data().decode('latin-1', errors='ignore')
                            elif isinstance(content_data, ArrayObject):
                                # Multiple content streams
                                raw_data = ""
                                for item in content_data:
                                    if isinstance(item, IndirectObject):
                                        obj = item.get_object()
                                        if hasattr(obj, 'get_data'):
                                            raw_data += obj.get_data().decode('latin-1', errors='ignore')
                            else:
                                raw_data = str(content_data)
                            
                            # Look for font size operations: Tf operator sets font and size
                            # Format is: /FontName FontSize Tf
                            # Extract numbers that appear before Tf operator
                            import re
                            tf_pattern = r'([\d.]+)\s+Tf'
                            matches = re.findall(tf_pattern, raw_data)
                            if matches:
                                page_font_sizes = [float(m) for m in matches]
                        except Exception:
                            pass
                
                page_samples.append(page_font_sizes)
                    
            except Exception:
                page_samples.append([])
        
        return page_samples
    except Exception:
        return []


def _estimate_body_font_size(page_font_sizes: List[float]) -> Optional[float]:
    """Estimate the dominant body font size on one page.

    We intentionally avoid a plain average because figure labels and footnotes
    can dominate counts on some pages and cause false positives.
    """
    # Ignore implausible values and very tiny decorative text.
    valid = [s for s in page_font_sizes if 5.0 <= s <= 20.0]
    if not valid:
        return None

    # Bucket to reduce tiny encoding jitter and pick the most frequent size.
    buckets = [round(s, 1) for s in valid]
    counts = Counter(buckets)
    dominant_bucket, _ = max(counts.items(), key=lambda item: (item[1], item[0]))

    # Use a tight neighborhood around the dominant bucket.
    neighborhood = [s for s in valid if abs(s - dominant_bucket) <= 0.4]
    if len(neighborhood) >= 5:
        return median(neighborhood)
    return median(valid)


def _estimate_typical_document_font(font_sizes: List[Optional[float]], page_limit: int) -> Optional[float]:
    """Estimate the typical body font across the main content pages.

    This gives spacing checks a stable fallback when a table-heavy page causes
    the page-local dominant font to jump away from the manuscript's actual body
    text size.
    """
    candidates = [
        font_size
        for font_size in font_sizes[:page_limit]
        if font_size is not None and 6.0 <= font_size <= 14.0
    ]
    if not candidates:
        return None
    return median(candidates)


def _matches_expected_font_target(
    page_size: float,
    expected_size: Optional[float],
    tolerance: float = IEEE_FONT_TARGET_TOLERANCE,
) -> bool:
    if expected_size is None:
        return False
    return abs(page_size - expected_size) <= tolerance


def _is_references_header_line(line: str) -> bool:
    """Match references headers even when PDF extraction appends a page number suffix."""
    candidate = line.strip()
    if not candidate:
        return False

    # Some extractors merge the page number into the heading, e.g. "REFERENCES876".
    candidate = re.sub(r"\s*\d+\s*$", "", candidate)
    candidate = re.sub(r"[\s:;,.]+$", "", candidate)
    return bool(REFERENCES_HEADER.match(candidate))


def extract_font_sizes_per_page(pdf_path: Path) -> List[Optional[float]]:
    """Extract estimated body font size for each page.

    Returns one value per page or None when no size can be determined.
    """
    page_samples = extract_font_size_samples_per_page(pdf_path)
    if not page_samples:
        return []

    return [_estimate_body_font_size(samples) for samples in page_samples]


def extract_line_metrics_per_page(pdf_path: Path) -> List[List[Dict[str, Any]]]:
    """Extract approximate per-line layout metrics from each page.

    Each line entry contains the merged text, its vertical position, and a
    representative font size. We use this to detect suspiciously compressed
    line spacing in IEEE submissions.
    """
    try:
        reader = PdfReader(str(pdf_path))
        pages: List[List[Dict[str, Any]]] = []

        for page in reader.pages:
            fragments: List[Dict[str, Any]] = []

            def visitor_text(text, cm, tm, font_dict, font_size):
                stripped = (text or "").strip()
                if not stripped:
                    return

                try:
                    y_pos = float(tm[5])
                except Exception:
                    return

                try:
                    x_pos = float(tm[4])
                except Exception:
                    x_pos = 0.0

                try:
                    size = float(font_size)
                except Exception:
                    size = 0.0

                estimated_advance = max(8.0, len(stripped) * max(size, 8.0) * 0.45)

                fragments.append({
                    "text": stripped,
                    "x": x_pos,
                    "end_x": x_pos + estimated_advance,
                    "y": y_pos,
                    "font_size": size,
                })

            try:
                page.extract_text(visitor_text=visitor_text)
            except Exception:
                pages.append([])
                continue

            if not fragments:
                pages.append([])
                continue

            lines_by_band: Dict[float, List[Dict[str, Any]]] = {}
            for fragment in fragments:
                band = round(fragment["y"] / 1.5) * 1.5
                lines_by_band.setdefault(band, []).append(fragment)

            page_lines: List[Dict[str, Any]] = []
            for band, band_fragments in lines_by_band.items():
                ordered = sorted(band_fragments, key=lambda item: (item["x"], item["text"]))
                text = " ".join(fragment["text"] for fragment in ordered).strip()
                font_sizes = [fragment["font_size"] for fragment in ordered if fragment["font_size"] > 0]
                x_positions = [fragment["x"] for fragment in ordered]
                end_positions = [fragment.get("end_x", fragment["x"]) for fragment in ordered]
                if not text:
                    continue
                page_lines.append({
                    "text": text,
                    "y": band,
                    "font_size": median(font_sizes) if font_sizes else 0.0,
                    "min_x": min(x_positions) if x_positions else 0.0,
                    "max_x": max(end_positions) if end_positions else 0.0,
                })

            pages.append(sorted(page_lines, key=lambda item: item["y"], reverse=True))

        return pages
    except Exception:
        return []


def extract_text_blocks_per_page(pdf_path: Path) -> List[List[Dict[str, Any]]]:
    """Extract approximate text blocks per page with x/y spans.

    Blocks are built from text fragments that share a similar baseline and are
    horizontally close enough to belong to the same visual line segment. This
    lets us distinguish wide single-column prose from two-column layouts.
    """
    try:
        reader = PdfReader(str(pdf_path))
        pages: List[List[Dict[str, Any]]] = []

        for page in reader.pages:
            fragments: List[Dict[str, Any]] = []

            def visitor_text(text, cm, tm, font_dict, font_size):
                stripped = (text or "").strip()
                if not stripped:
                    return

                try:
                    y_pos = float(tm[5])
                except Exception:
                    return

                try:
                    x_pos = float(tm[4])
                except Exception:
                    x_pos = 0.0

                try:
                    size = float(font_size)
                except Exception:
                    size = 0.0

                estimated_advance = max(8.0, len(stripped) * max(size, 8.0) * 0.45)

                fragments.append({
                    "text": stripped,
                    "x": x_pos,
                    "end_x": x_pos + estimated_advance,
                    "y": y_pos,
                    "font_size": size,
                })

            try:
                page.extract_text(visitor_text=visitor_text)
            except Exception:
                pages.append([])
                continue

            if not fragments:
                pages.append([])
                continue

            lines_by_band: Dict[float, List[Dict[str, Any]]] = {}
            for fragment in fragments:
                band = round(fragment["y"] / 1.5) * 1.5
                lines_by_band.setdefault(band, []).append(fragment)

            page_blocks: List[Dict[str, Any]] = []
            for band, band_fragments in lines_by_band.items():
                ordered = sorted(band_fragments, key=lambda item: (item["x"], item["text"]))
                clusters: List[List[Dict[str, Any]]] = []
                current_cluster: List[Dict[str, Any]] = []
                previous_x: Optional[float] = None

                for fragment in ordered:
                    if previous_x is not None and fragment["x"] - previous_x > 80.0 and current_cluster:
                        clusters.append(current_cluster)
                        current_cluster = []
                    current_cluster.append(fragment)
                    previous_x = fragment["x"]

                if current_cluster:
                    clusters.append(current_cluster)

                for cluster in clusters:
                    text = " ".join(fragment["text"] for fragment in cluster).strip()
                    if not text:
                        continue

                    x_positions = [fragment["x"] for fragment in cluster]
                    end_positions = [fragment.get("end_x", fragment["x"]) for fragment in cluster]
                    font_sizes = [fragment["font_size"] for fragment in cluster if fragment["font_size"] > 0]
                    page_blocks.append({
                        "text": text,
                        "y": band,
                        "min_x": min(x_positions),
                        "max_x": max(end_positions),
                        "font_size": median(font_sizes) if font_sizes else 0.0,
                    })

            pages.append(sorted(page_blocks, key=lambda item: (item["y"], item["min_x"]), reverse=True))

        return pages
    except Exception:
        return []


def extract_page_widths(pdf_path: Path) -> List[float]:
    try:
        reader = PdfReader(str(pdf_path))
        widths: List[float] = []
        for page in reader.pages:
            try:
                widths.append(float(page.mediabox.width))
            except Exception:
                widths.append(0.0)
        return widths
    except Exception:
        return []


def _is_heading_line(text: str, font_size: float, body_font_size: Optional[float]) -> bool:
    stripped = text.strip()
    if not stripped:
        return False

    if re.search(r"[•,:;]", stripped):
        return False

    if re.match(r"^(references?|table|figure|fig\.|appendix)\b", stripped, flags=re.IGNORECASE):
        return False

    if len(stripped) <= 80 and re.match(
        r"^(?:\d+(?:\.\d+)*|[IVXLCM]+|[A-Z])\.?(?:\s+[A-Z][A-Za-z0-9\-]*){1,10}$",
        stripped,
    ):
        return True

    if (
        body_font_size is not None
        and font_size >= body_font_size * 1.18
        and len(stripped) <= 80
        and re.match(r"^[A-Z][A-Za-z0-9\-]+(?:\s+(?:[A-Z][A-Za-z0-9\-]+|and|or|of|to|for|with|in|on)){0,9}$", stripped)
    ):
        return True

    return False


def _is_strong_heading_line(text: str, font_size: float, body_font_size: Optional[float]) -> bool:
    """Conservative heading classifier used by spacing heuristics.

    PDF extraction from tables and diagrams often yields short label fragments
    that look like headings (e.g., "Control", "Access", "KU-3"). This helper
    keeps the heading detector stricter for spacing checks to reduce
    false-positive warnings.
    """
    if not _is_heading_line(text, font_size, body_font_size):
        return False

    # Reject obviously corrupted extraction sizes that often come from tables
    # and vector labels rather than actual section headings.
    if font_size <= 0 or font_size > 40.0:
        return False

    compact = " ".join(text.split())
    numbered_section = bool(
        re.match(r"^(?:\d+(?:\.\d+)*|[IVXLCM]+)\.?\s+[A-Z][A-Za-z0-9\-]{3,}", compact)
    )
    if len(compact) < 12 and not numbered_section:
        return False

    if re.search(r"\b(fig\.|figure|table)\b", compact, flags=re.IGNORECASE):
        return False

    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]*", compact)
    if len(words) < 2 and not numbered_section:
        return False

    # Keep heading-size tolerance narrow enough to reject large diagram labels
    # while still allowing common IEEE section heading sizes.
    if body_font_size is not None:
        if font_size < body_font_size * 0.92:
            return False
        if font_size > body_font_size * 1.35:
            return False
    elif font_size > 24.0:
        return False

    return True


def _is_body_text_line(text: str) -> bool:
    compact = " ".join(text.split())
    if len(compact) < 25:
        return False
    if not re.search(r"[A-Za-z]", compact):
        return False
    if re.match(r"^(references?|figure|fig\.|table|appendix)\b", compact, flags=re.IGNORECASE):
        return False
    return True


def _is_spacing_body_line(
    text: str,
    font_size: float,
    body_font_size: Optional[float],
    page_width: float,
    min_x: float,
    max_x: float,
) -> bool:
    """Filter line candidates down to prose-like body text for spacing checks."""
    if not _is_body_text_line(text):
        return False

    compact = " ".join(text.split())
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]*", compact)
    if len(words) < 5:
        return False

    if not re.search(r"[a-z]", compact):
        return False

    line_width = max_x - min_x
    if page_width > 0 and max_x > min_x:
        if line_width < max(60.0, page_width * 0.10):
            return False

    if body_font_size is not None and abs(font_size - body_font_size) > 1.0:
        return False

    return True


def check_ieee_line_spacing(
    pdf_path: Path,
    main_pages_limit: int = 10,
    references_page: Optional[int] = None,
    page_texts: Optional[List[str]] = None,
) -> Optional[str]:
    """Detect suspiciously compressed line spacing in IEEE manuscripts."""

    def _split_lines_for_spacing(
        lines: List[Dict[str, Any]],
        page_width: float,
    ) -> List[List[Dict[str, Any]]]:
        if not lines:
            return []

        if page_width <= 0:
            return [sorted(lines, key=lambda item: item["y"], reverse=True)]

        has_geometry = all("min_x" in line and "max_x" in line for line in lines)
        if not has_geometry:
            return [sorted(lines, key=lambda item: item["y"], reverse=True)]

        center = page_width / 2.0
        gutter = 15.0

        left: List[Dict[str, Any]] = []
        right: List[Dict[str, Any]] = []
        spanning: List[Dict[str, Any]] = []

        for line in lines:
            min_x = float(line.get("min_x", 0.0))
            max_x = float(line.get("max_x", min_x))
            if max_x <= center - gutter:
                left.append(line)
            elif min_x >= center + gutter:
                right.append(line)
            else:
                spanning.append(line)

        # Treat sparse pages as two-column as long as both columns are represented.
        if len(left) >= 2 and len(right) >= 2:
            groups: List[List[Dict[str, Any]]] = []
            if len(left) >= 2:
                groups.append(sorted(left, key=lambda item: item["y"], reverse=True))
            if len(right) >= 2:
                groups.append(sorted(right, key=lambda item: item["y"], reverse=True))
            return groups

        return [sorted(lines, key=lambda item: item["y"], reverse=True)]

    try:
        line_metrics = extract_line_metrics_per_page(pdf_path)
        font_sizes = extract_font_sizes_per_page(pdf_path)
        page_widths = extract_page_widths(pdf_path)
        if not line_metrics:
            return None

        check_until_page = main_pages_limit
        if references_page is not None and references_page > 1:
            check_until_page = min(check_until_page, references_page - 1)

        typical_body_font = _estimate_typical_document_font(font_sizes, check_until_page)

        def _spacing_body_font(page_font: Optional[float]) -> Optional[float]:
            if page_font is None:
                return typical_body_font
            if typical_body_font is None:
                return page_font
            if page_font < typical_body_font * 0.85 or page_font > typical_body_font * 1.15:
                return typical_body_font
            return page_font

        baseline_gaps: List[float] = []
        baseline_pages = min(3, len(line_metrics), check_until_page)
        for page_idx in range(baseline_pages):
            lines = line_metrics[page_idx]
            body_font_size = _spacing_body_font(font_sizes[page_idx] if page_idx < len(font_sizes) else None)
            page_width = page_widths[page_idx] if page_idx < len(page_widths) else 0.0
            for group in _split_lines_for_spacing(lines, page_width):
                for current, following in zip(group, group[1:]):
                    if not (
                        _is_spacing_body_line(
                            current["text"],
                            current["font_size"],
                            body_font_size,
                            page_width,
                            current.get("min_x", 0.0),
                            current.get("max_x", current.get("min_x", 0.0)),
                        )
                        and _is_spacing_body_line(
                            following["text"],
                            following["font_size"],
                            body_font_size,
                            page_width,
                            following.get("min_x", 0.0),
                            following.get("max_x", following.get("min_x", 0.0)),
                        )
                    ):
                        continue
                    gap = current["y"] - following["y"]
                    if 6.0 <= gap <= 24.0:
                        baseline_gaps.append(gap)

        if len(baseline_gaps) < 4:
            return None

        baseline_gap = median(baseline_gaps)
        compressed_pages: List[int] = []
        heading_hits_by_page: Dict[int, int] = {}
        heading_min_gap_by_page: Dict[int, float] = {}

        for page_idx in range(baseline_pages, min(check_until_page, len(line_metrics))):
            if page_texts is not None and page_idx < len(page_texts) and not _is_body_like_page(page_texts[page_idx]):
                continue

            lines = line_metrics[page_idx]
            body_font_size = _spacing_body_font(font_sizes[page_idx] if page_idx < len(font_sizes) else None)
            body_gaps: List[float] = []
            page_width = page_widths[page_idx] if page_idx < len(page_widths) else 0.0

            for group in _split_lines_for_spacing(lines, page_width):
                for current, following in zip(group, group[1:]):
                    gap = current["y"] - following["y"]
                    if gap <= 0:
                        continue

                    if _is_strong_heading_line(current["text"], current["font_size"], body_font_size) and _is_spacing_body_line(
                        following["text"],
                        following["font_size"],
                        body_font_size,
                        page_width,
                        following.get("min_x", 0.0),
                        following.get("max_x", following.get("min_x", 0.0)),
                    ):
                        # Use a stricter threshold for heading transitions; many
                        # false positives come from extracted table/diagram labels.
                        if gap < baseline_gap * 0.68:
                            page_num = page_idx + 1
                            heading_hits_by_page[page_num] = heading_hits_by_page.get(page_num, 0) + 1
                            prev_min = heading_min_gap_by_page.get(page_num)
                            heading_min_gap_by_page[page_num] = gap if prev_min is None else min(prev_min, gap)

                    if not (
                        _is_spacing_body_line(
                            current["text"],
                            current["font_size"],
                            body_font_size,
                            page_width,
                            current.get("min_x", 0.0),
                            current.get("max_x", current.get("min_x", 0.0)),
                        )
                        and _is_spacing_body_line(
                            following["text"],
                            following["font_size"],
                            body_font_size,
                            page_width,
                            following.get("min_x", 0.0),
                            following.get("max_x", following.get("min_x", 0.0)),
                        )
                    ):
                        continue

                    if 4.0 <= gap <= 24.0:
                        body_gaps.append(gap)

            if len(body_gaps) >= 4:
                page_gap = median(body_gaps)
                compressed_count = sum(1 for gap in body_gaps if gap < baseline_gap * 0.84)
                if page_gap < baseline_gap * 0.87 and compressed_count >= max(3, len(body_gaps) // 2):
                    compressed_pages.append(page_idx + 1)

        all_warnings: List[str] = []
        
        # De-duplicate heading warnings by page. We only report a page when
        # there is repeated evidence or one very strong compression event.
        compressed_page_set = set(compressed_pages)
        heading_warning_pages = [
            page
            for page, count in heading_hits_by_page.items()
            if (
                count >= 3
                or (count >= 2 and heading_min_gap_by_page.get(page, baseline_gap) < baseline_gap * 0.65)
                or (
                    page in compressed_page_set
                    and count >= 1
                    and heading_min_gap_by_page.get(page, baseline_gap) < baseline_gap * 0.68
                )
            )
        ]
        if heading_warning_pages:
            pages = ", ".join(str(page) for page in sorted(heading_warning_pages)[:3])
            all_warnings.append(
                "Line spacing appears compressed near a heading "
                f"on page(s) {pages} (baseline {baseline_gap:.1f}pt)."
            )
        
        # Add compressed pages warning
        if compressed_pages:
            pages = ", ".join(str(page) for page in compressed_pages[:3])
            all_warnings.append(
                "Line spacing appears tighter than the IEEE baseline "
                f"on page(s) {pages} (baseline {baseline_gap:.1f}pt)."
            )

        if all_warnings:
            return " ".join(all_warnings)

        return None
    except Exception:
        return None


def check_ieee_column_layout(
    pdf_path: Path,
    main_pages_limit: int = 10,
    references_page: Optional[int] = None,
    page_texts: Optional[List[str]] = None,
) -> Optional[str]:
    """Detect suspicious IEEE two-column geometry changes.

    We flag pages that likely switched to single-column prose, materially
    changed column width, or reduced the gutter between columns.
    """

    def _percentile(values: List[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        q = min(1.0, max(0.0, q))
        pos = q * (len(ordered) - 1)
        lower = int(pos)
        upper = min(lower + 1, len(ordered) - 1)
        weight = pos - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    def _estimate_two_column_metrics(page_blocks: List[Dict[str, Any]], page_width: float) -> Optional[Dict[str, float]]:
        if page_width <= 0:
            return None

        center = page_width / 2.0
        # Keep this narrow so small gutter changes are still measurable.
        split_margin = max(2.0, page_width * 0.004)
        body_blocks = [block for block in page_blocks if _is_body_text_line(block.get("text", ""))]
        if len(body_blocks) < 6:
            return None

        left_blocks = [
            block for block in body_blocks
            if float(block.get("max_x", 0.0)) <= center - split_margin
        ]
        right_blocks = [
            block for block in body_blocks
            if float(block.get("min_x", 0.0)) >= center + split_margin
        ]
        if len(left_blocks) < 3 or len(right_blocks) < 3:
            return None

        left_min = _percentile([float(block.get("min_x", 0.0)) for block in left_blocks], 0.15)
        left_max = _percentile([float(block.get("max_x", 0.0)) for block in left_blocks], 0.85)
        right_min = _percentile([float(block.get("min_x", 0.0)) for block in right_blocks], 0.15)
        right_max = _percentile([float(block.get("max_x", 0.0)) for block in right_blocks], 0.85)

        left_width = left_max - left_min
        right_width = right_max - right_min
        column_gap = right_min - left_max
        text_span = right_max - left_min

        if min(left_width, right_width, column_gap, text_span) <= 0:
            return None

        return {
            "avg_column_width": (left_width + right_width) / 2.0,
            "column_gap": column_gap,
            "text_span": text_span,
        }

    try:
        page_blocks = extract_text_blocks_per_page(pdf_path)
        page_widths = extract_page_widths(pdf_path)
        if not page_blocks or not page_widths:
            return None

        check_until_page = main_pages_limit
        if references_page is not None and references_page > 1:
            check_until_page = min(check_until_page, references_page - 1)

        start_page = 1 if check_until_page >= 2 else 0
        single_column_pages: List[int] = []
        width_change_pages: List[int] = []
        narrow_gap_pages: List[int] = []
        metric_pages: List[Tuple[int, float, Dict[str, float]]] = []

        for page_idx in range(start_page, min(check_until_page, len(page_blocks), len(page_widths))):
            if page_texts is not None and page_idx < len(page_texts) and not _is_body_like_page(page_texts[page_idx]):
                continue

            page_width = page_widths[page_idx]
            if page_width <= 0:
                continue

            metrics = _estimate_two_column_metrics(page_blocks[page_idx], page_width)
            if metrics is not None:
                metric_pages.append((page_idx + 1, page_width, metrics))
                continue

            center = page_width / 2.0
            gutter = max(24.0, page_width * 0.08)
            body_blocks = [
                block for block in page_blocks[page_idx]
                if _is_body_text_line(block["text"])
            ]
            if len(body_blocks) < 6:
                continue

            spanning_blocks = [
                block for block in body_blocks
                if block["min_x"] < center - gutter and block["max_x"] > center + gutter
            ]
            left_blocks = [
                block for block in body_blocks
                if block["max_x"] <= center - gutter
            ]
            right_blocks = [
                block for block in body_blocks
                if block["min_x"] >= center + gutter
            ]

            if len(spanning_blocks) >= max(4, len(body_blocks) // 2) and len(right_blocks) <= max(1, len(body_blocks) // 8):
                single_column_pages.append(page_idx + 1)

        if metric_pages:
            baseline_slice = metric_pages[:min(2, len(metric_pages))]
            baseline_col_width = median(item[2]["avg_column_width"] for item in baseline_slice)
            baseline_gap = median(item[2]["column_gap"] for item in baseline_slice)

            for page_num, page_width, metrics in metric_pages:
                expected_col_width = page_width * IEEE_COLUMN_WIDTH_RATIO
                expected_gap = page_width * IEEE_COLUMN_GAP_RATIO
                expected_text_span = page_width * IEEE_TEXT_WIDTH_RATIO

                avg_col_width = metrics["avg_column_width"]
                column_gap = metrics["column_gap"]
                text_span = metrics["text_span"]

                width_far_from_ieee = abs(avg_col_width - expected_col_width) > expected_col_width * 0.10
                width_far_from_baseline = abs(avg_col_width - baseline_col_width) > baseline_col_width * 0.08
                text_span_far_from_ieee = abs(text_span - expected_text_span) > expected_text_span * 0.05
                if (width_far_from_ieee and width_far_from_baseline) or text_span_far_from_ieee:
                    width_change_pages.append(page_num)

                gap_far_from_ieee = column_gap < expected_gap * 0.75
                gap_far_from_baseline = column_gap < baseline_gap * 0.80
                if gap_far_from_ieee and gap_far_from_baseline:
                    narrow_gap_pages.append(page_num)

        warnings: List[str] = []
        if single_column_pages:
            pages = ", ".join(str(page) for page in single_column_pages[:3])
            warnings.append(
                "Body layout appears single-column instead of IEEE two-column "
                f"on page(s) {pages}."
            )

        if width_change_pages:
            pages = ", ".join(str(page) for page in sorted(set(width_change_pages))[:3])
            warnings.append(
                "Column width appears non-standard for IEEE layout "
                f"on page(s) {pages}."
            )

        if narrow_gap_pages:
            pages = ", ".join(str(page) for page in sorted(set(narrow_gap_pages))[:3])
            warnings.append(
                "Distance between columns appears narrower than IEEE expectations "
                f"on page(s) {pages}."
            )

        if warnings:
            return " ".join(warnings)

        return None
    except Exception:
        return None


def check_font_size_decrease(
    pdf_path: Path,
    main_pages_limit: int = 10,
    references_page: Optional[int] = None,
    page_texts: Optional[List[str]] = None,
    check_references: bool = True,
    style: Optional[str] = None,
) -> Optional[str]:
    """Check if font size significantly decreases anywhere in the main content area.
    
    Args:
        pdf_path: Path to the PDF file
        main_pages_limit: Expected limit for main content pages (to know where to check)
    
    Returns:
        Warning message if font size decrease detected, None otherwise
    """
    try:
        page_samples = extract_font_size_samples_per_page(pdf_path)
        font_sizes = [_estimate_body_font_size(samples) for samples in page_samples]
        
        if not font_sizes or len(font_sizes) < 2:
            return None
        
        # Filter out None values and track valid indices
        valid_sizes = [(i, size) for i, size in enumerate(font_sizes) if size is not None]
        
        if len(valid_sizes) < 2:
            return None
        
        normalized_style = (style or "").lower()
        expected_main_font = IEEE_BODY_FONT_TARGET if normalized_style == "ieee" else None
        expected_reference_font = IEEE_REFERENCE_FONT_TARGET if normalized_style == "ieee" else None

        # Restrict to main-content pages only.
        check_until_page = main_pages_limit
        if references_page is not None and references_page > 1:
            check_until_page = min(check_until_page, references_page - 1)

        # Check first 3 pages as baseline for "normal" font size.
        baseline_pages = valid_sizes[:min(3, len(valid_sizes))]
        if not baseline_pages:
            return None
        
        baseline_size = median(size for _, size in baseline_pages)

        # Some templates legitimately use two stable text-size buckets in the
        # early pages (e.g., around 10pt and 8pt). Treat such recurring sizes
        # as allowed so they do not trigger a false "decrease" warning later.
        baseline_page_indices = {idx for idx, _ in baseline_pages}
        baseline_samples: List[float] = []
        for idx in baseline_page_indices:
            baseline_samples.extend(s for s in page_samples[idx] if 5.0 <= s <= 20.0)

        baseline_bucket_counts = Counter(round(s, 1) for s in baseline_samples)
        baseline_allowed_sizes = {baseline_size}
        if baseline_bucket_counts:
            max_count = max(baseline_bucket_counts.values())
            min_alt_count = max(12, int(max_count * 0.3))
            for bucket_size, bucket_count in baseline_bucket_counts.items():
                if bucket_count >= min_alt_count:
                    baseline_allowed_sizes.add(bucket_size)
        
        # Look for significant decreases in subsequent pages.
        remaining_pages = [
            (page_idx, page_size)
            for page_idx, page_size in valid_sizes[3:]
            if (page_idx + 1) <= check_until_page
        ]
        
        for page_idx, page_size in remaining_pages:
            if page_texts is not None and page_idx < len(page_texts):
                if not _is_body_like_page(page_texts[page_idx]):
                    continue

            # If the page still contains enough baseline-sized text, this is
            # usually a figure/caption-heavy page, not body text shrinking.
            current_samples = [s for s in page_samples[page_idx] if 5.0 <= s <= 20.0]
            baseline_like_count = sum(1 for s in current_samples if abs(s - baseline_size) <= 0.5)
            if baseline_like_count >= 8:
                continue

            # If current page aligns with an allowed baseline size bucket,
            # consider it stable and do not flag as a decrease.
            if any(abs(page_size - allowed) <= 0.4 for allowed in baseline_allowed_sizes):
                continue

            if _matches_expected_font_target(page_size, expected_main_font):
                continue

            # If font size drops by more than 10%, flag it.
            if page_size < baseline_size * 0.9:
                decrease_pct = round((1 - page_size / baseline_size) * 100)
                return f"Font size decreases in main content starting from page {page_idx + 1} (from {baseline_size:.1f}pt to {page_size:.1f}pt, {decrease_pct}% reduction)."

        # Optionally check reference pages as well when the references section exists.
        if check_references and references_page is not None and references_page <= len(font_sizes):
            reference_pages = [
                (page_idx, page_size)
                for page_idx, page_size in valid_sizes
                if (page_idx + 1) >= references_page
            ]

            # References frequently use a smaller but consistent text size than
            # the body. Build a references-local baseline from the first one or
            # two reference pages and only flag further decreases within
            # references, instead of comparing directly to the body baseline.
            reference_baseline_candidates = [
                page_size
                for page_idx, page_size in reference_pages
                if page_idx in {references_page - 1, references_page}
            ]
            if not reference_baseline_candidates and reference_pages:
                reference_baseline_candidates = [reference_pages[0][1]]

            reference_baseline_size = median(reference_baseline_candidates) if reference_baseline_candidates else None

            # If references are stable at one smaller size (for example 8pt),
            # do not treat them as a violation. We only flag shrinkage that
            # occurs after references have already started.
            for page_idx, page_size in reference_pages:
                if reference_baseline_size is None:
                    break

                if _matches_expected_font_target(page_size, expected_reference_font):
                    continue

                if page_size < reference_baseline_size * 0.9:
                    decrease_pct = round((1 - page_size / reference_baseline_size) * 100)
                    return (
                        f"Font size decreases in references starting from page {page_idx + 1} "
                        f"(from {reference_baseline_size:.1f}pt to {page_size:.1f}pt, {decrease_pct}% reduction)."
                    )
        
        return None
    except Exception:
        return None


def find_references_page(texts: List[str]) -> Optional[int]:
    for idx, txt in enumerate(texts):
        lines = txt.splitlines()
        for line in lines:
            # Strict standalone "References" header.
            if _is_references_header_line(line):
                return idx + 1
    return None


def extract_references_text(texts: List[str], ref_page: Optional[int]) -> str:
    """Return text from the References header onward.

    This avoids pulling pre-header body text when references start mid-page.
    """
    if ref_page is None or ref_page < 1 or ref_page > len(texts):
        return ""

    first_ref_page_text = texts[ref_page - 1]
    lines = first_ref_page_text.splitlines()

    start_idx = 0
    for i, line in enumerate(lines):
        if _is_references_header_line(line):
            start_idx = i
            break

    sliced_first_page = "\n".join(lines[start_idx:])
    remaining_pages = "\n".join(texts[ref_page:])

    if remaining_pages:
        return f"{sliced_first_page}\n{remaining_pages}"
    return sliced_first_page


def is_references_at_page_start(texts: List[str], ref_page: int, max_lines_before: int = 5) -> bool:
    """Check if references section starts at the beginning of the page.
    
    Args:
        texts: List of page texts
        ref_page: Page number where references are found (1-indexed)
        max_lines_before: Maximum number of lines allowed before "References" header
    
    Returns:
        True if references start near the beginning of the page, False otherwise
    """
    if ref_page < 1 or ref_page > len(texts):
        return False
    
    page_text = texts[ref_page - 1]
    lines = page_text.splitlines()
      
    # Find which line the References header is on
    for line_idx, line in enumerate(lines):
        # print(f"Checking line {line_idx}: '{line.strip()}'")
        
        # If the line only contains numbers, then it is the line numbers in the margin, 
        # and we should ignore it when counting lines before the "References" section appears
        if re.match(r"^\d{1,4}?\s*", line.strip()):
            # print("only a number in the line, skipping it")
            max_lines_before += 1
        if re.match(r"^references?\s*:?", line.strip(), flags=re.IGNORECASE):
            # References start at or very near the beginning of the page
            return line_idx <= max_lines_before
    
    return False


def contains_figure_table_appendix(text: str) -> bool:
    # Check for figure/table/appendix references
    # Pattern looks for "Figure/Table/Fig." followed by arabic or roman numbering.
    caption_pattern = r"\b(Figure|Table|Fig\.)\s+([0-9]+|[IVXLC]+)\b\s*:?"
    return bool(re.search(caption_pattern, text, flags=re.IGNORECASE))


def _contains_appendix_header(page_text: str) -> bool:
    """Detect likely appendix section headers on a page."""
    for line in (page_text or "").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if APPENDIX_HEADER.match(candidate):
            return True
    return False


def find_appendix_pages_outside_main_pages(texts: List[str], main_pages_limit: int) -> List[int]:
    """Return pages where appendix headers appear after the main-page limit."""
    appendix_pages: List[int] = []
    for page_idx in range(main_pages_limit, len(texts)):
        if _contains_appendix_header(texts[page_idx]):
            appendix_pages.append(page_idx + 1)
    return appendix_pages


def _is_body_like_page(text: str) -> bool:
    """Heuristic to identify pages dominated by prose body text."""
    compact = " ".join((text or "").split())
    if not compact:
        return False

    # Caption-led pages are commonly table/figure-heavy even when text length
    # is large due OCR/extraction of dense tabular content.
    if re.match(r"^(TABLE|FIGURE|Fig\.)\s+([0-9]+|[IVXLC]+)\b", compact, flags=re.IGNORECASE):
        return False

    # Most body pages in this corpus have substantially more prose content.
    if len(compact) >= 3500:
        return True

    # Short pages are accepted only when no explicit figure/table marker exists.
    return not contains_figure_table_appendix(compact)


def detect_style(text: str) -> str:
    # returns "acm", "ieee" or "unknown"
    for style, patterns in STYLE_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, text, flags=re.IGNORECASE):
                return style
    return "unknown"


def _reference_entry_lines(ref_text: str) -> List[str]:
    """Extract likely reference-entry start lines from the references section."""
    lines: List[str] = []
    for raw_line in ref_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        # Skip section headers and continuation lines when indentation is present.
        if REFERENCES_HEADER.match(stripped):
            continue
        if raw_line[:1].isspace():
            continue

        # Numeric IEEE-style entries: [1] ...
        if re.match(r"^\[\d+\]\s+\S", stripped):
            lines.append(stripped)
            continue

        # Bulleted list entries.
        if re.match(r"^[*\-•·]\s+\S", stripped):
            lines.append(stripped)
            continue

        # Numbered entries without brackets: 1. ..., 1) ..., 1: ..., 1 ...
        plain_number_match = re.match(r"^(\d+)(?:[\).:]|\s)\s+(.+)$", stripped)
        if plain_number_match:
            number_prefix = int(plain_number_match.group(1))
            remainder = plain_number_match.group(2).strip()
            # Ignore year-like prefixes from wrapped continuation lines.
            if 1800 <= number_prefix <= 2100:
                continue
            # Ignore common continuation fragments, e.g., "30. [Online] ...".
            if not re.match(r"^\[(online|accessed)\]", remainder, flags=re.IGNORECASE):
                lines.append(stripped)
            continue

        # Author-year entry styles.
        if re.match(r"^\[[A-Za-z][^\]]*\(\d{4}\)[^\]]*\]\s+\S", stripped):
            lines.append(stripped)
            continue
        if re.match(r"^[A-Z][A-Za-z'`\-]+(?:\s+et\s+al\.)?\s*\(\d{4}\)\s+\S", stripped):
            lines.append(stripped)
            continue

    return lines


def detect_nonstandard_reference_format(ref_text: str) -> List[str]:
    """Return detected non-standard reference entry styles.

    Non-standard means entries are not in bracketed numeric style, e.g.:
    "[1] Author, Title, Venue, Year".
    """
    entry_lines = _reference_entry_lines(ref_text)
    if not entry_lines:
        return []

    detected: List[str] = []
    first_bracket_numeric_idx = next(
        (i for i, line in enumerate(entry_lines) if re.match(r"^\[\d+\]\s+\S", line)),
        None,
    )
    has_bracket_numeric = first_bracket_numeric_idx is not None

    # If numeric-bracket references exist, treat lines before the first [n]
    # as likely non-reference body artifacts (e.g., numbered lists, bullets).
    lines_for_nonstandard = (
        entry_lines[first_bracket_numeric_idx:]
        if first_bracket_numeric_idx is not None
        else entry_lines
    )

    plain_number_count = 0
    bullet_count = 0

    for idx, line in enumerate(lines_for_nonstandard):
        # Expected: [number] ...
        if re.match(r"^\[\d+\]\s+\S", line):
            continue

        # Bullet lists.
        if re.match(r"^[*\-•·]\s+\S", line):
            if has_bracket_numeric and line.startswith("- "):
                prev_line = lines_for_nonstandard[idx - 1] if idx > 0 else ""
                next_line = lines_for_nonstandard[idx + 1] if idx + 1 < len(lines_for_nonstandard) else ""
                # In numeric references, dashed lines between [n] entries are
                # often wrapped continuation text, not bullet-list items.
                if re.match(r"^\[\d+\]\s+\S", prev_line) or re.match(r"^\[\d+\]\s+\S", next_line):
                    continue
            bullet_count += 1
            continue

        # Numbering without brackets: "1.", "1)", "1:" or bare "1 "
        plain_number_match = re.match(r"^(\d+)(?:[\).:]|\s)\s+\S", line)
        if plain_number_match:
            number_prefix = int(plain_number_match.group(1))
            if 1800 <= number_prefix <= 2100:
                continue
            # Large prefixes are commonly volume numbers, page spans, etc.
            if number_prefix >= 1000:
                continue
            plain_number_count += 1
            continue

        # Author-year and key-year styles.
        if re.match(r"^\[[A-Za-z][^\]]*\(\d{4}\)[^\]]*\]\s+\S", line) or re.match(r"^[A-Z][A-Za-z'`\-]+(?:\s+et\s+al\.)?\s*\(\d{4}\)\s+\S", line):
            if "author-year citations" not in detected:
                detected.append("author-year citations")
            continue

    # A single plain-number-looking line inside otherwise numeric-bracketed
    # references is often a wrapped continuation fragment (false positive).
    if (has_bracket_numeric and bullet_count >= 3) or (not has_bracket_numeric and bullet_count >= 1):
        if "bullet points" not in detected:
            detected.append("bullet points")

    if plain_number_count >= 2 or (plain_number_count == 1 and not has_bracket_numeric):
        if "plain numbering without brackets" not in detected:
            detected.append("plain numbering without brackets")

    return detected


def check_reference_format(ref_text: str) -> str:
    """Check if references use numeric citations ([1], [2], etc) or author citations.
    
    Returns:
        "numeric" if using [1], [2], etc.
        "author" if using [Author et al.(year)] or similar
        "mixed" if both formats present
        "unknown" if no citations found
    """
    entry_lines = _reference_entry_lines(ref_text)

    has_numeric = any(re.match(r"^\[\d+\]\s+\S", line) for line in entry_lines)
    has_author_style = any(
        re.match(r"^\[[A-Za-z][^\]]*\(\d{4}\)[^\]]*\]\s+\S", line)
        or re.match(r"^[A-Z][A-Za-z'`\-]+(?:\s+et\s+al\.)?\s*\(\d{4}\)\s+\S", line)
        for line in entry_lines
    )
    
    if has_numeric and not has_author_style:
        return "numeric"
    elif has_author_style and not has_numeric:
        return "author"
    elif has_numeric and has_author_style:
        return "mixed"
    else:
        return "unknown"



def check_file(
    file_path: str,
    max_pages: Optional[int] = None,
    min_pages: Optional[int] = None,
    style: Optional[str] = None,
    timeout: int = 10,
    main_pages: Optional[int] = None,
    check_reference_font_size: bool = True,
) -> List[str]:
    warnings: List[str] = []
    path = Path(file_path)
    if not path.exists():
        return [f"File not found: {file_path}"]

    try:
        texts = extract_text_with_timeout(path, timeout=timeout)
    except Exception as e:
        return [f"Error reading PDF: {str(e)[:100]}"]

    # A one-time retry helps reduce transient extraction failures under
    # parallel load without affecting normal successful files.
    if not texts:
        retry_timeout = max(timeout + 10, timeout * 3)
        if retry_timeout != timeout:
            try:
                texts = extract_text_with_timeout(path, timeout=retry_timeout)
            except Exception:
                texts = []

    # Final fallback to direct extraction path.
    if not texts:
        try:
            texts = extract_text_per_page(path)
        except Exception:
            texts = []

    if not texts:
        return [f"Could not extract text from PDF (possibly corrupted, encrypted, or slow to read)."]
    
    metadata = get_metadata(path)
    num_pages = len(texts)

    if max_pages is not None and num_pages > max_pages:
        warnings.append(f"Number of pages ({num_pages}) exceeds limit ({max_pages}).")

    if min_pages is not None and num_pages < min_pages:
        warnings.append(f"Number of pages ({num_pages}) is less than minimum required ({min_pages}).")

    ref_page = find_references_page(texts)
    if ref_page is not None and max_pages is not None and ref_page > max_pages:
        warnings.append(f"References start on page {ref_page}, which is after page limit {max_pages}.")
    
    # Check if references start too late (implying main text exceeds limit)
    main_pages_limit = main_pages if main_pages is not None else 10  # Default to 10 for ICSE
    if ref_page is not None and ref_page > main_pages_limit + 1:
        warnings.append(f"References must start no later than page {main_pages_limit + 1}, but found on page {ref_page}.")
    
    # Check if references are on the expected page but not at the beginning (main content exceeded limit)
    if ref_page is not None and ref_page == main_pages_limit + 1:
        if not is_references_at_page_start(texts, ref_page):
            warnings.append(f"Main content exceeds {main_pages_limit} pages (references do not start at beginning of page {ref_page}).")
    
    # If no references found, check if total pages exceed main text limit
    if ref_page is None and num_pages > main_pages_limit:
        warnings.append(f"No references section found, and total pages ({num_pages}) exceed main text limit ({main_pages_limit}).")

    # pages after references
    after_refs = []
    if ref_page is not None:
        # Only flag figures/tables/appendix if they appear after valid content area
        # Use max_pages if specified, otherwise use main_pages_limit
        figure_check_limit = max_pages if max_pages is not None else main_pages_limit
        after_refs = []
        for pageno in range(ref_page - 1, num_pages):
            page_num = pageno + 1  # Convert to 1-indexed
            # Only flag if page is beyond the figure/table check limit
            if page_num > figure_check_limit and contains_figure_table_appendix(texts[pageno]):
                after_refs.append(page_num)
        if after_refs:
            warnings.append(
                f"Figures/tables appear on pages after references: {after_refs}."
            )

    appendix_pages = find_appendix_pages_outside_main_pages(texts, main_pages_limit)
    if appendix_pages:
        warnings.append(
            f"Appendix content appears outside main pages on page(s): {appendix_pages}."
        )

    # style detection
    combined = "\n".join(texts[:2])
    detected = detect_style(combined)

    # Detect non-standard references format regardless of style requirement.
    if ref_page is not None and ref_page <= len(texts):
        ref_content = extract_references_text(texts, ref_page)
        nonstandard_formats = detect_nonstandard_reference_format(ref_content)
        if nonstandard_formats:
            details = ", ".join(nonstandard_formats)
            warnings.append(
                "Non-standard references format detected "
                f"({details}). Expected format like '[1] Author, Title, Venue, Year'."
            )

    if style:
        style = style.lower()
        if style not in ("acm", "ieee"):
            warnings.append(f"Unknown requested style '{style}'.")
        else:
            if style == "acm" and detected != "acm":
                warnings.append("Document may not conform to ACM style.")
            if style == "ieee" and detected == "acm":
                warnings.append("Document seems to be ACM style, not IEEE.")
            
            # Check reference format if IEEE style is requested
            if style == "ieee" and ref_page is not None and ref_page <= len(texts):
                ref_content = extract_references_text(texts, ref_page)
                ref_format = check_reference_format(ref_content)
                if ref_format == "author":
                    warnings.append("References use author citations instead of numeric citations (required for IEEE style).")
                elif ref_format == "mixed":
                    warnings.append("References mix numeric and author citations (IEEE style requires numeric only).")
    else:
        if detected == "acm":
            warnings.append("Document appears to follow ACM style.")
        elif detected == "ieee":
            warnings.append("Document appears to follow IEEE style.")

    # check for email on page1
    if texts:
        page1 = texts[0]
        email_match = EMAIL_RE.search(page1)
        if email_match:
            found_email = email_match.group(0).lower()
            if found_email not in ALLOWED_EMAILS:
                warnings.append("Non-anonymous email detected on page 1.")

    # suspicious wording
    fulltext = "\n".join(texts)
    for phrase in SUSPICIOUS_PHRASES:
        if re.search(phrase, fulltext, flags=re.IGNORECASE):
            warnings.append(f"Suspicious wording detected: '{phrase}'.")


    # Only check /Author metadata
    if metadata:
        author = metadata.get('/Author')
        if author:
            author_str = str(author).strip()
            if author_str and author_str.lower() not in ("author", "anonymous", "ieee"):
                warnings.append("PDF metadata contains potentially identifying information.")

    # Check for font size decrease in main content area
    main_pages_limit = main_pages if main_pages is not None else 10  # Default to 10 for ICSE
    font_warning = check_font_size_decrease(
        path,
        main_pages_limit=main_pages_limit,
        references_page=ref_page,
        page_texts=texts,
        check_references=check_reference_font_size,
        style=style,
    )
    if font_warning:
        warnings.append(font_warning)

    return warnings


def check_folder(
    folder_path: str,
    max_pages: Optional[int] = None,
    min_pages: Optional[int] = None,
    style: Optional[str] = None,
    timeout: int = 10,
    main_pages: Optional[int] = None,
    check_reference_font_size: bool = True,
    workers: Optional[int] = None,
) -> dict:
    """Check all PDFs in a folder and subfolders, returning results.
    
    Args:
        folder_path: Path to folder containing PDFs
        max_pages: Optional page limit
        style: Optional style ('acm' or 'ieee')
    
    Returns:
        Dict with 'passed', 'failed', and 'results' (list of tuples: (filename, warnings))
    """
    folder = Path(folder_path)
    if not folder.is_dir():
        return {"error": f"Folder not found: {folder_path}", "passed": 0, "failed": 0, "results": []}
    
    results = []
    passed = 0
    failed = 0
    
    # Find all PDFs in the folder and subfolders recursively
    pdf_files = sorted(folder.glob("**/*.pdf"))
    
    if not pdf_files:
        return {"passed": 0, "failed": 0, "results": [], "message": "No PDF files found in folder or subfolders."}
    
    def _resolve_workers(total_files: int, requested_workers: Optional[int]) -> int:
        if total_files <= 1:
            return 1

        if requested_workers is not None:
            return max(1, min(requested_workers, total_files))

        cpu_count = os.cpu_count() or 4
        # PDF parsing is mixed IO/CPU; this keeps concurrency high without oversubscription.
        auto_workers = min(16, max(4, cpu_count * 2))
        return max(1, min(auto_workers, total_files))

    worker_count = _resolve_workers(len(pdf_files), workers)

    if worker_count == 1:
        count = 0
        for pdf_file in pdf_files:
            count += 1
            # Get relative path for display
            try:
                rel_path = pdf_file.relative_to(folder)
            except ValueError:
                rel_path = pdf_file

            print(f"Checking file {count}/{len(pdf_files)}: {rel_path}")

            try:
                warnings = check_file(
                    str(pdf_file),
                    max_pages=max_pages,
                    min_pages=min_pages,
                    style=style,
                    timeout=timeout,
                    main_pages=main_pages,
                    check_reference_font_size=check_reference_font_size,
                )
            except Exception as e:
                warnings = [f"Error processing file: {str(e)[:100]}"]

            results.append((str(rel_path), warnings))
            if warnings:
                failed += 1
            else:
                passed += 1
    else:
        indexed_pdf_files: List[Tuple[int, Path, Path]] = []
        for idx, pdf_file in enumerate(pdf_files):
            try:
                rel_path = pdf_file.relative_to(folder)
            except ValueError:
                rel_path = pdf_file
            indexed_pdf_files.append((idx, pdf_file, rel_path))

        def _check_pdf(indexed_item: Tuple[int, Path, Path]) -> Tuple[int, str, List[str]]:
            idx, pdf_file, rel_path = indexed_item
            try:
                warnings = check_file(
                    str(pdf_file),
                    max_pages=max_pages,
                    min_pages=min_pages,
                    style=style,
                    timeout=timeout,
                    main_pages=main_pages,
                    check_reference_font_size=check_reference_font_size,
                )
            except Exception as e:
                warnings = [f"Error processing file: {str(e)[:100]}"]
            return idx, str(rel_path), warnings

        ordered_results: Dict[int, Tuple[str, List[str]]] = {}
        completed = 0
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_relpath = {
                executor.submit(_check_pdf, indexed_item): indexed_item[2]
                for indexed_item in indexed_pdf_files
            }

            for future in as_completed(future_to_relpath):
                completed += 1
                rel_path = future_to_relpath[future]
                print(f"Checking file {completed}/{len(pdf_files)}: {rel_path}")

                idx, rel_path_str, warnings = future.result()
                ordered_results[idx] = (rel_path_str, warnings)

        for idx in range(len(pdf_files)):
            rel_path_str, warnings = ordered_results[idx]
            results.append((rel_path_str, warnings))
            if warnings:
                failed += 1
            else:
                passed += 1
    
    return {"passed": passed, "failed": failed, "results": results}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Check academic submission PDFs for policy issues.")
    parser.add_argument(
        "--file", 
        help="Path to a single PDF file"
    )
    parser.add_argument(
        "--folder", 
        help="Path to folder containing PDFs to check"
    )
    parser.add_argument(
        "--max-pages", 
        type=int, 
        help="Maximum total pages allowed (main text + references)"
    )
    parser.add_argument(
        "--main-pages",
        type=int,
        default=10,
        help="Maximum pages for main text (default: 10). References must start after this.",
    )
    parser.add_argument(
        "--style",
        choices=["acm", "ieee"],
        help="Declare expected style (acm or ieee) for additional validation",
    )
    parser.add_argument(
        "--check-reference-font-size",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Also check for font-size shrinking in references (on by default; use --no-check-reference-font-size to disable).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Maximum seconds to wait when extracting text from each PDF (default: 10)",
    )
    parser.add_argument(
        "--min-pages",
        type=int,
        help="Minimum total pages required (main text + references)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of PDFs to process concurrently in folder mode (default: auto).",
    )
    parser.add_argument(
        "--csv",
        help="Path to output CSV report file (for folder checks)",
    )
    parser.add_argument(
        "--hotcrp-csv",
        help="Path to HotCRP CSV file for bulk-updating paper tags in HotCRP. Paper ID is extracted from the filename (last number in filename), e.g., for paper ase26-paper123.pdf the paper id is 123.",
    )
    args = parser.parse_args()
    
    print("Submission Checker Configuration:")
    for arg_name, arg_value in vars(args).items():
        print(f"- {arg_name}: {arg_value}")

    # Check that at least one of --file or --folder is provided
    if not args.file and not args.folder:
        parser.error("Either --file or --folder must be provided.")
    
    if args.file and args.folder:
        parser.error("Provide either --file or --folder, not both.")
    
    if args.csv and not args.folder:
        parser.error("--csv can only be used with --folder.")

    # Handle single file
    if args.file:
        print(f"Checking file: {args.file}")
        warnings = check_file(
            args.file,
            max_pages=args.max_pages,
            min_pages=args.min_pages,
            style=args.style,
            timeout=args.timeout,
            main_pages=args.main_pages,
            check_reference_font_size=args.check_reference_font_size,
        )
        if warnings:
            print("Warnings:")
            for w in warnings:
                print(" -", w)
            sys.exit(1)
        else:
            print("No issues detected.")
            sys.exit(0)

    # Handle folder
    if args.folder:
        result = check_folder(
            args.folder,
            max_pages=args.max_pages,
            min_pages=args.min_pages,
            style=args.style,
            timeout=args.timeout,
            main_pages=args.main_pages,
            check_reference_font_size=args.check_reference_font_size,
            workers=args.workers,
        )
        
        if "error" in result:
            print(f"Error: {result['error']}")
            sys.exit(1)
        
        if "message" in result:
            print(result["message"])
            sys.exit(0)
        
        if args.csv:
            # Write to CSV
            with open(args.csv, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['Filename', 'Status', 'Issues'])
                for filename, warnings in result["results"]:
                    status = "PASS" if not warnings else "FAIL"
                    issues = "; ".join(warnings) if warnings else ""
                    writer.writerow([filename, status, issues])
            print(f"CSV report written to {args.csv}")
            print(f"Summary: {result['passed']} passed, {result['failed']} failed out of {result['passed'] + result['failed']} files")
        else:
            # Print results
            print(f"\n{'Filename':<40} {'Status':<10} {'Issues'}")
            print("=" * 70)
            
            for filename, warnings in result["results"]:
                status = "✓ PASS" if not warnings else "✗ FAIL"
                num_issues = len(warnings)
                print(f"{filename:<40} {status:<10} {num_issues}")
                if warnings:
                    for w in warnings:
                        print(f"  - {w}")
            
            print("\n" + "=" * 70)
            print(f"Summary: {result['passed']} passed, {result['failed']} failed out of {result['passed'] + result['failed']} files")
            
        if args.hotcrp_csv:
            # Create HotCRP CSV for bulk-updating tags in HotCRP
            with open(args.hotcrp_csv, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['paper', 'tag'])
                for filename, warnings in result["results"]:
                    paper_id = "N/A"
                    match = re.search(r"(\d+)(?!.*\d)", filename) # Extract last number in filename as paper ID
                    if match:
                        paper_id = int(match.group(1))   
                    tag = "pdf-pass" if not warnings else "pdf-warning" 
                    writer.writerow([paper_id, tag])
            print(f"CSV report written to {args.hotcrp_csv}")
        
        sys.exit(1 if result["failed"] > 0 else 0)



if __name__ == "__main__":
    main()
