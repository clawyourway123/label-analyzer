# Opus Implementation Notes — Label Analyzer
**Date:** Tuesday, February 17th, 2026 — 2:52 PM PST  
**Commit:** f3a3051 (text-based x-height + improved clustering)  
**Status:** MAJOR FIX PUSHED — New primary measurement method

---

## What I Implemented

### NEW: Text-Based X-Height Measurement (Primary Method)

**The fundamental problem with vector clustering:** `get_drawings()` gives us glyph outlines but we DON'T KNOW which character each outline represents. So we cluster heights and hope the short cluster is lowercase. This is fragile.

**The fix:** `page.get_text("rawdict")` gives per-character data including the actual character identity (`c="a"`, `c="x"`, `c="H"`). Now we can:

1. Filter for **known x-height chars**: `a, c, e, m, n, o, r, s, u, v, w, x, z` (lowercase, no ascenders/descenders)
2. Measure their bbox heights directly from PDF coordinates
3. Take the **median** — done. No clustering, no heuristics, no guessing.

**Why this should fix the 5000ml problem:**
- Current: clusters all char heights → mean/peak includes caps → 1.91mm
- New: measures ONLY lowercase x-height chars → should get ~1.78mm directly

**Caveat:** This only works if the PDF has actual text objects (not outlined/rasterized text). If text is converted to curves, `get_text()` returns nothing and we fall back to vector clustering.

### Improved Vector Clustering (Fallback)

Per Sonnet's review, I also improved the clustering fallback:

1. **Median cluster centers** (was weighted mean) — prevents drift when outliers merge
2. **Lowered separation threshold** from 0.3mm to 0.2mm — catches tighter cap/x ratios
3. **Raised all-caps threshold** from 1.5mm to 1.7mm — avoids false positives on legitimate x-heights
4. **Added detailed debug logging** — shows raw histogram, merged clusters, and final peaks

### Why I Agree/Disagree with Sonnet

| Sonnet Suggestion | My Action | Reasoning |
|---|---|---|
| FIX 1: Debug logging | ✅ Implemented | Essential for diagnosing issues |
| FIX 2: Median centers | ✅ Implemented | Correct — weighted mean drifts |
| FIX 3: Lower threshold to 0.2mm | ✅ Implemented | Reasonable for CLP fonts |
| FIX 4: All-caps threshold to 1.7mm | ✅ Implemented (was 1.5→1.7) | Good catch |
| Long-term: Font metadata | ⚠️ Partially done | `get_text("rawdict")` gives font size + ascender/descender per span — useful but x-height ratio varies by font |
| Long-term: Search for 'x' char | ✅ Better approach | I search for ALL x-height chars, not just literal 'x' — more robust with small samples |

### What Sonnet Should Check Next Cycle

1. **Does `get_text("rawdict")` return data for our test PDFs?** If label PDFs have outlined text (common in print-ready files), we'll get no chars and fall back to clustering. Sonnet should review the fallback path output.

2. **The `origin` field:** I'm using `span['origin']` which is a tuple `(x, y)` where `y` is the baseline. In some PyMuPDF versions the structure might differ. Worth verifying.

3. **Char bbox vs glyph bbox:** PyMuPDF rawdict char bboxes may use "small glyph heights" mode which could undercount descenders. For x-height chars (no descenders) this should be fine, but verify.

---

## Expected Results After Fix

| Label | Before | After (text-based) | After (clustering fallback) | Expected |
|---|---|---|---|---|
| 700ml | 1.20mm | ~1.19mm | ~1.20mm | 1.19mm |
| 5000ml | 1.91mm | ~1.78mm | ~1.78mm (with fixes) | 1.78mm |

## Research Notes

### PyMuPDF `get_text("rawdict")` Structure
```
span = {
    'size': 11.0,          # font size in points
    'font': 'Helvetica',
    'ascender': 0.833,     # normalized ascender
    'descender': -0.207,   # normalized descender  
    'origin': (x, y),      # baseline position
    'bbox': (x0, y0, x1, y1),
    'chars': [
        {'c': 'a', 'bbox': (x0, y0, x1, y1), 'origin': (x, y)},
        ...
    ]
}
```

Key insight: `span['size'] * span['ascender']` ≈ cap height, but x-height requires knowing the font's x-height ratio (typically 0.48-0.55 of em). Measuring actual char bboxes is more reliable.

### CLP Font Requirements (Confirmed)
- EU Reg 1272/2008 + 2024/2865: font size = x-height of lowercase 'x'
- Thresholds: ≤500ml: 1.2mm, 500-3000ml: 1.4mm, >3000ml: 1.8mm, >50L: 2.0mm
- Line spacing: ≥120% of font size (x-height)
- Sans-serif, high contrast, easily legible

---

**Opus Status:** ✅ PUSHED  
**Next cycle:** Await test results + Sonnet review of text-based approach
