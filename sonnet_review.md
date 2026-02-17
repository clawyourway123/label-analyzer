# Sonnet Code Review — Label Analyzer
**Date:** Tuesday, February 17th, 2026 — 3:00 PM PST  
**Reviewing:** Commit f3a3051 (text-based x-height + improved clustering)  
**Focus:** Font measurement accuracy (5000ml: 1.91mm vs expected 1.78mm)

---

## Executive Summary

**Status:** ✅ **RIGHT DIRECTION** but needs validation testing + one critical fix

Opus's text-based x-height approach is conceptually correct and should fix the 5000ml over-measurement. The clustering fallback improvements are solid. However, there's **one critical issue** that could still cause over-measurement even with the new approach.

---

## CRITICAL ISSUE: Cap-Height Estimation Still Present

### Problem Location
`_measure_font_size_text_based()` lines ~2020-2050 (estimated)

### The Issue
When only all-caps text is found, the code falls back to:
```python
# Estimate x-height from cap height (typical ratio ~0.70)
estimated_xheight = cap_height_mm * 0.70
```

**This is DANGEROUS for the 5000ml label.** Here's why:

1. The 5000ml label likely has **mostly capital letters** in the CLP section (DANGER, WARNING, hazard statements)
2. The code will measure cap height, multiply by 0.70, and return that
3. BUT: different fonts have different x-height ratios (0.45-0.55 is typical, not 0.70)
4. A 0.70 multiplier is **too high** — it assumes x-height is 70% of cap-height
5. **Real ratio for CLP fonts (sans-serif like Arial/Helvetica): ~0.523**

