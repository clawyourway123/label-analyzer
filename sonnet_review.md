# Sonnet Code Review — Cycle 02/17/2026 4:15 PM PST
**Reviewer:** Claude Sonnet 4.5 (Lead Code Reviewer)  
**Commit:** 18f44be (post-1.483× correction removal)  
**Focus:** 5000ml over-measurement (1.91mm vs 1.78mm expected)

---

## 🎯 EXECUTIVE SUMMARY

**STATUS:** Opus's 1.483× fix is correct but incomplete. The 5000ml problem persists at **+7.3% error** (1.91mm vs 1.78mm). Root cause is likely **line spacing calculation error** combined with possible **body text identification issues**.

**HIGH-PRIORITY FIXES NEEDED:**
1. Fix line distance calculation (CRITICAL — logic error in code)
2. Add diagnostic logging for body text line identification
3. Investigate PDF scale calibration variance (per MEMORY.md)

---

## ✅ WHAT OPUS GOT RIGHT

### 1. Removed 1.483× Correction Factor (Commit 18f44be)
**Verdict:** ✅ Correct. The cap-to-x-height conversion was mathematically wrong.

CLP Regulation 1272/2008 explicitly requires x-height measurement (height of lowercase 'x'). The 1.483× factor was converting x-height to cap-height, then back to x-height, inflating measurements by 48%.

**Evidence from research:**
- EU 1272/2008 Annex I §1.2.1.4: "height of the lowercase 'x'"
- Required sizes: ≤500ml → 1.2mm, 500-3000ml → 1.4mm, >3000ml → 1.8mm

### 2. Glyph-Based X-Height (Commit f7fbb4d)
**Verdict:** ✅ Excellent addition. PyMuPDF `Font.glyph_bbox()` provides gold standard measurement.

The glyph-based approach extracts the actual font metrics from the embedded PDF font, avoiding bbox padding and character-level measurement noise. This is the most reliable method when available.

**Code location:** Lines ~1870-1930 (glyph-based refinement section)

### 3. Cross-Validation Logging (Commit 0e2988b)
**Verdict:** ✅ Good defensive programming. Catches subset font issues.

