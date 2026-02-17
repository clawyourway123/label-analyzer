import fitz
from collections import Counter

try:
    doc = fitz.open(IMAGE_PATH)
    page = doc.load_page(0)
    print(f"Page: {page.rect.width/72*25.4:.2f} x {page.rect.height/72*25.4:.2f} mm")

    drawings = page.get_drawings()
    print(f"Total drawings: {len(drawings)}")

    horiz = []
    vert = []
    for d in drawings:
        for item in d["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 2:
                    length = abs(p2.x - p1.x) / 72 * 25.4
                    if length > 50:
                        horiz.append(length)
                elif abs(p1.x - p2.x) < 2:
                    length = abs(p2.y - p1.y) / 72 * 25.4
                    if length > 50:
                        vert.append(length)

    horiz.sort(reverse=True)
    vert.sort(reverse=True)
    print(f"\nTop 10 HORIZONTAL lines (mm):")
    for i, mm in enumerate(horiz[:10]):
        print(f"  {i+1}. {mm:.2f}mm")
    print(f"\nTop 10 VERTICAL lines (mm):")
    for i, mm in enumerate(vert[:10]):
        print(f"  {i+1}. {mm:.2f}mm")

    # Scale factor calc for known annotations
    known = [662.90, 636.07, 172.75]
    all_lines = horiz[:10] + vert[:10]
    print(f"\nPossible scale factors:")
    for real_mm in known:
        for vec_mm in all_lines:
            scale = real_mm / vec_mm
            if 0.9 < scale < 1.3:
                font_scaled = 1.57 * scale
                print(f"  {real_mm:.2f} / {vec_mm:.2f} = {scale:.4f} -> font would be {font_scaled:.3f}mm")

    doc.close()
    print("Done")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
