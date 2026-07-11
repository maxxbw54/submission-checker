# Submission Checker

A command-line tool for academic conference submissions that automatically validates PDFs for compliance with conference policies.

## Features

- **Page limit checking** – Warn when the number of pages exceeds a configurable limit.
- **References position** – Detect if references start after the allowed page.
- **Content validation** – Flag figures/tables on pages that should contain references only.
- **Appendix outside main pages** – Identify appendix sections that appear after the configured main-page limit.
- **Style conformance** – Verify conformance to ACM or IEEE citation style.
- **References format check** – Detect non-standard references styles such as bullet points, author-year entries, or plain numbering without brackets.
- **Anonymity checks** – Detect non-anonymous emails mentioned on page 1.
- **Suspicious wording** – Identify potentially revealing phrases like "our previous paper [3]".
- **Metadata inspection** – Inspect PDF metadata for possible author information that could reveal identity.
- **Font size detection** – Flag PDFs where font size decreases in the main content/body area; optional extra check for reference-section shrink.
- **IEEE spacing check** – Optional heuristic to flag IEEE submissions where line spacing appears compressed, including unusually tight spacing after section/subsection headings.
- **IEEE column size check** – Detect potential IEEE layout tampering, including non-standard column widths, reduced distance between columns, and single-column body pages.

## Options

- `--file <path>`: Path to a single PDF file to check
- `--folder <path>`: Path to folder containing PDFs to check (recursive)
- `--max-pages <int>`: Maximum total pages allowed (main text + references)
- `--min-pages <int>`: Minimum total pages required (main text + references)
- `--main-pages <int>`: Maximum pages for main text (default: 10)
- `--style <acm|ieee>`: Expected citation style for validation
- `--check-ieee-spacing`: Enable the experimental IEEE line-spacing heuristic (off by default)
- `--check-reference-font-size`: Also check for font-size shrinking in references (off by default)
- `--timeout <int>`: Maximum seconds for PDF text extraction (default: 10)
- `--csv <path>`: Output CSV report file (requires `--folder`)

## Check Details

The tool performs the following checks on each PDF. All checks require successful text extraction from the PDF.

### 1. Page Limit Check
- **Logic**: Counts the total number of pages in the PDF. If the count exceeds the specified `--max-pages`, a warning is issued. If the count is less than the specified `--min-pages`, a warning is issued.
- **Configuration**: `--max-pages` (integer, optional), `--min-pages` (integer, optional). `--main-pages` (integer, default 10) specifies the limit for main text.
- **Example**: `--max-pages 12 --main-pages 10` warns if PDF has more than 12 pages total.
- **Example (min)**: `--min-pages 8` warns if PDF has fewer than 8 pages total.

### 2. References Placement Check
- **Logic**: Scans each page for a line starting with "reference" or "references" (case-insensitive). Warns if references start after the total page limit or if they start after main text limit +1 (implying main text exceeds limit). If no references found, warns if total pages exceed main text limit.
- **Configuration**: Requires `--max-pages` and uses `--main-pages` (default 10).
- **Note**: Ensures main text does not exceed limit and references are properly placed.

### 3. Figures/Tables After References Check
- **Logic**: After locating the references section, scans subsequent pages for figure/table markers and lists page numbers where found.
- **Configuration**: None (automatic if references are found).
- **Note**: Uses word boundaries to avoid false positives.

### 4. Appendix Outside Main Pages Check
- **Logic**: Scans pages after `--main-pages` for appendix section headers such as `Appendix`, `Appendix A`, or `Appendices`.
- **Configuration**: `--main-pages` (default 10).
- **Output**: Emits a warning with page numbers where appendix headers are detected outside the main-page window.

### 5. Style Detection Check
- **Logic**: Searches the first two pages for style-specific keywords:
  - ACM: "acm" or "association for computing machinery"
  - IEEE: "ieee" or "institute of electrical and electronics engineers"
- **Configuration**: `--style acm` or `--style ieee` (optional). If specified, warns on mismatch. If not, reports detected style.
- **Note**: Only reports if ACM or IEEE is detected.

### 6. Email Detection Check
- **Logic**: Uses regex to search for email patterns on the first page.
- **Configuration**: None (always checked).
- **Regex**: `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`

### 7. Non-Standard References Format Check
- **Logic**: Scans reference entries and validates expected bracketed numeric format such as `[1] Author, Title, Venue, Year`.
- **Detects**:
  - Bullet-point references (for example, `- Author, Title...`)
  - Plain numbering without brackets (for example, `1. Author, Title...`)
  - Author-year style entries (for example, `Smith et al. (2020) ...`)
- **Output**: Emits a warning including detected non-standard format categories.

