#!/usr/bin/env python3
"""Check what lines exist in the CLP hazard region (y=210-300mm)."""
import fitz
from collections import Counter
import statistics

PDF_PATH = "/Users/clawdy/Desktop/hazard_label_700ml.pdf"
doc = fitz.open(PDF_PATH)
page = doc.load_page(0)
drawings = page.get_drawings()

rects = []
for d in drawings:
    r = d.get('rect')
    if not r: continue
    w = r[2] - r[0]; h = r[3] - r[1]
    if w <= 0 or h <= 0: continue
    stroke = d.get('width', 0) or 0
    h_mm = (h + stroke) / 72 * 25.4
    w_mm = w / 72 * 25.4
    if 0.3 < h_mm < 5 and 0.05 < w_mm < 10:
        rects.append({
            'h_mm': h_mm, 'h_raw_mm': h/72*25.4,
            'y_center_mm': ((r[1]+r[3])/2)/72*25.4,
            'x_mm': r[0]/72*25.4, 'stroke': stroke
        })

# Group into lines by y_center
rects.sort(key=lambda r: r['y_center_mm'])
lines = []
cur = [rects[0]]
for r in rects[1:]:
    if abs(r['y_center_mm'] - statistics.mean([x['y_center_mm'] for x in cur])) < 0.5:
        cur.append(r)
    else:
        if len(cur) >= 3:
            lines.append(cur)
        cur = [r]
if len(cur) >= 3:
    lines.append(cur)

print(f"Total lines: {len(lines)}")
print(f"\nLines in CLP region (y=210-300mm):")
for line in lines:
    y = statistics.median([r['y_center_mm'] for r in line])
    if 210 <= y <= 300:
        heights_stroke = [r['h_mm'] for r in line]
        med = statistics.median(heights_stroke)
        print(f"  y={y:.1f}mm: {len(line)} glyphs, median_h={med:.3f}mm (stroke-adj)")

# Now: what if we ONLY measure CLP region?
print(f"\n\n--- CLP region only (y=210-300mm) height distribution ---")
clp_glyphs = [r for r in rects if 210 <= r['y_center_mm'] <= 300]
h_bins = Counter(round(r['h_mm'], 2) for r in clp_glyphs)
print(f"Total CLP glyphs: {len(clp_glyphs)}")
for h, c in h_bins.most_common(10):
    print(f"  {h:.2f}mm: {c}")

# Cluster
sorted_h = sorted(h_bins.keys())
clusters = []
for h in sorted_h:
    c = h_bins[h]
    merged = False
    for cl in clusters:
        if abs(h - cl[0]) <= 0.05:
            cl[1] += c
            cl[2].extend([h]*c)
            merged = True
            break
        if not merged:
            pass
    if not merged:
        clusters.append([h, c, [h]*c])
for cl in clusters:
    cl[0] = statistics.median(cl[2])
peaks = sorted([(cl[0], cl[1]) for cl in clusters if cl[1] >= 3], key=lambda x: -x[1])
print(f"\nCLP region peaks (±0.05mm clusters):")
for h, c in peaks[:8]:
    pct_err = (h - 1.19) / 1.19 * 100
    print(f"  {h:.3f}mm: {c} glyphs (err vs 1.19mm: {pct_err:+.1f}%)")

doc.close()
