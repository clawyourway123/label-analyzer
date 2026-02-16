"""Tests for EnsembleConfidence scoring."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from label_analyzer_production import EnsembleConfidence, ConfidenceSignal


def make_rect(xmin, ymin, xmax, ymax):
    return {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}


class TestEnsembleConfidence:

    def setup_method(self):
        self.scorer = EnsembleConfidence()

    # --- IoU ---

    def test_iou_identical(self):
        r = make_rect(0, 0, 100, 100)
        assert self.scorer._iou(r, r) == 1.0

    def test_iou_no_overlap(self):
        a = make_rect(0, 0, 50, 50)
        b = make_rect(60, 60, 100, 100)
        assert self.scorer._iou(a, b) == 0.0

    def test_iou_partial(self):
        a = make_rect(0, 0, 100, 100)
        b = make_rect(50, 50, 150, 150)
        iou = self.scorer._iou(a, b)
        assert 0.1 < iou < 0.5

    # --- Geometric plausibility ---

    def test_geo_normal_region(self):
        """A region covering ~10% of the image should score 1.0."""
        rect = make_rect(0, 0, 100, 100)
        score = self.scorer.geometric_plausibility(rect, 300, 300)
        assert score == 1.0

    def test_geo_tiny_region(self):
        """A very tiny region should be penalised."""
        rect = make_rect(0, 0, 2, 2)  # 4px in a 1000x1000 image
        score = self.scorer.geometric_plausibility(rect, 1000, 1000)
        assert score < 0.5

    def test_geo_full_image(self):
        """A region covering >85% should be penalised."""
        rect = make_rect(0, 0, 950, 950)
        score = self.scorer.geometric_plausibility(rect, 1000, 1000)
        assert score < 1.0

    # --- Refinement agreement ---

    def test_refinement_identical(self):
        r = make_rect(10, 10, 200, 200)
        assert self.scorer.refinement_agreement(r, r) == 1.0

    def test_refinement_no_rough(self):
        r = make_rect(10, 10, 200, 200)
        assert self.scorer.refinement_agreement(None, r) == 0.5

    # --- Full scoring ---

    def test_score_high_confidence(self):
        """High model conf + good geometry + good agreement → high score."""
        score, signals = self.scorer.score(
            model_confidence=0.95,
            rough_rect=make_rect(10, 10, 200, 200),
            refined_rect=make_rect(12, 8, 198, 205),
            image_width=1000,
            image_height=800,
        )
        assert score > 0.8
        assert len(signals) == 3

    def test_score_low_model_conf(self):
        """Low model confidence drags the ensemble down."""
        score, _ = self.scorer.score(
            model_confidence=0.2,
            rough_rect=make_rect(10, 10, 200, 200),
            refined_rect=make_rect(12, 8, 198, 205),
            image_width=1000,
            image_height=800,
        )
        assert score < 0.7

    def test_score_returns_signals(self):
        _, signals = self.scorer.score(
            model_confidence=0.8,
            rough_rect=None,
            refined_rect=make_rect(50, 50, 300, 300),
            image_width=1000,
            image_height=1000,
        )
        names = {s.name for s in signals}
        assert names == {"model_confidence", "geometric_plausibility", "refinement_agreement"}

    def test_custom_weights(self):
        """Custom weights should shift the score."""
        # Model-only weighting
        scorer = EnsembleConfidence(weights={
            "model_confidence": 10.0,
            "geometric_plausibility": 0.0,
            "refinement_agreement": 0.0,
        })
        score, _ = scorer.score(
            model_confidence=0.9,
            rough_rect=None,
            refined_rect=make_rect(0, 0, 1, 1),  # terrible geometry
            image_width=1000, image_height=1000,
        )
        # Should be ~0.9 since only model matters
        assert abs(score - 0.9) < 0.05

    def test_score_clamped(self):
        """Score should always be in [0, 1]."""
        score, _ = self.scorer.score(
            model_confidence=1.5,  # out of range
            rough_rect=make_rect(0, 0, 500, 500),
            refined_rect=make_rect(0, 0, 500, 500),
            image_width=1000, image_height=1000,
        )
        assert 0.0 <= score <= 1.0
