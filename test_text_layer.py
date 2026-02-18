#!/usr/bin/env python3
"""Check if text layer exists and what font sizes it reports."""
import fitz

doc = fitz.open("/Users/clawdy/Desktop/hazard_label_700ml.pdf")
page = doc.load_page(0)

# Try text extraction
blocks = page.get_text("dict")["blocks"]
print(f"Text blocks: {len(blocks)}")

from collections import Counter
font_sizes = Counter()
for b in blocks:
    if b.get("type") != 0: continue
    for line in b.get("lines", []):
        for span in line.get("spans", []):
            sz = round(span["size"], 2)
            txt = span["text"][:30]
            font_sizes[sz] += len(span["text"])
            if len(font_sizes) < 30:
                print(f"  size={sz}pt font={span['font'][:20]} text='{txt}'")

print(f"\nFont size distribution (by char count):")
for sz, cnt in font_sizes.most_common(20):
    mm = sz / 72 * 25.4
    print(f"  {sz}pt = {mm:.3f}mm: {cnt} chars")

# Also check with get_text("rawdict") for bbox info  
print("\n--- Checking span bboxes ---")
blocks2 = page.get_text("rawdict")["blocks"]
for b in blocks2:
    if b.get("type") != 0: continue
    for line in b.get("lines", []):
        for span in line.get("spans", []):
            if 3.0 < span["size"] < 4.0:  # Near our target range
                bbox = span["bbox"]
                h_pt = bbox[3] - bbox[1]
                h_mm = h_pt / 72 * 25.4
                print(f"  size={span['size']:.2f}pt bbox_h={h_pt:.2f}pt={h_mm:.3f}mm text='{span['text'][:40]}'")
                break
        else:
            continue
        break

doc.close()
