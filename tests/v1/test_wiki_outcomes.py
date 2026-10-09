"""Contract/control-flow only. Fake checkers do not establish model semantics."""
from dataclasses import replace
import sqlite3
import subprocess
import sys

import pytest

from knowledge_distiller.v1 import wiki_support as ws
from knowledge_distiller.v1.ingestion import digest, encoded
from knowledge_distiller.v1.wiki_outcomes import (
    CONTRACT, VALUE_CHECKS, NoKnowledgeReview, Outcome, OutcomeError,
    WikiOutcomes, frozen_context, plan_explicit_groups,
)
from knowledge_distiller.v1.wiki_tasks import FrozenRaw, WikiTask, WikiBatch, _boundary
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_publish import publish_wiki, PublishExpectation
from knowledge_distiller.v1.wiki_staging import prepare_staging, WikiStagingError
from .test_wiki_support import FakeClient
from .test_wiki_worker import _setup


def synthetic_task(count=1):
    rows, contents = [], {}
    for index in range(count):
        rid = f"R-20261008-{index + 1:04d}"
        content = (f"---\n编号: {rid}\n格式版本: 1\n身份: 第三方\n作者: 作者甲\n标题: 合成原件\n"
                   "---\n\n完整合成原文，包含条件与否定。\n\n^source-1\n").encode()
        contents[rid] = content
        rows.append(FrozenRaw(f"raw/外部/2026/10/{rid}.md", rid, "第三方", len(content),
                              digest(content), index + 1, 1))
    rows = tuple(rows)
    task = WikiTask(task_id="a" * 32, vault_path="/unused-synthetic-vault", vault_key="b" * 64,
                    request_kind="all", trigger_source="local_web", backend="codex_cli",
                    model="fake", effort="medium", kit_version="synthetic",
                    kit_manifest_sha256="c" * 64, boundary_sha256=_boundary((rows,)),
                    state="running", raw_count=count, batch_count=1, completed_batch_count=0,
                    error_code=None, recovery_state="not_needed", recovery_phase="none",
                    created_at="2026-10-08T00:00:00+00:00", updated_at="2026-10-08T00:00:00+00:00",
                    batches=(WikiBatch(1, "running", count, None),), raw=rows)
    return task, contents


def no_knowledge(task, r, *, documents=(("wiki/log.md", "d" * 64),)):
    return Outcome(r.raw_id, r.content_sha256, CONTRACT, task.boundary_sha256,
                   "processed_no_knowledge", "non_substantive",
                   "完整内容仅为结束致意，未包含定义、方法或检索线索。", documents)


class Checker:
    def __init__(self, status="verified", complete=True):
        self.status, self.complete, self.calls = status, complete, []

    def review(self, *, raw, full_raw, outcome, context):
        assert len(full_raw) == raw.byte_count and digest(full_raw) == raw.content_sha256
        self.calls.append((raw.raw_id, full_raw, context, outcome.reason))
        return NoKnowledgeReview(self.status, "合成检查：完整覆盖并考虑短定义、方法、线索与关系。",
                                 self.complete, tuple(sorted(VALUE_CHECKS)))


@pytest.fixture
def outcomes(tmp_path):
    candidate = WikiOutcomes(tmp_path / "outcomes.sqlite3")
    candidate.initialize()
    return candidate


def test_full_raw_independent_check_and_restart(outcomes):
    task, contents = synthetic_task()
    checker = Checker()
    rid = outcomes.validate(task, 1, contents, (no_knowledge(task, task.raw[0]),), checker=checker)
    assert checker.calls[0][1] == contents[task.raw[0].raw_id]
    assert checker.calls[0][2] == ((task.raw[0], contents[task.raw[0].raw_id]),)
    assert WikiOutcomes(outcomes.path).get(rid)["reviews"][0]["source_complete"] is True
    with pytest.raises(OutcomeError, match="receipt_missing"):
        outcomes.get(rid, "accepted")
    assert outcomes.validate(task, 1, contents, (no_knowledge(task, task.raw[0]),), checker=Checker()) == rid


