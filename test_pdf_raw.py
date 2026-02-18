#!/usr/bin/env python3
"""Examine raw PDF content streams for font size commands (Tf, Tm, etc.)."""
import fitz

doc = fitz.open("/Users/clawdy/Desktop/hazard_label_700ml.pdf")
page = doc.load_page(0)

# Get page content as text  
xref = page.xref
content = page.read_contents().decode('latin-1')

# Look for text-related operators
import re
# Tf = set font and size
tf_matches = re.findall(r'/(\S+)\s+([\d.]+)\s+Tf', content)
print(f"Font settings (Tf commands): {len(tf_matches)}")
from collections import Counter
size_counts = Counter()
for font, size in tf_matches:
    size_counts[float(size)] += 1
print("Font sizes used:")
for sz, cnt in size_counts.most_common(20):
    mm = sz / 72 * 25.4
    print(f"  {sz}pt = {mm:.3f}mm: {cnt} occurrences")

# Also check for text matrices (Tm)
tm_matches = re.findall(r'([\d.]+)\s+[\d.]+\s+[\d.]+\s+([\d.]+)\s+[\d.]+\s+[\d.]+\s+Tm', content)
if tm_matches:
    print(f"\nText matrices (Tm): {len(tm_matches)}")
    tm_sizes = Counter()
    for sx, sy in tm_matches:
        tm_sizes[(float(sx), float(sy))] += 1
    for (sx, sy), cnt in tm_sizes.most_common(10):
        print(f"  scale=({sx}, {sy}): {cnt}")

doc.close()
