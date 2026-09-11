import json
import sqlite3
from io import BytesIO
import pytest
from PIL import Image
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.file_sources import prepare_file
from knowledge_distiller.v1.pipeline import Distiller
from knowledge_distiller.v1.docling_source import (DoclingSourceResult, DoclingSourceError,
    DocumentEntry, DocumentProvenance, DocumentPageImage)
from knowledge_distiller.v1.domain import Knowledge, Point, Evidence
from .test_document_sources import pdf_bytes, epub_bytes


def test_document_runtime_failure_retry_then_atomic_media_fact_and_publication(tmp_path):
    store=Store(tmp_path/'data/db.sqlite3');store.initialize()
    source=prepare_file('document.pdf',pdf_bytes('Complete document body'))
    item=store.submit_source(source)
    output=BytesIO();Image.new('RGB',(10,10),'white').save(output,'PNG')
    class Converter:
        fail=True
        def convert_bytes(self, content, kind):
            assert content==source.content and kind=='pdf'
            if self.fail: raise DoclingSourceError('docling_runtime_unavailable')
            return DoclingSourceResult((DocumentEntry('text','Complete document body',
                (DocumentProvenance(page=1,bbox=(0,0,100,20),coord_origin='TOPLEFT',charspan=(0,22)),),'#/texts/0',label='text'),),
                1,page_images=(DocumentPageImage(1,10,10,output.getvalue()),))
    class Model:
        def derive(self,snapshot,uncertainties):
            return Knowledge('Title','Subtitle','Summary',(Point('p1','Statement','Argument',('e1',)),),(),
                (Evidence('e1',0,22,'Complete document body'),))
    vault=tmp_path/'vault';vault.mkdir()
    converter=Converter()
    service=Distiller(store=store,source=None,normalizer=None,recognizer=None,reviewer=None,
        confirmation_clipper=None,knowledge_model=Model(),runtime_root=tmp_path/'runtime',vault=vault,documents=converter)
    assert service.run(item).state=='failed'
    assert store.item_bundle(item)['source_fact_id'] is None
    assert store.submitted_source(item).content==source.content
    store.retry_item(item);converter.fail=False
    assert service.run(item).state=='succeeded'

    row=store.item_bundle(item)
    assert json.loads(row['lineage_json'])['spans'][0]['physical_page']==1
    assert store.media_members(row['material_id'])[0]['member_id']=='page-1'
    assert not list((vault/'知识蒸馏器').glob('附件/*/*.png'))
    with pytest.raises(sqlite3.IntegrityError),connect(store.path) as db:
        db.execute("DELETE FROM source_media")
    assert service.run(item).state=='succeeded'


def test_epub_ocr_runtime_error_is_not_misclassified_as_malformed_input():
    from knowledge_distiller.v1.file_sources import parse_submitted_source
    from knowledge_distiller.v1.ocr import OcrError
    class Converter:
        def convert_bytes(self, content, kind):
            return DoclingSourceResult((DocumentEntry('image','',(), '#/pictures/0',
                image_bytes=b'image-bytes',mime='image/png'),),0)
    class UnavailableOcr:
        def recognize_bytes(self, content, mime):
            raise OcrError('ocr_runtime_unavailable')
    with pytest.raises(OcrError,match='ocr_runtime_unavailable'):
        parse_submitted_source(prepare_file('book.epub',epub_bytes()),
            converter=Converter(),ocr=UnavailableOcr())
