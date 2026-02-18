#!/usr/bin/env python3
"""Measure character height from rendered image at 600 DPI."""
import fitz
import statistics
from collections import Counter
import numpy as np

doc = fitz.open("/Users/clawdy/Desktop/hazard_label_700ml.pdf")
page = doc.load_page(0)
pw_mm = page.rect.width / 72 * 25.4
ph_mm = page.rect.height / 72 * 25.4
print(f"Page: {pw_mm:.2f} x {ph_mm:.2f} mm")

dpi = 600
dpmm = dpi / 25.4
print(f"Rendering at {dpi} DPI ({dpmm:.2f} px/mm)")
print(f"1.19mm = {1.19*dpmm:.1f}px, 1.08mm = {1.08*dpmm:.1f}px")

# Only render the body text region to save memory
# Body text is roughly y=60mm to y=95mm
clip_y_top_pt = 60 / 25.4 * 72
clip_y_bot_pt = 95 / 25.4 * 72
clip = fitz.Rect(0, clip_y_top_pt, page.rect.width, clip_y_bot_pt)
pix = page.get_pixmap(dpi=dpi, clip=clip)
print(f"Clip rendered: {pix.width}x{pix.height}px")

samples = pix.samples
img = np.frombuffer(samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
if pix.n >= 3:
    gray = np.mean(img[:,:,:3], axis=2)
else:
    gray = img[:,:,0].astype(float)

ink = gray < 128
row_ink = np.mean(ink, axis=1)

# Find text rows
in_row = False
rows = []
row_start = 0
for i, density in enumerate(row_ink):
    if density > 0.005 and not in_row:
        row_start = i
        in_row = True
    elif density <= 0.005 and in_row:
        rows.append((row_start, i))
        in_row = False
if in_row:
    rows.append((row_start, len(row_ink)))

print(f"\nText rows found: {len(rows)}")
row_heights_mm = [(r[1]-r[0]) / dpmm for r in rows]

# Filter to reasonable text rows (0.5-3mm)
text_rows = [(r, h) for r, h in zip(rows, row_heights_mm) if 0.5 < h < 3.0]
print(f"Text-sized rows (0.5-3mm): {len(text_rows)}")

bins = Counter(round(h, 2) for _, h in text_rows)
print(f"\nRow height distribution:")
for h, c in sorted(bins.items()):
    marker = ""
    if abs(h - 1.19) < 0.05: marker = " <-- 1.19mm target"
    if abs(h - 1.08) < 0.05: marker = " <-- 1.08mm (old)"
    if abs(h - 1.14) < 0.05: marker = " <-- 1.14mm (stroke 1x)"
    print(f"  {h:.2f}mm: {c}{marker}")

# The rendered row heights ARE the visual truth
body_heights = [h for _, h in text_rows if 0.8 < h < 1.8]
if body_heights:
    print(f"\nBody text row heights (0.8-1.8mm): {len(body_heights)}")
    h_bins = Counter(round(h, 1) for h in body_heights)
    for h, c in h_bins.most_common(10):
        print(f"  {h:.1f}mm: {c} rows")
    print(f"  Median: {statistics.median(body_heights):.4f}mm")
    print(f"  Mean: {statistics.mean(body_heights):.4f}mm")

# C2C spacing
if len(text_rows) >= 2:
    spacings = []
    for i in range(len(text_rows)-1):
        s = (text_rows[i+1][0][0] - text_rows[i][0][0]) / dpmm
        spacings.append(s)
    body_sp = [s for s in spacings if 1.5 < s < 3.0]
    if body_sp:
        print(f"\nBody text c2c: median={statistics.median(body_sp):.4f}mm")

doc.close()
