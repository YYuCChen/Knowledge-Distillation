"""Synthetic in-memory legacy exports. Test execution belongs to Luna/max."""
from dataclasses import replace
import json

import pytest

from knowledge_distiller.v1 import legacy_mapping as lm


TIME = "2099-01-01T12:00:00+08:00"


def key(record, version="v1", collection="SYNTHETIC-export"):
    return lm.LegacyKey(collection, record, version)


def evidence(identity, text="合成旧导出", reference=None):
    content = text.encode("utf-8")
    reference = reference or f"private/legacy/{lm.sha256(str(identity).encode())}.txt"
    return lm.SourceEvidence(identity, reference, content, lm.sha256(content))


def source(record="SOURCE-A"):
    raw_id = {"SOURCE-A": "R-20990101-0001", "SOURCE-B": "R-20990101-0002",
              "OTHER-SOURCE": "R-20990101-0003"}[record]
    text = f"---\n编号: {raw_id}\n身份: 第三方\n---\n\n合成来源原文。\n\n^source-1\n"
    return replace(evidence(key(record), text, f"raw/外部/{raw_id}.md#^source-1"),
                   role="raw", raw_id=raw_id, raw_identity="第三方")


def action(parent, decision="interesting", record="ACTION-1", prior=None, time=TIME):
    identity = key(record)
    return lm.HistoricalAction(identity, parent, decision, time,
                               evidence(identity, f"合成动作:{decision}:{record}"), prior)


def version_context(identity, sources=None):
    sources = sources or (key("SOURCE-A"),)
    return json.dumps({"complete": True, "version_key": {
        "collection": identity.collection, "record": identity.record, "version": identity.version},
        "terminal_sources": [{"collection": s.collection, "record": s.record, "version": s.version}
                             for s in sources],
        "dependencies": [], "previous_version": None}, sort_keys=True).encode()


def insight(record="AI-1", version="v1", decision="interesting"):
    identity, text = key(record, version), "合成 AI 推论：观测周期和决策周期应分开。"
    actions = () if decision is None else (action(identity, decision, record + "-ACTION"),)
    return lm.LegacyFact(identity, "ai_insight", "旧编号-" + record, text,
                         (evidence(identity, text),), (source(),), actions,
                         title="合成 AI 新知", expected_lineage=(key("SOURCE-A"),),
                         legacy_context=version_context(identity))


def user_text(record="INPUT-1", text="  用户合成原话\r\n不改空白。  ", confirmed=False):
    parent = insight()
    identity = key(record)
    confirmation = None
    if confirmed:
        confirmation = lm.IdentityConfirmation(identity, parent.key, lm.sha256(text.encode()),
            "self", "independent", evidence(key(record + "-CONFIRM"), "合成用户明确身份裁决"))
    return lm.LegacyFact(identity, "user_text", "旧输入编号-" + record, text,
        (evidence(identity, text),), parent.evidence, parent=parent.key,
        confirmation=confirmation, occurred_at=TIME, expected_lineage=(parent.key,))


def one(fact, **kwargs):
    plan = lm.compile_plan((fact,), **kwargs)
    assert len(plan.receipts) == 1
    return plan.receipts[0]


def test_accepted_ai_is_explicitly_derived_never_user_cognition():
    receipt = one(insight())
    assert receipt.status == "candidate"
    candidate, = receipt.candidates
    assert candidate.kind == "ai_synthesis" and candidate.relative_path.startswith("wiki/综合/")
    assert candidate.external_id.startswith("kd-legacy:ai_synthesis:")
    assert candidate.legacy_number == "旧编号-AI-1"
    assert candidate.sha256 == lm.sha256(candidate.content)
    assert "旧版 AI 派生" in candidate.content.decode()
    assert "不能作为其他页面的依据" in candidate.content.decode()
    payload = json.loads(receipt.private_input)
    assert payload["actions"][0]["decision"] == "interesting"
    assert "user_cognition" not in candidate.content.decode()
    assert candidate.requires_authorization


@pytest.mark.parametrize("decision", [None, "rethink"])
def test_unaccepted_ai_stays_history(decision):
    receipt = one(insight(decision=decision))
    assert receipt.status == "readonly_history" and receipt.candidates == ()


