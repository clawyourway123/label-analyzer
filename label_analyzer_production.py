"""
Production-ready CLP Label Analyzer
====================================
Robust detection and compliance checking for product labels

Key improvements over POC:
- Multi-stage detection (identify → refine → validate)
- Handles irregular shapes, not just rectangles
- Confidence scoring for detected regions
- Better error handling and logging
- Structured output for downstream processing
- Batch processing support
- Disk-based response caching (avoids redundant API calls)
"""

import os
import json
import base64
import hashlib
import logging
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import List, Optional, Dict, Tuple, Callable, TypeVar
from pathlib import Path
from io import BytesIO

T = TypeVar("T")

import fitz  # PyMuPDF
from PIL import Image as PIL_Image, ImageDraw as PIL_ImageDraw
from pydantic import BaseModel, Field


# ============================================================================
# LOGGING SETUP
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# DATA MODELS
# ============================================================================

class Point(BaseModel):
    x: int
    y: int


class Polygon(BaseModel):
    """Support for non-rectangular regions"""
    points: List[Point]
    
    def to_list_of_tuples(self) -> List[Tuple[int, int]]:
        return [(p.x, p.y) for p in self.points]


class Rectangle(BaseModel):
    xmin: int
    ymin: int
    xmax: int
    ymax: int
    
    def width(self) -> int:
        return self.xmax - self.xmin
    
    def height(self) -> int:
        return self.ymax - self.ymin
    
    def area(self) -> int:
        return self.width() * self.height()
    
    def center(self) -> Tuple[int, int]:
        return ((self.xmin + self.xmax) // 2, (self.ymin + self.ymax) // 2)


class ConfidenceSignal(BaseModel):
    """A single confidence signal from one scoring dimension."""
    name: str
    value: float = Field(ge=0.0, le=1.0)
    weight: float = Field(default=1.0, ge=0.0)


class EnsembleConfidence:
    """Multi-signal ensemble confidence scorer for detected regions.

    Combines independent confidence signals (model output, geometric
    plausibility, cross-region consistency) into a single calibrated
    score using weighted averaging with optional signal gating.

    Signals:
        - model_confidence: Raw confidence from the vision model.
        - geometric_plausibility: How reasonable the region's shape/size is
          relative to the full image (penalizes tiny or full-image regions).
        - refinement_agreement: How much the refined bbox agrees with the
          rough bbox (high IoU = both stages agree = more trustworthy).

    Usage::

        scorer = EnsembleConfidence()
        score = scorer.score(
            model_confidence=0.85,
            rough_rect={"xmin": 10, "ymin": 10, "xmax": 200, "ymax": 200},
            refined_rect={"xmin": 12, "ymin": 8, "xmax": 198, "ymax": 205},
            image_width=1000,
            image_height=800,
        )
    """

    # Default weights (sum needn't be 1; they are normalised internally).
    DEFAULT_WEIGHTS = {
        "model_confidence": 2.0,
        "geometric_plausibility": 1.0,
        "refinement_agreement": 1.5,
    }

    # Regions smaller than this fraction of the image are suspect.
    MIN_AREA_FRACTION = 0.005
    # Regions larger than this fraction are suspect (probably whole-image).
    MAX_AREA_FRACTION = 0.85

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = weights or dict(self.DEFAULT_WEIGHTS)

    # ------------------------------------------------------------------
    # Individual signal calculators
    # ------------------------------------------------------------------

    @staticmethod
    def _iou(a: Dict, b: Dict) -> float:
        """Intersection-over-Union for two axis-aligned rectangles (dict form)."""
        ix1 = max(a["xmin"], b["xmin"])
        iy1 = max(a["ymin"], b["ymin"])
        ix2 = min(a["xmax"], b["xmax"])
        iy2 = min(a["ymax"], b["ymax"])
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        area_a = max(1, (a["xmax"] - a["xmin"]) * (a["ymax"] - a["ymin"]))
        area_b = max(1, (b["xmax"] - b["xmin"]) * (b["ymax"] - b["ymin"]))
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    def geometric_plausibility(self, rect: Dict, image_width: int, image_height: int) -> float:
        """Score how geometrically plausible a region is (0–1).

        Penalises regions that are implausibly small or that cover almost the
        entire image (likely a detection failure).
        """
        area = max(1, (rect["xmax"] - rect["xmin"]) * (rect["ymax"] - rect["ymin"]))
        image_area = max(1, image_width * image_height)
        frac = area / image_area

        if frac < self.MIN_AREA_FRACTION:
            # Very small — linearly ramp from 0 at 0% to 1 at threshold
            return max(0.0, frac / self.MIN_AREA_FRACTION)
        if frac > self.MAX_AREA_FRACTION:
            # Covers almost everything — ramp down
            return max(0.0, (1.0 - frac) / (1.0 - self.MAX_AREA_FRACTION))
        return 1.0

    def refinement_agreement(self, rough_rect: Optional[Dict], refined_rect: Dict) -> float:
        """Score how well rough and refined detections agree (IoU).

        If no rough rect is available, returns a neutral 0.5.
        """
        if rough_rect is None:
            return 0.5
        return self._iou(rough_rect, refined_rect)

    # ------------------------------------------------------------------
    # Main scorer
    # ------------------------------------------------------------------

    def score(
        self,
        model_confidence: float,
        rough_rect: Optional[Dict],
        refined_rect: Dict,
        image_width: int,
        image_height: int,
    ) -> Tuple[float, List[ConfidenceSignal]]:
        """Compute ensemble confidence score.

        Returns:
            Tuple of (final_score, list_of_signals) so callers can inspect
            which signals contributed and by how much.
        """
        signals = [
            ConfidenceSignal(
                name="model_confidence",
                value=min(max(model_confidence, 0.0), 1.0),
                weight=self.weights.get("model_confidence", 1.0),
            ),
            ConfidenceSignal(
                name="geometric_plausibility",
                value=self.geometric_plausibility(refined_rect, image_width, image_height),
                weight=self.weights.get("geometric_plausibility", 1.0),
            ),
            ConfidenceSignal(
                name="refinement_agreement",
                value=self.refinement_agreement(rough_rect, refined_rect),
                weight=self.weights.get("refinement_agreement", 1.0),
            ),
        ]

        total_weight = sum(s.weight for s in signals)
        if total_weight == 0:
            return 0.0, signals

        weighted_sum = sum(s.value * s.weight for s in signals)
        final = weighted_sum / total_weight
        return round(min(max(final, 0.0), 1.0), 4), signals


class PartClassification(str, Enum):
    CLP = "CLP"
    NON_CLP = "NON-CLP"
    UNKNOWN = "UNKNOWN"


@dataclass
class DetectedPart:
    """Internal representation of a detected region"""
    classification: PartClassification
    label: str
    rect: Rectangle
    polygon: Optional[Polygon] = None
    confidence: float = 0.0  # 0.0 to 1.0
    content_type: Optional[str] = None  # e.g., "Ingredients", "Hazard Symbols"
    raw_response: Optional[Dict] = None
    compliance_check: Optional[Dict] = None  # CLP validation results
    
    def is_confident(self, threshold: float = 0.7) -> bool:
        """Check if detection meets confidence threshold"""
        return self.confidence >= threshold
    
    def is_compliant(self) -> bool:
        """Check if CLP region passes all compliance checks.
        
        Only explicit "PASS" status counts as compliant.
        "SKIP" (too uncertain), "UNCLEAR", and "FAIL" all return False.
        Non-CLP regions always return True (no compliance rules apply).
        """
        # Non-CLP regions don't have strict compliance rules
        if self.classification != PartClassification.CLP:
            return True
        
        # For CLP regions, check if we have compliance data
        if not self.compliance_check:
            return False
        
        # Check overall_compliance from rule_results
        status = self.compliance_check.get("overall_compliance", "")
        
        # Only explicit PASS wins; SKIP, UNCLEAR, FAIL all return False
        return status == "PASS"
    
    def needs_human_review(self, confidence_threshold: float = 0.85, margin_pct: float = 0.1) -> bool:
        """Flag for human review if uncertain or borderline.
        
        Args:
            confidence_threshold: Flag if measurement_confidence < this
            margin_pct: Flag if result is within this % of compliance threshold
        
        Returns:
            True if human review recommended
        """
        if not self.compliance_check or self.classification != PartClassification.CLP:
            return False
        
        # Any SKIP status requires human review (measurements too uncertain)
        overall_status = self.compliance_check.get("overall_compliance", "")
        if overall_status == "SKIP":
            return True
        
        # Low confidence measurements
        if self.compliance_check.get("measurement_confidence", 1.0) < confidence_threshold:
            return True
        
        # Borderline results (within margin of threshold)
        rule_results = self.compliance_check.get("rule_results", {})
        
        # Check if any rule is borderline
        for rule_key in ["rule_1_font_size", "rule_2_line_distance", "rule_3_background_contrast"]:
            rule = rule_results.get(rule_key, {})
            if rule.get("status") in ["PASS", "FAIL"]:
                measured = rule.get("measured_mm", 0)
                threshold = rule.get("threshold_mm", 1)
                if threshold > 0:
                    pct_diff = abs(measured - threshold) / threshold
                    if pct_diff < margin_pct:
                        return True
        
        return False


@dataclass
class BatchResult:
    """Result of analyzing a single image in a batch run.

    Attributes:
        path: Original file path of the image.
        parts: Detected label parts (empty on failure).
        analyzer: The LabelAnalyzer instance used (carries calibration state).
        error: Exception if analysis failed, else None.
        elapsed_seconds: Wall-clock time for this image.
    """
    path: str
    parts: List[DetectedPart] = field(default_factory=list)
    analyzer: Optional['LabelAnalyzer'] = None
    error: Optional[Exception] = None
    elapsed_seconds: float = 0.0

    @property
    def success(self) -> bool:
        return self.error is None


class MeasurementLine(BaseModel):
    start_point: Point
    end_point: Point
    value_mm: float = Field(description="The numeric value in mm associated with the line")
    confidence: float = Field(default=0.8, description="Confidence in the measurement")


class CalibrationResult:
    """DPI calibration result"""
    def __init__(self, original_dpi: int):
        self.original_dpi = original_dpi
        self.true_dpi = original_dpi
        self.dpmm = original_dpi / 25.4
        self.measurement_line: Optional[MeasurementLine] = None
        self.is_calibrated = False
    
    def update(self, line: MeasurementLine):
        """Update DPI based on measurement line"""
        px_length = ((line.end_point.x - line.start_point.x)**2 + 
                     (line.end_point.y - line.start_point.y)**2)**0.5
        
        if line.value_mm > 0:
            calculated_dpmm = px_length / line.value_mm
            self.true_dpi = int(round(calculated_dpmm * 25.4))
            self.dpmm = calculated_dpmm
            self.measurement_line = line
            self.is_calibrated = True
            logger.info(f"Calibrated DPI: {self.true_dpi} DPI ({self.dpmm:.2f} px/mm)")
            return True
        return False


# ============================================================================
# DETECTION STAGE 1: ROUGH PART IDENTIFICATION
# ============================================================================

PROMPT_ROUGH_DETECTION = """
You are analyzing a product label image to identify regions containing CLP or Non-CLP sections.

**CLP Parts** (strict compliance sections):
- Ingredients list (INCI notation)
- Hazard symbols (GHS pictograms, warning signs)
- Warnings section (mandatory precautions, first aid)
- Signal word (DANGER, WARNING, CAUTION)
- Hazard statements (H-statements)
- Precautionary statements (P-statements)

**Non-CLP Parts** (marketing/informational):
- Brand name and logo
- Marketing claims ("hypoallergenic", "natural", etc.)
- Usage instructions
- Directions for use
- Contact information
- Barcode/UPC
- Volume/weight declarations

Task: Identify ALL distinct regions on this label, even if they overlap or have irregular shapes.
For each region, provide:
1. A clear classification (CLP or NON-CLP) based on CONTENT
2. A descriptive label (e.g., "Hazard Symbols", "Ingredients List", "Usage Instructions")
3. Tight bounding box using Gemini's native box_2d format: [ymin, xmin, ymax, xmax] (normalized 0-1000)
   - Make sure the bounding box is as tight as possible around the content
   - Use 0-1000 scale (0=top/left, 1000=bottom/right)
4. Your confidence level (0.0-1.0)

Be exhaustive - we want to catch every distinct section, even small ones.
Return just box_2d, label, classification, and confidence. No additional text.
"""

ROUGH_DETECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "classification": {"type": "string", "enum": ["CLP", "NON-CLP"]},
                    "label": {"type": "string"},
                    "content_type": {"type": "string", "description": "Specific section type (e.g., 'Ingredients', 'Hazard Symbols', 'Usage Instructions')"},
                    "box_2d": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                        "description": "[ymin, xmin, ymax, xmax] normalized 0-1000"
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                },
                "required": ["classification", "label", "content_type", "box_2d", "confidence"]
            }
        }
    },
    "required": ["regions"]
}


