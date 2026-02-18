#!/usr/bin/env python3
"""
Test the production code's measure_font_from_pdf_vectors method directly.
"""

import sys
sys.path.insert(0, '/Users/clawdy/Desktop/label-analyzer')

from label_analyzer_production import LabelAnalyzer
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PDF_PATH = "/Users/clawdy/Desktop/hazard_label_700ml.pdf"

def test_production_vector_measurement():
    """Test the production code's measurement function."""
    
    # Create analyzer
    analyzer = LabelAnalyzer(project_id="test", dpi=300, cache_dir="/tmp/label_analyzer_cache")
    analyzer._pdf_path = PDF_PATH
    
    # Compute PDF hash
    import hashlib
    with open(PDF_PATH, 'rb') as f:
        pdf_hash = hashlib.sha256(f.read()).hexdigest()
    analyzer._pdf_hash = pdf_hash
    
    # Simulate the region (full page)
    region_rect_px = {
        'xmin': 0,
        'ymin': 0,
        'xmax': int(1544 * 300 / 72),  # page width in pixels at 300 DPI
        'ymax': int(1265 * 300 / 72)   # page height in pixels at 300 DPI
    }
    
    print("=" * 60)
    print(f"TEST 1: Production code measurement")
    print("=" * 60)
    
    result = analyzer.measure_font_from_pdf_vectors(PDF_PATH, region_rect_px)
    
    if result:
        print(f"\n✅ Measurement succeeded!")
        print(f"  Font x-height: {result['font_size_mm']:.4f}mm")
        print(f"  Line gap: {result['line_distance_mm']:.4f}mm")
        print(f"  Method: {result['measurement_method']}")
        print(f"  Chars measured: {result['characters_measured']}")
        print(f"  Peaks: {result['height_peaks']}")
    else:
        print(f"\n❌ Measurement failed")
    
    # Check if DPI was locked
    if analyzer.calibration.locked_dpi:
        print(f"\n  🔒 DPI locked at: {analyzer.calibration.locked_dpi}")
    
    return result


if __name__ == '__main__':
    result = test_production_vector_measurement()
    sys.exit(0 if result else 1)