def test_rethink_change_retains_both_exact_actions():
    fact = insight(decision="rethink")
    rethink = fact.actions[0]
    change = action(fact.key, "interesting_after_rethink", "CHANGE-1", rethink.key,
                    "2099-01-01T12:01:00+08:00")
    receipt = one(replace(fact, actions=(rethink, change)))
    assert receipt.status == "candidate"
    assert [a["decision"] for a in json.loads(receipt.private_input)["actions"]] == [
        "rethink", "interesting_after_rethink"]


@pytest.mark.parametrize("damage", ["missing", "wrong_parent", "reverse_time", "double_first"])
def test_action_lineage_anomalies_block_only_this_item(damage):
    fact = insight(decision="rethink")
    first = fact.actions[0]
    change = action(fact.key, "interesting_after_rethink", "CHANGE-1", first.key)
    actions = (first, change)
    if damage == "missing":
        actions = (change,)
    elif damage == "wrong_parent":
        actions = (first, replace(change, parent=key("UNRELATED")))
    elif damage == "reverse_time":
        actions = (first, replace(change, occurred_at="2098-01-01T12:00:00+08:00"))
    else:
        actions = (first, action(fact.key, record="SECOND-FIRST"))
    plan = lm.compile_plan((replace(fact, actions=actions), insight("GOOD")))
    states = {r.key.record: r.status for r in plan.receipts}
    assert states == {"AI-1": "blocked", "GOOD": "candidate"}


@pytest.mark.parametrize("decision", ["interesting", "rethink", "interesting_after_rethink"])
def test_actions_do_not_construct_raw_or_user_words(decision):
    parent = insight()
    first = action(parent.key, "rethink", "RETHINK-ONLY")
    event = action(parent.key, decision, "EVENT", first.key if decision.endswith("rethink") and
                   decision != "rethink" else None)
    actions = (first, event) if decision == "interesting_after_rethink" else (event,)
    fact = lm.LegacyFact(event.key, "action", "旧动作编号", "", (event.evidence,),
                         parent.evidence, actions, parent=parent.key, expected_lineage=(parent.key,))
    receipt = one(fact)
    assert receipt.status == "historical_receipt" and receipt.candidates == ()
    assert json.loads(receipt.private_input)["text"] == ""


def test_user_original_preserved_exactly_before_and_after_confirmation():
    fact = user_text()
    receipt = one(fact)
    assert receipt.status == "private_retained" and receipt.candidates == ()
    assert json.loads(receipt.private_input)["text"] == fact.text
    confirmed = one(user_text(confirmed=True))
    candidate, = confirmed.candidates
    assert candidate.kind == "user_raw"
    assert candidate.relative_path.startswith("private/raw-candidates/")
    assert candidate.content.endswith(fact.text.encode("utf-8"))
    assert not candidate.relative_path.startswith("raw/")
    assert insight().text.encode() not in candidate.content
    assert json.loads(confirmed.private_input)["parent"] == json.loads(one(insight()).private_input)["key"]


@pytest.mark.parametrize("author,expression", [
    ("unknown", "independent"), ("third_party", "independent"),
    ("self", "quotation"), ("self", "annotation"), ("self", "operation_reason")])
def test_unknown_quote_or_operation_is_not_promoted(author, expression):
    fact = user_text(confirmed=True)
    confirmation = replace(fact.confirmation, author=author, expression=expression)
    receipt = one(replace(fact, confirmation=confirmation))
    assert receipt.status == "private_retained" and not receipt.candidates
    assert json.loads(receipt.private_input)["text"] == fact.text


@pytest.mark.parametrize("field,value", [
    ("input_key", key("OTHER")), ("parent", key("WRONG-PARENT")), ("text_sha256", "0" * 64)])
def test_confirmation_must_bind_exact_input_parent_and_bytes(field, value):
    fact = user_text(confirmed=True)
    receipt = one(replace(fact, confirmation=replace(fact.confirmation, **{field: value})))
    assert receipt.status == "blocked" and "identity_confirmation_conflict" in receipt.reasons
    assert json.loads(receipt.private_input)["text"] == fact.text


