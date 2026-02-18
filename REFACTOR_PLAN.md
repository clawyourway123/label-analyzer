# Refactor Plan - Phase 2

## measure_font_from_pdf_vectors breakdown (lines 1700-2598, 898 lines)

### Sub-functions to extract:

1. **`_compute_pdf_scale(page, pdf_path)`** (lines 1732-1796)
   - Cache check + Manual ref dimension mode + Auto disabled mode
   - Returns: (vertical_scale, horizontal_scale)
   - Logs: [SCALE]

2. **`_extract_region_paths(page, pt_xmin, pt_ymin, pt_xmax, pt_ymax)`** (lines 1798-1827)
   - Filter drawings in region → region_paths list
   - Returns: List[Dict] with path metadata
   - Logs: [MEASURE]

3. **`_group_paths_into_lines(region_paths)`** (lines 1829-1860)
   - Group by y_center → text_lines
   - Returns: List[List[Dict]]
   - Logs: [MEASURE]

4. **`_measure_char_heights_per_line(text_lines, PT_TO_MM)`** (lines 1862-1908)
   - For each line: group by x overlap → characters, measure heights
   - Returns: (line_char_heights, line_y_centers_mm)
   - Logs: [MEASURE]

5. **`_identify_body_text_lines(line_char_heights)`** (lines 1910-1960)
   - Cluster line heights → find dominant body text cluster
   - Returns: (body_line_indices, body_char_heights, best_line_bin)
   - Logs: [MEASURE]

6. **`_measure_x_height_from_text_layer(pdf_path, region_rect_px)`** (lines 1962-2100)
   - Use get_text("rawdict") to read actual chars
   - Identify x-height and cap-height from char bboxes
   - Returns: (text_xheight_mm, text_capheight_mm) or (None, None)
   - Logs: [TEXT]

7. **`_cluster_heights_by_histogram(char_heights_mm, tolerance_mm=0.02)`** (lines 2102-2230)
   - Bin → merge peaks → filter → sort
   - Returns: List[(height_mm, count), ...]
   - Logs: [CLUSTER]

8. **`_select_best_peak_with_threshold(peaks, clp_threshold_mm)`** (lines 2232-2280)
   - If threshold > 0: prefer peaks near threshold
   - Else: use most frequent
   - Returns: (selected_height_mm, selection_method)
   - Logs: [PEAK]

9. **`_measure_line_spacing(body_line_indices, line_y_centers_mm, x_height_mm)`** (lines 2282-2310)
   - Calculate center-to-center spacing, gap
   - Returns: Dict with c2c_mm, gap_mm, confidence
   - Logs: [SPACING]

### Dependencies:
- All use self.original_dpi, PT_TO_MM, self._pdf_scale_cache, logger
- Need to pass these as args or keep as instance vars

### Tests:
- test_700ml.py: expects 1.19mm (mixed-case, threshold=1.2mm)
- test_5000ml.py: expects 1.78mm (all-caps, threshold=1.8mm)
- Must keep BOTH passing throughout

### Phase 2 Steps:
1. Extract helpers to production code (one at a time)
2. Test after each extraction
3. Simplify nested conditions
4. Add docstrings as we go
5. Report when ready for Phase 3
