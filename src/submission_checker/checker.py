"""Entry point for submission checker CLI."""
import re
import sys
import csv
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
STYLE_KEYWORDS = {
    "acm": [r"acm", r"association for computing machinery"],
    "ieee": [r"ieee", r"institute of electrical and electronics engineers"],
}


from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError


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
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(extract_text_per_page, pdf_path)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            return []
        except Exception:
            return []


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


def _is_body_text_line(text: str) -> bool:
    compact = " ".join(text.split())
    if len(compact) < 25:
        return False
    if not re.search(r"[A-Za-z]", compact):
        return False
    if re.match(r"^(references?|figure|fig\.|table|appendix)\b", compact, flags=re.IGNORECASE):
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
        gutter = 24.0

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

        baseline_gaps: List[float] = []
        baseline_pages = min(3, len(line_metrics), check_until_page)
        for page_idx in range(baseline_pages):
            lines = line_metrics[page_idx]
            body_font_size = font_sizes[page_idx] if page_idx < len(font_sizes) else None
            page_width = page_widths[page_idx] if page_idx < len(page_widths) else 0.0
            for group in _split_lines_for_spacing(lines, page_width):
                for current, following in zip(group, group[1:]):
                    if not (_is_body_text_line(current["text"]) and _is_body_text_line(following["text"])):
                        continue
                    if body_font_size is not None:
                        if abs(current["font_size"] - body_font_size) > 1.0 or abs(following["font_size"] - body_font_size) > 1.0:
                            continue
                    gap = current["y"] - following["y"]
                    if 6.0 <= gap <= 24.0:
                        baseline_gaps.append(gap)

        if len(baseline_gaps) < 4:
            return None

        baseline_gap = median(baseline_gaps)
        compressed_pages: List[int] = []

        for page_idx in range(baseline_pages, min(check_until_page, len(line_metrics))):
            if page_texts is not None and page_idx < len(page_texts) and not _is_body_like_page(page_texts[page_idx]):
                continue

            lines = line_metrics[page_idx]
            body_font_size = font_sizes[page_idx] if page_idx < len(font_sizes) else None
            body_gaps: List[float] = []
            page_width = page_widths[page_idx] if page_idx < len(page_widths) else 0.0

            for group in _split_lines_for_spacing(lines, page_width):
                for current, following in zip(group, group[1:]):
                    gap = current["y"] - following["y"]
                    if gap <= 0:
                        continue

                    if _is_heading_line(current["text"], current["font_size"], body_font_size) and _is_body_text_line(following["text"]):
                        if gap < baseline_gap * 0.78:
                            return (
                                f"Line spacing appears compressed near a heading on page {page_idx + 1} "
                                f"(gap {gap:.1f}pt vs baseline {baseline_gap:.1f}pt)."
                            )

                    if not (_is_body_text_line(current["text"]) and _is_body_text_line(following["text"])):
                        continue

                    if body_font_size is not None:
                        if abs(current["font_size"] - body_font_size) > 1.0 or abs(following["font_size"] - body_font_size) > 1.0:
                            continue

                    if 4.0 <= gap <= 24.0:
                        body_gaps.append(gap)

            if len(body_gaps) >= 4:
                page_gap = median(body_gaps)
                compressed_count = sum(1 for gap in body_gaps if gap < baseline_gap * 0.84)
                if page_gap < baseline_gap * 0.87 and compressed_count >= max(3, len(body_gaps) // 2):
                    compressed_pages.append(page_idx + 1)

        if compressed_pages:
            pages = ", ".join(str(page) for page in compressed_pages[:3])
            return (
                "Line spacing appears tighter than the IEEE baseline "
                f"on page(s) {pages} (baseline {baseline_gap:.1f}pt)."
            )

        return None
    except Exception:
        return None


def check_ieee_column_layout(
    pdf_path: Path,
    main_pages_limit: int = 10,
    references_page: Optional[int] = None,
    page_texts: Optional[List[str]] = None,
) -> Optional[str]:
    """Detect likely single-column body layout in IEEE manuscripts."""
    try:
        page_blocks = extract_text_blocks_per_page(pdf_path)
        page_widths = extract_page_widths(pdf_path)
        if not page_blocks or not page_widths:
            return None

        check_until_page = main_pages_limit
        if references_page is not None and references_page > 1:
            check_until_page = min(check_until_page, references_page - 1)

        start_page = 1 if check_until_page >= 2 else 0
        suspect_pages: List[int] = []

        for page_idx in range(start_page, min(check_until_page, len(page_blocks), len(page_widths))):
            if page_texts is not None and page_idx < len(page_texts) and not _is_body_like_page(page_texts[page_idx]):
                continue

            page_width = page_widths[page_idx]
            if page_width <= 0:
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
                suspect_pages.append(page_idx + 1)

        if suspect_pages:
            pages = ", ".join(str(page) for page in suspect_pages[:3])
            return (
                "Body layout appears single-column instead of IEEE two-column "
                f"on page(s) {pages}."
            )

        return None
    except Exception:
        return None


def check_font_size_decrease(
    pdf_path: Path,
    main_pages_limit: int = 10,
    references_page: Optional[int] = None,
    page_texts: Optional[List[str]] = None,
    check_references: bool = True,
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

            for page_idx, page_size in reference_pages:
                # If references align with baseline size buckets, treat as stable.
                if any(abs(page_size - allowed) <= 0.4 for allowed in baseline_allowed_sizes):
                    continue

                if page_size < baseline_size * 0.9:
                    decrease_pct = round((1 - page_size / baseline_size) * 100)
                    return f"Font size decreases in references starting from page {page_idx + 1} (from {baseline_size:.1f}pt to {page_size:.1f}pt, {decrease_pct}% reduction)."
        
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
        if _is_references_header_line(line):
            # References start at or very near the beginning of the page
            return line_idx <= max_lines_before
    
    return False


def contains_figure_table_appendix(text: str) -> bool:
    # Check for figure/table/appendix references
    # Pattern looks for "Figure/Table/Fig." followed by arabic or roman numbering.
    caption_pattern = r"\b(Figure|Table|Fig\.)\s+([0-9]+|[IVXLC]+)\b\s*:?"
    return bool(re.search(caption_pattern, text, flags=re.IGNORECASE))


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
    check_ieee_spacing: bool = False,
    check_reference_font_size: bool = False,
) -> List[str]:
    warnings: List[str] = []
    path = Path(file_path)
    if not path.exists():
        return [f"File not found: {file_path}"]

    try:
        texts = extract_text_with_timeout(path, timeout=timeout)
    except Exception as e:
        return [f"Error reading PDF: {str(e)[:100]}"]
    
    # If no text could be extracted, still return a warning
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
            # Only flag if page is beyond the figure check limit
            if page_num > figure_check_limit and contains_figure_table_appendix(texts[pageno]):
                after_refs.append(page_num)
        if after_refs:
            warnings.append(
                f"Figures/tables/appendix appear on pages after references: {after_refs}."
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
    )
    if font_warning:
        warnings.append(font_warning)

    if style == "ieee":
        column_warning = check_ieee_column_layout(
            path,
            main_pages_limit=main_pages_limit,
            references_page=ref_page,
            page_texts=texts,
        )
        if column_warning:
            warnings.append(column_warning)

        if check_ieee_spacing:
            spacing_warning = check_ieee_line_spacing(
                path,
                main_pages_limit=main_pages_limit,
                references_page=ref_page,
                page_texts=texts,
            )
            if spacing_warning:
                warnings.append(spacing_warning)

    return warnings


def check_folder(
    folder_path: str,
    max_pages: Optional[int] = None,
    min_pages: Optional[int] = None,
    style: Optional[str] = None,
    timeout: int = 10,
    main_pages: Optional[int] = None,
    check_ieee_spacing: bool = False,
    check_reference_font_size: bool = False,
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
    
    for pdf_file in pdf_files:
        # Get relative path for display
        try:
            rel_path = pdf_file.relative_to(folder)
        except ValueError:
            rel_path = pdf_file
        
        print(f"Checking file: {rel_path}")
        
        try:
            warnings = check_file(
                str(pdf_file),
                max_pages=max_pages,
                min_pages=min_pages,
                style=style,
                timeout=timeout,
                main_pages=main_pages,
                check_ieee_spacing=check_ieee_spacing,
                check_reference_font_size=check_reference_font_size,
            )
        except Exception as e:
            warnings = [f"Error processing file: {str(e)[:100]}"]
        
        results.append((str(rel_path), warnings))
        if warnings:
            failed += 1
        else:
            passed += 1
    
    return {"passed": passed, "failed": failed, "results": results}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Check academic submission PDFs for policy issues.")
    parser.add_argument("--file", help="Path to a single PDF file")
    parser.add_argument("--folder", help="Path to folder containing PDFs to check")
    parser.add_argument("--max-pages", type=int, help="Maximum total pages allowed (main text + references)")
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
        "--check-ieee-spacing",
        action="store_true",
        help="Enable the experimental IEEE line-spacing heuristic (off by default).",
    )
    parser.add_argument(
        "--check-reference-font-size",
        action="store_true",
        help="Also check for font-size shrinking in references (off by default).",
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
        "--csv",
        help="Path to output CSV report file (for folder checks)",
    )
    args = parser.parse_args()

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
            check_ieee_spacing=args.check_ieee_spacing,
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
            check_ieee_spacing=args.check_ieee_spacing,
            check_reference_font_size=args.check_reference_font_size,
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
        
        sys.exit(1 if result["failed"] > 0 else 0)



if __name__ == "__main__":
    main()