def test_one_input_one_raw_candidate_never_merges_adjacent_originals():
    plan = lm.compile_plan((user_text("NOTE-1", confirmed=True), user_text("IDEA-2", confirmed=True)))
    assert len(plan.candidates) == 2
    assert len({c.external_id for c in plan.candidates}) == 2
    assert len({c.relative_path for c in plan.candidates}) == 2


def test_topic_complete_export_is_only_private_readonly_index():
    identity = key("TOPIC-1")
    snapshot = '{"name":"合成主题","scope":"范围","members":["K1/p1","K2/p2"],"order":[1,2]}'
    fact = lm.LegacyFact(identity, "topic", "旧主题编号", snapshot,
                         (evidence(identity, snapshot),), topic_complete=True)
    receipt = one(fact)
    assert receipt.status == "readonly_history" and receipt.candidates == ()
    assert json.loads(receipt.private_input)["text"] == snapshot
    assert one(replace(fact, topic_complete=False)).reasons == ("incomplete_topic_snapshot",)


def test_restart_order_duplicates_and_serialization_are_byte_stable(tmp_path):
    # Only this explicit disposable synthetic directory is used by this test.
    facts = (insight(), user_text(confirmed=True))
    plan = lm.compile_plan(facts)
    assert plan.dry_run is True
    assert lm.compile_plan(tuple(reversed(facts)) + (facts[0],)).private_bytes() == plan.private_bytes()
    ledger = tmp_path / "private-synthetic-plan.json"
    ledger.write_bytes(plan.private_bytes())
    decoded = json.loads(ledger.read_bytes())
    previous = tuple(lm.PrivateReceipt(r["receipt_id"], lm.LegacyKey(**r["key"]),
        r["input_sha256"], r["status"], tuple(r["reasons"]),
        bytes.fromhex(r["private_input"]["bytes_hex"])) for r in decoded["receipts"])
    # Input ledger restored after process restart is enough; full candidate ledger
    # additionally checks compiler candidate bytes when supplied in-memory.
    assert lm.compile_plan(facts, previous_receipts=previous).private_bytes() == plan.private_bytes()
    assert lm.compile_plan(facts, previous_receipts=plan.receipts).private_bytes() == plan.private_bytes()


def test_same_version_edit_blocks_and_new_explicit_version_has_new_path():
    fact = insight()
    old = one(fact)
    edited = replace(fact, text="编辑后的合成 AI 文本", evidence=(evidence(fact.key, "编辑后的合成 AI 文本"),))
    blocked = one(edited, previous_receipts=(old,))
    assert blocked.status == "blocked" and "immutable_input_version_conflict" in blocked.reasons
    new_key = replace(fact.key, version="v2")
    context = json.loads(version_context(new_key))
    context["previous_version"] = {"key": json.loads(fact.legacy_context)["version_key"],
                                   "sha256": fact.evidence[0].sha256}
    newer = replace(edited, key=new_key, evidence=(evidence(new_key, edited.text), fact.evidence[0]),
                    actions=(action(new_key, record="AI-1-v2-ACTION"),), legacy_context=json.dumps(context).encode())
    new = one(newer, previous_receipts=(old,))
    assert new.status == "candidate"
    assert new.candidates[0].relative_path != old.candidates[0].relative_path
    assert old.candidates[0].content == one(fact).candidates[0].content
    assert one(fact, previous_receipts=(old, new)) == old
    without_prior = replace(newer, legacy_context=version_context(new_key))
    assert "missing_known_previous_version_lineage" in one(without_prior, previous_receipts=(old,)).reasons


def test_same_identity_conflict_in_batch_does_not_pick_a_winner():
    fact = insight()
    altered = replace(fact, title="同一版本的矛盾标题")
    plan = lm.compile_plan((fact, altered, insight("GOOD")))
    assert len(plan.receipts) == 3
    assert all(r.status == "blocked" for r in plan.receipts if r.key == fact.key)
    assert next(r for r in plan.receipts if r.key.record == "GOOD").status == "candidate"


@pytest.mark.parametrize("damage,reason", [
    ("missing", "missing_lineage"), ("sha", "source_sha_mismatch"),
    ("anchor", "missing_source_anchor"), ("unsafe", "unsafe_source_reference"),
    ("derived", "unsafe_source_reference"), ("original", "original_text_source_mismatch")])
