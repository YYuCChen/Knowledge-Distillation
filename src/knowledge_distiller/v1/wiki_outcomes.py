"""Private per-raw outcome candidate; no runner, kit or UI integration.

Source support stays owned by R14. A checked candidate is not published;
acceptance rechecks the existing publication journal and formal readback.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import os
import re
import sqlite3
import stat
from typing import Callable, Protocol

from .ingestion import digest, encoded, read_regular, envelope_fields
from .raw import FORMAT_VERSION
from .wiki_tasks import FrozenRaw, WikiTask, WikiTaskStore, TASK_STATES, _boundary
from .wiki_publish import PublishState, _load_journal
from .wiki_staging import StagingSnapshot, verify_recovered_publish
from .wiki_support import WikiSupportGate

CONTRACT = "r08-wiki-outcomes-v1"
VALUE_CHECKS = frozenset({"definition", "method", "reference_lead", "relations"})
NORMAL = frozenset({"processed_with_knowledge", "processed_no_knowledge"})


class OutcomeError(ValueError):
    pass


@dataclass(frozen=True)
class Outcome:
    raw_id: str
    content_sha256: str
    contract: str
    boundary_sha256: str
    status: str
    reason_code: str
    reason: str
    documents: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class NoKnowledgeReview:
    status: str  # verified / unsupported / unknown / technical_failed
    reason: str
    source_complete: bool
    considered: tuple[str, ...]


class NoKnowledgeChecker(Protocol):
    def review(self, *, raw: FrozenRaw, full_raw: bytes, outcome: Outcome,
               context: tuple[tuple[FrozenRaw, bytes], ...]) -> NoKnowledgeReview: ...


def frozen_context(task: WikiTask, batch_no: int, contents: dict[str, bytes]):
    if (type(batch_no) is not int or type(task.batch_count) is not int
            or type(task.raw_count) is not int or task.batch_count <= 0
            or task.raw_count != len(task.raw) or not task.raw
            or len(task.batches) != task.batch_count
            or [b.batch_no for b in task.batches] != list(range(1, task.batch_count + 1))
            or not 1 <= batch_no <= task.batch_count):
        raise OutcomeError("task_boundary_invalid")
    if (any(not isinstance(r, FrozenRaw) or not isinstance(r.raw_id, str)
            or not isinstance(r.relative_path, str) for r in task.raw)
            or len({r.raw_id for r in task.raw}) != len(task.raw)
            or len({r.relative_path for r in task.raw}) != len(task.raw)):
        raise OutcomeError("task_boundary_invalid")
    for ordinal, r in enumerate(task.raw, 1):
        path = r.relative_path.split("/")
        if (type(r.ordinal) is not int or r.ordinal != ordinal
                or type(r.batch_no) is not int or not 1 <= r.batch_no <= task.batch_count
                or not isinstance(r.raw_id, str) or re.fullmatch(r"R-\d{8}-\d{4}", r.raw_id) is None
                or not isinstance(r.identity, str) or r.identity not in {"第三方", "本人", "本人附言"}
                or len(path) != 5 or path[0] != "raw" or path[1] != ("外部" if r.identity == "第三方" else "自述")
                or path[-1] != r.raw_id + ".md" or "\\" in r.relative_path
                or any(p in {"", ".", ".."} for p in path)
                or type(r.byte_count) is not int or r.byte_count <= 0
                or not isinstance(r.content_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", r.content_sha256) is None):
            raise OutcomeError("task_boundary_invalid")
    if [r.batch_no for r in task.raw] != sorted(r.batch_no for r in task.raw):
        raise OutcomeError("task_boundary_invalid")
    for b in task.batches:
        if (type(b.batch_no) is not int or type(b.item_count) is not int or b.item_count <= 0
                or b.item_count != sum(r.batch_no == b.batch_no for r in task.raw)
                or not isinstance(b.state, str) or b.state not in TASK_STATES):
            raise OutcomeError("task_boundary_invalid")
    batches = tuple(tuple(r for r in task.raw if r.batch_no == batch.batch_no)
                    for batch in task.batches)
    if not task.raw or _boundary(batches) != task.boundary_sha256:
        raise OutcomeError("task_boundary_invalid")
    rows = tuple(r for r in task.raw if r.batch_no == batch_no)
    if (not rows or len({r.raw_id for r in rows}) != len(rows)
            or set(contents) != {r.raw_id for r in rows}):
        raise OutcomeError("raw_coverage_invalid")
    context = []
    for r in rows:
        content = contents[r.raw_id]
        if not isinstance(content, bytes) or len(content) != r.byte_count or digest(content) != r.content_sha256:
            raise OutcomeError("raw_hash_mismatch")
        try:
            fields = envelope_fields(content)
        except (ValueError, UnicodeError) as error:
            raise OutcomeError("raw_invalid") from error
        if (fields.get("编号") != r.raw_id or fields.get("身份") != r.identity
                or type(fields.get("格式版本")) is not int
                or fields["格式版本"] != FORMAT_VERSION):
            raise OutcomeError("raw_identity_invalid")
        context.append((r, content))
    return tuple(context)


def _specific_reason(reason):
    if not isinstance(reason, str) or not reason.strip():
        return False
    return reason.strip().strip("。.!！？? ").lower() not in {
        "太短", "内容太短", "无知识", "没有知识", "无可提炼知识", "无价值", "没价值",
        "不足", "不确定", "too short", "short", "no knowledge", "no_knowledge", "unknown", "none", "n/a"}


def plan_explicit_groups(raws: tuple[FrozenRaw, ...], contents: dict[str, bytes],
                         groups: tuple[tuple[str, ...], ...], *, budget: int,
                         measure: Callable[[tuple[tuple[FrozenRaw, bytes], ...]], int],
                         target_size: int = 5):
    """Keep an explicit same-topic group intact; reject rather than truncate.

