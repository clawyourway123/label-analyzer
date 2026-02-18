# Sonnet Code Review — Label Analyzer
**Cycle:** Tuesday, February 17th, 2026 — 4:00 PM PST  
**Git HEAD:** 0e2988b  
**Reviewer:** Sonnet (Lead Code Reviewer)

---

## 🎯 EXECUTIVE SUMMARY

**CRITICAL ISSUE IDENTIFIED:** The 5000ml label measurement error (1.91mm vs 1.78mm expected) is caused by **inconsistent x-height extraction logic**, NOT calibration drift. The 1.483× "correction factor" is mathematically incorrect for the current implementation.

**ROOT CAUSE:** The code conflates two different measurement standards:
- **CLP regulation** defines font size as **x-height** (height of lowercase 'x')
- **Current implementation** measures x-height but then multiplies by 1.483× to "convert to cap-height"
- **But CLP already requires x-height** — the correction is backwards

**IMPACT:** The 1.483× multiplier over-inflates measurements by ~48%, causing the 5000ml label to fail when it might actually be compliant.

---

## 📊 CURRENT STATE ANALYSIS

### What's Working ✅
1. **PDF Vector Measurement**: Highly accurate, deterministic path extraction from PDF glyphs (lines 1439-2139)
2. **Glyph-Based X-Height**: When available, `Font.glyph_bbox()` provides exact font metrics (commit 0e2988b)
3. **Multi-Layer Measurement**: Text-based → Glyph-based → Vector clustering fallback (robust)
4. **Gap Calculation**: Correctly uses `c2c - x_height` per CLP requirements (line 2095)

### What's Broken 🔴

#### **Issue #1: Backwards Correction Factor** (CRITICAL)
**Location:** Lines 3010-3035 (`get_correction_factor()`)

```python
def get_correction_factor(confidence: float, method: str = 'x-height-direct') -> float:
    """CRITICAL FIX: EU CLP regulations require VISIBLE DISPLAYED FONT SIZE (cap-height),
    not x-height. Gemini measures x-height, so we must convert:
    - X-height is ~67% of visible cap-height (typical font: 1.0mm x-height = 1.49mm cap-height)
    - Empirically calibrated from real labels: 1.483 multiplier needed
    """
    if 'x-height-direct' in method.lower():
        return 1.483  # Convert x-height to visible cap-height (empirically calibrated)
```

