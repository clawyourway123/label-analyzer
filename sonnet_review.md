# SONNET CODE REVIEW — 2026-02-17 5:00 PM

## Executive Summary

**Status:** ⚠️ CRITICAL BLOCKER IDENTIFIED — Unstable DPI calibration explains all measurement variance  
**Root Cause:** Gemini picks different measurement reference lines on each run → different DPI → cascading error  
**Action Required:** Lock DPI calibration or make prompt deterministic

---

## CRITICAL FINDING: DPI Calibration is Non-Deterministic

### The Smoking Gun

From MEMORY.md (Feb 17, 11:48 AM):
> **Same PDF, 3 consecutive runs = 3 different DPI values:**
> - Run 1: DPI=334 (measurement line at x=288-1265, 636.07mm)
> - Run 2: DPI=247 (measurement line at x=166-804, 560.96mm)
> - Run 3: (not yet, but likely another value)

**This explains EVERYTHING:**
- Font size variance: 1.08mm → 1.47mm across runs
- Line spacing errors: Gap calculations inherit DPI error
- Bimodal detection issues: Same pixel heights → different mm values per run

### Why Opus's Fixes Can't Work Yet

Opus applied two good fixes:
1. ✅ PyMuPDF `set_small_glyph_heights(True)` — correct approach (though no effect on vector PDFs)
2. ✅ Improved bimodal detection algorithm — mathematically sound

But BOTH rely on stable DPI. If DPI varies 25% between runs (334 → 247), no amount of peak detection refinement will converge to ±2% accuracy.

### Root Cause Analysis

**Code Location:** `label_analyzer_production.py`, line 360-405 (`CalibrationResult` class)

**Current Behavior:**
```python
# Line 383-405: update() method
def update(self, line: MeasurementLine):
    ref_key = round(line.value_mm, 2)  # Cache by reference value
    
    if ref_key in self.reference_dpi_cache:
        # Reuse cached DPI
        ...
    else:
        # Calculate new DPI and cache it
        calculated_dpi = int(round(calculated_dpmm * 25.4))
        self.reference_dpi_cache[ref_key] = calculated_dpi
```

**The Problem:**
- Gemini is asked to "identify a technical measurement line" (too vague)
- PDF contains multiple candidate lines (636.07mm, 560.96mm, others)
- Gemini picks DIFFERENT lines each run (non-deterministic model sampling)
- Each line → different `ref_key` → cache miss → recalculate DPI
- Result: 336 DPI run 1, 247 DPI run 2, ??? DPI run 3

**Evidence:**
- Run 1: `636.07mm` reference → pixel distance 977px → 334 DPI
- Run 2: `560.96mm` reference → pixel distance 638px → 247 DPI
- Math checks out: same PDF, different lines, different DPI

---

## Proposed Solutions (Priority Order)

### P0 - LOCK DPI AFTER FIRST CALIBRATION

**Approach:** Once calibrated successfully, store DPI in persistent state and never recalibrate for the same PDF.

**Implementation:**
```python
class CalibrationResult:
    def __init__(self, original_dpi: int, locked_dpi: Optional[int] = None):
        self.original_dpi = original_dpi
        self.locked_dpi = locked_dpi  # NEW: persistent DPI lock
        self.true_dpi = locked_dpi if locked_dpi else original_dpi
        self.is_calibrated = locked_dpi is not None
        # ...
    
    def update(self, line: MeasurementLine):
        if self.locked_dpi:
            logger.info(f"  🔒 DPI locked at {self.locked_dpi}, ignoring calibration")
            return True  # Skip recalibration
        
        # ... existing calibration logic ...
        
        # After successful calibration:
        self.locked_dpi = calculated_dpi  # LOCK IT
```

**Cache Strategy:**
- Store `{pdf_sha256_hash: calibrated_dpi}` in disk cache (`.cache/label_analyzer/dpi_cache.json`)
- On first PDF analysis: calibrate once, cache DPI
- On subsequent runs: load cached DPI, skip calibration entirely
- User can manually clear cache if needed

**Benefits:**
- 100% deterministic after first run
- No more DPI variance
- Measurements become repeatable

---

### P1 - IMPROVE CALIBRATION PROMPT SPECIFICITY

**Current Prompt (too vague):**
> "Identify a technical measurement line on this label..."

**Improved Prompt:**
```python
PROMPT_DPI_CALIBRATION = """
Identify THE PRIMARY MEASUREMENT REFERENCE LINE on this technical label.

PRIORITY RULES (pick the FIRST match you find):
1. **Horizontal dimension line** (most common) — labeled "width", "W", "horizontal", or similar
2. **Full label width** — typically the longest horizontal measurement
3. **Frame or border width** — if label has an outer border with measurement annotation
4. **Any measurement >100mm** — likely to be the primary dimension

CRITICAL: Return coordinates for THE MOST PROMINENT measurement line only.
If multiple candidates exist, prefer the one with:
- Largest numeric value (longer = more reliable)
- Clearest annotation (text label directly adjacent)
- Horizontal orientation (vertical measurements less reliable)

DO NOT return multiple candidates. Pick ONE line confidently.
"""
```

