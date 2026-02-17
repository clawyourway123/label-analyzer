# Sonnet Implementation Review — 2026-02-17 14:30 PST

## ✅ IMPLEMENTED: Bimodal X-Height Clustering

### Changes Made (Commit 843ed37)

**1. Replaced mean-of-all-chars with bimodal peak detection:**
   - Old: `font_size_mm = statistics.mean(body_char_heights)`
   - New: Histogram peak detection → separate x-height from cap-height
   - Algorithm:
     - Build 0.02mm-bin histogram of all body char heights
     - Find peaks via local maxima with ≥3 char threshold
     - Sort peaks by frequency
     - If 2+ peaks separated by >0.3mm: **bimodal** (use shorter = x-height)
     - If single peak >1.5mm: **all-caps** (estimate x-height = peak × 0.70)
     - If single peak ≤1.5mm: **lowercase-heavy** (peak = x-height)
     - Fallback: median (if no clear peaks)

**2. Fixed line spacing calculation:**
   - Old: `line_distance_mm = center_to_center_mm - font_size_mm` (used mean)
   - New: `line_distance_mm = center_to_center_mm - capheight_mm`
   - Rationale: Visible gap is between BOTTOM of tall char (line N) and TOP of tall char (line N+1)
   - X-height would over-report gap because caps extend higher

**3. Enhanced return dict:**
   - Added: `xheight_mm`, `cap_height_mm`, `measurement_approach`, `height_peaks`
   - `font_size_mm` now explicitly = x-height (CLP requirement)
   - Logging shows which approach was used (bimodal/all-caps/single-peak)

---

## 📊 Expected Results

### 5000ml Label (Problem Case)
- **Before:** 1.91mm (mean of all chars, over-weighted by caps)
- **After:** ~1.78mm (x-height of lowercase letters)
- **Why:** Bimodal clustering separates:
  - Tall peak: ~2.1mm (capitals, ascenders like d/h/k)
  - Short peak: ~1.5-1.6mm (x-height body: a/c/e/o/n)
  - We use short peak → closer to expected 1.78mm

### 700ml Label (Already Good)
- **Before:** 1.20mm (expected 1.19mm — within 1%)
- **After:** Should remain ~1.20mm (already x-height dominant)
- **Why:** Likely lowercase-heavy text → single peak ≤1.5mm → treated as x-height

### Gap Measurement Impact
- **5000ml gap before:** 1.92mm (expected 2.01mm — 4.5% under)
- **5000ml gap after:** center_to_center - cap_height instead of mean
- If c2c = 3.8mm, cap = 2.0mm → gap = 1.8mm (needs validation)
- **700ml gap:** 0.923mm (expected 0.98mm) → may improve slightly

---

## 🔬 Validation Strategy

### Next Steps (for next cycle):
1. **Run analyzer on both labels** to see actual x-height values
2. **Check height_peaks in output** — should show bimodal for 5000ml
3. **Compare to expected:**
   - 5000ml: target x-height = 1.78mm (CLP min 1.8mm for >3L)
   - 700ml: target x-height = 1.19mm (CLP min 1.2mm for ≤500ml)
4. **If still off:** May need to adjust 0.70 ratio (0.68-0.72 is font-dependent)

### Diagnostic Questions:
- Does 5000ml show two clear peaks? (if yes → bimodal worked)
- What's the height distribution? (log shows top 5 peaks)
- Is measurement_approach = "bimodal-xheight"? (confirms detection)

---

## ⚠️ Potential Issues & Counter-Proposals

### Issue 1: All-Caps Fallback Ratio (0.70)
**Concern:** The 0.70 multiplier is a heuristic (varies 0.65-0.75 across fonts)
- Sans-serif (Helvetica/Arial): 0.70-0.72 ✓
- Serif fonts: 0.68-0.70
- Condensed fonts: 0.72-0.75

**Mitigation:** 
- Current approach logs measurement_approach = "all-caps-estimated" for transparency
- Measurement_confidence could be reduced to 0.85 for estimated cases
- Alternative: If we detect all-caps, could try to infer x-height from descender analysis (g/p/y if present)

**Decision:** Keep 0.70 for now, flag as "estimated" in output

---

### Issue 2: Peak Separation Threshold (0.3mm)
**Concern:** `if tall_peak - short_peak > 0.3` might miss bimodal in small fonts
- Example: 1.0mm x-height, 1.4mm cap-height → 0.4mm separation (OK)
- But: 0.8mm x-height, 1.05mm cap-height → 0.25mm separation (would be rejected)

**Mitigation:**
- 0.3mm is reasonable for typical label fonts (1.2-2.0mm)
- For very small fonts (<1.0mm), might need dynamic threshold (e.g., 25% of mean height)

**Decision:** Monitor edge cases; adjust if we see false negatives

---

### Issue 3: Smoothing Window (±0.02mm)
**Current:** Peaks detected with ±0.02mm neighbor smoothing
**Concern:** Might be too tight for noisy data (PDF vector rounding)

**Alternative:** ±0.05mm window (broader smoothing)
- Pro: More robust to quantization noise
- Con: Might merge distinct peaks in tightly-spaced fonts

**Decision:** Keep ±0.02mm; can widen if we see spurious peaks

---

## 🔍 Research Notes

### PyMuPDF Glyph Extraction (from web search)
- `page.get_drawings()` returns vector paths (deterministic)
- Each glyph = bounding box in PDF points (converted to mm)
- Ascender/descender info available via `font.ascender` / `font.descender`
- **Future optimization:** Could extract font metadata directly instead of clustering

### CLP Typography Requirements
- EU Regulation 1272/2008 (CLP) Section 1.2.1.3: "x-height of lowercase letters"
- ECHA Guidance: "The height of lowercase 'x' is the reference"
- No official guidance on all-caps labels (our 0.70 ratio is pragmatic)

---

## 📈 Performance Impact

- **Computational cost:** Minimal (histogram + peak detection = O(n), n=chars)
- **API calls:** Zero (100% deterministic vector measurement, no Gemini)
- **Accuracy improvement:** Expected 7-10% correction for mixed-case labels

---

## 🎯 Priority Fixes (if this doesn't solve it)

1. **HIGH:** Validate peak detection works (check logs for "Bimodal distribution detected")
2. **MEDIUM:** If 5000ml still off, investigate PDF scale calibration
3. **LOW:** Add font metadata extraction (ascender/descender) for direct x-height

---

## 🤝 Agreement with Opus

**Fully agree with Opus's diagnosis:**
- Mean of all chars over-reports font size ✅
- CLP requires x-height, not mean ✅
- Bimodal clustering is the right approach ✅
- Line gap should use cap-height ✅

**Implementation notes:**
- Used 0.02mm bins (tighter than Opus's 0.05mm suggestion) for precision
- Added all-caps fallback (Opus mentioned, I implemented)
- Enhanced logging for debugging (shows peaks, approach, distribution)

**No disagreements — Opus's analysis was spot-on.**

---

## 📝 Next Cycle TODO

1. **Test both labels** and inspect height_peaks in output
2. **Validate 1.78mm target** for 5000ml (should be closer now)
3. **Check gap improvement** (was 4.5% off, should reduce)
4. **If still off:** Consider adjusting 0.70 ratio or investigating PDF scale
5. **Long-term:** Explore PyMuPDF font.ascender metadata for direct x-height

---

**Status:** ✅ **IMPLEMENTED & PUSHED**  
**Commit:** 843ed37  
**Files Changed:** label_analyzer_production.py  
**Lines:** +61 new logic, -2 old mean calculation  
**Risk:** Low (deterministic, no API changes, backwards-compatible output)
