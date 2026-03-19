"""Entry point for submission checker CLI."""
import re
import sys
import csv
from pathlib import Path
from typing import List, Tuple, Optional

from pypdf import PdfReader

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SUSPICIOUS_PHRASES = [r"our previous paper", r"in our previous work"]
REFERENCES_HEADER = re.compile(r"^references?$", flags=re.IGNORECASE)
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


def find_references_page(texts: List[str]) -> Optional[int]:
    for idx, txt in enumerate(texts):
        lines = txt.splitlines()
        for line in lines:
            if REFERENCES_HEADER.match(line.strip()):
                return idx + 1
    return None


def contains_figure_table_appendix(text: str) -> bool:
    # naive check for keywords
    return bool(re.search(r"\b(Figure|Table|Appendix)\b", text, flags=re.IGNORECASE))


def detect_style(text: str) -> str:
    # returns "acm", "ieee" or "unknown"
    for style, patterns in STYLE_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, text, flags=re.IGNORECASE):
                return style
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
        warnings.append(f"References start on page {ref_page}, which implies main text exceeds {main_pages_limit} pages.")
    
    # If no references found, check if total pages exceed main text limit
    if ref_page is None and num_pages > main_pages_limit:
        warnings.append(f"No references section found, and total pages ({num_pages}) exceed main text limit ({main_pages_limit}).")

    # pages after references
    after_refs = []
    if ref_page is not None:
        for pageno in range(ref_page - 1, num_pages):
            if contains_figure_table_appendix(texts[pageno]):
                after_refs.append(pageno + 1)
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
    else:
        if detected == "acm":
            warnings.append("Document appears to follow ACM style.")
        elif detected == "ieee":
            warnings.append("Document appears to follow IEEE style.")

    # check for email on page1
    if texts:
        page1 = texts[0]
        if EMAIL_RE.search(page1):
            warnings.append("Non-anonymous email detected on page 1.")

    # suspicious wording
    fulltext = "\n".join(texts)
    for phrase in SUSPICIOUS_PHRASES:
        if re.search(phrase, fulltext, flags=re.IGNORECASE):
            warnings.append(f"Suspicious wording detected: '{phrase}'.")

    # metadata
    identifying_keys = ['/Author', '/Title', '/Subject', '/Keywords']
    if metadata:
        for key in identifying_keys:
            value = metadata.get(key)
            if value and str(value).strip():
                warnings.append("PDF metadata contains potentially identifying information.")
                break

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
