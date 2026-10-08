"""Apple Vision OCR, preserving the same immutable original-pixel evidence plane."""
from io import BytesIO
from dataclasses import asdict, dataclass, replace
from importlib.metadata import distribution
from pathlib import Path
import hashlib
import json
import platform
import math
import subprocess
import sys
import weakref

from .ocr import OcrError, OcrResult, decode_image, validate_lines


# Apple Vision pads the box of text that touches the canvas edge, so a box can
# reach a few pixels past it (measured 3.1 px on a 1179x606 X screenshot,
# BUG-20260930-03). Such a box is clamped and its original polygon kept as
# evidence; anything farther out, or not finite, is still rejected.
BOUNDARY_TOLERANCE_RATIO = 0.01
BOUNDARY_TOLERANCE_MIN_PIXELS = 4.0


def _tolerance(bound):
    return max(BOUNDARY_TOLERANCE_MIN_PIXELS, bound * BOUNDARY_TOLERANCE_RATIO)

VISION_REVISION = 3  # Available on the supported macOS 14+ baseline.
VISION_MODEL = "VNRecognizeTextRequestRevision3"

# Diagnostic stages; the user-facing codes keep their existing meaning.
STAGE_REQUEST = "request_setup"
STAGE_HANDLER = "handler_init"
STAGE_PERFORM = "perform_request"
STAGE_PARSE = "parse_observations"
STAGE_COORDINATES = "validate_coordinates"

_RECEIPT_SEAL = object()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    def check(node):
        if node is None or type(node) in (bool, int):
            return
        if isinstance(node, str):
            if '\x00' in node:
                raise ValueError
            node.encode('utf-8', errors='strict')
        elif type(node) is float:
            if not math.isfinite(node):
                raise ValueError
        elif type(node) in (list, tuple):
            for child in node:
                check(child)
        elif type(node) is dict:
            for key, child in node.items():
                if type(key) is not str:
                    raise ValueError
                check(key)
                check(child)
        else:
            raise ValueError
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _output(result):
    # Never recurse through an opaque seal/weakref with dataclasses.asdict.
    payload = asdict(replace(result, receipt=None))
    payload.pop('receipt')
    return payload


@dataclass(frozen=True, slots=True, init=False)
class ImageOcrReceipt:
    """Same-result, in-process evidence, not a whole-source qualification.

    JSON cannot recreate authority. Arbitrary code controlling this process is
    outside this boundary; this is not remote or cryptographic attestation.
    """
    _seal: object
    _audit: bytes
    _result: object

    def __init__(self):
        raise TypeError('opaque_image_ocr_receipt')

    @property
    def audit_json(self):
        return self._audit.decode('utf-8')


def _input(data, mime, member_id):
    if (type(data) is not bytes or not data or type(mime) is not str
            or type(member_id) is not str or not member_id or '\x00' in member_id):
        raise ValueError
    member_id.encode('utf-8', errors='strict')
    return {'member_id': member_id, 'mime': mime, 'sha256': _sha(data), 'byte_count': len(data)}


def validate_image_receipt(result, data, mime, member_id):
    """Pure validation: never import a bridge, read an image or run OCR."""
    try:
        receipt = result.receipt
        if (type(result) is not OcrResult or type(receipt) is not ImageOcrReceipt
                or receipt._seal is not _RECEIPT_SEAL or receipt._result() is not result):
            raise ValueError
        audit = json.loads(receipt.audit_json)
        output = _output(result)
        if (audit['input'] != _input(data, mime, member_id)
                or audit['output_sha256'] != _sha(_json_bytes(output))
                or _json_bytes(audit['output']) != _json_bytes(output)):
            raise ValueError
        return audit
    except (AttributeError, TypeError, ValueError, KeyError, OverflowError) as error:
        raise OcrError('ocr_invalid_output', stage='receipt_validation') from error


def _bridge_origin(module, name):
    dist = distribution(name)
    if dist.version != '12.2.2':
        raise ValueError
    origin = Path(module.__file__).resolve(strict=True)
    matches = [p for p in dist.files or () if Path(dist.locate_file(p)).resolve() == origin]
    if len(matches) != 1 or not origin.is_file():
        raise ValueError
    before = origin.stat()
    digest = _sha(origin.read_bytes())
    after = origin.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if identity(before) != identity(after):
        raise ValueError
    return {'distribution': name, 'version': dist.version, 'origin': str(matches[0]),
            'origin_identity': identity(after), 'origin_sha256': digest}


def _environment(objc, Vision, Foundation):
    """Default native boundary; no caller-provided environment dict is trusted."""
    if sys.platform != 'darwin':
        raise ValueError
    bridges = [_bridge_origin(module, name) for module, name in (
        (objc, 'pyobjc-core'), (Vision, 'pyobjc-framework-Vision'),
        (Foundation, 'pyobjc-framework-Cocoa'))]
    classes = (Vision.VNRecognizeTextRequest, Vision.VNImageRequestHandler)
    root = Path('/System/Library/Frameworks/Vision.framework').resolve(strict=True)
    bundles = []
    for cls in classes:
        if not isinstance(cls, objc.objc_class):
            raise ValueError
        bundle = Foundation.NSBundle.bundleForClass_(cls)
        path = Path(str(bundle.bundlePath())).resolve(strict=True)
        identifier = str(bundle.bundleIdentifier())
        if path != root or identifier != 'com.apple.VN':
            raise ValueError
        info = bundle.infoDictionary()
        value = info.get('CFBundleVersion') if info is not None else None
        bundles.append({'identifier': identifier, 'path': str(path),
                        'version': str(value) if value is not None else 'unknown',
                        'provenance': 'system-framework'})
    if bundles[0] != bundles[1]:
        raise ValueError
    build = subprocess.run(['/usr/bin/sw_vers', '-buildVersion'], capture_output=True,
                           text=True, check=True, timeout=2).stdout.strip()
    product = platform.mac_ver()[0]
    if not build or len(build) > 128 or not build.isalnum() or not product:
        raise ValueError
    return {'platform': 'darwin', 'os_product_version': product, 'os_build': build,
            'bridges': bridges, 'pillow_version': distribution('Pillow').version,
            'framework': bundles[0]}