### 8. Suspicious Wording Check
- **Logic**: Searches the entire document for predefined phrases (case-insensitive).
- **Configuration**: Hardcoded phrases: "our previous paper", "in our previous work".
- **Note**: Warns for each matching phrase found.

### 9. Metadata Check
- **Logic**: Extracts PDF metadata (e.g., author, title) and checks if any fields contain text.
- **Configuration**: None (always checked).
- **Note**: Metadata often includes identifying information like author names.

### 10. Font Size Detection Check
- **Logic**: Analyzes font sizes across the paper using the first 3 pages as baseline. It checks both the main content area and the references section. Flags if any checked page has a font size that decreases by more than 10% from the baseline.
- **Configuration**: Main-content check is automatic; reference-section check requires `--check-reference-font-size`.
- **Detection scope**: Main content area (determined by `--main-pages` parameter, default 10). References pages are checked only when `--check-reference-font-size` is enabled.
- **Sensitivity**: 10% reduction threshold
- **Baseline**: Average of first 3 pages
- **Output**: Reports the exact page where decrease starts, font sizes (in points), and percentage reduction
- **Example warning**: `Font size decreases in main content starting from page 6 (from 10.1pt to 9.0pt, 10% reduction).`
- **Note**: This detects a common technique to fit more content by reducing font size mid-document. The check is flexible and detects font shrinking at any point in the main content, not just at a specific page.

### 11. IEEE Line Spacing Check
- **Logic**: Uses PDF text positions to estimate vertical gaps between adjacent lines, builds a baseline from the early body pages, and flags later pages when prose lines are consistently tighter than that baseline.
- **Configuration**: Active only when both `--style ieee` and `--check-ieee-spacing` are used.
- **Detects**:
  - Compressed spacing between body-text lines
  - Compressed spacing between a section or subsection heading and the first paragraph line
- **Detection scope**: Main-content pages only; pages dominated by figures or tables are skipped when possible.
- **Output**: Emits a warning with the page number and whether the issue looks like general line compression or a heading-to-paragraph spacing issue.
- **Note**: This is an experimental supporting heuristic intended to catch layout tightening that may violate IEEE spacing expectations. It depends on extractable PDF layout information and may miss image-only PDFs.

### 12. IEEE Column Size and Gap Check
- **Logic**: Uses per-line x coordinates to estimate left/right column envelopes and compare them against IEEE two-column geometry. The IEEEtran class defaults are approximately:
  - Column width: 21pc (about 252pt)
  - Column gap (`\columnsep`): 1pc (about 12pt)
  - Text width: 43pc (about 516pt)
- **Configuration**: Active when `--style ieee` is used.
- **Detects**:
  - Pages that look like single-column body layout
  - Non-standard column widths relative to IEEE expectations and early-page baseline
  - Narrowed inter-column spacing (reduced column gap)
- **Detection scope**: Main-content pages only (stops before references when references are detected).
- **Output**: Emits warnings with affected page numbers, e.g. `Column width appears non-standard for IEEE layout...` or `Distance between columns appears narrower than IEEE expectations...`.
- **Note**: This check is heuristic and depends on extractable text geometry. It may not trigger on scanned/image-only PDFs.

## Installation

```bash
cd submission-checker
pip install -e .
pip install pypdf
```

Or use `requirements.txt`:
```bash
pip install -r requirements.txt
pip install -e .
```

## Usage

### Check a Single PDF

```bash
submission-checker --file paper.pdf --max-pages 12 --main-pages 10 --style ieee
```

This checks a single PDF with:
- Maximum total pages: 12 (main text + references)
- Maximum main text pages: 10 (references must start after page 10)
- Expected style: IEEE

Output:
```
Checking file: paper.pdf
Warnings:
 - Number of pages (13) exceeds limit (12).
 - Non-anonymous email detected on page 1.
 - Suspicious wording detected: 'our previous paper'.
```

Exit code: **0** if no issues, **1** if warnings found.

### Scan a Folder of PDFs

Check all PDFs in a directory (recursive):

```bash
submission-checker --folder submissions --max-pages 12 --main-pages 10 --style ieee --timeout 30 --csv report.csv
```

This scans all PDFs in the `submissions` folder with the same page limits and style check, saves results to `report.csv`.

To also enable the experimental IEEE spacing heuristic:

```bash
submission-checker --folder submissions --max-pages 12 --main-pages 10 --style ieee --check-ieee-spacing --csv report.csv
```

Output:
```
Checking file: paper1.pdf
Checking file: paper2.pdf
Checking file: paper3.pdf
Filename                                      Status     Issues
================================================== ===== ========
paper1.pdf                                    ✓ PASS     0
paper2.pdf                                    ✗ FAIL     2
  - Number of pages (10) exceeds limit (8).
  - Non-anonymous email detected on page 1.
paper3.pdf                                    ✗ FAIL     1
  - Could not extract text from PDF (possibly corrupted, encrypted, or slow to read).

================================================== ===== ========
Summary: 1 passed, 2 failed out of 3 files
```

