#!/usr/bin/env python3
"""
Test vector measurement against 700ml hazard label PDF.
Ground truth (measured from PDF): font x-height=1.19mm, line gap=0.98mm

Tests both:
1. Direct measurement (simulating production code with region crop + CLP threshold)
2. Full-page measurement (baseline comparison)
"""
import sys
import fitz
import statistics
from collections import Counter

PDF_PATH = "/Users/clawdy/Desktop/hazard_label_700ml.pdf"
EXPECTED_FONT_MM = 1.19
EXPECTED_GAP_MM = 0.98


def measure_region(drawings, xmin_pt, ymin_pt, xmax_pt, ymax_pt, clp_threshold_mm=0.0, label=""):
    """Measure font in a region, simulating production code logic."""
    # Filter drawings to region
    region_paths = []
    margin = 2  # pts
    for d in drawings:
        r = d.get('rect')
        if not r:
            continue
        if (r[0] >= xmin_pt - margin and r[2] <= xmax_pt + margin and
            r[1] >= ymin_pt - margin and r[3] <= ymax_pt + margin):
            w = r[2] - r[0]
            h = r[3] - r[1]
            stroke_w = d.get('width', 0) or 0
            h_mm = (h + stroke_w) / 72 * 25.4
            w_mm = w / 72 * 25.4
            if 0.3 < h_mm < 5 and 0.05 < w_mm < 10:
                region_paths.append({
                    'h_mm': h_mm, 'h_raw_mm': h / 72 * 25.4,
                    'y_top': r[1], 'y_bot': r[3],
                    'x': r[0], 'x_end': r[2],
                    'y_center': (r[1] + r[3]) / 2,
                    'stroke_w': stroke_w
                })

    if len(region_paths) < 10:
        print(f"  [{label}] Only {len(region_paths)} paths — too few")
        return None

    # Group into text lines
    common_h = Counter(round(g['h_mm'] / 25.4 * 72, 1) for g in region_paths).most_common(1)[0][0]
    line_tol = max(0.8, common_h * 0.4)
    region_paths.sort(key=lambda g: g['y_center'])
    
    text_lines = []
    cur_line = [region_paths[0]]
    cur_y = region_paths[0]['y_center']
    for g in region_paths[1:]:
        if abs(g['y_center'] - cur_y) < line_tol:
            cur_line.append(g)
            cur_y = sum(p['y_center'] for p in cur_line) / len(cur_line)
        else:
            if len(cur_line) >= 3:
                text_lines.append(cur_line)
            cur_line = [g]
            cur_y = g['y_center']
    if len(cur_line) >= 3:
        text_lines.append(cur_line)

    # Per-line: group into chars, measure heights
    line_char_heights = []
    line_y_centers_mm = []
    for line in text_lines:
        line.sort(key=lambda g: g['x'])
        chars = []
        cc = [line[0]]
        for g in line[1:]:
            if g['x'] < max(p['x_end'] for p in cc) + 0.5:
                cc.append(g)
            else:
                chars.append(cc)
                cc = [g]
        chars.append(cc)
        
        ch_heights = []
        for ch in chars:
            top = min(p['y_top'] for p in ch)
            bot = max(p['y_bot'] for p in ch)
            stroke = max(p['stroke_w'] for p in ch) if ch else 0
            ch_heights.append((bot - top + stroke) / 72 * 25.4)
        
        line_char_heights.append(ch_heights)
        line_y_centers_mm.append(statistics.median([p['y_center'] for p in line]) / 72 * 25.4)

    # Find body text lines (most common line height cluster)
    line_medians = [(i, statistics.median(lh)) for i, lh in enumerate(line_char_heights) if lh]
    lm_bins = Counter(round(h, 1) for _, h in line_medians)
    best_bin = max(lm_bins, key=lambda h: sum(lm_bins.get(round(h + d * 0.1, 1), 0) for d in [-1, 0, 1]))
    
    body_indices = set()
    body_char_heights = []
    for i, med_h in line_medians:
        if abs(med_h - best_bin) <= 0.3:
            body_indices.add(i)
            body_char_heights.extend(line_char_heights[i])

    if len(body_char_heights) < 5:
        body_char_heights = [h for lh in line_char_heights for h in lh]
        body_indices = set(range(len(text_lines)))

    # Filter tiny paths
    body_char_heights = [h for h in body_char_heights if h >= 0.5]

    # Cluster heights (±0.05mm)
    hb = Counter(round(h, 2) for h in body_char_heights)
    sorted_h = sorted(hb.keys())
    clusters = []
    for h in sorted_h:
        c = hb[h]
        merged = False
        for cl in clusters:
            if abs(h - cl[0]) <= 0.05:
                cl[1] += c
                cl[2].extend([h] * c)
                merged = True
                break
        if not merged:
            clusters.append([h, c, [h] * c])
    
    if len(clusters) > 8:
        clusters = []
        for h in sorted_h:
            c = hb[h]
            merged = False
            for cl in clusters:
                if abs(h - cl[0]) <= 0.08:
                    cl[1] += c
                    cl[2].extend([h] * c)
                    merged = True
                    break
            if not merged:
                clusters.append([h, c, [h] * c])

    for cl in clusters:
        cl[0] = statistics.median(cl[2])

    peaks = sorted([(cl[0], cl[1]) for cl in clusters if cl[1] >= 3], key=lambda x: -x[1])

    if not peaks:
        print(f"  [{label}] No peaks found")
        return None

    # Peak selection: CLP-threshold-aware
    peaks_by_count = sorted(peaks, key=lambda x: -x[1])
    
    if clp_threshold_mm > 0:
        top_count = peaks_by_count[0][1]
        clp_candidates = [
            (h, c) for h, c in peaks_by_count
            if abs(h - clp_threshold_mm) <= 0.15 and c >= top_count * 0.2
        ]
        if clp_candidates:
            best_clp = min(clp_candidates, key=lambda p: abs(p[0] - clp_threshold_mm))
            xheight_mm = best_clp[0]
            selection = f"CLP-aware (threshold={clp_threshold_mm}mm)"
        else:
            xheight_mm = peaks_by_count[0][0]
            selection = "most-frequent (no peak near threshold)"
    else:
        xheight_mm = peaks_by_count[0][0]
        selection = "most-frequent"

    # Line spacing
    body_ys = sorted([line_y_centers_mm[i] for i in body_indices])
    gap_mm = None
    c2c_mm = None
    if len(body_ys) >= 2:
        spacings = [body_ys[i + 1] - body_ys[i] for i in range(len(body_ys) - 1)]
        sp_bins = Counter(round(s, 1) for s in spacings)
        mode_sp = sp_bins.most_common(1)[0][0]
        tight = [s for s in spacings if abs(s - mode_sp) <= 0.3]
        c2c_mm = statistics.mean(tight) if tight else statistics.median(spacings)
        gap_mm = max(0, c2c_mm - xheight_mm)

    return {
        'xheight_mm': xheight_mm,
        'gap_mm': gap_mm,
        'c2c_mm': c2c_mm,
        'selection': selection,
        'peaks': peaks_by_count[:5],
        'n_paths': len(region_paths),
        'n_lines': len(text_lines),
        'n_body_lines': len(body_indices),
        'body_cluster': best_bin,
    }


