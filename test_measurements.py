#!/usr/bin/env python3
"""Quick test of 700ml and 5000ml measurements"""
import sys
import os

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from label_analyzer_production import LabelAnalyzer
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')

# Test both PDFs
test_cases = [
    ("/Users/clawdy/Desktop/hazard_label_700ml.pdf", 1.19, "700ml (mixed-case)"),
    ("/Users/clawdy/Desktop/30660179_2.pdf", 1.78, "5000ml (all-caps)"),
]

print("\n" + "=" * 80)
print("FONT MEASUREMENT TEST")
print("=" * 80)

analyzer = LabelAnalyzer(project_id="test", model="gemini-3-pro-preview")

for pdf_path, expected_mm, label in test_cases:
    print(f"\n📋 Testing: {label}")
    print(f"   File: {pdf_path}")
    print(f"   Expected: {expected_mm}mm")
    
    if not os.path.exists(pdf_path):
        print(f"   ❌ FILE NOT FOUND")
        continue
    
    try:
        # Analyze the PDF
        parts = analyzer.analyze_pdf(pdf_path)
        
        # Find font measurements in any CLP region
        fonts_found = []
        for part in parts:
            if part.font_size_mm > 0:
                fonts_found.append(part.font_size_mm)
                print(f"   ✓ Measured: {part.font_size_mm:.2f}mm (from {part.label})")
        
        if not fonts_found:
            print(f"   ⚠️  No measurements found")
        else:
            actual = fonts_found[0]
            error_pct = abs(actual - expected_mm) / expected_mm * 100
            status = "✅ PASS" if error_pct < 2 else f"❌ FAIL ({error_pct:.1f}% error)"
            print(f"   {status}")
    
    except Exception as e:
        print(f"   ❌ ERROR: {e}")

print("\n" + "=" * 80)
