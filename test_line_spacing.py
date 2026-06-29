#!/usr/bin/env python3
"""Quick test to verify the IEEE line spacing check collects multiple warnings."""

from pathlib import Path
from submission_checker.checker import check_ieee_line_spacing

# Test with a sample paper
test_pdf = Path("test_data/icse2025/icse-2025-papers/icse2025-paper1527.pdf")

if test_pdf.exists():
    print(f"Testing with: {test_pdf}")
    result = check_ieee_line_spacing(test_pdf, main_pages_limit=10)
    
    if result:
        print(f"\nWarnings found:")
        print(f"  {result}")
        
        # Check if multiple warnings are present
        if "." in result and result.count(".") > 1:
            print(f"\n✓ Multiple warnings successfully collected!")
        else:
            print(f"\n- Single warning (or no issue)")
    else:
        print("\nNo line spacing issues detected.")
else:
    print(f"Test file not found: {test_pdf}")
