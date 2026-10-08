"""Real synthetic PDF Info round trips; no converter, model or database."""
import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject, BooleanObject, ByteStringObject, DictionaryObject, NameObject,
    NumberObject, TextStringObject,
)

from knowledge_distiller.v1.pdf_source import _metadata
from knowledge_distiller.v1.source_parsing import SourceReadError


def info_reader(entries):
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    # add_metadata coerces arbitrary values to strings; preserve PDF object
    # types here so rejected cases actually reach the reader's Info boundary.
    writer._info = DictionaryObject({NameObject(key): value for key, value in entries.items()})
    output = io.BytesIO()
    writer.write(output)
    return PdfReader(io.BytesIO(output.getvalue()), strict=True)


@pytest.mark.parametrize('value', ['中文原声明', 'first\r\nsecond\rthird\nfourth',
                                  'Cafe\u0301 / Café / 重复重复', ''])
def test_text_string_round_trip_preserves_exact_value_as_plain_str(value):
    reader = info_reader({'/Title': TextStringObject(value)})
    original = reader.metadata['/Title']
    original_bytes = original.original_bytes
    assert type(original) is TextStringObject and original == value
    result = _metadata(reader)
    declaration, = result['document_declared']
    assert declaration == {'structure': 'Info', 'field': '/Title', 'role': 'title',
                           'value': value, 'provenance': 'document-declared'}
    assert type(declaration['value']) is str
    if value:
        assert result['source_title'] == value and type(result['source_title']) is str
    else:
        assert 'source_title' not in result
    assert reader.metadata['/Title'] is original
    assert original.original_bytes == original_bytes


def test_all_info_declarations_retained_in_existing_order_without_normalization():
    values = {'/Title': '标题\r\n原行', '/Author': '作者 e\u0301', '/Subject': '同一原值',
              '/Keywords': '同一原值', '/CreationDate': 'D:20261008090000+08\'00\'',
              '/ModDate': '未知日期原声明', '/Creator': '原工具', '/Producer': '原生产者'}
    reader = info_reader({key: TextStringObject(value) for key, value in values.items()})
    before = dict(reader.metadata)
    result = _metadata(reader)
    declarations = result['document_declared']
    assert [(entry['field'], entry['value']) for entry in declarations] == list(values.items())
    assert all(type(entry['value']) is str for entry in declarations)
    assert result['source_title'] == values['/Title']
    assert result['author'] == {'display_name': values['/Author'], 'provenance': 'document-declared'}
    assert type(result['author']['display_name']) is str
    assert dict(reader.metadata) == before
    assert all(reader.metadata[key] is before[key] for key in values)


def test_plain_str_boundary_on_actual_reader_preserves_original():
    reader = info_reader({'/Title': TextStringObject('serialized original')})
    value = 'ordinary str\r\n中文 e\u0301'
    # Serialized PDF text reads as TextStringObject. Exercise the explicitly
    # supported plain-str boundary on the real reader, not a fake Info dict.
    dict.__setitem__(reader._info, NameObject('/Title'), value)
    result = _metadata(reader)
    assert result['source_title'] == value and type(result['source_title']) is str
    assert result['document_declared'][0]['value'] is value
    assert dict.__getitem__(reader.metadata, '/Title') is value


@pytest.mark.parametrize('value', [
    ByteStringObject(b'\x7f'),
    NumberObject(17),
    BooleanObject(True),
    NameObject('/UndecodedDeclaration'),
    ArrayObject([TextStringObject('nested declaration')]),
    DictionaryObject({NameObject('/Nested'): TextStringObject('nested declaration')}),
], ids=['undecodable-bytes', 'number', 'boolean', 'name-str-subclass', 'array', 'dictionary'])
def test_non_text_info_objects_round_trip_rejected_without_coercion(value):
    reader = info_reader({'/Title': TextStringObject('preserved title'), '/Author': value})
    original = reader.metadata['/Author']
    assert type(original) is type(value)
    before = dict(reader.metadata)
    with pytest.raises(SourceReadError) as caught:
        _metadata(reader)
    assert str(caught.value) == 'pdf_metadata_invalid'
    assert caught.value.args == ('pdf_metadata_invalid',)
    assert dict(reader.metadata) == before
    assert reader.metadata['/Author'] is original
    assert reader.metadata['/Title'] == 'preserved title'


def test_unknown_string_subclass_not_coerced_or_echoed():
    class UnknownText(str):
        def __str__(self):
            raise AssertionError('must not stringify unknown declaration')

    reader = info_reader({'/Title': TextStringObject('preserved title')})
    unknown = UnknownText('private synthetic declaration')
    dict.__setitem__(reader._info, NameObject('/Author'), unknown)
    with pytest.raises(SourceReadError) as caught:
        _metadata(reader)
    assert caught.value.args == ('pdf_metadata_invalid',)
    assert dict.__getitem__(reader.metadata, '/Author') is unknown
    assert reader.metadata['/Title'] == 'preserved title'
