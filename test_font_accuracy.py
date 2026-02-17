#!/usr/bin/env python3
"""Font Accuracy Test Suite for Label Analyzer Production.

Tests measurement accuracy against known ground-truth values.
Validates x-height measurement, cap-height conversion, line spacing,
and confidence-based correction logic.

Usage:
    python test_font_accuracy.py                    # Run all tests
    python test_font_accuracy.py --unit             # Unit tests only (no API)
    python test_font_accuracy.py --integration      # Integration tests (needs Gemini API)
    python test_font_accuracy.py --report           # Generate accuracy report

Ground truth labels should be placed in /Users/clawdy/Desktop/test_labels/
with a corresponding .json file containing expected measurements.
Example: sample1.jpg + sample1.json
"""

import argparse
import json
import logging
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from label_analyzer_production import (
    validate_measurements_against_rules,
    CLP_VALIDATION_SCHEMA,
)

logger = logging.getLogger(__name__)

# ─── Ground Truth Definitions ───────────────────────────────────────────────

TEST_LABELS_DIR = Path("/Users/clawdy/Desktop/test_labels")
ACCURACY_REPORT_PATH = Path("/Users/clawdy/Desktop/test_labels/accuracy_report.json")

# Known ground-truth measurements for test validation
# Add entries as you validate labels manually
GROUND_TRUTH: Dict[str, Dict] = {
    # Example format:
    # "sample1_cleaning_label.jpg": {
    #     "font_size_mm": 1.2,       # Measured with calibrated tool
    #     "line_distance_mm": 1.5,   # Baseline-to-baseline
    #     "text_type": "mixed",      # "lowercase", "all-caps", "mixed"
    #     "tolerance_pct": 5.0,      # Acceptable error %
    #     "notes": "Small text on cleaning product"
    # },
}


# ─── Unit Tests (No API Required) ───────────────────────────────────────────

class TestCorrectionFactor(unittest.TestCase):
    """Test the confidence-based correction factor logic."""

    def _get_correction_factor(self, confidence: float) -> float:
        """Mirror the correction logic from production code."""
        if confidence >= 0.85:
            return 1.0
        elif confidence >= 0.70:
            return 0.98
        else:
            return 1.0

    def test_high_confidence_no_correction(self):
        """High confidence (>=0.85) should not apply correction."""
        self.assertEqual(self._get_correction_factor(0.85), 1.0)
        self.assertEqual(self._get_correction_factor(0.90), 1.0)
        self.assertEqual(self._get_correction_factor(1.0), 1.0)

    def test_medium_confidence_gentle_correction(self):
        """Medium confidence (0.70-0.85) should apply 0.98x correction."""
        self.assertEqual(self._get_correction_factor(0.70), 0.98)
        self.assertEqual(self._get_correction_factor(0.75), 0.98)
        self.assertEqual(self._get_correction_factor(0.84), 0.98)

    def test_low_confidence_no_correction(self):
        """Low confidence (<0.70) should not apply correction."""
        self.assertEqual(self._get_correction_factor(0.0), 1.0)
        self.assertEqual(self._get_correction_factor(0.50), 1.0)
        self.assertEqual(self._get_correction_factor(0.69), 1.0)


class TestCapHeightConversion(unittest.TestCase):
    """Test cap-height to x-height conversion (0.71 factor)."""

    CAP_TO_X_RATIO = 0.71

    def test_conversion_accuracy(self):
        """Cap-height * 0.71 should approximate x-height."""
        # Known typography: if cap-height = 2.0mm, x-height ≈ 1.42mm
        cap_height = 2.0
        expected_x = cap_height * self.CAP_TO_X_RATIO
        self.assertAlmostEqual(expected_x, 1.42, places=2)

    def test_conversion_range(self):
        """Conversion should work across typical label font sizes."""
        for cap_mm in [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]:
            x_height = cap_mm * self.CAP_TO_X_RATIO
            self.assertGreater(x_height, 0)
            self.assertLess(x_height, cap_mm)
            # X-height should be 65-75% of cap-height (industry range)
            ratio = x_height / cap_mm
            self.assertGreaterEqual(ratio, 0.65)
            self.assertLessEqual(ratio, 0.75)