# ============================================================================
# DETECTION STAGE 2: REFINEMENT AND POLYGON BOUNDARIES
# ============================================================================

PROMPT_BOUNDARY_REFINEMENT = """
You previously identified a region on this label as:
- Classification: {classification}
- Label: {label}
- Content Type: {content_type}
- Approximate bounding box (0-1000 normalized): [ymin={ymin}, xmin={xmin}, ymax={ymax}, xmax={xmax}]

Now, refine the boundaries by identifying the exact borders of this region.
Make the bounding box as tight as possible around the content.
If the region has an irregular shape, provide a polygon with corner points.
Otherwise, provide the precise rectangular bounds using box_2d format.

Return the refined region with tight bounds. Use box_2d=[ymin, xmin, ymax, xmax] normalized 0-1000.
"""

BOUNDARY_REFINEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "refined_box_2d": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 4,
            "maxItems": 4,
            "description": "[ymin, xmin, ymax, xmax] normalized 0-1000 for tight bounds"
        },
        "has_irregular_shape": {"type": "boolean"},
        "polygon_points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "x coordinate 0-1000"},
                    "y": {"type": "number", "description": "y coordinate 0-1000"}
                }
            }
        },
        "refinement_confidence": {"type": "number", "minimum": 0, "maximum": 1}
    }
}


# ============================================================================
# DETECTION STAGE 3: CLP COMPLIANCE VALIDATION
# ============================================================================

def get_clp_validation_prompt(true_dpi: int, dpmm: float, package_size_ml: int = 500) -> str:
    """Generate CLP validation prompt that measures (not judges) compliance.
    
    NOTE: This prompt is used with a CROPPED image of the CLP region.
    Coordinates in the cropped image are in that crop's local coordinate space.
    The DPI/DPMM values apply to this crop (inherited from original image).
    
    Args:
        true_dpi: Calibrated DPI (from original image)
        dpmm: Pixels per millimeter (from original image)
        package_size_ml: Package size in ml (determines font size threshold)
    """
    return f"""
You are MEASURING font sizes and spacing on a CLP label section.
Do NOT judge compliance. Just provide the measurements.

### **Reference Scale:**
The image resolution is {true_dpi} DPI ({dpmm:.2f} pixels per mm).

### **Task: Measure these metrics (provide numbers, not judgments):**

1. **Font size:** Find the smallest readable text in the CLP section. 
   - Measure the height of a capital letter (e.g., "H") in pixels
   - Convert to mm using: mm = pixels / {dpmm:.2f}
   - Report both: {{pixels: X, mm: Y}}

2. **Line distance:** Measure the vertical gap between two consecutive lines of text.
   - Measure from baseline of one line to baseline of next line
   - Report in pixels and mm

3. **Background color:** Describe the background of the text region
   - Is it white? Black? Another color? Gradient?
   - Describe the text color (black, white, other)
   - Is there sufficient contrast? (visual assessment only)

### **Important:**
- Be as precise as possible with pixel measurements
- If no clear measurements possible, indicate "unclear" with your reasoning
- Assume package size is {package_size_ml} ml for font size thresholds
"""

CLP_VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "font_size_pixels": {"type": "number", "description": "Measured font height in pixels"},
        "font_size_mm": {"type": "number", "description": "Calculated font size in mm"},
        "line_distance_pixels": {"type": "number", "description": "Measured baseline-to-baseline distance in pixels"},
        "line_distance_mm": {"type": "number", "description": "Calculated line distance in mm"},
        "background_color": {"type": "string", "description": "Described background color"},
        "text_color": {"type": "string", "description": "Described text color"},
        "contrast_assessment": {"type": "string", "description": "Contrast quality (high/medium/low)"},
        "measurement_confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "How confident in measurements (0-1)"},
        "notes": {"type": "string", "description": "Any ambiguities or special observations"}
    },
    "required": ["font_size_pixels", "font_size_mm", "line_distance_pixels", "line_distance_mm", "background_color", "text_color", "contrast_assessment", "measurement_confidence"]
}

def validate_measurements_against_rules(metrics: Dict, package_size_ml: int = 500, is_inner_packaging: bool = False) -> Dict:
    """Apply CLP rules to measurements. 100% deterministic.
    
    EU Regulation 1223/2009 Compliance Rules:
    - Rule 1: Font size (package-size dependent)
    - Rule 2: Line distance (120% of font size)
    - Rule 3: White background + Black text (STRICT)
    - Rule 4: Inner packaging ≤10ml exemption (font can be smaller but must be legible)
    
    Returns "SKIP" status when measurements are too uncertain to evaluate.
    Only returns "PASS" or "FAIL" when measurement_confidence >= 0.5.
    
    Args:
        metrics: Measurement dict with font_size_mm, line_distance_mm, colors, etc.
        package_size_ml: Package size in ml (affects font threshold)
        is_inner_packaging: True if this is inner packaging ≤10ml (exemption applies)
    """
    
    # Check if measurements are too uncertain to evaluate
    measurement_confidence = metrics.get("measurement_confidence", 0)
    if measurement_confidence < 0.5:
        logger.warning(f"  ⚠️  Measurement confidence too low ({measurement_confidence:.2f}), marking as SKIP")
        return {
            "rule_1_font_size": {
                "status": "SKIP",
                "detail": "Measurement confidence too low",
                "threshold_mm": 0,
                "measured_mm": metrics.get("font_size_mm", 0),
                "pass": None
            },
            "rule_2_line_distance": {
                "status": "SKIP",
                "detail": "Measurement confidence too low",
                "threshold_mm": 0,
                "measured_mm": metrics.get("line_distance_mm", 0),
                "pass": None
            },
            "rule_3_background_contrast": {
                "status": "SKIP",
                "detail": "Measurement confidence too low",
                "measured": metrics.get("contrast_assessment"),
                "pass": None
            },
            "overall_compliance": "SKIP",
            "compliance_confidence": measurement_confidence
        }
    
    # Determine font size threshold based on package size
    if package_size_ml <= 500:
        min_font_mm = 1.2
        rule_label = "≤500 ml"
    elif package_size_ml <= 3000:
        min_font_mm = 1.4
        rule_label = "500-3000 ml"
    else:
        min_font_mm = 1.8
        rule_label = ">3000 ml"
    
    font_mm = metrics.get("font_size_mm", 0)
    line_mm = metrics.get("line_distance_mm", 0)
    contrast = metrics.get("contrast_assessment", "").lower()
    
    # Rule 1: Font size
    # EU Regulation 1223/2009: Inner packaging ≤10ml can be smaller (exemption) but must remain easily legible
    if is_inner_packaging and package_size_ml <= 10:
        # Inner packaging exemption: font can be smaller, but must be measurable and legible
        # No minimum threshold, but must be legible (Gemini should report measurement_confidence)
        font_pass = font_mm > 0 and measurement_confidence >= 0.7  # Must be legible (0.7+ confidence)
        font_status = "PASS" if font_pass else "UNCLEAR"
        font_detail = f"{font_mm:.2f} mm (Inner packaging ≤10ml exemption - must remain easily legible. Legibility confidence: {measurement_confidence:.0%})"
    else:
        font_pass = font_mm >= min_font_mm
        font_status = "PASS" if font_pass else "FAIL"
        font_detail = f"{font_mm:.2f} mm ({rule_label} requires ≥{min_font_mm} mm)"
    
    # Rule 2: Line distance (≥120% of font size)
    min_line_mm = font_mm * 1.2
    line_pass = line_mm >= min_line_mm if line_mm > 0 else None
    if line_pass is None:
        line_status = "UNCLEAR"
        line_detail = "Line distance not measurable"
    else:
        line_status = "PASS" if line_pass else "FAIL"
        line_detail = f"{line_mm:.2f} mm (requires ≥{min_line_mm:.2f} mm = 120% of {font_mm:.2f} mm)"
    
    # Rule 3: Background & Text Color (STRICT: White background with Black text ONLY)
    # EU Regulation 1223/2009: CLP text MUST be white background with black letters
    bg_color = metrics.get('background_color', '').lower()
    text_color = metrics.get('text_color', '').lower()
    
    # Check for white background with black text
    is_white_bg = any(word in bg_color for word in ['white', 'off-white', 'ivory'])
    is_black_text = any(word in text_color for word in ['black', 'dark', 'dark gray', 'grey'])
    
    contrast_pass = is_white_bg and is_black_text
    contrast_status = "PASS" if contrast_pass else "FAIL"
    contrast_detail = f"Requirement: White background + Black text. Found: {metrics.get('background_color', 'unknown')} bg, {metrics.get('text_color', 'unknown')} text"
    
    overall_pass = font_pass and (line_pass if line_pass is not None else True) and contrast_pass
    
    return {
        "rule_1_font_size": {
            "status": font_status,
            "detail": font_detail,
            "threshold_mm": min_font_mm,
            "measured_mm": font_mm,
            "pass": font_pass
        },
        "rule_2_line_distance": {
            "status": line_status,
            "detail": line_detail,
            "threshold_mm": min_line_mm,
            "measured_mm": line_mm,
            "pass": line_pass
        },
        "rule_3_background_contrast": {
            "status": contrast_status,
            "detail": contrast_detail,
            "measured": metrics.get("contrast_assessment"),
            "pass": contrast_pass
        },
        "overall_compliance": "PASS" if overall_pass else "FAIL",
        "compliance_confidence": measurement_confidence
    }


