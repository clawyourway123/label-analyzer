#!/usr/bin/env python3
"""
Test vector measurement without initializing the full analyzer (avoids Gemini calls).
"""

import sys
import fitz
import statistics
from collections import Counter
import hashlib

PDF_PATH = "/Users/clawdy/Desktop/hazard_label_700ml.pdf"

def measure_font_from_pdf_vectors_simplified(pdf_path, region_rect_px):
    """Simplified version of the measurement function (copy-pasted and reduced)."""
    
    doc = fitz.open(pdf_path)
    page = doc.load_page(0)
    
    # Enable small glyph heights
    fitz.TOOLS.set_small_glyph_heights(True)
    
    zoom = 300 / 72
    pt_xmin = region_rect_px['xmin'] / zoom
    pt_ymin = region_rect_px['ymin'] / zoom
    pt_xmax = region_rect_px['xmax'] / zoom
    pt_ymax = region_rect_px['ymax'] / zoom
    
    # Extract all drawings in region
    drawings = page.get_drawings()
    region_paths = []
    for d in drawings:
        r = d.get('rect')
        if r:
            margin = 2
            if (r[0] >= pt_xmin - margin and r[2] <= pt_xmax + margin and
                r[1] >= pt_ymin - margin and r[3] <= pt_ymax + margin):
                w = r[2] - r[0]
                h = r[3] - r[1]
                if 0.3 < h < 20 and 0.1 < w < 30:
                    region_paths.append({
                        'rect': r, 'w': w, 'h': h,
                        'y_top': r[1], 'y_bot': r[3],
                        'x': r[0], 'x_end': r[2],
                        'y_center': (r[1] + r[3]) / 2
                    })
    
    if len(region_paths) < 10:
        print(f"  Only {len(region_paths)} paths, too few")
        return None
    
    # Group into lines
    common_h = Counter(round(g['h'], 1) for g in region_paths).most_common(1)[0][0]
    line_tolerance = max(0.8, common_h * 0.4)
    
    region_paths.sort(key=lambda g: g['y_center'])
    text_lines = []
    current_line = [region_paths[0]]
    current_line_y_median = region_paths[0]['y_center']
    for g in region_paths[1:]:
        if abs(g['y_center'] - current_line_y_median) < line_tolerance:
            current_line.append(g)
            current_line_y_median = sum(p['y_center'] for p in current_line) / len(current_line)
        else:
            if len(current_line) >= 3:
                text_lines.append(current_line)
            current_line = [g]
            current_line_y_median = g['y_center']
    if len(current_line) >= 3:
        text_lines.append(current_line)
    
    if len(text_lines) < 1:
        print(f"  No text lines detected")
        return None
    
    # Measure character heights per line
    all_char_heights_mm = []
    line_y_centers_mm = []
    
    for line_paths in text_lines:
        line_paths.sort(key=lambda g: g['x'])
        chars = []
        current_char = [line_paths[0]]
        for g in line_paths[1:]:
            cur_x_end = max(p['x_end'] for p in current_char)
            if g['x'] < cur_x_end + 0.5:
                current_char.append(g)
            else:
                chars.append(current_char)
                current_char = [g]
        chars.append(current_char)
        
        char_heights_mm = []
        for ch in chars:
            top = min(p['y_top'] for p in ch)
            bot = max(p['y_bot'] for p in ch)
            h_mm = (bot - top) / 72 * 25.4
            char_heights_mm.append(h_mm)
        
        all_char_heights_mm.extend(char_heights_mm)
        line_y_centers_mm.append(statistics.median([p['y_center'] for p in line_paths]) / 72 * 25.4)
    
    if not all_char_heights_mm:
        return None
    
    # Cluster heights
    height_bins = Counter(round(h, 2) for h in all_char_heights_mm)
    sorted_heights = sorted(height_bins.keys())
    clusters = []
    for h in sorted_heights:
        count = height_bins[h]
        merged = False
        for cluster in clusters:
            if abs(h - cluster[0]) <= 0.08:
                cluster[1] += count
                cluster[2].extend([h] * count)
                merged = True
                break
        if not merged:
            clusters.append([h, count, [h] * count])
    
    for cluster in clusters:
        cluster[0] = statistics.median(cluster[2])
    
    peaks = sorted([(c[0], c[1]) for c in clusters if c[1] >= 3], key=lambda x: -x[1])
    
    # Get x-height (most frequent)
    xheight_mm = peaks[0][0] if peaks else statistics.median(all_char_heights_mm)
    
    # Get line gap
    body_text_line_ys = sorted(line_y_centers_mm)
    if len(body_text_line_ys) >= 2:
        spacings = [body_text_line_ys[i+1] - body_text_line_ys[i] for i in range(len(body_text_line_ys)-1)]
        spacing_bins = Counter(round(s, 1) for s in spacings)
        most_common_spacing = spacing_bins.most_common(1)[0][0]
        tight_spacings = [s for s in spacings if abs(s - most_common_spacing) <= 0.3]
        center_to_center_mm = statistics.mean(tight_spacings) if tight_spacings else statistics.median(spacings)
        line_distance_mm = max(0, center_to_center_mm - xheight_mm)
    else:
        line_distance_mm = 0
    
    doc.close()
    
    return {
        'font_size_mm': round(xheight_mm, 4),
        'line_distance_mm': round(line_distance_mm, 4),
        'peaks': peaks[:3],
        'chars_measured': len(all_char_heights_mm)
    }


if __name__ == '__main__':
    # Read PDF and compute hash
    with open(PDF_PATH, 'rb') as f:
        pdf_hash = hashlib.sha256(f.read()).hexdigest()
    print(f"PDF hash: {pdf_hash[:16]}...")
    
    # Define region (full page at 300 DPI)
    region_rect_px = {
        'xmin': 0,
        'ymin': 0,
        'xmax': int(1544 * 300 / 72),
        'ymax': int(1265 * 300 / 72)
    }
    
    print("\n=== TEST: Vector measurement ===")
    result1 = measure_font_from_pdf_vectors_simplified(PDF_PATH, region_rect_px)
    if result1:
        print(f"Run 1: x-height={result1['font_size_mm']:.4f}mm, gap={result1['line_distance_mm']:.4f}mm")
    
    print("\n=== TEST: Second run (verify determinism) ===")
    result2 = measure_font_from_pdf_vectors_simplified(PDF_PATH, region_rect_px)
    if result2:
        print(f"Run 2: x-height={result2['font_size_mm']:.4f}mm, gap={result2['line_distance_mm']:.4f}mm")
        
        # Check if deterministic
        if abs(result1['font_size_mm'] - result2['font_size_mm']) < 0.0001:
            print("✅ Deterministic: x-height matches")
        else:
            print("❌ NOT deterministic: x-height differs!")
        
        if abs(result1['line_distance_mm'] - result2['line_distance_mm']) < 0.0001:
            print("✅ Deterministic: gap matches")
        else:
            print("❌ NOT deterministic: gap differs!")
    
    print(f"\n=== Results vs Ground Truth ===")
    print(f"Expected: x-height=1.19mm, gap=0.98mm")
    print(f"Measured: x-height={result1['font_size_mm']:.4f}mm, gap={result1['line_distance_mm']:.4f}mm")
    print(f"Error: x-height={(result1['font_size_mm']-1.19)/1.19*100:+.2f}%, gap={(result1['line_distance_mm']-0.98)/0.98*100:+.2f}%")
