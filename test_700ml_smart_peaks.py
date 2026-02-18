#!/usr/bin/env python3
"""
Test vector measurement - SMART PEAK DETECTION
Instead of fixed tolerance clustering, use kernel density estimation or histogram modes.
"""

import sys
import fitz
import statistics
from collections import Counter

PDF_PATH = "/Users/clawdy/Desktop/hazard_label_700ml.pdf"
EXPECTED_FONT_MM = 1.19
EXPECTED_GAP_MM = 0.98

def find_peaks_smart(heights_mm, bin_width=0.02):
    """
    Find peaks in height distribution using histogram + local maxima detection.
    bin_width: 0.02mm bins (finer than before)
    """
    if not heights_mm:
        return []
    
    # Create histogram with fine bins
    min_h = min(heights_mm)
    max_h = max(heights_mm)
    bins = {}
    for h in heights_mm:
        bin_key = round(h / bin_width) * bin_width
        bins[bin_key] = bins.get(bin_key, 0) + 1
    
    # Find local maxima
    sorted_bins = sorted(bins.items())
    peaks = []
    for i, (h, count) in enumerate(sorted_bins):
        is_peak = True
        # Check if neighbors are lower
        if i > 0 and bins[sorted_bins[i-1][0]] > count:
            is_peak = False
        if i < len(sorted_bins) - 1 and bins[sorted_bins[i+1][0]] > count:
            is_peak = False
        
        # Must have at least 50 counts to be considered a peak
        if is_peak and count >= 50:
            peaks.append((h, count))
    
    # Sort by count descending
    peaks.sort(key=lambda x: -x[1])
    return peaks