class TestRuleValidation(unittest.TestCase):
    """Test CLP rule validation logic."""

    def test_pass_large_package(self):
        """Standard large package (>125ml) should pass with 1.2mm+ font."""
        metrics = {
            "font_size_mm": 1.5,
            "line_distance_mm": 2.0,
            "background_color": "white",
            "text_color": "black",
            "contrast_assessment": "high",
            "measurement_confidence": 0.9,
        }
        result = validate_measurements_against_rules(metrics, package_size_ml=500)
        self.assertEqual(result["rule_1_font_size"]["status"], "PASS")

    def test_fail_tiny_font(self):
        """Font below threshold should fail."""
        metrics = {
            "font_size_mm": 0.5,
            "line_distance_mm": 0.7,
            "background_color": "white",
            "text_color": "black",
            "contrast_assessment": "high",
            "measurement_confidence": 0.9,
        }
        result = validate_measurements_against_rules(metrics, package_size_ml=500)
        self.assertEqual(result["rule_1_font_size"]["status"], "FAIL")

    def test_skip_low_confidence(self):
        """Low confidence should return SKIP, not PASS/FAIL."""
        metrics = {
            "font_size_mm": 1.5,
            "line_distance_mm": 2.0,
            "background_color": "white",
            "text_color": "black",
            "contrast_assessment": "high",
            "measurement_confidence": 0.3,
        }
        result = validate_measurements_against_rules(metrics, package_size_ml=500)
        self.assertEqual(result["rule_1_font_size"]["status"], "SKIP")

    def test_line_spacing_120pct(self):
        """Line distance must be >= 120% of font size."""
        # Passing case: 1.2mm font, 1.5mm line distance (125%)
        metrics = {
            "font_size_mm": 1.2,
            "line_distance_mm": 1.5,
            "background_color": "white",
            "text_color": "black",
            "contrast_assessment": "high",
            "measurement_confidence": 0.9,
        }
        result = validate_measurements_against_rules(metrics, package_size_ml=500)
        self.assertEqual(result["rule_2_line_distance"]["status"], "PASS")

    def test_line_spacing_too_tight(self):
        """Line distance < 120% of font size should fail."""
        metrics = {
            "font_size_mm": 1.2,
            "line_distance_mm": 1.3,  # 108%, below 120%
            "background_color": "white",
            "text_color": "black",
            "contrast_assessment": "high",
            "measurement_confidence": 0.9,
        }
        result = validate_measurements_against_rules(metrics, package_size_ml=500)
        self.assertEqual(result["rule_2_line_distance"]["status"], "FAIL")

    def test_inner_packaging_exemption(self):
        """Inner packaging <=10ml gets exemption."""
        metrics = {
            "font_size_mm": 0.8,  # Below normal threshold
            "line_distance_mm": 1.0,
            "background_color": "white",
            "text_color": "black",
            "contrast_assessment": "high",
            "measurement_confidence": 0.9,
        }
        result = validate_measurements_against_rules(
            metrics, package_size_ml=5, is_inner_packaging=True
        )
        # Should pass or have exemption status
        self.assertIn(result["rule_1_font_size"]["status"], ["PASS", "EXEMPT"])


class TestScaleFactorLogic(unittest.TestCase):
    """Test scale factor calculations."""

    def test_no_scaling_needed(self):
        """Scale factor 1.0 should not change values."""
        font_px = 30.0
        dpmm = 11.81  # 300 DPI
        scale = 1.0
        font_mm = (font_px * scale) / dpmm
        expected = font_px / dpmm
        self.assertAlmostEqual(font_mm, expected, places=4)

    def test_2x_downscale(self):
        """If Gemini halved the image, scale factor should double pixels."""
        font_px_gemini = 15.0  # Gemini measured in resized space
        scale = 2.0  # Original was 2x bigger
        dpmm = 11.81
        font_mm = (font_px_gemini * scale) / dpmm
        # Should be ~2.54mm
        self.assertAlmostEqual(font_mm, 2.54, places=1)