# ============================================================================
# CUSTOM EXCEPTIONS
# ============================================================================

class LabelAnalyzerError(Exception):
    """Base exception for label analyzer errors."""


class APIError(LabelAnalyzerError):
    """Raised when an API call fails after all retries."""

    def __init__(self, message: str, attempts: int = 0, last_exception: Optional[Exception] = None):
        self.attempts = attempts
        self.last_exception = last_exception
        super().__init__(message)


class CalibrationError(LabelAnalyzerError):
    """Raised when DPI calibration encounters an unrecoverable error."""


class DetectionError(LabelAnalyzerError):
    """Raised when region detection fails completely."""


# ============================================================================
# RETRY WITH EXPONENTIAL BACKOFF
# ============================================================================

def retry_with_backoff(
    fn: Callable[..., T],
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retryable_exceptions: Tuple = (Exception,),
    **kwargs,
) -> T:
    """Execute *fn* with exponential backoff on transient failures.

    Args:
        fn: Callable to execute.
        max_retries: Maximum number of retry attempts (0 = no retries).
        base_delay: Initial delay in seconds before the first retry.
        max_delay: Cap on the delay between retries.
        jitter: Add random jitter (±25 %) to prevent thundering herd.
        retryable_exceptions: Tuple of exception types that trigger a retry.

    Returns:
        The return value of *fn*.

    Raises:
        APIError: If all retry attempts are exhausted.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_retries + 2):  # attempt 1 = first try
        try:
            return fn(*args, **kwargs)
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt == max_retries + 1:
                break  # exhausted
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            if jitter:
                delay *= 1.0 + random.uniform(-0.25, 0.25)
            logger.warning(
                f"Attempt {attempt}/{max_retries + 1} failed: {exc!r} — "
                f"retrying in {delay:.1f}s"
            )
            time.sleep(delay)

    raise APIError(
        f"All {max_retries + 1} attempts failed. Last error: {last_exc!r}",
        attempts=max_retries + 1,
        last_exception=last_exc,
    )


# ============================================================================
# RESPONSE CACHE
# ============================================================================

class ResponseCache:
    """Disk-backed cache for API responses keyed by image content + prompt hash.

    Eliminates redundant Gemini calls when re-analyzing the same image or when
    the pipeline retries after a partial failure.  Cache entries are JSON files
    stored under *cache_dir* with a configurable TTL (default 7 days).

    The cache key is SHA-256(image_data_bytes + prompt + schema_json).
    """

    def __init__(self, cache_dir: Optional[str] = None, ttl_seconds: int = 7 * 86400):
        self.cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".cache" / "label_analyzer"
        self.ttl_seconds = ttl_seconds
        self._enabled = True
        self.hits = 0
        self.misses = 0

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    # ------------------------------------------------------------------

    @staticmethod
    def _make_key(image_data: Dict, prompt: str, schema: Optional[Dict] = None) -> str:
        """Deterministic SHA-256 key from image bytes + prompt + schema."""
        h = hashlib.sha256()
        # Hash the base64 image data (stable for same image)
        inline = image_data.get("inline_data", {})
        h.update(inline.get("data", "").encode("utf-8"))
        h.update(prompt.encode("utf-8"))
        if schema:
            h.update(json.dumps(schema, sort_keys=True).encode("utf-8"))
        return h.hexdigest()

    def _path_for(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    # ------------------------------------------------------------------

    def get(self, image_data: Dict, prompt: str, schema: Optional[Dict] = None) -> Optional[str]:
        """Return cached response text, or None on miss / expired / disabled."""
        if not self._enabled:
            return None
        key = self._make_key(image_data, prompt, schema)
        path = self._path_for(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            entry = json.loads(path.read_text())
            if time.time() - entry.get("ts", 0) > self.ttl_seconds:
                path.unlink(missing_ok=True)
                self.misses += 1
                return None
            self.hits += 1
            logger.debug(f"Cache HIT ({key[:12]}…)")
            return entry["response"]
        except Exception:
            self.misses += 1
            return None

    def put(self, image_data: Dict, prompt: str, response_text: str,
            schema: Optional[Dict] = None) -> None:
        """Store a response in the cache."""
        if not self._enabled:
            return
        key = self._make_key(image_data, prompt, schema)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "response": response_text}
        self._path_for(key).write_text(json.dumps(entry))

    def clear(self) -> int:
        """Remove all cached entries. Returns count of files removed."""
        if not self.cache_dir.exists():
            return 0
        removed = 0
        for f in self.cache_dir.glob("*.json"):
            f.unlink()
            removed += 1
        logger.info(f"Cache cleared: {removed} entries removed")
        return removed

    def stats(self) -> Dict:
        """Return hit/miss statistics."""
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hits / total if total else 0.0,
        }


# ============================================================================
# PACKAGE SIZE DETECTION
# ============================================================================

def detect_package_size(image_data: Dict, gemini_client: 'GeminiClient', image_width: int, image_height: int) -> Tuple[int, float]:
    """Detect package/container size from label image using Gemini vision.
    
    Looks for volume declarations (ml, L, oz, g, kg, etc.) anywhere on the label.
    
    Args:
        image_data: Base64 image data in Gemini format
        gemini_client: GeminiClient instance for API calls
        image_width: Image width in pixels
        image_height: Image height in pixels
        
    Returns:
        Tuple of (package_size_ml, confidence)
        - package_size_ml: Detected size in milliliters (defaults to 500ml if not found)
        - confidence: Confidence in detection (0.0-1.0)
    """
    prompt = """
Scan this product label and identify the package/container size/volume.
Look for any explicit volume declarations like:
- "Volume: 250ml"
- "500mL"
- "1L" (convert to ml: 1000ml)
- "250g" (for non-liquid, use weight)
- "16 fl oz" (convert: ~473ml)
- "Net: 500ml"

Extract the PRIMARY volume/size declaration (usually near barcode or top of label).

Return ONLY:
- value: The numeric size value
- unit: The unit (ml, L, g, oz, fl oz, etc.)
- value_in_ml: The value converted to milliliters (1L=1000ml, 1oz≈29.57ml, 1fl oz≈29.57ml)
- confidence: How confident you are (0.0-1.0)
- location: Brief description of where on label (e.g., "bottom right", "near barcode")

