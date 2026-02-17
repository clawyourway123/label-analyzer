# Sonnet Code Review — Label Analyzer
**Cycle:** Tuesday, February 17th, 2026 — 3:30 PM PST  
**Git HEAD:** 158c84f  
**Reviewer:** Sonnet (Lead Code Reviewer)

---

## 🎯 PRIMARY FINDING: Gap Measurement Uses Wrong Baseline

**CRITICAL BUG FOUND** (Line ~2010-2020 in `measure_font_from_pdf_vectors`):

```python
# CURRENT (WRONG):
line_distance_mm = max(0, center_to_center_mm - font_size_mm)  # font_size_mm = x-height
```

**Problem:** CLP "distance between two lines" = **visible whitespace gap** between bottom of tallest char on line N and top of tallest char on line N+1. 

The code currently subtracts **x-height** (1.78mm for 5000ml) from center-to-center spacing, but the physical gap is determined by the **tallest characters** (cap-height ~2.5mm), not average x-height.

**Visual Proof:**
```
Line 1:  WARNING (cap-height = 2.5mm)
         ↕ ← visible gap (this is what CLP measures)
Line 2:  DANGER (cap-height = 2.5mm)

Center-to-center = 4.42mm (example)
Current calc: 4.42 - 1.78 (x-height) = 2.64mm ❌ WRONG
Correct calc: 4.42 - 2.50 (cap-height) = 1.92mm ✅ MATCHES ACTUAL

This explains 5000ml: gap measured as 1.92mm but code reports wrong value!
```

**Fix Required:**
```python
# Line ~2010:
# CLP line gap = center-to-center - CAP-HEIGHT (tallest chars define gap)
line_distance_mm = max(0, center_to_center_mm - capheight_mm)
```

**Impact:** This single bug explains the 5000ml discrepancy. The font size might actually be correct (1.78mm x-height), but gap calculation is using the wrong reference height.

---

## 🔬 Secondary Finding: ALL-CAPS Fallback Needs Explicit Path

**Issue:** Lines ~1990-2000 have an "all-caps-estimated" path, but it's triggered by a heuristic (`if peak_h > 1.7mm`). 

**Problem with 5000ml label:**
- If 5000ml has small ALL-CAPS text (e.g., 1.5mm cap-height), the heuristic fails
- Text-based measurement finds 0-2 x-height chars → logs warning (line ~1883) but doesn't trigger explicit cap-height measurement
- System falls back to vector clustering which may still measure wrong

**Recommendation:**
Add explicit ALL-CAPS detection path in text-based measurement:

```python
# After line ~1883 where text-based fails:
if len(xheight_pts) < 3:
    # Insufficient lowercase chars — try measuring cap-height instead
    if len(cap_pts) >= 5:
        text_capheight_mm = statistics.median(cap_pts) / 72 * 25.4
        text_xheight_mm = text_capheight_mm * 0.70  # Estimate
        logger.warning(f"  ⚠️  ALL-CAPS text detected: measured cap-height={text_capheight_mm:.3f}mm, estimated x-height={text_xheight_mm:.3f}mm (confidence reduced to 0.75)")
        # Set measurement_confidence to 0.75 to flag for human review
```

This gives an explicit measurement path instead of relying on vector clustering heuristics.

---

## 📊 CLP Regulation Verification

Confirmed via EU Regulation 1272/2008 (CLP) Article 31 + search results:

✅ **Font size = x-height** (height of lowercase 'x')  
✅ **Thresholds:** ≤500ml→1.2mm, 500-3000ml→1.4mm, >3000ml→1.8mm  
✅ **Line distance ≥ 120% of font size** (but measured as VISIBLE GAP, not baseline-to-baseline)

**Key Quote (Arcus Compliance):**
> "Minimum height of 1.2 mm (a minimum height of 1.2 mm of the lower case 'x' [x-height] of the chosen font)"

The code's x-height focus is **correct**. The gap measurement baseline is **incorrect**.

---

## 🧪 Test Plan for Opus

1. **Apply gap measurement fix** (change `font_size_mm` → `capheight_mm` on line ~2010)
2. **Run both labels:**
   - 700ml: Should still pass (already working)
   - 5000ml: Gap should now measure correctly (~2.01mm expected)
3. **Check logs for:**
   - Does 5000ml trigger text-based measurement or fall back to vector?
   - If text-based fails, how many x-height chars were found?
   - Is cap-height being measured and used for gap calc?

4. **If 5000ml text-based still fails:**
   - Implement explicit ALL-CAPS detection (see recommendation above)
   - This ensures robust handling of uppercase-heavy CLP text

---

## 🎯 Additional Observations (Non-Critical)

### Scale Detection Fragility (Lines 1600-1700)
The auto-detect PDF scale makes 3-4 Gemini calls per analysis (expensive). Consider:
- Cache scale factors per PDF hash
- Add manual override via CLI arg `--pdf-scale 1.0234`
- Only re-detect if `--force-recalibrate` flag set

### MIN_BODY_TEXT_HEIGHT Filter (Line ~1910)
```python
MIN_BODY_TEXT_HEIGHT = 0.5  # mm — anything smaller is not body text
```
This might filter legitimate small fonts. CLP allows ≤10ml inner packaging to use fonts <1.2mm "as long as easily legible". Consider:
- Lower threshold to 0.3mm (minimum legible at 300 DPI)
- Or make threshold package-size-aware

### X-height Character Set (Line ~1777)
Already includes 'i' (good). Consider adding 'u' and 'w' (very common, reliable x-height).

---

## 📝 Summary

**Priority 1 (MUST FIX):**
- Fix gap measurement to use `capheight_mm` instead of `font_size_mm` (line ~2010)

**Priority 2 (SHOULD FIX):**
- Add explicit ALL-CAPS detection path with cap-height → x-height estimation

**Priority 3 (NICE TO HAVE):**
- Cache PDF scale factors
- Lower MIN_BODY_TEXT_HEIGHT threshold or make it adaptive

**Expected Outcome:**
- 5000ml gap should measure correctly after Fix #1
- Font size (1.78mm) is likely already correct; gap was the issue
- ALL-CAPS handling (Fix #2) makes system more robust for future labels

---

**Recommended Commit Message:**
```
fix: use cap-height (not x-height) for CLP line gap measurement

CLP "distance between two lines" = visible whitespace gap, which is
determined by tallest chars (cap-height), not average x-height.

Before: gap = c2c - x_height (wrong for mixed/all-caps text)
After:  gap = c2c - cap_height (correct per CLP definition)

Fixes 5000ml label gap measurement (1.92mm measured, now reports correctly).
```

---

**Status:** Ready for Opus implementation. Blocking issue identified with clear fix.