@pytest.mark.parametrize("damage", ["missing", "duplicate", "unknown", "hash", "contract", "boundary", "failed"])
def test_exact_coverage_and_binding_rejects(outcomes, damage):
    task, contents = synthetic_task(2)
    proposed = [no_knowledge(task, r) for r in task.raw]
    if damage == "missing":
        proposed.pop()
    elif damage == "duplicate":
        proposed[1] = proposed[0]
    else:
        fields = {"unknown": {"raw_id": "R-20000101-9999"}, "hash": {"content_sha256": "0" * 64},
                  "contract": {"contract": "old"}, "boundary": {"boundary_sha256": "0" * 64},
                  "failed": {"status": "technical_failed"}}[damage]
        proposed[0] = replace(proposed[0], **fields)
    checker = Checker()
    with pytest.raises(OutcomeError):
        outcomes.validate(task, 1, contents, tuple(proposed), checker=checker)
    with sqlite3.connect(outcomes.path) as db:
        assert db.execute("SELECT COUNT(*) FROM wiki_outcome_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("reason,code", [("太短", "non_substantive"), ("", "non_substantive"),
                                          ("日志里已经列出路径", "log_string"), ("API离线", "technical_failed")])
def test_no_empty_short_or_technical_reason(outcomes, reason, code):
    task, contents = synthetic_task()
    proposed = replace(no_knowledge(task, task.raw[0]), reason=reason, reason_code=code)
    with pytest.raises(OutcomeError, match="no_knowledge_reason_invalid"):
        outcomes.validate(task, 1, contents, (proposed,), checker=Checker())


@pytest.mark.parametrize("status,complete", [("unknown", True), ("technical_failed", True),
                                              ("unsupported", True), ("verified", False)])
def test_missing_context_and_short_value_uncertainty_never_normal(outcomes, status, complete):
    task, contents = synthetic_task()
    with pytest.raises(OutcomeError, match="no_knowledge_unknown"):
        outcomes.validate(task, 1, contents, (no_knowledge(task, task.raw[0]),),
                          checker=Checker(status, complete))


def test_checker_failure_and_incomplete_value_check_are_unknown(outcomes):
    task, contents = synthetic_task()
    class Failed:
        def review(self, **kwargs):
            raise TimeoutError("synthetic timeout")
    class Partial:
        def review(self, **kwargs):
            return NoKnowledgeReview("verified", "没有独立知识", True, ("definition",))
    for checker in (Failed(), Partial()):
        with pytest.raises(OutcomeError):
            outcomes.validate(task, 1, contents, (no_knowledge(task, task.raw[0]),), checker=checker)


@pytest.mark.parametrize("text", ["定义：熵是状态不确定性的度量。", "[听辨不清]缺少问题前提。"])
def test_full_short_value_or_missing_context_reaches_independent_checker(outcomes, text):
    task, contents = synthetic_task()
    r = task.raw[0]
    full = contents[r.raw_id].replace("完整合成原文，包含条件与否定。".encode(), text.encode())
    r = replace(r, byte_count=len(full), content_sha256=digest(full))
    task = replace(task, raw=(r,), boundary_sha256=_boundary(((r,),)))
    class ValueChecker:
        def review(self, *, raw, full_raw, outcome, context):
            assert full_raw == full and len(context) == 1
            return NoKnowledgeReview("unsupported" if "定义：" in full_raw.decode() else "unknown",
                                     "短定义有价值或语义关键上下文未完成，拒绝无知识。",
                                     "听辨不清" not in full_raw.decode(), tuple(VALUE_CHECKS))
    with pytest.raises(OutcomeError, match="no_knowledge_unknown"):
        outcomes.validate(task, 1, {r.raw_id: full}, (no_knowledge(task, r),), checker=ValueChecker())


def test_fourteen_explicit_group_complete_and_not_total_limit():
    task, contents = synthetic_task(20)
    group = tuple(r.raw_id for r in task.raw[:14])
    batches = plan_explicit_groups(task.raw, contents, (group,), budget=100000,
                                   measure=lambda ctx: sum(len(c) for _, c in ctx))
    assert [len(b) for b in batches] == [14, 5, 1]
    assert tuple(r.raw_id for r in batches[0]) == group
    with pytest.raises(OutcomeError, match="group_over_budget"):
        plan_explicit_groups(task.raw, contents, (group,), budget=1,
                             measure=lambda ctx: sum(len(c) for _, c in ctx))
    with pytest.raises(OutcomeError, match="group_invalid"):
        plan_explicit_groups(task.raw, contents, (group, group), budget=100000, measure=lambda ctx: 1)


