# Sonnet Code Review — Label Analyzer
**Cycle:** Tuesday, February 17th, 2026 — 3:45 PM PST  
**Git HEAD:** f7fbb4d  
**Reviewer:** Sonnet (Lead Code Reviewer)

---

## 🎯 EXECUTIVE SUMMARY

**Current Status:**
- ✅ 700ml label: ACCURATE (font=1.20mm vs expected 1.19mm)
- ❌ 5000ml label: BROKEN (font=1.91mm vs expected 1.78mm, +7.3% over)

**Root Cause Identified:**
The glyph-based x-height measurement (commit f7fbb4d) is theoretically correct but may have **implementation edge cases** causing the 5000ml over-measurement. The 700ml accuracy proves the approach works — we need to debug why 5000ml diverges.

**Key Insight from Opus's Notes:**
✅ Opus is CORRECT: The gap formula `gap = c2c - x_height` is right for CLP. Using cap-height would make things worse. The 5000ml gap error (1.92mm vs 2.01mm) is a **downstream symptom** of the font over-measurement, not a formula problem.

---

## 📚 REGULATION COMPLIANCE RESEARCH

### EU CLP Regulation 1272/2008 (Amendment 2024/2865)

**Font Size Requirement:**
- **Metric:** X-height (lowercase 'x' height)
- **Source:** Arcus Compliance: "Minimum height of 1.2 mm (a minimum height of 1.2 mm of the lower case 'x' [x-height] of the chosen font)"
- **Thresholds:**
  - ≤500ml: 1.2mm x-height
  - 500-3000ml: 1.4mm x-height  
  - >3000ml: 1.8mm x-height
- **Exception:** Inner packaging ≤10ml can be smaller if "easily legible"

**Line Spacing Requirement:**
- **Metric:** Distance between lines ≥ 120% of font size (x-height)
- **Source:** Multiple sources confirm: "the distance between two lines must be at least 120% of the font size"
- **Example:** 1.2mm x-height → 1.44mm minimum gap
- **Interpretation:** The gap is measured from descender bottom (line N) to ascender/cap top (line N+1)

**✅ CODE COMPLIANCE:** The analyzer correctly targets x-height and uses 120% rule.

---

## 🔬 CODE ANALYSIS — FONT MEASUREMENT APPROACH

### Implementation Hierarchy (Priority Order)

1. **Glyph-Based (f7fbb4d)** — Lines 2017-2070
   - Uses `Font.glyph_bbox(ord('x'))` for mathematically exact x-height
   - Requires: `doc.extract_font(xref)` → `fitz.Font(fontbuffer=...)`
   - **CRITICAL CHECK:** Only prefers glyph-based if origin-based is >5% higher
   - **Risk:** Fails gracefully on subset fonts, CIDFonts, Type3 fonts

2. **Text-Based Origin Measurement** — Lines 1922-2013
   - Uses `get_text("rawdict")` to get per-char bboxes with character identity
   - Measures from bbox top to baseline (`origin_y`) for x-height chars ('aceimnorsuvwxz')
   - **Advantage:** Avoids bbox padding by using baseline as reference
   - **Minimum threshold:** ≥3 x-height chars (lowered from 5 in commit 158c84f)

3. **Bimodal Height Clustering (Fallback)** — Lines 2077-2167
   - Histogram peak detection on vector path heights
   - Identifies short peak (x-height) vs tall peak (cap-height)
   - **Risk:** Less precise, heuristic-based

### 🐛 SUSPECTED BUG — 5000ml Over-Measurement

**Hypothesis:** The glyph-based measurement may not be engaging for 5000ml, leaving origin-based measurement active with residual bbox padding.

**Debug Questions:**
1. **Does glyph extraction succeed for 5000ml?**
   - Check logs for: "GLYPH-BASED x-height: [value]mm"
   - If missing → font extraction failed, fallback to origin-based

2. **Is the 5% threshold too high?**
   - 5% of 1.78mm = 0.089mm tolerance
   - If origin-based measures 1.87mm (5.05% over), glyph-based won't override
   - **Recommendation:** Lower threshold to 3% or always prefer glyph-based when available

3. **Is the baseline detection accurate for 5000ml?**
   - Origin-based relies on `span.get('origin')[1]` for baseline
   - If origin is misreported → baseline is wrong → x-height is wrong
   - **Check:** Does 5000ml use a non-standard font where origin != baseline?

---

## 🔧 RECOMMENDED FIXES (PRIORITY ORDER)

### 1. **IMMEDIATE: Add Diagnostic Logging**

**File:** `label_analyzer_production.py`  
**Function:** `measure_font_from_pdf_vectors()` (lines 1850-2200)

**Add detailed logging BEFORE line 2070 (after glyph-based measurement attempt):**

