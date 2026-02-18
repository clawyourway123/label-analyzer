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
# PYMUPDF GLYPH HEIGHT CORRECTION
# ============================================================================
# PyMuPDF's default glyph bbox includes 10-37% padding above/below visible
# characters (font design metrics). Setting this flag returns VISIBLE heights
# only, matching CLP measurement requirements. This must be set BEFORE any
# PyMuPDF operations to take effect.
# Ref: https://github.com/pymupdf/PyMuPDF/discussions/3067
fitz.TOOLS.set_small_glyph_heights(True)


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
    
    @property
    def font_size_mm(self) -> float:
        """Extract font size in mm from compliance check (0.0 if unavailable).
        
        Safely handles missing, malformed, or nested measurement data structures.
        Returns 0.0 if data is unavailable or cannot be parsed.
        """
        if not self.compliance_check:
            return 0.0
        
        # Handle both nested and direct measurement storage
        measurements = self.compliance_check.get("measurements", self.compliance_check)
        
        try:
            font_mm = measurements.get("font_size_mm")
            if font_mm is None:
                return 0.0
            return float(font_mm)
        except (ValueError, TypeError, AttributeError):
            logger.debug(f"Could not extract font_size_mm from compliance check: {self.compliance_check}")
            return 0.0
    
    @property
    def compliance_status(self) -> str:
        """Overall compliance status string (PASS/FAIL/SKIP/ERROR or N/A).
        
        Returns one of: PASS, FAIL, SKIP, ERROR, or N/A (if no compliance check).
        """
        if not self.compliance_check:
            return "N/A"
        
        status = self.compliance_check.get("overall_compliance")
        if status and isinstance(status, str):
            return status
        
        return "N/A"
    
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
        # Cache: map reference value (mm) → DPI for consistency
        # If Gemini finds the same reference again (diff pixel coords), reuse same DPI
        self.reference_dpi_cache = {}
    
    def update(self, line: MeasurementLine):
        """Update DPI based on measurement line
        
        CRITICAL: If we've seen this reference value (mm) before, reuse the DPI.
        This prevents different pixel measurements of the same reference from causing
        DPI to vary wildly (336 → 247 → 209 for the same 636.07mm reference).
        """
        px_length = ((line.end_point.x - line.start_point.x)**2 + 
                     (line.end_point.y - line.start_point.y)**2)**0.5
        
        if line.value_mm > 0:
            # Check if we've calibrated using this reference value before
            ref_key = round(line.value_mm, 2)  # Round to 2 decimals for matching
            
            if ref_key in self.reference_dpi_cache:
                # Reuse cached DPI for this reference
                cached_dpi = self.reference_dpi_cache[ref_key]
                logger.info(f"  ℹ️  Reference {line.value_mm}mm seen before, reusing cached DPI: {cached_dpi} DPI")
                self.true_dpi = cached_dpi
                self.dpmm = cached_dpi / 25.4
                self.measurement_line = line
                self.is_calibrated = True
                return True
            
            # First time seeing this reference - calculate and cache
            calculated_dpmm = px_length / line.value_mm
            calculated_dpi = int(round(calculated_dpmm * 25.4))
            
            # Cache it
            self.reference_dpi_cache[ref_key] = calculated_dpi
            
            self.true_dpi = calculated_dpi
            self.dpmm = calculated_dpmm
            self.measurement_line = line
            self.is_calibrated = True
            logger.info(f"Calibrated DPI: {self.true_dpi} DPI ({self.dpmm:.2f} px/mm) [reference: {line.value_mm}mm]")
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
    
    CRITICAL: This prompt MUST measure X-HEIGHT (not full cap height) for accuracy.
    EU Regulation 1272/2008 (CLP) specifies x-height, not total character height.
    
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

### **CRITICAL INSTRUCTION — Measure X-HEIGHT (NOT Full Character Height):**

EU Regulation 1272/2008 (CLP) CLP compliance requires measuring x-height (the height of lowercase 'x').
This is the height of lowercase letters WITHOUT ascenders (like 'd', 'k', 'l') or descenders (like 'g', 'p', 'y').

**CRITICAL RULE:** You MUST measure x-height directly from lowercase letters. Do NOT estimate or convert.
- Capital letter height is ~25-30% LARGER than x-height (e.g., 'H' is too tall)
- Even if text is all capitals, find a mixed-case section or identify the X-HEIGHT from descender analysis
- If you must approximate from all-caps text, be explicit: "Estimated x-height from [letter]: [value]px"

### **Task: Measure these metrics (provide numbers, not judgments):**

1. **Font size (X-HEIGHT - CRITICAL):** Find the SMALLEST readable text in the CLP section. 
   - **PREFERRED METHOD:** Identify any lowercase letter word (e.g., "natural", "certified", "contains", "hazard", "precaution")
     * Measure from baseline (bottom of letter) to TOP of x-body (top of 'a', 'e', 'o', 'x', 'n', etc.)
     * **MUST EXCLUDE:** Ascenders (d, h, k, l, t) and descenders (g, j, p, q, y) - these are taller/shorter
     * Example: In "text", measure the 'e' or 'x' (NOT the 't')
     * Report: "X-height measured from letter '[letter]' in word '[word]': [X]px = [Y]mm"
   
   - **IF ONLY ALL-CAPS TEXT EXISTS:**
     * Look for letters like 'O' or 'I' that DON'T have descenders to identify baseline
     * Measure the VISIBLE HEIGHT of the capital letter (baseline to top, full height)
     * This is cap-height, NOT x-height—**DO NOT apply 0.71 conversion**
     * Report: "All-caps text: measured cap-height [X]px from [letter]"
     * CRITICAL: Provide an ESTIMATED x-height using 0.70 multiplier
     * Calculate and report: "estimated_xheight_mm = [cap-height-mm] × 0.70"
     * This allows automatic correction: code will use your estimate directly
   
   - **STEP 4:** Report measurement with confirmation
     * Height in pixels: X
     * Converted to mm: X / {dpmm:.2f} = Y mm
     * Measurement method: "x-height-direct" (measured from lowercase) or "cap-height-estimated" (from all-caps with 0.70× estimate)
     * CRITICAL: If measurement_method is "cap-height-estimated", ALWAYS include the estimated_xheight_mm field
     * Confidence level: 0.9-1.0 if direct x-height, 0.75-0.85 if cap-height with estimate

2. **Line distance (BASELINE-TO-BASELINE - CRITICAL):** Measure the vertical gap between two consecutive lines of text.
   - **MUST** measure from baseline of one line to baseline of next line (NOT top-to-top or bottom-to-bottom)
   - Baseline = the imaginary line that letters sit on (bottom of 'a', 'e', 'x'; excludes descenders like 'g', 'p')
   - Report in pixels and mm with confirmation: "Baseline-to-baseline distance: [X]px = [Y]mm"

3. **Background color:** Describe the background of the text region
   - Is it white? Black? Another color? Gradient?
   - Describe the text color (black, white, other)
   - Is there sufficient contrast? (visual assessment only)

### **Important:**
- Be as precise as possible with pixel measurements
- X-HEIGHT is the measurement criterion (lowercase letters, NOT capitals)
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
        "measurement_method": {"type": "string", "enum": ["x-height-direct", "cap-height-estimated", "unclear"], "description": "Method: x-height-direct (measured lowercase), cap-height-estimated (from all-caps with estimate), unclear (unmeasurable)"},
        "estimated_xheight_mm": {"type": "number", "description": "If measurement_method is 'cap-height-estimated', this is the estimated x-height in mm (cap-height-mm × 0.70)"},
        "notes": {"type": "string", "description": "Any ambiguities or special observations"}
    },
    "required": ["font_size_pixels", "font_size_mm", "line_distance_pixels", "line_distance_mm", "background_color", "text_color", "contrast_assessment", "measurement_confidence", "measurement_method"]
}

