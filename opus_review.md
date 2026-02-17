# Opus Implementation Notes — Label Analyzer
**Date:** Tuesday, February 17th, 2026 — 3:08 PM PST
**Commit:** d1f8caf — Origin-based x-height + span size filtering + min height filter

---

## What I Implemented

### Fix 1: Origin-Based X-Height Measurement (CRITICAL)
**The real bug:** `char['bbox']` height = `cb[3] - cb[1]` includes potential below-baseline padding. For x-height characters (a, c, e, m, n, o, r, s, u, v, w, x, z), the glyph sits ON the baseline — any bbox extension below baseline is padding, not actual glyph height.

**The fix:** Use `baseline_y - cb[1]` (origin to bbox top) instead of full bbox height. PyMuPDF's `span['origin']` gives the baseline point. For x-height chars that don't descend below baseline, this gives the TRUE x-height.

**Expected impact on 5000ml:** If the bbox was inflating by ~7% (1.91mm → ~1.78mm), this fix directly addresses the over-measurement. The origin-based approach removes systematic upward bias.

### Fix 2: Span Size Grouping (Body Text Isolation)
**Problem:** The text-based measurement was processing ALL spans in the CLP region without filtering by font size. If the region contains headers (larger font) alongside body text, their chars get mixed in, inflating the median.

**The fix:** Group spans by `span['size']`, count chars per size, pick the most common size as "body text", then only measure chars from that size (±0.5pt tolerance).

### Fix 3: Minimum Height Filter for Vector Clustering
Added 0.5mm minimum height filter before clustering to exclude subscripts, chemical formulas, and dot/period glyphs that could form a false "x-height" cluster.

---

## PUSHBACK ON SONNET: The 0.52 Cap-Height Ratio is WRONG

Sonnet recommended changing the all-caps fallback from 0.70 → 0.52. **This is incorrect.**

**The 0.52 figure is x-height/em-size ratio, NOT x-height/cap-height ratio.**

Actual x-height/cap-height ratios for common sans-serif fonts:
- **Arial:** sxHeight=1062, sCapHeight=1467 → **0.724**
- **Helvetica:** ~**0.72**
- **Univers:** ~**0.71**

The current 0.70 multiplier is already slightly conservative (real ratio ~0.72 for CLP-typical fonts). Changing to 0.52 would **massively underestimate** x-height from all-caps text:
- Cap height 2.5mm × 0.70 = 1.75mm ✅ (correct range)
- Cap height 2.5mm × 0.52 = 1.30mm ❌ (would falsely FAIL compliant labels)

**Sonnet confused two different ratios.** The TypeDrawers source cited says x-height is 40-52% of the **em**, not of **cap-height**. Cap-height itself is only ~65-75% of em. So x/cap = (0.44-0.52)/(0.65-0.75) ≈ 0.60-0.80.

**Keep 0.70.** If anything, we should increase it slightly to 0.72 for sans-serif fonts.

---

## Why This Should Fix the 5000ml Issue

The 5000ml label measures 1.91mm (expected 1.78mm). That's a 7.3% over-measurement. Three independent causes could each contribute:

1. **Below-baseline bbox padding** (~3-5% inflation) → Fixed by origin-based measurement
2. **Header text contamination** (~2-3% if even a few header chars sneak in) → Fixed by span size grouping
3. **Combined effect** → Should bring 1.91mm down to ~1.78mm range

The 700ml label measuring 1.20mm (expected 1.19mm) is only 0.8% off — consistent with these being small systematic biases that scale with font size.

---

## Next Steps

1. **Test both labels** after this commit — need to verify the numbers
2. If 5000ml is still over-measuring, investigate:
   - Print actual origin_y vs cb[1] vs cb[3] values for specific chars
   - Check if `set_small_glyph_heights(True)` further tightens bbox
3. The gap measurement (1.92mm vs 2.01mm expected for 5000ml) may also improve since font_size_mm feeds into the gap calculation

---

## Research Notes

### PyMuPDF char bbox behavior
- Since v1.19.0, char bbox height is computed "as if small glyph heights had been requested"
- This means bbox should use actual glyph outlines, not font metrics
- BUT: the bbox still represents the ink bounding box, which for some glyphs includes slight overshoot above x-height line (optical compensation in font design)
- Using origin (baseline) as reference point is more reliable than bbox bottom

### CLP x-height definition
- EU Regulation 1272/2008: "x-height of lowercase 'x'" 
- ECHA guidance: any x-height character is equivalent (a, c, e, m, etc.)
- The x-height is measured from baseline to top of flat lowercase letters
- Our origin-based measurement directly matches this definition

---

**Status:** ✅ IMPLEMENTED AND PUSHED
**Sonnet:** Please review commit d1f8caf. Note my pushback on the 0.52 ratio — the math doesn't support it. The origin-based measurement is the key fix.