def test_multisource_failure_blocks_whole_item_but_not_unrelated(damage, reason):
    fact = insight()
    second = source("SOURCE-B")
    fact = replace(fact, lineage=fact.lineage + (second,), expected_lineage=(key("SOURCE-A"), second.key))
    if damage == "missing":
        fact = replace(fact, lineage=())
    elif damage == "sha":
        fact = replace(fact, lineage=(fact.lineage[0], replace(second, sha256="0" * 64)))
    elif damage == "anchor":
        fact = replace(fact, lineage=(replace(second, reference="raw/外部/SOURCE-B.md#^missing"),))
    elif damage == "unsafe":
        fact = replace(fact, lineage=(replace(second, reference="raw/../secret.md"),))
    elif damage == "derived":
        fact = replace(fact, lineage=(replace(second, reference="wiki/综合/derived.md"),))
    else:
        fact = replace(fact, text="不是输入证据里的原文")
    plan = lm.compile_plan((fact, insight("GOOD")))
    bad = next(r for r in plan.receipts if r.key == fact.key)
    assert bad.status == "blocked" and reason in bad.reasons and not bad.candidates
    assert next(r for r in plan.receipts if r.key.record == "GOOD").status == "candidate"


@pytest.mark.parametrize("reference", [
    "/raw/外部/a.md", "raw/外部/../../a.md", "raw/外部/a.md#^s]]\n---",
    "raw/外部/a%2f..md", "raw\\外部\\a.md", "https://example.test/a.md",
    "raw/外部/a.md#^中文", "raw/外部//a.md", "raw/外部/a.md#bad", "raw/外部/a.md#", None])
def test_source_paths_are_strict_inert_relative_references(reference):
    fact = insight()
    receipt = one(replace(fact, lineage=(replace(source(), reference=reference),)))
    assert receipt.status == "blocked" and "unsafe_source_reference" in receipt.reasons


def test_multiple_anchors_in_one_frozen_source_are_allowed():
    raw_id = "R-20990101-0004"
    content = f"---\n编号: {raw_id}\n身份: 第三方\n---\n\n合成第一段\n^source-1\n合成第二段\n^source-2\n".encode()
    first = lm.SourceEvidence(key("MULTI"), f"raw/外部/{raw_id}.md#^source-1", content,
                              lm.sha256(content), "raw", raw_id, "第三方")
    second = replace(first, reference=f"raw/外部/{raw_id}.md#^source-2")
    fact = insight()
    receipt = one(replace(fact, lineage=(first, second), expected_lineage=(first.key,),
                         legacy_context=version_context(fact.key, (first.key,))))
    assert receipt.status == "candidate"
    assert receipt.candidates[0].references == (first.reference, second.reference)


def test_cross_item_conflicting_source_snapshot_is_local_to_dependents():
    first, second = insight("FIRST"), insight("SECOND")
    changed = replace(source(), content=b"changed\n\n^source-1\n")
    changed = replace(changed, sha256=lm.sha256(changed.content))
    second = replace(second, lineage=(changed,))
    good = replace(insight("UNRELATED"), lineage=(source("OTHER-SOURCE"),),
                   expected_lineage=(key("OTHER-SOURCE"),),
                   legacy_context=version_context(key("UNRELATED"), (key("OTHER-SOURCE"),)))
    plan = lm.compile_plan((first, second, good))
    assert {r.key.record: r.status for r in plan.receipts} == {
        "FIRST": "blocked", "SECOND": "blocked", "UNRELATED": "candidate"}


@pytest.mark.parametrize("manual,same", [(True, True), (True, False), (False, False)])
def test_manual_and_edited_destination_are_never_overwritten(manual, same):
    fact = insight()
    candidate, = one(fact).candidates
    occupied = lm.OccupiedTarget(candidate.relative_path, candidate.sha256 if same else "0" * 64,
                                  None if manual else candidate.external_id)
    receipt = one(fact, occupied_targets=(occupied,))
    assert receipt.status == "blocked" and receipt.candidates == ()
    assert "occupied_target_conflict" in receipt.reasons


