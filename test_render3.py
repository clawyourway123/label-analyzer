#!/usr/bin/env python3
"""Use vector paths directly to get RENDERED visual heights by checking actual path fill."""
import fitz, statistics
from collections import Counter

doc = fitz.open("/Users/clawdy/Desktop/hazard_label_700ml.pdf")
page = doc.load_page(0)

# Let's examine a few specific characters carefully.
# Find lines around y=70mm (body text region)
drawings = page.get_drawings()

# Group paths by y_center, focusing on body text region (y 60-95mm range in pts)
y_min_pt = 60 / 25.4 * 72  # ~170pt
y_max_pt = 95 / 25.4 * 72  # ~269pt

body_paths = []
for d in drawings:
    r = d.get('rect')
    if not r: continue
    y_c = (r[1] + r[3]) / 2
    if y_min_pt < y_c < y_max_pt:
        h = r[3] - r[1]
        w = r[2] - r[0]
        h_mm = h / 72 * 25.4
        if 0.3 < h_mm < 5 and w > 0:
            sw = d.get('width', 0) or 0
            body_paths.append({
                'rect': r, 'h_pt': h, 'h_mm': h_mm, 
                'w_pt': w, 'stroke_w': sw,
                'y_top': r[1], 'y_bot': r[3], 'x': r[0],
                'y_center': y_c,
                'fill': d.get('fill'),
                'color': d.get('color'),
                'items': d.get('items', []),
            })

print(f"Body text paths: {len(body_paths)}")

# Let's look at a few characters in detail
# Group into lines
body_paths.sort(key=lambda p: p['y_center'])
lines = []
cur = [body_paths[0]]
cur_y = body_paths[0]['y_center']
for p in body_paths[1:]:
    if abs(p['y_center'] - cur_y) < 2.0:  # ~0.7mm tolerance
        cur.append(p)
        cur_y = sum(pp['y_center'] for pp in cur) / len(cur)
    else:
        if len(cur) >= 3: lines.append(cur)
        cur = [p]
        cur_y = p['y_center']
if len(cur) >= 3: lines.append(cur)

print(f"Lines in body region: {len(lines)}")

# For each line, group into characters and measure
for li, line in enumerate(lines[:15]):
    line.sort(key=lambda p: p['x'])
    chars = []
    cur_char = [line[0]]
    for p in line[1:]:
        cur_end = max(pp['x'] + pp['w_pt'] for pp in cur_char)
        if p['x'] < cur_end + 0.5:
            cur_char.append(p)
        else:
            chars.append(cur_char)
            cur_char = [p]
    chars.append(cur_char)
    
    # Measure character heights
    char_data = []
    for ch in chars:
        top = min(p['y_top'] for p in ch)
        bot = max(p['y_bot'] for p in ch)
        stroke_max = max(p['stroke_w'] for p in ch)
        raw_h = (bot - top) / 72 * 25.4
        vis_h = (bot - top + stroke_max) / 72 * 25.4
        vis_h_2x = (bot - top + 2 * stroke_max) / 72 * 25.4
        n_paths = len(ch)
        char_data.append((raw_h, vis_h, vis_h_2x, stroke_max, n_paths))
    
    raw_med = statistics.median([c[0] for c in char_data])
    vis_med = statistics.median([c[1] for c in char_data])
    vis2x_med = statistics.median([c[2] for c in char_data])
    y_mm = statistics.median([p['y_center'] for p in line]) / 72 * 25.4
    
    print(f"  Line {li} (y={y_mm:.2f}mm, {len(chars)} chars): raw={raw_med:.4f} 1x={vis_med:.4f} 2x={vis2x_med:.4f}mm")
    
    # Show height distribution for this line
    if li < 5:
        raw_bins = Counter(round(c[0], 2) for c in char_data)
        print(f"    Raw heights: {dict(sorted(raw_bins.items()))}")
        stroke_bins = Counter(round(c[3], 3) for c in char_data)
        print(f"    Strokes: {dict(stroke_bins.most_common(5))}")

doc.close()
