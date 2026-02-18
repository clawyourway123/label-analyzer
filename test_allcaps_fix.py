#!/usr/bin/env python3
"""Test all-caps fix for 5000ml and mixed-case for 700ml."""
import sys, logging
logging.basicConfig(level=logging.INFO, format='%(message)s')

sys.path.insert(0, '.')
from label_analyzer_production import LabelAnalyzer
import fitz

# Create analyzer with dummy project_id (we only use vector measurement, no Gemini)
analyzer = LabelAnalyzer.__new__(LabelAnalyzer)
analyzer.original_dpi = 300

# Helper: convert mm region to px (at 300 DPI)
def mm_to_px(mm):
    return int(mm / 25.4 * 300)

# 5000ml test - full page region
print("=" * 60)
print("TEST 1: 5000ml (all-caps, expect ~1.78mm font, ~2.01mm gap)")
print("=" * 60)
result = analyzer.measure_font_from_pdf_vectors(
    '/Users/clawdy/Desktop/30660179_2.pdf',
    {'xmin': 0, 'ymin': 0, 'xmax': 13000, 'ymax': 8000},
    clp_threshold_mm=1.8
)
if result:
    font_ok = abs(result['font_size_mm'] - 1.78) < 0.05
    gap_ok = abs(result['line_distance_mm'] - 2.01) < 0.3
    print(f"\n>>> RESULT: font={result['font_size_mm']:.3f}mm {('✓' if font_ok else '✗')}, gap={result['line_distance_mm']:.3f}mm {('✓' if gap_ok else '✗')}")
    print(f">>> Expected: font=1.78mm, gap=2.01mm")
    print(f">>> Method: {result.get('measurement_approach', 'unknown')}")
else:
    print(">>> FAILED: No result")

# 700ml test - USE HAZARD-SPECIFIC REGION
print("\n" + "=" * 60)
print("TEST 2: 700ml (mixed-case, expect ~1.19mm font, ~0.98mm gap)")
print("=" * 60)

# From test_700ml_diag3.py: hazard region is y=215-255mm
# Full page width; convert to px
page = fitz.open('/Users/clawdy/Desktop/hazard_label_700ml.pdf').load_page(0)
pw_pt = page.rect.width
ph_pt = page.rect.height
ymin_hazard_mm = 215
ymax_hazard_mm = 255
ymin_px = mm_to_px(ymin_hazard_mm)
ymax_px = mm_to_px(ymax_hazard_mm)
xmax_px = int(pw_pt / 72 * 300)
ymax_page_px = int(ph_pt / 72 * 300)

# Clamp to page bounds
ymin_px = max(0, ymin_px)
ymax_px = min(ymax_page_px, ymax_px)

result2 = analyzer.measure_font_from_pdf_vectors(
    '/Users/clawdy/Desktop/hazard_label_700ml.pdf',
    {'xmin': 0, 'ymin': ymin_px, 'xmax': xmax_px, 'ymax': ymax_px},
    clp_threshold_mm=1.2
)
if result2:
    font_ok = abs(result2['font_size_mm'] - 1.19) < 0.05
    gap_ok = abs(result2['line_distance_mm'] - 0.98) < 0.2
    print(f"\n>>> RESULT: font={result2['font_size_mm']:.3f}mm {('✓' if font_ok else '✗')}, gap={result2['line_distance_mm']:.3f}mm {('✓' if gap_ok else '✗')}")
    print(f">>> Expected: font=1.19mm, gap=0.98mm")
    print(f">>> Method: {result2.get('measurement_approach', 'unknown')}")
else:
    print(">>> FAILED: No result")