For CSV output:

```bash
submission-checker --folder submissions --max-pages 8 --style acm --csv report.csv
```

This generates a CSV file with columns: Filename, Status, Issues.

Exit code: **0** if all passed, **1** if any failed.

## Troubleshooting

- **"Could not extract text from PDF"**: The PDF may be scanned (image-only), encrypted, or corrupted. Try re-saving as a text PDF or using OCR tools like `ocrmypdf`.
- **Slow scans on network drives**: Increase `--timeout` (e.g., `--timeout 60`) or copy files locally first.
- **No warnings on expected issues**: Ensure the PDF has extractable text. Test with `pdftotext file.pdf -` to verify.
- **Import errors**: Install dependencies with `pip install -r requirements.txt`.

## Development

Run tests:
```bash
pytest
```

Build package:
```bash
pip install -e .
```
```
Filename                                 Status     Issues
======================================================================
paper1.pdf                               ✓ PASS     0
paper2.pdf                               ✗ FAIL     2
  - Number of pages (10) exceeds limit (8).
  - Non-anonymous email detected on page 1.
paper3.pdf                               ✓ PASS     0

======================================================================
Summary: 2 passed, 1 failed out of 3 files
```

## Command-Line Options

- `--file <path>` – Check a single PDF file
- `--folder <path>` – Check all PDFs in a folder (recursive search for `*.pdf`)
- `--max-pages <num>` – Maximum total pages allowed (optional)
- `--min-pages <num>` – Minimum total pages required (optional)
- `--main-pages <num>` – Maximum main text pages (default 10)
- `--style {acm,ieee}` – Enforce a specific citation style (optional)
- `--timeout <num>` – Timeout in seconds for PDF text extraction (default: 10)
- `--csv <path>` – Output results to CSV (folder only)

**Note:** Provide either `--file` or `--folder`, not both.

## Examples

```bash
# Single file, no style requirement
submission-checker --file paper.pdf --max-pages 10
```

# Single file, ACM style required

```bash
submission-checker --file paper.pdf --max-pages 8 --style acm
```

# Batch check IEEE submissions

```bash
submission-checker --folder submissions --max-pages 12 --style ieee
```

# Check folder without page limit

```bash
submission-checker --folder papers
```

## Running Tests

```bash
pytest tests/test_checker.py -q
```

With verbose output:
```bash
pytest tests/test_checker.py -v -s
```

## How It Works

1. **Text Extraction** – Extracts text from each page of the PDF
2. **Content Analysis** – Scans for patterns, emails, suspicious phrases
3. **Metadata Inspection** – Checks PDF metadata for identifying information
4. **Style Detection** – Analyzes text for ACM/IEEE keywords
5. **Reporting** – Lists all issues found with line-by-line details

## Contributing

We welcome contributions! For any bug reports, feature requests, or suggestions for general improvements to the tool, please raise them on our [GitHub Issues](https://github.com/maxxbw54/submission-checker/issues) page.

### General Contributions

For bug fixes and general improvements to the tool (those that benefit all users):
1. Raise an issue first on our [GitHub Issues](https://github.com/maxxbw54/submission-checker/issues) page to discuss the change
2. Fork the repository and create a feature branch (e.g., `fix/email-detection`, `feature/new-check`)
3. Submit a pull request to the `main` branch with a description of your changes and a reference to the issue

### For Conference Organizers

If your conference wants to use and customize this tool for your submission review process, follow these steps:

1. **Create a Conference Branch**
   - Create a new branch with the naming format: `CONFERENCENAME-YEAR`
   - Example: `ICSE-2025`, `OSDI-2026`, `NeurIPS-2025`

2. **Customize for Your Conference**
   - Within your conference branch, you can customize the tool to match your specific requirements:
     - Adjust page limits via configuration
     - Add/modify citation style requirements
     - Create custom validation checks specific to your conference
     - Update check thresholds and parameters
   - Make all changes within this branch without submitting pull requests to `main`

3. **Benefits of This Approach**
   - Your conference-specific customizations remain isolated and don't affect the main tool
   - You can maintain your branch independently without conflicts with other conferences
   - Easy to update from `main` when new general improvements are released
   - Clear separation between core tool improvements and conference-specific configurations

4. **Updating from Main**
   - To incorporate improvements from the `main` branch into your conference branch:
   ```bash
   git fetch origin
   git rebase origin/main
   ```
   - Resolve any conflicts specific to your conference customizations

For questions or if you'd like to establish a conference-specific branch, please open an issue on our [GitHub Issues](https://github.com/maxxbw54/submission-checker/issues) page.