If you cannot find a size declaration, return value_in_ml=null with low confidence.
"""
    
    schema = {
        "type": "object",
        "properties": {
            "value": {"type": ["number", "null"]},
            "unit": {"type": ["string", "null"]},
            "value_in_ml": {"type": ["number", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "location": {"type": ["string", "null"]},
            "notes": {"type": "string"}
        },
        "required": ["value", "unit", "value_in_ml", "confidence"]
    }
    
    try:
        logger.info("Stage 0b: Detecting package size from label...")
        response_text = gemini_client.analyze_image(
            image_data,
            prompt,
            schema,
            original_width=image_width,
            original_height=image_height,
            temperature=0.3  # Very low temp for deterministic size detection
        )
        
        result = json.loads(response_text)
        value_ml = result.get("value_in_ml")
        confidence = result.get("confidence", 0.0)
        
        if value_ml and confidence >= 0.5:
            logger.info(f"  ✓ Detected package size: {result.get('value')} {result.get('unit')} = {int(value_ml)}ml (confidence: {confidence:.0%})")
            return int(value_ml), confidence
        else:
            logger.info(f"  ⚠️  Could not reliably detect package size (confidence: {confidence:.0%}), using default 500ml")
            return 500, 0.0  # Default fallback
            
    except Exception as e:
        logger.warning(f"Package size detection failed: {e}, using default 500ml")
        return 500, 0.0


# ============================================================================
# BBOX HELPER FUNCTIONS
# ============================================================================

def denormalize_box_2d(box_2d: List[float], image_width: int, image_height: int) -> Dict[str, int]:
    """Convert Gemini's native box_2d format to pixel coordinates.
    
    Gemini returns: [ymin, xmin, ymax, xmax] normalized 0-1000
    We convert to: {xmin, ymin, xmax, ymax} in pixel space
    
    Args:
        box_2d: [ymin, xmin, ymax, xmax] in 0-1000 range
        image_width: Original image width in pixels
        image_height: Original image height in pixels
        
    Returns:
        Dict with xmin, ymin, xmax, ymax in pixel coordinates
    """
    ymin_norm, xmin_norm, ymax_norm, xmax_norm = box_2d
    
    # Convert from 0-1000 to 0-1 then scale to image dimensions
    xmin_px = int(round(xmin_norm / 1000 * image_width))
    ymin_px = int(round(ymin_norm / 1000 * image_height))
    xmax_px = int(round(xmax_norm / 1000 * image_width))
    ymax_px = int(round(ymax_norm / 1000 * image_height))
    
    # Ensure valid bounds
    xmin_px = max(0, min(xmin_px, image_width))
    ymin_px = max(0, min(ymin_px, image_height))
    xmax_px = max(0, min(xmax_px, image_width))
    ymax_px = max(0, min(ymax_px, image_height))
    
    # Swap if needed
    if xmin_px > xmax_px:
        xmin_px, xmax_px = xmax_px, xmin_px
    if ymin_px > ymax_px:
        ymin_px, ymax_px = ymax_px, ymin_px
    
    return {
        "xmin": xmin_px,
        "ymin": ymin_px,
        "xmax": xmax_px,
        "ymax": ymax_px
    }

def normalize_rect_to_box_2d(rect: Dict[str, int], image_width: int, image_height: int) -> List[float]:
    """Convert pixel rectangle to Gemini's native box_2d format.
    
    Input: {xmin, ymin, xmax, ymax} in pixel coordinates
    Output: [ymin, xmin, ymax, xmax] normalized 0-1000 (for prompts)
    
    Args:
        rect: Dict with xmin, ymin, xmax, ymax in pixels
        image_width: Original image width in pixels
        image_height: Original image height in pixels
        
    Returns:
        List [ymin, xmin, ymax, xmax] in 0-1000 range
    """
    xmin, ymin, xmax, ymax = rect["xmin"], rect["ymin"], rect["xmax"], rect["ymax"]
    
    # Scale to 0-1000 range
    ymin_norm = int(round(ymin / image_height * 1000)) if image_height > 0 else 0
    xmin_norm = int(round(xmin / image_width * 1000)) if image_width > 0 else 0
    ymax_norm = int(round(ymax / image_height * 1000)) if image_height > 0 else 1000
    xmax_norm = int(round(xmax / image_width * 1000)) if image_width > 0 else 1000
    
    return [ymin_norm, xmin_norm, ymax_norm, xmax_norm]

# ============================================================================
# GEMINI CLIENT WRAPPER
# ============================================================================

class GeminiClient:
    """Wrapper for Gemini API interactions"""
    
    # Exceptions that are safe to retry (transient / rate-limit).
    RETRYABLE_EXCEPTIONS: Tuple = ()  # populated lazily after import
    
    # Maximum dimension (width or height) Gemini accepts before it
    # internally resizes the image (maintaining aspect ratio).
    # Gemini returns pixel coordinates in this resized space.
    # Use _calculate_scale_factor() to convert back to original coordinates.
    GEMINI_MAX_DIMENSION = 1440  # pixels

    def __init__(self, project_id: str, model: str = "gemini-3-pro-preview", location: str = "global",
                 cache: Optional[ResponseCache] = None, max_retries: int = 3):
        self.project_id = project_id
        self.model = model
        self.location = location
        self._client = None
        self.cache = cache or ResponseCache()
        self.max_retries = max_retries
        self._last_image_width: int = 0   # Track original image dimensions for coordinate denormalization
        self._last_image_height: int = 0
    
    def _get_client(self):
        """Lazy-load Gemini client"""
        if self._client is None:
            try:
                from google import genai
                self._client = genai.Client(vertexai=True, project=self.project_id, location=self.location)
            except ImportError:
                logger.error("google-genai library not installed. Install with: pip install google-genai")
                raise
        return self._client
    
    def _calculate_scale_factor(self, original_width: int, original_height: int) -> float:
        """Calculate scale factor for Gemini's internal resizing.
        
        Gemini resizes images that exceed GEMINI_MAX_DIMENSION in either dimension,
        maintaining aspect ratio. Coordinates returned are in Gemini's resized space,
        so we need to scale them back to the original.
        
        Returns:
            scale_factor: multiply Gemini coordinates by this to get original coords
        """
        max_dim = max(original_width, original_height)
        if max_dim <= self.GEMINI_MAX_DIMENSION:
            return 1.0  # No resize happened
        
        scale = max_dim / self.GEMINI_MAX_DIMENSION
        logger.debug(f"  🔍 Gemini resize detected: {max_dim}px → {self.GEMINI_MAX_DIMENSION}px, scale factor: {scale:.4f}")
        return scale
    
    def analyze_image(self, image_data: Dict, prompt: str, response_schema: Optional[Dict] = None, 
                      original_width: Optional[int] = None, original_height: Optional[int] = None,
                      temperature: float = 0.7) -> str:
        """Call Gemini with image and optional structured output.

        Results are cached by (image_data, prompt, schema) so repeated
        calls for the same input return instantly without an API round-trip.
        
        Args:
            image_data: Image in Gemini format (inline_data dict)
            prompt: Prompt text
            response_schema: Optional JSON schema for structured output
            original_width: Original image width (before any Gemini resizing)
            original_height: Original image height (before any Gemini resizing)
            temperature: Model temperature (0.0=deterministic, 1.0=creative). Default 0.7.
                Use 0.0-0.5 for precise coordinate detection, 0.7+ for exploratory tasks.
        """
        import time as time_module
        call_start = time_module.time()
        
        # CRITICAL: Calculate scale factor FIRST, before cache check
        # This ensures _last_image_scale_factor is always set, even on cache hits
        if original_width and original_height:
            self._last_image_scale_factor = self._calculate_scale_factor(original_width, original_height)
        else:
            self._last_image_scale_factor = 1.0
        
        # Check cache AFTER scale factor is calculated
        cached = self.cache.get(image_data, prompt, response_schema)
        if cached is not None:
            cache_hit_time = time_module.time() - call_start
            logger.info(f"  ⚡ Cache HIT ({cache_hit_time:.2f}s) - scale factor: {self._last_image_scale_factor:.4f}")
            return cached

        client = self._get_client()

        config = {}
        if response_schema:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = response_schema
        
        # Set temperature for deterministic results (lower = more precise)
        config["temperature"] = temperature

        # Log image size and prompt length
        inline = image_data.get("inline_data", {})
        img_bytes = len(inline.get("data", "")) * 3 / 4  # base64 decode size estimate
        logger.debug(f"  📤 Sending to Gemini: image={img_bytes/1e6:.1f}MB, prompt={len(prompt)} chars, schema={'yes' if response_schema else 'no'}, temp={temperature}")

        def _call() -> str:
            api_call_start = time_module.time()
            logger.info(f"  🔄 Calling Gemini API (temp={temperature})...")
            response = client.models.generate_content(
                model=self.model,
                contents=[prompt, image_data],
                config=config if config else None
            )
            api_time = time_module.time() - api_call_start
            response_len = len(response.text)
            logger.info(f"  ✓ Gemini response received ({api_time:.1f}s, {response_len} chars)")
            logger.debug(f"    Response preview: {response.text[:200]}")
            return response.text

        # Build retryable set dynamically so imports are optional.
        retryable: list = [ConnectionError, TimeoutError, OSError]
        try:
            from google.api_core.exceptions import (
                ServiceUnavailable, TooManyRequests, DeadlineExceeded,
                InternalServerError,
            )
            retryable.extend([ServiceUnavailable, TooManyRequests,
                              DeadlineExceeded, InternalServerError])
        except ImportError:
            pass

        try:
            text = retry_with_backoff(
                _call,
                max_retries=self.max_retries,
                base_delay=2.0,
                max_delay=60.0,
                retryable_exceptions=tuple(retryable),
            )
            self.cache.put(image_data, prompt, text, response_schema)
            total_time = time_module.time() - call_start
            logger.info(f"  ⏱️  Total call time: {total_time:.1f}s")
            return text
        except APIError:
            raise
        except Exception as e:
            logger.error(f"Gemini API error (non-retryable): {e}")
            raise APIError(f"Non-retryable API error: {e}", attempts=1, last_exception=e)


# ============================================================================
# LABEL ANALYZER (MAIN CLASS)
# ============================================================================

class LabelAnalyzer:
    """Production-ready label analyzer with multi-stage detection"""
    
    def __init__(self, project_id: str, dpi: int = 300, cache_dir: Optional[str] = None,
                 confidence_weights: Optional[Dict[str, float]] = None, use_cache: bool = True,
                 package_size_ml: Optional[int] = None):
        cache = ResponseCache(cache_dir=cache_dir) if use_cache else ResponseCache(cache_dir=cache_dir)
        if not use_cache:
            cache.disable()
        self.gemini = GeminiClient(project_id, cache=cache)
        self.original_dpi = dpi
        self.calibration = CalibrationResult(dpi)
        self.detected_parts: List[DetectedPart] = []
        self.ensemble_scorer = EnsembleConfidence(weights=confidence_weights)
        self._image_size: Tuple[int, int] = (0, 0)  # (width, height)
        self._gemini_scale_factor: float = 1.0  # Track coordinate scaling
        self.package_size_ml: int = package_size_ml or 500  # Default fallback
        self.package_size_confidence: float = 0.0  # Confidence in detected size
    
    def clear_cache(self) -> int:
        """Clear all cached API responses. Use when analyzing new images.
        
        Returns:
            Number of cache entries removed
        """
        return self.gemini.cache.clear()
    
    def _scale_region_coordinates(self, region: Dict, scale_factor: float) -> Dict:
        """Scale region coordinates back to original image space.
        
        Args:
            region: Region dict with 'rect' and optional 'polygon_points'
            scale_factor: Multiply coordinates by this to get original space
            
        Returns:
            Region dict with scaled coordinates
        """
        # Check if already scaled to prevent double-scaling
        if region.get("has_been_scaled"):
            logger.debug(f"  ⏩ Skipping scale for '{region.get('label', '?')}' - already scaled")
            return region
        
        if scale_factor == 1.0:
            region["has_been_scaled"] = True
            return region  # No scaling needed
        
        logger.debug(f"  🔄 Scaling region '{region.get('label', '?')}' by {scale_factor:.4f}")
        
        # Scale rectangle
        rect = region.get("rect", {})
        if rect:
            region["rect"] = {
                "xmin": int(round(rect["xmin"] * scale_factor)),
                "ymin": int(round(rect["ymin"] * scale_factor)),
                "xmax": int(round(rect["xmax"] * scale_factor)),
                "ymax": int(round(rect["ymax"] * scale_factor))
            }
        
        # Scale polygon points if present
        if region.get("polygon_points"):
            region["polygon_points"] = [
                {
                    "x": int(round(p["x"] * scale_factor)),
                    "y": int(round(p["y"] * scale_factor))
                }
                for p in region["polygon_points"]
            ]
        
        # Mark as scaled to prevent re-scaling in later stages
        region["has_been_scaled"] = True
        
        return region
    
    # ========================================================================
    # STAGE 0: CALIBRATION
    # ========================================================================
    
    def calibrate_dpi(self, image: PIL_Image.Image, image_data: Dict) -> bool:
        """
        Attempt to calibrate DPI from measurement lines in the image.
        Updates self.calibration.
        
        Returns:
            bool: True if calibration succeeded, False if using default DPI
        """
        logger.info("Stage 0: DPI Calibration")
        
        prompt = """