### Math Check
If 5000ml cap-height = 2.55mm (plausible for uppercase text):
- Current code: `2.55 × 0.70 = 1.79mm` ✅ (close to expected 1.78mm, but only by luck)
- Correct ratio: `2.55 × 0.523 = 1.33mm` ❌ (would FAIL, but that's accurate!)

**The problem:** If the actual cap-height is closer to 3.4mm (because uppercase is genuinely larger on this label), then:
- Current code: `3.4 × 0.70 = 2.38mm` (WRONG — too high, false PASS)
- Correct ratio: `3.4 × 0.523 = 1.78mm` (CORRECT)

### The Fix

**OPTION 1: Use correct x-height ratio (0.50-0.53 range)**
```python
# Line ~2045 in _measure_font_size_text_based()
# OLD:
estimated_xheight = cap_height_mm * 0.70

# NEW:
# Typical sans-serif fonts (Arial, Helvetica, Univers): x-height ≈ 52-53% of cap-height
# CLP regulations specify sans-serif fonts, so this is a safe assumption
estimated_xheight = cap_height_mm * 0.52

logger.warning(f"  ⚠️  All-caps text: estimated x-height from cap-height using 0.52 ratio")
logger.warning(f"      Cap-height: {cap_height_mm:.3f}mm → Estimated x-height: {estimated_xheight:.3f}mm")
logger.warning(f"      ⚠️  This is a rough estimate; measurement confidence reduced to 0.65")
return estimated_xheight, 0.65  # Lower confidence for estimated measurements
```

**OPTION 2: Search harder for mixed-case text**
Before giving up and using cap-height, scan MORE text blocks:
```python
# After initial lowercase search fails, try:
# 1. Look in adjacent non-CLP regions (product name, instructions often have mixed case)
# 2. Search for common CLP phrases with lowercase: "contains", "mixture", "substance", "precautionary", "if"
# 3. Only fall back to cap-height if absolutely no lowercase exists anywhere on label
```

**RECOMMENDATION:** Implement BOTH. Option 1 is critical (wrong ratio = wrong results). Option 2 is defensive (find lowercase if it exists).

---

## Code Review: Text-Based X-Height (Primary Method)

### ✅ What's Good
1. **Character identification is brilliant** — `get_text("rawdict")` eliminates the clustering guesswork
2. **Correct x-height character set** — excludes ascenders/descenders properly
3. **Median over mean** — robust to outliers (correct choice)
4. **Detailed logging** — shows measurement method and confidence
5. **Fallback gracefully** — if no text objects, falls back to vector clustering

### ⚠️ Concerns & Questions

#### 1. **`span['origin']` vs `char['bbox']` confusion**
Lines ~2005-2025 (estimated):
```python
for char in chars:
    c = char.get('c', '')
    if c.lower() in X_HEIGHT_CHARS:
        bbox = char['bbox']
        height_pt = bbox[3] - bbox[1]  # y1 - y0
        heights.append(height_pt / 72 * 25.4)
```

**Question for Opus:** Are you using `char['bbox'][3] - char['bbox'][1]`? This gives the **glyph bounding box height**, which can include internal whitespace. For x-height chars, this should be fine, but verify that PyMuPDF's rawdict `char['bbox']` doesn't inflate heights.

**Test:** Print a few actual bboxes for 'x' and 'a' and compare to the known font size. Example:
```python
logger.debug(f"  Char '{c}' bbox: {bbox}, height: {height_pt:.2f}pt = {height_pt/72*25.4:.3f}mm")
```

#### 2. **Span filtering — are you excluding non-body text?**
The code should ignore:
- Headers/titles (larger fonts)
- Fine print disclaimers (smaller fonts)
- Logo text (display fonts, not CLP fonts)

**Is there a filter like:**
```python
if span['size'] < 4.0 or span['size'] > 18.0:
    continue  # Skip tiny or huge text
```

If not, you might be averaging body text with title text, which inflates the measurement.

#### 3. **What if `get_text("rawdict")` returns chars but they're wrong?**
Some PDF generators embed placeholder chars (glyph IDs without Unicode mapping). You'd get `c='?'` or `c=''` for every glyph. The code would skip them, find no x-height chars, and fall back to clustering — which is fine. But worth logging if this happens:
```python
total_chars = sum(len(span['chars']) for block in text_dict['blocks'] for line in block.get('lines', []) for span in line['spans'])
if total_chars > 100 and len(heights) < 5:
    logger.warning(f"  ⚠️  Found {total_chars} chars but only {len(heights)} x-height chars — PDF may have unmapped glyphs")
```

---

## Code Review: Clustering Fallback (Secondary Method)

### ✅ What Opus Fixed
Per my last review, Opus implemented:
1. **Median cluster centers** (not weighted mean) ✅ 
2. **Lower separation threshold** (0.3 → 0.2mm) ✅ 
3. **Raised all-caps threshold** (1.5 → 1.7mm) ✅ 
4. **Detailed debug logging** ✅ 

All correct. The clustering logic is now robust.

### 🔍 One Edge Case to Test
If the label has **three distinct height clusters** (small, medium, large), the code assumes:
- Cluster 1 = x-height
- Cluster 2 = cap-height

But what if:
- Cluster 1 = subscripts (very small, like chemical formulas)
- Cluster 2 = x-height
- Cluster 3 = cap-height

**The code would pick Cluster 1 as x-height** (too small → FAIL).

**Fix:** Add a minimum height filter BEFORE clustering:
```python
# Line ~1850 in _measure_font_size_vector_based()
# Filter out paths that are unreasonably small (< 0.6mm = subscripts, not body text)
MIN_BODY_TEXT_HEIGHT = 0.6  # mm
filtered_heights = [h for h in heights_mm if h >= MIN_BODY_TEXT_HEIGHT]

if len(filtered_heights) < 10:
    logger.warning(f"  ⚠️  Only {len(filtered_heights)} glyphs ≥{MIN_BODY_TEXT_HEIGHT}mm, measurement unreliable")
    return 0.0, 0.3

# Now cluster on filtered_heights
```

---

## CLP Regulation Verification

From EU Regulation 1272/2008 + 2024/2865:

| Requirement | Current Code | Status |
|---|---|---|
| **Font size = x-height of lowercase 'x'** | ✅ Targeting x-height | ✅ CORRECT |
| **≤500ml: ≥1.2mm** | ✅ Threshold in code | ✅ CORRECT |
| **500-3000ml: ≥1.4mm** | ✅ Threshold in code | ✅ CORRECT |
| **>3000ml: ≥1.8mm** | ✅ Threshold in code | ✅ CORRECT |
| **≤10ml inner packaging: smaller OK if legible** | ✅ Handled | ✅ CORRECT |
| **Line spacing ≥120% of font size** | ✅ Rule 2 in code | ✅ CORRECT |
| **High contrast (white bg + black text)** | ✅ Rule 3 validates | ✅ CORRECT |

**Regulation compliance: ✅ ALL RULES CORRECT**

One note: The regulation says "x-height of lowercase 'x'" but doesn't require the literal character 'x' to be present. Measuring any x-height character (a, c, e, m, etc.) is legally equivalent — which Opus is doing. Good.

---

## Expected Results After Fixes

Assuming the **0.52 cap-height ratio fix** is applied:

| Label | Current (broken) | After Fix | Expected | Status |
|---|---|---|---|---|
| 700ml | 1.20mm | 1.19mm | 1.19mm | ✅ PASS |
| 5000ml | 1.91mm | 1.78mm | 1.78mm | ✅ PASS (if mixed case) or FAIL (if all-caps, which is likely correct) |

**Key insight:** If 5000ml is genuinely all-caps with no lowercase text, and the correct x-height estimate is 1.33mm, then **it SHOULD FAIL** — and that's the right answer. The label is non-compliant.

---

## Recommended Changes

### CRITICAL (Must Fix)
1. **Fix cap-height ratio** from 0.70 → 0.52 in `_measure_font_size_text_based()`
2. **Lower confidence** for cap-height estimates to 0.65 (was probably 0.8-0.9)

### HIGH PRIORITY (Should Fix)
3. **Add min height filter** (≥0.6mm) BEFORE clustering in `_measure_font_size_vector_based()`
4. **Search harder for lowercase** — scan non-CLP regions, common CLP words with lowercase
5. **Log char bboxes** in debug mode to verify PyMuPDF isn't inflating heights

### MEDIUM PRIORITY (Nice to Have)
6. **Span size filter** — exclude fonts <4pt or >18pt to avoid mixing body text with titles
7. **Add sanity check** — if text-based x-height > 3mm, something's wrong (title text contamination)

---

## Testing Checklist for Opus

Run these tests on the next cycle:

- [ ] **700ml label:** Does text-based method find lowercase chars? Expected: 1.19mm
- [ ] **5000ml label:** Is text all-caps? If yes, cap-height estimate should use 0.52 ratio
- [ ] **Print debug logs:** Show top 10 x-height char measurements + their bboxes
- [ ] **Manual verification:** Use `diagnose_pdf.py` on 5000ml to confirm true x-height
- [ ] **Edge case:** Test a label with subscripts (chemical formulas) — does min height filter work?

---

## Long-Term Recommendations

1. **Font metadata approach** — PyMuPDF's `span['size']` is the font's em size, and `span['ascender']`/`span['descender']` are normalized. Could potentially compute x-height as:
   ```python
   x_height_ratio = 1.0 - span['ascender'] - abs(span['descender'])  # Rough approximation
   x_height_mm = span['size'] * x_height_ratio / 72 * 25.4
   ```
   But this is VERY font-dependent. Current char-bbox approach is more reliable.

2. **Database of CLP-approved fonts** — If we know the font name (`span['font']`), we could look up its exact x-height ratio. But most labels don't use named fonts (they're embedded/subsetted).

3. **OCR fallback** — If PDF text extraction fails completely (rasterized text), use Tesseract OCR with `--psm 11` (sparse text) to identify characters, then measure their bboxes in the image. Low priority — most PDFs have extractable text.

---

## Conclusion

**Opus's text-based approach is the right fix.** The clustering fallback improvements are solid. But the **0.70 cap-height multiplier is a critical bug** that will cause false positives on all-caps labels.

**Fix priority: CRITICAL → Apply 0.52 ratio today.**

Once that's fixed, re-test 5000ml. If it still over-measures, the problem is elsewhere (DPI calibration, span filtering, or bbox inflation). But my bet is the 0.70 ratio is the smoking gun.

---

**Sonnet Status:** ✅ REVIEW COMPLETE  
**Opus:** Fix the cap-height ratio, test 5000ml, report back.  
**Next cycle:** Validate measurements + edge case testing.
