"""
Quick diagnostic: run on any PDF to show font measurement details.
Usage: python diagnose_pdf.py <pdf_path> [xmin_mm ymin_mm xmax_mm ymax_mm]

If no region given, analyzes the full page.
"""
import sys, fitz, statistics
from collections import Counter

pdf_path = sys.argv[1]
doc = fitz.open(pdf_path)
page = doc.load_page(0)

print(f"Page: {page.rect.width:.1f} x {page.rect.height:.1f} pts")
print(f"Page: {page.rect.width/72*25.4:.2f} x {page.rect.height/72*25.4:.2f} mm")

# Find measurement reference lines (longest horizontal/vertical lines)
drawings = page.get_drawings()
horiz = []
for d in drawings:
    for item in d['items']:
        if item[0] == 'l':
            p1, p2 = item[1], item[2]
            if abs(p1.y - p2.y) < 2:
                length = abs(p2.x - p1.x)
                if length > 100:
                    horiz.append((length/72*25.4, p1.y/72*25.4))

horiz.sort(key=lambda x: -x[0])
print(f"\nTop 10 horizontal lines (for scale check):")
for i, (mm, y) in enumerate(horiz[:10]):
    print(f"  {i+1}. {mm:.2f}mm at y={y:.1f}mm")

# Region analysis
if len(sys.argv) >= 6:
    xmin_mm, ymin_mm, xmax_mm, ymax_mm = [float(x) for x in sys.argv[2:6]]
    pt_xmin, pt_ymin = xmin_mm/25.4*72, ymin_mm/25.4*72
    pt_xmax, pt_ymax = xmax_mm/25.4*72, ymax_mm/25.4*72
    print(f"\nAnalyzing region: ({xmin_mm},{ymin_mm})-({xmax_mm},{ymax_mm})mm")
else:
    pt_xmin, pt_ymin = page.rect.x0, page.rect.y0
    pt_xmax, pt_ymax = page.rect.x1, page.rect.y1
    print(f"\nAnalyzing full page")

# Extract paths in region
paths = []
for d in drawings:
    r = d.get('rect')
    if r:
        w, h = r[2]-r[0], r[3]-r[1]
        if (r[0] >= pt_xmin-2 and r[2] <= pt_xmax+2 and
            r[1] >= pt_ymin-2 and r[3] <= pt_ymax+2 and
            0.3 < h < 20 and 0.1 < w < 30):
            paths.append({'y_top': r[1], 'y_bot': r[3], 'h': h,
                         'x': r[0], 'x_end': r[2], 'y_center': (r[1]+r[3])/2})

print(f"Glyph paths found: {len(paths)}")

# Height distribution
h_dist = Counter(round(p['h']/72*25.4, 2) for p in paths)
print(f"\nPath height distribution (mm):")
for h, c in sorted(h_dist.items()):
    if c >= 3:
        bar = '#' * min(c, 50)
        print(f"  {h:.2f}mm: {bar} ({c})")

# Group into lines
common_h = Counter(round(g['h'], 1) for g in paths).most_common(1)[0][0]
tol = max(0.8, common_h * 0.4)
paths.sort(key=lambda g: g['y_center'])
lines = []
cur = [paths[0]]; cur_mean = paths[0]['y_center']
for g in paths[1:]:
    if abs(g['y_center'] - cur_mean) < tol:
        cur.append(g)
        cur_mean = sum(p['y_center'] for p in cur) / len(cur)
    else:
        if len(cur) >= 3: lines.append(cur)
        cur = [g]; cur_mean = g['y_center']
if len(cur) >= 3: lines.append(cur)

print(f"\nText lines: {len(lines)} (tolerance={tol:.1f}pt)")

# Per-line detail: group into chars and show heights
for i, line in enumerate(lines[:15]):
    line.sort(key=lambda g: g['x'])
    chars = []
    cur_ch = [line[0]]
    for g in line[1:]:
        if g['x'] < max(p['x_end'] for p in cur_ch) + 0.5:
            cur_ch.append(g)
        else:
            chars.append(cur_ch)
            cur_ch = [g]
    chars.append(cur_ch)
    
    ch_heights = []
    for ch in chars:
        t = min(p['y_top'] for p in ch)
        b = max(p['y_bot'] for p in ch)
        ch_heights.append((b-t)/72*25.4)
    
    y_mm = statistics.median([p['y_center'] for p in line])/72*25.4
    mean_h = statistics.mean(ch_heights)
    med_h = statistics.median(ch_heights)
    h_counts = Counter(round(h, 1) for h in ch_heights)
    
    print(f"  Line {i+1} (y={y_mm:.1f}mm): {len(chars)} chars, mean={mean_h:.2f}mm, median={med_h:.2f}mm, heights={h_counts.most_common(3)}")

# Also try wider character grouping (tolerance = 2pt instead of 0.5pt)
print(f"\n--- With wider char grouping (2pt tolerance) ---")
for i, line in enumerate(lines[:5]):
    line.sort(key=lambda g: g['x'])
    chars = []
    cur_ch = [line[0]]
    for g in line[1:]:
        if g['x'] < max(p['x_end'] for p in cur_ch) + 2.0:  # wider tolerance
            cur_ch.append(g)
        else:
            chars.append(cur_ch)
            cur_ch = [g]
    chars.append(cur_ch)
    
    ch_heights = [(max(p['y_bot'] for p in ch)-min(p['y_top'] for p in ch))/72*25.4 for ch in chars]
    mean_h = statistics.mean(ch_heights)
    med_h = statistics.median(ch_heights)
    h_counts = Counter(round(h, 1) for h in ch_heights)
    print(f"  Line {i+1}: {len(chars)} chars, mean={mean_h:.2f}mm, median={med_h:.2f}mm, heights={h_counts.most_common(3)}")

doc.close()