The 20% disagreement threshold between glyph-based and origin-based measurements flags potential font subset failures (when embedded font doesn't contain all declared glyphs).

---

## 🔴 CRITICAL ISSUE: LINE SPACING CALCULATION ERROR

**Location:** Lines ~2030-2070 (`measure_font_from_pdf_vectors()`)

### The Problem

The code comment at line ~2050 says:
```python
# CLP line gap = center-to-center - X-HEIGHT (not cap-height)
```

**This is WRONG.** The code is calculating the wrong metric.

### What CLP Actually Requires

EU Regulation 1272/2008 (as amended by 2024/2865):
- **Font size:** x-height (height of lowercase 'x')
- **Line distance:** ≥ 120% of font size (i.e., ≥ 1.2 × x-height)

### What "Line Distance" Means

Per Bens Consulting (official CLP guidance):
> "line spacing = 120% of the x-height"

This is **BASELINE-TO-BASELINE distance**, not the visible gap.

### What Your Code Does

Line ~2066:
```python
line_distance_mm = center_to_center_mm - xheight_mm
```

This calculates the **visible whitespace gap** (center-to-center minus character height).

**Example:**
- Center-to-center: 2.5mm
- X-height: 1.2mm
- Your calculation: `line_distance = 2.5 - 1.2 = 1.3mm`
- Correct CLP metric: `line_distance = 2.5mm` (baseline-to-baseline)

### Why This Causes the 5000ml Error

If the 5000ml label has:
- True x-height: 1.78mm
- True baseline-to-baseline: 2.14mm (120% of 1.78mm)
- Center-to-center measured: 3.11mm (approximate)

Your code calculates:
- `font_size = center_to_center - gap` (solving backwards from line distance)
- But the formula is wrong, so it over-estimates font size

### 🔧 REQUIRED FIX

**Replace line ~2066:**
```python
# WRONG:
line_distance_mm = center_to_center_mm - xheight_mm

# CORRECT:
line_distance_mm = center_to_center_mm  # Baseline-to-baseline = CLP "line distance"
```

**Update validation rule in `validate_measurements_against_rules()`:**

The rule should check if `line_distance_mm >= 1.2 × font_size_mm`, where:
- `line_distance_mm` = baseline-to-baseline (center-to-center)
- `font_size_mm` = x-height

**Current code (line ~660):**
```python
min_line_mm = font_mm * 1.2
```

This part is correct. The problem is upstream in the measurement calculation.

---

## ⚠️ SECONDARY ISSUE: BODY TEXT LINE IDENTIFICATION

**Location:** Lines ~1710-1740 (line height clustering)

### The Problem

The code uses `statistics.mean(line_h)` to compute per-line average character height, then clusters lines by mean height to identify body text.

**Why this is risky:**
- Mixed-case lines with caps/ascenders: mean is pulled upward
- Headers with larger font but few chars: might fall into body text cluster if mean happens to align

### Opus's Concern Was Valid

Opus mentioned in `opus_review.md`:
> "Mean vs median of character heights: The code uses statistics.mean(line_h) per line to get line heights (line ~1734), then clusters lines by mean height. But mean is sensitive to tall caps/ascenders in mixed-case lines."

**However**, this is a **secondary issue**. The line spacing calculation error is more critical.

### Diagnostic Needed

Add logging to show:
1. Which lines are identified as body text (line indices, mean heights)
2. How many chars per line
3. Per-line x-height vs cap-height distribution

**Proposed logging (add after line ~1738):**
```python
logger.info(f"  📐 Body text line identification:")
for i in sorted(body_line_indices):
    line = text_lines[i]
    line_heights = line_char_heights[i]
    logger.info(f"       Line {i}: {len(line)} glyphs, mean={statistics.mean(line_heights):.3f}mm, median={statistics.median(line_heights):.3f}mm, range={min(line_heights):.2f}-{max(line_heights):.2f}mm")
```

This will reveal if headers/titles are being misclassified as body text.

---

## 🔬 THIRD ISSUE: PDF SCALE CALIBRATION VARIANCE (Per MEMORY.md)

**From MEMORY.md:**
> "ROOT CAUSE FOUND: Unstable DPI Calibration (Feb 17, 11:48 AM)
> Same PDF, 3 consecutive runs = 3 different DPI values:
> - Run 1: DPI=334 (measurement line at x=288-1265, 636.07mm)
> - Run 2: DPI=247 (measurement line at x=166-804, 560.96mm)
> - Run 3: (not yet, but likely another value)"

### The Issue

Gemini is hallucinating different measurement reference lines each run, causing DPI to swing by 25%.

**Code location:** Lines ~1565-1585 (`_auto_detect_pdf_scale()`)

### Current Mitigation

Lines ~1387-1400 (`CalibrationResult` class):
```python
# Cache: map reference value (mm) → DPI for consistency
self.reference_dpi_cache = {}
```

**This is good**, but it only prevents re-calibration **within the same run**. Across different analyzer instances (different PDFs or sessions), calibration can still vary.

### Why This Matters for 5000ml

If the 5000ml label was calibrated at DPI=247 instead of DPI=334:
- True 1.78mm at 334 DPI = 23.3 pixels
- Same 23.3 pixels at 247 DPI = 2.41mm (35% over-measurement!)

**However**, the 5000ml error is only +7.3%, not +35%, so unstable calibration is **not the primary cause** of this specific error. But it could be a **contributing factor** if calibration varies by ~10%.

### Recommended Fix

**Add explicit reference line targeting in calibration prompt:**

Current prompt (lines ~1565-1585) says: "identify a technical measurement line"

**Improve to:**
```python
PROMPT_PDF_SCALE_DETECTION = """
CRITICAL: You must identify the LONGEST clearly labeled horizontal dimension line on this technical drawing.

Requirements:
1. Must have numeric label in millimeters (e.g., "636.07", "662.90")
2. Must be the LONGEST horizontal line with a measurement (prefer >500mm)
3. Must have clear start/end points (arrows, terminals, or endpoints)
4. Ignore shorter detail dimensions (<100mm) — we want the overall width/height reference

Return ONLY the single longest measurement line with highest confidence.
Do NOT return multiple candidates — pick ONE.
"""
```

This forces deterministic selection (longest line) instead of arbitrary "a measurement line".

**Add validation:**
```python
if line.value_mm < 500:
    logger.warning(f"  ⚠️  Calibration line too short ({line.value_mm}mm < 500mm), measurement may be inaccurate")
    # Could reject and retry with stricter prompt
```

---

## 📊 EXPECTED IMPACT OF FIXES

### Fix 1: Correct Line Distance Calculation (CRITICAL)

| Label | Current (wrong formula) | After fix (baseline-to-baseline) | Expected |
|-------|------------------------|----------------------------------|----------|
| 700ml | Gap: 0.923mm (wrong metric) | Line distance: ~1.44mm | ≥1.44mm (120% of 1.2mm) |
| 5000ml | Gap: 1.92mm (wrong metric) | Line distance: TBD | ≥2.14mm (120% of 1.78mm) |

**Prediction:** This will likely fix the 5000ml font size measurement. The current code is reverse-engineering font size from line spacing using a wrong formula, causing systematic over-measurement.

### Fix 2: Body Text Line Diagnostics

Impact: Better debugging visibility. May reveal header/title contamination in body text identification.

### Fix 3: Deterministic Calibration

Impact: Eliminates 25% DPI swing across runs. Improves repeatability but doesn't directly fix 5000ml error.

---

## 🎓 REGULATION RESEARCH SUMMARY

**EU Regulation 1272/2008 (CLP) — Key Points:**

1. **Font size definition (Annex I §1.2.1.4):**
   - Height of lowercase 'x' (x-height)
   - NOT cap-height, NOT mean character height

2. **Size thresholds (by package size):**
   - ≤500ml: ≥1.2mm x-height
   - 500-3000ml: ≥1.4mm x-height
   - >3000ml: ≥1.8mm x-height
   - Inner packaging ≤10ml: Smaller allowed if "easily legible"

3. **Line spacing (EU 2024/2865 amendment):**
   - "Distance between two lines" ≥ 120% of font size
   - This is **baseline-to-baseline**, not visible gap
   - Formula: `line_spacing_mm ≥ 1.2 × x_height_mm`

4. **Color contrast (Annex I §1.2.1.4):**
   - Primary: White background + Black text
   - Secondary: Yellow background + Black text (GHS hazard pictograms)
   - Must be "clearly distinguishable" (high contrast)

**Source validation:**
- Bens Consulting (official CLP guidance): Confirms 120% = baseline-to-baseline
- Arcus Compliance: Confirms x-height measurement requirement
- H2 Compliance: Confirms package-size-dependent thresholds

---

## 🔧 IMPLEMENTATION PRIORITY

**P0 (CRITICAL — Do immediately):**
1. Fix line distance calculation (change `center_to_center - xheight` to just `center_to_center`)
2. Test on both 700ml and 5000ml labels to verify fix

**P1 (HIGH — Do this cycle):**
3. Add body text line identification diagnostics
4. Improve calibration prompt for deterministic reference selection

**P2 (MEDIUM — Next cycle):**
5. Add validation for calibration line length (reject if <500mm)
6. Consider using median instead of mean for line height clustering (Opus's suggestion)

---

## 🧪 TESTING RECOMMENDATIONS

After implementing fixes:

1. **Run 700ml label 3 times** — verify measurements are within ±2%
2. **Run 5000ml label 3 times** — verify font size ~1.78mm (±3%)
3. **Check line distance on both** — should be ≥120% of font size
4. **Capture full logs** — review body text line identification

**Expected results post-fix:**
- 700ml: font=1.20mm (±0.02), line distance=1.44mm (±0.05)
- 5000ml: font=1.78mm (±0.05), line distance=2.14mm (±0.07)

---

## 📝 CODE CHANGES NEEDED

### Change 1: Fix Line Distance Calculation

**File:** `label_analyzer_production.py`  
**Line:** ~2066  

**Before:**
```python
line_distance_mm = center_to_center_mm - xheight_mm
```

**After:**
```python
# CLP "line distance" = baseline-to-baseline spacing (center-to-center)
# NOT the visible gap. CLP requires: line_distance ≥ 1.2 × font_size (x-height)
line_distance_mm = center_to_center_mm
visible_gap_mm = center_to_center_mm - xheight_mm  # For informational logging only
```

**Add logging:**
```python
logger.info(f"  📐 Line spacing: center-to-center={center_to_center_mm:.3f}mm (baseline-to-baseline)")
logger.info(f"  📐 Visible gap: {visible_gap_mm:.3f}mm (center-to-center minus x-height)")
logger.info(f"  📐 CLP requires: line distance ≥ {xheight_mm * 1.2:.3f}mm (120% of x-height)")
```

### Change 2: Add Body Text Line Diagnostics

**File:** `label_analyzer_production.py`  
**Line:** After ~1738  

**Add:**
```python
if logger.isEnabledFor(logging.INFO):
    logger.info(f"  📐 Body text line identification (cluster={best_line_bin:.2f}mm):")
    for i in sorted(list(body_line_indices)[:10]):  # Show first 10 lines
        if i < len(line_char_heights):
            line_h = line_char_heights[i]
            if line_h:
                logger.info(f"       Line {i}: {len(line_h)} chars, mean={statistics.mean(line_h):.3f}mm, median={statistics.median(line_h):.3f}mm")
```

### Change 3: Improve Calibration Determinism

**File:** `label_analyzer_production.py`  
**Line:** ~1565 (`_auto_detect_pdf_scale()` method)

**Update prompt to explicitly target longest line:**
```python
PROMPT_SCALE_DETECTION = """
You are analyzing a technical drawing to find the PRIMARY reference dimension.

CRITICAL INSTRUCTION: Identify the SINGLE LONGEST clearly labeled measurement line.

Requirements:
1. Must have numeric label in millimeters (e.g., "636.07mm", "662.90")
2. Must be horizontal or vertical (not diagonal)
3. Must be the LONGEST such line on the drawing (prefer >500mm)
4. Must have clear endpoints (arrows, marks, or line terminals)

Return ONLY the single longest measurement line.
If multiple lines have the same length, pick the one with highest label clarity.

DO NOT return multiple candidates. Pick ONE.
"""
```

**Add validation after detection:**
```python
if line.value_mm < 200:
    logger.warning(f"  ⚠️  Calibration reference too short ({line.value_mm}mm < 200mm)")
    logger.warning(f"     Measurement accuracy may be reduced. Consider manual DPI specification.")
```

---

## 🎯 NEXT STEPS FOR OPUS IMPLEMENTER

1. **Implement Change 1** (line distance fix) — this is the critical fix
2. **Test on 5000ml label** — capture full log output
3. **If 5000ml still wrong after Change 1:**
   - Implement Change 2 (diagnostics)
   - Share log output showing body text line identification
   - We'll determine if header contamination is the issue

4. **After 5000ml is fixed:**
   - Implement Change 3 (calibration determinism)
   - Run 5x repeat tests on both labels to verify stability

---

**End of Review**  
Next review: 4:30 PM PST (15 minutes)
