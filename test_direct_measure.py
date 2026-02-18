#!/usr/bin/env python3
"""
Direct approach: render the PDF at very high DPI, find text rows,
measure pixel heights, convert to mm.

This bypasses all vector extraction and measures what you'd SEE.
"""
import fitz
import statistics
from collections import Counter

doc = fitz.open("/Users/clawdy/Desktop/hazard_label_700ml.pdf")
page = doc.load_page(0)

# Page size in mm
pw_mm = page.rect.width / 72 * 25.4
ph_mm = page.rect.height / 72 * 25.4
print(f"Page: {pw_mm:.2f} x {ph_mm:.2f} mm")

# Render at 1200 DPI for precision
dpi = 1200
pix = page.get_pixmap(dpi=dpi)
print(f"Rendered: {pix.width} x {pix.height} px at {dpi} DPI")
dpmm = dpi / 25.4
print(f"Resolution: {dpmm:.2f} px/mm")
print(f"1.19mm = {1.19 * dpmm:.1f} px, 1.08mm = {1.08 * dpmm:.1f} px")

# Save for manual inspection
pix.save("/Users/clawdy/Desktop/label-analyzer/test_render_1200dpi.png")
print("Saved render to test_render_1200dpi.png")

# Now let's also look at specific vector paths and their ACTUAL rendered size
# by checking what 1 path height looks like in pixels
drawings = page.get_drawings()
print(f"\nTotal drawings: {len(drawings)}")

# Find paths in the body text range and check their actual rendered heights
# Take a sample of paths with raw height ~1.08mm
sample_paths = []
for d in drawings:
    r = d.get('rect')
    if not r: continue
    h = r[3] - r[1]
    h_mm = h / 72 * 25.4
    if abs(h_mm - 1.08) < 0.02:
        sw = d.get('width', 0) or 0
        sample_paths.append({
            'rect': r, 'h_pt': h, 'h_mm': h_mm, 'stroke_w': sw,
            'y_top_mm': r[1]/72*25.4, 'y_bot_mm': r[3]/72*25.4
        })
        if len(sample_paths) >= 5:
            break

print(f"\nSample paths at ~1.08mm raw height:")
for i, p in enumerate(sample_paths):
    sw_mm = p['stroke_w'] / 72 * 25.4
    visual_1x = p['h_mm'] + sw_mm
    visual_2x = p['h_mm'] + 2 * sw_mm
    print(f"  Path {i}: raw={p['h_mm']:.4f}mm stroke={p['stroke_w']:.4f}pt={sw_mm:.4f}mm")
    print(f"    1x stroke: {visual_1x:.4f}mm")
    print(f"    2x stroke: {visual_2x:.4f}mm")
    # What the rendered pixel height would be
    y_top_px = p['rect'][1] / 72 * dpi
    y_bot_px = p['rect'][3] / 72 * dpi
    print(f"    Rendered: top={y_top_px:.1f}px bot={y_bot_px:.1f}px span={y_bot_px-y_top_px:.1f}px = {(y_bot_px-y_top_px)/dpmm:.4f}mm")

# Let's try a completely different approach: measure using the RENDERED image
# Scan columns of the rendered image to find ink extent
import numpy as np

# Convert pixmap to numpy array
samples = pix.samples
img = np.frombuffer(samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)

# Convert to grayscale if needed
if pix.n >= 3:
    gray = np.mean(img[:, :, :3], axis=2)
else:
    gray = img[:, :, 0].astype(float)

# Threshold to find ink (dark pixels)
threshold = 128
ink = gray < threshold

# Find the dense body text region (lots of ink between y=65mm and y=90mm)
y_start_px = int(65 * dpmm)
y_end_px = int(95 * dpmm)
x_start_px = int(5 * dpmm)  # skip margins
x_end_px = int(pw_mm * dpmm * 0.95)

region = ink[y_start_px:y_end_px, x_start_px:x_end_px]
print(f"\nBody text region: y={65}-{95}mm, ink pixels: {np.sum(region)}")

# Row-by-row ink density
row_ink = np.mean(region, axis=1)

# Find text rows: contiguous runs of rows with ink density > threshold
ink_threshold = 0.01
in_row = False
rows = []
row_start = 0
for i, density in enumerate(row_ink):
    if density > ink_threshold and not in_row:
        row_start = i
        in_row = True
    elif density <= ink_threshold and in_row:
        rows.append((row_start, i))
        in_row = False
if in_row:
    rows.append((row_start, len(row_ink)))

print(f"Text rows found: {len(rows)}")
row_heights_px = [r[1] - r[0] for r in rows]
row_heights_mm = [h / dpmm for h in row_heights_px]

# Show all row heights
bins = Counter(round(h, 2) for h in row_heights_mm)
print(f"Row height distribution (mm):")
for h, c in sorted(bins.items()):
    if c >= 1:
        print(f"  {h:.2f}mm: {c} rows {'<-- target' if abs(h - 1.19) < 0.05 else ''}")

# The row heights should be close to the visual character height
print(f"\nRow heights (mm): {[f'{h:.3f}' for h in row_heights_mm[:30]]}")

# Line spacing from row starts
if len(rows) >= 2:
    spacings_px = [rows[i+1][0] - rows[i][0] for i in range(len(rows)-1)]
    spacings_mm = [s / dpmm for s in spacings_px]
    # Filter to body text spacing (~2mm)
    body_spacings = [s for s in spacings_mm if 1.5 < s < 3.0]
    if body_spacings:
        c2c = statistics.median(body_spacings)
        print(f"\nBody text c2c spacing: {c2c:.4f}mm")

doc.close()
