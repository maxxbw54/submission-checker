"""Entry point for submission checker CLI."""
import re
import sys
import csv
from pathlib import Path
from typing import List, Tuple, Optional

from pypdf import PdfReader

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Generic institutional emails that should not be flagged as anonymity issues
ALLOWED_EMAILS = {"authors@instituitons.edu", "email@email.email", "permissions@acm.org"}
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


def extract_font_sizes_per_page(pdf_path: Path) -> List[Optional[float]]:
    """Extract average font sizes for each page.
    
    Returns a list where each element is the average font size for that page,
    or None if font size could not be determined for that page.
    """
    try:
        reader = PdfReader(str(pdf_path))
        font_sizes = []
        
        for page in reader.pages:
            try:
                # Get all font sizes used on this page
                page_font_sizes = []
                
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
                
                # Calculate average if we found any font sizes
                if page_font_sizes:
                    avg_size = sum(page_font_sizes) / len(page_font_sizes)
                    font_sizes.append(avg_size)
                else:
                    font_sizes.append(None)
                    
            except Exception:
                font_sizes.append(None)
        
        return font_sizes
    except Exception:
        return []


def check_font_size_decrease(pdf_path: Path, main_pages_limit: int = 10) -> Optional[str]:
    """Check if font size significantly decreases anywhere in the main content area.
    
    Args:
        pdf_path: Path to the PDF file
        main_pages_limit: Expected limit for main content pages (to know where to check)
    
    Returns:
        Warning message if font size decrease detected, None otherwise
    """
    try:
        font_sizes = extract_font_sizes_per_page(pdf_path)
        
        if not font_sizes or len(font_sizes) < 2:
            return None
        
        # Filter out None values and track valid indices
        valid_sizes = [(i, size) for i, size in enumerate(font_sizes) if size is not None]
        
        if len(valid_sizes) < 2:
            return None
        
        # Only check the main content area (up to references or main_pages_limit)
        # Check first 3 pages as baseline for "normal" font size
        baseline_pages = valid_sizes[:min(3, len(valid_sizes))]
        if not baseline_pages:
            return None
        
        baseline_size = sum(size for _, size in baseline_pages) / len(baseline_pages)
        
        # Look for significant decreases in subsequent pages
        remaining_pages = valid_sizes[3:main_pages_limit]
        
        for page_idx, page_size in remaining_pages:
            # If font size drops by more than 10%, flag it
            if page_size < baseline_size * 0.9:
                decrease_pct = round((1 - page_size / baseline_size) * 100)
                return f"Font size decreases in main content starting from page {page_idx + 1} (from {baseline_size:.1f}pt to {page_size:.1f}pt, {decrease_pct}% reduction)."
        
        return None
    except Exception:
        return None


def find_references_page(texts: List[str]) -> Optional[int]:
    for idx, txt in enumerate(texts):
        lines = txt.splitlines()
        for line in lines:
            # Check if line starts with "references" (allows for line numbers, etc after it)
            if re.match(r"^references?\s*:?", line.strip(), flags=re.IGNORECASE):
                return idx + 1
    return None


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
        if re.match(r"^\d{1,4}?\s*", line.strip(), flags=re.IGNORECASE):
            #print("only a number in the line, skipping it")
            max_lines_before += 1
        if re.match(r"^references?\s*:?", line.strip(), flags=re.IGNORECASE):
            # References start at or very near the beginning of the page
            return line_idx <= max_lines_before
    
    return False


def contains_figure_table_appendix(text: str) -> bool:
    # Check for figure/table/appendix references
    # Pattern looks for "Figure/Table/Fig. followed by number (with optional colon)"
    caption_pattern = r"\b(Figure|Table|Fig\.)\s+\d+\s*:?" 
    return bool(re.search(caption_pattern, text, flags=re.IGNORECASE))


def detect_style(text: str) -> str:
    # returns "acm", "ieee" or "unknown"
    for style, patterns in STYLE_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, text, flags=re.IGNORECASE):
                return style
    return "unknown"


def check_reference_format(ref_text: str) -> str:
    """Check if references use numeric citations ([1], [2], etc) or author citations.
    
    Returns:
        "numeric" if using [1], [2], etc.
        "author" if using [Author et al.(year)] or similar
        "mixed" if both formats present
        "unknown" if no citations found
    """
    # Look for numeric citations like [1], [2], [99], etc.
    numeric_citations = re.findall(r"\[\d+\]", ref_text)
    
    # Look for author-style citations like [Author et al.(2020)] or [key(year)]
    author_citations = re.findall(r"\[[A-Za-z].*\(\d{4}\)\]", ref_text)
    
    # Also check for abbreviated key style like [sou(2018)]
    key_citations = re.findall(r"\[[a-z]+\(\d{4}\)\]", ref_text)
    
    has_numeric = len(numeric_citations) > 0
    has_author = len(author_citations) > 0
    has_key = len(key_citations) > 0
    has_author_style = has_author or has_key
    
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
                ref_content = "\n".join(texts[ref_page - 1:])
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
            found_email = email_match.group(0)
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
    font_warning = check_font_size_decrease(path, main_pages_limit=main_pages_limit)
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
        default=12,
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
        default="acm",
        help="Declare expected style (acm or ieee) for additional validation",
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
                writer.writerow(['Paper ID', 'Filename', 'Status', 'Issues'])
                for filename, warnings in result["results"]:
                    status = "PASS" if not warnings else "FAIL"
                    issues = "; ".join(warnings) if warnings else ""
                    paper_id = "N/A"
                    match = re.search(r"(\d+)(?!.*\d)", filename) # Extract last number in filename as paper ID
                    if match:
                        paper_id = int(match.group(1))    
                    writer.writerow([paper_id, filename, status, issues])
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