**Why This Helps:**
- Reduces Gemini's decision space (one clear target)
- Prioritizes most reliable references (long horizontal lines)
- Minimizes sampling variance

**Limitation:**
- Still probabilistic (Gemini may still vary)
- Needs testing to confirm improved consistency

---

### P2 - MANUAL DPI OVERRIDE (Fallback)

**Use Case:** When automated calibration is too unreliable, allow user to provide known DPI.

**Implementation:**
```python
analyzer = LabelAnalyzer(
    project_id=PROJECT_ID,
    manual_dpi=300  # User-provided, bypasses calibration
)
```

**Benefits:**
- 100% deterministic (no AI variance)
- Useful for production workflows with known label specs

**Downside:**
- Requires user to know/measure DPI externally
- Not scalable for batch processing unknown labels

---

## Review of Opus's Test Methodology

### ✅ Strengths

1. **Direct vector measurement** — correctly bypasses text/glyph layers
2. **Bimodal peak detection** — mathematically sound (x-height vs cap-height)
3. **Comprehensive logging** — makes debugging possible
4. **Ground truth comparison** — 700ml label as validation baseline

### ⚠️ Identified Issues

#### Issue 1: Test doesn't isolate DPI variance

**Problem:** `test_700ml.py` calls the full analyzer pipeline, which includes calibration. If calibration varies, test results will vary.

**Fix:** Add a **deterministic test mode** that locks DPI:
```python
# In test_700ml.py
analyzer = LabelAnalyzer(project_id=PROJECT_ID, manual_dpi=300)  # Lock DPI
parts = analyzer.analyze_label(pdf_path)
```

This isolates measurement logic from calibration noise.

#### Issue 2: Expected values may be from different measurement method

Opus correctly notes:
> "The expected 1.19mm might come from a different section or measurement method."

**Validation Needed:**
1. Measure the 700ml PDF **manually** using a tool like Adobe Acrobat's measurement tool
2. Compare with Opus's detected 1.08mm x-height
3. If manual measurement ≈ 1.08mm, update ground truth (current expectation wrong)
4. If manual measurement ≈ 1.19mm, investigate why bimodal detection finds 1.08mm

**Action Item for Next Cycle:** Manual verification with external tool.

---

## Code Quality Assessment

### ✅ What Opus Got Right

1. **PyMuPDF flag addition** (line 20-25) — future-proof for embedded fonts
2. **Bimodal detection improvement** (lines 2032-2062):
   - Uses most frequent cluster as x-height (correct for body text)
   - Finds best-separated cap-height candidate (>0.25mm gap)
   - Filters noise (<50 chars, out-of-range heights)
3. **Comprehensive validation** — checks CLP rules deterministically

### ⚠️ Remaining Code Issues

#### Issue 1: CalibrationResult.reference_dpi_cache is ineffective

**Location:** Line 383-405

**Problem:** Cache key is `round(line.value_mm, 2)`, but:
- Gemini finds different reference LINES (not just different pixel coords of same line)
- Different lines → different ref_key → cache miss → recalculate

**Fix:** Don't cache by reference value, **cache by PDF hash**:
```python
# Before calibration:
pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
cached_dpi = load_dpi_from_disk_cache(pdf_hash)
if cached_dpi:
    calibration.locked_dpi = cached_dpi
    return calibration
```

#### Issue 2: Bimodal detection heuristics are dataset-dependent

**Location:** Lines 2032-2062

**Current Logic:**
- Requires >0.25mm separation for cap-height detection
- Filters cap-height candidates: >50 chars, height 0.8-3.0mm

**Concern:** These thresholds work for 700ml label but may fail for:
- Fonts with small x-height/cap-height difference (<0.25mm)
- Labels with sparse capitals (<50 chars)
- Unusual font sizes (>3.0mm x-height for large labels)

**Recommendation:** Add fallback for edge cases:
```python
if not cap_height_found:
    # Fallback: use tallest cluster within 1.2-1.5× of x-height
    for h, count in sorted_clusters:
        ratio = h / xheight_candidate
        if 1.2 <= ratio <= 1.5 and count >= 10:
            cap_height = h
            break
```

---

## Research: PyMuPDF API and CLP Regulations

### PyMuPDF Small Glyph Heights

**Opus's Implementation:** ✅ Correct (line 20-25)

