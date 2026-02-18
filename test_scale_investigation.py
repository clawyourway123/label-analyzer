#!/usr/bin/env python3
"""Investigate scale factor impact and peak selection for font measurement."""

import sys
import os
sys.path.insert(0, '/Users/clawdy/Desktop/label-analyzer')

import fitz
import statistics
from collections import Counter
import json

PDF_PATH = '/Users/clawdy/Desktop/hazard_label_700ml.pdf'

def analyze_raw_vectors(pdf_path):
    """Extract raw vector heights without any scale factor."""
    doc = fitz.open(pdf_path)
    page = doc.load_page(0)
    
    page_rect = page.rect
    print(f"Page size: {page_rect.width:.2f} x {page_rect.height:.2f} pts")
    print(f"Page size mm: {page_rect.width/72*25.4:.2f} x {page_rect.height/72*25.4:.2f} mm")
    
    # Get all drawings
    drawings = page.get_drawings()
    print(f"\nTotal drawings: {len(drawings)}")
    
    # Find measurement/dimension lines (long horizontal or vertical lines)
    long_h_lines = []
    long_v_lines = []
    for d in drawings:
        for item in d.get('items', []):
            if item[0] == 'l':
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 2:  # horizontal
                    length_mm = abs(p2.x - p1.x) / 72 * 25.4
                    if length_mm > 50:
                        long_h_lines.append((length_mm, p1, p2))
                elif abs(p1.x - p2.x) < 2:  # vertical
                    length_mm = abs(p2.y - p1.y) / 72 * 25.4
                    if length_mm > 50:
                        long_v_lines.append((length_mm, p1, p2))
    
    print(f"\nLong horizontal lines (>50mm): {len(long_h_lines)}")
    for l, p1, p2 in sorted(long_h_lines, key=lambda x: -x[0])[:5]:
        print(f"  {l:.2f}mm at y={p1.y:.1f}pt")
    
    print(f"\nLong vertical lines (>50mm): {len(long_v_lines)}")
    for l, p1, p2 in sorted(long_v_lines, key=lambda x: -x[0])[:5]:
        print(f"  {l:.2f}mm at x={p1.x:.1f}pt")
    
    # Get text blocks to find hazard text regions
    text_dict = page.get_text("dict")
    print(f"\n--- Text blocks analysis ---")
    for block in text_dict.get("blocks", []):
        if block.get("type") == 0:  # text block
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if text and len(text) > 3:
                        size_pt = span.get("size", 0)
                        size_mm = size_pt / 72 * 25.4
                        font = span.get("font", "")
                        # Only show hazard-related or significant text
                        if any(kw in text.upper() for kw in ['DANGER', 'WARNING', 'HAZARD', 'H3', 'P2', 'EUH']):
                            print(f"  '{text[:50]}' font={font} size={size_pt:.2f}pt ({size_mm:.3f}mm)")
    
    # Now analyze vector path heights for ALL text
    print(f"\n--- Vector path height analysis (full page) ---")
    all_heights_mm = []
    for d in drawings:
        rect = d.get('rect')
        if rect is None:
            continue
        h_pts = rect.height
        h_mm = h_pts / 72 * 25.4
        if 0.3 < h_mm < 5.0 and rect.width / 72 * 25.4 < 5.0:  # glyph-sized
            all_heights_mm.append(round(h_mm, 3))
    
    print(f"Total glyph-sized paths: {len(all_heights_mm)}")
    
    # Histogram
    height_bins = Counter(round(h, 2) for h in all_heights_mm)
    print(f"\nHeight histogram (top 15):")
    for h, c in height_bins.most_common(15):
        bar = '#' * min(c, 60)
        print(f"  {h:.2f}mm: {c:4d} {bar}")
    
    # Cluster analysis
    sorted_heights = sorted(height_bins.keys())
    clusters = []
    for h in sorted_heights:
        count = height_bins[h]
        merged = False
        for cluster in clusters:
            if abs(h - cluster[0]) <= 0.05:
                cluster[1] += count
                cluster[2].extend([h] * count)
                merged = True
                break
        if not merged:
            clusters.append([h, count, [h] * count])
    
    for cluster in clusters:
        cluster[0] = statistics.median(cluster[2])
    
    peaks = [(c[0], c[1]) for c in clusters if c[1] >= 3]
    peaks.sort(key=lambda x: -x[1])
    
    print(f"\nClusters (peaks with ≥3 chars):")
    for i, (h, c) in enumerate(peaks[:10]):
        print(f"  Peak {i+1}: {h:.3f}mm ({c} chars)")
    
    # Check what CLP x-height ratio would give from cap heights
    print(f"\n--- X-height estimation from cap-height peaks ---")
    for h, c in peaks[:5]:
        if c >= 10:
            for ratio in [0.70, 0.75, 0.80, 0.85]:
                est = h * ratio
                print(f"  Cap={h:.3f}mm * {ratio} = x-height {est:.3f}mm")
    
    # Also check text extraction for font sizes
    print(f"\n--- PyMuPDF text spans (font sizes) ---")
    size_counter = Counter()
    for block in text_dict.get("blocks", []):
        if block.get("type") == 0:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    size_pt = span.get("size", 0)
                    if size_pt > 0:
                        size_mm = round(size_pt / 72 * 25.4, 3)
                        text = span.get("text", "").strip()
                        if text:
                            size_counter[(size_mm, span.get("font", ""))] += len(text)
    
    print("Font sizes found:")
    for (size_mm, font), count in size_counter.most_common(10):
        print(f"  {size_mm:.3f}mm ({font}): {count} chars")
    
    doc.close()

if __name__ == '__main__':
    analyze_raw_vectors(PDF_PATH)