Budget measurement is supplied by the real caller's complete prompt tokenizer,
not a chars/4 estimate. No automatic relation batch or synthesis is requested.
"""
    if type(budget) is not int or budget <= 0 or type(target_size) is not int or target_size <= 0:
        raise OutcomeError("budget_invalid")
    by_id = {r.raw_id: r for r in raws}
    if not raws or len(by_id) != len(raws) or set(contents) != set(by_id):
        raise OutcomeError("raw_coverage_invalid")
    grouped = {}
    for group in groups:
        if (len(group) < 2 or len(set(group)) != len(group)
                or not set(group) <= set(by_id) or set(group) & set(grouped)):
            raise OutcomeError("group_invalid")
        for raw_id in group:
            grouped[raw_id] = frozenset(group)
    units, seen = [], set()
    for r in raws:
        if r.raw_id in seen:
            continue
        ids = grouped.get(r.raw_id, frozenset({r.raw_id}))
        unit = tuple(x for x in raws if x.raw_id in ids)
        seen.update(ids)
        units.append(unit)
    batches, pending = [], ()
    for unit in units:
        if pending and len(pending) + len(unit) > target_size:
            batches.append(pending)
            pending = ()
        pending += unit
        if len(pending) >= target_size:
            batches.append(pending)
            pending = ()
    if pending:
        batches.append(pending)
    for batch in batches:
        context = tuple((r, contents[r.raw_id]) for r in batch)
        if any(not isinstance(c, bytes) or len(c) != r.byte_count or digest(c) != r.content_sha256
               for r, c in context):
            raise OutcomeError("raw_hash_mismatch")
        cost = measure(context)
        if type(cost) is not int or cost < 0 or cost > budget:
            raise OutcomeError("group_over_budget")
    return tuple(batches)


class WikiOutcomes:
    def __init__(self, candidate_database: Path):
        self.path = Path(candidate_database).absolute()

    @contextmanager
    def _db(self):
        if not self.path.parent.is_dir() or self.path.is_symlink():
            raise OutcomeError("candidate_path_invalid")
        for parent in self.path.parents:
            if parent.is_symlink():
                raise OutcomeError("candidate_path_invalid")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise OutcomeError("candidate_path_invalid")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise OutcomeError("candidate_permissions_invalid")
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=FULL")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"wiki_outcome_receipts"}:
                raise OutcomeError("candidate_database_required")
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS wiki_outcome_receipts(
                    receipt_id TEXT NOT NULL, phase TEXT NOT NULL,
                    payload TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    PRIMARY KEY(receipt_id,phase));
                CREATE TRIGGER IF NOT EXISTS wiki_outcome_receipts_no_update
                    BEFORE UPDATE ON wiki_outcome_receipts
                    BEGIN SELECT RAISE(ABORT,'immutable outcome'); END;
                CREATE TRIGGER IF NOT EXISTS wiki_outcome_receipts_no_delete
                    BEFORE DELETE ON wiki_outcome_receipts
                    BEGIN SELECT RAISE(ABORT,'immutable outcome'); END;
            """)

    def _save(self, receipt_id, phase, payload):
        text = encoded(payload)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if phase == "accepted":
                for other_id, other_text in db.execute(
                        "SELECT receipt_id,payload FROM wiki_outcome_receipts WHERE phase='accepted'"):
                    other = json.loads(other_text)
                    if (other_id != receipt_id and other["task_id"] == payload["task_id"]
                            and other["batch_no"] == payload["batch_no"]):
                        raise OutcomeError("accepted_boundary_conflict")
            old = db.execute("SELECT payload FROM wiki_outcome_receipts WHERE receipt_id=? AND phase=?",
                             (receipt_id, phase)).fetchone()
            if old is not None and old[0] != text:
                raise OutcomeError("receipt_conflict")
            db.execute("INSERT OR IGNORE INTO wiki_outcome_receipts VALUES(?,?,?,?)",
                       (receipt_id, phase, text, datetime.now(UTC).isoformat()))

    def get(self, receipt_id: str, phase="validated"):
        with self._db() as db:
            row = db.execute("SELECT payload FROM wiki_outcome_receipts WHERE receipt_id=? AND phase=?",
                             (receipt_id, phase)).fetchone()
        if row is None:
            raise OutcomeError("receipt_missing")
        return json.loads(row[0])

    def validate(self, task: WikiTask, batch_no: int, contents: dict[str, bytes],
                 outcomes: tuple[Outcome, ...], *, checker: NoKnowledgeChecker,
                 support_gate: WikiSupportGate | None = None, support_client=None):
        context = frozen_context(task, batch_no, contents)
        if len(outcomes) != len(context) or {o.raw_id for o in outcomes} != {r.raw_id for r, _ in context}:
            raise OutcomeError("outcome_coverage_invalid")
        ordered = {o.raw_id: o for o in outcomes}
        reviews = []
        for r, content in context:
            o = ordered[r.raw_id]
            if (o.contract != CONTRACT or o.boundary_sha256 != task.boundary_sha256
                    or o.content_sha256 != r.content_sha256 or not isinstance(o.status, str) or o.status not in NORMAL):
                raise OutcomeError("outcome_binding_invalid")
            if not o.documents or len(set(p for p, _ in o.documents)) != len(o.documents):
                raise OutcomeError("document_binding_invalid")
            for path, sha in o.documents:
                if (not isinstance(path, str) or not path.startswith("wiki/")
                        or not path.endswith(".md")
                        or "\\" in path or any(p in {"", ".", ".."} for p in path.split("/"))
                        or not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None):
                    raise OutcomeError("document_binding_invalid")
            if o.status == "processed_no_knowledge":
                if envelope_fields(content).get("未保留附件"):
                    raise OutcomeError("no_knowledge_unknown")
                if (not isinstance(o.reason_code, str)
                        or o.reason_code not in {"non_substantive", "no_distinct_claim", "support_only"}
                        or not _specific_reason(o.reason)):
                    raise OutcomeError("no_knowledge_reason_invalid")
                try:
                    result = checker.review(raw=r, full_raw=content, outcome=o, context=context)
                except Exception as error:
                    raise OutcomeError("no_knowledge_check_failed") from error
                if (not isinstance(result, NoKnowledgeReview) or type(result.status) is not str or result.status != "verified"
                        or type(result.source_complete) is not bool or result.source_complete is not True
                        or not _specific_reason(result.reason) or type(result.considered) is not tuple
                        or len(result.considered) != len(VALUE_CHECKS)
                        or any(type(value) is not str for value in result.considered)
                        or set(result.considered) != VALUE_CHECKS):
                    raise OutcomeError("no_knowledge_unknown")
                reviews.append({"raw_id": r.raw_id, **asdict(result)})
        support = None
        if any(o.status == "processed_with_knowledge" or o.reason_code == "support_only" for o in outcomes):
            if not isinstance(support_gate, WikiSupportGate):
                raise OutcomeError("source_support_required")
            registry = support_gate.registry
            if {(r.stable_id, r.sha256, r.content) for r in registry.raws} != {
                    (r.raw_id, r.content_sha256, c) for r, c in context}:
                raise OutcomeError("support_boundary_invalid")
            for o in outcomes:
                if (o.status == "processed_with_knowledge" or o.reason_code == "support_only") and not any(
                    claim.block.path in {p for p, _ in o.documents}
                    and any(e["stable_id"] == o.raw_id for e in claim.evidence)
                    for claim in registry.claims):
                    raise OutcomeError("source_claim_missing")
            result = support_gate.review(support_client)
            if result.status != "supported_candidate_not_published" or result.diagnostics:
                raise OutcomeError("source_support_failed")
            changed = {c.path: c.after_sha256 for c in registry.changes}
            if any(changed.get(p) != sha for o in outcomes for p, sha in o.documents):
                raise OutcomeError("support_document_mismatch")
            support = {"candidate_hash": result.candidate_hash,
                       "receipt_path": str(result.receipt_path),
                       "receipt_sha256": digest(read_regular(result.receipt_path.parent, result.receipt_path.name))}
        payload = {"contract": CONTRACT, "task_id": task.task_id, "batch_no": batch_no,
                   "boundary_sha256": task.boundary_sha256,
                   "raw": [asdict(r) for r, _ in context],
                   "outcomes": [asdict(ordered[r.raw_id]) for r, _ in context],
                   "reviews": reviews, "support": support}
        receipt_id = digest(encoded(payload).encode())
        self._save(receipt_id, "validated", payload)
        return receipt_id

    def accept(self, receipt_id: str, *, task_store: WikiTaskStore,
               snapshot: StagingSnapshot, journal: Path, lock):
        payload = self.get(receipt_id)
        if digest(encoded(payload).encode()) != receipt_id or payload["contract"] != CONTRACT:
            raise OutcomeError("receipt_binding_invalid")
        if self.path.resolve() == Path(task_store.database_path).resolve():
            raise OutcomeError("candidate_database_required")
        task = task_store.get(payload["task_id"])
        if (task.boundary_sha256 != payload["boundary_sha256"] or snapshot.task_id != task.task_id
                or [asdict(r) for r in task.raw if r.batch_no == payload["batch_no"]] != payload["raw"]):
            # JSON normalizes tuple-free FrozenRaw dictionaries exactly.
            raise OutcomeError("task_binding_invalid")
        batch = next((b for b in task.batches if b.batch_no == payload["batch_no"]), None)
        if batch is None or batch.state != "succeeded":
            raise OutcomeError("readback_required")
        root = Path(task.vault_path)
        if Path(journal).absolute() != snapshot.control / f"publish-{payload['batch_no']}":
            raise OutcomeError("journal_binding_invalid")
        # Same strict loader and formal input verifier as the existing publisher
        # recovery path. No caller supplied 'published=True' certificate.
        journal_bytes = read_regular(Path(journal), "journal.json")
        data = _load_journal(Path(journal), root)
        if (data.get("state") != PublishState.COMMITTED.value or not data["items"]
                or any(entry.get("status") != "verified" for entry in data["items"])):
            raise OutcomeError("committed_publish_required")
        after = {entry["path"]: entry["after"] for entry in data["items"]}
        if len(after) != len(data["items"]):
            raise OutcomeError("journal_duplicate_path")
        for o in payload["outcomes"]:
            for path, sha in o["documents"]:
                if after.get(path) != sha:
                    raise OutcomeError("published_document_mismatch")
        verify_recovered_publish(snapshot, root, after, lock=lock)
        if payload["support"] is not None:
            support = payload["support"]
            receipt = Path(support["receipt_path"])
            if digest(read_regular(receipt.parent, receipt.name)) != support["receipt_sha256"]:
                raise OutcomeError("source_support_receipt_changed")
        for r in task.raw:
            content = read_regular(root, r.relative_path)
            if len(content) != r.byte_count or digest(content) != r.content_sha256:
                raise OutcomeError("formal_raw_changed")
        if read_regular(Path(journal), "journal.json") != journal_bytes:
            raise OutcomeError("journal_changed")
        accepted = {**payload, "journal_sha256": digest(journal_bytes),
                    "published_after": after}
        self._save(receipt_id, "accepted", accepted)
        return accepted