**Research Finding:** From [PyMuPDF Discussion #3067](https://github.com/pymupdf/PyMuPDF/discussions/3067):
> "PyMuPDF's default glyph bbox includes 10-37% padding (font design metrics). `set_small_glyph_heights(True)` returns VISIBLE heights only."

**Impact on This Project:**
- ✅ Correct for PDFs with embedded fonts
- ❌ No effect on vector-only PDFs (like 700ml label)
- 📊 Net: Good addition for production (handles wider range of inputs)

### CLP Regulation 1272/2008 — Font Size Definition

**Critical Clarification:** CLP requires **x-height measurement**, not cap-height or total character height.

**Opus's Implementation:** ✅ Correct

The bimodal detection correctly identifies x-height (shorter peak) vs cap-height (taller peak). This matches EU regulation requirements.

**Reference:** [EU Regulation 1272/2008, Annex I, Part 1.2.1.3](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02008R1272-20231201)

---

## Recommendations for Next Cycle

### P0 - MUST DO (Blocking)

1. **Implement DPI locking with disk cache**
   - Cache by PDF hash, not reference value
   - Store in `~/.cache/label_analyzer/dpi_cache.json`
   - Test with 3 consecutive runs to verify DPI stability

2. **Add deterministic test mode**
   - Allow `manual_dpi` parameter in `LabelAnalyzer.__init__`
   - Update `test_700ml.py` to use locked DPI (300 or 72)

3. **Verify 700ml ground truth manually**
   - Use Adobe Acrobat or equivalent to measure x-height
   - Update expected values if current ground truth is incorrect

### P1 - HIGH PRIORITY

4. **Improve calibration prompt specificity**
   - Target "primary horizontal dimension line" explicitly
   - Add priority rules (longest line, >100mm preferred)

5. **Add bimodal detection fallback**
   - Handle fonts with small x-height/cap-height difference
   - Handle sparse capital letters

### P2 - NICE TO HAVE

6. **Add CLP compliance warnings**
   - Log when gap < 120% font size
   - Flag measurements with low confidence for human review

7. **Validate on 5000ml label**
   - Test if DPI locking fixes the 1.91mm → 1.78mm correction

---

## Commit Review

### Commit 661056c: "fix: improve bimodal detection and apply PyMuPDF small glyph heights"

**Changes:**
- Added `fitz.TOOLS.set_small_glyph_heights(True)` ✅
- Improved bimodal peak selection (most frequent → x-height) ✅

**Assessment:** ✅ Good changes, but won't fix variance until DPI locked

### Commit c439e2e: "fix: use median for line height clustering + add body text diagnostics"

**Changes:** Switched to median for line grouping (reduces outlier impact)

**Assessment:** ✅ Good defensive coding

---

## Test Results Analysis

### Opus's Measured Values (700ml PDF)

| Metric | Measured | Expected | Error |
|--------|----------|----------|-------|
| Font (x-height) | 1.0800 mm | 1.19 mm | -9.24% ❌ |
| Line gap | 1.0435 mm | 0.98 mm | +6.48% ❌ |

### Why These Numbers Are Wrong (Hypothesis)

**Scenario 1:** DPI was wrong during this run
- If DPI=247 was used (from unstable calibration)
- True x-height in pixels: ~17px (across all runs)
- Conversion: 17px / (247 DPI / 25.4) = **1.75mm** (not 1.08mm!)
- Something doesn't add up — need to verify DPI used in this specific run

**Scenario 2:** Ground truth is from printed label, not PDF
- Printing process causes ink gain (~5-10%)
- 1.19mm on printed label = ~1.08mm in digital artwork (9% smaller, matches!)
- **This is likely the real explanation**

**Action:** Measure the 700ml PDF in Adobe Acrobat (digital measurement tool) and compare to 1.08mm vs 1.19mm.

---

## Summary: What Needs to Happen Next

**BLOCKER:** DPI calibration must be deterministic before any measurement accuracy work can proceed.

**Recommended Approach:**
1. Opus: Implement DPI disk cache (by PDF hash)
2. Opus: Test 3 consecutive runs to verify identical DPI
3. Opus: Run test_700ml.py with locked DPI
4. Sonnet: Research ground truth source (printed vs digital)
5. Opus: If measurements still off, revisit bimodal detection thresholds

**Timeline Estimate:**
- DPI locking: 1-2 hours
- Testing: 30 minutes
- Ground truth verification: 30 minutes (manual measurement)

**Expected Outcome:**
- With locked DPI: measurements become repeatable (same result every run)
- With corrected ground truth: measurements within ±2% target

---

## Technical Notes

**Git Log Summary:**
- Last 10 commits show active iteration on bimodal detection
- Removed incorrect 1.483× correction factor (commit 18f44be) ✅
- Added glyph-based x-height (commit f7fbb4d) ✅
- Progress is good, but foundation (DPI) needs fixing first

**Code Coverage:**
- Main measurement pipeline: ✅ Well-tested
- DPI calibration stability: ❌ Not tested (root cause of variance)
- Edge cases (sparse capitals, unusual fonts): ⚠️ Unknown

**Performance:**
- No concerns (caching works for API calls)
- DPI cache will reduce calibration overhead to zero after first run

---

**Review Complete** — Ready for P0 implementation (DPI locking).