def validate_measurements_against_rules(metrics: Dict, package_size_ml: int = 500, is_inner_packaging: bool = False) -> Dict:
    """Apply CLP rules to measurements. 100% deterministic.
    
    EU Regulation 1272/2008 (CLP) Compliance Rules:
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
    # EU Regulation 1272/2008 (CLP): Inner packaging ≤10ml can be smaller (exemption) but must remain easily legible
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
    
    # Rule 3: Background & Text Color (High Contrast Required)
    # EU Regulation 1272/2008 (CLP): CLP text MUST have high contrast
    # - Primary: White background with Black text (classic CLP)
    # - Secondary: Yellow background with Black text (valid for hazard pictograms, GHS color-coded)
    # - Requirement: Must be high contrast (visual assessment)
    bg_color = metrics.get('background_color', '').lower()
    text_color = metrics.get('text_color', '').lower()
    contrast_assess = metrics.get('contrast_assessment', 'medium').lower()
    
    # Valid combinations for CLP compliance
    is_white_bg = any(word in bg_color for word in ['white', 'off-white', 'ivory', 'cream', 'light'])
    is_yellow_bg = any(word in bg_color for word in ['yellow', 'gold', 'amber', 'orange-yellow', 'mustard'])
    is_black_text = any(word in text_color for word in ['black', 'dark', 'dark gray', 'dark grey', 'charcoal'])
    is_high_contrast = contrast_assess in ['high', 'very high', 'excellent']
    
    # Also accept: dark background + white/light text with high contrast
    is_dark_bg = any(word in bg_color for word in ['dark', 'black', 'navy', 'purple', 'deep'])
    is_white_text = any(word in text_color for word in ['white', 'light', 'cream', 'ivory'])
    
    # Accept: (White + Black) OR (Yellow + Black) OR (Dark bg + White text with high contrast)
    contrast_pass = ((is_white_bg and is_black_text) or 
                     (is_yellow_bg and is_black_text and is_high_contrast) or
                     (is_dark_bg and is_white_text and is_high_contrast))
    contrast_status = "PASS" if contrast_pass else "FAIL"
    
    if contrast_pass:
        contrast_detail = f"High contrast confirmed: {metrics.get('background_color', 'unknown')} bg + {metrics.get('text_color', 'unknown')} text"
    else:
        contrast_detail = f"Insufficient contrast: {metrics.get('background_color', 'unknown')} bg + {metrics.get('text_color', 'unknown')} text (assessment: {contrast_assess})"
    
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
            logger.warning(f"  ⚠️  Could not reliably detect package size (confidence: {confidence:.0%}), using default 500ml — font size thresholds may be inaccurate")
            return 500, 0.0  # Default fallback
            
    except (APIError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Package size detection failed ({type(e).__name__}: {e}), using default 500ml")
        return 500, 0.0
    except Exception as e:
        logger.error(f"Unexpected error in package size detection: {type(e).__name__}: {e}")
        logger.warning("Using default 500ml package size (fallback)")
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
        self._last_image_scale_factor: float = 1.0  # Scale factor for Gemini's internal resizing
    
    def _get_client(self):
        """Lazy-load Gemini client"""
        if self._client is None:
            try:
                from google import genai
                self._client = genai.Client(vertexai=True, project=self.project_id, location=self.location)
            except ImportError:
                logger.error("google-genai library not installed. Install with: pip install google-genai")
                raise
            except Exception as e:
                # Check if it's a credentials error and provide helpful guidance
                if "credentials" in str(e).lower() or "authentication" in str(e).lower():
                    logger.error("=" * 80)
                    logger.error("GCP CREDENTIALS NOT CONFIGURED")
                    logger.error("=" * 80)
                    logger.error("")
                    logger.error("To use the Label Analyzer, you need to set up Google Cloud credentials:")
                    logger.error("")
                    logger.error("Option 1 - Application Default Credentials (recommended for local dev):")
                    logger.error("  1. Install gcloud CLI: https://cloud.google.com/sdk/docs/install")
                    logger.error("  2. Run: gcloud auth application-default login")
                    logger.error("  3. Follow the browser authentication flow")
                    logger.error("")
                    logger.error("Option 2 - Service Account Key (recommended for production):")
                    logger.error("  1. Create a service account with Vertex AI permissions")
                    logger.error("  2. Download the JSON key file")
                    logger.error("  3. Set environment variable:")
                    logger.error("     export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json")
                    logger.error("")
                    logger.error("=" * 80)
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
        # BUG FIX: Avoid redundant cache creation in both branches
        cache = ResponseCache(cache_dir=cache_dir)
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
        self._image_cache: Dict[str, str] = {}  # Cache preprocessed images by hash
        self._reference_dimensions: List[Dict] = []  # For PDF scale detection
        self.package_size_confidence: float = 0.0  # Confidence in detected size
    
    def clear_cache(self) -> int:
        """Clear all cached API responses. Use when analyzing new images.
        
        Returns:
            Number of cache entries removed
        """
        return self.gemini.cache.clear()
    
    def _get_or_cache_image(self, image: PIL_Image.Image) -> str:
        """Get or cache base64-encoded image data.
        
        Args:
            image: PIL Image object
            
        Returns:
            base64-encoded image data (cached for repeated use)
        """
        # Create hash from image data
        img_bytes = BytesIO()
        image.save(img_bytes, format='PNG')
        img_hash = hashlib.md5(img_bytes.getvalue()).hexdigest()
        
        if img_hash not in self._image_cache:
            # First time seeing this image, encode and cache
            self._image_cache[img_hash] = base64.b64encode(img_bytes.getvalue()).decode('utf-8')
            logger.debug(f"  💾 Cached image (hash: {img_hash[:8]}...)")
        else:
            logger.debug(f"  ⚡ Using cached image (hash: {img_hash[:8]}...)")
        
        return self._image_cache[img_hash]
    
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
    # PDF VECTOR FONT MEASUREMENT (deterministic, no Gemini needed)
    # ========================================================================
    
    def _auto_detect_pdf_scale(self, page, pdf_path: str):
        """
        Automatically detect PDF scale by OCR'ing dimension line labels.
        
        1. Find the top measurement lines (longest H/V vector lines)
        2. Render a small crop around each line's label area
        3. Ask Gemini to READ the mm value (OCR — reliable)
        4. Scale = OCR'd value / vector length
        """
        import fitz
        
        all_drawings = page.get_drawings()
        
        # Collect measurement lines with their positions
        h_lines = []  # (length_mm, y_pt, x_start_pt, x_end_pt)
        v_lines = []  # (length_mm, x_pt, y_start_pt, y_end_pt)
        for d in all_drawings:
            for item in d.get('items', []):
                if item[0] == 'l':
                    p1, p2 = item[1], item[2]
                    if abs(p1.y - p2.y) < 2:  # horizontal
                        length_mm = abs(p2.x - p1.x) / 72 * 25.4
                        if length_mm > 50:
                            h_lines.append((length_mm, p1.y, min(p1.x, p2.x), max(p1.x, p2.x)))
                    elif abs(p1.x - p2.x) < 2:  # vertical
                        length_mm = abs(p2.y - p1.y) / 72 * 25.4
                        if length_mm > 50:
                            v_lines.append((length_mm, p1.x, min(p1.y, p2.y), max(p1.y, p2.y)))
        
        h_lines.sort(key=lambda x: -x[0])
        v_lines.sort(key=lambda x: -x[0])
        
        # Take top 3 unique-length lines of each orientation
        def unique_by_length(lines, tol=5):
            seen = []
            result = []
            for line in lines:
                if not any(abs(line[0] - s) < tol for s in seen):
                    seen.append(line[0])
                    result.append(line)
                if len(result) >= 3:
                    break
            return result
        
        candidates = []
        for line in unique_by_length(h_lines):
            candidates.append(('horizontal', line[0], line[1], line[2], line[3]))
        for line in unique_by_length(v_lines):
            candidates.append(('vertical', line[0], line[1], line[2], line[3]))
        
        if not candidates:
            logger.info("  📏 No measurement lines found for auto scale detection")
            return
        
        logger.info(f"  📏 Auto scale: found {len(candidates)} candidate measurement lines")
        
        # Render crops around each measurement line and OCR the label
        zoom = 3.0  # Render at high res for OCR readability
        mat = fitz.Matrix(zoom, zoom)
        
        ocr_results = []
        for orient, length_mm, pos, start, end in candidates[:4]:  # Max 4 to limit API calls
            # Create a crop rect around the measurement line's label area
            margin = 30  # pts around the line
            if orient == 'horizontal':
                # Label is usually above/below the line, near the center
                mid_x = (start + end) / 2
                clip = fitz.Rect(mid_x - 80, pos - margin, mid_x + 80, pos + margin)
            else:
                # Label is usually left/right of the line, near the center
                mid_y = (start + end) / 2
                clip = fitz.Rect(pos - margin, mid_y - 80, pos + margin, mid_y + 80)
            
            # Ensure clip is within page bounds
            clip = clip & page.rect
            if clip.is_empty:
                continue
            
            pix = page.get_pixmap(matrix=mat, clip=clip)
            img_bytes = pix.tobytes("png")
            img_b64 = base64.b64encode(img_bytes).decode('utf-8')
            
            # Ask Gemini to read JUST the number
            ocr_prompt = """Read the numeric measurement value shown in this image.
