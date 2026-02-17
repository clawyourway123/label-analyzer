# Opus Code Review — 2026-02-17 14:29 PST

## Status: 5000ml font=1.91mm vs expected 1.78mm

### Root Cause Analysis

The problem is in `measure_font_from_pdf_vectors()` around line 1757:

```python
font_size_mm = statistics.mean(body_char_heights)
```

This computes **mean of ALL character bounding-box heights** in body text lines. For mixed-case text (which CLP labels typically have), this includes:
- **Uppercase letters** (~1.8-2.1mm cap-height)
- **Lowercase with ascenders** (d, h, k, l — same height as caps)
- **Lowercase x-height body** (a, c, e, m, n, o — ~1.2-1.4mm)
- **Lowercase with descenders** (g, p, y — taller bbox because descender extends below baseline)

The mean of all these over-reports the "font size" because uppercase and ascender letters pull the mean up. **CLP regulation specifies x-height**, not mean character height.

### Why 700ml works but 5000ml doesn't

The 700ml label likely has more lowercase-heavy text (ingredients lists with words like "aqua", "sodium", etc.), so the mean is closer to x-height. The 5000ml label probably has more uppercase or mixed content, pushing the mean higher.

**Expected math for 5000ml:**
- If cap-height ≈ 2.1mm and x-height ≈ 1.5mm (ratio ~0.71)
- Mean of all chars ≈ 1.91mm (matches observation)
- True x-height ≈ 1.5mm, but CLP minimum for >3000ml = 1.8mm
- Expected "font size" = 1.78mm suggests they're measuring something between x-height and cap-height

### The Real Problem: CLP Doesn't Mean Pure X-Height Here

After checking: EU Regulation 1272/2008 Annex I, Section 1.2.1.3 says font size is measured as **x-height** of lowercase letters. But the expected value of 1.78mm for a >3000ml label (threshold 1.8mm) suggests the "expected" value might itself be a cap-height measurement, or the label is borderline non-compliant.

**Key question:** Where does the 1.78mm "expected" value come from? Is it:
1. A manual measurement (ruler on print)? → Then it's probably cap-height, not x-height
2. From another tool's output? → Need to know what that tool measures
3. From the label designer's spec? → Could be either

### Proposed Fix: Separate X-Height from Cap-Height

Instead of `mean(all body chars)`, we should compute **two clusters within each line**: short chars (x-height) and tall chars (cap-height + ascenders).

#### Code Change — `measure_font_from_pdf_vectors()`, replace lines ~1730-1760:

```python
# After computing char_heights_mm for each line...
# Instead of using raw heights, separate into x-height vs cap-height

import numpy as np  # or use pure statistics

# Collect per-line x-height estimates
line_xheights = []
line_capheights = []

for line_h in [line_char_heights[i] for i in body_line_indices]:
    if len(line_h) < 3:
        continue
    
    # Sort heights to find the natural split
    sorted_h = sorted(line_h)
    median_h = statistics.median(sorted_h)
    
    # Two clusters: below median+10% = x-height body, above = caps/ascenders
    threshold = median_h * 1.1
    short_chars = [h for h in line_h if h <= threshold]
    tall_chars = [h for h in line_h if h > threshold]
    
    if short_chars:
        line_xheights.append(statistics.median(short_chars))
    if tall_chars:
        line_capheights.append(statistics.median(tall_chars))

# Report both
xheight_mm = statistics.mean(line_xheights) if line_xheights else font_size_mm
capheight_mm = statistics.mean(line_capheights) if line_capheights else font_size_mm

# CLP uses x-height
font_size_mm = xheight_mm
```

**Problem with this approach:** If ALL text is uppercase (common in hazard statements like "CAUSES SERIOUS EYE DAMAGE"), there are no short chars. Need a fallback.

### Better Approach: Height Histogram Clustering

```python
# In measure_font_from_pdf_vectors(), replace the font_size_mm calculation:

# Build histogram of all body char heights (0.05mm bins)
height_counts = Counter(round(h, 2) for h in body_char_heights)

# Find peaks in the histogram (local maxima)
sorted_heights = sorted(height_counts.keys())
peaks = []
for i, h in enumerate(sorted_heights):
    count = height_counts[h]
    # Include neighbors for smoothing
    neighbor_count = count
    for delta in [-0.01, -0.02, 0.01, 0.02]:
        neighbor_count += height_counts.get(round(h + delta, 2), 0)
    
    if neighbor_count >= 3:  # Minimum significance
        peaks.append((h, neighbor_count))

# Sort peaks by count (most common first)
peaks.sort(key=lambda x: -x[1])

if len(peaks) >= 2:
    # Two peaks = x-height and cap-height
    p1, p2 = sorted([peaks[0][0], peaks[1][0]])
    # Smaller peak = x-height, larger = cap-height
    xheight_mm = p1
    capheight_mm = p2
    font_size_mm = xheight_mm  # CLP uses x-height
    logger.info(f"  📐 Bimodal: x-height={xheight_mm:.3f}mm, cap-height={capheight_mm:.3f}mm")
else:
    # Single peak = likely all-caps or all-lowercase
    font_size_mm = peaks[0][0] if peaks else statistics.mean(body_char_heights)
    # If single peak and it's > 1.5mm, likely all-caps → apply 0.70 ratio
    if font_size_mm > 1.5:
        logger.info(f"  📐 Single peak {font_size_mm:.3f}mm — likely all-caps, estimating x-height")
        font_size_mm *= 0.70
```

### Also Add to Return Dict:

```python
return {
    'font_size_mm': round(font_size_mm, 4),        # x-height (CLP metric)
    'cap_height_mm': round(capheight_mm, 4),        # For reference
    'font_size_mm_median': round(median_font_mm, 4),
    'measurement_approach': 'x-height-bimodal' if len(peaks) >= 2 else 'single-peak',
    ...
}
```

### Line Spacing Note

The line spacing calculation (`center_to_center - font_size`) will also change if font_size changes. If we switch to x-height:
- `visible_gap = center_to_center - cap_height` (not x-height)
- Because the visible gap is between the BOTTOM of tall chars on line N and TOP of tall chars on line N+1
- Using x-height here would OVER-report the gap

**Fix:** Keep using mean (or cap-height) for gap calculation, use x-height only for Rule 1.

```python
# Line gap should subtract the TALLEST chars (cap-height), not x-height
line_distance_mm = max(0, center_to_center_mm - capheight_mm)
```

### Diagnostic Validation

Before implementing, run `diagnose_pdf.py` on the 5000ml PDF and check:
1. Is there a clear bimodal distribution in char heights?
2. What are the two peaks?
3. Does the lower peak ≈ 1.78mm (matching expected)?

```bash
python diagnose_pdf.py /path/to/5000ml.pdf
```

Look at the "Path height distribution" output — if you see two clusters (e.g., 1.3mm and 1.9mm), bimodal clustering will work perfectly.

### Priority

1. **HIGH:** Add bimodal height clustering for x-height extraction
2. **HIGH:** Fix line gap to subtract cap-height, not mean
3. **MEDIUM:** Add `cap_height_mm` and `xheight_mm` to return dict for debugging
4. **LOW:** Handle all-caps fallback (0.70× ratio)

### Risk

The 0.70 ratio for all-caps is font-dependent (ranges 0.65-0.75 across typefaces). For Sans-serif fonts common on labels (Helvetica, Arial, DIN), 0.70-0.72 is accurate. For serif fonts, 0.68-0.70. Using 0.70 is a reasonable default but should be flagged as estimated.