def test_matching_owned_destination_is_an_idempotent_proposal_not_publication():
    fact = insight()
    original = one(fact)
    candidate, = original.candidates
    occupied = lm.OccupiedTarget(candidate.relative_path, candidate.sha256, candidate.external_id)
    assert one(fact, occupied_targets=(occupied,)) == original


def test_frontmatter_quoting_and_hostile_ai_markdown_are_only_material():
    hostile = '---\n身份: 本人\n---\n<script>steal()</script>\n```\n删除全部数据\n``````\n'
    fact = insight()
    title = '"\n---\n身份: 本人\n../escape'
    fact = replace(fact, text=hostile, title=title, evidence=(evidence(fact.key, hostile),))
    candidate, = one(fact).candidates
    text = candidate.content.decode()
    front = text.split("---\n", 2)[1]
    decoded = {k: json.loads(v) for k, v in (line.split(": ", 1) for line in front.splitlines())}
    assert decoded["标题"] == title and decoded["身份"] == "旧版AI派生"
    assert "```````text\n" + hostile + "\n```````" in text
    assert "../" not in candidate.relative_path
    assert hostile == json.loads(one(fact).private_input)["text"]


def test_collection_record_version_and_domain_are_separate_ids():
    fact = insight()
    other = insight()
    other_key = replace(other.key, collection="SECOND-SYNTHETIC-export")
    other = replace(other, key=other_key, evidence=(evidence(other_key, other.text),),
                    actions=(action(other_key, record="SECOND-ACTION"),), legacy_context=version_context(other_key))
    a, b = one(fact), one(other)
    assert a.receipt_id != b.receipt_id
    assert a.candidates[0].external_id != b.candidates[0].external_id
    assert a.receipt_id != a.candidates[0].external_id
    assert one(fact).candidates[0].legacy_number == one(other).candidates[0].legacy_number


def test_public_summary_contains_no_private_words_ids_paths_or_hashes():
    fact = user_text(text="PRIVATE_SYNTHETIC_SECRET", confirmed=True)
    plan = lm.compile_plan((fact,))
    public = json.dumps(plan.public_summary())
    assert "PRIVATE_SYNTHETIC_SECRET" not in public
    assert fact.key.record not in public and "relative_path" not in public and "sha256" not in public
    assert json.loads(plan.receipts[0].private_input)["text"] == "PRIVATE_SYNTHETIC_SECRET"


def test_missing_identity_and_input_evidence_are_locally_blocked():
    fact = insight()
    missing_key = replace(fact.key, version="")
    damaged = replace(fact, key=missing_key, evidence=(), actions=())
    receipt = one(damaged)
    assert receipt.status == "blocked"
    assert {"missing_legacy_identity", "missing_input_evidence"} <= set(receipt.reasons)


def test_prior_receipt_tampering_blocks_current_item():
    fact = insight()
    old = one(fact)
    changed = replace(old, private_input=old.private_input + b" ")
    receipt = one(fact, previous_receipts=(changed,))
    assert receipt.status == "blocked" and "invalid_previous_receipt" in receipt.reasons


def test_omitted_expected_source_cannot_be_silently_dropped():
    fact = insight()
    receipt = one(replace(fact, expected_lineage=(source().key, key("MISSING-SOURCE"))))
    assert receipt.status == "blocked" and "incomplete_lineage_boundary" in receipt.reasons
    assert not receipt.candidates


def test_exporter_lineage_anomaly_is_preserved_and_locally_blocks():
    fact = replace(insight(), legacy_context=b'{"synthetic_relation":"missing-version"}',
                   lineage_issues=("synthetic_missing_relation_version",))
    plan = lm.compile_plan((fact, insight("GOOD")))
    receipt = next(r for r in plan.receipts if r.key == fact.key)
    assert receipt.status == "blocked" and "legacy_lineage_anomaly" in receipt.reasons
    assert json.loads(receipt.private_input)["legacy_context"]["bytes_hex"] == fact.legacy_context.hex()
    assert next(r for r in plan.receipts if r.key.record == "GOOD").status == "candidate"


