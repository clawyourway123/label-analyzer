#!/usr/bin/env python3
"""
Test vector measurement against 700ml hazard label PDF (vector-only, no text layer).
Ground truth: font x-height=1.19mm, line gap=0.98mm

Focus: Account for STROKE WIDTH in character height calculations.
PyMuPDF's get_drawings() returns path bboxes EXCLUDING stroke width.
"""

import sys
import fitz
import statistics
from collections import Counter

PDF_PATH = "/Users/clawdy/Desktop/hazard_label_700ml.pdf"
EXPECTED_FONT_MM = 1.19
EXPECTED_GAP_MM = 0.98

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
                    'h_mm_with_stroke': (h + stroke_w) / 72 * 25.4  # Add full stroke width
                })
    
    print(f"Rects with w>0, h>0: {len(all_rects)}")
    
    # Filter to glyph-sized elements
    glyphs = [r for r in all_rects if 0.3 < r['h_mm'] < 5 and 0.05 < r['w_mm'] < 10]
    print(f"\nGlyph-sized paths (0.3-5mm h, 0.05-10mm w): {len(glyphs)}")
    
    if not glyphs:
        print("No glyph paths found!")
        doc.close()
        return
    
    # Show height distribution RAW (without stroke)
    g_h = [g['h_mm'] for g in glyphs]
    g_bins = Counter(round(h, 2) for h in g_h)
    print(f"\nGlyph height distribution RAW (no stroke, top 15):")
    for h, c in g_bins.most_common(15):
        print(f"  {h:.2f}mm: {c}")
    
    # Show height distribution WITH STROKE
    g_h_stroke = [g['h_mm_with_stroke'] for g in glyphs]
    g_bins_stroke = Counter(round(h, 2) for h in g_h_stroke)
    print(f"\nGlyph height distribution WITH STROKE (top 15):")
    for h, c in g_bins_stroke.most_common(15):
        print(f"  {h:.2f}mm: {c}")
    
    # ---- Group into text lines by y_center ----
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
    
    print(f"\nText lines detected: {len(text_lines)}")
    
    # Per-line stats - measure BOTH raw and with stroke
    line_medians_raw = []
    line_medians_stroke = []
    line_y_centers = []
    for i, line in enumerate(text_lines):
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
        
        char_heights_raw = []
        char_heights_stroke = []
        for ch in chars:
            top = min(p['y_top'] for p in ch)
            bot = max(p['y_bot'] for p in ch)
            # Use the maximum stroke width among paths in this character
            # (typically they all have the same stroke, but be safe)
            stroke_widths = [p['stroke_w'] for p in ch]
            stroke_max = max(stroke_widths) if stroke_widths else 0
            
            char_h_raw = (bot - top) / 72 * 25.4
            # Add FULL stroke width (visual extent extends stroke_width on each side of the mathematical path)
            char_h_stroke = (bot - top + stroke_max) / 72 * 25.4
            
            char_heights_raw.append(char_h_raw)
            char_heights_stroke.append(char_h_stroke)
        
        med_raw = statistics.median(char_heights_raw)
        med_stroke = statistics.median(char_heights_stroke)
        line_medians_raw.append(med_raw)
        line_medians_stroke.append(med_stroke)
        y_c = statistics.median([p['y_center'] for p in line]) / 72 * 25.4
        line_y_centers.append(y_c)
        
        if i < 20:
            print(f"  Line {i}: {len(chars)} chars, median_raw={med_raw:.3f}mm, median_stroke={med_stroke:.3f}mm, y={y_c:.2f}mm")
    
    # Find body text lines (most common line height cluster) - use RAW for now
    lm_bins = Counter(round(m, 1) for m in line_medians_raw)
    best_bin = None
    best_count = 0
    for h_bin, cnt in lm_bins.items():
        total = cnt
        if total > best_count:
            best_count = total
            best_bin = h_bin
    
    print(f"\nBody text line height cluster: {best_bin:.1f}mm (RAW, {best_count} lines)")
    
    body_indices = [i for i, m in enumerate(line_medians_raw) if abs(m - best_bin) <= 0.3]
    body_chars_raw = []
    body_chars_stroke = []
    for i in body_indices:
        line = text_lines[i]
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
            stroke_widths = [p['stroke_w'] for p in ch]
            stroke_max = max(stroke_widths) if stroke_widths else 0
            body_chars_raw.append((bot - top) / 72 * 25.4)
            body_chars_stroke.append((bot - top + stroke_max) / 72 * 25.4)
    
    print(f"Body text chars: {len(body_chars_raw)}")
    
    # Height peaks in body text - BOTH raw and with stroke
    print(f"\n--- RAW (no stroke adjustment) ---")
    bc_bins_raw = Counter(round(h, 2) for h in body_chars_raw)
    bc_sorted = sorted(bc_bins_raw.keys())
    bc_clusters_raw = []
    for h in bc_sorted:
        c = bc_bins_raw[h]
        merged = False
        for cl in bc_clusters_raw:
            if abs(h - cl[0]) <= 0.08:
                cl[1] += c
                cl[2].extend([h]*c)
                merged = True
                break
        if not merged:
            bc_clusters_raw.append([h, c, [h]*c])
    for cl in bc_clusters_raw:
        cl[0] = statistics.median(cl[2])
    
    bc_peaks_raw = sorted([(cl[0], cl[1]) for cl in bc_clusters_raw if cl[1] >= 3], key=lambda x: -x[1])
    print(f"Body text height peaks (RAW):")
    for h, c in bc_peaks_raw[:8]:
        print(f"  {h:.3f}mm: {c} chars")
    
    print(f"\n--- WITH STROKE ADJUSTMENT ---")
    bc_bins_stroke = Counter(round(h, 2) for h in body_chars_stroke)
    bc_sorted_stroke = sorted(bc_bins_stroke.keys())
    bc_clusters_stroke = []
    for h in bc_sorted_stroke:
        c = bc_bins_stroke[h]
        merged = False
        for cl in bc_clusters_stroke:
            if abs(h - cl[0]) <= 0.08:
                cl[1] += c
                cl[2].extend([h]*c)
                merged = True
                break
        if not merged:
            bc_clusters_stroke.append([h, c, [h]*c])
    for cl in bc_clusters_stroke:
        cl[0] = statistics.median(cl[2])
    
    bc_peaks_stroke = sorted([(cl[0], cl[1]) for cl in bc_clusters_stroke if cl[1] >= 3], key=lambda x: -x[1])
    print(f"Body text height peaks (WITH STROKE):")
    for h, c in bc_peaks_stroke[:8]:
        print(f"  {h:.3f}mm: {c} chars")
    
    # Bimodal analysis
    if bc_peaks_raw:
        xheight_raw = bc_peaks_raw[0][0]
        xh_count = bc_peaks_raw[0][1]
        
        capheight_raw = None
        for h, c in bc_peaks_raw[1:]:
            if abs(h - xheight_raw) > 0.15 and c >= 10:
                capheight_raw = h
                break
        
        print(f"\n  RAW - X-height (most frequent): {xheight_raw:.4f}mm ({xh_count} chars)")
        if capheight_raw:
            print(f"  RAW - Cap-height: {capheight_raw:.4f}mm")
            print(f"  RAW - Ratio: {capheight_raw/xheight_raw:.3f}")
    else:
        xheight_raw = statistics.median(body_chars_raw)
        print(f"\n  RAW - Fallback median: {xheight_raw:.4f}mm")
    
    if bc_peaks_stroke:
        xheight_stroke = bc_peaks_stroke[0][0]
        xh_count_stroke = bc_peaks_stroke[0][1]
        
        capheight_stroke = None
        for h, c in bc_peaks_stroke[1:]:
            if abs(h - xheight_stroke) > 0.15 and c >= 10:
                capheight_stroke = h
                break
        
        print(f"\n  STROKE-ADJ - X-height (most frequent): {xheight_stroke:.4f}mm ({xh_count_stroke} chars)")
        if capheight_stroke:
            print(f"  STROKE-ADJ - Cap-height: {capheight_stroke:.4f}mm")
            print(f"  STROKE-ADJ - Ratio: {capheight_stroke/xheight_stroke:.3f}")
    else:
        xheight_stroke = statistics.median(body_chars_stroke)
        print(f"\n  STROKE-ADJ - Fallback median: {xheight_stroke:.4f}mm")
    
    # Line spacing
    body_ys = sorted([line_y_centers[i] for i in body_indices])
    if len(body_ys) >= 2:
        spacings = [body_ys[i+1] - body_ys[i] for i in range(len(body_ys)-1)]
        sp_bins = Counter(round(s, 1) for s in spacings)
        mode_sp = sp_bins.most_common(1)[0][0]
        tight = [s for s in spacings if abs(s - mode_sp) <= 0.3]
        c2c = statistics.mean(tight) if tight else statistics.median(spacings)
        gap_raw = max(0, c2c - xheight_raw)
        gap_stroke = max(0, c2c - xheight_stroke)
        
        print(f"\n  Line spacings (c2c): {[round(s,3) for s in spacings]}")
        print(f"  Mode c2c: {c2c:.4f}mm")
        print(f"  Gap (RAW): {gap_raw:.4f}mm")
        print(f"  Gap (STROKE-ADJ): {gap_stroke:.4f}mm")
    else:
        gap_raw = None
        gap_stroke = None
        c2c = None
    
    # ---- RESULTS ----
    print(f"\n{'='*60}")
    print(f"RESULTS vs GROUND TRUTH")
    print(f"{'='*60}")
    
    font_err_raw = (xheight_raw - EXPECTED_FONT_MM) / EXPECTED_FONT_MM * 100
    print(f"  Font x-height (RAW): {xheight_raw:.4f}mm (expected {EXPECTED_FONT_MM}mm, error {font_err_raw:+.2f}%) {'✅' if abs(font_err_raw) <= 2 else '❌'}")
    
    font_err_stroke = (xheight_stroke - EXPECTED_FONT_MM) / EXPECTED_FONT_MM * 100
    print(f"  Font x-height (STROKE): {xheight_stroke:.4f}mm (expected {EXPECTED_FONT_MM}mm, error {font_err_stroke:+.2f}%) {'✅' if abs(font_err_stroke) <= 2 else '❌'}")
    
    if gap_raw is not None:
        gap_err_raw = (gap_raw - EXPECTED_GAP_MM) / EXPECTED_GAP_MM * 100
        print(f"  Line gap (RAW): {gap_raw:.4f}mm (expected {EXPECTED_GAP_MM}mm, error {gap_err_raw:+.2f}%) {'✅' if abs(gap_err_raw) <= 2 else '❌'}")
    
    if gap_stroke is not None:
        gap_err_stroke = (gap_stroke - EXPECTED_GAP_MM) / EXPECTED_GAP_MM * 100
        print(f"  Line gap (STROKE): {gap_stroke:.4f}mm (expected {EXPECTED_GAP_MM}mm, error {gap_err_stroke:+.2f}%) {'✅' if abs(gap_err_stroke) <= 2 else '❌'}")
    
    doc.close()
    return xheight_raw, xheight_stroke, gap_raw, gap_stroke


if __name__ == '__main__':
    analyze_vectors()