def _supported(outcomes, tmp_path, *, unsupported=False):
    task, contents = synthetic_task(14)
    stage = tmp_path / "stage"
    stage.mkdir()
    for r in task.raw:
        target = stage / r.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[r.raw_id])
    document_path = "wiki/来源/合成.md"
    document = ("## 核心论点\n" + "\n\n".join(
        f"作者甲陈述完整条件（{r.relative_path}#^source-1）。" for r in task.raw[:12])).encode()
    target = stage / document_path
    target.parent.mkdir(parents=True)
    target.write_bytes(document)
    change = ws.DocumentChange(document_path, None, None, document, digest(document))
    registry = ws.build_registry(stage, (change,), tuple(
        ws.FrozenRaw(r.relative_path, r.raw_id, contents[r.raw_id], r.content_sha256) for r in task.raw))
    gate = ws.WikiSupportGate(tmp_path / "support", "same_topic_14", registry, "e" * 64)
    proposed = tuple(Outcome(r.raw_id, r.content_sha256, CONTRACT, task.boundary_sha256,
                            "processed_with_knowledge", "", "", ((document_path, digest(document)),))
                     if index < 12 else no_knowledge(task, r, documents=((document_path, digest(document)),))
                     for index, r in enumerate(task.raw))
    return outcomes.validate(task, 1, contents, proposed, checker=Checker(), support_gate=gate,
                             support_client=FakeClient({0: "unsupported"} if unsupported else {}))


def test_twelve_knowledge_two_normal_without_old_sql(outcomes, tmp_path):
    rid = _supported(outcomes, tmp_path)
    payload = outcomes.get(rid)
    assert len(payload["outcomes"]) == 14 and len(payload["reviews"]) == 2
    assert sum(o["status"] == "processed_with_knowledge" for o in payload["outcomes"]) == 12
    assert payload["support"]["candidate_hash"]
    with sqlite3.connect(outcomes.path) as db:
        assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"wiki_outcome_receipts"}


def test_r14_failure_cannot_be_relabelled_completed(outcomes, tmp_path):
    with pytest.raises(OutcomeError, match="source_support_failed"):
        _supported(outcomes, tmp_path, unsupported=True)
    with sqlite3.connect(outcomes.path) as db:
        assert db.execute("SELECT COUNT(*) FROM wiki_outcome_receipts").fetchone()[0] == 0


def test_with_knowledge_and_support_only_require_real_r14_gate(outcomes):
    task, contents = synthetic_task()
    for proposed in (replace(no_knowledge(task, task.raw[0]), status="processed_with_knowledge"),
                     replace(no_knowledge(task, task.raw[0]), reason_code="support_only")):
        with pytest.raises(OutcomeError, match="source_support_required"):
            outcomes.validate(task, 1, contents, (proposed,), checker=Checker())


def _publish_fixture(tmp_path, outcomes):
    vault, runtime, _, store, task = _setup(tmp_path)
    contents = {r.raw_id: (vault / r.relative_path).read_bytes() for r in task.raw}
    lock = VaultWriteLock.acquire(vault)
    snapshot = prepare_staging(vault, runtime, task.task_id, task.raw,
                               python_executable=sys.executable, source_kit_root=store.kit_root, lock=lock)
    path = "wiki/log.md"
    before = (vault / path).read_bytes()
    after = before + b"\nSynthetic private no-knowledge process receipt.\n"
    (snapshot.workspace / path).write_bytes(after)
    proposed = no_knowledge(task, task.raw[0], documents=((path, digest(after)),))
    rid = outcomes.validate(task, 1, contents, (proposed,), checker=Checker())
    journal = snapshot.control / "publish-1"
    store.set_task_state(task.task_id, "preparing")
    store.set_task_state(task.task_id, "running")
    for state in ("preparing", "running", "validating", "publishing"):
        store.set_batch_state(task.task_id, 1, state)
    return vault, store, task, lock, snapshot, journal, rid, {path: PublishExpectation(digest(before), digest(after))}


