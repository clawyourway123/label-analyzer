# Opus Review — Circular CLP Threshold Fix

**Commit:** 1add56f  
**Date:** Feb 17, 2026 — 7:55 PM PST  
**Issue:** Package size detection failure cascades into wrong font measurement

## Problem

The bimodal peak detection used the CLP threshold to SELECT which peak was x-height. This created circular logic:
1. Package size → CLP threshold (e.g., 500ml → 1.2mm)
2. CLP threshold → peak selection (picks peak nearest 1.2mm)
3. Peak selection → font measurement
4. **If package size is wrong, measurement is wrong**

When Gemini returned `500ml (confidence: 0%)` for a 5000ml container:
- Threshold = 1.2mm instead of 1.8mm
- Algorithm picked 1.57mm × 0.75 = 1.177mm (matching 1.2mm threshold)
- But 1.57mm is the actual x-height, not cap-height!
- Correct answer: x-height=1.57mm, with cap-height=2.09mm

## Changes

### 1. Threshold-Independent Bimodal Detection
**Before:** `if clp_threshold_mm > 0:` → pick peaks near threshold, try cap×ratio combos targeting threshold  
**After:** Find bimodal pairs using ONLY distribution shape:
- Enumerate all significant peak pairs (≥15% of top count)
- Valid pair: separation > 0.25mm, height ratio 0.60-0.88
- Pick pair with highest combined character count
- Smaller peak = x-height, larger = cap-height
- CLP threshold logged but NOT used for selection

### 2. Safe Package Size Fallback
**Before:** Default to 500ml (1.2mm threshold) — most lenient  
**After:** Default to 5000ml (1.8mm threshold) — most strict  
Rationale: If detection fails, conservative threshold avoids false passes.

### 3. Constructor Default Updated
`package_size_ml` default: 500 → 5000

## Test Results (700ml PDF, full-page scan)

| CLP Threshold | Font (mm) | Gap (mm) | Approach |
|---|---|---|---|
| 0.0 (none) | 1.100 | 1.033 | bimodal-xheight |
| 1.2 (wrong) | 1.100 | 1.033 | bimodal-xheight |
| 1.4 (correct) | 1.100 | 1.033 | bimodal-xheight |

**Key result:** All three thresholds produce IDENTICAL measurements. The circular dependency is eliminated.

### Note on 1.100mm vs expected 1.19mm
Full-page scan includes non-CLP text (ingredient lists, marketing copy) which has smaller font. The 1.19mm peak exists (456 chars) but the 1.100mm peak (641 chars) + 1.570mm peak (664 chars) form a stronger bimodal pair (ratio=0.701). In production, Gemini crops the CLP region first, excluding non-CLP text, so the 1.19mm peak should dominate.

## Risks
- If the CLP region contains two different font sizes with similar counts but the non-CLP font has more chars, the wrong pair could be selected
- Mitigation: In production, Gemini region cropping narrows the peak set significantly
- The 5000ml default is conservative — may flag small containers as non-compliant when they actually pass

## Next Steps
- Test with Gemini credentials to verify full pipeline (region crop → vector measurement)
- Verify 5000ml PDF produces correct 1.78mm measurement
- Consider adding package size inference from label dimensions as a backup
