import fitz
from collections import Counter

try:
    doc = fitz.open(IMAGE_PATH)
    page = doc.load_page(0)
    print(f"Page: {page.rect.width/72*25.4:.2f} x {page.rect.height/72*25.4:.2f} mm")

    print("Getting drawings...")
    drawings = page.get_drawings()
    print(f"Total drawings: {len(drawings)}")

    print("Finding lines...")
    horiz = []
    for d in drawings:
        for item in d["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 2:
                    length = abs(p2.x - p1.x)
                    if length > 100:
                        horiz.append(length/72*25.4)
    horiz.sort(reverse=True)
    print(f"Top 10 measurement lines (mm):")
    for i, mm in enumerate(horiz[:10]):
        print(f"  {i+1}. {mm:.2f}mm")

    print("Counting path heights...")
    heights = []
    for d in drawings:
        r = d.get("rect")
        if r:
            h = (r[3]-r[1])/72*25.4
            w = (r[2]-r[0])/72*25.4
            if 0.1 < h < 7 and 0.04 < w < 11:
                heights.append(round(h, 2))
    h_dist = Counter(heights)
    print(f"Top 15 path heights:")
    for h, c in h_dist.most_common(15):
        print(f"  {h:.2f}mm: {c}x")
    doc.close()
    print("Done")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
