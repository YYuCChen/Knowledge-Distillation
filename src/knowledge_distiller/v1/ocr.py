"""Shared OCR image/coordinate contract and the non-Mac Paddle implementation.

Mac uses Apple Vision; platform routing never retries with a different engine.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
import math
import sys
from numbers import Real
from typing import Callable


OCR_VERSION = "3.7.0"
PADDLEX_VERSION = "3.7.2"
PADDLE_VERSION = "3.3.0"
DETECTION_MODEL = "PP-OCRv6_small_det"
RECOGNITION_MODEL = "PP-OCRv6_small_rec"
_MIMES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP",
          "image/bmp": "BMP", "image/tiff": "TIFF"}


class OcrError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OcrLine:
    text: str
    polygon: tuple[tuple[float, float], ...]
    confidence: float
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class OcrResult:
    width: int
    height: int
    lines: tuple[OcrLine, ...]
    engine: str = "paddleocr"
    runtime_version: str = OCR_VERSION
    detection_model: str = DETECTION_MODEL
    recognition_model: str = RECOGNITION_MODEL
    framework_version: str = PADDLE_VERSION

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def default_ocr_runner():
    """Select the product's fixed platform engine without loading model runtimes."""
    if sys.platform == "darwin":
        from .vision_ocr import VisionOcrRunner
        return VisionOcrRunner()
    return PaddleOcrRunner()


class PaddleOcrRunner:
    """Lazy predictor, owned by the existing single worker (not thread-safe)."""

    def __init__(self, *, predictor_factory: Callable | None = None):
        self._factory = predictor_factory or _load_predictor
        self._predictor = None

    def recognize_bytes(self, data: bytes, mime: str) -> OcrResult:
        pixels, width, height = _decode(data, mime)
        if self._predictor is None:
            self._predictor = self._factory()
        try:
            results = self._predictor.predict(pixels)
        except Exception as error:
            raise OcrError("ocr_inference_failed") from error
        return _parse(results, width, height)


def _load_predictor():
    try:
        for distribution, expected in (("paddleocr", OCR_VERSION),
                                       ("paddlex", PADDLEX_VERSION),
                                       ("paddlepaddle", PADDLE_VERSION)):
            if version(distribution) != expected:
                raise OcrError("ocr_runtime_unavailable")
        from paddleocr import PaddleOCR
        from paddlex.utils.deps import DependencyError
    except OcrError:
        raise
    except (ImportError, PackageNotFoundError, OSError) as error:
        raise OcrError("ocr_runtime_unavailable") from error
    try:
        return PaddleOCR(text_detection_model_name=DETECTION_MODEL,
                         text_recognition_model_name=RECOGNITION_MODEL,
                         use_doc_orientation_classify=False, use_doc_unwarping=False,
                         use_textline_orientation=False, device="cpu")
    except (ImportError, DependencyError) as error:
        raise OcrError("ocr_runtime_unavailable") from error
    except Exception as error:
        # Initialization resolves/downloads model artifacts and loads their weights.
        raise OcrError("ocr_model_unavailable") from error


def _decode(data, mime):
    rgb = decode_image(data, mime)
    try:
        import numpy as np
    except ImportError as error:
        raise OcrError("ocr_runtime_unavailable") from error
    return np.asarray(rgb)[:, :, ::-1].copy(), *rgb.size  # Paddle expects BGR.


def decode_image(data, mime):
    """Decode to RGB on the original pixel plane, without applying EXIF rotation."""
    if not isinstance(data, bytes) or not data or mime not in _MIMES:
        raise OcrError("ocr_invalid_image")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as error:
        raise OcrError("ocr_runtime_unavailable") from error
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != _MIMES[mime] or getattr(image, "n_frames", 1) != 1:
                raise OcrError("ocr_invalid_image")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > 40_000_000:
                raise OcrError("ocr_invalid_image")
            # No EXIF transpose/unwarping: evidence boxes remain in original pixels.
            if "A" in image.getbands() or "transparency" in image.info:
                rgba = image.convert("RGBA")
                rgb = Image.new("RGB", image.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
            else:
                rgb = image.convert("RGB")
        return rgb
    except OcrError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise OcrError("ocr_invalid_image") from error


def _sequence(value):
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        raise ValueError
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError
    return float(value)


def _parse(results, width, height):
    try:
        if not isinstance(results, (list, tuple)) or len(results) != 1:
            raise ValueError
        result = results[0]
        if not isinstance(result, Mapping):
            raise ValueError
        return OcrResult(width, height, validate_lines(
            result["rec_texts"], result["rec_scores"], result["rec_polys"], width, height))
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise OcrError("ocr_invalid_output") from error


def validate_lines(texts, scores, polygons, width, height):
    """Validate engine output once before freezing text and original-pixel evidence."""
    try:
        texts, scores, polygons = map(_sequence, (texts, scores, polygons))
        if not len(texts) == len(scores) == len(polygons):
            raise ValueError
        lines = []
        for text, raw_score, raw_polygon in zip(texts, scores, polygons, strict=True):
            if not isinstance(text, str) or not text.strip():
                raise ValueError
            score = _number(raw_score)
            if not 0 <= score <= 1:
                raise ValueError
            polygon = []
            for raw_point in _sequence(raw_polygon):
                point = _sequence(raw_point)
                if len(point) != 2:
                    raise ValueError
                x, y = _number(point[0]), _number(point[1])
                if not (0 <= x <= width and 0 <= y <= height):
                    raise ValueError
                polygon.append((x, y))
            if len(polygon) < 4 or len(set(polygon)) != len(polygon):
                raise ValueError
            area = abs(sum(x * polygon[(i + 1) % len(polygon)][1]
                           - polygon[(i + 1) % len(polygon)][0] * y
                           for i, (x, y) in enumerate(polygon))) / 2
            if area <= 0:
                raise ValueError
            # Preserve the engine's reading order and exact text verbatim.
            lines.append(OcrLine(text, tuple(polygon), score))
        return tuple(lines)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise OcrError("ocr_invalid_output") from error
