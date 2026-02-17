# Sonnet Code Review — Label Analyzer
**Date:** Tuesday, February 17th, 2026 — 3:15 PM PST  
**Review of:** Commit d1f8caf (Origin-based x-height + span filtering + min height)  
**Status:** CRITICAL ISSUE IDENTIFIED — Text-based measurement not being used

---

## Executive Summary

Opus's implementation of origin-based x-height measurement (d1f8caf) is **architecturally correct** but **not being applied to the problem case**. The 5000ml over-measurement (1.91mm vs 1.78mm) persists because:

1. **Text-based measurement requires ≥5 x-height chars** but likely finds fewer on the 5000ml label
2. **Falls back to unreliable vector clustering** which measures FULL character heights (including ascenders/descenders), not x-height
3. **Vector clustering can't distinguish x-height from cap-height** when uppercase is more frequent (common in hazard warnings)

**Verdict:** The fix is correct, but the fallback is broken. We need to make text-based measurement more aggressive OR improve the vector clustering to exclude ascenders/descenders.

---

## Detailed Analysis

### ✅ What Opus Got Right

1. **Origin-based measurement** (`baseline_y - cb[1]`) correctly measures x-height without below-baseline bbox padding
2. **Span size grouping** isolates body text from headers (prevents contamination)
3. **CLP regulation confirmation** — EU 1272/2008 explicitly defines font size as "height of lowercase 'x' [x-height]" (confirmed via web search)

### ❌ Critical Flaw: Fallback Vector Clustering Measures Wrong Thing

When text-based measurement fails (< 5 x-height chars), the code falls back to vector clustering. **This measures FULL character heights, not x-height:**

```python
# From measure_font_from_pdf_vectors() around line 1800
for ch in chars:
    top = min(p['y_top'] for p in ch)
    bot = max(p['y_bot'] for p in ch)
    h_mm = (bot - top) / 72 * 25.4  # ❌ Full height (includes ascenders/descenders)
    char_heights_mm.append(h_mm)
```

**Problem:** For mixed-case text, this creates a messy distribution:
- 'a', 'c', 'e': x-height only (~1.2mm) ✓
- 'b', 'd', 'h', 'k': x-height + ascender (~1.7mm) ❌
- 'p', 'q', 'g', 'y': x-height + descender (~1.7mm) ❌  
- 'H', 'T', 'A': cap-height (~1.7mm) ❌

The clustering then tries to identify the "short peak" as x-height, but if there are many ascenders/caps, the peaks blur together or the algorithm picks the wrong cluster.

**For the 5000ml label:** If text is predominantly uppercase (e.g., "DANGER", "WARNING", "PRECAUTIONARY STATEMENTS"), the text-based measurement fails to find lowercase chars, and the vector clustering incorrectly measures cap-height as "body text".

---

## Why 700ml Works But 5000ml Doesn't

| Label | Expected | Measured | Error | Likely Cause |
|-------|----------|----------|-------|--------------|
| 700ml | 1.19mm | 1.20mm | +0.8% | Text-based measurement succeeds (enough lowercase in ingredients) |
| 5000ml | 1.78mm | 1.91mm | +7.3% | Text-based measurement fails (mostly uppercase hazard text) → falls back to vector clustering → measures cap-height instead of x-height |

**Hypothesis:** The 5000ml CLP region is predominantly uppercase hazard warnings with minimal lowercase text. Text extraction finds < 5 x-height chars, falls back to vector clustering, which measures the cap-height cluster (1.91mm) instead of x-height (1.78mm).

---

## Recommended Fixes (Priority Order)

### 🔥 Fix 1: Lower Text-Based Threshold (IMMEDIATE)
**File:** `label_analyzer_production.py` line ~1764  
**Current:**
```python
if len(xheight_pts) >= 5:
    text_xheight_mm = statistics.median(xheight_pts) / 72 * 25.4
```

**Change to:**
```python
if len(xheight_pts) >= 3:  # Lower threshold — even 3 chars is reliable
    text_xheight_mm = statistics.median(xheight_pts) / 72 * 25.4
```

**Rationale:** If we can find even 3 lowercase x-height characters, the text-based measurement (using baseline-origin) is vastly more reliable than vector clustering. The median of 3 chars is still robust against outliers.

**Impact:** HIGH — Likely fixes 5000ml if it has ANY lowercase text in the region.

---

### 🔥 Fix 2: Expand X-Height Character Set (IMMEDIATE)
**File:** `label_analyzer_production.py` line ~1718  
**Current:**
```python
XHEIGHT_CHARS = set('acemnorsuvwxz')
```

**Change to:**
```python
XHEIGHT_CHARS = set('aceimnorsuvwxz')  # Added 'i' (common, reliable x-height)
# Note: Do NOT add 'b', 'd', 'h', 'k', 'l', 't' (ascenders extend above x-height)
# Note: Do NOT add 'g', 'p', 'q', 'y', 'j' (descenders extend below baseline)
```

**Rationale:** The letter 'i' is very common in English text ("in", "is", "precaution", "ingredients") and has clear x-height. This increases the chance of finding ≥3 x-height chars.

**Impact:** MEDIUM-HIGH — Improves text-based measurement coverage.

---

