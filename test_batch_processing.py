"""Tests for batch processing in label_analyzer_production.py"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from PIL import Image as PIL_Image

# Add parent dir to path
sys.path.insert(0, os.path.dirname(__file__))

from label_analyzer_production import (
    BatchResult,
    DetectedPart,
    LabelAnalyzer,
    PartClassification,
    Rectangle,
    analyze_batch,
)


class TestBatchResult(unittest.TestCase):
    def test_success_property(self):
        r = BatchResult(path="a.jpg", elapsed_seconds=1.0)
        self.assertTrue(r.success)

    def test_failure_property(self):
        r = BatchResult(path="a.jpg", error=ValueError("boom"))
        self.assertFalse(r.success)

    def test_default_parts_empty(self):
        r = BatchResult(path="a.jpg")
        self.assertEqual(r.parts, [])


class TestAnalyzeBatch(unittest.TestCase):
    """Test batch orchestration with mocked analysis."""

    def _make_test_images(self, count: int) -> list:
        """Create temporary test image files."""
        paths = []
        for i in range(count):
            f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            img = PIL_Image.new("RGB", (100, 100), color=(i * 50, 100, 150))
            img.save(f, format="JPEG")
            f.close()
            paths.append(f.name)
        return paths

    @patch.object(LabelAnalyzer, "analyze", return_value=[])
    @patch.object(LabelAnalyzer, "calibrate_dpi", return_value=False)
    def test_batch_returns_correct_count(self, mock_cal, mock_analyze):
        paths = self._make_test_images(3)
        try:
            results = LabelAnalyzer.analyze_batch(paths, "test-project", max_workers=2)
            self.assertEqual(len(results), 3)
            for r in results:
                self.assertTrue(r.success)
        finally:
            for p in paths:
                os.unlink(p)

    def test_batch_handles_missing_file(self):
        results = LabelAnalyzer.analyze_batch(
            ["/nonexistent/image.jpg"], "test-project", max_workers=1
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertIsInstance(results[0].error, Exception)

    def test_callback_invoked(self):
        callback_calls = []

        def cb(result, idx, total):
            callback_calls.append((idx, total))

        results = LabelAnalyzer.analyze_batch(
            ["/nonexistent/a.jpg", "/nonexistent/b.jpg"],
            "test-project",
            max_workers=1,
            on_complete=cb,
        )
        self.assertEqual(len(callback_calls), 2)
        self.assertTrue(all(t == 2 for _, t in callback_calls))

    def test_preserves_order(self):
        """Results should be in same order as input paths."""
        paths = ["/nonexistent/a.jpg", "/nonexistent/b.jpg", "/nonexistent/c.jpg"]
        results = LabelAnalyzer.analyze_batch(paths, "test-project", max_workers=2)
        for i, r in enumerate(results):
            self.assertEqual(r.path, paths[i])


if __name__ == "__main__":
    unittest.main()
