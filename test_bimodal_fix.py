#!/usr/bin/env python3
"""Test the improved bimodal detection after fixes."""

import sys, os
import fitz
import statistics
from collections import Counter

PDF_PATH = "/Users/clawdy/Desktop/hazard_label_700ml.pdf"
EXPECTED_FONT_MM = 1.19
EXPECTED_GAP_MM = 0.98
TOLERANCE_PCT = 2.0

# Apply PyMuPDF fix
fitz.TOOLS.set_small_glyph_heights(True)

def measure_vectors():
    """Measure using improved bimodal detection."""
    doc = fitz.open(PDF_PATH)
    page = doc.load_page(0)
    drawings = page.get_drawings()
    
    # Collect all glyph-like paths
    glyphs = []
    for d in drawings:
        r = d.get('rect')
        if r:
            w = r[2] - r[0]
            h = r[3] - r[1]
            if 0.3 < h < 20 and 0.1 < w < 30:
                glyphs.append({
                    'rect': r, 'w': w, 'h': h,
                    'y_top': r[1], 'y_bot': r[3],
                    'x': r[0], 'x_end': r[2],
                    'y_center': (r[1] + r[3]) / 2,
                    'h_mm': h / 72 * 25.4,
                })
    
    print(f"\n{'='*60}")
    print("IMPROVED BIMODAL DETECTION TEST")
    print(f"{'='*60}")
    print(f"Total glyphs: {len(glyphs)}")
    
    # Group into text lines
    common_h = Counter(round(g['h'], 1) for g in glyphs).most_common(1)[0][0]
    line_tolerance = max(0.8, common_h * 0.4)
    
    glyphs.sort(key=lambda g: g['y_center'])
    text_lines = []
    current_line = [glyphs[0]]
    current_y = glyphs[0]['y_center']
    for g in glyphs[1:]:
        if abs(g['y_center'] - current_y) < line_tolerance:
            current_line.append(g)
            current_y = sum(p['y_center'] for p in current_line) / len(current_line)
        else:
            if len(current_line) >= 3:
                text_lines.append(current_line)
            current_line = [g]
            current_y = g['y_center']
    if len(current_line) >= 3:
        text_lines.append(current_line)
    
    print(f"Text lines detected: {len(text_lines)}")
    
    # Per-line: group into chars, measure heights
    line_data = []
    for i, line_paths in enumerate(text_lines):
        line_paths.sort(key=lambda g: g['x'])
        chars = []
        cur_char = [line_paths[0]]
        for g in line_paths[1:]:
            cur_end = max(p['x_end'] for p in cur_char)
            if g['x'] < cur_end + 0.5:
                cur_char.append(g)
            else:
                chars.append(cur_char)
                cur_char = [g]
        chars.append(cur_char)
        
        char_heights = []
        for ch in chars:
            top = min(p['y_top'] for p in ch)
            bot = max(p['y_bot'] for p in ch)
            char_heights.append((bot - top) / 72 * 25.4)
        
        if char_heights:
            med_h = statistics.median(char_heights)
            y_center_mm = statistics.median([p['y_center'] for p in line_paths]) / 72 * 25.4
            line_data.append((i, med_h, y_center_mm, len(chars)))
    
    # Find body text cluster
    line_h_bins = Counter(round(h, 1) for _, h, _, _ in line_data)
    best_bin = None
    best_count = 0
    for h_bin, count in line_h_bins.items():
        total = count
        for nb in [h_bin - 0.1, h_bin + 0.1]:
            total += line_h_bins.get(round(nb, 1), 0)
        if total > best_count:
            best_count = total
            best_bin = h_bin
    
    body_lines = [(idx, h, y, nc) for idx, h, y, nc in line_data if abs(h - best_bin) <= 0.3]
    print(f"Body text lines: {len(body_lines)} (dominant cluster: {best_bin}mm)")
    
    # Collect body char heights
    body_char_heights = []
    for idx, _, _, _ in body_lines:
        line_paths = text_lines[idx]
        line_paths.sort(key=lambda g: g['x'])
        chars = []
        cur_char = [line_paths[0]]
        for g in line_paths[1:]:
            cur_end = max(p['x_end'] for p in cur_char)
            if g['x'] < cur_end + 0.5:
                cur_char.append(g)
            else:
                chars.append(cur_char)
                cur_char = [g]
        chars.append(cur_char)
        for ch in chars:
            top = min(p['y_top'] for p in ch)
            bot = max(p['y_bot'] for p in ch)
            body_char_heights.append((bot - top) / 72 * 25.4)
    
    # Bimodal analysis with improved logic
    MIN_H = 0.5
    body_char_heights = [h for h in body_char_heights if h >= MIN_H]
    h_hist = Counter(round(h, 2) for h in body_char_heights)
    
    print(f"\nRaw histogram (top 10):")
    for h, c in h_hist.most_common(10):
        print(f"  {h:.2f}mm: {c}")
    
    # Cluster nearby bins (0.08mm radius)
    sorted_h = sorted(h_hist.keys())
    clusters = []
    for h in sorted_h:
        count = h_hist[h]
        merged = False
        for cl in clusters:
            if abs(h - cl[0]) <= 0.08:
                cl[1] += count
                cl[2].extend([h] * count)
                merged = True
                break
        if not merged:
            clusters.append([h, count, [h] * count])
    
    for cl in clusters:
        cl[0] = statistics.median(cl[2])
    
    # Extract peaks
    peaks = [(c[0], c[1]) for c in clusters if c[1] >= 3]
    peaks_sorted_by_height = sorted(peaks, key=lambda x: x[0])
    
    print(f"\nClusters (old method - sorted by count):")
    for h, c in sorted(peaks, key=lambda x: -x[1])[:5]:
        print(f"  {h:.3f}mm: {c} chars")
    
    print(f"\nClusters (NEW method - sorted by height):")
    for i, (h, c) in enumerate(peaks_sorted_by_height):
        print(f"  Cluster {i+1}: {h:.3f}mm ({c} chars)")
    
    # ==================== NEW BIMODAL DETECTION ====================
    print(f"\n--- IMPROVED BIMODAL DETECTION LOGIC ---")
    
    xheight_mm = 0.0
    capheight_mm = 0.0
    measurement_approach = 'single-peak'
    
    if len(peaks_sorted_by_height) >= 2:
        # IMPROVED ALGORITHM: 
        # 1. Start with most frequent cluster as x-height (body text is most common)
        # 2. Find the next cluster with clear separation (>0.25mm) as cap-height
        # 3. This correctly identifies mixed-case text bimodal distribution
        
        top_by_count = sorted(peaks_sorted_by_height, key=lambda x: -x[1])
        print(f"Clusters by character count: {[(h, c) for h, c in top_by_count[:5]]}")
        
        # Use most frequent cluster as x-height
        xheight_h, xheight_count = top_by_count[0]
        
        # Find cap-height: prefer the SECOND most frequent cluster if it has good separation,
        # otherwise find ANY cluster with good separation and high count
        best_capheight_h = None
        best_capheight_c = 0
        best_separation = 0
        
        for h, c in top_by_count[1:]:
            separation = abs(h - xheight_h)
            # Good separation (>0.25mm) + reasonable frequency (>50 chars) + not tiny/huge
            if separation > 0.25 and c > 50 and 0.8 < h < 3.0:
                # Prefer the most frequent among valid candidates
                if c > best_capheight_c:
                    best_capheight_h = h
                    best_capheight_c = c
                    best_separation = separation
        
        print(f"Most frequent cluster (x-height): {xheight_h:.3f}mm ({xheight_count} chars)")
        
        if best_capheight_h is not None:
            print(f"Best cap-height candidate: {best_capheight_h:.3f}mm ({best_capheight_c} chars, separation={best_separation:.3f}mm)")
            xheight_mm = min(xheight_h, best_capheight_h)
            capheight_mm = max(xheight_h, best_capheight_h)
            measurement_approach = 'bimodal-xheight'
            print(f"✓ BIMODAL DETECTION SUCCESS: {xheight_mm:.4f}mm (x-height), {capheight_mm:.4f}mm (cap-height)")
        else:
            # No clear second cluster — single peak
            xheight_mm = xheight_h
            capheight_mm = xheight_h
            measurement_approach = 'single-peak'
            print(f"✗ No valid cap-height cluster found (need >0.25mm sep, >50 chars, height 0.8-3.0mm), using single peak: {xheight_mm:.4f}mm")
    elif peaks_sorted_by_height:
        xheight_mm = peaks_sorted_by_height[0][0]
        capheight_mm = peaks_sorted_by_height[0][0]
        measurement_approach = 'single-peak'
        print(f"Only {len(peaks_sorted_by_height)} cluster(s) found, using single peak: {xheight_mm:.4f}mm")
    else:
        xheight_mm = statistics.median(body_char_heights)
        capheight_mm = xheight_mm / 0.70
        measurement_approach = 'fallback-median'
        print(f"No clusters, using median: {xheight_mm:.4f}mm")
    
    font_mm = xheight_mm
    
    # Line spacing
    body_ys = sorted([y for _, _, y, _ in body_lines])
    if len(body_ys) >= 2:
        spacings = [body_ys[i+1] - body_ys[i] for i in range(len(body_ys) - 1)]
        spacing_bins = Counter(round(s, 1) for s in spacings)
        mode_spacing = spacing_bins.most_common(1)[0][0]
        tight = [s for s in spacings if abs(s - mode_spacing) <= 0.3]
        c2c_mm = statistics.mean(tight) if tight else statistics.median(spacings)
        gap_mm = max(0, c2c_mm - font_mm)
    else:
        gap_mm = None
    
    doc.close()
    
    # Results
    print(f"\n{'='*60}")
    print("RESULTS vs GROUND TRUTH:")
    if font_mm:
        err = (font_mm - EXPECTED_FONT_MM) / EXPECTED_FONT_MM * 100
        status = "✅" if abs(err) <= TOLERANCE_PCT else "❌"
        print(f"  Font: {font_mm:.4f}mm vs {EXPECTED_FONT_MM}mm → {err:+.2f}% {status}")
    if gap_mm:
        err = (gap_mm - EXPECTED_GAP_MM) / EXPECTED_GAP_MM * 100
        status = "✅" if abs(err) <= TOLERANCE_PCT else "❌"
        print(f"  Gap:  {gap_mm:.4f}mm vs {EXPECTED_GAP_MM}mm → {err:+.2f}% {status}")


if __name__ == "__main__":
    measure_vectors()
