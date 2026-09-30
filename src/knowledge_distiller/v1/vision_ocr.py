"""Apple Vision OCR, preserving the same immutable original-pixel evidence plane."""
from io import BytesIO
from dataclasses import replace
import platform
import math

from .ocr import OcrError, OcrResult, decode_image, validate_lines


BOUNDARY_TOLERANCE_PIXELS = 1e-4

VISION_REVISION = 3  # Available on the supported macOS 14+ baseline.
VISION_MODEL = "VNRecognizeTextRequestRevision3"

# Diagnostic stages; the user-facing codes keep their existing meaning.
STAGE_REQUEST = "request_setup"
STAGE_HANDLER = "handler_init"
STAGE_PERFORM = "perform_request"
STAGE_PARSE = "parse_observations"
STAGE_COORDINATES = "validate_coordinates"


class VisionOcrRunner:
    """Synchronous requests owned by the existing single worker; no network/model cache."""

    def recognize_bytes(self, data: bytes, mime: str) -> OcrResult:
        image = decode_image(data, mime)
        width, height = image.size
        # A fresh PNG strips EXIF orientation and normalizes formats Vision does
        # not directly decode (e.g. WebP); no pixel resize/rotation is performed.
        encoded = BytesIO()
        image.save(encoded, format="PNG", exif=b"")
        try:
            import objc
            import Vision
            from Foundation import NSData
        except (ImportError, OSError) as error:
            raise OcrError("ocr_runtime_unavailable", stage="bridge_import", cause=error) from error
        with objc.autorelease_pool():
            stage = STAGE_REQUEST
            try:
                request = Vision.VNRecognizeTextRequest.alloc().init()
                request.setRevision_(VISION_REVISION)
                request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
                request.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en-US"])
                request.setUsesLanguageCorrection_(False)
                request.setAutomaticallyDetectsLanguage_(False)
                stage = STAGE_HANDLER
                payload = NSData.dataWithBytes_length_(encoded.getvalue(), encoded.tell())
                # nil options: PyObjC 12.2.2 on macOS 27 bridges an empty Python
                # dict into an NSDictionary Vision rejects with
                # NSInvalidArgumentException (BUG-20260922-01).
                handler = Vision.VNImageRequestHandler.alloc().initWithData_orientation_options_(
                    payload, 1, None)  # CGImagePropertyOrientation.up, matching decoded pixels.
                if handler is None:
                    raise OcrError("ocr_inference_failed", stage=stage)
                stage = STAGE_PERFORM
                succeeded, error = handler.performRequests_error_([request], None)
                if not succeeded or error is not None:
                    failure = OcrError("ocr_inference_failed", stage=stage)
                    failure.native_error = _native_error(error)
                    raise failure
                observations = request.results()
            except OcrError:
                raise
            except Exception as error:
                raise OcrError("ocr_inference_failed", stage=stage, cause=error) from error
            return _result(observations, width, height)


def _native_error(error):
    """NSError domain/code only; localized descriptions may echo inputs."""
    try:
        return {'domain': str(error.domain()), 'code': int(error.code())}
    except Exception:
        return None


def _result(observations, width, height):
    texts = []
    stage = STAGE_PARSE
    try:
        # nil is not a completed blank-image result; an actual empty NSArray is.
        if observations is None:
            raise ValueError
        texts, scores, polygons, alternatives, originals = [], [], [], [], []
        for observation in observations:
            candidates = observation.topCandidates_(3)
            if not candidates:
                raise ValueError
            candidate = candidates[0]
            texts.append(candidate.string())
            scores.append(candidate.confidence())
            alternatives.append(tuple(dict.fromkeys(c.string() for c in candidates[1:]
                if c.string() != candidate.string() and c.confidence() == candidate.confidence())))
            # Vision uses normalized bottom-left coordinates, our evidence uses
            # top-left original pixels. Keep the quadrilateral, including skew.
            points = (observation.topLeft(), observation.topRight(),
                      observation.bottomRight(), observation.bottomLeft())
            raw = tuple((point.x * width, (1 - point.y) * height) for point in points)
            if any(not math.isfinite(v) or v < -BOUNDARY_TOLERANCE_PIXELS
                   or v > bound + BOUNDARY_TOLERANCE_PIXELS
                   for x,y in raw for v,bound in ((x,width),(y,height))):
                # Keep the text for diagnostics, but let the shared validator
                # reject these coordinates. Never clamp a genuine violation.
                polygons.append(raw)
            else:
                polygons.append(tuple((min(width,max(0,x)), min(height,max(0,y))) for x,y in raw))
            originals.append(raw if raw != polygons[-1] else ())
        stage = STAGE_COORDINATES
        lines = validate_lines(texts, scores, polygons, width, height)
        lines = tuple(replace(line, alternatives=choices, original_polygon=raw)
                      for line, choices, raw in zip(lines, alternatives, originals, strict=True))
        return OcrResult(width, height, lines, engine="apple_vision",
                         runtime_version=platform.mac_ver()[0],
                         detection_model=VISION_MODEL, recognition_model=VISION_MODEL,
                         framework_version=platform.mac_ver()[0])
    except OcrError as error:
        if error.stage is None:
            error.stage = stage
        raise
    except (AttributeError, TypeError, ValueError, OverflowError) as error:
        failure = OcrError("ocr_invalid_output", stage=stage, cause=error)
        failure.line_diagnostics = [
            {'line_index': index, 'text': text if isinstance(text, str) else None,
             'text_available': isinstance(text, str) and bool(text.strip()),
             'evidence_valid': False, 'code': 'ocr_invalid_output'}
            for index, text in enumerate(texts)]
        raise failure from error
