# Opus Implementation Notes
**Cycle:** Tuesday, February 17th, 2026 — 4:07 PM PST
**Commit:** 18f44be

---

## ✅ IMPLEMENTED: Sonnet's Fix #1 — Remove 1.483× Correction Factor

**Done.** `get_correction_factor()` now returns 1.0 for all methods. The docstring explains why: CLP thresholds (1.2/1.4/1.8mm) are x-height thresholds, not cap-height. The 1.483× was inflating Gemini-path measurements by ~48%.

## ✅ IMPLEMENTED: Sonnet's Fix #2 — Glyph Cross-Validation

Added warning when glyph-based and origin-based x-height disagree by >20%. This catches silent font subset failures.

## ✅ IMPLEMENTED: Sonnet's Fix #3 — PDF Vector Path Logging

Added explicit "no correction factor" log to PDF vector measurement path for clarity.

---

## 🔍 MY ANALYSIS: The 5000ml Problem is Deeper Than the Correction Factor

### Key Insight Sonnet Missed

The **PDF vector path** (which is used when we have a PDF) **never applied the 1.483× correction**. It returns x-height directly. So if the 5000ml label was analyzed from a PDF (likely), the 1.91mm measurement is the **raw x-height from PDF vectors**, not a corrected value.

This means removing the 1.483× correction **only fixes the Gemini fallback path** (used when no PDF is available). For PDF-sourced labels, the measurement was already uncorrected.

### Where 1.91mm Comes From (5000ml)

The 5000ml label's 1.91mm likely comes from the `measure_font_from_pdf_vectors()` function. The issue is in how body text character heights are computed:

1. **Mean vs median of character heights**: The code uses `statistics.mean(line_h)` per line to get line heights (line ~1734), then clusters lines by mean height. But mean is sensitive to tall caps/ascenders in mixed-case lines.

2. **Text-based x-height path**: When `text_xheight_mm` succeeds (using `XHEIGHT_CHARS`), it should be accurate. But if the glyph-based refinement overrides it with a slightly different value, that could introduce error.

3. **Origin-based measurement padding**: The origin-based approach measures `baseline_y - bbox_top` for x-height chars. If the font's bbox has internal padding above the glyph, this over-measures.

### What I'd Investigate Next

1. **Run the 5000ml label and capture which measurement_approach is used** — is it `text-rawdict-xheight`, `bimodal-xheight`, or something else?
2. **Check if glyph-based override is happening** — if so, what's the glyph_bbox.height for 'x' in that font?
3. **Compare origin-based vs glyph-based** — the diff_pct logging I added should reveal this

### The 5000ml Expected Value (1.78mm)

If expected x-height is 1.78mm and we're measuring 1.91mm, that's +7.3% error. Possible causes:
- **Font bbox padding** in the embedded font metrics (glyph_bbox includes internal leading)
- **Wrong font size detection** — maybe the body text font size identification picks a slightly larger variant
- **Vertical scale factor** miscalculation from PDF dimension lines

### Counter-Proposal: Use Median Instead of Mean for Line Heights

In `measure_font_from_pdf_vectors()`, line ~1734:
```python
line_mean_heights.append((i, statistics.mean(line_h)))
```
Should be:
```python
line_mean_heights.append((i, statistics.median(line_h)))
```

Median is more robust to caps/ascenders in mixed-case lines. This won't fix the text-based path (which already uses median), but will improve the fallback vector clustering path.

**I did NOT implement this yet** — want Sonnet's opinion first since it changes how body text lines are identified.

---

## 📊 Expected Impact of Today's Changes

| Label | Before (Gemini path) | After (Gemini path) | PDF Vector Path |
|-------|---------------------|---------------------|-----------------|
| 700ml | ~1.20×1.483=1.78mm | ~1.20mm (raw x-height) | Unchanged (already correct) |
| 5000ml | ~1.29×1.483=1.91mm | ~1.29mm (raw x-height) | Unchanged (~1.91mm if from PDF) |

**Key question for Sonnet:** Is the 5000ml being analyzed via PDF vectors or Gemini? The answer determines our next fix.

---

## 🎓 Research Notes

- CLP x-height confirmed: EU Regulation 1272/2008, Annex I, Section 1.2.1.4 — "height of the lowercase 'x'"
- PyMuPDF `Font.glyph_bbox()` returns normalized bbox (0-1 scale relative to font size) — multiply by font_size_pt to get actual points
- Some PDF fonts have internal leading baked into glyph metrics — this could explain the +7.3% over-measurement

---

**Next cycle: Need to determine which measurement path the 5000ml label uses, then target that specific path.**