```python
# After line 2070 — log why glyph-based was or wasn't used
if text_xheight_mm is not None:
    logger.info(f"  🔍 DECISION: text_xheight (origin)={text_xheight_mm:.3f}mm")
    if glyph_xheight_mm is not None:
        diff_pct = abs(text_xheight_mm - glyph_xheight_mm) / glyph_xheight_mm * 100
        logger.info(f"  🔍 DECISION: glyph_xheight={glyph_xheight_mm:.3f}mm, diff={diff_pct:.1f}%")
        if text_xheight_mm > glyph_xheight_mm * 1.05:
            logger.info(f"  🔍 DECISION: Using glyph (origin is {diff_pct:.1f}% too high)")
        else:
            logger.info(f"  🔍 DECISION: Using origin (within 5% of glyph)")
    else:
        logger.info(f"  🔍 DECISION: No glyph data, using origin")
```

**Purpose:** This will reveal if glyph-based is engaging for 5000ml or failing silently.

### 2. **CRITICAL FIX: Lower Glyph Preference Threshold**

**Current:** Prefers glyph-based only if origin-based is >5% higher  
**Problem:** 5% might be too generous for bbox padding at large sizes

**Change line 2063:**
```python
# OLD:
if text_xheight_mm > glyph_xheight_mm * 1.05:

# NEW (OPTION 1 — always prefer glyph when available):
if glyph_xheight_mm is not None:
    logger.info(f"  📐 ⚡ Preferring GLYPH-BASED x-height (mathematically exact)")
    text_xheight_mm = glyph_xheight_mm
    if cap_glyph_bbox and cap_glyph_bbox.height > 0:
        text_capheight_mm = glyph_capheight_mm

# NEW (OPTION 2 — lower threshold to 3%):
if text_xheight_mm > glyph_xheight_mm * 1.03:
```

**Rationale:** Glyph bbox is the gold standard — it's the font designer's intended metrics. Origin-based is an approximation that tries to work around bbox padding. If we have the exact glyph data, we should use it.

**Risk Assessment:**
- **Option 1 (always prefer):** Safe — glyph data is authoritative
- **Option 2 (3% threshold):** Conservative middle ground

**Recommendation:** Start with Option 1, test on both 700ml and 5000ml. If 700ml regresses, fall back to Option 2.

### 3. **CODE QUALITY: Add Cross-Validation Alerting**

**File:** `label_analyzer_production.py`  
**After line 2170 (cross-validation logging):**

```python
# After line 2170 — escalate disagreement to ERROR level
if text_xheight_mm is not None and len(peaks) >= 1:
    vector_xheight = sorted([p[0] for p in peaks[:2]])[0] if len(peaks) >= 2 else peaks[0][0]
    disagreement_pct = abs(text_xheight_mm - vector_xheight) / text_xheight_mm if text_xheight_mm > 0 else 0
    if disagreement_pct > 0.15:
        logger.error(f"  ❌ CROSS-VALIDATION FAILED: text-based ({text_xheight_mm:.3f}mm) vs vector ({vector_xheight:.3f}mm) disagree by {disagreement_pct:.0%}")
        logger.error(f"     This suggests measurement instability — recommend human review")
    elif disagreement_pct > 0.05:
        logger.warning(f"  ⚠️  Cross-validation: text ({text_xheight_mm:.3f}mm) vs vector ({vector_xheight:.3f}mm) differ by {disagreement_pct:.0%}")
    else:
        logger.info(f"  ✓ Cross-validation: text ({text_xheight_mm:.3f}mm) vs vector ({vector_xheight:.3f}mm) agree within {disagreement_pct:.0%}")
```

**Purpose:** Catch measurement instability early and flag for human review.

### 4. **OPTIONAL: Add Glyph Extraction Failure Alerting**

**File:** `label_analyzer_production.py`  
**In the glyph extraction try/except block (around line 2050):**

```python
except Exception as glyph_err:
    logger.warning(f"  ⚠️  Glyph-based measurement failed for '{font_name}': {glyph_err}")
    logger.warning(f"     Font type: {font_data[2] if font_data and len(font_data) >= 3 else 'unknown'}")
    logger.warning(f"     Falling back to origin-based measurement (less accurate)")
```

**Purpose:** Make glyph extraction failures visible, not silent.

---

## 🎯 REGRESSION RISK ASSESSMENT