### 🔧 Fix 3: Validation and Fallback Warning (RECOMMENDED)
**File:** `label_analyzer_production.py` after line ~1768  
**Add:**
```python
# Validation: compare text-based vs vector-based if both available
if text_xheight_mm is not None and len(peaks) >= 1:
    vector_xheight = peaks[0][0] if len(peaks) == 1 else sorted([peaks[0][0], peaks[1][0]])[0]
    disagreement_pct = abs(text_xheight_mm - vector_xheight) / text_xheight_mm
    if disagreement_pct > 0.15:  # >15% disagreement
        logger.warning(f"  ⚠️  Text-based ({text_xheight_mm:.3f}mm) vs vector ({vector_xheight:.3f}mm) disagree by {disagreement_pct:.0%}")
else:
    if text_xheight_mm is None:
        logger.warning(f"  ⚠️  Text-based measurement FAILED (only {len(xheight_pts)} x-height chars) — relying on vector clustering (less reliable)")
```

**Rationale:** Explicitly flag when the measurement is falling back to the unreliable vector method. This helps diagnose future issues.

**Impact:** LOW (debugging aid) — doesn't fix the problem but makes it visible.

---

### 🛠️ Fix 4: Improve Vector Clustering to Exclude Ascenders (COMPLEX)
**File:** `label_analyzer_production.py` around line 1800  
**Current approach:** Measures full character height (union of all paths at same x)  
**Better approach:** For each character, identify which paths are "body" vs "ascender" vs "descender"

**Pseudocode:**
```python
# For each character (group of overlapping paths at same x):
body_top = min(p['y_top'] for p in char_paths)
body_bot = max(p['y_bot'] for p in char_paths)
char_height = body_bot - body_top

# But separate ascender/descender paths:
# - If a path extends >30% above the median top → ascender
# - If a path extends >30% below the median bot → descender
# Only measure paths in the "body" band

median_top = statistics.median([p['y_top'] for p in all_char_paths_on_line])
median_bot = statistics.median([p['y_bot'] for p in all_char_paths_on_line])
body_height = median_bot - median_top  # X-height estimate
```

**Rationale:** This makes vector clustering more accurate by filtering out ascender/descender strokes BEFORE measuring height.

**Impact:** HIGH (if implemented correctly) — Makes fallback method reliable, but complex to implement.

**Recommendation:** Defer until Fixes 1-3 are tested. If 5000ml still fails after lowering threshold and expanding char set, implement this.

---

## Opus's Pushback on 0.52 Ratio — I Was Wrong

**Status:** ✅ Opus is correct, I (Sonnet) was wrong.

**My mistake:** I confused **x-height/em-size** (0.40-0.52) with **x-height/cap-height** (0.68-0.72).

**Correct ratios for CLP-typical fonts (Arial, Helvetica, Univers):**
- x-height / cap-height ≈ **0.70-0.72**
- x-height / em-size ≈ 0.44-0.52 (this is what I incorrectly cited)

**The current 0.70 multiplier is correct.** Do not change to 0.52 (would underestimate x-height by 26%).

---

## Testing Plan

1. **Add debug logging** to show which measurement method was used:
   ```
   "TEXT-BASED x-height: 1.20mm (from 7 lowercase chars)"
   "FALLBACK to vector clustering (only 2 x-height chars found)"
   ```

2. **Test 5000ml label** after implementing Fix 1 + Fix 2:
   - Expect: Text-based measurement now succeeds (≥3 chars)
   - Expect: Font size drops from 1.91mm → ~1.78mm

3. **If still fails**, check logs:
   - How many x-height chars were found?
   - What's the distribution of font sizes in the region?
   - Is there a header contaminating the body text?

4. **Validate against 700ml** (should remain at ~1.20mm)

---

## Recommended Implementation Order

1. ✅ **Immediate:** Implement Fix 1 (lower threshold to 3 chars) + Fix 2 (add 'i' to char set)
2. ✅ **Immediate:** Add Fix 3 (validation logging)
3. 🧪 **Test:** Run on both 700ml and 5000ml labels, check logs
4. ⏸️ **If still broken:** Implement Fix 4 (ascender filtering in vector clustering)

**Expected outcome:** After Fix 1+2, the 5000ml label should measure correctly via text-based method, matching the 700ml accuracy (~1% error).

---

## Additional Notes

### CLP Regulation Research (confirmed)
- **EU 1272/2008:** Font size = "height of lowercase 'x' [x-height]"
- **Arcus Compliance source:** "Minimum height of 1.2 mm of the lower case 'x' [x-height] of the chosen font"
- **Thresholds by package size:**
  - ≤500ml: 1.2mm x-height
  - 500-3000ml: 1.4mm x-height  
  - >3000ml: 1.8mm x-height
  - Inner packaging ≤10ml: no minimum (but must be legible)

### Line Spacing (Gap) Issue
The 5000ml gap measurement (1.92mm vs 2.01mm expected) may also improve once font size is corrected, since gap calculation uses:
```python
gap = center_to_center - capheight
```

If `capheight` is currently over-measured (due to wrong font size), the gap will be under-measured. Once font size is fixed, gap should auto-correct.

---

**Status:** ⚠️ ACTIONABLE — Fixes identified, ready for Opus to implement.  
**Next review:** After fixes are implemented and tested on both labels.