This is a dimension label from technical artwork. It shows a measurement in millimeters.
Return ONLY the numeric value (e.g., 172.75 or 636.07). 
If you see "mm" after the number, ignore it.
If no clear number is visible, return 0."""
            
            ocr_schema = {
                "type": "object",
                "properties": {
                    "value_mm": {"type": "number", "description": "The numeric mm value read from the label"}
                },
                "required": ["value_mm"]
            }
            
            try:
                img_data = {"inline_data": {"mime_type": "image/png", "data": img_b64}}
                response = self.gemini.analyze_image(img_data, ocr_prompt, ocr_schema, temperature=0.1)
                result = json.loads(response)
                read_value = result.get('value_mm', 0)
                if read_value > 0:
                    scale = read_value / length_mm
                    ocr_results.append((orient, length_mm, read_value, scale))
                    logger.info(f"  📏 OCR: {orient} line {length_mm:.2f}mm → label reads {read_value}mm → scale={scale:.4f}")
            except Exception as e:
                logger.warning(f"  ⚠️ OCR failed for {orient} {length_mm:.1f}mm line: {e}")
                continue
        
        # Apply scales
        v_scale = 1.0
        h_scale = 1.0
        for orient, vec_mm, real_mm, scale in ocr_results:
            if 0.8 < scale < 1.3:  # Sanity check
                if orient == 'vertical':
                    v_scale = scale
                else:
                    h_scale = scale
        
        if not hasattr(self, '_pdf_scale_cache'):
            self._pdf_scale_cache = {}
        self._pdf_scale_cache[pdf_path] = (v_scale, h_scale)
        
        if v_scale != 1.0 or h_scale != 1.0:
            logger.info(f"  📏 Auto-detected scales: vertical={v_scale:.4f}, horizontal={h_scale:.4f}")
        else:
            logger.info(f"  📏 PDF appears to be at 1:1 scale")
    
    def measure_font_from_pdf_vectors(self, pdf_path: str, region_rect_px: Dict) -> Optional[Dict]:
        """
        Measure font size directly from PDF vector paths (100% deterministic).
        
        Instead of asking Gemini to measure pixels (unreliable), we extract the
        actual glyph outlines from the PDF and measure their bounding boxes in
        PDF points, then convert to mm.
        
        Args:
            pdf_path: Path to the PDF file
            region_rect_px: Region bounding box in pixel coordinates at rendering DPI
                           {'xmin': int, 'ymin': int, 'xmax': int, 'ymax': int}
        
        Returns:
            Dict with font_size_mm, line_distance_mm, measurement_confidence, etc.
            or None if measurement failed
        """
        try:
            import fitz
            import statistics
            from collections import Counter
            
            doc = fitz.open(pdf_path)
            page = doc.load_page(0)
            
            # ============================================================
            # AUTO SCALE DETECTION from PDF measurement/dimension lines
            # ============================================================
            # PDFs may not be at 1:1 physical scale. Detect by:
            # 1. Find measurement lines (longest H/V vector lines)
            # 2. Render area around each line, ask Gemini to READ the mm label (OCR)
            # 3. Scale = OCR'd value / vector length
            # This is reliable because Gemini reads text well (unlike pixel measurement).
            vertical_scale = 1.0
            horizontal_scale = 1.0
            
            # Use cached scale if already computed for this PDF
            if hasattr(self, '_pdf_scale_cache') and pdf_path in self._pdf_scale_cache:
                vertical_scale, horizontal_scale = self._pdf_scale_cache[pdf_path]
                if vertical_scale != 1.0 or horizontal_scale != 1.0:
                    logger.info(f"  📏 Using cached scale: v={vertical_scale:.4f}, h={horizontal_scale:.4f}")
            elif hasattr(self, '_reference_dimensions') and self._reference_dimensions:
                # MANUAL MODE: user provided known dimensions
                all_drawings = page.get_drawings()
                v_lines_mm = []
                h_lines_mm = []
                for d in all_drawings:
                    for item in d.get('items', []):
                        if item[0] == 'l':
                            p1, p2 = item[1], item[2]
                            if abs(p1.y - p2.y) < 2:
                                length = abs(p2.x - p1.x) / 72 * 25.4
                                if length > 50:
                                    h_lines_mm.append(length)
                            elif abs(p1.x - p2.x) < 2:
                                length = abs(p2.y - p1.y) / 72 * 25.4
                                if length > 50:
                                    v_lines_mm.append(length)
                
                for ref in self._reference_dimensions:
                    ref_mm = ref['mm']
                    orientation = ref.get('orientation', 'auto')
                    best_match = None
                    best_diff = float('inf')
                    
                    if orientation in ('vertical', 'auto'):
                        for v in v_lines_mm:
                            diff = abs(v - ref_mm)
                            if diff < best_diff and 0.8 < ref_mm / v < 1.3:
                                best_diff = diff
                                best_match = ('vertical', v, ref_mm / v)
                    if orientation in ('horizontal', 'auto'):
                        for h in h_lines_mm:
                            diff = abs(h - ref_mm)
                            if diff < best_diff and 0.8 < ref_mm / h < 1.3:
                                best_diff = diff
                                best_match = ('horizontal', h, ref_mm / h)
                    
                    if best_match:
                        orient, vec_mm, scale = best_match
                        if orient == 'vertical':
                            vertical_scale = scale
                        else:
                            horizontal_scale = scale
                        logger.info(f"  📏 Scale from ref {ref_mm}mm: {orient} {vec_mm:.2f}mm → scale={scale:.4f}")
                
                if not hasattr(self, '_pdf_scale_cache'):
                    self._pdf_scale_cache = {}
                self._pdf_scale_cache[pdf_path] = (vertical_scale, horizontal_scale)
            else:
                # AUTO MODE: Find dimension lines and OCR their labels via Gemini
                try:
                    self._auto_detect_pdf_scale(page, pdf_path)
                    if hasattr(self, '_pdf_scale_cache') and pdf_path in self._pdf_scale_cache:
                        vertical_scale, horizontal_scale = self._pdf_scale_cache[pdf_path]
                except Exception as e:
                    logger.warning(f"  ⚠️ Auto scale detection failed: {e}")
            
            if vertical_scale != 1.0 or horizontal_scale != 1.0:
                logger.info(f"  📏 Scales: vertical={vertical_scale:.4f}, horizontal={horizontal_scale:.4f}")
            
            # Convert pixel coordinates back to PDF points
            zoom = self.original_dpi / 72  # Same zoom used in pdf_to_image()
            pt_xmin = region_rect_px['xmin'] / zoom
            pt_ymin = region_rect_px['ymin'] / zoom
            pt_xmax = region_rect_px['xmax'] / zoom
            pt_ymax = region_rect_px['ymax'] / zoom
            
            logger.info(f"  📐 PDF vector measurement: region px=({region_rect_px['xmin']},{region_rect_px['ymin']})-({region_rect_px['xmax']},{region_rect_px['ymax']})")
            logger.info(f"  📐 Converted to PDF pts: ({pt_xmin:.1f},{pt_ymin:.1f})-({pt_xmax:.1f},{pt_ymax:.1f})")
            
            # Extract all drawings in this region
            drawings = page.get_drawings()
            region_paths = []
            for d in drawings:
                r = d.get('rect')
                if r:
                    # Check if drawing is within the region (with small margin)
                    margin = 2  # pts
                    if (r[0] >= pt_xmin - margin and r[2] <= pt_xmax + margin and
                        r[1] >= pt_ymin - margin and r[3] <= pt_ymax + margin):
                        w = r[2] - r[0]
                        h = r[3] - r[1]
                        # Filter for text-glyph-sized elements
                        if 0.3 < h < 20 and 0.1 < w < 30:
                            region_paths.append({
                                'rect': r, 'w': w, 'h': h,
                                'y_top': r[1], 'y_bot': r[3],
                                'x': r[0], 'x_end': r[2],
                                'y_center': (r[1] + r[3]) / 2
                            })
            
            doc.close()
            
            if len(region_paths) < 10:
                logger.info(f"  ⚠️ Only {len(region_paths)} glyph paths in region — too few for reliable measurement")
                return None
            
            logger.info(f"  📐 Found {len(region_paths)} glyph paths in region")
            
            # Group paths into text lines by y_center
            # CRITICAL: Compare against line MEDIAN y_center (not last element)
            # to prevent chain-linking across adjacent lines when tall glyphs
            # (descenders/ascenders) bridge the gap.
            # Tolerance = 30% of most common path height (tight enough to separate
            # lines spaced ~2mm apart with ~1.5mm tall glyphs)
            common_h = Counter(round(g['h'], 1) for g in region_paths).most_common(1)[0][0]
            line_tolerance = max(0.8, common_h * 0.4)  # pts — tighter than before
            
            region_paths.sort(key=lambda g: g['y_center'])
            text_lines = []
            current_line = [region_paths[0]]
            current_line_y_median = region_paths[0]['y_center']
            for g in region_paths[1:]:
                # Compare against line MEDIAN, not last element (prevents drift)
                if abs(g['y_center'] - current_line_y_median) < line_tolerance:
                    current_line.append(g)
                    # Update running median (use mean for speed)
                    current_line_y_median = sum(p['y_center'] for p in current_line) / len(current_line)
                else:
                    if len(current_line) >= 3:  # Low threshold to catch short lines
                        text_lines.append(current_line)
                    current_line = [g]
                    current_line_y_median = g['y_center']
            if len(current_line) >= 3:
                text_lines.append(current_line)
            
            if len(text_lines) < 1:
                logger.info(f"  ⚠️ No text lines detected in region")
                return None
            
            logger.info(f"  📐 Detected {len(text_lines)} text lines (tolerance={line_tolerance:.1f}pt)")
            
            # For each text line: group overlapping paths into characters, measure heights
            line_char_heights = []  # Per-line list of character heights in mm
            line_y_centers_mm = []
            
            for line_paths in text_lines:
                # Group by x-overlap into characters
                line_paths.sort(key=lambda g: g['x'])
                chars = []
                current_char = [line_paths[0]]
                for g in line_paths[1:]:
                    cur_x_end = max(p['x_end'] for p in current_char)
                    if g['x'] < cur_x_end + 0.5:  # Overlapping or adjacent
                        current_char.append(g)
                    else:
                        chars.append(current_char)
                        current_char = [g]
                chars.append(current_char)
                
                # Measure each character's full height (union of all sub-paths)
                char_heights_mm = []
                for ch in chars:
                    top = min(p['y_top'] for p in ch)
                    bot = max(p['y_bot'] for p in ch)
                    h_mm = (bot - top) / 72 * 25.4
                    char_heights_mm.append(h_mm)
                
                line_char_heights.append(char_heights_mm)
                
                # Line y-center for spacing calculation
                line_y = statistics.median([p['y_center'] for p in line_paths])
                line_y_centers_mm.append(line_y / 72 * 25.4)
            
            # Flatten all character heights
            all_char_heights_mm = [h for line_h in line_char_heights for h in line_h]
            if not all_char_heights_mm:
                return None
            
            # ================================================================
            # FONT SIZE: Identify body text by LINE height, not char height
            # ================================================================
            # Each line has a mix of upper/lowercase — the MEAN char height per line
            # represents that line's "font size". Cluster LINES by their mean height
            # to separate body text from headers.
            
            line_mean_heights = []  # (line_index, median_char_height_mm)
            for i, line_h in enumerate(line_char_heights):
                if line_h:
                    # Use MEDIAN instead of mean: resistant to tall caps/ascenders
                    # pulling the line height upward in mixed-case text.
                    # In a typical mixed-case line, majority of chars are lowercase,
                    # so median ≈ x-height (what CLP cares about).
                    line_mean_heights.append((i, statistics.median(line_h)))
            
            # Cluster line heights (0.2mm bins)
            line_h_bins = Counter(round(h, 1) for _, h in line_mean_heights)
            
            # Find dominant line height cluster (most lines = body text)
            best_line_bin = None
            best_line_count = 0
            for h_bin, count in line_h_bins.items():
                total = count
                for neighbor in [h_bin - 0.1, h_bin + 0.1]:
                    total += line_h_bins.get(round(neighbor, 1), 0)
                if total > best_line_count:
                    best_line_count = total
                    best_line_bin = h_bin
            
            # Collect all characters from body text lines (lines matching dominant cluster)
            body_line_indices = set()
            body_char_heights = []
            for i, mean_h in line_mean_heights:
                if abs(mean_h - best_line_bin) <= 0.3:  # Body text line
                    body_line_indices.add(i)
                    body_char_heights.extend(line_char_heights[i])
            
            if not body_char_heights or len(body_char_heights) < 5:
                body_char_heights = all_char_heights_mm
                body_line_indices = set(range(len(text_lines)))
                logger.info(f"  📐 Using all chars (no clear body text cluster)")
            else:
                logger.info(f"  📐 Body text: {len(body_line_indices)} lines, {len(body_char_heights)} chars, line_height_cluster={best_line_bin:.1f}mm")
                # Diagnostic: show which lines are body text and their height stats
                for i in sorted(list(body_line_indices)[:10]):
                    if i < len(line_char_heights) and line_char_heights[i]:
                        lh = line_char_heights[i]
                        logger.info(f"       Line {i}: {len(lh)} chars, mean={statistics.mean(lh):.3f}mm, median={statistics.median(lh):.3f}mm, range={min(lh):.2f}-{max(lh):.2f}mm")
            
            # ================================================================
            # PRIMARY: Try text-based x-height measurement (uses actual char identities)
            # ================================================================
            # PyMuPDF get_text("rawdict") gives per-character bboxes WITH the actual
            # character (e.g. 'a', 'x', 'H'). This lets us measure x-height directly
            # from lowercase letters — no clustering heuristics needed.
            text_xheight_mm = None
            text_capheight_mm = None
            try:
                doc2 = fitz.open(pdf_path)
                page2 = doc2.load_page(0)
                rawdict = page2.get_text("rawdict")
                
                # X-height chars: lowercase letters without ascenders/descenders
                XHEIGHT_CHARS = set('aceimnorsuvwxz')  # Added 'i' — common, reliable x-height char
                CAP_CHARS = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ')
                
                # STEP 1: Collect all spans in region, grouped by font size
                # This prevents header/title text from contaminating body text measurements
                spans_by_size = {}  # rounded pt size → list of spans
                
                for block in rawdict.get('blocks', []):
                    if block.get('type') != 0:
                        continue
                    for line in block.get('lines', []):
                        for span in line.get('spans', []):
                            sbbox = span.get('bbox', (0, 0, 0, 0))
                            if (sbbox[0] >= pt_xmin - 2 and sbbox[2] <= pt_xmax + 2 and
                                sbbox[1] >= pt_ymin - 2 and sbbox[3] <= pt_ymax + 2):
                                sz = round(span.get('size', 0), 1)
                                if sz > 0:
                                    spans_by_size.setdefault(sz, []).append(span)
                
                # STEP 2: Find the dominant (most common) font size = body text
                # Count chars per font size to find body text size
                size_char_counts = {}
                for sz, spans in spans_by_size.items():
                    total_chars = sum(len(sp.get('chars', [])) for sp in spans)
                    size_char_counts[sz] = total_chars
                
                if size_char_counts:
                    body_font_size = max(size_char_counts, key=size_char_counts.get)
                    logger.info(f"  📐 Font sizes in region: {dict(sorted(size_char_counts.items()))}")
                    logger.info(f"  📐 Body text font size: {body_font_size}pt ({size_char_counts[body_font_size]} chars)")
                    
                    # Only measure chars from body text font size (±0.5pt tolerance)
                    body_spans = []
                    for sz, spans in spans_by_size.items():
                        if abs(sz - body_font_size) <= 0.5:
                            body_spans.extend(spans)
                else:
                    body_spans = []
                
                # STEP 3: Measure x-height using origin (baseline) for precision
                # For x-height chars: measure from bbox TOP to BASELINE (origin_y)
                # This avoids below-baseline bbox padding that inflates full bbox height.
                # For cap chars: use full bbox height (caps sit on baseline, no descenders)
                xheight_pts = []
                xheight_bbox_pts = []  # full bbox for comparison/debugging
                cap_pts = []
                
                for span in body_spans:
                    origin = span.get('origin')
                    if isinstance(origin, (list, tuple)) and len(origin) >= 2:
                        baseline_y = origin[1]
                    else:
                        baseline_y = None
                    
                    for ch_info in span.get('chars', []):
                        c = ch_info.get('c', '')
                        cb = ch_info.get('bbox', (0, 0, 0, 0))
                        ch_height_pt = cb[3] - cb[1]  # full bbox height
                        
                        if ch_height_pt > 0.3 and ch_height_pt < 20:
                            if c in XHEIGHT_CHARS:
                                if baseline_y is not None and baseline_y > cb[1]:
                                    # PRECISE: measure from top of glyph to baseline
                                    # x-height chars don't extend below baseline,
                                    # so this gives true x-height without bbox padding
                                    xh = baseline_y - cb[1]
                                    if xh > 0.3:
                                        xheight_pts.append(xh)
                                        xheight_bbox_pts.append(ch_height_pt)
                                else:
                                    # Fallback: use full bbox (less precise)
                                    xheight_pts.append(ch_height_pt)
                                    xheight_bbox_pts.append(ch_height_pt)
                            elif c in CAP_CHARS:
                                cap_pts.append(ch_height_pt)
                
                doc2.close()
                
                if len(xheight_pts) >= 3:  # Lowered from 5: even 3 chars gives reliable median
                    text_xheight_mm = statistics.median(xheight_pts) / 72 * 25.4
                    bbox_xheight_mm = statistics.median(xheight_bbox_pts) / 72 * 25.4
                    logger.info(f"  📐 TEXT-BASED x-height: {text_xheight_mm:.3f}mm (from {len(xheight_pts)} lowercase chars, method=origin-based)")
                    if abs(text_xheight_mm - bbox_xheight_mm) > 0.01:
                        logger.info(f"  📐 TEXT-BASED bbox x-height would be: {bbox_xheight_mm:.3f}mm (origin-based saved {bbox_xheight_mm - text_xheight_mm:.3f}mm)")
                    if len(cap_pts) >= 3:
                        text_capheight_mm = statistics.median(cap_pts) / 72 * 25.4
                        logger.info(f"  📐 TEXT-BASED cap-height: {text_capheight_mm:.3f}mm (from {len(cap_pts)} uppercase chars)")
                        logger.info(f"  📐 TEXT-BASED cap/x ratio: {text_capheight_mm/text_xheight_mm:.3f}")
                    
                    # STEP 4: Glyph-based x-height refinement using embedded font metrics
                    # PyMuPDF Font.glyph_bbox() gives the precise glyph outline bbox,
                    # which avoids char-level bbox padding that inflates origin-based measurement.
                    # This is the GOLD STANDARD for x-height measurement.
                    try:
                        glyph_xheight_mm = None
                        # Get font info from the body text spans
                        if body_spans:
                            sample_span = body_spans[0]
                            font_name = sample_span.get('font', '')
                            font_size_pt = sample_span.get('size', 0)
                            
                            # Try to extract embedded font from PDF
                            fonts_on_page = page2.get_fonts(full=True)
                            font_xref = None
                            for f in fonts_on_page:
                                # f = (xref, ext, type, basefont, name, encoding, ...)
                                if len(f) >= 5 and (f[3] == font_name or f[4] == font_name):
                                    font_xref = f[0]
                                    break
                            
                            if font_xref:
                                font_data = doc2.extract_font(font_xref)
                                # font_data = (basename, ext, subtype, content_bytes)
                                if font_data and len(font_data) >= 4 and font_data[3]:
                                    font_obj = fitz.Font(fontbuffer=font_data[3])
                                    # Get glyph bbox for 'x' — height gives x-height ratio
                                    x_glyph_bbox = font_obj.glyph_bbox(ord('x'))
                                    if x_glyph_bbox and x_glyph_bbox.height > 0:
                                        # glyph_bbox is in font units (normalized to font size 1)
                                        # Actual x-height = glyph_bbox.height * font_size_pt
                                        glyph_xheight_pt = x_glyph_bbox.height * font_size_pt
                                        glyph_xheight_mm = glyph_xheight_pt / 72 * 25.4
                                        logger.info(f"  📐 GLYPH-BASED x-height: {glyph_xheight_mm:.3f}mm (font={font_name}, size={font_size_pt}pt, glyph_ratio={x_glyph_bbox.height:.4f})")
                                        
                                        # Also get cap-height from 'X' glyph
                                        cap_glyph_bbox = font_obj.glyph_bbox(ord('X'))
                                        if cap_glyph_bbox and cap_glyph_bbox.height > 0:
                                            glyph_capheight_mm = cap_glyph_bbox.height * font_size_pt / 72 * 25.4
                                            logger.info(f"  📐 GLYPH-BASED cap-height: {glyph_capheight_mm:.3f}mm (glyph_ratio={cap_glyph_bbox.height:.4f})")
                                        
                                        # Compare with origin-based measurement
                                        diff_pct = abs(text_xheight_mm - glyph_xheight_mm) / glyph_xheight_mm * 100 if glyph_xheight_mm > 0 else 0
                                        logger.info(f"  📐 Origin vs Glyph x-height difference: {diff_pct:.1f}% (origin={text_xheight_mm:.3f}mm, glyph={glyph_xheight_mm:.3f}mm)")
                                        
                                        # Prefer glyph-based, but cross-validate against origin-based
                                        if diff_pct > 20:
                                            logger.error(f"  ❌ GLYPH vs ORIGIN disagree by {diff_pct:.1f}%: glyph={glyph_xheight_mm:.3f}mm, origin={text_xheight_mm:.3f}mm")
                                            logger.error(f"     Possible subset font issue — using glyph (higher confidence) but flagging for review")
                                        logger.info(f"  📐 ⚡ Preferring GLYPH-BASED x-height (gold standard, origin diff={diff_pct:.1f}%)")
                                        text_xheight_mm = glyph_xheight_mm
                                        if cap_glyph_bbox and cap_glyph_bbox.height > 0:
                                            text_capheight_mm = glyph_capheight_mm
                                    else:
                                        logger.debug(f"  📐 Glyph bbox for 'x' not available or zero height")
                                else:
                                    logger.debug(f"  📐 Could not extract font buffer for {font_name} (xref={font_xref})")
                            else:
                                logger.debug(f"  📐 Font '{font_name}' not found in page fonts for glyph-based measurement")
                    except Exception as glyph_err:
                        logger.warning(f"  ⚠️  Glyph-based measurement failed for font: {glyph_err}")
                        logger.warning(f"     Falling back to origin-based measurement (less accurate)")
                    
                else:
                    logger.warning(f"  ⚠️  Text-based measurement FAILED: only {len(xheight_pts)} x-height chars found (need ≥3) — falling back to vector clustering (less reliable)")
                    if size_char_counts:
                        logger.warning(f"  ⚠️  Hint: {sum(size_char_counts.values())} total chars in region, but only {len(xheight_pts)} are x-height lowercase (chars in set: aceimnorsuvwxz)")
            except Exception as e:
                logger.debug(f"  📐 Text-based measurement failed: {e}")
            
            # ================================================================
            # FALLBACK: Bimodal height clustering from vector paths
            # ================================================================
            # CLP requires X-HEIGHT (lowercase letters), not mean of all chars.
            # Mixed-case text has two clusters: short (x-height) and tall (caps/ascenders).
            # We use histogram peak detection to find both, then use x-height for compliance.
            
            # Filter out unreasonably small paths (subscripts, chemical formulas, dots)
            MIN_BODY_TEXT_HEIGHT = 0.5  # mm — anything smaller is not body text
            filtered_body_heights = [h for h in body_char_heights if h >= MIN_BODY_TEXT_HEIGHT]
            if len(filtered_body_heights) >= 10:
                body_char_heights = filtered_body_heights
                logger.info(f"  📐 Filtered to {len(body_char_heights)} chars ≥{MIN_BODY_TEXT_HEIGHT}mm (removed {len(filtered_body_heights)} tiny glyphs)")
            
            # Build histogram of body char heights (0.02mm bins)
            height_bins = Counter(round(h, 2) for h in body_char_heights)
            
            # DEBUG: Show raw histogram (top 10 bins)
            logger.info(f"  🔬 DEBUG: Height histogram (top 10):")
            for h, c in height_bins.most_common(10):
                logger.info(f"       {h:.2f}mm: {c} chars")
            
            # Cluster nearby bins (within 0.08mm) into groups
            # Use MEDIAN for cluster center (prevents drift from outliers)
            sorted_heights = sorted(height_bins.keys())
            clusters = []  # list of [center, total_count, [member_heights]]
            for h in sorted_heights:
                count = height_bins[h]
                merged = False
                for cluster in clusters:
                    if abs(h - cluster[0]) <= 0.08:
                        cluster[1] += count
                        cluster[2].extend([h] * count)  # Add with multiplicity for proper median
                        merged = True
                        break
                if not merged:
                    clusters.append([h, count, [h] * count])
            
            # Recalculate centers as MEDIAN (prevents drift)
            for cluster in clusters:
                cluster[0] = statistics.median(cluster[2])
            
            # DEBUG: Show clusters after merging
            logger.info(f"  🔬 DEBUG: Clusters after merging:")
            for i, (center, count, members) in enumerate(clusters):
                unique_members = sorted(set(members))
                logger.info(f"       Cluster {i+1}: center={center:.3f}mm, count={count}, range={min(unique_members):.2f}-{max(unique_members):.2f}mm")
            
            # Convert to peaks: (center_height, total_count)
            peaks = [(c[0], c[1]) for c in clusters if c[1] >= 3]
            
            # Sort peaks by count (most common first)
            peaks.sort(key=lambda x: -x[1])
            
            # DEBUG: Show final peaks
            logger.info(f"  🔬 DEBUG: Final peaks (sorted by count):")
            for i, (h, c) in enumerate(peaks[:5]):
                logger.info(f"       Peak {i+1}: {h:.3f}mm, {c} chars")
            
            logger.info(f"  📐 Height clusters: {[(round(h,3), n) for h, n in peaks[:5]]}")
            
            # Determine x-height and cap-height
            xheight_mm = 0.0
            capheight_mm = 0.0
            measurement_approach = 'single-peak'
            
            # If text-based measurement succeeded, use it as primary
            if text_xheight_mm is not None:
                xheight_mm = text_xheight_mm
                capheight_mm = text_capheight_mm if text_capheight_mm else text_xheight_mm / 0.70
                measurement_approach = 'text-rawdict-xheight'
                logger.info(f"  📐 Using TEXT-BASED x-height: {xheight_mm:.3f}mm (most reliable)")
            elif len(peaks) >= 2:
                # Two or more peaks detected = potential bimodal distribution (mixed case)
                # IMPROVED ALGORITHM:
                # 1. Most frequent cluster = x-height (body text is most common)
                # 2. Find the next most frequent cluster with >0.25mm separation = cap-height
                # 3. Avoid edge cases (noise) by requiring >50 chars and reasonable height (0.8-3.0mm)
                
                # Sort peaks by character count (descending)
                peaks_by_count = sorted(peaks, key=lambda x: -x[1])
                
                # Use most frequent cluster as x-height
                xheight_mm = peaks_by_count[0][0]
                xheight_count = peaks_by_count[0][1]
                
                # Find best cap-height candidate
                best_capheight_h = None
                best_capheight_c = 0
                best_separation = 0.0
                
                for h, c in peaks_by_count[1:]:
                    separation = abs(h - xheight_mm)
                    # Good separation + reasonable frequency + plausible height
                    if separation > 0.25 and c > 50 and 0.8 < h < 3.0:
                        # Prefer the most frequent among valid candidates
                        if c > best_capheight_c:
                            best_capheight_h = h
                            best_capheight_c = c
                            best_separation = separation
                
                if best_capheight_h is not None:
                    # Found a clear bimodal split
                    capheight_mm = best_capheight_h
                    measurement_approach = 'bimodal-xheight'
                    logger.info(f"  📐 Bimodal distribution detected: x-height={xheight_mm:.3f}mm ({xheight_count} chars), cap-height={capheight_mm:.3f}mm ({best_capheight_c} chars), separation={best_separation:.3f}mm")
                else:
                    # No valid cap-height found — treat as single peak
                    capheight_mm = xheight_mm
                    measurement_approach = 'single-peak'
                    logger.info(f"  📐 Only one significant cluster detected: {xheight_mm:.3f}mm ({xheight_count} chars), treating as single peak (all lowercase or monotype)")
                    logger.info(f"  📐 Available peaks (top 5): {[(round(h, 3), c) for h, c in peaks_by_count[:5]]}")
            
            if measurement_approach == 'single-peak' and len(peaks) == 1:
                # Single peak — could be all-caps OR all-lowercase
                peak_h = peaks[0][0]
                
                # Heuristic: if peak > 1.7mm, likely all-caps (estimate x-height via 0.70 ratio)
                # Raised from 1.5mm to avoid false positives on legitimate x-heights near threshold
                if peak_h > 1.7:
                    capheight_mm = peak_h
                    xheight_mm = peak_h * 0.70  # Typical cap-to-x-height ratio for sans-serif
                    measurement_approach = 'all-caps-estimated'
                    logger.info(f"  📐 Single peak {peak_h:.3f}mm (>1.7mm) — likely all-caps, estimating x-height = {xheight_mm:.3f}mm (70% ratio)")
                else:
                    # Likely all-lowercase or small font
                    xheight_mm = peak_h
                    capheight_mm = peak_h / 0.70  # Estimate cap-height for spacing calc
                    measurement_approach = 'single-peak-lowercase'
                    logger.info(f"  📐 Single peak {peak_h:.3f}mm (≤1.7mm) — treating as x-height")
            
            if measurement_approach == 'single-peak' and len(peaks) == 0:
                # No clear peaks (very uniform or too few chars) — fall back to median
                xheight_mm = statistics.median(body_char_heights)
                capheight_mm = xheight_mm / 0.70
                measurement_approach = 'fallback-median'
                logger.warning(f"  ⚠️ No clear peaks in height distribution, using median: {xheight_mm:.3f}mm")
            
            # Final font size = x-height (CLP requirement)
            font_size_mm = xheight_mm
            median_font_mm = statistics.median(body_char_heights)
            
            # Cross-validation: compare text-based vs vector-based if both available
            if text_xheight_mm is not None and len(peaks) >= 1:
                vector_xheight = sorted([p[0] for p in peaks[:2]])[0] if len(peaks) >= 2 else peaks[0][0]
                disagreement_pct = abs(text_xheight_mm - vector_xheight) / text_xheight_mm if text_xheight_mm > 0 else 0
                if disagreement_pct > 0.15:
                    logger.error(f"  ❌ CROSS-VALIDATION FAILED: text-based ({text_xheight_mm:.3f}mm) vs vector ({vector_xheight:.3f}mm) disagree by {disagreement_pct:.0%}")
                    logger.error(f"     This suggests measurement instability — recommend human review")
                elif disagreement_pct > 0.05:
                    logger.warning(f"  ⚠️  Cross-validation: text ({text_xheight_mm:.3f}mm) vs vector ({vector_xheight:.3f}mm) differ by {disagreement_pct:.0%}")
                else:
                    logger.info(f"  ✓ Cross-validation: text ({text_xheight_mm:.3f}mm) vs vector ({vector_xheight:.3f}mm) agree within {disagreement_pct:.0%}")
            
            # ================================================================
            # LINE SPACING (CLP): Visible gap between lines
            # ================================================================
            # CLP Regulation 2024/2865: "The distance between two lines" = the visible
            # whitespace between the bottom of text in one line and top of text in the next.
            # This is NOT leading (baseline-to-baseline) but the inter-line GAP.
            # 
            # CRITICAL: Gap is measured from TALLEST chars (cap-height), not x-height.
            # The visible gap is between bottom of "H" on line N and top of "H" on line N+1,
            # so we subtract cap-height from center-to-center spacing.
            
            body_text_line_ys = sorted([line_y_centers_mm[i] for i in body_line_indices])
            
            line_distance_mm = 0.0
            center_to_center_mm = 0.0
            if len(body_text_line_ys) >= 2:
                spacings = [body_text_line_ys[i+1] - body_text_line_ys[i] 
                           for i in range(len(body_text_line_ys) - 1)]
                # Use the MODE of spacings (rounded to 0.1mm) for the most common spacing
                spacing_bins = Counter(round(s, 1) for s in spacings)
                most_common_spacing = spacing_bins.most_common(1)[0][0]
                # Average all spacings near the mode (±0.3mm)
                tight_spacings = [s for s in spacings if abs(s - most_common_spacing) <= 0.3]
                center_to_center_mm = statistics.mean(tight_spacings) if tight_spacings else statistics.median(spacings)
                
                # CLP line gap = center-to-center - X-HEIGHT (not cap-height)
                # CLP defines font size as x-height; "distance between lines" ≥ 120% of font size.
                # The practical gap measurement: c2c minus the dominant body text height (x-height),
                # since most characters are lowercase and that defines the visual line body.
                line_distance_mm = max(0, center_to_center_mm - font_size_mm)
                
                logger.info(f"  📐 Center-to-center: {center_to_center_mm:.3f}mm")
                logger.info(f"  📐 CLP line gap: {center_to_center_mm:.3f} - {font_size_mm:.3f} (x-height) = {line_distance_mm:.3f}mm")
                logger.info(f"  📐 Body text line spacings (c2c): {[round(s,2) for s in spacings]}")
            elif len(line_y_centers_mm) >= 2:
                spacings = [line_y_centers_mm[i+1] - line_y_centers_mm[i] 
                           for i in range(len(line_y_centers_mm) - 1)]
                center_to_center_mm = statistics.median(spacings)
                line_distance_mm = max(0, center_to_center_mm - font_size_mm)
            
            # Height distribution for logging
            height_dist = Counter(round(h, 2) for h in all_char_heights_mm)
            common_heights = height_dist.most_common(5)
            
            # Apply vertical scale factor (font height and line spacing are vertical)
            if vertical_scale != 1.0:
                font_size_mm_raw = font_size_mm
                median_font_mm_raw = median_font_mm
                line_distance_mm_raw = line_distance_mm
                font_size_mm *= vertical_scale
                median_font_mm *= vertical_scale
                line_distance_mm *= vertical_scale
                logger.info(f"  📏 Vertical scale {vertical_scale:.4f} applied:")
                logger.info(f"     Font: {font_size_mm_raw:.3f}mm → {font_size_mm:.3f}mm")
                logger.info(f"     Line gap: {line_distance_mm_raw:.3f}mm → {line_distance_mm:.3f}mm")
            
            logger.info(f"  ✅ PDF vector font measurement:")
            logger.info(f"     X-height (CLP metric): {font_size_mm:.3f}mm")
            logger.info(f"     Cap-height (for gap calc): {capheight_mm:.3f}mm")
            logger.info(f"     Median char height: {median_font_mm:.3f}mm")
            logger.info(f"     Measurement approach: {measurement_approach}")
            logger.info(f"     Characters measured: {len(all_char_heights_mm)}")
            logger.info(f"     Line spacing (gap): {line_distance_mm:.3f}mm ({len(text_lines)} lines)")
            logger.info(f"     Height distribution: {common_heights}")
            
            return {
                'font_size_mm': round(font_size_mm, 4),  # x-height (CLP requirement)
                'xheight_mm': round(xheight_mm, 4),
                'cap_height_mm': round(capheight_mm, 4),
                'font_size_mm_median': round(median_font_mm, 4),
                'line_distance_mm': round(line_distance_mm, 4),
                'center_to_center_mm': round(center_to_center_mm, 4),
                'measurement_confidence': 0.95,  # Vector measurement is high-confidence
                'measurement_method': f'pdf-vector-{measurement_approach}',
                'measurement_approach': measurement_approach,
                'characters_measured': len(all_char_heights_mm),
                'text_lines_found': len(text_lines),
                'height_peaks': [(round(h, 3), c) for h, c in peaks[:3]],  # Top 3 peaks for debugging
                'notes': f'Bimodal clustering: {measurement_approach}, {len(all_char_heights_mm)} chars across {len(text_lines)} lines'
            }
            
        except Exception as e:
            logger.warning(f"  ⚠️ PDF vector font measurement failed: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    # ========================================================================
    # STAGE 0: CALIBRATION
    # ========================================================================
    
    def calibrate_dpi_from_pdf(self, pdf_path: str) -> bool:
        """
        Calibrate DPI from PDF measurement lines using PyMuPDF (deterministic).
        
        Extracts horizontal lines from the PDF's vector content and matches them
        against known reference values. This is 100% deterministic — no Gemini needed.
        
        Returns:
            bool: True if calibration succeeded
        """
        try:
            doc = fitz.open(pdf_path)
            page = doc.load_page(0)
            
            # Extract all horizontal lines from PDF
            drawings = page.get_drawings()
            horiz_lines = []
            for d in drawings:
                for item in d["items"]:
                    if item[0] == "l":  # line object
                        p1, p2 = item[1], item[2]
                        if abs(p1.y - p2.y) < 2:  # roughly horizontal
                            length_pts = abs(p2.x - p1.x)
                            if length_pts > 50:  # significant length
                                length_mm = length_pts / 72 * 25.4
                                horiz_lines.append((length_pts, length_mm))
            
            if not horiz_lines:
                logger.info("  No vector measurement lines found in PDF, falling back to Gemini")
                return False
            
            # Sort by length, take the most common length (measurement references appear multiple times)
            from collections import Counter
            rounded_lengths = Counter(round(l[0], 0) for l in horiz_lines)
            most_common_length = rounded_lengths.most_common(1)[0][0]
            
            # Find lines matching most common length
            matching = [l for l in horiz_lines if abs(l[0] - most_common_length) < 2]
            avg_pts = sum(l[0] for l in matching) / len(matching)
            physical_mm = avg_pts / 72 * 25.4
            
            # Calculate scale: PDF points per mm
            pts_per_mm = avg_pts / physical_mm
            standard_pts_per_mm = 72 / 25.4
            scale_ratio = pts_per_mm / standard_pts_per_mm
            
            # True DPI = rendering DPI * scale ratio
            true_dpi = int(round(self.original_dpi * scale_ratio))
            
            self.calibration.true_dpi = true_dpi
            self.calibration.dpmm = true_dpi / 25.4
            self.calibration.is_calibrated = True
            
            logger.info(f"  ✓ PDF vector calibration: {len(horiz_lines)} lines found, reference={physical_mm:.2f}mm")
            logger.info(f"  ✓ Scale ratio: {scale_ratio:.4f} (1.0 = PDF is 1:1 physical)")
            logger.info(f"  ✓ Calibrated DPI: {true_dpi} DPI ({self.calibration.dpmm:.2f} px/mm)")
            doc.close()
            return True
            
        except Exception as e:
            logger.warning(f"  PDF vector calibration failed: {e}, falling back to Gemini")
            return False
    
    def calibrate_dpi(self, image: PIL_Image.Image, image_data: Dict) -> bool:
        """
        Attempt to calibrate DPI. 
        
        Priority:
        1. PDF vector extraction (deterministic, exact)
        2. Gemini vision (fallback for non-PDF images)
        
        Returns:
            bool: True if calibration succeeded, False if using default DPI
        """
        logger.info("Stage 0: DPI Calibration")
        
        # Try PDF-based calibration first (if we have a PDF path)
        if hasattr(self, '_pdf_path') and self._pdf_path:
            if self.calibrate_dpi_from_pdf(self._pdf_path):
                return True
        
        prompt = """