Identify a technical measurement line or ruler on this image that is labeled in 'mm'.
The ruler should have clear pixel coordinates for start and end points, and a labeled numeric value in millimeters.

Return the following:
- start_point: {x, y} pixel coordinates of the line start
- end_point: {x, y} pixel coordinates of the line end  
- value_mm: The numeric value in mm labeled on the ruler
- confidence: How confident you are (0.0 to 1.0)

If no measurement line is found, set measurement_line to null.
"""
        
        calibration_schema = {
            "type": "object",
            "properties": {
                "measurement_line": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "start_point": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "integer"},
                                        "y": {"type": "integer"}
                                    },
                                    "required": ["x", "y"]
                                },
                                "end_point": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "integer"},
                                        "y": {"type": "integer"}
                                    },
                                    "required": ["x", "y"]
                                },
                                "value_mm": {"type": "number"},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                            },
                            "required": ["start_point", "end_point", "value_mm", "confidence"]
                        },
                        {"type": "null"}
                    ]
                }
            },
            "required": ["measurement_line"]
        }
        
        try:
            logger.debug(f"  📋 Calibration prompt length: {len(prompt)} chars")
            # Pass original dimensions for scale factor tracking
            img_w, img_h = self._image_size
            response_text = self.gemini.analyze_image(
                image_data, prompt, calibration_schema,
                original_width=img_w, original_height=img_h
            )
            
            logger.debug(f"  📥 Raw response: {response_text[:500]}")  # First 500 chars
            response = json.loads(response_text)
            logger.debug(f"  ✓ Parsed JSON successfully")
            
            if response.get("measurement_line"):
                line_data = response["measurement_line"]
                logger.info(f"  🎯 Found measurement line (pre-scale): {line_data}")
                
                # CRITICAL: Scale coordinates back to original image space
                # Gemini returns coordinates in its internally-resized space
                scale_factor = self.gemini._last_image_scale_factor
                if scale_factor != 1.0:
                    logger.info(f"  🔄 Scaling calibration coordinates by {scale_factor:.4f}")
                    line_data["start_point"]["x"] = int(round(line_data["start_point"]["x"] * scale_factor))
                    line_data["start_point"]["y"] = int(round(line_data["start_point"]["y"] * scale_factor))
                    line_data["end_point"]["x"] = int(round(line_data["end_point"]["x"] * scale_factor))
                    line_data["end_point"]["y"] = int(round(line_data["end_point"]["y"] * scale_factor))
                    logger.info(f"  ✓ Scaled measurement line: {line_data}")
                
                line = MeasurementLine(
                    start_point=Point(**line_data["start_point"]),
                    end_point=Point(**line_data["end_point"]),
                    value_mm=line_data["value_mm"],
                    confidence=line_data.get("confidence", 0.8)
                )
                
                if self.calibration.update(line):
                    logger.info(f"✓ Calibration successful: {self.calibration.true_dpi} DPI")
                    return True
            
            logger.info(f"✗ No measurement line found (response: {response}), using default {self.original_dpi} DPI")
            return False
            
        except json.JSONDecodeError as e:
            logger.warning(f"Calibration failed (JSON parse): {e}")
            logger.warning(f"  Raw response was: {response_text[:200]}")
            logger.warning(f"  Using default DPI: {self.original_dpi}")
            return False
        except Exception as e:
            logger.warning(f"Calibration failed: {type(e).__name__}: {e}, using default DPI")
            return False
    
    # ========================================================================
    # STAGE 1: ROUGH PART DETECTION
    # ========================================================================
    
    def detect_parts_rough(self, image_data: Dict) -> List[Dict]:
        """
        Stage 1: Identify all rough regions (CLP vs Non-CLP)
        
        Parses Gemini's native box_2d format [ymin, xmin, ymax, xmax] (0-1000 normalized)
        and denormalizes to pixel coordinates in original image space.
        
        Returns:
            List of detected regions with classifications (coordinates in original space)
        """
        import time as time_module
        stage_start = time_module.time()
        logger.info("Stage 1: Rough Part Detection")
        
        try:
            # Pass original dimensions so Gemini can calculate scale factor
            img_w, img_h = self._image_size
            
            # Call Gemini with low temperature for deterministic results
            response_text = self.gemini.analyze_image(
                image_data, 
                PROMPT_ROUGH_DETECTION,
                ROUGH_DETECTION_SCHEMA,
                original_width=img_w,
                original_height=img_h,
                temperature=0.5  # Lower temp for more precise coordinates
            )
            response = json.loads(response_text)
            
            regions = response.get("regions", [])
            
            # Denormalize box_2d format [ymin, xmin, ymax, xmax] to pixel coordinates
            logger.info(f"  📐 Denormalizing {len(regions)} box_2d bounding boxes...")
            for region in regions:
                if "box_2d" in region:
                    box_2d = region.pop("box_2d")
                    # Gemini coordinates are in its resized space if scaling occurred
                    # We need to scale them back to original image space BEFORE denormalizing
                    scale_factor = self.gemini._last_image_scale_factor
                    
                    # Denormalize to Gemini's coordinate space first
                    gemini_width = int(img_w / scale_factor) if scale_factor > 1.0 else img_w
                    gemini_height = int(img_h / scale_factor) if scale_factor > 1.0 else img_h
                    rect = denormalize_box_2d(box_2d, gemini_width, gemini_height)
                    
                    # Then scale back to original image space if needed
                    if scale_factor != 1.0:
                        rect = {
                            "xmin": int(round(rect["xmin"] * scale_factor)),
                            "ymin": int(round(rect["ymin"] * scale_factor)),
                            "xmax": int(round(rect["xmax"] * scale_factor)),
                            "ymax": int(round(rect["ymax"] * scale_factor))
                        }
                    
                    region["rect"] = rect
                    logger.debug(f"    box_2d={box_2d} → pixel rect=({rect['xmin']},{rect['ymin']})-({rect['xmax']},{rect['ymax']})")
            
            self._gemini_scale_factor = self.gemini._last_image_scale_factor
            
            elapsed = time_module.time() - stage_start
            logger.info(f"✓ Detected {len(regions)} regions in {elapsed:.1f}s")
            
            for i, region in enumerate(regions):
                rect = region.get('rect', {})
                if rect:
                    logger.info(f"  Region {i}: {region['classification']:8s} - {region['label']:25s} @ ({rect['xmin']},{rect['ymin']})-({rect['xmax']},{rect['ymax']}) conf={region.get('confidence', 0):.2f}")
                else:
                    logger.info(f"  Region {i}: {region['classification']:8s} - {region['label']:25s} (no rect) conf={region.get('confidence', 0):.2f}")
            
            return regions
            
        except Exception as e:
            logger.error(f"Rough detection failed: {e}")
            return []
    
    # ========================================================================
    # STAGE 2: BOUNDARY REFINEMENT
    # ========================================================================
    
    def refine_boundaries(self, image_data: Dict, region: Dict) -> Dict:
        """
        Stage 2: Refine boundaries and detect irregular shapes
        
        Parses refined_box_2d format and denormalizes to pixel coordinates.
        
        Returns:
            Refined region with better boundaries (coordinates in original space)
        """
        classification = region["classification"]
        label = region["label"]
        content_type = region.get("content_type", "Unknown")
        rect = region["rect"]
        
        # Convert pixel rect to 0-1000 normalized coords for prompt
        img_w, img_h = self._image_size
        box_2d = normalize_rect_to_box_2d(rect, img_w, img_h)
        ymin_norm, xmin_norm, ymax_norm, xmax_norm = box_2d
        
        prompt = PROMPT_BOUNDARY_REFINEMENT.format(
            classification=classification,
            label=label,
            content_type=content_type,
            xmin=xmin_norm,
            ymin=ymin_norm,
            xmax=xmax_norm,
            ymax=ymax_norm
        )
        
        try:
            response_text = self.gemini.analyze_image(
                image_data,
                prompt,
                BOUNDARY_REFINEMENT_SCHEMA,
                original_width=img_w,
                original_height=img_h,
                temperature=0.5  # Low temp for precise refinement
            )
            response = json.loads(response_text)
            
            # Parse refined_box_2d if provided
            refined_box_2d = response.get("refined_box_2d")
            if refined_box_2d:
                logger.debug(f"  📐 Refined box_2d: {refined_box_2d}")
                new_rect = denormalize_box_2d(refined_box_2d, img_w, img_h)
            else:
                new_rect = None
            
            got_new_rect = new_rect is not None
            
            # Build refined dict WITHOUT **region spread to avoid copying flags
            refined = {
                "classification": region["classification"],
                "label": region["label"],
                "content_type": region.get("content_type", "Unknown"),
                "confidence": region.get("confidence", 0.5),
                "rect": new_rect if got_new_rect else rect,
                "has_irregular_shape": response.get("has_irregular_shape", False),
                "polygon_points": None,  # Will denormalize below if present
                "refinement_confidence": response.get("refinement_confidence", 0.8)
            }
            
            # CRITICAL: Denormalize polygon points from 0-1000 normalized to pixel coordinates
            # (Stage 2 returns polygon_points in normalized 0-1000 space per schema)
            if response.get("polygon_points"):
                refined["polygon_points"] = [
                    {
                        "x": int(round(p["x"] / 1000 * img_w)),
                        "y": int(round(p["y"] / 1000 * img_h))
                    }
                    for p in response["polygon_points"]
                ]
                logger.debug(f"  📐 Denormalized {len(refined['polygon_points'])} polygon points for '{label}'")
            
            out_rect = refined.get('rect', {})
            if out_rect:
                logger.info(f"  ✓ Refined: {label} ({out_rect['xmin']},{out_rect['ymin']})-({out_rect['xmax']},{out_rect['ymax']}) irregular={refined['has_irregular_shape']}")
            else:
                logger.info(f"  ✓ Refined: {label} (no rect) irregular={refined['has_irregular_shape']}")
            return refined
            
        except Exception as e:
            logger.warning(f"Boundary refinement failed for '{label}': {e}, using rough boundaries")
            return region
    
    # ========================================================================
    # STAGE 3: CLP COMPLIANCE VALIDATION
    # ========================================================================
    
    def validate_clp_compliance(self, image_data: Dict, region: Dict, cropped_image: PIL_Image.Image, package_size_ml: int = 500, is_inner_packaging: bool = False) -> Dict:
        """
        Stage 3: Two-layer CLP compliance validation
        
        Implements EU Regulation 1223/2009 CLP rules:
        - Rule 1: Font size (1.2/1.4/1.8mm based on package size)
        - Rule 2: Line distance (≥120% of font)
        - Rule 3: White background + Black text (STRICT)
        - Rule 4: Inner packaging ≤10ml exemption (can be smaller if legible)
        
        Layer 1: Gemini MEASURES (font size, line distance, contrast)
        Layer 2: Local rules apply deterministic checks
        
        Only applies to CLP-classified regions.
        Returns: {measurements, rule_results, overall_compliance}
        """
        import time as time_module
        if region["classification"] != "CLP":
            return {}  # Non-CLP regions don't need validation
        
        region_label = region['label']
        val_start = time_module.time()
        logger.info(f"Stage 3: Measuring CLP metrics for '{region_label}'")
        
        try:
            # LAYER 1: GEMINI MEASURES (not judges)
            # IMPORTANT: Use ORIGINAL calibration DPI, not cropped image DPI
            # The cropped image inherits the same pixel density as the original
            # No need to recalibrate - DPI is a property of the PDF/scan resolution
            validation_prompt = get_clp_validation_prompt(
                self.calibration.true_dpi,
                self.calibration.dpmm,
                package_size_ml
            )
            
            # Convert cropped region to base64
            encode_start = time_module.time()
            buffered = BytesIO()
            cropped_image.save(buffered, format="JPEG")
            cropped_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
            encode_time = time_module.time() - encode_start
            logger.debug(f"  📸 Image encoding took {encode_time:.2f}s ({len(cropped_b64)/1e6:.1f}MB base64)")
            
            # Correct Gemini API format: inline_data wrapper
            cropped_data = {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": cropped_b64
                }
            }
            
            # Pass cropped image dimensions for proper scale factor tracking
            # Even though cropped regions typically won't need resizing, pass dimensions for consistency
            cropped_w, cropped_h = cropped_image.size
            response_text = self.gemini.analyze_image(
                cropped_data,
                validation_prompt,
                CLP_VALIDATION_SCHEMA,
                original_width=cropped_w,
                original_height=cropped_h
            )
            
            measurements = json.loads(response_text)
            
            # Safely extract measurements with numeric coercion
            try:
                font_mm = float(measurements.get('font_size_mm') or 0)
            except (ValueError, TypeError):
                font_mm = 0
            
            try:
                font_px = float(measurements.get('font_size_pixels') or 0)
            except (ValueError, TypeError):
                font_px = 0
                
            try:
                line_dist_mm = float(measurements.get('line_distance_mm') or 0)
            except (ValueError, TypeError):
                line_dist_mm = 0
                
            try:
                line_dist_px = float(measurements.get('line_distance_pixels') or 0)
            except (ValueError, TypeError):
                line_dist_px = 0
                
            try:
                meas_conf = float(measurements.get('measurement_confidence') or 0)
            except (ValueError, TypeError):
                meas_conf = 0
            
            # DEBUG: Log all values for troubleshooting
            logger.info(f"  ✓ Gemini measurements (TRUSTING Gemini's mm calculations):")
            logger.info(f"    [DEBUG] DPI calibration: true_dpi={self.calibration.true_dpi}, dpmm={self.calibration.dpmm:.4f}, is_calibrated={self.calibration.is_calibrated}")
            logger.info(f"    [DEBUG] Cropped image size: {cropped_w}×{cropped_h}px")
            logger.info(f"    [DEBUG] Gemini returned: {font_px:.1f}px → {font_mm:.4f}mm, {line_dist_px:.1f}px → {line_dist_mm:.4f}mm")
            
            logger.info(f"  ✓ Gemini measurements:")
            logger.info(f"    Font: {font_mm:.2f} mm ({font_px:.0f} px)")
            logger.info(f"    Line distance: {line_dist_mm:.2f} mm ({line_dist_px:.0f} px)")
            logger.info(f"    Background: {measurements.get('background_color', 'unknown')} text")
            logger.info(f"    Contrast: {measurements.get('contrast_assessment', 'unknown')}")
            logger.info(f"    Confidence: {meas_conf:.0%}")
            if measurements.get('notes'):
                logger.info(f"    Notes: {measurements.get('notes')}")
            
            # Check if region is genuinely unreadable (all measurements are near-zero)
            is_unreadable = (font_mm < 0.1 and line_dist_mm < 0.1) or meas_conf < 0.1
            
            if is_unreadable:
                logger.warning(f"  ⚠️  Unreadable region '{region_label}' - measurements indicate no readable text (font={font_mm:.2f}mm, confidence={meas_conf:.0%})")
                return {
                    "measurements": measurements,
                    "rule_results": {
                        "rule_1_font_size": {"status": "SKIP", "detail": "Region unreadable - no measurable text", "pass": None},
                        "rule_2_line_distance": {"status": "SKIP", "detail": "Region unreadable - no measurable text", "pass": None},
                        "rule_3_background_contrast": {"status": "SKIP", "detail": "Region unreadable - no measurable text", "pass": None}
                    },
                    "overall_compliance": "SKIP",
                    "compliance_confidence": meas_conf,
                    "measurement_confidence": meas_conf
                }
            
            # LAYER 2: LOCAL DETERMINISTIC RULE CHECKS (100% reproducible)
            rule_results = validate_measurements_against_rules(measurements, package_size_ml, is_inner_packaging=is_inner_packaging)
            
            # Log rule application for audit trail
            logger.info(f"  ✓ Rule validation:")
            logger.info(f"    Rule 1 (Font size): {rule_results['rule_1_font_size']['status']} - {rule_results['rule_1_font_size']['detail']}")
            logger.info(f"    Rule 2 (Line distance): {rule_results['rule_2_line_distance']['status']} - {rule_results['rule_2_line_distance']['detail']}")
            logger.info(f"    Rule 3 (Contrast): {rule_results['rule_3_background_contrast']['status']} - {rule_results['rule_3_background_contrast']['detail']}")
            logger.info(f"    Overall: {rule_results['overall_compliance']}")
            
            val_time = time_module.time() - val_start
            logger.info(f"  ✓ Stage 3 complete for '{region_label}' ({val_time:.1f}s)")
            
            return {
                "measurements": measurements,
                "rule_results": rule_results,
                "overall_compliance": rule_results["overall_compliance"],
                "compliance_confidence": measurements.get("measurement_confidence", 0),
                "measurement_confidence": meas_conf
            }
            
        except json.JSONDecodeError as e:
            val_time = time_module.time() - val_start
            logger.error(f"  ❌ JSON parse error in Stage 3 for '{region_label}' ({val_time:.1f}s): {e}")
            logger.error(f"     Response preview: {response_text[:300] if 'response_text' in locals() else 'N/A'}")
            # Return safe error structure (won't crash downstream)
            return {
                "measurements": {},
                "rule_results": {},
                "overall_compliance": "ERROR",
                "compliance_confidence": 0,
                "error": f"JSON parse failed: {str(e)}"
            }
        except Exception as e:
            val_time = time_module.time() - val_start
            logger.error(f"  ❌ CLP validation failed for '{region_label}' ({val_time:.1f}s): {type(e).__name__}: {e}")
            # Return safe error structure (won't crash downstream)
            return {
                "measurements": {},
                "rule_results": {},
                "overall_compliance": "ERROR",
                "compliance_confidence": 0,
                "error": f"Validation error: {str(e)}"
            }
    
    # ========================================================================
    # STAGE 4: CONVERT TO INTERNAL FORMAT & FILTER
    # ========================================================================
    
    def _regions_to_detected_parts(self, regions: List[Dict], rough_regions: Optional[List[Dict]] = None) -> List[DetectedPart]:
        """Convert region dicts to DetectedPart objects with ensemble scoring.

        Args:
            regions: Refined region dicts (post Stage 2).
            rough_regions: Original rough region dicts (pre Stage 2) used to
                compute refinement agreement. Matched by index.

        Returns:
            List of DetectedPart with ensemble-calibrated confidence scores.
        """
        parts = []
        img_w, img_h = self._image_size if self._image_size != (0, 0) else (1, 1)

        for i, region in enumerate(regions):
            try:
                classification = PartClassification(region["classification"])
            except ValueError:
                classification = PartClassification.UNKNOWN

            rect_dict = region["rect"]
            rect = Rectangle(
                xmin=rect_dict["xmin"],
                ymin=rect_dict["ymin"],
                xmax=rect_dict["xmax"],
                ymax=rect_dict["ymax"]
            )

            polygon = None
            if region.get("polygon_points"):
                polygon = Polygon(points=[Point(**p) for p in region["polygon_points"]])

            # --- Ensemble confidence scoring ---
            model_conf = max(
                region.get("confidence", 0.5),
                region.get("refinement_confidence", 0.5),
            )
            rough_rect = rough_regions[i]["rect"] if rough_regions and i < len(rough_regions) else None

            confidence, signals = self.ensemble_scorer.score(
                model_confidence=model_conf,
                rough_rect=rough_rect,
                refined_rect=rect_dict,
                image_width=img_w,
                image_height=img_h,
            )

            logger.debug(
                f"  Ensemble [{region.get('label', '?')}]: "
                + ", ".join(f"{s.name}={s.value:.2f}(w={s.weight})" for s in signals)
                + f" → {confidence:.3f}"
            )

            part = DetectedPart(
                classification=classification,
                label=region["label"],
                content_type=region.get("content_type", "Unknown"),
                rect=rect,
                polygon=polygon,
                confidence=confidence,
                raw_response={**region, "ensemble_signals": [s.model_dump() for s in signals]},
            )
            parts.append(part)

        return parts
    
    def filter_low_confidence(self, parts: List[DetectedPart], threshold: float = 0.6) -> List[DetectedPart]:
        """Filter out low-confidence detections"""
        filtered = [p for p in parts if p.is_confident(threshold)]
        removed_count = len(parts) - len(filtered)
        
        if removed_count > 0:
            logger.info(f"✓ Filtered {removed_count} low-confidence regions (threshold: {threshold})")
        
        return filtered
    
    # ========================================================================
    # MAIN PIPELINE
    # ========================================================================
    
    def analyze(self, image: PIL_Image.Image, image_data: Dict) -> List[DetectedPart]:
        """
        Main analysis pipeline: calibrate → detect package size → detect rough → refine → validate CLP → filter
        
        Returns:
            List of detected parts with high confidence
        """
        logger.info("=" * 60)
        logger.info("Starting label analysis...")
        logger.info("=" * 60)
        
        # Store image dimensions for ensemble scoring
        self._image_size = (image.width, image.height)

        # Stage 0a: Calibrate DPI
        self.calibrate_dpi(image, image_data)
        
        # Stage 0b: Detect package size (if not provided)
        if self.package_size_ml == 500 and self.package_size_confidence == 0.0:
            # Only auto-detect if using default (not explicitly provided)
            pkg_size, pkg_conf = detect_package_size(image_data, self.gemini, image.width, image.height)
            self.package_size_ml = pkg_size
            self.package_size_confidence = pkg_conf
            logger.info(f"Package size: {self.package_size_ml}ml (confidence: {pkg_conf:.0%})")
        
        # Stage 1: Rough detection
        rough_regions = self.detect_parts_rough(image_data)
        if not rough_regions:
            logger.warning("No regions detected in Stage 1")
            return []
        
        # Stage 2: Refine boundaries (only for CLP regions to save time)
        import time as time_module
        stage2_start = time_module.time()
        logger.info("Stage 2: Boundary Refinement (CLP regions only)")
        refined_regions = []
        clp_count = sum(1 for r in rough_regions if r["classification"] == "CLP")
        
        for i, region in enumerate(rough_regions):
            if region["classification"] == "CLP":
                logger.debug(f"  Refining CLP region {i + 1}/{len(rough_regions)}...")
                refined = self.refine_boundaries(image_data, region)
                refined_regions.append(refined)
            else:
                # Skip refinement for non-CLP to save API calls
                logger.debug(f"  Skipping refinement for NON-CLP region {i + 1}/{len(rough_regions)}")
                refined_regions.append(region)
        
        stage2_time = time_module.time() - stage2_start
        logger.info(f"Stage 2 complete: refined {clp_count} CLP regions in {stage2_time:.1f}s")
        
        # Stage 3: CLP Compliance Validation (for CLP regions only)
        logger.info("Stage 3: CLP Compliance Validation")
        for i, region in enumerate(refined_regions):
            if region["classification"] == "CLP":
                # Crop the region for closer analysis
                rect = region["rect"]
                
                # Ensure crop coordinates are within image bounds
                xmin = max(0, int(rect["xmin"]))
                ymin = max(0, int(rect["ymin"]))
                xmax = min(image.width, int(rect["xmax"]))
                ymax = min(image.height, int(rect["ymax"]))
                
                # Validate crop is non-empty
                if xmax <= xmin or ymax <= ymin:
                    logger.warning(f"Skipping region '{region['label']}': invalid crop bounds ({xmin},{ymin})-({xmax},{ymax})")
                    region["compliance_check"] = {
                        "error": "Invalid crop coordinates",
                        "overall_compliance": "SKIP"
                    }
                    continue
                
                cropped = image.crop((xmin, ymin, xmax, ymax))
                logger.info(f"  ✓ Cropped '{region['label']}': {cropped.width}×{cropped.height}px")
                
                # Pass detected package size and inner packaging flag for correct rule application
                # Inner packaging exemption applies to ≤10ml (font can be smaller if legible)
                is_inner = self.package_size_ml <= 10 and region.get("content_type", "").lower() in ["inner packaging", "inner label", "inner"]
                compliance = self.validate_clp_compliance(
                    image_data, region, cropped,
                    package_size_ml=self.package_size_ml,
                    is_inner_packaging=is_inner
                )
                region["compliance_check"] = compliance
        
        # Stage 4: Convert & filter (with ensemble scoring)
        logger.info("Stage 4: Ensemble Scoring & Filtering")
        self.detected_parts = self._regions_to_detected_parts(refined_regions, rough_regions)
        
        # Add compliance data to DetectedPart objects
        for i, part in enumerate(self.detected_parts):
            if i < len(refined_regions):
                part.compliance_check = refined_regions[i].get("compliance_check")
        
        self.detected_parts = self.filter_low_confidence(self.detected_parts, threshold=0.6)
        
        # Log compliance summary with human review flags
        clp_parts = [p for p in self.detected_parts if p.classification == PartClassification.CLP]
        compliant_parts = [p for p in clp_parts if p.is_compliant()]
        non_compliant = len(clp_parts) - len(compliant_parts)
        review_flagged = [p for p in clp_parts if p.needs_human_review()]
        
        logger.info(f"=" * 60)
        logger.info(f"Analysis complete: {len(self.detected_parts)} confident regions detected")
        logger.info(f"Package size: {self.package_size_ml}ml (confidence: {self.package_size_confidence:.0%})")
        logger.info(f"CLP regions: {len(clp_parts)} total, {len(compliant_parts)} compliant, {non_compliant} non-compliant")
        if review_flagged:
            logger.warning(f"⚠️  HUMAN REVIEW NEEDED: {len(review_flagged)} regions flagged (low confidence or borderline)")
            for p in review_flagged:
                compliance = p.compliance_check or {}
                meas_conf = compliance.get('measurement_confidence', compliance.get('compliance_confidence', 'N/A'))
                if isinstance(meas_conf, (int, float)):
                    conf_str = f"{meas_conf:.0%}"
                else:
                    conf_str = str(meas_conf)
                logger.warning(f"   - {p.label} (confidence: {conf_str}, status: {compliance.get('overall_compliance', 'unknown')})")
        logger.info("=" * 60)
        
        # Auto-save visualization to Desktop (create dir if needed)
        # Generate unique filename with timestamp to avoid overwrites
        try:
            import datetime
            viz_dir = Path.home() / "Desktop"
            viz_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # Include milliseconds
            viz_filename = f"label_analysis_{timestamp}.jpg"
            viz_path = str(viz_dir / viz_filename)
            self.visualize(image, output_path=viz_path)
            logger.info(f"✓ Visualization saved with unique filename: {viz_filename}")
        except Exception as e:
            logger.warning(f"Could not save visualization: {e}")
        
        return self.detected_parts
    
    # ========================================================================
    # BATCH PROCESSING
    # ========================================================================

    @staticmethod
    def analyze_batch(
        image_paths: List[str],
        project_id: str,
        dpi: int = 300,
        max_workers: int = 3,
        cache_dir: Optional[str] = None,
        confidence_weights: Optional[Dict[str, float]] = None,
        on_complete: Optional[Callable[['BatchResult', int, int], None]] = None,
        use_cache: bool = True,
    ) -> List['BatchResult']:
        """Analyze multiple label images concurrently.

        Each image gets its own ``LabelAnalyzer`` instance (independent
        calibration state) but they share the same disk cache, so identical
        images are not re-processed.

        Args:
            image_paths: List of file paths to images or PDFs.
            project_id: GCP project ID for Gemini.
            dpi: Default DPI for PDF rendering.
            max_workers: Maximum concurrent analyses. Keep ≤ 5 to respect
                Gemini rate limits.
            cache_dir: Shared cache directory (default: ~/.cache/label_analyzer).
            confidence_weights: Override ensemble confidence weights.
            on_complete: Optional callback ``(result, index, total)`` invoked
                after each image finishes (useful for progress bars).

        Returns:
            List of ``BatchResult`` in the same order as *image_paths*.

        Example::

            results = LabelAnalyzer.analyze_batch(
                ["label1.jpg", "label2.pdf"],
                project_id="my-project",
                on_complete=lambda r, i, n: print(f"[{i+1}/{n}] {r.path}: {'OK' if r.success else 'FAIL'}"),
            )
            for r in results:
                if r.success:
                    print(f"{r.path}: {len(r.parts)} parts in {r.elapsed_seconds:.1f}s")
        """
        total = len(image_paths)
        results: List[Optional[BatchResult]] = [None] * total

        def _process_one(index: int, path: str) -> BatchResult:
            t0 = time.monotonic()
            try:
                if path.lower().endswith(".pdf"):
                    img, _ = pdf_to_image(path, dpi=dpi)
                    image_dpi = dpi  # Use specified DPI for PDFs
                else:
                    img = PIL_Image.open(path)
                    # Extract DPI from image metadata if available
                    image_dpi = dpi  # Default
                    if hasattr(img, 'info') and 'dpi' in img.info:
                        dpi_tuple = img.info['dpi']
                        if isinstance(dpi_tuple, (tuple, list)) and len(dpi_tuple) >= 1:
                            image_dpi = int(dpi_tuple[0])
                        elif isinstance(dpi_tuple, (int, float)):
                            image_dpi = int(dpi_tuple)

                image_data = image_to_base64(img)
                analyzer = LabelAnalyzer(
                    project_id, dpi=image_dpi, cache_dir=cache_dir,
                    confidence_weights=confidence_weights,
                    use_cache=use_cache,
                )
                parts = analyzer.analyze(img, image_data)
                return BatchResult(
                    path=path, parts=parts, analyzer=analyzer,
                    elapsed_seconds=time.monotonic() - t0,
                )
            except Exception as exc:
                logger.error(f"Batch item failed [{path}]: {exc}")
                return BatchResult(
                    path=path, error=exc,
                    elapsed_seconds=time.monotonic() - t0,
                )

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_process_one, i, p): i
                for i, p in enumerate(image_paths)
            }
            for future in as_completed(futures):
                idx = futures[future]
                result = future.result()
                results[idx] = result
                if on_complete:
                    on_complete(result, idx, total)

        return results  # type: ignore[return-value]

    # ========================================================================
    # OUTPUT & VISUALIZATION
    # ========================================================================
    
    def to_dict(self) -> Dict:
        """Export results as dictionary"""
        return {
            "calibration": {
                "original_dpi": self.calibration.original_dpi,
                "true_dpi": self.calibration.true_dpi,
                "dpmm": self.calibration.dpmm,
                "is_calibrated": self.calibration.is_calibrated
            },
            "package_size": {
                "value_ml": self.package_size_ml,
                "confidence": self.package_size_confidence
            },
            "detected_parts": [
                {
                    "classification": part.classification.value,
                    "label": part.label,
                    "confidence": part.confidence,
                    "rect": asdict(part.rect),
                    "has_polygon": part.polygon is not None,
                    "is_compliant": part.is_compliant(),
                    "needs_review": part.needs_human_review()
                }
                for part in self.detected_parts
            ]
        }
    
    def export_results(self, output_dir: str) -> Dict[str, str]:
        """Export analysis results (JSON, visualization, CSV) to directory
        
        Args:
            output_dir: Directory to save files
            
        Returns:
            Dict with paths to saved files
        """
        from pathlib import Path
        import csv
        
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        
        files = {}
        
        # Save JSON results
        json_path = out_path / "analysis_results.json"
        with open(json_path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        files["json"] = str(json_path)
        logger.info(f"✓ Results saved: {json_path}")
        
        # Save CSV (easy spreadsheet import)
        csv_path = out_path / "analysis_results.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["label", "classification", "confidence", "compliant", "needs_review", "x_min", "y_min", "x_max", "y_max"])
            writer.writeheader()
            for part in self.detected_parts:
                writer.writerow({
                    "label": part.label,
                    "classification": part.classification.value,
                    "confidence": f"{part.confidence:.2%}",
                    "compliant": part.is_compliant(),
                    "needs_review": part.needs_human_review(),
                    "x_min": part.rect.xmin,
                    "y_min": part.rect.ymin,
                    "x_max": part.rect.xmax,
                    "y_max": part.rect.ymax
                })
        files["csv"] = str(csv_path)
        logger.info(f"✓ CSV saved: {csv_path}")
        
        return files
    
    def visualize(self, image: PIL_Image.Image, output_path: Optional[str] = None) -> PIL_Image.Image:
        """Draw detected regions on image and optionally save to file.
        
        NOTE: All coordinates must be in the SAME SPACE as the input image.
        If image was resized, coordinates should be scaled proportionally.
        """
        img_copy = image.copy()
        draw = PIL_ImageDraw.Draw(img_copy)
        
        colors = {
            PartClassification.CLP: "red",
            PartClassification.NON_CLP: "blue",
            PartClassification.UNKNOWN: "yellow"
        }
        
        for part in self.detected_parts:
            color = colors.get(part.classification, "white")
            
            if part.polygon:
                points = part.polygon.to_list_of_tuples()
                draw.polygon(points, outline=color, width=5)
            else:
                rect = part.rect
                # CRITICAL: Ensure rectangle is within image bounds
                # This prevents boxes from appearing in wrong locations
                xmin = max(0, rect.xmin)
                ymin = max(0, rect.ymin)
                xmax = min(image.width, rect.xmax)
                ymax = min(image.height, rect.ymax)
                
                if xmax > xmin and ymax > ymin:  # Valid box
                    draw.rectangle(
                        [(xmin, ymin), (xmax, ymax)],
                        outline=color,
                        width=5
                    )
                    
                    # Add label at center
                    center_x = (xmin + xmax) // 2
                    center_y = (ymin + ymax) // 2
                    text = f"{part.label}\n({part.confidence:.0%})"
                    draw.text((center_x, center_y), text, fill=color)
                else:
                    logger.warning(f"Skipped invalid box for {part.label}: ({xmin},{ymin})-({xmax},{ymax}) in {image.width}×{image.height} image")
        
        # Auto-save if path provided
        if output_path:
            img_copy.save(output_path)
            logger.info(f"✓ Visualization saved: {output_path}")
        
        return img_copy


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def pdf_to_image(pdf_path: str, page: int = 0, dpi: int = 300) -> Tuple[PIL_Image.Image, str]:
    """Convert PDF page to image"""
    doc = fitz.open(pdf_path)
    page_obj = doc.load_page(page)
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    pix = page_obj.get_pixmap(matrix=matrix)
    
    # Convert to PIL Image
    img_data = pix.tobytes("ppm")
    img = PIL_Image.open(BytesIO(img_data))
    
    return img, pdf_path


def image_to_base64(img: PIL_Image.Image, format: str = "JPEG") -> Dict:
    """Convert PIL Image to Gemini-compatible base64 format"""
    buffer = BytesIO()
    img.save(buffer, format=format)
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
    
    mime_type = f"image/{format.lower()}"
    
    return {
        "inline_data": {
            "mime_type": mime_type,
            "data": encoded
        }
    }


def analyze_image_file(image_path: str, project_id: str) -> Tuple[LabelAnalyzer, List[DetectedPart]]:
    """Convenience function: load image and run full analysis
    
    Automatically extracts DPI from image metadata if available.
    Falls back to 300 DPI if not embedded.
    """
    logger.info(f"Loading image: {image_path}")
    
    img = PIL_Image.open(image_path)
    
    # Extract DPI from image metadata if available
    image_dpi = 300  # Default fallback
    if hasattr(img, 'info') and 'dpi' in img.info:
        dpi_tuple = img.info['dpi']
        if isinstance(dpi_tuple, (tuple, list)) and len(dpi_tuple) >= 1:
            image_dpi = int(dpi_tuple[0])  # Use X-DPI (usually same as Y-DPI)
            logger.info(f"  ✓ Extracted DPI from image metadata: {image_dpi} DPI")
        elif isinstance(dpi_tuple, (int, float)):
            image_dpi = int(dpi_tuple)
            logger.info(f"  ✓ Extracted DPI from image metadata: {image_dpi} DPI")
    else:
        logger.info(f"  ℹ️  No DPI in image metadata, using default: {image_dpi} DPI")
    
    image_data = image_to_base64(img)
    
    analyzer = LabelAnalyzer(project_id, dpi=image_dpi)
    parts = analyzer.analyze(img, image_data)
    
    return analyzer, parts


def analyze_batch(image_paths: List[str], project_id: str, **kwargs) -> List[BatchResult]:
    """Convenience wrapper for ``LabelAnalyzer.analyze_batch``.

    Accepts the same keyword arguments. See
    :meth:`LabelAnalyzer.analyze_batch` for full documentation.
    """
    return LabelAnalyzer.analyze_batch(image_paths, project_id, **kwargs)


if __name__ == "__main__":
    # Example usage
    print("Production-ready CLP Label Analyzer loaded")
    print("Use: analyzer, parts = analyze_image_file('path/to/image.jpg', 'your-project-id')")
    print("Batch: results = analyze_batch(['img1.jpg', 'img2.jpg'], 'your-project-id')")
