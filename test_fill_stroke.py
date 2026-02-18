#!/usr/bin/env python3
"""Check if body text paths are filled, stroked, or both."""
import fitz
from collections import Counter

doc = fitz.open("/Users/clawdy/Desktop/hazard_label_700ml.pdf")
page = doc.load_page(0)
drawings = page.get_drawings()

y_min = 60/25.4*72; y_max = 95/25.4*72
body = [d for d in drawings if d.get('rect') and y_min < (d['rect'][1]+d['rect'][3])/2 < y_max]

fill_stroke = Counter()
for d in body:
    has_fill = d.get('fill') is not None
    has_color = d.get('color') is not None  # stroke color
    sw = d.get('width', 0) or 0
    key = f"fill={has_fill},stroke_color={has_color},sw={round(sw,3)}"
    fill_stroke[key] += 1

print("Path types in body text:")
for k, c in fill_stroke.most_common(10):
    print(f"  {k}: {c}")

# Show a few sample paths
for d in body[:3]:
    print(f"\nSample path:")
    print(f"  rect: {d['rect']}")
    print(f"  fill: {d.get('fill')}")
    print(f"  color: {d.get('color')}")
    print(f"  width: {d.get('width')}")
    print(f"  closePath: {d.get('closePath')}")
    print(f"  type: {d.get('type')}")
    n_items = len(d.get('items', []))
    print(f"  items: {n_items}")
    if n_items <= 5:
        for item in d.get('items', []):
            print(f"    {item[0]}: {[round(v,2) if isinstance(v,float) else v for v in item[1:]]}")

doc.close()
