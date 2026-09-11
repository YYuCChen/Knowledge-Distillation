from types import SimpleNamespace

import pytest
from PIL import Image
from docling_core.types.doc import (BoundingBox, ContentLayer, CoordOrigin, DocItemLabel,
    DoclingDocument, ImageRef, ProvenanceItem, Size, TableCell, TableData)

from knowledge_distiller.v1 import docling_source as source


def document(pdf=True):
    doc = DoclingDocument(name="Fixture")
    prov = None
    if pdf:
        doc.add_page(1, Size(width=100, height=80), ImageRef.from_pil(Image.new("RGB", (200,160)), dpi=144))
        prov = ProvenanceItem(page_no=1, bbox=BoundingBox(l=5,t=70,r=90,b=10,coord_origin=CoordOrigin.BOTTOMLEFT),charspan=(0,4))
    doc.add_text(DocItemLabel.TEXT, "原文 exact", prov=prov)
    return doc, prov


def convert(doc, kind="pdf", status="success", errors=None):
    return source.DoclingSourceConverter(converter_factory=lambda: SimpleNamespace(
        convert=lambda *args, **kwargs: SimpleNamespace(document=doc,status=status,errors=errors or [])
    )).convert_bytes(b"%PDF-fixture" if kind=="pdf" else b"PKfixture", kind)


def test_native_text_keeps_pdf_page_box_and_rendered_image():
    doc,_ = document()
    result = convert(doc)
    assert result.entries[0].text == "原文 exact"
    assert result.entries[0].provenance[0].bbox == (5,70,90,10)
    assert result.entries[0].provenance[0].coord_origin == "BOTTOMLEFT"
    assert result.page_count == 1 and result.page_images[0].width == 200
    assert result.page_images[0].image_bytes.startswith(b"\x89PNG")
    assert result.runtime_version == "2.126.0"


def test_epub_retains_image_and_exposes_missing_chapter_instead_of_guess():
    doc,_ = document(False)
    doc.add_picture(image=ImageRef.from_pil(Image.new("RGB",(20,10)),dpi=72))
    result = convert(doc,"epub")
    assert result.page_count is None and result.entries[0].provenance[0].chapter is None
    assert result.entries[1].kind == "image" and result.entries[1].image_bytes.startswith(b"\x89PNG")
    assert result.metadata["epub_chapter_provenance"] == "unavailable"


def test_table_cell_structure_and_formula_kind_are_retained():
    doc,prov = document()
    table_prov=prov.model_copy(update={"charspan":(0,0)})
    doc.add_table(TableData(num_rows=1,num_cols=1,table_cells=[TableCell(text="Cell",start_row_offset_idx=0,
        end_row_offset_idx=1,start_col_offset_idx=0,end_col_offset_idx=1)]),prov=table_prov)
    doc.add_text(DocItemLabel.FORMULA,"E = mc²",prov=prov)
    result=convert(doc)
    assert result.entries[1].kind == "table" and "Cell" in result.entries[1].text
    assert result.entries[1].table_data["table_cells"][0]["text"] == "Cell"
    assert result.entries[2].kind == "formula" and result.entries[2].text == "E = mc²"


def test_unrecognized_formula_retains_crop_without_invented_text():
    doc,prov=document()
    prov=prov.model_copy(update={"charspan":(0,0)})
    doc.add_text(DocItemLabel.FORMULA,"",prov=prov)
    result=convert(doc)
    assert result.entries[1].kind == "formula" and result.entries[1].text == ""
    assert result.entries[1].image_bytes.startswith(b"\x89PNG")


def test_furniture_is_explicit_not_silently_omitted():
    doc,prov=document()
    doc.add_text(DocItemLabel.PAGE_HEADER,"Source header",prov=prov,content_layer=ContentLayer.FURNITURE)
    result=convert(doc)
    assert result.entries[1].text == "Source header"
    assert result.entries[1].content_layer == "furniture"


@pytest.mark.parametrize("status,errors", [("partial_success",[]),("failure",[]),("success",["page failed"])])
def test_partial_conversion_never_becomes_complete(status,errors):
    doc,_=document()
    with pytest.raises(source.DoclingSourceError,match="docling_incomplete"):
        convert(doc,status=status,errors=errors)


def test_missing_pdf_provenance_fails():
    doc,_=document()
    doc.texts[0].prov=[]
    with pytest.raises(source.DoclingSourceError,match="docling_invalid_output"):
        convert(doc)


