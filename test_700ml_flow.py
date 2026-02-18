#!/usr/bin/env python3
"""Run analyzer on 700ml PDF with detailed logging, NO Gemini."""
import sys, os, logging
sys.path.insert(0, '/Users/clawdy/Desktop/label-analyzer')

# Set up detailed logging
logging.basicConfig(level=logging.DEBUG, format='%(message)s')
logger = logging.getLogger()

from label_analyzer_production import LabelAnalyzer
import fitz

PDF_PATH = '/Users/clawdy/Desktop/hazard_label_700ml.pdf'

# We need to test measure_font_from_pdf_vectors directly
# First, figure out what region the analyzer would use

doc = fitz.open(PDF_PATH)
page = doc.load_page(0)
page_rect = page.rect
print(f"Page: {page_rect.width:.1f} x {page_rect.height:.1f} pts ({page_rect.width/72*25.4:.1f} x {page_rect.height/72*25.4:.1f} mm)")

# Use full page as region for testing
DPI = 300
zoom = DPI / 72
region_px = {
    'xmin': 0,
    'ymin': 0, 
    'xmax': int(page_rect.width * zoom),
    'ymax': int(page_rect.height * zoom)
}

# Create analyzer with dummy API key (won't use Gemini)
analyzer = LabelAnalyzer.__new__(LabelAnalyzer)
analyzer.original_dpi = DPI
analyzer._reference_dimensions = None

# Bypass Gemini scale detection by pre-populating cache
# Test 1: No scale factor
print("\n" + "="*60)
print("TEST 1: No scale factor (raw PDF vectors)")
print("="*60)
analyzer._pdf_scale_cache = {PDF_PATH: (1.0, 1.0)}

result = analyzer.measure_font_from_pdf_vectors(PDF_PATH, region_px, clp_threshold_mm=1.2)
if result:
    print(f"\nRESULT:")
    print(f"  x-height: {result['font_size_mm']:.4f}mm (expected 1.19mm)")
    print(f"  cap-height: {result.get('cap_height_mm', 'N/A')}mm")
    print(f"  gap: {result['line_distance_mm']:.4f}mm (expected 0.98mm)")
    print(f"  approach: {result.get('measurement_approach', 'N/A')}")

doc.close()
