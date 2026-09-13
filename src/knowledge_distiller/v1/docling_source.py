"""Local structured PDF/EPUB conversion; no VLM descriptions or invented provenance.

Callers retain exact input bytes and EPUB spine/link completeness qualification.
Docling does not retain EPUB chapter anchors; chapter=None explicitly reflects it.
Frozen releases read all document models from their bundled artifact directory.
Source development retains upstream caches unless an explicit artifacts path is supplied.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import version, PackageNotFoundError
from io import BytesIO
from itertools import chain
import math
from pathlib import Path
import sys
from numbers import Real
from typing import Callable


DOCLING_VERSION = "2.126.0"


class DoclingSourceError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DocumentProvenance:
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    coord_origin: str | None = None
    chapter: str | None = None
    charspan: tuple[int, int] | None = None
    original_charspan: tuple[int, int] | None = None


@dataclass(frozen=True)
class DocumentEntry:
    kind: str
    text: str
    provenance: tuple[DocumentProvenance, ...]
    ref: str
    image_bytes: bytes | None = None
    mime: str | None = None
    table_data: dict | None = None
    label: str = ""
    content_layer: str = "body"
    original_text: str | None = None


@dataclass(frozen=True)
class DocumentPageImage:
    page: int
    width: int
    height: int
    image_bytes: bytes
    mime: str = "image/png"


@dataclass(frozen=True)
class DoclingSourceResult:
    entries: tuple[DocumentEntry, ...]
    page_count: int | None
    metadata: dict = field(default_factory=dict)
    runtime_version: str = DOCLING_VERSION
    page_images: tuple[DocumentPageImage, ...] = ()


class DoclingSourceConverter:
    def __init__(self, *, converter_factory: Callable | None = None, components_root=None):
        self._factory = converter_factory or (lambda: _build_converter(components_root))
        self._converter = None

    def convert_bytes(self, data: bytes, kind: str) -> DoclingSourceResult:
        if kind not in {"pdf", "epub"} or not isinstance(data, bytes) or not data:
            raise DoclingSourceError("docling_invalid_input")
        if (kind == "pdf" and not data.startswith(b"%PDF-")) or (kind == "epub" and not data.startswith(b"PK")):
            raise DoclingSourceError("docling_invalid_input")
        if self._converter is None:
            self._converter = self._factory()
        try:
            from docling.datamodel.base_models import DocumentStream
        except ImportError as error:
            raise DoclingSourceError("docling_runtime_unavailable") from error
        try:
            converted = self._converter.convert(
                DocumentStream(name="source." + kind, stream=BytesIO(data)), raises_on_error=True)
        except Exception as error:
            raise DoclingSourceError("docling_conversion_failed") from error
        raw_status = getattr(converted, "status", None)
        status = getattr(raw_status, "value", raw_status)
        if status != "success" or getattr(converted, "errors", []):
            raise DoclingSourceError("docling_incomplete")
        return _translate(getattr(converted, "document", None), kind)


def _bundled_artifacts():
    if not getattr(sys, "frozen", False):
        import os
        explicit = os.environ.get('KNOWLEDGE_DISTILLER_DOCLING_MODELS')
        if explicit:
            root = Path(explicit).resolve()
            if not (root / 'manifest.json').is_file():
                raise DoclingSourceError('docling_runtime_unavailable')
            return root
        return None
    root = Path(sys._MEIPASS) / "docling-models"
    if not (root / "manifest.json").is_file():
        raise DoclingSourceError("docling_runtime_unavailable")
    return root


def _build_converter(components_root=None):
    if components_root is not None:
        from .docling_component import DoclingComponent, DoclingComponentError
        component = DoclingComponent(components_root)
        try:
            artifacts = component.verify()
        except DoclingComponentError as error:
            if getattr(sys, 'frozen', False) or component.root.exists():
                raise DoclingSourceError(str(error)) from error
            artifacts = _bundled_artifacts()
    else:
        artifacts = _bundled_artifacts()
    try:
        if version("docling") != DOCLING_VERSION:
            raise DoclingSourceError("docling_runtime_unavailable")
        from docling.document_converter import DocumentConverter, PdfFormatOption, EpubFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.datamodel.backend_options import EpubBackendOptions
        from docling.datamodel.settings import settings
        import onnxruntime  # noqa: F401
        import rapidocr  # noqa: F401
    except DoclingSourceError:
        raise
    except (ImportError, PackageNotFoundError, OSError) as error:
        raise DoclingSourceError("docling_runtime_unavailable") from error
    options = PdfPipelineOptions()
    options.artifacts_path = artifacts
    options.do_ocr = True
    # This fixed Docling release resolves Chinese to PP-OCRv6 small, not a VLM.
    options.ocr_options = RapidOcrOptions(backend="onnxruntime", lang=["ch"],
        rapidocr_params={"Global.model_root_dir": str(artifacts / "RapidOcr" if artifacts else settings.cache_dir / "rapidocr")})
    options.do_table_structure = True
    options.generate_page_images = True
    options.generate_picture_images = True
    options.images_scale = 2
    options.enable_remote_services = False
    options.do_picture_description = False
    options.do_picture_classification = False
    options.do_chart_extraction = False
    options.do_formula_enrichment = True
    options.do_code_enrichment = False
    return DocumentConverter(allowed_formats=[InputFormat.PDF, InputFormat.EPUB], format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=options, pipeline_cls=_scan_aware_pipeline()),
        InputFormat.EPUB: EpubFormatOption(backend_options=EpubBackendOptions(
            fetch_images=True, enable_local_fetch=True, enable_remote_fetch=False)),
    })


def _scan_aware_pipeline():
    """Use page OCR only for actual scans, retaining native PDF cells elsewhere."""
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
    from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel
    from docling_core.types.doc import BoundingBox, CoordOrigin

    class ScanAwareOcr(RapidOcrModel):
        def get_ocr_rects(self, page):
            if _is_scan(page):
                return [BoundingBox(l=0, t=0, r=page.size.width, b=page.size.height,
                                    coord_origin=CoordOrigin.TOPLEFT)]
            return super().get_ocr_rects(page)

    class ScanAwarePipeline(StandardPdfPipeline):
        def _make_ocr_model(self, art_path):
            return ScanAwareOcr(options=self.pipeline_options.ocr_options,
                enabled=self.pipeline_options.do_ocr, artifacts_path=art_path,
                accelerator_options=self.pipeline_options.accelerator_options)

    return ScanAwarePipeline


def _is_scan(page):
    if page._backend is None or page.size is None:
        return False
    cells = page._backend.get_visible_text_cells()
    if cells is None:
        cells = page._backend.get_text_cells()
    return not any(cell.text.strip() for cell in cells)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError
    return float(value)


def _png(image):
    if image is None or image.width <= 0 or image.height <= 0:
        raise ValueError
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _provenance(item, pages, kind):
    result = []
    for prov in item.prov:
        page = prov.page_no
        if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page not in pages:
            raise ValueError
        box = tuple(_number(getattr(prov.bbox, key)) for key in ("l", "t", "r", "b"))
        origin = getattr(prov.bbox.coord_origin, "value", prov.bbox.coord_origin)
        if origin not in {"TOPLEFT", "BOTTOMLEFT"}:
            raise ValueError
        l, t, r, b = box
        width, height = _number(pages[page].size.width), _number(pages[page].size.height)
        if not (0 <= l < r <= width + .01 and 0 <= min(t, b) < max(t, b) <= height + .01):
            raise ValueError
        charspan = getattr(prov, "charspan", None)
        if (not isinstance(charspan, (tuple, list)) or len(charspan) != 2
                or any(isinstance(n, bool) or not isinstance(n, int) for n in charspan)):
            raise ValueError
        start, end = charspan
        text = getattr(item, "text", "")
        original = getattr(item, "orig", text)
        if not isinstance(original, str) or not isinstance(text, str):
            raise ValueError
        if not 0 <= start <= end <= len(original):
            raise ValueError
        mapped = (start, end)
        if original != text:
            if len(item.prov) != 1:
                raise ValueError  # Cannot invent cross-page alignment after enrichment.
            mapped = (0, len(text))
        result.append(DocumentProvenance(page, box, origin, charspan=mapped,
                                         original_charspan=(start, end)))
    if kind == "pdf" and not result:
        raise ValueError
    # None does not invent an EPUB chapter identity that Docling discarded.
    return tuple(result) if result else (DocumentProvenance(),)


def _translate(document, kind):
    try:
        from docling_core.types.doc import PictureItem, TableItem, TextItem, ContentLayer
        pages = document.pages
        if kind == "pdf" and (not pages or sorted(pages) != list(range(1, len(pages) + 1))):
            raise ValueError
        entries, page_images = [], []
        for page_no, page in sorted(pages.items()):
            image = page.image.pil_image if page.image else None
            page_images.append(DocumentPageImage(page_no, image.width, image.height, _png(image)))
        refs = set()
        items = chain(document.iterate_items(traverse_pictures=True, included_content_layers=set(ContentLayer)),
                      document.iterate_items(root=document.furniture, traverse_pictures=True,
                                             included_content_layers=set(ContentLayer)))
        for item, _level in items:
            if not isinstance(item, (PictureItem, TableItem, TextItem)):
                raise ValueError
            if not item.self_ref or item.self_ref in refs:
                raise ValueError
            refs.add(item.self_ref)
            layer = getattr(item.content_layer, "value", item.content_layer)
            provenance = _provenance(item, pages, kind)
            if isinstance(item, PictureItem):
                entries.append(DocumentEntry("image", "", provenance, item.self_ref,
                    _png(item.get_image(document)), "image/png", label="picture", content_layer=layer))
            elif isinstance(item, TableItem):
                table_data = item.data.model_dump(mode="json")
                if not item.data.table_cells or any(not isinstance(cell.text, str) for cell in item.data.table_cells):
                    raise ValueError
                text = item.export_to_markdown(doc=document)
                if not isinstance(text, str) or not text.strip():
                    raise ValueError
                entries.append(DocumentEntry("table", text, provenance, item.self_ref,
                    table_data=table_data, label="table", content_layer=layer))
            else:
                if not isinstance(item.text, str):
                    raise ValueError
                label = getattr(item.label, "value", item.label)
                entry_kind = "formula" if label == "formula" else "text"
                original = getattr(item, "orig", item.text)
                if not item.text.strip():
                    # Empty formula text cannot be interpreted as recognized mathematics.
                    if entry_kind != "formula":
                        raise ValueError
                    entries.append(DocumentEntry("formula", "", provenance, item.self_ref,
                        _png(item.get_image(document)), "image/png", label=label, content_layer=layer,
                        original_text=original))
                else:
                    entries.append(DocumentEntry(entry_kind, item.text, provenance, item.self_ref,
                                                 label=label, content_layer=layer, original_text=original))
        origin = document.origin.model_dump(mode="json") if document.origin else None
        return DoclingSourceResult(tuple(entries), len(pages) if kind == "pdf" else None,
            {"document_name": document.name, "origin": origin,
             "epub_chapter_provenance": "unavailable" if kind == "epub" else None,
             "formula_enrichment": kind == "pdf",
             "formula_model": "docling-project/CodeFormulaV2" if kind == "pdf" else None},
            page_images=tuple(page_images))
    except DoclingSourceError:
        raise
    except Exception as error:
        raise DoclingSourceError("docling_invalid_output") from error