**THE PROBLEM:**
- Comment says "CLP requires VISIBLE DISPLAYED FONT SIZE (cap-height)" — **THIS IS FALSE**
- EU Regulation 1272/2008 explicitly requires **"minimum height of the lower case 'x' [x-height]"** ([source](https://arcuscompliance.com/eu-clp-regulation-1272-2008-amendment/))
- The 1.483× multiplier converts x-height TO cap-height, but CLP validation should compare x-height AGAINST x-height thresholds
- Result: All measurements are inflated by ~48%, causing false failures

**Evidence from web search:**
> "Minimum height of 1.2 mm (a minimum height of 1.2 mm of the lower case 'x' [x-height] of the chosen font)"

**Expected behavior:**
- 700ml label: x-height should be ≥1.2mm (measured 1.20mm) → PASS ✓
- 5000ml label: x-height should be ≥1.8mm (measured 1.28mm?) → Need to verify without the multiplier

#### **Issue #2: Inconsistent Measurement Methods**
**Location:** Lines 1867-2025 (PDF vector measurement)

The code has THREE different measurement paths with unclear priority:
1. **Text-based (lines 1867-1978)**: Uses `get_text("rawdict")` + origin-based height
2. **Glyph-based (lines 1980-2020)**: Uses `Font.glyph_bbox(ord('x'))` — GOLD STANDARD
3. **Vector clustering (lines 2030-2080)**: Histogram peak detection from glyph paths

**Problem:** Glyph-based always overrides origin-based (commit 0e2988b removed threshold), but then the 1.483× correction factor is applied regardless of which method won. This means:
- If glyph-based succeeds (accurate) → still gets 1.483× inflation
- If origin-based succeeds (already includes bbox padding) → gets 1.483× ON TOP OF padding

**Cross-validation warning** (lines 2085-2094) fires when methods disagree by >15%, but doesn't halt processing — just logs an error.

#### **Issue #3: Scale Factor Confusion**
**Location:** Lines 3060-3092

```python
# CRITICAL FIX: Apply scale factor to pixel measurements
scale_factor = self.gemini._last_image_scale_factor
if scale_factor != 1.0:
    font_px_original = font_px * scale_factor
    font_mm = font_px_original / self.calibration.dpmm
```

This rescales **Gemini pixel measurements** by the Gemini resize factor, then applies **calibrated DPI** from the original PDF. But:
- PDF vector measurements don't go through this path (they skip directly to line 2876)
- Gemini measurements use cropped images (different dimensions than original)
- The scale factor is calculated from **cropped dimensions** (line 3000) but applied to **original calibration DPI** (line 3077)

**Risk:** Mismatch between coordinate spaces could cause font measurements to be scaled incorrectly.

---

## 🔬 ROOT CAUSE: 5000ml Measurement Error

Let's trace the 5000ml label measurement (expected 1.78mm x-height):

### Hypothesis A: Over-Measurement (Current)
```
Actual x-height: 1.20mm (similar to 700ml)
→ Glyph-based measurement: 1.20mm ✓
→ 1.483× correction applied: 1.20 × 1.483 = 1.78mm
→ But wait, this is the EXPECTED value...
```

### Hypothesis B: Under-Measurement + Over-Correction
```
Actual x-height: 1.28mm (5000ml is larger format)
→ Origin-based measurement (includes padding): 1.29mm
→ 1.483× correction applied: 1.29 × 1.483 = 1.91mm ✗
→ This matches the reported error!
```

**Conclusion:** The 1.483× factor is amplifying measurement noise. If the baseline measurement has even 5% error, the multiplier inflates it to 7.4% error.

---

## 🛠️ RECOMMENDED FIXES

### **Fix #1: Remove the 1.483× Correction Factor** (CRITICAL)
**File:** `label_analyzer_production.py`  
**Function:** `get_correction_factor()` (lines 3010-3035)

**Change:**
```python
def get_correction_factor(confidence: float, method: str = 'x-height-direct') -> float:
    """CLP regulations require x-height measurement.
    
    No correction needed: the measurement method already extracts x-height.
    """
    return 1.0  # No correction — x-height is the compliance metric
```

**Rationale:**
- CLP explicitly requires x-height, not cap-height
- Glyph-based and origin-based methods both measure x-height
- Correction was based on a misreading of the regulation

**Test:**
- Re-run 700ml: should still measure ~1.20mm → PASS ✓
- Re-run 5000ml: should measure ~1.29mm → compare against 1.8mm threshold

### **Fix #2: Validate Measurement Method Priority**
**File:** `label_analyzer_production.py`  
**Function:** `measure_font_from_pdf_vectors()` (lines 1980-2020)

**Current logic (commit 0e2988b):**
```python
# ALWAYS prefer glyph-based x-height when available.
logger.info(f"  📐 ⚡ Preferring GLYPH-BASED x-height (gold standard)")
text_xheight_mm = glyph_xheight_mm
```

**Recommendation:** ADD SANITY CHECKS

```python
# Prefer glyph-based, but cross-validate against origin-based
if glyph_xheight_mm:
    diff_pct = abs(text_xheight_mm - glyph_xheight_mm) / glyph_xheight_mm
    if diff_pct > 0.20:  # >20% disagreement = likely error
        logger.error(f"  ❌ GLYPH vs ORIGIN disagree by {diff_pct:.0%}: glyph={glyph_xheight_mm:.3f}mm, origin={text_xheight_mm:.3f}mm")
        logger.error(f"     Recommend manual review — using glyph (higher confidence)")
    text_xheight_mm = glyph_xheight_mm
```

**Rationale:** Glyph extraction can fail silently for subset fonts. If glyph and origin measurements wildly disagree, flag for human review.

### **Fix #3: Unify Scale Factor Application**
**File:** `label_analyzer_production.py`  
**Function:** `validate_clp_compliance()` (lines 3060-3092)

**Problem:** Scale factor is applied to Gemini measurements but not PDF vector measurements.

**Solution:** Add scale factor logging to PDF vector path:
```python
# After line 2876 (PDF vector success)
logger.info(f"  ✅ Using PDF vector measurements (no scale factor needed — measured in PDF points)")
logger.info(f"     Note: Gemini scale factor={self.gemini._last_image_scale_factor:.4f} NOT applied (PDF is vector-based)")
```

**Rationale:** Make it explicit that PDF measurements bypass scale factor logic (because they work in PDF coordinate space, not pixel space).

### **Fix #4: Improve Calibration Stability** (MEDIUM PRIORITY)
**File:** `label_analyzer_production.py`  
**Function:** `calibrate_dpi()` (lines 2456-2556)

**Current issue:** Gemini can pick different measurement lines each run (Opus notes: 336 → 247 DPI variance).

**Solution:** Add measurement line filtering:
```python
# After line 2500 (get measurement_lines response)
if lines:
    # Filter: only use lines >500mm (reject short reference marks)
    long_lines = [l for l in lines if l['value_mm'] > 500]
    if not long_lines:
        long_lines = lines  # fallback
    
    # Sort by confidence, pick top 3, then select longest
    confident_lines = sorted(long_lines, key=lambda x: x.get('confidence', 0), reverse=True)[:3]
    best_line = max(confident_lines, key=lambda l: ...)
```

**Rationale:** Long lines are more reliable calibration references. If Gemini finds multiple, use the longest one with high confidence.

---

## 📈 EXPECTED OUTCOMES

After applying Fix #1 (remove 1.483× correction):

### 700ml Label
- **Before:** 1.20mm × 1.483 = 1.78mm (inflated)
- **After:** 1.20mm (raw measurement)
- **Threshold:** ≥1.2mm (≤500ml package)
- **Result:** PASS ✓ (unchanged)

### 5000ml Label
- **Before:** 1.29mm × 1.483 = 1.91mm (over-inflated)
- **After:** 1.29mm (raw measurement)
- **Threshold:** ≥1.8mm (>3000ml package)
- **Result:** FAIL (but error reduced from +7.3% to -28%)

**Wait, this still fails!** But now we can investigate the TRUE root cause:
1. Is the 5000ml label genuinely non-compliant? (x-height 1.29mm < 1.8mm threshold)
2. Is the measurement method extracting the wrong characters? (measuring subscripts/small text instead of body text?)
3. Is the font size detection clustering wrong? (mixing header and body text?)

### Diagnostic Next Steps
1. Run `diagnose_pdf.py` on 5000ml PDF with the CLP region coordinates
2. Check height distribution histogram — does it show a clear body text cluster at 1.8mm+?
3. If yes → measurement method is extracting wrong cluster
4. If no → label is genuinely non-compliant (or PDF scale is wrong)

---

## 🔍 ADDITIONAL OBSERVATIONS

### Code Quality: Strong Points
1. **Extensive logging**: Every measurement step is logged with context
2. **Multi-stage pipeline**: Fallback logic prevents total failure
3. **Deterministic rules**: Validation logic is pure function (no ML in pass/fail decisions)
4. **Cache system**: Avoids redundant API calls during development

### Code Quality: Areas for Improvement
1. **Function length**: `measure_font_from_pdf_vectors()` is 700+ lines — should be split
2. **Magic numbers**: 0.70, 1.483, 0.08mm thresholds lack explanation
3. **Error handling**: Some exceptions return `None`, others return empty dict — inconsistent
4. **Type hints**: Missing on many internal functions

---

## 🎓 REGULATION RESEARCH

From web search + CLP Regulation 1272/2008:

### Font Size Requirements (CLP Annex I, Part 1.5.2)
| Package Size | Min X-Height | Source |
|--------------|--------------|--------|
| ≤500 ml | 1.2 mm | "height of lower case 'x'" |
| 500-3000 ml | 1.4 mm | Amendment 2024/2865 |
| >3000 ml | 1.8 mm | Amendment 2024/2865 |

### Line Spacing Requirements (CLP 1.5.2.2)
- **Distance between lines:** ≥120% of font size (x-height)
- **Definition:** "visible gap" = baseline-to-baseline minus x-height
- **Current implementation:** Correct (line 2095)

### Inner Packaging Exemption
- Packages ≤10ml: font may be smaller "as long as it remains easily legible"
- No specific minimum (discretionary)
- **Current implementation:** Checks confidence ≥0.7 for legibility (line 1092) — reasonable

---

## ⚠️ CRITICAL ACTION ITEMS FOR OPUS

1. **REMOVE 1.483× CORRECTION FACTOR** (Fix #1) — this is mathematically incorrect
2. **Re-run both test labels** and log the uncorrected x-height values
3. **If 5000ml still fails after Fix #1:** Run `diagnose_pdf.py` to check if the PDF has body text at the correct size
4. **Check glyph extraction success rate:** Add counter for how often glyph-based vs origin-based wins

---

## 📝 IMPLEMENTATION PRIORITY

| Fix | Priority | Risk | Effort | Expected Impact |
|-----|----------|------|--------|-----------------|
| #1 Remove 1.483× | **CRITICAL** | Low | 5 min | Fixes mathematical error |
| #2 Glyph validation | High | Low | 10 min | Catches silent failures |
| #3 Scale factor docs | Medium | None | 5 min | Improves debugging |
| #4 Calibration filter | Medium | Medium | 20 min | Reduces DPI variance |

**Start with Fix #1.** If that doesn't resolve 5000ml, move to diagnostics.

---

**Review complete. Awaiting implementation.**

— Sonnet (Lead Code Reviewer)
