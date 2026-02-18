#!/usr/bin/env python3
"""Simulate production code: crop to hazard region, run body text clustering."""
import fitz
from collections import Counter
import statistics

PDF_PATH = "/Users/clawdy/Desktop/hazard_label_700ml.pdf"
doc = fitz.open(PDF_PATH)
page = doc.load_page(0)
drawings = page.get_drawings()

# Page dimensions
pw_mm = page.rect.width / 72 * 25.4
ph_mm = page.rect.height / 72 * 25.4
print(f"Page: {pw_mm:.1f} x {ph_mm:.1f} mm")

# Simulate various crop regions and see what peak wins
regions = [
    ("Full page", 0, 0, pw_mm, ph_mm),
    ("Upper half", 0, 0, pw_mm, ph_mm/2),
    ("CLP broad (y=60-300)", 0, 60, pw_mm, 300),
    ("CLP hazard only (y=215-255)", 0, 215, pw_mm, 255),
    ("CLP lower (y=250-300)", 0, 250, pw_mm, 300),
    ("CLP all text (y=215-300)", 0, 215, pw_mm, 300),
]

for name, xmin_mm, ymin_mm, xmax_mm, ymax_mm in regions:
    # Convert to pts
    xmin = xmin_mm / 25.4 * 72
    ymin = ymin_mm / 25.4 * 72
    xmax = xmax_mm / 25.4 * 72
    ymax = ymax_mm / 25.4 * 72
    
    rects = []
    for d in drawings:
        r = d.get('rect')
        if not r: continue
        if r[0] < xmin - 2 or r[2] > xmax + 2 or r[1] < ymin - 2 or r[3] > ymax + 2:
            continue
        w = r[2] - r[0]; h = r[3] - r[1]
        if w <= 0 or h <= 0: continue
        stroke = d.get('width', 0) or 0
        h_mm = (h + stroke) / 72 * 25.4
        w_mm = w / 72 * 25.4
        if 0.3 < h_mm < 5 and 0.05 < w_mm < 10:
            rects.append({
                'h_mm': h_mm, 'y_center': (r[1]+r[3])/2,
                'x': r[0], 'x_end': r[2]
            })
    
    if len(rects) < 10:
        print(f"\n{name}: only {len(rects)} glyphs, skipping")
        continue
    
    # Group into lines
    rects.sort(key=lambda r: r['y_center'])
    common_h_pt = Counter(round(r['h_mm']/25.4*72, 1) for r in rects).most_common(1)[0][0]
    tol = max(0.8, common_h_pt * 0.4)
    
    lines = []
    cur = [rects[0]]
    cur_y = rects[0]['y_center']
    for r in rects[1:]:
        if abs(r['y_center'] - cur_y) < tol:
            cur.append(r)
            cur_y = sum(p['y_center'] for p in cur) / len(cur)
        else:
            if len(cur) >= 3:
                lines.append(cur)
            cur = [r]
            cur_y = r['y_center']
    if len(cur) >= 3:
        lines.append(cur)
    
    # Line medians
    line_meds = []
    for line in lines:
        line.sort(key=lambda g: g['x'])
        chars = []
        cc = [line[0]]
        for g in line[1:]:
            if g['x'] < max(p['x_end'] for p in cc) + 0.5:
                cc.append(g)
            else:
                chars.append(cc)
                cc = [g]
        chars.append(cc)
        ch_heights = []
        for ch in chars:
            ch_heights.append(statistics.median([p['h_mm'] for p in ch]))
        line_meds.append(statistics.median(ch_heights))
    
    # Cluster lines by height
    lm_bins = Counter(round(m, 1) for m in line_meds)
    best_bin = max(lm_bins, key=lambda h: sum(lm_bins.get(round(h+d*0.1, 1), 0) for d in [-1,0,1]))
    
    body_idx = [i for i, m in enumerate(line_meds) if abs(m - best_bin) <= 0.3]
    body_heights = []
    for i in body_idx:
        line = lines[i]
        line.sort(key=lambda g: g['x'])
        chars = []
        cc = [line[0]]
        for g in line[1:]:
            if g['x'] < max(p['x_end'] for p in cc) + 0.5:
                cc.append(g)
            else:
                chars.append(cc)
                cc = [g]
        chars.append(cc)
        for ch in chars:
            body_heights.append(statistics.median([p['h_mm'] for p in ch]))
    
    # Cluster body heights
    hb = Counter(round(h, 2) for h in body_heights)
    sh = sorted(hb.keys())
    clusters = []
    for h in sh:
        c = hb[h]
        merged = False
        for cl in clusters:
            if abs(h - cl[0]) <= 0.05:
                cl[1] += c
                cl[2].extend([h]*c)
                merged = True
                break
        if not merged:
            clusters.append([h, c, [h]*c])
    for cl in clusters:
        cl[0] = statistics.median(cl[2])
    peaks = sorted([(cl[0], cl[1]) for cl in clusters if cl[1] >= 3], key=lambda x: -x[1])
    
    xh = peaks[0][0] if peaks else 0
    err = (xh - 1.19) / 1.19 * 100
    
    print(f"\n{name}: {len(rects)} glyphs, {len(lines)} lines, body_cluster={best_bin:.1f}mm ({len(body_idx)} lines)")
    print(f"  Top peaks: {[(f'{h:.3f}mm', c) for h, c in peaks[:5]]}")
    print(f"  Selected x-height: {xh:.3f}mm (err vs 1.19mm: {err:+.1f}%)")

doc.close()
