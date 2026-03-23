# Submission Checker

A command-line tool for academic conference submissions that automatically validates PDFs for compliance with conference policies.

## Features

- **Page limit checking** – Warn when the number of pages exceeds a configurable limit.
- **References position** – Detect if references start after the allowed page.
- **Content validation** – Flag occurrences of figures, tables, and appendices on pages that should contain references only.
- **Style conformance** – Verify conformance to ACM or IEEE citation style.
- **Anonymity checks** – Detect non-anonymous emails mentioned on page 1.
- **Suspicious wording** – Identify potentially revealing phrases like "our previous paper [3]".
- **Metadata inspection** – Inspect PDF metadata for possible author information that could reveal identity.
- **Font size detection** – Flag PDFs where font size decreases in the main content/body area (a common technique to evade page limits).

## Options

- `--file <path>`: Path to a single PDF file to check
- `--folder <path>`: Path to folder containing PDFs to check (recursive)
- `--max-pages <int>`: Maximum total pages allowed (main text + references)
- `--min-pages <int>`: Minimum total pages required (main text + references)
- `--main-pages <int>`: Maximum pages for main text (default: 10)
- `--style <acm|ieee>`: Expected citation style for validation
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

### 3. Figures/Tables/Appendix After References Check
- **Logic**: After locating the references section, scans subsequent pages for keywords "Figure", "Table", or "Appendix" (case-insensitive). Lists page numbers where found.
- **Configuration**: None (automatic if references are found).
- **Note**: Uses word boundaries to avoid false positives.

### 4. Style Detection Check
- **Logic**: Searches the first two pages for style-specific keywords:
  - ACM: "acm" or "association for computing machinery"
  - IEEE: "ieee" or "institute of electrical and electronics engineers"
- **Configuration**: `--style acm` or `--style ieee` (optional). If specified, warns on mismatch. If not, reports detected style.
- **Note**: Only reports if ACM or IEEE is detected.

### 5. Email Detection Check
- **Logic**: Uses regex to search for email patterns on the first page.
- **Configuration**: None (always checked).
- **Regex**: `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`

### 6. Suspicious Wording Check
- **Logic**: Searches the entire document for predefined phrases (case-insensitive).
- **Configuration**: Hardcoded phrases: "our previous paper", "in our previous work".
- **Note**: Warns for each matching phrase found.

### 7. Metadata Check
- **Logic**: Extracts PDF metadata (e.g., author, title) and checks if any fields contain text.
- **Configuration**: None (always checked).
- **Note**: Metadata often includes identifying information like author names.

### 8. Font Size Detection Check
- **Logic**: Analyzes font sizes across all main content pages. Compares the average font size in the first 3 pages (baseline) with pages 4-10. Flags if any page has a font size that decreases by more than 10% from the baseline.
- **Configuration**: None (automatic, always checked using `--main-pages` limit).
- **Detection scope**: Main content area (determined by `--main-pages` parameter, default 10)
- **Sensitivity**: 10% reduction threshold
- **Baseline**: Average of first 3 pages
- **Output**: Reports the exact page where decrease starts, font sizes (in points), and percentage reduction
- **Example warning**: `Font size decreases in main content starting from page 6 (from 10.1pt to 9.0pt, 10% reduction).`
- **Note**: This detects a common technique to fit more content by reducing font size mid-document. The check is flexible and detects font shrinking at any point in the main content, not just at a specific page.

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