def test_accepted_only_committed_existing_publish_and_readback(outcomes, tmp_path):
    vault, store, task, lock, snapshot, journal, rid, expected = _publish_fixture(tmp_path, outcomes)
    try:
        with pytest.raises(OutcomeError, match="readback_required"):
            outcomes.accept(rid, task_store=store, snapshot=snapshot, journal=journal, lock=lock)
        publish_wiki(vault, snapshot.workspace, journal, expected, lock=lock)
        with pytest.raises(OutcomeError, match="readback_required"):
            outcomes.accept(rid, task_store=store, snapshot=snapshot, journal=journal, lock=lock)
        store.mark_batch_readback_succeeded(task.task_id, 1)
        result = outcomes.accept(rid, task_store=store, snapshot=snapshot, journal=journal, lock=lock)
        assert result["journal_sha256"] and result["published_after"] == {p: e.after_sha256 for p, e in expected.items()}
        assert WikiOutcomes(outcomes.path).accept(rid, task_store=store, snapshot=snapshot,
                                                 journal=journal, lock=lock) == result
        (vault / "wiki/log.md").write_text("later synthetic user edit", encoding="utf-8")
        with pytest.raises(WikiStagingError, match="publish_conflict"):
            outcomes.accept(rid, task_store=store, snapshot=snapshot, journal=journal, lock=lock)
        assert (vault / "wiki/log.md").read_text() == "later synthetic user edit"
    finally:
        lock.close()


def test_readback_flag_without_any_publish_is_insufficient(outcomes, tmp_path):
    _, store, task, lock, snapshot, journal, rid, _ = _publish_fixture(tmp_path, outcomes)
    try:
        store.mark_batch_readback_succeeded(task.task_id, 1)
        with pytest.raises(ValueError):
            outcomes.accept(rid, task_store=store, snapshot=snapshot, journal=journal, lock=lock)
        with pytest.raises(OutcomeError, match="receipt_missing"):
            outcomes.get(rid, "accepted")
    finally:
        lock.close()


def test_different_candidate_cannot_replace_accepted_same_batch(outcomes, tmp_path):
    vault, store, task, lock, snapshot, journal, rid, expected = _publish_fixture(tmp_path, outcomes)
    try:
        publish_wiki(vault, snapshot.workspace, journal, expected, lock=lock)
        store.mark_batch_readback_succeeded(task.task_id, 1)
        outcomes.accept(rid, task_store=store, snapshot=snapshot, journal=journal, lock=lock)
        contents = {r.raw_id: (vault / r.relative_path).read_bytes() for r in task.raw}
        o = no_knowledge(task, task.raw[0], documents=tuple((p, e.after_sha256) for p, e in expected.items()))
        other = outcomes.validate(task, 1, contents, (replace(o, reason="另一候选具体理由，不能改写已接受终态。"),), checker=Checker())
        with pytest.raises(OutcomeError, match="accepted_boundary_conflict"):
            outcomes.accept(other, task_store=store, snapshot=snapshot, journal=journal, lock=lock)
        with pytest.raises(OutcomeError, match="receipt_missing"):
            outcomes.get(other, "accepted")
    finally:
        lock.close()


def test_corrupt_bytes_and_old_contract_do_not_resume(outcomes):
    task, contents = synthetic_task()
    with pytest.raises(OutcomeError, match="raw_hash_mismatch"):
        frozen_context(task, 1, {task.raw[0].raw_id: b"truncated"})
    with pytest.raises(OutcomeError, match="task_boundary_invalid"):
        frozen_context(replace(task, boundary_sha256="0" * 64), 1, contents)