CRITICAL: Find ALL measurement reference lines on this image (there may be multiple).

For EACH line you find:
- It must have a visible mm label next to or on the line
- Measure exact pixel coordinates of line start and end
- READ THE NUMERIC VALUE VERY CAREFULLY (critical for accuracy)

IMPORTANT: When reading the mm value, report EXACTLY what you see:
- Look closely at the numeric label (e.g., "636.07", "662.90", etc.)
- Read EACH DIGIT carefully: tens, ones, decimal point, decimal places
- Do NOT approximate or round
- If label says "636.07mm", report 636.07 (not 637, not 602.6)
- If you see "662.90mm", report 662.90 exactly

Return a list of ALL measurement lines found, ordered by length (longest first).
DO NOT filter—return them ALL so we can analyze all references.

For each line:
- start_point: {x, y} pixel coordinates where line begins
- end_point: {x, y} pixel coordinates where line ends
- value_mm: The EXACT numeric mm value labeled on this line (read carefully!)
- confidence: How confident (0.0 to 1.0)

If no measurement lines found, return empty array.
"""
        
        calibration_schema = {
            "type": "object",
            "properties": {
                "measurement_lines": {
                    "type": "array",
                    "items": {
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
                    "description": "All measurement lines found, ordered by length (longest first)"
                }
            },
            "required": ["measurement_lines"]
        }
        
        try:
            logger.debug(f"  📋 Calibration prompt length: {len(prompt)} chars")
            # Pass original dimensions for scale factor tracking
            img_w, img_h = self._image_size
            response_text = self.gemini.analyze_image(
                image_data, prompt, calibration_schema,
                original_width=img_w, original_height=img_h,
                temperature=0.3  # Low temp for precise coordinate detection (research: 0.0-0.5 optimal)
            )
            
            logger.debug(f"  📥 Raw response: {response_text[:500]}")  # First 500 chars
            response = json.loads(response_text)
            logger.debug(f"  ✓ Parsed JSON successfully")
            
            lines = response.get("measurement_lines", [])
            if lines:
                logger.info(f"  🎯 Found {len(lines)} measurement line(s)")
                for i, l in enumerate(lines):
                    dist = ((l["end_point"]["x"] - l["start_point"]["x"])**2 + 
                           (l["end_point"]["y"] - l["start_point"]["y"])**2)**0.5
                    logger.info(f"      Line {i+1}: {dist:.1f}px for {l['value_mm']}mm (confidence: {l.get('confidence', 0.5):.0%})")
                
                # Find longest line (most reliable reference)
                best_line = max(lines, key=lambda l: 
                    ((l["end_point"]["x"] - l["start_point"]["x"])**2 + 
                     (l["end_point"]["y"] - l["start_point"]["y"])**2)**0.5)
                logger.info(f"  ✓ Selecting longest: {best_line['value_mm']}mm")
                
                line_data = best_line
                px_distance = ((line_data["end_point"]["x"] - line_data["start_point"]["x"])**2 + 
                              (line_data["end_point"]["y"] - line_data["start_point"]["y"])**2)**0.5
                
                logger.info(f"  📏 Measurement: start=({line_data['start_point']['x']}, {line_data['start_point']['y']}), end=({line_data['end_point']['x']}, {line_data['end_point']['y']}), distance={px_distance:.1f}px")
                
                # CRITICAL: Scale coordinates back to original image space
                # Gemini returns coordinates in its internally-resized space
                scale_factor = self.gemini._last_image_scale_factor
                if scale_factor != 1.0:
                    logger.info(f"  🔄 Scaling calibration coordinates by {scale_factor:.4f}")
                    line_data["start_point"]["x"] = int(round(line_data["start_point"]["x"] * scale_factor))
                    line_data["start_point"]["y"] = int(round(line_data["start_point"]["y"] * scale_factor))
                    line_data["end_point"]["x"] = int(round(line_data["end_point"]["x"] * scale_factor))
                    line_data["end_point"]["y"] = int(round(line_data["end_point"]["y"] * scale_factor))
                    px_distance_scaled = ((line_data["end_point"]["x"] - line_data["start_point"]["x"])**2 + 
                                         (line_data["end_point"]["y"] - line_data["start_point"]["y"])**2)**0.5
                    logger.info(f"  ✓ Scaled measurement line: start=({line_data['start_point']['x']}, {line_data['start_point']['y']}), end=({line_data['end_point']['x']}, {line_data['end_point']['y']}), distance={px_distance_scaled:.1f}px, value={line_data['value_mm']}mm")
                
                line = MeasurementLine(
                    start_point=Point(**line_data["start_point"]),
                    end_point=Point(**line_data["end_point"]),
                    value_mm=line_data["value_mm"],
                    confidence=line_data.get("confidence", 0.8)
                )
                
                if self.calibration.update(line):
                    # SANITY CHECK: Detect implausible calibration results
                    # Typical label scans: 150-600 DPI. Outside this range suggests error.
                    if self.calibration.true_dpi < 50 or self.calibration.true_dpi > 1200:
                        logger.warning(f"  ⚠️  Calibrated DPI ({self.calibration.true_dpi}) is outside expected range (50-1200)")
                        logger.warning(f"      This might indicate a failed calibration. Consider manual review.")
                        logger.warning(f"      Measurement line: {line.value_mm}mm across {((line.end_point.x - line.start_point.x)**2 + (line.end_point.y - line.start_point.y)**2)**0.5:.1f}px")
                    
                    logger.info(f"✓ Calibration successful: {self.calibration.true_dpi} DPI")
                    return True
            
            logger.info(f"✗ No measurement lines found (response: {response}), using default {self.original_dpi} DPI")
            return False
            
        except json.JSONDecodeError as e:
            logger.warning(f"Calibration failed (JSON parse): {e}")
            logger.warning(f"  Raw response was: {response_text[:200]}")
            logger.warning(f"  Using default DPI: {self.original_dpi}")
            return False
        except (APIError, KeyError) as e:
            logger.warning(f"Calibration failed: {type(e).__name__}: {e}, using default DPI")
            return False
        except Exception as e:
            logger.error(f"Unexpected error in calibration: {type(e).__name__}: {e}")
            logger.error("Stack trace:", exc_info=True)
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
            
        except (APIError, json.JSONDecodeError, KeyError) as e:
            logger.error(f"Rough detection failed: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error in rough detection: {type(e).__name__}: {e}")
            logger.error("Stack trace:", exc_info=True)
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
            
        except (APIError, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Boundary refinement failed for '{label}': {e}, using rough boundaries")
            return region
        except Exception as e:
            logger.error(f"Unexpected error in boundary refinement for '{label}': {type(e).__name__}: {e}")
            logger.error("Stack trace:", exc_info=True)
            return region
    
    # ========================================================================
    # STAGE 3: CLP COMPLIANCE VALIDATION
    # ========================================================================
    
    def validate_clp_compliance(self, image_data: Dict, region: Dict, cropped_image: PIL_Image.Image, package_size_ml: int = 500, is_inner_packaging: bool = False) -> Dict:
        """
        Stage 3: Two-layer CLP compliance validation
        
        Implements EU Regulation 1272/2008 (CLP) CLP rules:
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
            # ================================================================
            # PRIORITY: Try PDF vector measurement first (deterministic, exact)
            # ================================================================
            pdf_vector_measurements = None
            if hasattr(self, '_pdf_path') and self._pdf_path:
                pdf_vector_measurements = self.measure_font_from_pdf_vectors(
                    self._pdf_path, region['rect']
                )
            
            if pdf_vector_measurements and pdf_vector_measurements.get('font_size_mm', 0) > 0:
                # Success! Use deterministic vector measurements
                logger.info(f"  ✅ Using PDF vector measurements (deterministic, no Gemini needed for font size)")
                
                # We still need Gemini for COLOR assessment (background, text color, contrast)
                # but NOT for font size measurement
                color_prompt = """Analyze ONLY the colors of this CLP label region:
1. What is the background color? (e.g., white, yellow, orange, dark purple)
2. What is the text color? (e.g., black, white, dark blue)
3. Is the contrast between text and background sufficient for readability?

Report ONLY colors and contrast. Do NOT measure font sizes."""
                
                color_schema = {
                    "type": "object",
                    "properties": {
                        "background_color": {"type": "string"},
                        "text_color": {"type": "string"},
                        "contrast_assessment": {"type": "string", "enum": ["high", "medium", "low"]}
                    },
                    "required": ["background_color", "text_color", "contrast_assessment"]
                }
                
                # Convert cropped region for color analysis
                buffered = BytesIO()
                cropped_image.save(buffered, format="JPEG")
                cropped_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
                cropped_data = {"inline_data": {"mime_type": "image/jpeg", "data": cropped_b64}}
                
                crop_w, crop_h = cropped_image.width, cropped_image.height
                color_response = self.gemini.analyze_image(
                    cropped_data, color_prompt, color_schema,
                    original_width=crop_w, original_height=crop_h,
                    temperature=0.2
                )
                color_data = json.loads(color_response)
                
                # Merge vector measurements with color data
                measurements = {
                    'font_size_mm': pdf_vector_measurements['font_size_mm'],
                    'font_size_pixels': 0,  # Not applicable for vector measurement
                    'line_distance_mm': pdf_vector_measurements['line_distance_mm'],
                    'line_distance_pixels': 0,
                    'background_color': color_data.get('background_color', 'unknown'),
                    'text_color': color_data.get('text_color', 'unknown'),
                    'contrast_assessment': color_data.get('contrast_assessment', 'unknown'),
                    'measurement_confidence': pdf_vector_measurements['measurement_confidence'],
                    'measurement_method': pdf_vector_measurements['measurement_method'],
                    'notes': pdf_vector_measurements.get('notes', ''),
                    'characters_measured': pdf_vector_measurements.get('characters_measured', 0),
                    'text_lines_found': pdf_vector_measurements.get('text_lines_found', 0),
                }
                
                font_mm = measurements['font_size_mm']
                line_dist_mm = measurements['line_distance_mm']
                meas_conf = measurements['measurement_confidence']
                
                logger.info(f"  ✓ PDF Vector measurements (no correction factor — x-height is CLP metric):")
                logger.info(f"    Font (x-height)={font_mm:.2f}mm, Line gap={line_dist_mm:.2f}mm")
                logger.info(f"    BG={measurements['background_color']}, Contrast={measurements['contrast_assessment']}")
                
                # Skip all the Gemini font measurement, scale factor, correction factor logic
                # Go directly to rule validation
                val_time = time_module.time() - val_start
                rule_results = validate_measurements_against_rules(
                    measurements, package_size_ml, is_inner_packaging
                )
                
                return {
                    "measurements": measurements,
                    "rule_results": rule_results,
                    "overall_compliance": rule_results.get("overall_compliance", "FAIL"),
                    "measurement_time_seconds": round(val_time, 2),
                }
            
            # ================================================================
            # FALLBACK: Gemini-based measurement (for non-PDF images)
            # ================================================================
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
            
            # Pass CROPPED image dimensions for scale factor calculation.
            # The cropped image is what Gemini actually receives, so scale factor
            # must be based on the crop dimensions (not the full original image).
            crop_w, crop_h = cropped_image.width, cropped_image.height
            
            # Use configured Gemini model for font measurement (Stage 3 is most critical for accuracy)
            response_text = self.gemini.analyze_image(
                cropped_data,
                validation_prompt,
                CLP_VALIDATION_SCHEMA,
                original_width=crop_w,
                original_height=crop_h,
                temperature=0.2  # Very low temperature for deterministic precision
            )
            
            measurements = json.loads(response_text)
            
            # ⭐ X-HEIGHT CONFIDENCE-BASED CORRECTION
            # With improved prompt, Gemini should measure x-height correctly.
            # But if confidence is borderline, apply gentle correction to reduce upward bias.
            # This is empirically calibrated: vision models tend to measure ~2-3% high.
            
            def get_correction_factor(confidence: float, method: str = 'x-height-direct') -> float:
                """CLP compliance correction factor.
                
                EU Regulation 1272/2008 (CLP) defines font size as x-height
                (height of lowercase 'x'). No conversion to cap-height is needed.
                
                Previous code applied 1.483× to convert x-height → cap-height,
                but this was INCORRECT: CLP thresholds (1.2mm, 1.4mm, 1.8mm)
                are already x-height thresholds, not cap-height thresholds.
                
                Removed 2026-02-17: was inflating measurements by ~48%.
                """
                # No correction needed — x-height IS the compliance metric
                return 1.0
            
            # Safely extract measurements with numeric coercion
            # NOTE: Correction factor is applied AFTER scale factor (below),
            # so we extract raw values first and correct at the end.
            try:
                font_mm_raw = float(measurements.get('font_size_mm') or 0)
                meas_conf_raw = float(measurements.get('measurement_confidence') or 0)
                font_mm = font_mm_raw  # Will be corrected after scale factor
            except (ValueError, TypeError):
                font_mm = 0
                font_mm_raw = 0
                meas_conf_raw = 0
            
            try:
                font_px = float(measurements.get('font_size_pixels') or 0)
            except (ValueError, TypeError):
                font_px = 0
            
            # Calculate what cap-height would be (for debugging only — NOT used for compliance)
            # Cap-height is typically 1.43x x-height (1/0.70)
            estimated_cap_height_mm = font_mm_raw / 0.70 if font_mm_raw > 0 else 0
                
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
            
            # ⭐ CHECK: If Gemini provided estimated_xheight_mm (for cap-height-estimated method),
            # we'll use that DIRECTLY instead of applying manual correction
            estimated_xheight_mm = None
            try:
                estimated_xheight_mm = float(measurements.get('estimated_xheight_mm') or 0)
                if estimated_xheight_mm > 0:
                    logger.info(f"  ℹ️  Gemini provided estimated x-height: {estimated_xheight_mm:.4f}mm")
            except (ValueError, TypeError):
                estimated_xheight_mm = None
            
            # CRITICAL FIX: Apply scale factor to pixel measurements
            # If Gemini resized the image, pixel coordinates are in resized space.
            # We must scale them back to original image space before mm conversion.
            # The scale_factor is calculated from original image dimensions passed above.
            scale_factor = self.gemini._last_image_scale_factor
            if scale_factor != 1.0:
                logger.info(f"  🔄 Scaling measurements by {scale_factor:.4f} (Gemini resized image)")
                # Scale pixel values back to original image space
                font_px_original = font_px * scale_factor
                line_dist_px_original = line_dist_px * scale_factor
                
                # Recalculate mm using scaled pixels and original calibration
                if font_px_original > 0:
                    font_mm = font_px_original / self.calibration.dpmm
                    logger.info(f"    Scaled font: {font_px:.1f}px → {font_px_original:.1f}px → {font_mm:.4f}mm")
                
                if line_dist_px_original > 0:
                    line_dist_mm = line_dist_px_original / self.calibration.dpmm
                    logger.info(f"    Scaled line distance: {line_dist_px:.1f}px → {line_dist_px_original:.1f}px → {line_dist_mm:.4f}mm")
                
                # CRITICAL: Update measurements dict with scaled values for rule validation
                # Without this, validate_measurements_against_rules() uses unscaled values!
                measurements['font_size_mm'] = font_mm
                measurements['line_distance_mm'] = line_dist_mm
                measurements['font_size_pixels_original'] = font_px
                measurements['font_size_pixels_scaled'] = font_px_original
                measurements['line_distance_pixels_original'] = line_dist_px
                measurements['line_distance_pixels_scaled'] = line_dist_px_original
                measurements['scale_factor_applied'] = scale_factor
            
            # ⭐ Apply x-height correction LAST (after scale factor recalculation)
            # PRIORITY: Use Gemini's estimated_xheight_mm if provided (new smart method)
            font_mm_before_correction = font_mm
            measurement_method = measurements.get('measurement_method', 'x-height-direct')
            
            if estimated_xheight_mm and estimated_xheight_mm > 0:
                # BEST: Use Gemini's explicit estimate (already includes 0.70× correction)
                font_mm = estimated_xheight_mm
                correction = 1.0  # Already corrected by Gemini
                logger.info(f"  ✓ Using Gemini's estimated x-height: {font_mm_before_correction:.4f}mm → {font_mm:.4f}mm")
            else:
                # FALLBACK: Apply automatic correction based on method + confidence
                correction = get_correction_factor(meas_conf_raw, measurement_method)
                font_mm = font_mm * correction
                if correction != 1.0:
                    logger.info(f"  🔧 Applied correction ({correction:.2f}x, method={measurement_method}): {font_mm_before_correction:.4f}mm → {font_mm:.4f}mm (confidence: {meas_conf_raw:.0%})")
            
            # CRITICAL: Update measurements dict with corrected value for rule validation
            measurements['font_size_mm'] = font_mm
            measurements['font_size_mm_before_correction'] = font_mm_before_correction
            measurements['correction_factor_applied'] = correction
            
            if correction == 1.0 and not estimated_xheight_mm:
                logger.info(f"  ✓ X-height measured (no correction needed): {font_mm:.4f}mm (confidence: {meas_conf_raw:.0%})")
            
            # Get actual cropped image dimensions for logging
            cropped_w, cropped_h = cropped_image.width, cropped_image.height
            
            # DEBUG: Log all values for troubleshooting
            measurement_method = measurements.get('measurement_method', 'x-height')
            logger.info(f"  ✓ Gemini measurements (X-HEIGHT OPTIMIZED):")
            logger.info(f"    [CALIBRATION] DPI={self.calibration.true_dpi}, dpmm={self.calibration.dpmm:.4f}, calibrated={self.calibration.is_calibrated}")
            logger.info(f"    [CROP] {cropped_w}×{cropped_h}px")
            logger.info(f"    [MEASUREMENT] Font: {font_px:.1f}px (raw: {font_mm_raw:.4f}mm) → corrected: {font_mm:.4f}mm (method: {measurement_method})")
            logger.info(f"    [MEASUREMENT] Line: {line_dist_px:.1f}px → {line_dist_mm:.4f}mm")
            logger.info(f"    [CAP-HEIGHT DEBUG] Raw x-height={font_mm_raw:.4f}mm, Est. Cap-Height (raw/0.70)={estimated_cap_height_mm:.4f}mm, Final={font_mm:.4f}mm")
            logger.info(f"    [CONFIDENCE] measurement={meas_conf_raw:.0%}, x-height correction applied={meas_conf_raw >= 0.7}")
            
            logger.info(f"    [SUMMARY] Font={font_mm:.2f}mm, Line={line_dist_mm:.2f}mm, BG={measurements.get('background_color', '?')}, Contrast={measurements.get('contrast_assessment', '?')}")
            
            # Extract and log cap-height if mentioned in notes
            notes_text = measurements.get('notes', '')
            if 'cap-height' in notes_text.lower():
                logger.info(f"    [CAP-HEIGHT INFO] Found in notes: {notes_text}")
            
            if measurements.get('notes'):
                logger.info(f"    [NOTES] {measurements.get('notes')}")
            
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

        # Stage 0a: Calibrate DPI (skip for PDFs — vector measurement in Stage 3 is exact)
        if hasattr(self, '_pdf_path') and self._pdf_path:
            logger.info("Stage 0: Skipping DPI calibration (PDF vector measurement will be used in Stage 3)")
            self.calibration.true_dpi = self.original_dpi
            self.calibration.dpmm = self.original_dpi / 25.4
            self.calibration.is_calibrated = True
        else:
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
                
                # Validate crop is non-empty and meets minimum size
                crop_width = xmax - xmin
                crop_height = ymax - ymin
                if crop_width <= 0 or crop_height <= 0:
                    logger.warning(f"Skipping region '{region['label']}': invalid crop bounds ({xmin},{ymin})-({xmax},{ymax})")
                    region["compliance_check"] = {
                        "error": "Invalid crop coordinates (zero or negative dimensions)",
                        "overall_compliance": "SKIP"
                    }
                    continue
                
                # Check minimum crop size (at least 20x20 pixels to be measurable)
                if crop_width < 20 or crop_height < 20:
                    logger.warning(f"Skipping region '{region['label']}': crop too small ({crop_width}×{crop_height}px) - not measurable")
                    region["compliance_check"] = {
                        "error": f"Crop too small ({crop_width}×{crop_height}px) - minimum 20×20px required",
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
                    # Ignore low DPI values (72/96) - they're JFIF/PNG defaults
                    image_dpi = dpi  # Default
                    if hasattr(img, 'info') and 'dpi' in img.info:
                        dpi_tuple = img.info['dpi']
                        raw_dpi = None
                        if isinstance(dpi_tuple, (tuple, list)) and len(dpi_tuple) >= 1:
                            raw_dpi = int(dpi_tuple[0])
                        elif isinstance(dpi_tuple, (int, float)):
                            raw_dpi = int(dpi_tuple)
                        if raw_dpi and raw_dpi >= 150:
                            image_dpi = raw_dpi

                image_data = image_to_base64(img)
                analyzer = LabelAnalyzer(
                    project_id, dpi=image_dpi, cache_dir=cache_dir,
                    confidence_weights=confidence_weights,
                    use_cache=use_cache,
                )
                # Pass PDF path for deterministic DPI calibration
                if path.lower().endswith(".pdf"):
                    analyzer._pdf_path = path
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
                try:
                    result = future.result()
                except Exception as exc:
                    # Catch any exceptions from the future itself (e.g., TimeoutError)
                    logger.error(f"Future execution failed for index {idx}: {exc}")
                    result = BatchResult(
                        path=image_paths[idx] if idx < len(image_paths) else f"item_{idx}",
                        error=exc,
                        elapsed_seconds=0
                    )
                results[idx] = result
                
                # Log result and invoke callback
                if result.success:
                    logger.info(f"  ✓ [{idx+1}/{total}] {result.path}: {len(result.parts)} parts in {result.elapsed_seconds:.1f}s")
                else:
                    logger.error(f"  ✗ [{idx+1}/{total}] {result.path}: FAILED in {result.elapsed_seconds:.1f}s")
                
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
                    "needs_review": part.needs_human_review(),
                    "compliance_check": part.compliance_check
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
            writer = csv.DictWriter(f, fieldnames=[
                "label", "classification", "confidence", "compliant", "needs_review",
                "font_size_mm", "line_distance_mm", "measurement_confidence",
                "overall_compliance", "compliance_confidence", "x_min", "y_min", "x_max", "y_max"
            ])
            writer.writeheader()
            for part in self.detected_parts:
                # Extract measurements from compliance check if available
                # Defensive: handle both nested ("measurements" key) and direct storage
                compliance = part.compliance_check or {}
                measurements = compliance.get("measurements", {}) or {}
                
                # Safe numeric extraction with fallback
                def safe_float(value, decimals=4):
                    """Safely convert to float string or empty string."""
                    if value is None:
                        return ""
                    try:
                        return f"{float(value):.{decimals}f}"
                    except (ValueError, TypeError):
                        return ""
                
                def safe_percent(value):
                    """Safely convert to percentage string or empty string."""
                    if value is None:
                        return ""
                    try:
                        return f"{float(value):.2%}"
                    except (ValueError, TypeError):
                        return ""
                
                writer.writerow({
                    "label": part.label,
                    "classification": part.classification.value,
                    "confidence": f"{part.confidence:.2%}",
                    "compliant": part.is_compliant(),
                    "needs_review": part.needs_human_review(),
                    "font_size_mm": safe_float(measurements.get('font_size_mm')),
                    "line_distance_mm": safe_float(measurements.get('line_distance_mm')),
                    "measurement_confidence": safe_percent(measurements.get('measurement_confidence')),
                    "overall_compliance": compliance.get("overall_compliance", ""),
                    "compliance_confidence": safe_percent(compliance.get("compliance_confidence")),
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
                    
                    # Add label with measurements at center
                    center_x = (xmin + xmax) // 2
                    center_y = (ymin + ymax) // 2
                    text = f"{part.label}\n({part.confidence:.0%})"
                    
                    # Add font size measurement if available
                    if part.compliance_check:
                        measurements = part.compliance_check.get("measurements", {}) or {}
                        font_mm = measurements.get("font_size_mm")
                        overall = part.compliance_check.get("overall_compliance", "")
                        
                        # Safely format font measurement
                        try:
                            if font_mm and isinstance(font_mm, (int, float)) and float(font_mm) > 0:
                                text += f"\n{float(font_mm):.2f}mm [{overall}]"
                        except (ValueError, TypeError):
                            pass  # Skip if measurement cannot be formatted
                    
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
    # NOTE: 72 and 96 DPI are JFIF/PNG defaults, NOT real print DPI.
    # Photos from cameras/web almost always report 72 DPI but actual
    # print resolution is much higher. Only trust DPI >= 150.
    image_dpi = 300  # Default fallback
    if hasattr(img, 'info') and 'dpi' in img.info:
        dpi_tuple = img.info['dpi']
        raw_dpi = None
        if isinstance(dpi_tuple, (tuple, list)) and len(dpi_tuple) >= 1:
            raw_dpi = int(dpi_tuple[0])
        elif isinstance(dpi_tuple, (int, float)):
            raw_dpi = int(dpi_tuple)
        
        if raw_dpi and raw_dpi >= 150:
            image_dpi = raw_dpi
            logger.info(f"  ✓ Extracted DPI from image metadata: {image_dpi} DPI")
        elif raw_dpi:
            logger.info(f"  ⚠️  Ignoring low metadata DPI ({raw_dpi}) - likely JFIF default, using {image_dpi} DPI")
        else:
            logger.info(f"  ℹ️  No valid DPI in image metadata, using default: {image_dpi} DPI")
    else:
        logger.info(f"  ℹ️  No DPI in image metadata, using default: {image_dpi} DPI")
    
    image_data = image_to_base64(img)
    
    analyzer = LabelAnalyzer(project_id, dpi=image_dpi)
    # Pass PDF path for deterministic DPI calibration
    if image_path.lower().endswith(".pdf"):
        analyzer._pdf_path = image_path
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
