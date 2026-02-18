# Opus Review — Commit 6c0b9a5

## Changes Made

### 1. Gap Calculation: IQR-based outlier removal (Problem 1)
**File:** `label_analyzer_production.py` lines ~2280-2310

**Before:** Mode-based filtering — rounded spacings to 0.1mm bins, picked mode, averaged values within ±0.3mm. With 5000ml spacings `[2.85, 3.97, 3.81, 15.08, 4.01, 3.61, 3.71, 4.0]`, mode was 4.0 (3 values round to 4.0), tight_spacings excluded 3.61/3.71/3.81, mean = ~3.92mm → gap 2.14mm (5.5% high).

**After:** IQR outlier removal + median. For n≥4, compute Q1/Q3, remove values outside 1.5×IQR bounds (min IQR 0.3mm to avoid over-filtering). For n<4, simple 0.4×–2.5× median filter. Then take median of filtered set.

**Expected impact:** The 15.08mm paragraph break gets removed. Remaining sorted: `[2.85, 3.61, 3.71, 3.81, 3.97, 4.0, 4.01]`. IQR = 4.0 - 3.61 = 0.39, bounds = [3.02, 4.59]. Filtered: `[3.61, 3.71, 3.81, 3.97, 4.0, 4.01]`. Median = (3.81+3.97)/2 = 3.89. Gap = 3.89 - 1.776 = 2.114mm. 

**Honest assessment:** This brings gap from ~2.14 to ~2.11mm — a small improvement. The remaining ~5% error may be inherent in c2c measurement (line y-center calculation from glyph medians). Getting to exactly 2.01mm likely requires measuring actual top-of-line to top-of-next-line rather than center-to-center minus font height.

### 2. Curved Text Detection (Problem 2)
**File:** `label_analyzer_production.py` lines ~2345-2362

Added sanity check: if `c2c < 0.8 × font_size`, flag `measurement_reliable = False`, set confidence to 0.3, clamp gap to 0.0mm. This catches the "Curved View" regions where c2c median (1.165mm) was less than x-height (1.827mm) — physically impossible for non-overlapping text.

The `measurement_reliable` flag is propagated through to the final output dict so downstream consumers can filter/skip unreliable regions.

### 3. Deterministic Ratio Selection (Problem 3)
**File:** `label_analyzer_production.py` lines ~2168-2192

**Before:** Sequential loop over `[0.85, 0.82, 0.80, ...]`, `break` on first match. Order-dependent: if cap-height peak at 2.275mm matches ratio 0.82 first (2.275×0.82=1.866, within 0.10 of threshold), it picks 0.82 even if 0.85 would also work.

**After:** Collect ALL valid (distance, count, height, ratio) tuples across all ratios, sort by distance-to-threshold (ties broken by count), pick the best single option. Same region always gets same ratio regardless of iteration order.

### 700ml Regression Test
Standalone test (`test_700ml.py`) confirms no regression:
- Font: 1.1900mm (0.00% error) ✅
- Gap: 0.9424mm (-3.84% error) ✅ (within tolerance)

### Remaining Gap Accuracy Issue
The ~5% gap overshoot on 5000ml may not be fully fixable with c2c-minus-font approach. The fundamental issue: "center of line" is approximated by median y-center of glyphs in that line, which varies with the uppercase/lowercase mix. Alternative approaches to explore:
- Measure actual bbox bottom of line N to bbox top of line N+1 (true visual gap)
- Use baseline positions from font metrics rather than glyph y-centers
