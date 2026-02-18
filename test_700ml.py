#!/usr/bin/env python3
"""
Test vector measurement against 700ml hazard label PDF (vector-only, no text layer).
Ground truth: font x-height=1.19mm, line gap=0.98mm
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
    
    # Collect all path rects
    all_rects = []
    for d in drawings:
        r = d.get('rect')
        if r:
            w = r[2] - r[0]
            h = r[3] - r[1]
            if w > 0 and h > 0:
                all_rects.append({
                    'rect': r, 'w': w, 'h': h,
                    'w_mm': w/72*25.4, 'h_mm': h/72*25.4,
                    'x': r[0], 'y_top': r[1], 'y_bot': r[3],
                    'y_center': (r[1]+r[3])/2
                })
    
    print(f"Rects with w>0, h>0: {len(all_rects)}")
    
    # Show height distribution for ALL rects
    all_h = [r['h_mm'] for r in all_rects]
    h_bins = Counter(round(h, 2) for h in all_h)
    print(f"\nHeight distribution (all, top 20):")
    for h, c in h_bins.most_common(20):
        print(f"  {h:.2f}mm: {c}")
    
    # Filter to glyph-sized elements
    glyphs = [r for r in all_rects if 0.3 < r['h_mm'] < 5 and 0.05 < r['w_mm'] < 10]
    print(f"\nGlyph-sized paths (0.3-5mm h, 0.05-10mm w): {len(glyphs)}")
    
    if not glyphs:
        print("No glyph paths found!")
        doc.close()
        return
    
    g_h = [g['h_mm'] for g in glyphs]
    g_bins = Counter(round(h, 2) for h in g_h)
    print(f"\nGlyph height distribution (top 20):")
    for h, c in g_bins.most_common(20):
        print(f"  {h:.2f}mm: {c}")
    
    # Cluster heights
    sorted_h = sorted(g_bins.keys())
    clusters = []
    for h in sorted_h:
        c = g_bins[h]
        merged = False
        for cl in clusters:
            if abs(h - cl[0]) <= 0.08:
                cl[1] += c
                cl[2].extend([h]*c)
                merged = True
                break
        if not merged:
            clusters.append([h, c, [h]*c])
    for cl in clusters:
        cl[0] = statistics.median(cl[2])
    
    peaks = sorted([(cl[0], cl[1]) for cl in clusters if cl[1] >= 3], key=lambda x: -x[1])
    print(f"\nClustered peaks (count≥3):")
    for h, c in peaks[:10]:
        print(f"  {h:.3f}mm: {c} paths")
    
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
    
    # Per-line stats
    line_medians = []
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
        
        char_heights = []
        for ch in chars:
            top = min(p['y_top'] for p in ch)
            bot = max(p['y_bot'] for p in ch)
            char_heights.append((bot - top) / 72 * 25.4)
        
        med = statistics.median(char_heights)
        line_medians.append(med)
        y_c = statistics.median([p['y_center'] for p in line]) / 72 * 25.4
        line_y_centers.append(y_c)
        
        if i < 30:
            print(f"  Line {i}: {len(chars)} chars, median_h={med:.3f}mm, min={min(char_heights):.2f}, max={max(char_heights):.2f}, y={y_c:.2f}mm")
    
    # Find body text lines (most common line height cluster)
    lm_bins = Counter(round(m, 1) for m in line_medians)
    best_bin = None
    best_count = 0
    for h_bin, cnt in lm_bins.items():
        total = cnt
        for n in [h_bin - 0.1, h_bin + 0.1]:
            total += lm_bins.get(round(n, 1), 0)
        if total > best_count:
            best_count = total
            best_bin = h_bin
    
    print(f"\nBody text line height cluster: {best_bin:.1f}mm ({best_count} lines)")
    
    body_indices = [i for i, m in enumerate(line_medians) if abs(m - best_bin) <= 0.3]
    body_chars = []
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
            body_chars.append((bot - top) / 72 * 25.4)
    
    print(f"Body text chars: {len(body_chars)}")
    
    # Height peaks in body text
    bc_bins = Counter(round(h, 2) for h in body_chars)
    bc_sorted = sorted(bc_bins.keys())
    bc_clusters = []
    for h in bc_sorted:
        c = bc_bins[h]
        merged = False
        for cl in bc_clusters:
            if abs(h - cl[0]) <= 0.08:
                cl[1] += c
                cl[2].extend([h]*c)
                merged = True
                break
        if not merged:
            bc_clusters.append([h, c, [h]*c])
    for cl in bc_clusters:
        cl[0] = statistics.median(cl[2])
    
    bc_peaks = sorted([(cl[0], cl[1]) for cl in bc_clusters if cl[1] >= 3], key=lambda x: -x[1])
    print(f"\nBody text height peaks:")
    for h, c in bc_peaks[:8]:
        print(f"  {h:.3f}mm: {c} chars")
    
    # Bimodal: most frequent = x-height
    if bc_peaks:
        xheight_mm = bc_peaks[0][0]
        xh_count = bc_peaks[0][1]
        
        capheight_mm = None
        for h, c in bc_peaks[1:]:
            if abs(h - xheight_mm) > 0.15 and c >= 10:
                capheight_mm = h
                break
        
        print(f"\n  X-height (most frequent): {xheight_mm:.4f}mm ({xh_count} chars)")
        if capheight_mm:
            print(f"  Cap-height: {capheight_mm:.4f}mm")
            print(f"  Ratio: {capheight_mm/xheight_mm:.3f}")
    else:
        xheight_mm = statistics.median(body_chars)
        print(f"\n  Fallback median: {xheight_mm:.4f}mm")
    
    # Line spacing
    body_ys = sorted([line_y_centers[i] for i in body_indices])
    if len(body_ys) >= 2:
        spacings = [body_ys[i+1] - body_ys[i] for i in range(len(body_ys)-1)]
        sp_bins = Counter(round(s, 1) for s in spacings)
        mode_sp = sp_bins.most_common(1)[0][0]
        tight = [s for s in spacings if abs(s - mode_sp) <= 0.3]
        c2c = statistics.mean(tight) if tight else statistics.median(spacings)
        gap = max(0, c2c - xheight_mm)
        
        print(f"\n  Line spacings (c2c): {[round(s,3) for s in spacings]}")
        print(f"  Mode c2c: {c2c:.4f}mm")
        print(f"  Gap (c2c - xheight): {gap:.4f}mm")
    else:
        gap = None
        c2c = None
    
    # ---- RESULTS ----
    print(f"\n{'='*60}")
    print(f"RESULTS vs GROUND TRUTH")
    print(f"{'='*60}")
    
    font_err = (xheight_mm - EXPECTED_FONT_MM) / EXPECTED_FONT_MM * 100
    print(f"  Font x-height: {xheight_mm:.4f}mm (expected {EXPECTED_FONT_MM}mm, error {font_err:+.2f}%) {'✅' if abs(font_err) <= 2 else '❌'}")
    
    if gap is not None:
        gap_err = (gap - EXPECTED_GAP_MM) / EXPECTED_GAP_MM * 100
        print(f"  Line gap:      {gap:.4f}mm (expected {EXPECTED_GAP_MM}mm, error {gap_err:+.2f}%) {'✅' if abs(gap_err) <= 2 else '❌'}")
    
    doc.close()
    return xheight_mm, gap


if __name__ == '__main__':
    analyze_vectors()
