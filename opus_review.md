# Opus Implementation Notes
**Cycle:** Tuesday, February 17th, 2026 — 3:52 PM PST  
**Git HEAD:** 0e2988b  
**Implementer:** Opus

---

## ✅ CHANGES IMPLEMENTED THIS CYCLE

### 1. Always Prefer Glyph-Based X-Height (Sonnet Fix #2, Option 1)
**Commit:** 0e2988b

Removed the 5% threshold gate. Now when glyph-based measurement is available, it's **always** used over origin-based.

**Rationale:** Glyph bbox comes from the font designer's actual outline data — it's mathematically exact. The origin-based method measures `baseline_y - bbox_top`, which still includes bbox padding from PyMuPDF's text extraction. The 5% threshold was letting padded measurements slip through on the 5000ml label.

**Risk:** Low. If the glyph extraction succeeds, the data is authoritative. The only risk is if `glyph_bbox()` returns wrong values for subset fonts, but PyMuPDF handles this well — it returns None/zero for missing glyphs, which we already check.

### 2. Glyph Extraction Failure Alerting (Sonnet Fix #4)
Changed `logger.debug` → `logger.warning` for glyph extraction failures so they're visible in normal log output.

### 3. Cross-Validation Escalation (Sonnet Fix #3)
- >15% disagreement: `logger.error` + human review recommendation  
- 5-15% disagreement: `logger.warning`  
- <5%: `logger.info` (agreement confirmed)

---

## 🔬 ANALYSIS

### Why This Should Fix 5000ml

The 5000ml label measures 1.91mm via origin-based but expected 1.78mm (+7.3% over). If the glyph-based measurement was available but not engaging due to the 5% threshold being borderline, removing the gate ensures we get the exact font metric.

**Key question I can't verify without running:** Does glyph extraction actually succeed for the 5000ml PDF's font? If the font is a CIDFont or heavily subset, `extract_font()` might return no buffer, and we'd still fall back to origin-based. The new warning-level logging will make this immediately visible.

### If Glyph Extraction Fails for 5000ml

If next run still shows 1.91mm, it means glyph extraction is failing silently (now it'll warn). In that case, we should:

1. **Use `Font.ascender` property** as alternative — PyMuPDF exposes `font.ascender` which gives the ascender height relative to font size. Combined with `font.descender`, we can compute x-height as `1.0 - ascender_overshoot`. But this is less precise.

2. **Apply a correction factor to origin-based** — If we know origin-based consistently over-measures by ~7%, we could apply a font-size-dependent correction. But this is fragile.

3. **Try multiple 'x' characters** — Instead of one `glyph_bbox(ord('x'))`, try 'o', 'e', 'a' and take the median. Some subset fonts include certain glyphs but not others.

### Gap Measurement

I agree with Sonnet: gap = c2c - x_height is correct per CLP. The 5000ml gap error (1.92mm vs 2.01mm expected) should improve as a downstream effect of fixing font measurement, since `line_distance_mm = max(0, center_to_center_mm - font_size_mm)`. Lower font_size_mm → higher gap.

If font drops from 1.91→1.78, and c2c stays the same, gap would increase by ~0.13mm (from 1.92 to ~2.05mm), which is closer to the expected 2.01mm. ✅

---

## 📋 NEXT CYCLE TODO

1. **Run both labels** and check logs for glyph decision path
2. If 5000ml still wrong → investigate `Font.ascender`/`Font.descender` as alternative metrics
3. If 700ml regresses → add size-dependent logic (use glyph only when origin > glyph + epsilon)
4. Consider adding `font.ascender` cross-validation as a third measurement source

---

## 🔍 RESEARCH NOTES

### PyMuPDF Font.glyph_bbox()
- Returns `Rect` in font units normalized to font size 1.0
- Actual size = `glyph_bbox.height * font_size_pt`
- For 'x', height gives exact x-height ratio
- Also available: `Font.ascender` (max ascender) and `Font.descender` (max descender)
- These are font-level metrics, not glyph-specific — less precise but always available

### CLP x-height
- EU Regulation 1272/2008 defines font size as x-height of lowercase letters
- ECHA guidance confirms: "minimum height of the lower case 'x'"
- Glyph-based measurement is the closest we can get to the typographic definition

---

**Implementation complete. Awaiting test results.**

— Opus (Code Implementer)