def _request_config(request):
    config = {'revision': int(request.revision()),
              'recognition_level': int(request.recognitionLevel()),
              'languages': [str(v) for v in request.recognitionLanguages()],
              'language_correction': bool(request.usesLanguageCorrection()),
              'automatic_language': bool(request.automaticallyDetectsLanguage()),
              'orientation': 1, 'handler_options': None}
    if config != {'revision': VISION_REVISION, 'recognition_level': 0,
                  'languages': ['zh-Hans', 'zh-Hant', 'en-US'],
                  'language_correction': False, 'automatic_language': False,
                  'orientation': 1, 'handler_options': None}:
        raise ValueError
    return config


def _observed(call):
    try:
        value = call()
        _json_bytes(value)
        return value
    except Exception:
        return None  # Conversion stays diagnostic; failure cannot issue a receipt.


class VisionOcrRunner:
    """Synchronous requests owned by the existing single worker; no network/model cache."""

    def __init__(self):
        self._receipt_diagnostic = 'unverified'

    @property
    def receipt_diagnostic(self):
        return self._receipt_diagnostic

    def recognize_bytes(self, data: bytes, mime: str) -> OcrResult:
        return self._recognize(data, mime)

    def recognize_member(self, data: bytes, mime: str, member_id: str) -> OcrResult:
        try:
            _input(data, mime, member_id)
        except (TypeError, ValueError) as error:
            raise OcrError('ocr_invalid_image', stage='member_binding') from error
        return self._recognize(data, mime, member_id=member_id)

    def _recognize(self, data, mime, *, member_id=None):
        self._receipt_diagnostic = 'unverified'
        image = decode_image(data, mime)
        width, height = image.size
        # A fresh PNG strips EXIF orientation and normalizes formats Vision does
        # not directly decode (e.g. WebP); no pixel resize/rotation is performed.
        encoded = BytesIO()
        image.save(encoded, format="PNG", exif=b"")
        try:
            import objc
            import Vision
            import Foundation
            from Foundation import NSData
        except (ImportError, OSError) as error:
            raise OcrError("ocr_runtime_unavailable", stage="bridge_import", cause=error) from error
        with objc.autorelease_pool():
            controlled = member_id is not None and type(self) is VisionOcrRunner
            plane = {'width': width, 'height': height, 'mode': image.mode,
                     'sha256': _sha(image.tobytes()), 'byte_count': width * height * 3,
                     'transparency': 'white-composite', 'exif_transpose': False,
                     'handler_png_sha256': _sha(encoded.getvalue()),
                     'handler_png_byte_count': encoded.tell()} if controlled else None
            before = _observed(lambda: _environment(objc, Vision, Foundation)) if controlled else None
            stage = STAGE_REQUEST
            try:
                request = Vision.VNRecognizeTextRequest.alloc().init()
                request.setRevision_(VISION_REVISION)
                request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
                request.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en-US"])
                request.setUsesLanguageCorrection_(False)
                request.setAutomaticallyDetectsLanguage_(False)
                config_before = _observed(lambda: _request_config(request)) if controlled else None
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
            result = _result(observations, width, height)
            if not controlled:
                return result
            config_after = _observed(lambda: _request_config(request))
            after = _observed(lambda: _environment(objc, Vision, Foundation))
            result = replace(result, framework_version=(before['framework']['version']
                             if before is not None else 'unknown'))
            if before is None or before != after or config_before is None or config_before != config_after:
                self._receipt_diagnostic = 'environment_or_configuration_unverified'
                return result
            try:
                output = _output(result)
                if (image.mode != 'RGB' or image.size != (width, height)
                        or plane['sha256'] != _sha(image.tobytes())
                        or plane['handler_png_sha256'] != _sha(encoded.getvalue())):
                    raise ValueError
                audit = {'protocol': 'image-ocr-execution-v1', 'input': _input(data, mime, member_id),
                    'pixel_plane': plane,
                    'environment': before, 'request': config_before,
                    'execution': {'perform_succeeded': True, 'observation_count': len(observations),
                        'outcome': 'blank' if not result.lines else 'nonblank',
                        'coordinates': 'validated'}, 'output': output,
                    'output_sha256': _sha(_json_bytes(output))}
                receipt = object.__new__(ImageOcrReceipt)
                result = replace(result, receipt=receipt)
                object.__setattr__(receipt, '_seal', _RECEIPT_SEAL)
                object.__setattr__(receipt, '_audit', _json_bytes(audit))
                object.__setattr__(receipt, '_result', weakref.ref(result))
                self._receipt_diagnostic = 'issued'
                return result
            except (AttributeError, TypeError, ValueError, OverflowError):
                self._receipt_diagnostic = 'output_unverified'
                return replace(result, receipt=None)


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
            if any(not math.isfinite(v) or v < -_tolerance(bound) or v > bound + _tolerance(bound)
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
