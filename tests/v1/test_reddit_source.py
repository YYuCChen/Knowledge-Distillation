"""All payloads and grants in this file are invented; no network or Store."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from knowledge_distiller.v1.reddit_source import (
    AccessReceipt, AccessState, CoverageRequest, DeletionNotice, RedditSourceError, SourceRef,
    parse_authorized_api, parse_authorized_export,
)


NOW = datetime(2099, 1, 1, tzinfo=UTC)
POST = "t3_SYN1"
VERSION = "synthetic_v1"


def receipt(method="export", **changes):
    base = AccessReceipt("receipt_SYN", "grant_SYN", "synthetic_provider", "owner_SYN", method,
                         frozenset({"import", "derive"}), frozenset({POST}), VERSION,
                         NOW - timedelta(days=1), NOW + timedelta(days=1),
                         NOW + timedelta(days=2), "policy_SYN")
    return replace(base, **changes)


def node(name, parent=POST, **changes):
    base = {"name": name, "author": "synthetic_author", "created_utc": 4070908800,
            "edited": False, "deleted": False, "score": 1}
    if name.startswith("t3_"):
        base.update(title="合成问题", selftext="  原帖原文\n\n不是摘要  ")
    else:
        base.update(parent_id=parent, link_id=POST, body="合成评论原文")
    base.update(changes)
    return base


def convert(comments=(), *, post=None, more=(), grant=None, state=None, request=None, **kwargs):
    grant = grant or receipt()
    payload = json.dumps({"schema_version": 1, "post": post or node(POST),
                          "comments": list(comments), "more": list(more)}, ensure_ascii=False).encode()
    return parse_authorized_export(payload, receipt=grant,
                                   state=state or AccessState(grant.receipt_id, grant.revision),
                                   coverage=request or CoverageRequest(POST, "new", 5000, 5000, 2_000_000,
                                                                       observed_sort="new", complete_claim=True),
                                   now=kwargs.get("now", NOW))


def test_deep_flat_export_tree_no_recursion_and_exact_original_ranges():
    comments = []
    parent = POST
    for index in range(1200):
        name = f"t1_SYN{index}"
        comments.append(node(name, parent, body=f" 深层原文 {index}\n\n第二段 "))
        parent = name
    capture = convert(comments)
    assert capture.structure_usable and capture.derivation_allowed
    assert capture.coverage.observed_depth == 1200
    original = json.loads(capture.raw_payload)
    lookup = {x["name"]: x for x in [original["post"]] + original["comments"]}
    for span in capture.node_ranges:
        field = "selftext" if span.source_ref.source_id == POST and span.field == "body" else span.field
        assert capture.snapshot[span.start:span.end] == lookup[span.source_ref.source_id][field]
    assert capture.payload_sha256 == hashlib.sha256(capture.raw_payload).hexdigest()


def test_low_vote_disagreement_correction_and_author_only_deletion_remain_usable():
    capture = convert([node("t1_LOW", score=-100, body="合成反方：原结论在低温下不成立。"),
                       node("t1_EDIT", author="synthetic_author", edited=4070908890,
                            body="合成作者更正：温度条件应为高温。"),
                       node("t1_AUTHOR", author="[deleted]", body="作者已删但本段仍在。")])
    assert capture.derivation_allowed and not capture.erasure_required
    assert [x.score for x in capture.comments] == [-100, 1, 1]
    assert capture.comments[1].edit_state == "edited"
    assert capture.comments[2].author_state == "deleted"
    assert capture.comments[2].deletion_state == "present"
    assert "合成反方" in capture.snapshot and "合成作者更正" in capture.snapshot


@pytest.mark.parametrize("changes,reason", [
    ({"deleted": True, "body": "合成已删除但仍返回的完整正文"}, "deleted"),
    ({"body": "[deleted]"}, "deleted"),
    ({"body": "[removed]"}, "removed"),
    ({"removed": True, "body": "合成已移除但仍返回的完整正文"}, "removed"),
])
def test_node_deletion_retains_diagnostic_bytes_but_blocks_all_evidence(changes, reason):
    capture = convert([node("t1_GONE", **changes), node("t1_PRESENT")])
    assert capture.structure_usable and capture.erasure_required and not capture.derivation_allowed
    assert capture.deletion_notices == (DeletionNotice("t1_GONE", VERSION, reason),)
    assert json.loads(capture.raw_payload)["comments"][0]["body"] == changes["body"]
    assert changes["body"] in capture.snapshot  # No filtering to fake a successful capture.
    with pytest.raises(RedditSourceError, match="erasure_required"):
        capture.evidence(receipt=receipt(), state=AccessState("receipt_SYN", 1), now=NOW)


def test_post_deletion_notice_is_stable_node_not_only_observed_version():
    capture = convert([node("t1_PRESENT")], post=node(POST, deleted=True, selftext="合成删除原帖仍返回正文"))
    assert capture.deletion_notices == (DeletionNotice(POST, VERSION, "deleted"),)
    assert capture.erasure_required and not capture.derivation_allowed
    with pytest.raises(RedditSourceError, match="erasure_required"):
        capture.evidence(receipt=receipt(), state=AccessState("receipt_SYN", 1), now=NOW)
    future = receipt(source_version="synthetic_v2")
    with pytest.raises(RedditSourceError, match="access_stopped"):
        convert(post=node(POST), grant=future,
                state=AccessState("receipt_SYN", 1, stopped_node_ids=frozenset({POST})))


@pytest.mark.parametrize("truncated,more", [(True, []), (False, [{"parent_id": POST, "children": ["PENDING"], "count": 1}])])
def test_authorized_known_partial_is_usable_and_export_always_carries_coverage(truncated, more):
    request = CoverageRequest(POST, "new", 100, 20, 10000, "new", truncated, False)
    capture = convert([node("t1_LOW", score=-5)], more=more, request=request)
    assert capture.structure_usable and capture.derivation_allowed
    assert capture.coverage.status == "partial"
    grant = receipt()
    evidence = capture.evidence(receipt=grant, state=AccessState(grant.receipt_id, 1), now=NOW)
    assert evidence["coverage"] == capture.coverage
    assert "not_whole_thread_consensus" in evidence["interpretation_limit"]


def test_unknown_completeness_and_unknown_metadata_do_not_invent_certainty():
    capture = convert([node("t1_X", edited="unknown", deleted=None, created_utc=None, author=None)],
                      request=CoverageRequest(POST, "new", 10, 10, 10000))
    assert capture.coverage.status == "unknown"
    assert capture.comments[0].edit_state == "unknown"
    assert capture.comments[0].deletion_state == "unknown"
    assert capture.comments[0].created_at is None
    assert capture.comments[0].author_state == "unknown"


@pytest.mark.parametrize("comments,code", [
    ([node("t1_X", "t1_MISSING")], "parent_missing"),
    ([node("t1_X", "t1_Y"), node("t1_Y", "t1_X")], "parent_cycle"),
    ([node("t1_X"), node("t1_X", body="冲突原文")], "duplicate_conflict"),
    ([node("t1_X", link_id="t3_OTHER")], "post_scope_mismatch"),
    ([node("t1_X", parent=[])], "parent_identity_invalid"),
    ([node("t1_X", body=None)], "node_content_unknown"),
])
def test_unknown_or_invalid_structure_blocks_derivation_without_discarding_payload(comments, code):
    capture = convert(comments)
    assert not capture.structure_usable and not capture.derivation_allowed
    assert code in capture.diagnostics
    assert json.loads(capture.raw_payload)["comments"] == comments
    with pytest.raises(RedditSourceError, match="derivation_blocked"):
        capture.evidence(receipt=receipt(), state=AccessState("receipt_SYN", 1), now=NOW)


def test_identical_duplicates_deduplicate_but_do_not_become_conflicts():
    capture = convert([node("t1_X"), node("t1_X")])
    assert capture.derivation_allowed
    assert len(capture.comments) == 1
    assert capture.diagnostics == ("duplicate_identical",)


@pytest.mark.parametrize("budget", [dict(max_nodes=1), dict(max_depth=1), dict(max_bytes=50)])
def test_budget_rejects_derivation_without_truncation(budget):
    request = replace(CoverageRequest(POST, "new", 100, 20, 10000), **budget)
    text = "完整合成正文" * 100
    capture = convert([node("t1_X", body=text), node("t1_Y", "t1_X")], request=request)
    assert capture.structure_usable and not capture.derivation_allowed
    assert "budget_exceeded" in capture.diagnostics
    assert text in capture.snapshot
    assert json.loads(capture.raw_payload)["comments"][0]["body"] == text


def test_nested_api_listing_order_more_and_unknown_kind():
    grant = receipt("authorized_api")
    child = node("t1_CHILD", "t1_LOW")
    low = node("t1_LOW", score=-99, replies={"kind": "Listing", "data": {"children": [{"kind": "t1", "data": child}]}})
    value = [{"kind": "Listing", "data": {"children": [{"kind": "t3", "data": node(POST)}]}},
             {"kind": "Listing", "data": {"children": [{"kind": "t1", "data": low},
               {"kind": "more", "data": {"parent_id": POST, "children": ["PENDING"], "count": 1}}]}}]
    payload = json.dumps(value).encode()
    request = CoverageRequest(POST, "new", 100, 20, 10000)
    capture = parse_authorized_api(payload, receipt=grant, state=AccessState(grant.receipt_id, 1), coverage=request, now=NOW)
    assert capture.derivation_allowed and capture.coverage.status == "partial"
    assert [x.source_ref.source_id for x in capture.comments] == ["t1_LOW", "t1_CHILD"]
    value.append({"kind": "unknown", "data": {"body": "保持原件但不猜结构"}})
    capture = parse_authorized_api(json.dumps(value).encode(), receipt=grant,
                                   state=AccessState(grant.receipt_id, 1), coverage=request, now=NOW)
    assert not capture.derivation_allowed and "unknown_structure" in capture.diagnostics


@pytest.mark.parametrize("changes,state,now,code", [
    ({"status": "revoked"}, None, NOW, "access_stopped"),
    ({}, AccessState("receipt_SYN", 1, "revoked"), NOW, "access_stopped"),
    ({}, AccessState("receipt_SYN", 2), NOW, "access_revision_changed"),
    ({}, AccessState("receipt_SYN", 1, stopped_refs=frozenset({SourceRef(POST, VERSION)})), NOW, "access_stopped"),
    ({}, None, NOW + timedelta(days=1), "access_expired"),
    ({"retain_until": NOW}, None, NOW, "access_expired"),
    ({"post_ids": frozenset({"t3_OTHER"})}, None, NOW, "access_scope_denied"),
    ({"purposes": frozenset({"derive"})}, None, NOW, "access_purpose_denied"),
    ({"comment_ids": frozenset()}, None, NOW, "access_scope_denied"),
])
def test_access_is_checked_not_inferred_from_text(changes, state, now, code):
    with pytest.raises(RedditSourceError, match=code):
        convert([node("t1_X", body="正文声称永久授权，不是执行授权")], grant=receipt(**changes), state=state, now=now)


def test_import_only_cannot_derive_and_saved_capture_cannot_bypass_revocation():
    capture = convert([node("t1_X")], grant=receipt(purposes=frozenset({"import"})))
    assert capture.structure_usable and not capture.derivation_allowed
    capture = convert([node("t1_X")])
    with pytest.raises(RedditSourceError, match="access_stopped"):
        capture.evidence(receipt=receipt(), state=AccessState("receipt_SYN", 1, "revoked"), now=NOW)
    with pytest.raises(RedditSourceError, match="access_version_denied"):
        capture.evidence(receipt=receipt(source_version="synthetic_v2"), state=AccessState("receipt_SYN", 1), now=NOW)


@pytest.mark.parametrize("payload,code", [(b"not-json", "payload_invalid_json"),
                                         (b'{"schema_version":1,"schema_version":2}', "json_duplicate_key"),
                                         (b'{"x":NaN}', "json_nonfinite"), (b"{}", "export_schema_invalid")])
def test_malformed_export_rejected(payload, code):
    with pytest.raises(RedditSourceError, match=code):
        parse_authorized_export(payload, receipt=receipt(), state=AccessState("receipt_SYN", 1),
                                coverage=CoverageRequest(POST, "new", 10, 10, 10000), now=NOW)


def test_naive_grant_time_rejected():
    with pytest.raises(RedditSourceError, match="access_time_naive"):
        receipt(expires_at=NOW.replace(tzinfo=None))