@pytest.mark.parametrize("damage", ["orphan", "duplicate_other_batch", "ordinal", "batch_duplicate", "item_count", "raw_count"])
def test_complete_task_metadata_checked_even_outside_requested_batch(damage):
    original, all_contents = synthetic_task(2)
    rows = (original.raw[0], replace(original.raw[1], batch_no=2))
    batches = (WikiBatch(1, "running", 1, None), WikiBatch(2, "queued", 1, None))
    boundary = _boundary(((rows[0],), (rows[1],)))
    task = replace(original, raw=rows, batches=batches, batch_count=2, boundary_sha256=boundary)
    if damage == "orphan":
        # The real algorithm only hashes supplied batches: orphan ordinal is omitted.
        orphan = replace(rows[1], raw_id="R-20261008-0099", relative_path="raw/外部/2026/10/R-20261008-0099.md",
                         ordinal=3, batch_no=99)
        task = replace(task, raw=rows + (orphan,), raw_count=3)
        assert _boundary(tuple(tuple(r for r in task.raw if r.batch_no == b.batch_no) for b in task.batches)) == boundary
    elif damage == "duplicate_other_batch":
        second = replace(rows[1], raw_id=rows[0].raw_id)
        task = replace(task, raw=(rows[0], second), boundary_sha256=_boundary(((rows[0],), (second,))))
    elif damage == "ordinal":
        # Existing _boundary excludes ordinal entirely, so hash remains unchanged.
        task = replace(task, raw=(rows[0], replace(rows[1], ordinal=1)))
    elif damage == "batch_duplicate":
        task = replace(task, batches=(batches[0], replace(batches[1], batch_no=1)))
    elif damage == "item_count":
        task = replace(task, batches=(batches[0], replace(batches[1], item_count=2)))
    else:
        task = replace(task, raw_count=99)
    with pytest.raises(OutcomeError, match="task_boundary_invalid"):
        frozen_context(task, 1, {rows[0].raw_id: all_contents[rows[0].raw_id]})


@pytest.mark.parametrize("fields", [{"reason": "太短。"}, {"reason": "无知识"}, {"reason": None},
                                    {"reason": 123}, {"status": True}, {"source_complete": 1},
                                    {"considered": list(VALUE_CHECKS)},
                                    {"considered": ("definition", "method", "reference_lead", {})}])
def test_independent_review_reason_and_types_are_strict(outcomes, fields):
    task, contents = synthetic_task()
    review = replace(NoKnowledgeReview("verified", "完整原文仅为问候，已核对四类价值。",
                                      True, tuple(sorted(VALUE_CHECKS))), **fields)
    class InvalidChecker:
        def review(self, **kwargs):
            return review
    with pytest.raises(OutcomeError, match="no_knowledge_unknown"):
        outcomes.validate(task, 1, contents, (no_knowledge(task, task.raw[0]),), checker=InvalidChecker())


def test_concurrent_private_receipt_commit_is_idempotent(outcomes):
    payload = {"contract": CONTRACT, "synthetic": "crash-before-commit"}
    rid = digest(encoded(payload).encode())
    code = """
import os,sqlite3,sys,json
from pathlib import Path
from knowledge_distiller.v1.wiki_outcomes import WikiOutcomes
c=WikiOutcomes(Path(sys.argv[1]));c.initialize()
if sys.argv[4]=='crash':
 d=sqlite3.connect(c.path);d.execute('BEGIN IMMEDIATE')
 d.execute('INSERT INTO wiki_outcome_receipts VALUES(?,?,?,?)',(sys.argv[2],'validated',sys.argv[3],'synthetic'))
 os._exit(23)
c._save(sys.argv[2],'validated',json.loads(sys.argv[3]))
"""
    args = [sys.executable, "-c", code, str(outcomes.path), rid, encoded(payload)]
    crash = subprocess.run(args + ["crash"], capture_output=True, timeout=30)
    assert crash.returncode == 23, crash.stderr
    with pytest.raises(OutcomeError, match="receipt_missing"):
        outcomes.get(rid)
    children = [subprocess.Popen(args + ["normal"], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for child in children:
        _, err = child.communicate(timeout=30)
        assert child.returncode == 0, err
    assert outcomes.get(rid) == payload
    with sqlite3.connect(outcomes.path) as db:
        assert db.execute("SELECT COUNT(*) FROM wiki_outcome_receipts").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM wiki_outcome_receipts")
