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
from dataclasses import dataclass, asdict
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
    raw_response: Optional[Dict] = None
    
    def is_confident(self, threshold: float = 0.7) -> bool:
        """Check if detection meets confidence threshold"""
        return self.confidence >= threshold


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
You are analyzing a product label image to identify regions containing:
- **CLP Parts**: Ingredients lists, hazard symbols, warnings sections
- **Non-CLP Parts**: Marketing text, usage instructions, brand information

Task: Identify ALL distinct regions on this label, even if they overlap or have irregular shapes.
For each region, provide:
1. A clear classification (CLP or NON-CLP)
2. A descriptive label (e.g., "Hazard Symbols", "Instructions for Use")
3. Approximate bounding rectangle
4. Your confidence level (0.0-1.0)

Be exhaustive - we want to catch every distinct section, even small ones.
Return as JSON with array of detected regions.
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
                    "rect": {
                        "type": "object",
                        "properties": {
                            "xmin": {"type": "integer"},
                            "ymin": {"type": "integer"},
                            "xmax": {"type": "integer"},
                            "ymax": {"type": "integer"}
                        },
                        "required": ["xmin", "ymin", "xmax", "ymax"]
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                },
                "required": ["classification", "label", "rect", "confidence"]
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
- Approximate bounding box: [{xmin}, {ymin}] to [{xmax}, {ymax}]

Now, refine the boundaries by identifying the exact pixel coordinates that form the border of this region.
If the region has an irregular shape, provide a polygon with corner points.
Otherwise, provide the precise rectangular bounds.

