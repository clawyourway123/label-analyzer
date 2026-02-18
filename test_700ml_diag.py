#!/usr/bin/env python3
"""Diagnose WHERE the 1.19mm glyphs are on the page."""
import fitz
from collections import Counter
import statistics

PDF_PATH = "/Users/clawdy/Desktop/hazard_label_700ml.pdf"

doc = fitz.open(PDF_PATH)
page = doc.load_page(0)
drawings = page.get_drawings()

# All rects with stroke adjustment
rects = []
for d in drawings:
    r = d.get('rect')
    if not r: continue
    w = r[2] - r[0]
    h = r[3] - r[1]
    if w <= 0 or h <= 0: continue
    stroke = d.get('width', 0) or 0
    h_mm = (h + stroke) / 72 * 25.4
    if 0.3 < h_mm < 5 and 0.05 < (w/72*25.4) < 10:
        rects.append({
            'h_mm': h_mm, 'h_raw_mm': h/72*25.4,
            'y_top': r[1]/72*25.4, 'y_bot': r[3]/72*25.4,
            'x': r[0]/72*25.4, 'stroke': stroke,
            'h_pt': h, 'stroke_pt': stroke
        })

# Find glyphs in the 1.19mm range (with stroke)
target_glyphs = [r for r in rects if 1.17 <= r['h_mm'] <= 1.21]
print(f"Glyphs with h_mm in [1.17, 1.21]: {len(target_glyphs)}")

if target_glyphs:
    y_positions = [r['y_top'] for r in target_glyphs]
    print(f"  Y range: {min(y_positions):.1f}mm - {max(y_positions):.1f}mm")
    print(f"  X range: {min(r['x'] for r in target_glyphs):.1f}mm - {max(r['x'] for r in target_glyphs):.1f}mm")
    
    # Y distribution
    y_bins = Counter(round(y, 0) for y in y_positions)
    print(f"\n  Y distribution (1.19mm glyphs):")
    for y, c in sorted(y_bins.items()):
        print(f"    y≈{y:.0f}mm: {c} glyphs")
    
    # What are their raw heights and strokes?
    raw_bins = Counter(round(r['h_raw_mm'], 2) for r in target_glyphs)
    print(f"\n  Raw heights of these glyphs:")
    for h, c in raw_bins.most_common(10):
        print(f"    {h:.2f}mm raw: {c}")
    
    stroke_bins = Counter(round(r['stroke_pt'], 3) for r in target_glyphs)
    print(f"\n  Stroke widths:")
    for s, c in stroke_bins.most_common(10):
        print(f"    {s:.3f}pt: {c}")

# Now also: what's in the 1.08mm raw range?
raw_108 = [r for r in rects if 1.06 <= r['h_raw_mm'] <= 1.10]
print(f"\n\nGlyphs with h_raw in [1.06, 1.10]: {len(raw_108)}")
if raw_108:
    y_positions = [r['y_top'] for r in raw_108]
    print(f"  Y range: {min(y_positions):.1f}mm - {max(y_positions):.1f}mm")

# Check: what does 1.19mm = in points?
print(f"\n\n1.19mm = {1.19/25.4*72:.3f}pt")
print(f"1.08mm = {1.08/25.4*72:.3f}pt")

# Look at the TEXT LAYER for comparison
print(f"\n\n--- TEXT LAYER (get_text with details) ---")
blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
font_sizes = []
for b in blocks:
    if "lines" not in b: continue
    for l in b["lines"]:
        for s in l["spans"]:
            sz = s["size"]
            txt = s["text"].strip()
            if txt:
                font_sizes.append((sz, txt[:40]))

sz_bins = Counter(round(s[0], 2) for s in font_sizes)
print(f"Font sizes in text layer:")
for sz, c in sz_bins.most_common(15):
    print(f"  {sz:.2f}pt ({sz/72*25.4:.3f}mm): {c} spans")
    # Show a sample
    samples = [s[1] for s in font_sizes if round(s[0], 2) == sz][:2]
    for samp in samples:
        print(f"    → \"{samp}\"")

doc.close()
