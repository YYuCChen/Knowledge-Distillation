from dataclasses import replace
import hashlib

import pytest

from knowledge_distiller.v1.file_sources import (
    SourceIntakeError, parse_submitted_source, prepare_direct_text, prepare_file,
)
from knowledge_distiller.v1.source_parsing import SourceReadError


def parse(value: str):
    return parse_submitted_source(prepare_file("note.md", value.encode("utf-8")))


def test_direct_exact_unicode_and_declarations_are_identity():
    text = " \r\n水果🍎 e\u0301\n\u200b"
    source = prepare_direct_text(text, {"author": "作者", "origin": "出处"})
    result = parse_submitted_source(source)
    assert result.snapshot == text
    assert source.content == text.encode("utf-8")
    assert result.metadata["user_declared"] == {"author": "作者", "origin": "出处"}
    assert prepare_direct_text(text, {"origin": "出处", "author": "作者"}).source_key == source.source_key
    assert prepare_direct_text(text, {"author": "另一作者", "origin": "出处"}).source_key != source.source_key
    assert prepare_direct_text(text.rstrip(), {"author": "作者"}).source_key != source.source_key
    assert prepare_direct_text(text.replace("\r\n", "\n")).source_key != prepare_direct_text(text).source_key
    assert prepare_direct_text("é").source_key != prepare_direct_text("e\u0301").source_key


@pytest.mark.parametrize("value", ["", "\t\n\r\v\f \x85\xa0\u1680\u2000\u200a\u2028\u2029\u202f\u205f\u3000"])
def test_direct_closed_whitespace_rejected(value):
    with pytest.raises(SourceIntakeError):
        prepare_direct_text(value)


@pytest.mark.parametrize("value", ["\ufeff", "\u200b", "\x1c"])
def test_direct_content_outside_closed_whitespace_set_kept(value):
    assert parse_submitted_source(prepare_direct_text(value)).snapshot == value


def test_declarations_do_not_guess_and_reject_unknown_fields_or_invalid_unicode():
    assert "author" not in parse_submitted_source(prepare_direct_text("作者似乎是张三")).metadata
    for claims in [{"unknown": "x"}, {"author": 12}, {"origin": "\ud800"}]:
        with pytest.raises(SourceIntakeError):
            prepare_direct_text("body", claims)
    with pytest.raises(SourceIntakeError):
        prepare_direct_text("\ud800")


def test_exact_file_identity_ignores_path_and_basename():
    first = prepare_file("/private/user/first.MD", b"body")
    second = prepare_file(r"C:\private\renamed.markdown", b"body")
    assert first.source_key == second.source_key
    assert first.label == "first.MD"
    assert second.label == "renamed.markdown"
    assert prepare_file("x.md", b"body\n").source_key != first.source_key
    assert prepare_file("x.md", b"\xef\xbb\xbfbody").source_key != first.source_key


@pytest.mark.parametrize("name", ["a.pdf", "a.epub", "dir/", "a.txt", ""])
def test_unsupported_name_or_invalid_container_rejected_before_acceptance(name):
    with pytest.raises(SourceIntakeError):
        prepare_file(name, b"body")


def test_modified_snapshot_or_declared_author_cannot_parse():
    for source in [
        replace(prepare_direct_text("body"), content=b"changed"),
        replace(prepare_direct_text("body", {"author": "A"}), metadata={"user_declared": {"author": "B"}}),
        replace(prepare_file("x.md", b"body"), content=b"changed"),
    ]:
        with pytest.raises(SourceReadError, match="snapshot_mismatch"):
            parse_submitted_source(source)


def test_body_and_declarations_keep_exact_spans_and_utf8_occurrences():
    raw = '\ufeff---\r\ntitle: "原题"\r\nauthors: [甲, 乙]\r\nopaque: 001\r\n---\r\n# 标题\r\n重复🍎\r\n重复🍎\r\n'
    content = raw.encode("utf-8")
    result = parse_submitted_source(prepare_file("x.md", content))
    assert result.snapshot == '# 标题\r\n重复🍎\r\n重复🍎\r\n'
    assert result.metadata["source_title"] == "原题"
    assert result.metadata["author"]["display_name"] == "甲、乙"
    decoded = raw[1:]
    for declaration in result.metadata["document_declared"]:
        assert decoded[declaration["start"]:declaration["end"]] == declaration["raw"]
        assert content[declaration["raw_byte_start"]:declaration["raw_byte_end"]].decode("utf-8") == declaration["raw"]
        for scalar in declaration["scalars"]:
            assert decoded[scalar["start"]:scalar["end"]] == scalar["raw"]
    line = result.lineage
    assert line["snapshot_sha256"] == hashlib.sha256(result.snapshot.encode("utf-8")).hexdigest()
    for begin in [result.snapshot.index("重复"), result.snapshot.rindex("重复")]:
        end = begin + len("重复🍎")
        byte_begin = line["raw_byte_start"] + len(result.snapshot[:begin].encode("utf-8"))
        byte_end = line["raw_byte_start"] + len(result.snapshot[:end].encode("utf-8"))
        assert content[byte_begin:byte_end].decode("utf-8") == result.snapshot[begin:end]
        assert decoded[line["decoded_start"] + begin:line["decoded_start"] + end] == "重复🍎"


