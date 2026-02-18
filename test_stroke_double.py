#!/usr/bin/env python3
"""Test if stroke extends width/2 on EACH side (total = bbox + 2*stroke_width)."""
import fitz, statistics
from collections import Counter

PDF_PATH = "/Users/clawdy/Desktop/hazard_label_700ml.pdf"

doc = fitz.open(PDF_PATH)
page = doc.load_page(0)
drawings = page.get_drawings()

# Collect glyph-sized rects
glyphs = []
for d in drawings:
    r = d.get('rect')
    if not r: continue
    w, h = r[2]-r[0], r[3]-r[1]
    if w <= 0 or h <= 0: continue
    h_mm = h/72*25.4
    w_mm = w/72*25.4
    if 0.3 < h_mm < 5 and 0.05 < w_mm < 10:
        sw = d.get('width', 0) or 0
        glyphs.append({'h': h, 'h_mm': h_mm, 'stroke_w': sw, 'y_center': (r[1]+r[3])/2})

# Focus on the 1.08mm raw cluster (±0.05)
cluster_1_08 = [g for g in glyphs if abs(g['h_mm'] - 1.08) <= 0.05]
print(f"Paths in 1.08mm raw cluster: {len(cluster_1_08)}")

# What stroke widths do they have?
sw_counter = Counter(round(g['stroke_w'], 4) for g in cluster_1_08)
print(f"Stroke widths (pt): {sw_counter.most_common(10)}")

# Compute heights with 1x, 2x stroke
for mult_label, mult in [("1x stroke", 1), ("2x stroke", 2)]:
    heights = [(g['h'] + mult * g['stroke_w']) / 72 * 25.4 for g in cluster_1_08]
    bins = Counter(round(h, 2) for h in heights)
    print(f"\n{mult_label} - top heights:")
    for h, c in bins.most_common(10):
        print(f"  {h:.2f}mm: {c}")
    print(f"  Median: {statistics.median(heights):.4f}mm")

# Also check: what does the 1.19mm WITH-STROKE cluster look like in raw?
cluster_1_19 = [g for g in glyphs if abs(g['h_mm'] + g['stroke_w']/72*25.4 - 1.19) <= 0.03]
print(f"\nPaths that land at 1.19mm with 1x stroke: {len(cluster_1_19)}")
if cluster_1_19:
    raw_h = [g['h_mm'] for g in cluster_1_19]
    sw_vals = [g['stroke_w'] for g in cluster_1_19]
    print(f"  Raw heights: median={statistics.median(raw_h):.4f}, mean={statistics.mean(raw_h):.4f}")
    print(f"  Stroke widths: {Counter(round(s,4) for s in sw_vals).most_common(5)}")

# Check: raw heights that with 2x stroke land at 1.19mm
cluster_2x_119 = [g for g in glyphs if abs(g['h_mm'] + 2*g['stroke_w']/72*25.4 - 1.19) <= 0.03]
print(f"\nPaths that land at 1.19mm with 2x stroke: {len(cluster_2x_119)}")
if cluster_2x_119:
    raw_h = [g['h_mm'] for g in cluster_2x_119]
    sw_vals = [g['stroke_w'] for g in cluster_2x_119]
    print(f"  Raw heights: median={statistics.median(raw_h):.4f}")
    print(f"  Stroke widths: {Counter(round(s,4) for s in sw_vals).most_common(5)}")

doc.close()
