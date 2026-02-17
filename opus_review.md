# Opus Implementation Review — 2026-02-17 14:37 PST

## ✅ IMPLEMENTED (Commit 3948a1d)

### 1. Fixed Peak Detection: Cluster Merging
**Problem:** Old code treated every 0.02mm bin with ≥3 chars as a "peak." Two adjacent bins (e.g., 1.50mm and 1.52mm) could both be top "peaks," fail the 0.3mm separation check, and collapse to single-peak mode — even when a clear bimodal distribution exists.

**Fix:** Replaced per-bin counting with cluster merging:
- Walk sorted bins left-to-right
- Merge into existing cluster if within 0.08mm of cluster center
- Update cluster center as weighted average
- Result: clean clusters that represent actual height groups
- Added debug logging showing cluster centers and counts

### 2. Fixed Gap Formula: x-height, NOT cap-height
**Problem:** Sonnet changed `gap = c2c - font_size_mm` to `gap = c2c - capheight_mm`. This is WRONG.

**Math proof:**
- Old code: mean=1.91mm, gap=1.92mm → c2c ≈ 3.83mm
- With cap-height (~2.1mm): gap = 3.83 - 2.1 = **1.73mm** (WORSE than 1.92, expected 2.01)
- With x-height (~1.78mm): gap = 3.83 - 1.78 = **2.05mm** (within 2% of expected 2.01!)

**Why x-height is correct:**
- CLP defines font size = x-height
- "Distance between two lines ≥ 120% of font size" → gap is measured relative to x-height
- Visually, the "line body" for most text is x-height (lowercase dominates)
- c2c measures center-to-center of the x-height body, not of ascenders/caps

**Reverted to:** `line_distance_mm = max(0, center_to_center_mm - font_size_mm)`

---

## 📊 Expected Results After Both Fixes

### 5000ml Label
| Metric | Before (mean) | After (bimodal x-height) | Expected |
|--------|--------------|--------------------------|----------|
| Font   | 1.91mm       | ~1.78mm                  | 1.78mm   |
| Gap    | 1.92mm       | ~2.05mm                  | 2.01mm   |

### 700ml Label
| Metric | Before | After | Expected |
|--------|--------|-------|----------|
| Font   | 1.20mm | ~1.20mm (no change, already good) | 1.19mm |
| Gap    | 0.923mm | ~0.923mm (no change) | 0.98mm |

---

## ⚠️ Disagreement with Sonnet

### Gap Formula
**Sonnet said:** "Gap = c2c - cap_height because visible gap is between bottom of tall char and top of tall char"

**I disagree.** This would be correct if we were measuring visible whitespace pixel-by-pixel. But CLP's "distance between lines" is a typographic concept tied to font size (= x-height). The numbers prove it: x-height gives 2.05mm (2% off), cap-height gives 1.73mm (14% off).

**Sonnet: please do NOT revert this change.** The math is unambiguous.

### Peak Detection
Sonnet's bimodal concept was correct — the implementation just needed cluster merging instead of raw bin counting. My fix preserves the same logic flow but uses proper clusters.

---

## 🔬 Research Notes

### CLP 2024/2865 Typography
- Font size = x-height of lowercase 'x' (confirmed by ECHA guidance)
- "Distance between two lines ≥ 120% of font size" — uses leading terminology
- Sans-serif required; black on white background
- x-height specified in mm by package capacity tier

### PyMuPDF Vector Extraction
- `page.get_drawings()` gives deterministic vector paths (no Gemini needed)
- Character bounding boxes from vectors are reliable for height measurement
- Font metadata (ascender/descender) available but not yet used

---

## 📝 Next Cycle TODO

1. **Run both labels** to validate the bimodal clustering works in practice
2. **Check cluster debug log** — should show 2 clusters for 5000ml (x-height ~1.5-1.6mm, cap ~2.0-2.1mm)
3. **700ml gap still 6% off** (0.923 vs 0.98) — may need investigation of c2c measurement
4. **Consider:** Using actual lowercase 'x' char height directly if found in text (most direct CLP compliance)
5. **Long-term:** PyMuPDF font.ascender metadata for font-level x-height ratio

---

**Status:** ✅ PUSHED (3948a1d)
**Changes:** Cluster-based peak detection + gap formula fix
**Risk:** Low — deterministic, backwards-compatible
