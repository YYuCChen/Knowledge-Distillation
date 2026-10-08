"""Real schema27 raw originals, synthetic bytes, no external reader/model."""
import hashlib
import json

import pytest

from knowledge_distiller.v1 import raw
from knowledge_distiller.v1.ingestion import Ingestion
from knowledge_distiller.v1.domain import SourceFact
from knowledge_distiller.v1.source_parsing import ParsedSource
from knowledge_distiller.v1.web_article import WebArticleSource, WebArticle, WebCapture
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_source_proof import trusted_source_callback, SourceProofError
from .test_raw_only_completion import world, claim
from .test_source_schema26_compat import frozen


@pytest.mark.parametrize('damage', [None, 'html', 'wire', 'body', 'extraction', 'missing', 'extra', 'staging'])
def test_typed_original_manifest_reads_all_bytes_and_rejects_inconsistency(world, damage):
    originals = {'html-1': b'<html>synthetic retained page</html>',
                 'wire-1': b'synthetic wire', 'body-1': '合成网页正文'.encode(),
                 'extraction-1': b'<body>synthetic extraction</body>'}
    url = 'https://synthetic.example/page'
    article = WebArticle(WebCapture(url, url, (), originals['html-1'], originals['wire-1']),
        ParsedSource(originals['body-1'].decode(), {'source_title': 'synthetic'},
                     {'extracted_body_xml': originals['extraction-1']}))
    captured = WebArticleSource(reader=lambda _: article).capture(url, world.root / 'web-work')
    item = world.store.create_item(url); claim(world, item)
    material = world.store.attach_material(item, captured)
    world.store.establish_source_fact(material, SourceFact(captured.metadata['original_description']),
                                     lineage=captured.metadata['web_lineage'])
    receipt = world.store.complete_raw_item(item)
    record = raw.RawLedger(world.store).record(receipt.raw_id)
    document = (world.vault / receipt.relative_path).read_bytes()
    assert '![[附件/raw/' not in document.decode()
    task, snapshot, context = frozen((world.store, world.vault, Ingestion(world.store)), record)
    attachments = {a['member_id']: a for a in json.loads(record['attachments_json'])}
    directory = '附件/raw/' + receipt.raw_id
    with VaultWriteLock.acquire(world.vault) as lock:
        callback = trusted_source_callback(world.store, lock)
        source = callback.verify(task=task, snapshot=snapshot, context=context).manifest['sources'][0]
        assert {(a['sha256'], a['byte_count']) for a in source['attachments']} == {
            (hashlib.sha256(content).hexdigest(), len(content)) for content in originals.values()}
        assert len(source['attachments']) == 4 and 'canonical_ingestion_event' in source['capabilities']
        if damage:
            if damage == 'extra':
                for root in (world.vault, snapshot.workspace):
                    (root / directory / 'unexpected.bin').write_bytes(b'unregistered original')
            else:
                member = damage+'-1' if damage in {'html','wire','body','extraction'} else 'html-1'
                path = directory + '/' + attachments[member]['filename']
                if damage == 'missing':
                    (world.vault / path).unlink()
                elif damage == 'staging':
                    (snapshot.workspace / path).write_bytes(b'staging-only corruption')
                else:
                    # Both mirrors changed identically must still fail against
                    # the immutable manifest and owned original DB bytes.
                    for root in (world.vault, snapshot.workspace):
                        (root / path).write_bytes(b'same corruption in both copies')
            with pytest.raises(SourceProofError):
                callback.verify(task=task, snapshot=snapshot, context=context)
    assert (world.vault / receipt.relative_path).read_bytes() == document