def analyze_vectors():
    doc = fitz.open(PDF_PATH)
    page = doc.load_page(0)
    
    page_w_mm = page.rect.width / 72 * 25.4
    page_h_mm = page.rect.height / 72 * 25.4
    print(f"Page: {page.rect.width:.1f} x {page.rect.height:.1f} pts = {page_w_mm:.1f} x {page_h_mm:.1f} mm")
    
    drawings = page.get_drawings()
    print(f"Total drawings: {len(drawings)}")
    
    # Collect all path rects with stroke info
    all_rects = []
    for d in drawings:
        r = d.get('rect')
        if r:
            w = r[2] - r[0]
            h = r[3] - r[1]
            if w > 0 and h > 0:
                stroke_w = d.get('width', 0) or 0
                all_rects.append({
                    'rect': r, 'w': w, 'h': h,
                    'w_mm': w/72*25.4, 'h_mm': h/72*25.4,
                    'x': r[0], 'y_top': r[1], 'y_bot': r[3],
                    'y_center': (r[1]+r[3])/2,
                    'stroke_w': stroke_w,
                    'h_mm_with_stroke': (h + stroke_w) / 72 * 25.4
                })
    
    # Filter to glyph-sized elements
    glyphs = [r for r in all_rects if 0.3 < r['h_mm'] < 5 and 0.05 < r['w_mm'] < 10]
    print(f"Glyph-sized paths: {len(glyphs)}")
    
    # Group into text lines
    glyphs.sort(key=lambda g: g['y_center'])
    common_h = Counter(round(g['h'], 1) for g in glyphs).most_common(1)[0][0]
    line_tol = max(0.8, common_h * 0.4)
    
    text_lines = []
    cur_line = [glyphs[0]]
    cur_y = glyphs[0]['y_center']
    for g in glyphs[1:]:
        if abs(g['y_center'] - cur_y) < line_tol:
            cur_line.append(g)
            cur_y = sum(p['y_center'] for p in cur_line) / len(cur_line)
        else:
            if len(cur_line) >= 3:
                text_lines.append(cur_line)
            cur_line = [g]
            cur_y = g['y_center']
    if len(cur_line) >= 3:
        text_lines.append(cur_line)
    
    print(f"Text lines detected: {len(text_lines)}")
    
    # Collect all character heights (stroked)
    all_chars_stroke = []
    line_y_centers = []
    for line in text_lines:
        # Group into chars by x-overlap
        line.sort(key=lambda g: g['x'])
        chars = []
        cur_char = [line[0]]
        for g in line[1:]:
            cur_end = max(p['x'] + p['w'] for p in cur_char)
            if g['x'] < cur_end + 0.5:
                cur_char.append(g)
            else:
                chars.append(cur_char)
                cur_char = [g]
        chars.append(cur_char)
        
        for ch in chars:
            top = min(p['y_top'] for p in ch)
            bot = max(p['y_bot'] for p in ch)
            stroke_max = max([p['stroke_w'] for p in ch]) if ch else 0
            h_stroke = (bot - top + stroke_max) / 72 * 25.4
            all_chars_stroke.append(h_stroke)
        
        y_c = statistics.median([p['y_center'] for p in line]) / 72 * 25.4
        line_y_centers.append(y_c)
    
    print(f"\nTotal characters (stroke-adjusted): {len(all_chars_stroke)}")
    
    # Use smart peak detection
    peaks = find_peaks_smart(all_chars_stroke, bin_width=0.02)
    print(f"\nPeaks (smart detection, bin_width=0.02mm):")
    for h, c in peaks[:10]:
        print(f"  {h:.3f}mm: {c} chars")
    
    if peaks:
        xheight = peaks[0][0]
        xh_count = peaks[0][1]
        
        capheight = None
        for h, c in peaks[1:]:
            if abs(h - xheight) > 0.15 and c >= 10:
                capheight = h
                break
        
        print(f"\n  X-height (most frequent peak): {xheight:.4f}mm ({xh_count} chars)")
        if capheight:
            print(f"  Cap-height: {capheight:.4f}mm")
            print(f"  Ratio: {capheight/xheight:.3f}")
    else:
        xheight = statistics.median(all_chars_stroke)
        print(f"\n  Fallback median: {xheight:.4f}mm")
    
    # Line spacing
    body_ys = sorted(line_y_centers)
    if len(body_ys) >= 2:
        spacings = [body_ys[i+1] - body_ys[i] for i in range(len(body_ys)-1)]
        sp_bins = Counter(round(s, 1) for s in spacings)
        mode_sp = sp_bins.most_common(1)[0][0]
        tight = [s for s in spacings if abs(s - mode_sp) <= 0.3]
        c2c = statistics.mean(tight) if tight else statistics.median(spacings)
        gap = max(0, c2c - xheight)
        
        print(f"\n  Line spacings (c2c, top 10):")
        for sp, cnt in sorted(sp_bins.most_common(10), key=lambda x: -x[1]):
            print(f"    {sp:.2f}mm: {cnt}")
        print(f"  Mode c2c: {c2c:.4f}mm")
        print(f"  Gap (c2c - xheight): {gap:.4f}mm")
    else:
        gap = None
        c2c = None
    
    # RESULTS
    print(f"\n{'='*60}")
    print(f"RESULTS vs GROUND TRUTH")
    print(f"{'='*60}")
    
    font_err = (xheight - EXPECTED_FONT_MM) / EXPECTED_FONT_MM * 100
    print(f"  Font x-height: {xheight:.4f}mm (expected {EXPECTED_FONT_MM}mm, error {font_err:+.2f}%) {'✅' if abs(font_err) <= 2 else '❌'}")
    
    if gap is not None:
        gap_err = (gap - EXPECTED_GAP_MM) / EXPECTED_GAP_MM * 100
        print(f"  Line gap:      {gap:.4f}mm (expected {EXPECTED_GAP_MM}mm, error {gap_err:+.2f}%) {'✅' if abs(gap_err) <= 2 else '❌'}")
    
    doc.close()
    return xheight, gap


if __name__ == '__main__':
    analyze_vectors()
