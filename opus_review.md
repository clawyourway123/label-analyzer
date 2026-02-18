# OPUS TEST & FIX CYCLE — 2026-02-17 4:52 PM

## Executive Summary

**Status:** ✅ Code fixes applied, but measurement accuracy remains below target  
**Root Cause:** PDF ground truth values appear misaligned with actual vector measurements

---

## Test Results: 700ml Hazard Label

### Measurements vs Ground Truth

| Metric | Measured | Expected | Error | Status |
|--------|----------|----------|-------|--------|
| Font Size (x-height) | 1.0800 mm | 1.19 mm | -9.24% | ❌ FAILED |
| Line Spacing (gap) | 1.0435 mm | 0.98 mm | +6.48% | ❌ FAILED |

**Target Accuracy:** ±2.0%  
**Measured Accuracy:** Font -9.24%, Gap +6.48% (both outside tolerance)

### Bimodal Distribution Analysis

The improved detection correctly identified:
- **X-height cluster:** 1.0800 mm (1,347 characters)
- **Cap-height cluster:** 1.4700 mm (844 characters)
- **Separation:** 0.390 mm (clearly bimodal)

---

## Fixes Applied This Cycle

### 1. ✅ PyMuPDF Small Glyph Heights (Sonnet's Suggestion)

**File:** `label_analyzer_production.py` (lines 20-25)

```python
# Add at module level after imports
fitz.TOOLS.set_small_glyph_heights(True)
```

**Impact:** This flag tells PyMuPDF to return VISIBLE glyph heights without padding (10-37% reduction). However, since the 700ml PDF uses pure vector outlines (not embedded fonts), this flag has NO EFFECT on vector measurements.

**Status:** Applied but ineffective for this PDF type.

### 2. ✅ Improved Bimodal Detection Algorithm

**File:** `label_analyzer_production.py` (lines 2032-2062)

**Before:** Algorithm compared the TOP 2 MOST FREQUENT clusters, which could skip the true x-height if nearby sub-clusters existed.

**After:** 
- Uses MOST FREQUENT cluster as x-height (body text is statistically dominant)
- Finds next cluster with >0.25mm separation as cap-height
- Filters noise: requires >50 chars and height 0.8-3.0mm for cap-height candidate
- More robust than comparing arbitrary height-separated pairs

**Test Output:**
```
Clusters by character count: [(1.08, 1347), (1.03, 967), (1.47, 844), (1.42, 585), (1.57, 204)]
Most frequent cluster (x-height): 1.0800mm (1347 chars)
Best cap-height candidate: 1.4700mm (844 chars, separation=0.390mm)
✓ BIMODAL DETECTION SUCCESS: 1.0800mm (x-height), 1.4700mm (cap-height)
```

**Status:** ✅ Working correctly, finds true bimodal split.

---

## Investigation: Why Is Measured Font 9.24% Too Small?

### Hypothesis 1: Ground Truth is From a Different PDF ❌
The annotation on the PDF says "width for text 197,84 mm" and "fond 202,84 mm". My measurements show 202.8mm horizontal dimension matches, confirming **this PDF is 1:1 physical scale.** The label was not scaled during creation.

### Hypothesis 2: Different Region of Label ⚠️
The PDF contains MULTIPLE text regions:
- Body text (what we measured): 1.08mm x-height
- Hazard warnings: 1.16-1.42mm range
- Headers/titles: 1.5-6.1mm range

The expected 1.19mm might come from a different section. However, **body text measurement is the correct CLP compliance check**, so using the most frequent clusters is methodologically sound.

### Hypothesis 3: Ground Truth Values Are Incorrect ⚠️
Alternative hypothesis: The stated ground truth (1.19mm font, 0.98mm gap) might have been:
- Measured from a printed physical label (ink gain/loss affects dimensions)
- Estimated from font metadata (not actual rendered size)
- From a different measurement method or tool

**Evidence for PDF accuracy:**
- Height histogram shows clear clustering at 1.08mm (1,556 chars total across entire PDF)
- Bimodal split at 1.08 vs 1.47mm is unambiguous (0.39mm separation)
- Font size measurements are internally consistent (x-height to cap-height ratio = 1.36, typical for sans-serif)

---

## Line Spacing Analysis

### Measurements
- **Center-to-center:** 2.1235 mm (mode of spacings)
- **Line gap:** 2.1235 - 1.0800 = 1.0435 mm (measured +6.48%)
- **Expected gap:** 0.98 mm

### Why the Discrepancy?

The formula is: `gap = c2c - x-height`

With measured values:
- Gap = 2.1235 - 1.0800 = 1.0435mm ✓ (math checks out)

But expected calculation would be:
- Gap = c2c - 1.19 = 0.98
- Therefore: c2c = 2.17mm (expected)

**Observation:** The expected c2c of 2.17mm assumes a specific x-height of 1.19mm. If the true x-height is 1.08mm, then either:
1. The c2c should also be proportionally smaller (~2.06mm), OR
2. The gap expectation (0.98mm) is based on different label artwork

### CLP Specification Check

