"""Only synthetic sources and tmp_path; never connect a real model or store."""
import copy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from knowledge_distiller.v1 import knowledge_recovery as module
from knowledge_distiller.v1 import knowledge_support as support

SNAPSHOT = "甲禁止开网。乙先检查再执行。"
CONFIG = "a" * 64


def payload():
    return {"qualified": True, "title": "合成操作", "subtitle": "对象和条件", "summary": "保留约束。",
            "core_points": [{"statement": "甲必须开网", "argument": "甲需要连接",
                             "source_ranges": [{"start_segment": "s1", "end_segment": "s1"}]}],
            "other_points": [{"statement": "乙先检查", "argument": "检查后执行",
                              "source_ranges": [{"start_segment": "s2", "end_segment": "s2"}]}]}


def checks(failures=()):
    result = []
    for pid in ("p1", "p2"):
        issues = [{"field": field, "category": category, "reason": "合成定位理由"}
                  for failed_pid, field, category in failures if failed_pid == pid]
        result.append({"point_id": pid, "status": "unsupported" if issues else "supported",
                       "reason": "合成判断", "issues": issues})
    return {"checks": result}


def repair(value, changes, mode="local"):
    return {"mode": mode, "candidate": value,
            "changes": [{"point_id": pid, "field": field, "reason": "据合成诊断修复"} for pid, field in changes]}


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        if not self.responses:
            raise AssertionError("unexpected client request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)


class Crash(BaseException):
    pass


def executor(tmp_path, fake_client, **kwargs):
    options = dict(checkpoint_root=tmp_path / "isolated-checkpoints", source_fact_id="sf-synthetic-1",
                   snapshot=SNAPSHOT, client=fake_client, model_config_hash=CONFIG)
    options.update(kwargs)
    return module.RecoveryExecutor(**options)


def test_success_reopened_terminal_same_result_no_call_and_immutable(tmp_path):
    client = Client(payload(), checks())
    first = executor(tmp_path, client).run()
    assert first.status == "supported_candidate_not_published"
    assert first.used_generations == 1 and len(client.calls) == 2
    assert [r.operation for r in client.calls] == ["generate", "support"]
    never = Client()
    assert executor(tmp_path, never).run() == first and never.calls == []
    with pytest.raises(FrozenInstanceError):
        first.status = "published"


def test_no_knowledge_never_reviews_and_reuses(tmp_path):
    client = Client({"qualified": False, "rejection_reason": "只有合成情绪"})
    first = executor(tmp_path, client).run()
    assert first.status == "no_knowledge" and first.review is None
    assert len(client.calls) == 1 and executor(tmp_path, Client()).run() == first


def test_feedback_actual_local_changes_parent_and_bytes_retained(tmp_path):
    old = payload()
    fixed = copy.deepcopy(old)
    fixed["core_points"][0]["statement"] = "甲禁止开网"
    client = Client(old, checks([("p1", "statement", "negation")]),
                    repair(fixed, [("p1", "statement")]), checks())
    result = executor(tmp_path, client).run()
    assert result.status == "supported_candidate_not_published" and result.used_generations == 2
    request = json.loads(client.calls[2].payload_json)
    assert request["snapshot"] == SNAPSHOT
    assert request["source_segments"] == [{"id": "s1", "text": "甲禁止开网。"}, {"id": "s2", "text": "乙先检查再执行。"}]
    assert json.loads(request["previous_candidate"]) == old
    assert request["allowed_fields"] == ["$.core_points[0].statement"]
    assert request["diagnostics"][0]["category"] == "negation"
    assert result.attempts[1].parent_hash == result.attempts[0].candidate.candidate_hash
    assert len(result.attempts[1].changes) == 1
    assert result.attempts[0].generation_raw == json.dumps(old, ensure_ascii=False)
    assert result.candidate.knowledge.other_points == result.attempts[0].candidate.knowledge.other_points


@pytest.mark.parametrize("mutate", [
    lambda v: v["candidate"]["core_points"].clear(),
    lambda v: v["candidate"].update(qualified=False),
    lambda v: v["candidate"].update(title="新标题"),
    lambda v: v["candidate"]["other_points"][0].update(argument="偷改成功字段"),
    lambda v: v.update(mode="regenerate"),
    lambda v: v.update(extra="字段"),
    lambda v: v.update(changes=[]),
    lambda v: v["changes"][0].update(reason=" "),
    lambda v: v["changes"].append({"point_id": "p2", "field": "argument", "reason": "假称修改"}),
    lambda v: v["changes"].append(dict(v["changes"][0])),
])
def test_illegal_repairs_terminate_without_second_review(tmp_path, mutate):
    fixed = payload()
    fixed["core_points"][0]["statement"] = "甲禁止开网"
    envelope = repair(fixed, [("p1", "statement")])
    mutate(envelope)
    client = Client(payload(), checks([("p1", "statement", "negation")]), envelope)
    result = executor(tmp_path, client).run()
    assert result.status == "technical_failure"
    assert result.diagnostics[0].category == "repair_invalid"
    assert len(client.calls) == 3 and executor(tmp_path, Client()).run() == result


def test_swapping_rejected_even_if_all_fields_diagnosed(tmp_path):
    old = payload()
    swapped = copy.deepcopy(old)
    swapped["core_points"][0], swapped["other_points"][0] = swapped["other_points"][0], swapped["core_points"][0]
    fields = [(pid, field) for pid in ("p1", "p2") for field in sorted(support.FIELDS)]
    client = Client(old, checks([(pid, field, "cross_point") for pid, field in fields]), repair(swapped, fields))
    assert executor(tmp_path, client).run().status == "technical_failure"
    assert len(client.calls) == 3


def test_same_candidate_same_failure_stops_after_one_repair(tmp_path):
    failure = checks([("p1", "statement", "negation")])
    client = Client(payload(), failure, repair(payload(), []), failure)
    result = executor(tmp_path, client).run()
    assert result.status == "no_improvement" and result.used_generations == 2
    assert len(client.calls) == 4


def test_changed_candidate_and_reason_without_strict_subset_not_progress(tmp_path):
    fixed = payload()
    fixed["core_points"][0]["statement"] = "甲或许需要开网"
    failure = checks([("p1", "statement", "negation")])
    changed_reason = checks([("p1", "statement", "strength")])
    changed_reason["checks"][0]["issues"][0]["reason"] = "换理由不改善"
    client = Client(payload(), failure, repair(fixed, [("p1", "statement")]), changed_reason)
    assert executor(tmp_path, client).run().status == "no_improvement"
    assert len(client.calls) == 4


def test_new_failure_field_prevents_recovery(tmp_path):
    fixed = payload()
    fixed["core_points"][0]["statement"] = "甲禁止开网"
    client = Client(payload(), checks([("p1", "statement", "negation")]),
                    repair(fixed, [("p1", "statement")]), checks([("p1", "argument", "unsupported")]))
    assert executor(tmp_path, client).run().status == "no_improvement"


def test_default_initial_plus_two_max_generation_three(tmp_path):
    first = payload()
    second, third = copy.deepcopy(first), copy.deepcopy(first)
    second["core_points"][0]["statement"] = "甲禁止开网"
    third["core_points"][0]["statement"] = second["core_points"][0]["statement"]
    third["core_points"][0]["argument"] = "来源禁止联网"
    client = Client(first, checks([("p1", f, "unsupported") for f in sorted(support.FIELDS)]),
        repair(second, [("p1", "statement")]), checks([("p1", "argument", "unsupported"), ("p1", "source_ranges", "cross_point")]),
        repair(third, [("p1", "argument")]), checks([("p1", "source_ranges", "cross_point")]))
    result = executor(tmp_path, client).run()
    assert result.status == "budget_exhausted" and result.used_generations == 3
    assert len(client.calls) == 6 and executor(tmp_path, Client()).run() == result
    assert all(a.review_raw is not None for a in result.attempts)


def test_zero_budget_retains_candidate_and_diagnostics(tmp_path):
    client = Client(payload(), checks([("p1", "source_ranges", "cross_point")]))
    result = executor(tmp_path, client, max_repairs=0).run()
    assert result.status == "budget_exhausted" and result.candidate is not None
    assert result.diagnostics[0].field_path == "$.core_points[0].source_ranges"
    assert len(client.calls) == 2


def test_structure_to_valid_support_failure_is_stage_progress(tmp_path):
    bad = payload()
    bad["core_points"][0]["source_ranges"][0]["start_segment"] = "s99"
    first = payload()
    fixed = copy.deepcopy(first)
    fixed["core_points"][0]["statement"] = "甲禁止开网"
    client = Client(bad, repair(first, [("p1", "source_ranges")], "regenerate"),
                    checks([("p1", "statement", "negation")]), repair(fixed, [("p1", "statement")]), checks())
    result = executor(tmp_path, client).run()
    assert result.status == "supported_candidate_not_published" and result.used_generations == 3
    assert len(client.calls) == 5
    assert json.loads(client.calls[1].payload_json)["mode"] == "regenerate"


@pytest.mark.parametrize("change", ["drop", "local"])
def test_structure_invalid_parent_regenerate_preserves_recognizable_counts(tmp_path, change):
    bad = payload()
    bad["core_points"][0]["source_ranges"] = []
    fixed = payload()
    envelope = repair(fixed, [("p1", "source_ranges")], "regenerate")
    if change == "drop":
        envelope["candidate"]["other_points"] = []
    else:
        envelope["mode"] = "local"
    client = Client(bad, envelope)
    assert executor(tmp_path, client).run().status == "technical_failure" and len(client.calls) == 2


def test_unparseable_parent_can_regenerate_with_all_actual_point_fields(tmp_path):
    fields = [(pid, f) for pid in ("p1", "p2") for f in sorted(support.FIELDS)]
    client = Client("{", repair(payload(), fields, "regenerate"), checks())
    result = executor(tmp_path, client).run()
    assert result.status == "supported_candidate_not_published" and len(client.calls) == 3


@pytest.mark.parametrize("responses,category", [
    ((RuntimeError("合成私密来源"),), "network_failure"),
    ((payload(), RuntimeError("合成私密来源")), "network_failure"),
    ((payload(), {"checks": []}), "coverage_invalid"),
    ((payload(), "{"), "json_invalid"),
])
def test_technical_terminal_no_loop_no_leak_reuse(tmp_path, responses, category):
    client = Client(*responses)
    result = executor(tmp_path, client).run()
    assert result.status == "technical_failure" and result.diagnostics[0].category == category
    assert result.used_generations == 1
    assert "合成私密来源" not in str(result.diagnostics)
    assert executor(tmp_path, Client()).run() == result


@pytest.mark.parametrize("operation", ["generate", "support"])
def test_reserved_without_response_restart_interrupted_never_resends(tmp_path, operation):
    client = Client(Crash()) if operation == "generate" else Client(payload(), Crash())
    with pytest.raises(Crash):
        executor(tmp_path, client).run()
    never = Client()
    result = executor(tmp_path, never).run()
    assert result.status == "interrupted" and result.used_generations == 1 and not never.calls
    assert executor(tmp_path, never).run() == result


@pytest.mark.parametrize("kind", ["generation", "review"])
@pytest.mark.parametrize("after_pointer", [False, True])
def test_received_crash_resume_exact_bytes_no_duplicate_call(tmp_path, monkeypatch, kind, after_pointer):
    original = module.write_record
    crashed = False
    def write(path, value):
        nonlocal crashed
        original(path, value)
        attempt = value.get("attempts", [{}])[-1]
        target = (Path(path).name == "state.json" and attempt.get("stage") == kind + "_received") if after_pointer else (
            Path(path).name == "attempt-0001-" + kind + ".json")
        if target and not crashed:
            crashed = True
            raise Crash()
    monkeypatch.setattr(module, "write_record", write)
    client = Client(payload(), checks())
    with pytest.raises(Crash):
        executor(tmp_path, client).run()
    before = len(client.calls)
    monkeypatch.setattr(module, "write_record", original)
    continuation = Client(checks()) if kind == "generation" else Client()
    result = executor(tmp_path, continuation).run()
    assert result.status == "supported_candidate_not_published"
    assert before + len(continuation.calls) == 2
    assert result.attempts[0].generation_raw == json.dumps(payload(), ensure_ascii=False)
    assert executor(tmp_path, Client()).run() == result


def test_request_reservation_and_used_budget_visible_before_each_client_call(tmp_path):
    seen = []
    ex = None
    def client(request):
        state = json.loads((ex.directory / "state.json").read_text())
        attempt = state["attempts"][-1]
        kind = "review" if request.operation == "support" else "generation"
        assert attempt["stage"] == kind + "_reserved"
        assert state["used_generations"] == len(state["attempts"]) == 1
        assert attempt[kind]["raw_hash"] is None
        assert not (ex.directory / attempt[kind]["file"]).exists()
        seen.append(kind)
        return json.dumps(checks() if kind == "review" else payload())
    ex = executor(tmp_path, client)
    assert ex.run().status == "supported_candidate_not_published" and seen == ["generation", "review"]


def test_crash_after_reservation_before_send_still_consumes_budget(tmp_path, monkeypatch):
    original = module.write_record
    def write(path, value):
        original(path, value)
        if Path(path).name == "state.json" and value["attempts"][-1]["stage"] == "generation_reserved":
            raise Crash()
    monkeypatch.setattr(module, "write_record", write)
    never = Client()
    with pytest.raises(Crash):
        executor(tmp_path, never).run()
    assert never.calls == []
    monkeypatch.setattr(module, "write_record", original)
    result = executor(tmp_path, never).run()
    assert result.status == "interrupted" and result.used_generations == 1 and never.calls == []


def test_restart_failed_stage_preserves_budget_and_continues_once(tmp_path, monkeypatch):
    original = module.write_record
    def write(path, value):
        original(path, value)
        if Path(path).name == "state.json" and value["attempts"][-1]["stage"] == "failed":
            raise Crash()
    monkeypatch.setattr(module, "write_record", write)
    client = Client(payload(), checks([("p1", "statement", "negation")]))
    with pytest.raises(Crash):
        executor(tmp_path, client).run()
    monkeypatch.setattr(module, "write_record", original)
    fixed = payload()
    fixed["core_points"][0]["statement"] = "甲禁止开网"
    resumed = Client(repair(fixed, [("p1", "statement")]), checks())
    result = executor(tmp_path, resumed).run()
    assert result.used_generations == 2 and len(client.calls) + len(resumed.calls) == 4


@pytest.mark.parametrize("kwargs", [{"model_config_hash": "b" * 64}, {"max_repairs": 0}, {"max_repairs": 10}])
def test_config_changes_cannot_reset_budget(tmp_path, kwargs):
    original = executor(tmp_path, Client(payload(), checks()))
    original.run()
    changed = executor(tmp_path, Client(), **kwargs)
    assert original.namespace == changed.namespace
    with pytest.raises(module.RecoveryError, match="configuration_mismatch"):
        changed.run()


def test_source_identity_snapshot_and_rule_version_namespaces(tmp_path, monkeypatch):
    a = executor(tmp_path, Client(payload(), checks()))
    a.run()
    b = executor(tmp_path, Client(payload(), checks()), source_fact_id="sf-synthetic-2")
    assert b.run().used_generations == 1 and a.namespace != b.namespace
    c = executor(tmp_path, Client(payload(), checks()), snapshot=SNAPSHOT + "合成背景。")
    assert c.run().used_generations == 1 and a.namespace != c.namespace
    namespaces = {a.namespace, b.namespace, c.namespace}
    for target, name in ((module, "RECOVERY_RULE_VERSION"), (support, "SUPPORT_RULE_VERSION"),
                         (module.candidates, "CONTRACT_VERSION"), (module.candidates, "PARSER_VERSION"),
                         (module.candidates, "VALIDATOR_VERSION")):
        with monkeypatch.context() as change:
            change.setattr(target, name, getattr(target, name) + "-future")
            new = executor(tmp_path, Client(payload(), checks()))
            assert new.namespace not in namespaces
            namespaces.add(new.namespace)
            assert new.run().used_generations == 1


@pytest.mark.parametrize("target", ["state", "identity", "receipt", "missing_state", "snapshot_binding"])
def test_corruption_never_fresh_initializes_or_calls(tmp_path, target):
    ex = executor(tmp_path, Client(payload(), checks()))
    ex.run()
    state_path = ex.directory / "state.json"
    if target == "state":
        state_path.write_text("{")
    elif target == "missing_state":
        state_path.unlink()
    elif target == "receipt":
        path = ex.directory / "attempt-0001-generation.json"
        value = json.loads(path.read_text())
        value["raw"] += " "
        path.write_text(json.dumps(value))
    else:
        value = json.loads(state_path.read_text())
        value["identity"]["source_fact_id" if target == "identity" else "snapshot_sha256"] = "sf-synthetic-wrong"
        value["seal"] = module._hash({k: v for k, v in value.items() if k != "seal"})
        state_path.write_text(json.dumps(value))
    never = Client()
    with pytest.raises(module.RecoveryError, match="checkpoint_corrupt"):
        executor(tmp_path, never).run()
    assert not never.calls


@pytest.mark.parametrize("target", ["root", "ancestor", "namespace", "lock", "state", "receipt"])
def test_symlinks_rejected_at_every_checkpoint_boundary(tmp_path, target):
    ex = executor(tmp_path, Client(payload(), checks()))
    if target in {"root", "ancestor", "namespace"}:
        destination = tmp_path / "symlink-destination"
        destination.mkdir()
        if target == "root":
            ex.root.symlink_to(destination, target_is_directory=True)
        elif target == "ancestor":
            linked = tmp_path / "linked-ancestor"
            linked.symlink_to(destination, target_is_directory=True)
            ex = executor(tmp_path, Client(), checkpoint_root=linked / "child")
        else:
            ex.root.mkdir()
            ex.directory.symlink_to(destination, target_is_directory=True)
    else:
        ex.run()
        name = {"lock": ".lock", "state": "state.json", "receipt": "attempt-0001-generation.json"}[target]
        path = ex.directory / name
        preserved = tmp_path / "preserved-record"
        path.rename(preserved)
        path.symlink_to(preserved)
    with pytest.raises(module.RecoveryError, match="checkpoint_unsafe"):
        ex.run()


def test_parallel_executor_rejected_by_lock_while_client_active(tmp_path):
    attempts = []
    replies = [payload(), checks()]
    def client(request):
        other = executor(tmp_path, Client())
        with pytest.raises(module.RecoveryError, match="checkpoint_busy"):
            other.run()
        attempts.append(request.operation)
        return json.dumps(replies.pop(0))
    assert executor(tmp_path, client).run().status == "supported_candidate_not_published"
    assert attempts == ["generate", "support"]


def test_orphan_files_are_not_recovery_candidates(tmp_path):
    ex = executor(tmp_path, Client(Crash()))
    with pytest.raises(Crash):
        ex.run()
    (ex.directory / "orphan.json").write_text(json.dumps({"raw": json.dumps(payload())}))
    assert executor(tmp_path, Client()).run().status == "interrupted"


@pytest.mark.parametrize("name,value", [("max_repairs", True), ("max_repairs", -1), ("max_repairs", 11),
    ("max_repairs", 2.0), ("checkpoint_root", None), ("source_fact_id", ""), ("snapshot", ""),
    ("client", None), ("model_config_hash", "secret"), ("model_config_hash", "g" * 64)])
def test_explicit_inputs_and_bounds(tmp_path, name, value):
    with pytest.raises(module.RecoveryError, match="input_invalid"):
        executor(tmp_path, Client(), **{name: value})


def test_max_repairs_ten_is_accepted(tmp_path):
    assert executor(tmp_path, Client(payload(), checks()), max_repairs=10).run().used_generations == 1


def test_model_hash_hex_case_canonical(tmp_path):
    client = Client(payload(), checks())
    first = executor(tmp_path, client, model_config_hash="A" * 64).run()
    assert executor(tmp_path, Client(), model_config_hash="a" * 64).run() == first


def test_wrong_reference_requires_fake_support_then_only_diagnosed_range_repaired(tmp_path):
    old = payload()
    old["other_points"][0]["source_ranges"] = copy.deepcopy(old["core_points"][0]["source_ranges"])
    client = Client(old, checks([("p2", "source_ranges", "cross_point")]),
                    repair(payload(), [("p2", "source_ranges")]), checks())
    result = executor(tmp_path, client).run()
    assert result.status == "supported_candidate_not_published"
    assert result.attempts[0].review.failed_fields == {"$.other_points[0].source_ranges"}
    first_review = json.loads(client.calls[1].payload_json)
    assert first_review["points"][1]["evidence"][0]["text"] == "甲禁止开网。"
    last_review = json.loads(client.calls[3].payload_json)
    assert last_review["points"][1]["evidence"][0]["text"] == "乙先检查再执行。"
    assert len(first_review["points"]) == len(last_review["points"]) == 2


def test_regenerate_root_title_and_illegal_id_have_complete_program_diff(tmp_path):
    bad = payload()
    bad["title"] = bad["subtitle"]
    bad["id"] = "synthetic-illegal-id"
    fixed = payload()
    client = Client(bad, repair(fixed, [], "regenerate"), checks())
    result = executor(tmp_path, client).run()
    assert result.status == "supported_candidate_not_published"
    attempt = result.attempts[1]
    assert attempt.changes == () and attempt.diff_status == "exact_payload_diff"
    diffs = {d.field_path: d for d in attempt.payload_diff}
    assert set(diffs) == {'$["title"]', '$["id"]'}
    assert diffs['$["title"]'].kind == "changed"
    assert diffs['$["title"]'].before_hash == module._hash(bad["title"])
    assert diffs['$["title"]'].after_hash == module._hash(fixed["title"])
    assert diffs['$["id"]'].kind == "removed" and diffs['$["id"]'].after_hash is None
    assert attempt.parent_response_hash == module._text_hash(result.attempts[0].generation_raw)
    assert executor(tmp_path, Client()).run() == result


def test_unparseable_regenerate_diff_unproven_explicit_and_parent_raw_hash(tmp_path):
    fields = [(pid, f) for pid in ("p1", "p2") for f in sorted(support.FIELDS)]
    result = executor(tmp_path, Client("{", repair(payload(), fields, "regenerate"), checks())).run()
    assert result.attempts[1].diff_status == "diff_unproven"
    assert result.attempts[1].payload_diff == ()
    assert result.attempts[1].parent_response_hash == module._text_hash("{")


def test_structurally_invalid_parent_with_overflow_extra_field_stops_safely(tmp_path):
    raw = json.dumps(payload(), ensure_ascii=False)[:-1] + ',"synthetic-private-field":1e309}'
    client = Client(raw, repair(payload(), [], "regenerate"))
    result = executor(tmp_path, client).run()
    assert result.status == "technical_failure"
    assert result.diagnostics[0].category == "repair_invalid"
    assert "synthetic-private-field" not in str(result.diagnostics)
    assert len(client.calls) == 2 and result.attempts[0].generation_raw == raw
    assert executor(tmp_path, Client()).run() == result


@pytest.mark.parametrize("mutation", ["false_success", "false_no_knowledge", "unfinished_success", "detached_candidate", "detached_changes", "detached_diff"])
def test_resealed_logically_inconsistent_terminal_rejected_no_calls(tmp_path, mutation):
    fixed = payload()
    fixed["core_points"][0]["statement"] = "甲禁止开网"
    client = Client(payload(), checks([("p1", "statement", "negation")]), repair(fixed, [("p1", "statement")]), checks())
    ex = executor(tmp_path, client)
    ex.run()
    path = ex.directory / "state.json"
    state = json.loads(path.read_text())
    last = state["attempts"][-1]
    if mutation == "false_success":
        # Change successful review artifact and matching raw hash; the state
        # still claims success. A valid seal does not establish that judgment.
        receipt_path = ex.directory / last["review"]["file"]
        record = json.loads(receipt_path.read_text())
        record["raw"] = json.dumps(checks([("p1", "statement", "negation")]))
        record["raw_hash"] = module._text_hash(record["raw"])
        receipt_path.write_text(json.dumps(record))
        last["review"]["raw_hash"] = record["raw_hash"]
    elif mutation == "false_no_knowledge":
        state["status"] = "no_knowledge"
    elif mutation == "unfinished_success":
        last["stage"] = "review_received"
    elif mutation == "detached_candidate":
        value = json.loads(last["candidate_text"])
        value["title"] = "独立状态伪造候选"
        last["candidate_text"] = json.dumps(value)
        last["candidate_hash"] = module.candidates.parse_knowledge_candidate(SNAPSHOT, last["candidate_text"]).candidate_hash
    elif mutation == "detached_changes":
        last["changes"][0]["reason"] = "状态与响应理由不一致"
    else:
        last["payload_diff"] = []
    state["seal"] = module._hash({k: v for k, v in state.items() if k != "seal"})
    path.write_text(json.dumps(state))
    never = Client()
    with pytest.raises(module.RecoveryError, match="checkpoint_corrupt"):
        executor(tmp_path, never).run()
    assert never.calls == []
