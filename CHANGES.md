# Changes to IEEE Line Spacing Check

## Issue
The `check_ieee_line_spacing` function had two problems:
1. **Poor column separation**: Used a gutter of 24.0 pixels, which was too large and failed to properly separate IEEE two-column layouts. This caused lines from different columns to be compared against each other (e.g., code listings vs section headers).
2. **Early return**: Function returned immediately upon finding the first line spacing issue, requiring multiple runs to discover all problems.

## Solution

### 1. Reduced Gutter Size (Line ~443)
Changed the gutter from `24.0` to `15.0` in the `_split_lines_for_spacing` helper function:

```python
# Before
gutter = 24.0

# After  
gutter = 15.0
```

This smaller gutter better separates the two columns in IEEE format, reducing false positives from cross-column comparisons.

### 2. Collect All Warnings (Lines ~505-561)
Modified the function to collect all warnings instead of returning early:

**Before:**
- When heading spacing issue found → immediate return
- When compressed pages found → return with single warning

**After:**
- Collect heading spacing warnings in a list: `heading_spacing_warnings`
- Collect compressed pages in existing list: `compressed_pages`
- At the end, combine all warnings and return them as a single concatenated string

This allows users to see all line spacing issues in one run instead of iteratively fixing and re-running.

## Example Output

Instead of:
```
Line spacing appears compressed near a heading on page 3 (gap 8.2pt vs baseline 10.5pt).
```

Now shows all issues:
```
Line spacing appears compressed near a heading on page 3 (gap 8.2pt vs baseline 10.5pt). Line spacing appears compressed near a heading on page 5 (gap 7.9pt vs baseline 10.5pt). Line spacing appears tighter than the IEEE baseline on page(s) 7, 8, 9 (baseline 10.5pt).
```

## Testing
The changes have been tested to ensure:
- No syntax errors
- The function still runs successfully on sample IEEE papers
- The return type remains `Optional[str]` (backwards compatible)
