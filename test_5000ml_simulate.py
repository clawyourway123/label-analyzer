#!/usr/bin/env python3
"""Simulate the 5000ml peak selection with the new cap-derived logic."""

# From the user's logs, the 5000ml peaks (raw, no scale) are:
# 1.57mm (55 chars), 2.09mm (51 chars), 2.28mm (35 chars)
# CLP threshold for 5000ml = 1.8mm
# Ground truth x-height = 1.78mm

peaks_by_count = [(1.57, 55), (2.09, 51), (2.28, 35)]
clp_threshold_mm = 1.8
top_count = peaks_by_count[0][1]

print("5000ml Peak Selection Simulation")
print(f"Peaks: {peaks_by_count}")
print(f"CLP threshold: {clp_threshold_mm}mm")
print()

# Step 1: Direct match (existing logic)
clp_candidates = [
    (h, c) for h, c in peaks_by_count
    if abs(h - clp_threshold_mm) <= 0.15 and c >= top_count * 0.2
]
print(f"Direct CLP candidates (±0.15mm of {clp_threshold_mm}mm): {clp_candidates}")

if not clp_candidates:
    print("No direct match — trying cap-height derivation...")
    
    # Step 2: Cap-height derivation (NEW logic)
    for ratio in [0.85, 0.82, 0.80, 0.78, 0.75, 0.72, 0.70]:
        cap_candidates = [
            (h, c) for h, c in peaks_by_count
            if h > clp_threshold_mm and abs(h * ratio - clp_threshold_mm) <= 0.10
            and c >= top_count * 0.15
        ]
        if cap_candidates:
            best_cap = max(cap_candidates, key=lambda p: p[1])
            xheight_mm = best_cap[0] * ratio
            print(f"  ✅ ratio={ratio}: cap={best_cap[0]:.3f}mm * {ratio} = x-height={xheight_mm:.3f}mm (target: 1.78mm, error: {abs(xheight_mm - 1.78)/1.78*100:.1f}%)")
            break
        else:
            # Show what each ratio would give for debugging
            for h, c in peaks_by_count:
                if h > clp_threshold_mm:
                    est = h * ratio
                    diff = abs(est - clp_threshold_mm)
                    if diff <= 0.15:
                        print(f"  ratio={ratio}: cap={h:.3f}mm → x={est:.3f}mm (diff from threshold: {diff:.3f}mm, need ≤0.10)")
