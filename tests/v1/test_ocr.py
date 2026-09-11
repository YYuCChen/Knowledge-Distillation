from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from knowledge_distiller.v1 import ocr


def image_bytes(mode="RGB", color="white"):
    image = Image.new(mode, (100, 80), color)
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def raw(texts=None, scores=None, polygons=None):
    return [{"rec_texts": [" 原文 A ", "English"] if texts is None else texts,
             "rec_scores": [.95, .8] if scores is None else scores,
             "rec_polys": [[[1, 2], [80, 2], [80, 20], [1, 20]],
                           [[1, 30], [80, 30], [80, 50], [1, 50]]] if polygons is None else polygons}]


def runner(results):
    return ocr.PaddleOcrRunner(predictor_factory=lambda: SimpleNamespace(predict=lambda image: results))


def test_lazy_predictor_reused_and_original_coordinates_text_preserved():
    calls = []
    def factory():
        calls.append(True)
        return SimpleNamespace(predict=lambda image: raw())
    recognizer = ocr.PaddleOcrRunner(predictor_factory=factory)
    assert calls == []
    result = recognizer.recognize_bytes(image_bytes(), "image/png")
    assert result.width == 100 and result.height == 80
    assert result.text == " 原文 A \nEnglish"
    assert result.lines[0].polygon == ((1, 2), (80, 2), (80, 20), (1, 20))
    assert result.lines[0].confidence == .95
    assert result.runtime_version == "3.7.0"
    assert result.detection_model == "PP-OCRv6_small_det"
    assert result.recognition_model == "PP-OCRv6_small_rec"
    assert recognizer.recognize_bytes(image_bytes(), "image/png") == result
    assert calls == [True]


def test_blank_image_can_return_explicit_empty_lines():
    result = runner(raw([], [], [])).recognize_bytes(image_bytes(), "image/png")
    assert result.text == "" and result.lines == () and result.width == 100


def test_numpy_native_results_are_supported():
    import numpy as np
    result = raw()
    result[0]["rec_scores"] = np.array([.95, .8])
    result[0]["rec_polys"] = np.array(result[0]["rec_polys"])
    assert len(runner(result).recognize_bytes(image_bytes(), "image/png").lines) == 2


@pytest.mark.parametrize("results", [
    [], [{}], raw(scores=[.5]), raw(scores=[float("nan"), .5]),
    raw(scores=[True, .5]), raw(scores=[1.01, .5]), raw(texts=["", "x"]),
    raw(texts=[42, "x"]), raw(polygons=[[[1, 1], [200, 1], [200, 2], [1, 2]]]),
    raw(["x"], [.9], [[[0, 0], [1, 0], [2, 0], [3, 0]]]),
    raw(["x"], [.9], [[[False, 0], [80, 0], [80, 10], [0, 10]]]),
    raw(["x"], [.9], [[[0, 0], [80, 0], [80, 10], [0, 0]]]),
    raw(["x"], [.9], [[[0, 0], [80, 0], [80, float("inf")], [0, 10]]]),
])
def test_invalid_external_result_is_never_silent_empty_success(results):
    with pytest.raises(ocr.OcrError, match="ocr_invalid_output"):
        runner(results).recognize_bytes(image_bytes(), "image/png")


@pytest.mark.parametrize("data,mime", [(b"", "image/png"), (b"bad", "image/png"),
                                       (image_bytes(), "image/jpeg"),
                                       (image_bytes(), "text/plain")])
def test_bad_image_fails_before_runtime_load(data, mime):
    def unexpected():
        raise AssertionError("must not load OCR for an invalid image")
    with pytest.raises(ocr.OcrError, match="ocr_invalid_image"):
        ocr.PaddleOcrRunner(predictor_factory=unexpected).recognize_bytes(data, mime)


def test_transparency_composites_white_and_pixels_are_bgr():
    def predict(image):
        assert tuple(image[0, 0]) == (255, 255, 255)
        return raw([], [], [])
    recognizer = ocr.PaddleOcrRunner(predictor_factory=lambda: SimpleNamespace(predict=predict))
    recognizer.recognize_bytes(image_bytes("RGBA", (0, 0, 0, 0)), "image/png")


def test_inference_error_is_explicit():
    def predict(image):
        raise RuntimeError("backend broke")
    recognizer = ocr.PaddleOcrRunner(predictor_factory=lambda: SimpleNamespace(predict=predict))
    with pytest.raises(ocr.OcrError, match="ocr_inference_failed"):
        recognizer.recognize_bytes(image_bytes(), "image/png")


def test_missing_or_wrong_runtime_is_distinct(monkeypatch):
    monkeypatch.setattr(ocr, "version", lambda distribution: "0.0")
    with pytest.raises(ocr.OcrError, match="ocr_runtime_unavailable"):
        ocr._load_predictor()


def test_model_initialization_failure_is_distinct(monkeypatch):
    import sys
    monkeypatch.setattr(ocr, "version", lambda distribution: {
        "paddleocr": "3.7.0", "paddlex": "3.7.2", "paddlepaddle": "3.3.0"}[distribution])
    def unavailable(**kwargs):
        assert kwargs["text_detection_model_name"] == "PP-OCRv6_small_det"
        assert kwargs["text_recognition_model_name"] == "PP-OCRv6_small_rec"
        raise OSError("download unavailable")
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=unavailable))
    monkeypatch.setitem(sys.modules, "paddlex.utils.deps", SimpleNamespace(DependencyError=ImportError))
    with pytest.raises(ocr.OcrError, match="ocr_model_unavailable"):
        ocr._load_predictor()


def test_animated_image_is_not_silently_reduced_to_one_frame():
    output = BytesIO()
    Image.new("RGB", (10, 10), "white").save(output, "PNG", save_all=True,
        append_images=[Image.new("RGB", (10, 10), "black")], duration=100, loop=0)
    with pytest.raises(ocr.OcrError, match="ocr_invalid_image"):
        runner(raw()).recognize_bytes(output.getvalue(), "image/png")