def test_missing_epub_image_fails_instead_of_dropping_image():
    doc,_=document(False)
    doc.add_picture()
    with pytest.raises(source.DoclingSourceError,match="docling_invalid_output"):
        convert(doc,"epub")


def test_missing_page_render_fails():
    doc,_=document()
    doc.pages[1].image=None
    with pytest.raises(source.DoclingSourceError,match="docling_invalid_output"):
        convert(doc)


def test_out_of_bounds_provenance_fails():
    doc,_=document()
    doc.texts[0].prov[0].bbox.r=500
    with pytest.raises(source.DoclingSourceError,match="docling_invalid_output"):
        convert(doc)


def test_valid_empty_document_is_distinguishable_from_broken_conversion():
    result=convert(DoclingDocument(name="empty"),"epub")
    assert result.entries == () and result.page_count is None


def test_conversion_error_is_not_empty_success():
    def broken(*args,**kwargs):raise RuntimeError("converter failed")
    c=source.DoclingSourceConverter(converter_factory=lambda:SimpleNamespace(convert=broken))
    with pytest.raises(source.DoclingSourceError,match="docling_conversion_failed"):
        c.convert_bytes(b"%PDF-broken","pdf")


def test_missing_runtime_is_explicit(monkeypatch):
    monkeypatch.setattr(source,"version",lambda name:"0.0")
    with pytest.raises(source.DoclingSourceError,match="docling_runtime_unavailable"):
        source._build_converter()


@pytest.mark.parametrize("data,kind",[(b"","pdf"),(b"garbage","pdf"),(b"%PDF-x","epub"),(b"x","html")])
def test_bad_input_does_not_load_converter(data,kind):
    def unexpected():raise AssertionError
    with pytest.raises(source.DoclingSourceError,match="docling_invalid_input"):
        source.DoclingSourceConverter(converter_factory=unexpected).convert_bytes(data,kind)


def test_cross_page_merged_text_keeps_each_provenance_charspan():
    doc,prov=document()
    doc.add_page(2,Size(width=100,height=80),ImageRef.from_pil(Image.new("RGB",(200,160)),dpi=144))
    doc.texts[0].text="same same"
    doc.texts[0].orig="same same"
    doc.texts[0].prov=[prov.model_copy(update={"charspan":(0,4)}),
                      prov.model_copy(update={"page_no":2,"charspan":(5,9)})]
    result=convert(doc)
    assert [(p.page,p.charspan) for p in result.entries[0].provenance] == [(1,(0,4)),(2,(5,9))]


def test_charspan_outside_text_fails():
    doc,prov=document()
    doc.texts[0].prov=[prov.model_copy(update={"charspan":(0,100)})]
    with pytest.raises(source.DoclingSourceError,match="docling_invalid_output"):
        convert(doc)


def test_formula_enrichment_maps_full_latex_and_preserves_original_span():
    doc,prov=document()
    item=doc.add_text(DocItemLabel.FORMULA,"a ^ { 2 } + 8 = 1 2",orig="a 2 +8 = 12",
                      prov=prov.model_copy(update={"charspan":(0,11)}))
    entry=convert(doc).entries[1]
    assert entry.original_text == "a 2 +8 = 12"
    assert entry.provenance[0].original_charspan == (0,11)
    assert entry.provenance[0].charspan == (0,len(entry.text))


def test_enriched_formula_cross_page_alignment_is_not_invented():
    doc,prov=document()
    doc.add_page(2,Size(width=100,height=80),ImageRef.from_pil(Image.new("RGB",(200,160)),dpi=144))
    item=doc.add_text(DocItemLabel.FORMULA,"a^{2}",orig="a2",prov=prov.model_copy(update={"charspan":(0,1)}))
    item.prov.append(prov.model_copy(update={"page_no":2,"charspan":(1,2)}))
    with pytest.raises(source.DoclingSourceError,match="docling_invalid_output"):
        convert(doc)


def test_only_pages_without_native_text_use_scan_rectangles():
    cells=[]
    page=SimpleNamespace(size=SimpleNamespace(width=100,height=80),
        _backend=SimpleNamespace(get_visible_text_cells=lambda:cells))
    assert source._is_scan(page)
    cells.append(SimpleNamespace(text="Native PDF fact"))
    assert not source._is_scan(page)
    page._backend=None
    assert not source._is_scan(page)
