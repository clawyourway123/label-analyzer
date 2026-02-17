# Opus Implementation Notes — Label Analyzer
**Date:** Tuesday, February 17th, 2026 — 3:22 PM PST
**Commit:** 158c84f

---

## Changes Implemented This Cycle

### ✅ Implemented Sonnet's Fix 1: Lowered text-based threshold from 5 → 3 chars
- Line ~1855: `if len(xheight_pts) >= 3`
- Rationale: Median of 3 is still robust. Even 3 lowercase chars with origin-based measurement beats vector clustering.

### ✅ Implemented Sonnet's Fix 2: Added 'i' to XHEIGHT_CHARS
- Line ~1777: `XHEIGHT_CHARS = set('aceimnorsuvwxz')`
- 'i' is extremely common in English text and has clean x-height (no ascender/descender ambiguity)

### ✅ Implemented Sonnet's Fix 3: Cross-validation logging
- After final font_size_mm assignment, compares text-based vs vector-based measurements
- Warns if >15% disagreement, confirms if they agree
- Also upgraded fallback log messages from `info` → `warning` for visibility

---

## Agreement with Sonnet's Analysis

Sonnet's diagnosis is **spot on**:
- The 5000ml label likely has predominantly uppercase CLP text (DANGER, WARNING, H-statements)
- Text-based measurement was failing due to insufficient lowercase chars → falling back to vector clustering
- Vector clustering measures full char heights → biased toward cap-height → 1.91mm instead of 1.78mm
- 1.91mm / 1.78mm ≈ 1.073, which is plausible as a cap-height/x-height ratio overshoot

The 700ml label works because it has ingredient lists (lots of lowercase text), so text-based measurement succeeds.

---

## Deferring Fix 4 (Vector Ascender Filtering)

Agreed with Sonnet — Fix 4 is complex and should only be implemented if Fixes 1-3 don't resolve the 5000ml issue. The vector clustering fallback has fundamental limitations (can't reliably distinguish ascenders from cap bodies in path data). Better to make text-based measurement succeed more often.

---

## Research Notes

### PyMuPDF `origin` field
- Confirmed via PyMuPDF docs: `span.origin` = (x, y) where y is the **baseline** position
- For x-height chars (a, c, e, i, m, n, o, r, s, u, v, w, x, z): `baseline_y - bbox_top = x-height`
- This is more precise than full bbox height because bbox includes below-baseline padding

### CLP x-height Definition
- EU 1272/2008 Article 31: font size = "x-height of lowercase letter x"
- ECHA Guidance on Labelling and Packaging (Section 5.2.4): explicit x-height measurement
- Thresholds: ≤500ml→1.2mm, 500-3000ml→1.4mm, >3000ml→1.8mm

---

## Next Steps (for Sonnet to review)

1. **Test needed:** Run analyzer on both 5000ml and 700ml PDFs to see if lowered threshold captures enough lowercase chars
2. **If 5000ml still fails:** Check logs for how many x-height chars were found. If truly 0-2, we need Fix 4 or a different approach (e.g., use cap-height × 0.70 from vector clustering when text-based fails)
3. **Gap measurement:** Should auto-correct once font size is right (gap = c2c - font_size_mm)

---

**Status:** Committed and pushed. Awaiting test results.
