#!/usr/bin/env python3
"""Test vector-based measurement against 700ml hazard label ground truth.

This PDF has NO embedded text — all glyphs are vector outlines (10145 drawings).
Only the vector clustering path in measure_font_from_pdf_vectors applies.
"""

import sys, os
sys.path.insert(0, '/Users/clawdy/Desktop/label-analyzer')

# We need to test the actual production code's vector measurement
# But it requires a region_rect_px. Let's use the full page.

import fitz
import statistics
from collections import Counter

PDF_PATH = "/Users/clawdy/Desktop/hazard_label_700ml.pdf"
EXPECTED_FONT_MM = 1.19
EXPECTED_GAP_MM = 0.98
TOLERANCE_PCT = 2.0

def measure_vectors(pdf_path):
    doc = fitz.open(pdf_path)
    page = doc.load_page(0)
    rect = page.rect
    
    print(f"Page: {rect.width:.1f} x {rect.height:.1f} pts ({rect.width/72*25.4:.1f} x {rect.height/72*25.4:.1f} mm)")
    
    drawings = page.get_drawings()
    print(f"Total drawings: {len(drawings)}")
    
    # Collect all glyph-like paths (small, text-sized)
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
    
    print(f"Glyph-like paths (0.3<h<20pt, 0.1<w<30pt): {len(glyphs)}")
    
    # Height distribution
    h_bins = Counter(round(g['h_mm'], 2) for g in glyphs)
    print(f"\nHeight histogram (top 15):")
    for h, c in h_bins.most_common(15):
        bar = '█' * min(c, 80)
        print(f"  {h:.2f}mm: {c:4d} {bar}")
    
    # Group into text lines by y_center
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
    
    print(f"\nDetected {len(text_lines)} text lines (tolerance={line_tolerance:.2f}pt)")
    
    # Per-line: group into chars, get median height
    line_data = []  # (line_idx, median_char_h_mm, y_center_mm, num_chars)
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
    
    # Show all lines
    print(f"\nAll lines (idx, median_h_mm, y_mm, #chars):")
    for idx, med_h, y_mm, nc in line_data:
        print(f"  Line {idx:3d}: h={med_h:.3f}mm  y={y_mm:.2f}mm  chars={nc}")
    
    # Cluster lines by median height (find body text)
    line_h_bins = Counter(round(h, 1) for _, h, _, _ in line_data)
    print(f"\nLine height clusters: {dict(sorted(line_h_bins.items()))}")
    
    best_bin = None
    best_count = 0
    for h_bin, count in line_h_bins.items():
        total = count
        for nb in [h_bin - 0.1, h_bin + 0.1]:
            total += line_h_bins.get(round(nb, 1), 0)
        if total > best_count:
            best_count = total
            best_bin = h_bin
    
    print(f"Dominant line height cluster: {best_bin:.1f}mm ({best_count} lines)")
    
    # Body text lines
    body_lines = [(idx, h, y, nc) for idx, h, y, nc in line_data if abs(h - best_bin) <= 0.3]
    print(f"Body text lines: {len(body_lines)}")
    
    # Collect all body char heights for bimodal analysis
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
    
    # Bimodal analysis
    MIN_H = 0.5
    body_char_heights = [h for h in body_char_heights if h >= MIN_H]
    h_hist = Counter(round(h, 2) for h in body_char_heights)
    
    print(f"\nBody char height histogram (top 10):")
    for h, c in h_hist.most_common(10):
        print(f"  {h:.2f}mm: {c}")
    
    # Cluster
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
    
    peaks = [(c[0], c[1]) for c in clusters if c[1] >= 3]
    peaks.sort(key=lambda x: -x[1])
    
    print(f"\nPeaks:")
    for h, c in peaks[:5]:
        print(f"  {h:.3f}mm: {c} chars")
    
    # Determine x-height
    if len(peaks) >= 2:
        p1_h, _ = peaks[0]
        p2_h, _ = peaks[1]
        short_peak, tall_peak = sorted([p1_h, p2_h])
        if tall_peak - short_peak > 0.2:
            xheight_mm = short_peak
            capheight_mm = tall_peak
            print(f"\nBimodal: x-height={xheight_mm:.4f}mm, cap-height={capheight_mm:.4f}mm")
        else:
            xheight_mm = peaks[0][0]
            print(f"\nPeaks too close, single peak: {xheight_mm:.4f}mm")
    elif peaks:
        xheight_mm = peaks[0][0]
        print(f"\nSingle peak: {xheight_mm:.4f}mm")
    else:
        xheight_mm = statistics.median(body_char_heights)
        print(f"\nFallback median: {xheight_mm:.4f}mm")
    
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
        
        print(f"\nLine spacings (c2c, mm): {[round(s, 3) for s in spacings]}")
        print(f"C2C: {c2c_mm:.4f}mm, Gap = {c2c_mm:.4f} - {font_mm:.4f} = {gap_mm:.4f}mm")
    else:
        gap_mm = None
        print("\nNot enough lines for spacing")
    
    doc.close()
    
    # Results
    print(f"\n{'='*60}")
    print("RESULTS vs GROUND TRUTH:")
    font_err = (font_mm - EXPECTED_FONT_MM) / EXPECTED_FONT_MM * 100
    font_ok = abs(font_err) <= TOLERANCE_PCT
    print(f"  Font: {font_mm:.4f}mm vs {EXPECTED_FONT_MM}mm → {font_err:+.2f}% {'✅' if font_ok else '❌'}")
    if gap_mm is not None:
        gap_err = (gap_mm - EXPECTED_GAP_MM) / EXPECTED_GAP_MM * 100
        gap_ok = abs(gap_err) <= TOLERANCE_PCT
        print(f"  Gap:  {gap_mm:.4f}mm vs {EXPECTED_GAP_MM}mm → {gap_err:+.2f}% {'✅' if gap_ok else '❌'}")
    
    return font_mm, gap_mm


if __name__ == "__main__":
    if not os.path.exists(PDF_PATH):
        print(f"ERROR: PDF not found: {PDF_PATH}")
        sys.exit(1)
    measure_vectors(PDF_PATH)
