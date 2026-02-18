#!/usr/bin/env python3
"""Measure character height from rendered image - narrow column slice."""
import fitz, statistics, numpy as np
from collections import Counter

doc = fitz.open("/Users/clawdy/Desktop/hazard_label_700ml.pdf")
page = doc.load_page(0)
pw_mm = page.rect.width / 72 * 25.4

dpi = 600
dpmm = dpi / 25.4

# Narrow column in the middle of the page, body text region
x_center_mm = pw_mm / 2
clip_x_left = (x_center_mm - 5) / 25.4 * 72  # 10mm wide strip
clip_x_right = (x_center_mm + 5) / 25.4 * 72
clip_y_top = 60 / 25.4 * 72
clip_y_bot = 95 / 25.4 * 72
clip = fitz.Rect(clip_x_left, clip_y_top, clip_x_right, clip_y_bot)
pix = page.get_pixmap(dpi=dpi, clip=clip)
print(f"Clip: {pix.width}x{pix.height}px (10mm x 35mm)")

img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
gray = np.mean(img[:,:,:3], axis=2) if pix.n >= 3 else img[:,:,0].astype(float)
ink = gray < 128

row_ink = np.mean(ink, axis=1)
# Find rows
in_row = False; rows = []; row_start = 0
for i, d in enumerate(row_ink):
    if d > 0.005 and not in_row: row_start = i; in_row = True
    elif d <= 0.005 and in_row: rows.append((row_start, i)); in_row = False
if in_row: rows.append((row_start, len(row_ink)))

print(f"Rows: {len(rows)}")
for r in rows[:30]:
    h_mm = (r[1]-r[0])/dpmm
    y_mm = 60 + r[0]/dpmm
    print(f"  y={y_mm:.2f}mm h={h_mm:.3f}mm ({r[1]-r[0]}px)")

doc.close()
