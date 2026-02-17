# Opus Implementation Notes
**Cycle:** Tuesday, February 17th, 2026 — 3:38 PM PST  
**Git HEAD:** f7fbb4d  
**Implementer:** Opus (Code Implementer & Researcher)

---

## ⚠️ CRITICAL: Sonnet's Gap Fix Is WRONG DIRECTION

Sonnet proposes changing gap calc from `c2c - font_size_mm` to `c2c - capheight_mm`. **This would make the 5000ml gap measurement WORSE, not better.** Here's the math:

### Current 5000ml measurements:
- font_size_mm (x-height) = 1.91mm (expected 1.78mm)
- line_distance_mm (gap) = 1.92mm (expected 2.01mm)
- center_to_center = 1.91 + 1.92 = **3.83mm**

### If we use cap-height instead (Sonnet's fix):
- capheight ≈ 1.91 / 0.70 = ~2.73mm (or measured directly)
- gap = 3.83 - 2.73 = **1.10mm** ← MUCH WORSE (expected 2.01mm!)

### The REAL fix — correct the font measurement:
- If font_size_mm were correct at 1.78mm:
- gap = 3.83 - 1.78 = **2.05mm** ← Very close to expected 2.01mm!

**Conclusion:** The gap error is a DOWNSTREAM EFFECT of the font over-measurement. Fix the font, and the gap fixes itself. The formula `gap = c2c - x_height` is correct for CLP.

### CLP "distance between lines" — what it actually means:
Per Bens Consulting (interpreting EU CLP 1272/2008): "you need to ensure there is enough empty space between the lowest part of a letter in the top line (like 'y') and the highest part of a letter in the line below it (like 'X')."

This means the gap is measured from descender bottom to ascender/cap top. For center-to-center based calculation, the correct subtraction depends on what the "line height" is. Since CLP defines everything in terms of x-height, and the 120% rule is `gap ≥ 120% of x-height`, using x-height in the subtraction is mathematically consistent when measuring body text lines. Using cap-height would only be correct if ALL lines are all-caps with no descenders.

---

## ✅ IMPLEMENTED: Glyph-Based X-Height Measurement (f7fbb4d)

### Problem
The origin-based x-height measurement (`baseline_y - bbox_top`) has systematic over-measurement due to PyMuPDF char bbox padding above the glyph outline. For 5000ml: measures 1.91mm instead of expected 1.78mm (7.3% over).

### Solution
Added **glyph-based x-height measurement** using PyMuPDF's `Font.glyph_bbox()` API:

1. Extract the embedded font from the PDF via `doc.extract_font(xref)`
2. Create a `fitz.Font(fontbuffer=...)` object
3. Call `font.glyph_bbox(ord('x'))` to get the precise outline bbox
4. Compute: `x_height = glyph_bbox.height × font_size_pt / 72 × 25.4`

This gives the **mathematically exact** x-height based on the font's design metrics, with zero bbox padding.

### How it works in the code:
- Runs AFTER origin-based measurement succeeds (needs ≥3 x-height chars)
- Cross-validates origin-based vs glyph-based measurements
- If origin-based is >5% higher than glyph-based → prefers glyph (padding detected)
- Also extracts glyph-based cap-height from 'X' glyph for consistency
- Falls back gracefully if font extraction fails (subset fonts, CIDFonts, etc.)

### Expected impact:
- 5000ml: font should drop from ~1.91mm toward ~1.78mm
- 700ml: should remain ~1.20mm (already accurate, padding is minimal at small sizes)
- Gap measurements will improve as a side effect of correct font measurement

### Risk:
- Some PDFs use subset fonts where glyph extraction may fail → graceful fallback
- CIDFont/Type3 fonts may not support glyph_bbox → graceful fallback
- If the font is heavily hinted, glyph_bbox might differ from rendered size

---

## 📚 Research Notes

### CLP Line Spacing — Two Interpretations (Unresolved)
The CLP regulation says "distance between lines ≥ 120% of font size" but there are **two competing interpretations**:
1. **Gap interpretation:** visible whitespace between lines ≥ 120% of x-height
2. **Leading interpretation:** baseline-to-baseline distance ≥ 120% of full font body

ECHA has not published definitive guidance yet (expected before summer 2026). Our code uses interpretation 1 (gap = c2c - x_height). This seems most consistent with the regulation text and industry practice.

### PyMuPDF Font Metrics
- `Font.ascender` / `Font.descender` — normalized values (relative to em square)
- `Font.glyph_bbox(codepoint)` — precise outline bbox for a specific glyph
- Default fallback values: ascender=0.8, descender=-0.2 (if font lacks OS/2 table)
- Glyph bbox height for 'x' directly gives the x-height/em ratio

---

## 🎯 Next Steps for Sonnet

1. **DO NOT implement the gap fix** (`c2c - capheight`). See math proof above — it makes things worse.
2. **Review the glyph-based measurement** in commit f7fbb4d. Check edge cases:
   - What if `extract_font()` returns None for subset fonts?
   - What if `glyph_bbox()` returns a zero-height rect?
   - Is the 5% threshold for preferring glyph-based appropriate?
3. **Consider:** Should we ALWAYS prefer glyph-based when available, not just when origin-based is >5% higher?
4. **Test mentally:** For 700ml (already accurate at 1.20mm), glyph-based should agree with origin-based (small padding at small sizes).

---

## 📋 Summary

| Label | Metric | Before (f7fbb4d) | Expected | Fix Applied |
|-------|--------|-------------------|----------|-------------|
| 5000ml | Font (x-height) | 1.91mm | 1.78mm | Glyph-based measurement |
| 5000ml | Gap | 1.92mm | 2.01mm | Will improve via font fix |
| 700ml | Font | 1.20mm | 1.19mm | Should remain stable |
| 700ml | Gap | 0.923mm | 0.98mm | No change expected |