CLP Regulation 2024/2865 requires:
- Line gap ≥ 120% of font size (x-height)
- Measured: 1.0435 / 1.0800 = 96.6% (BELOW minimum)
- Expected: 0.98 / 1.19 = 82.4% (also BELOW minimum)

**Both fail the CLP requirement!** This suggests either the label is non-compliant or the ground truth values are not representative of the actual label.

---

## Character Height Distribution (Full PDF)

Histogram of all measurable glyphs:

```
1.08mm: 1,556 chars ██████████████████████
1.16mm:   659 chars ██████████
1.05mm:   463 chars ███████
1.42mm:   439 chars ███████
1.00mm:   414 chars ███████
1.10mm:   378 chars ██████
1.58mm:   360 chars ██████
...
```

The 1.19mm range (1.17-1.21) exists but is sparse, suggesting it's NOT the primary body text font size.

---

## Code Quality Assessment

### ✅ Strengths

1. **Correct measurement path selection:** Vector → Text-based → Glyph-based fallback
2. **Robust line grouping:** Uses median y-center vs max/min to prevent chain-linking
3. **Multi-level clustering:** Both per-line and per-character height analysis
4. **Comprehensive logging:** Detailed diagnostics for debugging measurement failures

### ⚠️ Remaining Issues

1. **Bimodal heuristics are PDF-dependent:** Different fonts and layouts may need parameter tuning
2. **Line spacing formula assumes specific x-height:** Gap = c2c - x-height is correct but sensitive to accurate x-height detection
3. **No validation against CLP minimums:** Code measures but doesn't warn if gap < 120% font size
4. **Edge cases with noise clusters:** Rare glyphs (1-5 chars) could still bias clustering

### Recommendations for P2/P3

1. **Add CLP validation warnings** when gap < 120% font size
2. **Persist font metrics per PDF** (cache detected sizes to avoid re-analysis)
3. **Cross-validate vector vs text measurements** with larger sample size
4. **Add manual override for known-good font sizes** for difficult PDFs

---

## Commits Made

### Commit 1: Apply PyMuPDF Small Glyph Heights Fix

```
fix: add PyMuPDF set_small_glyph_heights(True) flag

PyMuPDF's default glyph bbox includes 10-37% padding above/below
visible characters. Setting this flag at module init returns visible-only
heights, matching CLP measurement requirements.

Note: This PDF uses vector outlines (no embedded fonts), so the flag
has no effect on current test. But it will help with PDFs containing
rasterized or embedded text layers.

Ref: https://github.com/pymupdf/PyMuPDF/discussions/3067
```

### Commit 2: Improve Bimodal Detection Algorithm

```
fix: improve bimodal peak detection for x-height vs cap-height

OLD: Compared top 2 peaks by count, which could skip true x-height
if nearby sub-clusters existed (e.g., 1.08mm and 1.03mm merging).

NEW: Uses most frequent cluster as x-height, then finds best
separated cluster (>0.25mm, >50 chars, height 0.8-3.0mm) as cap-height.
Avoids edge cases and noise clusters.

Test Results:
- Correctly identifies 1.08mm x-height (1,347 chars)
- Correctly identifies 1.47mm cap-height (844 chars)
- Separation: 0.39mm (unambiguous bimodal split)

This matches the measured distribution, but ground truth mismatch
(expected 1.19mm) suggests either:
1. Different label region expected, or
2. Ground truth values are from different measurement method
```

---

## Next Steps (For Next Cycle)

### P0 - Investigate Ground Truth
- Verify 700ml expected values (1.19mm font, 0.98mm gap) against original source
- Check if they're from a different font size region or measurement method
- If incorrect, update test expectations to match actual PDF content

### P1 - Validate Against 5000ml Label
- Test the improved bimodal detection on the 5000ml label
- Compare pre- and post-fix measurements
- Verify the 1.91mm → ~1.78mm improvement (from Sonnet's prediction)

### P2 - CLP Compliance Validation
- Add warning when line gap < 120% font size (CLP minimum)
- Log which measurement approach was used (text-based, glyph-based, vector clustering)
- Report confidence scores

### P3 - Font Metric Caching
- Persist detected font sizes per PDF hash
- Avoid re-analysis if same label PDF analyzed again
- Reduces API calls and improves determinism

---

## Technical Notes

**Test Script:** `/Users/clawdy/Desktop/label-analyzer/test_bimodal_fix.py`  
**Production Code:** `/Users/clawdy/Desktop/label-analyzer/label_analyzer_production.py`  
**PDF Input:** `/Users/clawdy/Desktop/hazard_label_700ml.pdf` (544.7 x 446.3 mm, all vector outlines)

**Key Findings:**
- PDF is confirmed 1:1 physical scale (202.84mm printed width matches vector dimensions)
- Vector measurements are accurate for the PDF content (no scale issue)
- Bimodal split (1.08 vs 1.47mm) is genuine, not artifact
- Ground truth may be from different source or measurement method
- CLP gap minimum (120%) is not met by either measured or expected values

---

**Test Cycle Complete** — Ready for next iteration with 5000ml label or ground truth verification.
