# Production v1.0 — Label Analyzer

**Status:** Production-ready (clean, tested, shipping version)  
**Branch:** `production/v1.0`

## Overview

CLP (Classification, Labelling, and Packaging) label analyzer for hazardous product compliance.

Measures font sizes and spacing from product labels (PDF or image) and validates against EU Regulation 1272/2008 requirements:
- Font size thresholds (size-dependent: 1.2–1.8mm)
- Line distance (≥120% of font size)
- Background contrast (white/yellow background required)

## Core Features

✅ **Vector Measurement** — Extracts PDF glyphs for exact font sizes (100% deterministic)  
✅ **DPI Calibration** — Auto-detects PDF scaling via measurement line OCR  
✅ **Compliance Validation** — Applies 3 EU CLP rules with clear PASS/FAIL/SKIP results  
✅ **Confidence Scoring** — Multi-signal ensemble scorer for region detection  
✅ **Batch Processing** — Process multiple labels in parallel with disk-based caching  

## Code Structure

### Module Constants (Top of File)

All magic numbers extracted to named constants for easy tuning:

```python
# Measurement & conversion
PT_TO_MM = 25.4 / 72  # Point to millimeter conversion
CAP_HEIGHT_TO_X_HEIGHT_RATIO = 0.85  # For deriving x-height from cap-height

# Height thresholds
X_HEIGHT_TOLERANCE_MM = 0.05
BIMODAL_MIN_SEPARATION_MM = 0.25
BIMODAL_RATIO_MIN = 0.60
BIMODAL_RATIO_MAX = 0.88

# Clustering & validation
HEIGHT_CLUSTER_BIN_MM = 0.02
MIN_CHARS_IN_CLUSTER = 3
MIN_CONFIDENCE_FOR_VALID_MEASUREMENT = 0.5
MIN_FONT_SIZE_FALLBACK_ML = 5000  # Safe fallback when size detection fails
```

Adjust these to tune detection behavior. All thresholds have comments explaining their purpose.

### Key Functions

#### `validate_measurements_against_rules(metrics, package_size_ml, is_inner_packaging)`

Applies CLP compliance checks to measured metrics.

**Returns:** Dict with:
- `rule_results`: Per-rule status (PASS/FAIL/SKIP)
- `overall_compliance`: Final status
- `compliance_confidence`: Measurement confidence

#### `detect_bimodal_peaks(peaks, clp_threshold_mm)`

Detects x-height and cap-height from character height distribution.

Handles:
- Single peak (ambiguous, uses heuristics)
- Bimodal distribution (mixed-case or all-caps detection)
- Fallback to most frequent peak

**Returns:** (x_height_mm, cap_height_mm, measurement_approach)

### Key Classes

#### `LabelAnalyzer`

Main analysis engine. Entry point for all analysis.

Methods:
- `analyze(image, image_data)` — Full pipeline (detect → refine → validate)
- `analyze_pdf(pdf_path)` — Analyze PDF directly
- `analyze_batch(pdf_paths, ...)` — Batch process multiple files
- `measure_font_from_pdf_vectors(...)` — Exact vector-based measurement

#### `DetectedPart`

Result of analyzing a single region.

Properties:
- `font_size_mm` — Measured x-height in millimeters
- `compliance_status` — Overall CLP compliance (PASS/FAIL/SKIP/N/A)
- `is_compliant()` — Boolean for pass/fail
- `confidence` — Detection confidence (0–1)

#### `ResponseCache`

Disk-backed cache for API responses. Avoids redundant Gemini calls.

Methods:
- `get(image_data, prompt, schema)` — Retrieve cached response
- `put(image_data, prompt, response_text, schema)` — Store response
- `clear()` — Clear all cache entries
- `stats()` — Hit/miss statistics

#### `CalibrationResult`

DPI calibration with persistent disk caching.

Features:
- Auto-detects PDF scale from measurement lines
- Locks DPI after first successful calibration
- Disk cache prevents re-calibration of known PDFs

### Logging

Structured logging with clear prefixes:

```
INFO | label_analyzer | Starting label analysis...
INFO | label_analyzer | Stage 1: Rough detection...
INFO | label_analyzer | ✓ Measured: 1.19mm x-height
INFO | label_analyzer | ✓ PASS: Font size OK (1.19mm ≥ 1.2mm threshold)
```

Log levels:
- `INFO`: Key milestones, measurements, results
- `WARNING`: Marginal measurements, borderline compliance
- `ERROR`: Failures, measurement disagreements
- `DEBUG`: Detailed diagnostic info (disabled by default)

## Usage

### Analyze a Single PDF

```python
analyzer = LabelAnalyzer(project_id="your-gcp-project")
parts = analyzer.analyze_pdf("/path/to/label.pdf")

for part in parts:
    print(f"{part.label}: {part.font_size_mm:.2f}mm → {part.compliance_status}")
```

### Batch Process

```python
results = analyzer.analyze_batch([
    "/path/to/label1.pdf",
    "/path/to/label2.pdf",
], max_workers=4)

for result in results:
    for part in result.parts:
        print(f"{part.label}: {part.compliance_status}")
    if result.error:
        print(f"Error: {result.error}")
```

### Validate Measurements

```python
metrics = {
    "font_size_mm": 1.15,
    "line_distance_mm": 1.40,
    "contrast_assessment": "high",
    "measurement_confidence": 0.9
}

rules = validate_measurements_against_rules(metrics, package_size_ml=500)
print(f"Overall: {rules['overall_compliance']}")
print(f"Font rule: {rules['rule_1_font_size']}")
```

## Configuration

### DPI Calibration

Auto-detects from PDF measurement lines. To provide manual calibration:

```python
analyzer = LabelAnalyzer(project_id="...")
analyzer._reference_dimensions = {"width_mm": 100, "height_mm": 150}
```

### Caching

Enable/disable API response caching:

```python
analyzer.response_cache.disable()  # Disable for fresh Gemini calls
analyzer.response_cache.clear()     # Clear cached responses
stats = analyzer.response_cache.stats()  # View hit/miss stats
```

### Package Size Detection

Auto-detects container size from label. To override:

```python
analyzer.package_size_ml = 500
analyzer.package_size_confidence = 1.0
```

## Testing

Run the test suite:

```bash
pytest tests/
```

Key test files:
- `tests/test_ensemble_confidence.py` — Confidence scoring
- `tests/test_response_cache.py` — Cache behavior
- `tests/test_retry.py` — Retry logic

## Performance

- **Single PDF:** ~3–5 seconds (depends on Gemini API latency)
- **Batch (10 files):** ~30–50 seconds with 4 workers
- **Caching:** 95%+ hit rate on repeated analyses

## Known Limitations

1. **Text-only PDFs:** Requires Gemini vision (slower than vector measurement)
2. **Rotated text:** May measure incorrectly (auto-detected and warned)
3. **Multiple font sizes:** Reports smallest (safest) measurement
4. **Inner packaging ≤10ml:** Uses exemption (requires human judgment for legibility)

## Dependencies

- `google-genai` — Gemini API client
- `pymupdf` — PDF processing
- `pydantic` — Data validation
- `pillow` — Image processing

## License

Internal use only.

---

**Questions?** See memory/2026-02-17.md for development notes.