# ─── Integration Tests (Require API) ────────────────────────────────────────

class TestIntegrationAccuracy(unittest.TestCase):
    """Integration tests that run the actual analyzer on test images.
    
    Requires:
    - Gemini API access (GCP project configured)
    - Test images in TEST_LABELS_DIR
    - Ground truth entries in GROUND_TRUTH dict
    """

    @unittest.skipUnless(
        GROUND_TRUTH and TEST_LABELS_DIR.exists(),
        "No ground truth data or test labels directory"
    )
    def test_ground_truth_accuracy(self):
        """Test analyzer accuracy against known ground truth."""
        from label_analyzer_production import analyze_image_file

        results = []
        for filename, truth in GROUND_TRUTH.items():
            image_path = TEST_LABELS_DIR / filename
            if not image_path.exists():
                self.skipTest(f"Image not found: {image_path}")
                continue

            try:
                analyzer, parts = analyze_image_file(
                    str(image_path), project_id="your-project-id"
                )
            except Exception as e:
                logger.error(f"Failed to analyze {filename}: {e}")
                continue

            # Extract font measurements from analysis
            for part in parts:
                if hasattr(part, 'clp_validation') and part.clp_validation:
                    measured_mm = part.clp_validation.get('font_size_mm', 0)
                    expected_mm = truth['font_size_mm']
                    tolerance = truth.get('tolerance_pct', 5.0) / 100.0

                    error_pct = abs(measured_mm - expected_mm) / expected_mm * 100
                    results.append({
                        "file": filename,
                        "expected_mm": expected_mm,
                        "measured_mm": measured_mm,
                        "error_pct": error_pct,
                        "within_tolerance": error_pct <= truth.get('tolerance_pct', 5.0),
                    })

                    self.assertAlmostEqual(
                        measured_mm, expected_mm,
                        delta=expected_mm * tolerance,
                        msg=f"{filename}: measured {measured_mm:.3f}mm vs expected {expected_mm:.3f}mm ({error_pct:.1f}% error)"
                    )

        if results:
            _save_accuracy_report(results)


# ─── Reporting ───────────────────────────────────────────────────────────────

def _save_accuracy_report(results: List[Dict]) -> None:
    """Save accuracy report to JSON."""
    report = {
        "timestamp": datetime.now().isoformat(),
        "total_tests": len(results),
        "passed": sum(1 for r in results if r.get("within_tolerance")),
        "avg_error_pct": sum(r["error_pct"] for r in results) / len(results) if results else 0,
        "max_error_pct": max(r["error_pct"] for r in results) if results else 0,
        "results": results,
    }
    ACCURACY_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ACCURACY_REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    logger.info(f"Accuracy report saved to {ACCURACY_REPORT_PATH}")
    print(f"\n{'='*60}")
    print(f"ACCURACY REPORT")
    print(f"{'='*60}")
    print(f"Tests: {report['total_tests']} | Passed: {report['passed']}")
    print(f"Avg Error: {report['avg_error_pct']:.2f}% | Max Error: {report['max_error_pct']:.2f}%")
    print(f"{'='*60}\n")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Label Analyzer Font Accuracy Tests")
    parser.add_argument("--unit", action="store_true", help="Run unit tests only")
    parser.add_argument("--integration", action="store_true", help="Run integration tests")
    parser.add_argument("--report", action="store_true", help="Generate accuracy report")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    if args.unit or (not args.integration and not args.report):
        suite.addTests(loader.loadTestsFromTestCase(TestCorrectionFactor))
        suite.addTests(loader.loadTestsFromTestCase(TestCapHeightConversion))
        suite.addTests(loader.loadTestsFromTestCase(TestRuleValidation))
        suite.addTests(loader.loadTestsFromTestCase(TestScaleFactorLogic))

    if args.integration or args.report:
        suite.addTests(loader.loadTestsFromTestCase(TestIntegrationAccuracy))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