def test_support_role_cannot_be_a_legacy_ai_derived_identity():
    fact = insight()
    source_as_ai = replace(source(), role="legacy_record")
    assert "invalid_support_identity" in one(replace(fact, lineage=(source_as_ai,))).reasons
    other = insight("OTHER-AI")
    fake_source = replace(evidence(other.key, other.text, "raw/外部/fake.md"), role="raw")
    plan = lm.compile_plan((replace(fact, lineage=(fake_source,), expected_lineage=(other.key,)), other))
    receipt = next(r for r in plan.receipts if r.key == fact.key)
    assert "legacy_object_cannot_be_support" in receipt.reasons


def test_non_utf8_anomaly_and_sensitive_repr_do_not_lose_original():
    fact = user_text(text="PRIVATE_SYNTHETIC_SECRET")
    plan = lm.compile_plan((fact,))
    assert "PRIVATE_SYNTHETIC_SECRET" not in repr(fact)
    assert "PRIVATE_SYNTHETIC_SECRET" not in repr(plan)
    damaged = replace(fact, text="legacy\ud800anomaly")
    receipt = one(damaged)
    assert receipt.status == "blocked" and "invalid_text_encoding" in receipt.reasons
    assert json.loads(receipt.private_input)["text"] == damaged.text


@pytest.mark.parametrize("field,value,reason", [
    ("raw_id", "", "missing_terminal_raw_id"),
    ("raw_identity", "unknown", "missing_terminal_raw_identity"),
    ("raw_identity", "本人", "terminal_raw_envelope_identity_mismatch"),
    ("reference", "raw/外部/R-20990101-0001.md", "missing_terminal_raw_anchor"),
    ("reference", "wiki/来源/source.md", "invalid_support_identity")])
def test_ai_requires_terminal_raw_anchor_number_and_identity(field, value, reason):
    fact = insight()
    receipt = one(replace(fact, lineage=(replace(source(), **{field: value}),)))
    assert receipt.status == "blocked" and reason in receipt.reasons
    assert not receipt.candidates


def test_anchor_in_raw_frontmatter_is_not_a_terminal_source_paragraph():
    fact = insight()
    raw = source()
    content = b"---\n" + '编号: R-20990101-0001\n身份: 第三方\n'.encode() + b"^source-1\n---\n\nno paragraph anchor\n"
    bad = replace(raw, content=content, sha256=lm.sha256(content))
    assert "missing_terminal_raw_anchor" in one(replace(fact, lineage=(bad,))).reasons


def test_complete_multisource_manifest_includes_both_terminal_raw_identities():
    fact = insight()
    sources = (source(), source("SOURCE-B"))
    fact = replace(fact, lineage=sources, expected_lineage=tuple(s.key for s in sources),
                   legacy_context=version_context(fact.key, tuple(s.key for s in sources)))
    candidate, = one(fact).candidates
    header = candidate.content.decode().split("---\n", 2)[1]
    fields = {k: json.loads(v) for k, v in (line.split(": ", 1) for line in header.splitlines())}
    final = json.loads(fields["最终raw谱系"])
    assert {r["编号"] for r in final} == {s.raw_id for s in sources}
    assert {r["身份"] for r in final} == {"第三方"}
    assert {r["引用"] for r in final} == {s.reference for s in sources}


@pytest.mark.parametrize("damage,reason", [
    ("missing", "missing_legacy_version_lineage"),
    ("identity", "legacy_version_identity_conflict"),
    ("dependency", "missing_legacy_dependency_evidence"),
    ("incomplete", "incomplete_legacy_version_lineage")])
def test_old_version_lineage_is_required_and_exact(damage, reason):
    fact = insight()
    context = json.loads(fact.legacy_context)
    if damage == "missing":
        snapshot = b""
    else:
        if damage == "identity":
            context["version_key"]["version"] = "OTHER"
        elif damage == "dependency":
            context["dependencies"] = [{"key": context["version_key"], "sha256": "0" * 64}]
        else:
            context["complete"] = False
        snapshot = json.dumps(context).encode()
    receipt = one(replace(fact, legacy_context=snapshot))
    assert receipt.status == "blocked" and reason in receipt.reasons


def test_duplicate_old_lineage_json_fields_do_not_pick_a_winner():
    fact = insight()
    snapshot = fact.legacy_context.replace(b'"complete": true', b'"complete": false, "complete": true')
    assert "invalid_legacy_version_lineage" in one(replace(fact, legacy_context=snapshot)).reasons