def main():
    doc = fitz.open(PDF_PATH)
    page = doc.load_page(0)
    drawings = page.get_drawings()

    pw = page.rect.width
    ph = page.rect.height
    pw_mm = pw / 72 * 25.4
    ph_mm = ph / 72 * 25.4
    print(f"Page: {pw:.1f} x {ph:.1f} pts = {pw_mm:.1f} x {ph_mm:.1f} mm")
    print(f"Total drawings: {len(drawings)}")

    # Test scenarios
    scenarios = [
        ("Full page, NO threshold", 0, 0, pw, ph, 0.0),
        ("Full page, CLP threshold=1.2mm", 0, 0, pw, ph, 1.2),
        ("CLP region y=60-300mm, NO threshold", 0, 60/25.4*72, pw, 300/25.4*72, 0.0),
        ("CLP region y=60-300mm, threshold=1.2mm", 0, 60/25.4*72, pw, 300/25.4*72, 1.2),
    ]

    print(f"\n{'=' * 70}")
    print(f"GROUND TRUTH: x-height={EXPECTED_FONT_MM}mm, gap={EXPECTED_GAP_MM}mm")
    print(f"{'=' * 70}")

    for name, xmin, ymin, xmax, ymax, threshold in scenarios:
        result = measure_region(drawings, xmin, ymin, xmax, ymax,
                                clp_threshold_mm=threshold, label=name)
        if not result:
            continue

        xh = result['xheight_mm']
        xh_err = (xh - EXPECTED_FONT_MM) / EXPECTED_FONT_MM * 100
        
        print(f"\n--- {name} ---")
        print(f"  Paths: {result['n_paths']}, Lines: {result['n_lines']}, Body lines: {result['n_body_lines']}")
        print(f"  Body cluster: {result['body_cluster']:.1f}mm")
        print(f"  Peaks: {[(f'{h:.3f}mm', c) for h, c in result['peaks']]}")
        print(f"  Selection: {result['selection']}")
        print(f"  X-height: {xh:.4f}mm (expected {EXPECTED_FONT_MM}mm, error {xh_err:+.2f}%) {'✅' if abs(xh_err) <= 2 else '❌'}")
        
        if result['gap_mm'] is not None:
            gap_err = (result['gap_mm'] - EXPECTED_GAP_MM) / EXPECTED_GAP_MM * 100
            print(f"  Gap: {result['gap_mm']:.4f}mm (expected {EXPECTED_GAP_MM}mm, error {gap_err:+.2f}%) {'✅' if abs(gap_err) <= 5 else '❌'}")
            print(f"  C2C: {result['c2c_mm']:.4f}mm")

    doc.close()


if __name__ == '__main__':
    main()