def test_yaml_safe_nodes_preserve_scalar_spelling_and_decode_quotes():
    result = parse('---\nTITLE: 001\nfloat: 1.0\nbool: true\ndate: 2026-09-06\n"null": null\n"quoted": "a\\tb"\nlist: [0, false, null, "null", \'a,b\']\n---\nBody')
    claims = {x["key"]: x for x in result.metadata["document_declared"]}
    assert result.metadata["source_title"] == "001"
    assert claims["float"]["value"] == "1.0"
    assert claims["bool"]["value"] == "true"
    assert claims["date"]["value"] == "2026-09-06"
    assert claims["quoted"]["value"] == "a\tb"
    assert claims["list"]["value"] == ["0", "false", None, "null", "a,b"]


def test_alias_matching_is_ascii_case_insensitive_without_trim_or_inference():
    result = parse('---\n" title": Wrong\nＴＩＴＬＥ: Wrong\nname: Wrong\nTitle: Right\n---\n# Heading')
    assert result.metadata["source_title"] == "Right"
    assert len(result.metadata["document_declared"]) == 4
    assert "source_title" not in parse("# Heading").metadata


@pytest.mark.parametrize("header", [
    "a: x\na: y", 'a: x\n"a": y', "a: {nested: x}", "a: [{nested: x}]",
    "a: [[x]]", "a: |\n  body", "a: >\n  body", "a: &ref x", "a: *ref", "<<: x",
    "a: !!str x", "a: !evil x", "a: !!python/object:os.system {}", "- x", "123: x",
    'a: "line\nbreak"', 'a: "line\\nbreak"', "a: x\n...\nb: y", "",
])
def test_frontmatter_unsupported_constructs_fail_closed(header):
    with pytest.raises(SourceReadError, match="markdown_frontmatter_invalid"):
        parse("---\n" + header + "\n---\nBody")


@pytest.mark.parametrize("body", ["---\ntitle: missing close", "---\ntitle: x\n---\n \n"])
def test_frontmatter_never_discards_broken_header_or_empty_body(body):
    with pytest.raises(SourceReadError):
        parse(body)


def test_non_exact_frontmatter_opener_stays_literal():
    for text in [" ---\ntitle: x\n---\nBody", "--- \ntitle: x\n---\nBody"]:
        assert parse(text).snapshot == text


@pytest.mark.parametrize("raw,code", [(b"\xff", "invalid_utf8"), (b"\xc0\xaf", "invalid_utf8"), ("正文".encode("utf-16"), "invalid_utf8"), (b"b\x00o\x00d\x00y\x00", "invalid_utf8"), (b"body\xef\xbb\xbf", "invalid_bom"), (b"\xef\xbb\xbf\xef\xbb\xbfbody", "invalid_bom")])
def test_invalid_encoding_has_no_partial_snapshot(raw, code):
    with pytest.raises(SourceReadError, match=code):
        parse_submitted_source(prepare_file("x.md", raw))


@pytest.mark.parametrize("body", ["<script>alert(1)</script>", '<span hidden>秘密</span>', "<div>排序内容</div>", "<!-- hidden -->", "```dataviewjs\nquery\n```", "~~~dataview\nquery\n~~~", "<% tp.file.include('x') %>", "{{include target}}"])
def test_active_or_visibility_affecting_constructs_fail(body):
    with pytest.raises(SourceReadError, match="markdown_unsupported_syntax"):
        parse(body)


@pytest.mark.parametrize("body", ["普通<文本>，a < b，<https://example.com>，<not ? a tag>", "<em>强调</em>", "`<script>x</script>`", "```html\n<script>x</script>\n```", "```html\n<script>x</script>\n````", "    <script>x</script>", "```html\n<script>x</script>", "[[target|label]] ![[target]] ![alt](image.png)", "```mermaid\ngraph LR\nA --> B\n```\n$$x$$", "# 标题\n\n| a | b |\n| - | - |\n> [!note]正文\n\n[^1]: 注释"])
def test_literal_supported_content_is_exact_and_never_dereferenced(body):
    assert parse(body).snapshot == body


def test_duplicate_aliases_preserved_and_attribution_conflict_rejected():
    same = parse("---\nauthor: A\nCreator: A\n---\nBody")
    assert len(same.metadata["document_declared"]) == 2
    for header in ["author: A\ncreator: B", "source: A\norigin: B"]:
        with pytest.raises(SourceReadError, match="metadata_conflict"):
            parse("---\n" + header + "\n---\nBody")


def test_semantic_aliases_keep_dates_opaque_keys_and_null_distinct():
    result = parse('---\npublished: 001\ncreated: true\ndate: 2026-09-06\nupdated: 1.0\nlang: [zh, en]\ntags: [one, null]\nunknown: secret\ntitle: [ignored]\n---\nBody')
    claims = result.metadata["semantic_claims"]
    assert [(c["role"], c["value"]) for c in claims] == [
        ("publication", "001"), ("creation", "true"), ("unqualified_date", "2026-09-06"),
        ("modification", "1.0"), ("language", "zh"), ("language", "en"), ("category", "one"),
    ]
    assert "source_title" not in result.metadata


def test_unicode_separator_is_not_a_frontmatter_logical_line():
    body = "---\u2028title: not a frontmatter\n---\nBody"
    assert parse(body).snapshot == body