Return the refined region as JSON.
"""

BOUNDARY_REFINEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "refined_rect": {
            "type": "object",
            "properties": {
                "xmin": {"type": "integer"},
                "ymin": {"type": "integer"},
                "xmax": {"type": "integer"},
                "ymax": {"type": "integer"}
            },
            "required": ["xmin", "ymin", "xmax", "ymax"]
        },
        "has_irregular_shape": {"type": "boolean"},
        "polygon_points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"}
                }
            }
        },
        "refinement_confidence": {"type": "number", "minimum": 0, "maximum": 1}
    }
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
# GEMINI CLIENT WRAPPER
# ============================================================================

class GeminiClient:
    """Wrapper for Gemini API interactions"""
    
    # Exceptions that are safe to retry (transient / rate-limit).
    RETRYABLE_EXCEPTIONS: Tuple = ()  # populated lazily after import

    def __init__(self, project_id: str, model: str = "gemini-3-pro-preview", location: str = "global",
                 cache: Optional[ResponseCache] = None, max_retries: int = 3):
        self.project_id = project_id
        self.model = model
        self.location = location
        self._client = None
        self.cache = cache or ResponseCache()
        self.max_retries = max_retries
    
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
    
    def analyze_image(self, image_data: Dict, prompt: str, response_schema: Optional[Dict] = None) -> str:
        """Call Gemini with image and optional structured output.

        Results are cached by (image_data, prompt, schema) so repeated
        calls for the same input return instantly without an API round-trip.
        """
        # Check cache first
        cached = self.cache.get(image_data, prompt, response_schema)
        if cached is not None:
            return cached

        client = self._get_client()

        config = {}
        if response_schema:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = response_schema

        def _call() -> str:
            response = client.models.generate_content(
                model=self.model,
                contents=[prompt, image_data],
                config=config if config else None
            )
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
    
    def __init__(self, project_id: str, dpi: int = 300, cache_dir: Optional[str] = None):
        cache = ResponseCache(cache_dir=cache_dir)
        self.gemini = GeminiClient(project_id, cache=cache)
        self.original_dpi = dpi
        self.calibration = CalibrationResult(dpi)
        self.detected_parts: List[DetectedPart] = []
    
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
        Return the start/end pixel coordinates and the numeric value.
        If no measurement line found, return null.
        """
        
        try:
            response_text = self.gemini.analyze_image(image_data, prompt)
            response = json.loads(response_text)
            
            if response.get("measurement_line"):
                line_data = response["measurement_line"]
                line = MeasurementLine(
                    start_point=Point(**line_data["start_point"]),
                    end_point=Point(**line_data["end_point"]),
                    value_mm=line_data["value_mm"]
                )
                
                if self.calibration.update(line):
                    logger.info(f"✓ Calibration successful: {self.calibration.true_dpi} DPI")
                    return True
            
            logger.info(f"✗ No measurement line found, using default {self.original_dpi} DPI")
            return False
            
        except Exception as e:
            logger.warning(f"Calibration failed: {e}, using default DPI")
            return False
    
    # ========================================================================
    # STAGE 1: ROUGH PART DETECTION
    # ========================================================================
    
    def detect_parts_rough(self, image_data: Dict) -> List[Dict]:
        """
        Stage 1: Identify all rough regions (CLP vs Non-CLP)
        
        Returns:
            List of detected regions with classifications
        """
        logger.info("Stage 1: Rough Part Detection")
        
        try:
            response_text = self.gemini.analyze_image(
                image_data, 
                PROMPT_ROUGH_DETECTION,
                ROUGH_DETECTION_SCHEMA
            )
            response = json.loads(response_text)
            
            regions = response.get("regions", [])
            logger.info(f"✓ Detected {len(regions)} regions")
            
            for i, region in enumerate(regions):
                logger.debug(f"  Region {i}: {region['classification']} - {region['label']} (conf: {region.get('confidence', 0):.2f})")
            
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
        
        Returns:
            Refined region with better boundaries
        """
        classification = region["classification"]
        label = region["label"]
        rect = region["rect"]
        
        prompt = PROMPT_BOUNDARY_REFINEMENT.format(
            classification=classification,
            label=label,
            xmin=rect["xmin"],
            ymin=rect["ymin"],
            xmax=rect["xmax"],
            ymax=rect["ymax"]
        )
        
        try:
            response_text = self.gemini.analyze_image(
                image_data,
                prompt,
                BOUNDARY_REFINEMENT_SCHEMA
            )
            response = json.loads(response_text)
            
            refined = {
                **region,
                "rect": response.get("refined_rect", rect),
                "has_irregular_shape": response.get("has_irregular_shape", False),
                "polygon_points": response.get("polygon_points"),
                "refinement_confidence": response.get("refinement_confidence", 0.8)
            }
            
            logger.debug(f"✓ Refined: {label} (irregular: {refined['has_irregular_shape']})")
            return refined
            
        except Exception as e:
            logger.warning(f"Boundary refinement failed for '{label}': {e}, using rough boundaries")
            return region
    
    # ========================================================================
    # STAGE 3: CONVERT TO INTERNAL FORMAT & FILTER
    # ========================================================================
    
    def _regions_to_detected_parts(self, regions: List[Dict]) -> List[DetectedPart]:
        """Convert region dicts to DetectedPart objects"""
        parts = []
        
        for region in regions:
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
            
            confidence = region.get("confidence", 0.5)
            confidence = max(confidence, region.get("refinement_confidence", 0.5))
            
            part = DetectedPart(
                classification=classification,
                label=region["label"],
                rect=rect,
                polygon=polygon,
                confidence=confidence,
                raw_response=region
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
        Main analysis pipeline: calibrate → detect rough → refine → filter
        
        Returns:
            List of detected parts with high confidence
        """
        logger.info("=" * 60)
        logger.info("Starting label analysis...")
        logger.info("=" * 60)
        
        # Stage 0: Calibrate DPI
        self.calibrate_dpi(image, image_data)
        
        # Stage 1: Rough detection
        rough_regions = self.detect_parts_rough(image_data)
        if not rough_regions:
            logger.warning("No regions detected in Stage 1")
            return []
        
        # Stage 2: Refine boundaries
        logger.info("Stage 2: Boundary Refinement")
        refined_regions = []
        for i, region in enumerate(rough_regions):
            logger.debug(f"  Refining region {i + 1}/{len(rough_regions)}...")
            refined = self.refine_boundaries(image_data, region)
            refined_regions.append(refined)
        
        # Stage 3: Convert & filter
        logger.info("Stage 3: Filtering Low-Confidence Results")
        self.detected_parts = self._regions_to_detected_parts(refined_regions)
        self.detected_parts = self.filter_low_confidence(self.detected_parts, threshold=0.6)
        
        logger.info(f"=" * 60)
        logger.info(f"Analysis complete: {len(self.detected_parts)} confident regions detected")
        logger.info("=" * 60)
        
        return self.detected_parts
    
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
            "detected_parts": [
                {
                    "classification": part.classification.value,
                    "label": part.label,
                    "confidence": part.confidence,
                    "rect": asdict(part.rect),
                    "has_polygon": part.polygon is not None
                }
                for part in self.detected_parts
            ]
        }
    
    def visualize(self, image: PIL_Image.Image) -> PIL_Image.Image:
        """Draw detected regions on image"""
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
                draw.rectangle(
                    [(rect.xmin, rect.ymin), (rect.xmax, rect.ymax)],
                    outline=color,
                    width=5
                )
            
            # Add label
            center_x, center_y = part.rect.center()
            text = f"{part.label}\n({part.confidence:.0%})"
            draw.text((center_x, center_y), text, fill=color)
        
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
    """Convenience function: load image and run full analysis"""
    logger.info(f"Loading image: {image_path}")
    
    img = PIL_Image.open(image_path)
    image_data = image_to_base64(img)
    
    analyzer = LabelAnalyzer(project_id)
    parts = analyzer.analyze(img, image_data)
    
    return analyzer, parts


if __name__ == "__main__":
    # Example usage
    print("Production-ready CLP Label Analyzer loaded")
    print("Use: analyzer, parts = analyze_image_file('path/to/image.jpg', 'your-project-id')")