### Safe Changes (Low Risk):
1. ✅ **Diagnostic logging** (Fix #1) — Zero runtime impact, pure observability
2. ✅ **Cross-validation alerting** (Fix #3) — Only adds logging, no logic change
3. ✅ **Glyph failure alerting** (Fix #4) — Only adds logging

### Risky Changes (Needs Testing):
4. ⚠️ **Lower glyph preference threshold** (Fix #2) — Could affect 700ml if not careful
   - **Test Plan:** Run on BOTH 700ml and 5000ml after change
   - **Expected:** 5000ml improves (1.91→1.78mm), 700ml stays stable (1.20mm)
   - **Rollback:** If 700ml regresses, revert to 5% threshold or add size-dependent logic

---

## 📋 TESTING CHECKLIST FOR OPUS

After implementing fixes, verify:

- [ ] **5000ml:** Font size drops from 1.91mm toward 1.78mm (within ±0.03mm)
- [ ] **5000ml:** Gap measurement improves as a side effect (toward 2.01mm)
- [ ] **700ml:** Font size remains stable at ~1.20mm (±0.02mm acceptable)
- [ ] **700ml:** Gap remains stable at ~0.92mm
- [ ] **Logs show:** "GLYPH-BASED x-height" for both labels (confirms extraction works)
- [ ] **Logs show:** Cross-validation passes (text vs vector agree within 5%)

---

## 🚫 DO NOT IMPLEMENT (Per Opus's Notes)

❌ **Gap Formula Change:** Do NOT change `gap = c2c - x_height` to `gap = c2c - cap_height`
- **Reason:** CLP requires gap ≥ 120% of x-height, not cap-height
- **Math Proof:** Opus showed this would make 5000ml gap WORSE (1.10mm instead of 2.05mm)
- **Verdict:** Current formula is correct; gap error is downstream of font error

---

## 🔍 EDGE CASES TO WATCH

1. **Subset Fonts:** Some PDFs embed font subsets where glyph extraction fails
   - **Current behavior:** Graceful fallback to origin-based
   - **Risk:** None, already handled

2. **CIDFonts / Type3 Fonts:** `glyph_bbox()` may not work
   - **Current behavior:** Exception caught, fallback to origin-based
   - **Risk:** None, already handled

3. **All-Caps Text:** No lowercase chars for x-height measurement
   - **Current behavior:** Estimates x-height from cap-height via 0.70 ratio
   - **Risk:** Less accurate, but annotated in logs

4. **Very Small Fonts (<0.8mm):** Below detection threshold
   - **Current behavior:** Filtered out by `MIN_BODY_TEXT_HEIGHT = 0.5mm`
   - **Risk:** Might miss legitimate small CLP text (rare)

---

## 📊 CODE QUALITY OBSERVATIONS

### Strengths:
1. ✅ **Robust fallback hierarchy** (glyph → origin → clustering)
2. ✅ **Deterministic PDF vector measurement** (no Gemini for font size)
3. ✅ **Good error handling** (graceful degradation on failures)
4. ✅ **Comprehensive logging** (easy to debug)
5. ✅ **Regulation-aligned** (x-height, 120% rule, package-size thresholds)

### Weaknesses:
1. ⚠️ **Silent glyph extraction failures** — Need more visible warnings
2. ⚠️ **5% threshold may be too generous** — Should prefer glyph more aggressively
3. ⚠️ **Cross-validation logging only** — Should escalate disagreements to ERROR level
4. ⚠️ **No measurement stability metrics** — Could track variance across runs

---

## 🎬 NEXT ACTIONS FOR OPUS

1. **Implement Fix #1** (diagnostic logging) — 5 minutes, zero risk
2. **Run 5000ml label** — capture new logs showing glyph decision path
3. **Analyze logs** — confirm if glyph-based is engaging or failing
4. **Implement Fix #2** (Option 1: always prefer glyph) — 2 minutes, moderate risk
5. **Run BOTH 700ml and 5000ml** — verify no regression on 700ml
6. **If 700ml regresses** → revert to Fix #2 Option 2 (3% threshold)
7. **If both pass** → commit with message: "fix: always prefer glyph-based x-height when available"
8. **Implement Fix #3 and #4** (alerting) — 10 minutes, zero risk

**Expected Outcome:** 5000ml font measurement drops to 1.78mm ± 0.03mm, gap improves to ~2.05mm as a side effect.

---

## 💡 FUTURE ENHANCEMENTS (Post-Fix)

1. **Measurement Stability Tracking:**
   - Run each measurement 3 times, report variance
   - Flag if variance > 5% (measurement instability)

2. **Font Hinting Compensation:**
   - Some fonts use aggressive hinting that distorts glyph_bbox
   - Could compare glyph_bbox vs actual rendered size and add correction factor

3. **Multi-Font Detection:**
   - Body text might use multiple fonts (italic, bold, etc.)
   - Could group by font family and measure each separately

4. **Gap Measurement Refinement:**
   - Currently uses center-to-center minus x-height
   - Could measure actual descender depth and ascender height per line for precision

---

**Review Complete. Ready for Opus implementation.**

— Sonnet (Lead Code Reviewer)  
Tuesday, February 17th, 2026 — 3:45 PM PST
