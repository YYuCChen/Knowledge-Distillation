"""Private, deterministic legacy mapping compiler. No I/O or publication API.

All evidence and occupied destinations must be supplied explicitly. This is
not a database adapter, raw allocator, wiki publisher, or identity classifier.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
import re
from typing import Iterable


CONTRACT = "kd-legacy-mapping/1"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(value: object) -> bytes:
    # ASCII JSON preserves even anomalous legacy Unicode without normalization.
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _value(value: object) -> object:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, dict):
        return {key: _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return value


@dataclass(frozen=True)
class LegacyKey:
    collection: str
    record: str
    version: str


@dataclass(frozen=True)
class SourceEvidence:
    """Explicit frozen source, including exact identity, bytes and citation."""
    key: LegacyKey
    reference: str
    content: bytes = field(repr=False)
    sha256: str
    role: str = "legacy_record"  # raw, source_page, or private legacy_record
    raw_id: str = ""  # Existing terminal ID, never allocated by this compiler.
    raw_identity: str = ""  # Exact recorded 第三方 / 本人 / 本人附言.


@dataclass(frozen=True)
class HistoricalAction:
    key: LegacyKey
    parent: LegacyKey
    decision: str
    occurred_at: str
    evidence: SourceEvidence
    # A change references the exact preceding rethink action, never its title.
    prior_action: LegacyKey | None = None


@dataclass(frozen=True)
class IdentityConfirmation:
    """Caller-supplied explicit user adjudication; never inferred by compiler."""
    input_key: LegacyKey
    parent: LegacyKey
    text_sha256: str
    author: str
    expression: str
    evidence: SourceEvidence


@dataclass(frozen=True)
class LegacyFact:
    key: LegacyKey
    kind: str  # ai_insight, action, user_text, topic
    legacy_number: str
    text: str = field(repr=False)
    evidence: tuple[SourceEvidence, ...]
    lineage: tuple[SourceEvidence, ...] = ()
    actions: tuple[HistoricalAction, ...] = ()
    parent: LegacyKey | None = None
    confirmation: IdentityConfirmation | None = None
    title: str = field(default="", repr=False)
    occurred_at: str = ""
    # Exact complete historical Topic export (name/scope/members/order/snapshot).
    # Caller must explicitly attest export completeness; compiler cannot query it.
    topic_complete: bool = False
    # The explicit expected boundary prevents dropping one missing participant
    # from a multi-source export and treating the remaining subset as complete.
    expected_lineage: tuple[LegacyKey, ...] = ()
    # Exact extra export of event boundaries/relations/previous versions/lifecycle.
    # No relationships are reconstructed from text or title similarity.
    legacy_context: bytes = field(default=b"", repr=False)
    lineage_issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    external_id: str
    kind: str
    relative_path: str
    content: bytes = field(repr=False)
    sha256: str
    legacy_key: LegacyKey
    legacy_number: str
    references: tuple[str, ...]
    requires_authorization: bool = True


@dataclass(frozen=True)
class PrivateReceipt:
    receipt_id: str
    key: LegacyKey
    input_sha256: str
    status: str
    reasons: tuple[str, ...]
    # Includes exact original text, AI text, actions, source bytes and confirmation.
    # MUST NOT be routed to public logs, UI, ordinary telemetry or wiki/log.md.
    private_input: bytes = field(repr=False)
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class OccupiedTarget:
    relative_path: str
    sha256: str
    owner_external_id: str | None = None  # None denotes manual/unknown ownership.


@dataclass(frozen=True)
class MappingPlan:
    receipts: tuple[PrivateReceipt, ...]
    dry_run: bool = True

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        return tuple(c for r in self.receipts for c in r.candidates)

    def private_bytes(self) -> bytes:
        """Canonical private plan; includes sensitive originals. No disk writes."""
        return _json({"contract": CONTRACT, **_value(asdict(self))})

    def public_summary(self) -> dict[str, object]:
        """Only aggregate fixed states, no text, identity, paths or source hashes."""
        return {"contract": CONTRACT, "dry_run": True,
                "items": len(self.receipts), "candidates": len(self.candidates),
                "states": dict(sorted(Counter(r.status for r in self.receipts).items()))}


def _id(domain: str, key: LegacyKey) -> str:
    digest = sha256(_json([CONTRACT, domain, asdict(key)]))
    return f"kd-legacy:{domain}:{digest}"


def _key_ok(key: LegacyKey | None) -> bool:
    return isinstance(key, LegacyKey) and all(
        isinstance(v, str) and bool(v.strip()) and not any(ord(c) < 32 for c in v)
        for v in (key.collection, key.record, key.version))


def _reference_ok(reference: str, *, lineage: bool = False) -> bool:
    if not isinstance(reference, str):
        return False
    path, _, anchor = reference.partition("#")
    parts = path.split("/")
    allowed = (parts[0] == "raw" or parts[:2] == ["wiki", "来源"])
    if not lineage:
        allowed = allowed or parts[:2] == ["private", "legacy"]
    return (len(parts) >= 3 and allowed
            and path.endswith((".md",) if lineage else (".md", ".json", ".txt"))
            and all(p not in {"", ".", ".."} for p in parts)
            and not any(c in reference for c in "\\%[]|<>\"'`:")
            and not any(ord(c) < 32 or ord(c) == 127 for c in reference)
            and ("#" not in reference or bool(re.fullmatch(r"\^[A-Za-z0-9_-]+", anchor))))


def _evidence_errors(items: tuple[SourceEvidence, ...], *, lineage: bool = False) -> set[str]:
    errors: set[str] = set()
    seen: dict[LegacyKey, str] = {}
    paths: dict[str, tuple[LegacyKey, str]] = {}
    for item in items:
        if not _key_ok(item.key):
            errors.add("missing_source_identity")
        safe_reference = isinstance(item.reference, str)
        if not _reference_ok(item.reference, lineage=lineage):
            errors.add("unsafe_source_reference")
        if lineage and (not safe_reference or item.role != "raw"
                        or not item.reference.startswith("raw/")):
            errors.add("invalid_support_identity")
        if lineage:
            errors.update(_terminal_raw_errors(item))
        if not isinstance(item.content, bytes) or not item.content:
            errors.add("missing_source_bytes")
            continue
        digest = sha256(item.content)
        if digest != item.sha256:
            errors.add("source_sha_mismatch")
        signature = digest
        if item.key in seen and seen[item.key] != signature:
            errors.add("conflicting_source_version")
        seen[item.key] = signature
        path = item.reference.split("#", 1)[0] if safe_reference else ""
        if path in paths and paths[path] != (item.key, digest):
            errors.add("conflicting_source_path")
        paths[path] = (item.key, digest)
        anchor = item.reference.partition("#")[2] if safe_reference else ""
        if anchor and (not re.fullmatch(r"\^[A-Za-z0-9_-]+", anchor)
                       or anchor.encode("ascii") not in item.content.splitlines()):
            errors.add("missing_source_anchor")
    return errors


def _terminal_raw_errors(source: SourceEvidence) -> set[str]:
    """Verify explicitly supplied final raw identity and exact paragraph anchor."""
    errors: set[str] = set()
    if not isinstance(source.raw_id, str) or not re.fullmatch(r"R-[0-9]{8}-[0-9]{4,}", source.raw_id):
        errors.add("missing_terminal_raw_id")
    if source.raw_identity not in {"第三方", "本人", "本人附言"}:
        errors.add("missing_terminal_raw_identity")
    if not isinstance(source.reference, str):
        return errors | {"missing_terminal_raw_anchor"}
    path, _, anchor = source.reference.partition("#")
    if not re.fullmatch(r"\^source-[1-9][0-9]*", anchor):
        errors.add("missing_terminal_raw_anchor")
    if path.rsplit("/", 1)[-1] != source.raw_id + ".md":
        errors.add("terminal_raw_id_path_mismatch")
    if not isinstance(source.content, bytes):
        return errors | {"invalid_terminal_raw_envelope"}
    try:
        text = source.content.decode("utf-8")
        lines = text.splitlines()
        end = lines.index("---", 1) if lines and lines[0] == "---" else 0
        header: dict[str, str] = {}
        for line in lines[1:end]:
            name, sep, value = line.partition(":")
            if not sep or name not in {"编号", "身份"}:
                continue
            if name in header:
                raise ValueError("duplicate raw identity field")
            value = value.strip()
            header[name] = json.loads(value) if value.startswith('"') else value
        if header.get("编号") != source.raw_id or header.get("身份") != source.raw_identity:
            errors.add("terminal_raw_envelope_identity_mismatch")
        # An anchor in frontmatter is not a source paragraph.
        if anchor not in lines[end + 1:]:
            errors.add("missing_terminal_raw_anchor")
    except (UnicodeDecodeError, ValueError):
        errors.add("invalid_terminal_raw_envelope")
    return errors


def _version_lineage_errors(fact: LegacyFact) -> set[str]:
    """An explicit complete old-version manifest is mandatory for AI mapping.

    Upstream verifies the real graph; this compiler checks exact declared
    identity, terminal boundary and frozen dependency evidence, never guesses.
    """
    if not fact.legacy_context:
        return {"missing_legacy_version_lineage"}
    try:
        context = json.loads(fact.legacy_context, object_pairs_hook=_unique_pairs)
        if not isinstance(context, dict) or context.get("complete") is not True:
            return {"incomplete_legacy_version_lineage"}
        errors: set[str] = set()
        if context.get("version_key") != asdict(fact.key):
            errors.add("legacy_version_identity_conflict")
        keys = {LegacyKey(**entry) for entry in context["terminal_sources"]}
        if keys != set(fact.expected_lineage):
            errors.add("legacy_version_boundary_conflict")
        dependencies = context["dependencies"]
        previous = context["previous_version"]
        if not isinstance(dependencies, list):
            raise ValueError("dependencies must be explicit")
        if previous is not None:
            dependencies = dependencies + [previous]
            old_key = LegacyKey(**previous["key"])
            if (old_key.collection != fact.key.collection or old_key.record != fact.key.record
                    or old_key.version == fact.key.version):
                errors.add("legacy_previous_version_conflict")
        for entry in dependencies:
            dependency = LegacyKey(**entry["key"])
            if not _key_ok(dependency) or not any(
                    e.key == dependency and e.sha256 == entry["sha256"] for e in fact.evidence + fact.lineage):
                errors.add("missing_legacy_dependency_evidence")
        return errors
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return {"invalid_legacy_version_lineage"}


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate lineage field")
        result[name] = value
    return result


def _action_errors(fact: LegacyFact) -> set[str]:
    errors: set[str] = set()
    actions = {a.key: a for a in fact.actions}
    if len(actions) != len(fact.actions):
        errors.add("duplicate_action_identity")
    if sum(a.decision in {"interesting", "rethink"} for a in fact.actions) > 1:
        errors.add("conflicting_classification_actions")
    if sum(a.decision == "interesting_after_rethink" for a in fact.actions) > 1:
        errors.add("conflicting_change_actions")
    for action in fact.actions:
        expected_parent = fact.parent if fact.kind == "action" else fact.key
        if not _key_ok(action.key) or not _key_ok(action.parent):
            errors.add("missing_action_identity")
        if action.parent != expected_parent or action.evidence.key != action.key:
            errors.add("action_parent_conflict")
        if not _time_ok(action.occurred_at):
            errors.add("invalid_action_time")
        if action.decision not in {"interesting", "rethink", "interesting_after_rethink"}:
            errors.add("unknown_action")
        if action.decision == "interesting_after_rethink":
            previous = actions.get(action.prior_action)
            if previous is None or previous.decision != "rethink" or previous.parent != action.parent:
                errors.add("missing_rethink_lineage")
            elif _time_ok(previous.occurred_at) and _time_ok(action.occurred_at):
                if datetime.fromisoformat(previous.occurred_at) > datetime.fromisoformat(action.occurred_at):
                    errors.add("change_precedes_rethink")
        elif action.prior_action is not None:
            errors.add("unexpected_action_lineage")
        errors.update(_evidence_errors((action.evidence,)))
    return errors


def _time_ok(value: str) -> bool:
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _render(fact: LegacyFact, kind: str) -> Candidate:
    external_id = _id(kind, fact.key)
    digest = external_id.rsplit(":", 1)[1]
    # raw candidates are private proposals, deliberately outside raw/ and R-*.
    relative_path = (f"wiki/综合/旧版AI派生-{digest}.md" if kind == "ai_synthesis"
                     else f"private/raw-candidates/{digest}.md")
    refs = tuple(sorted({e.reference for e in fact.lineage}))
    fields = {
        "外部候选ID": external_id, "旧版编号": fact.legacy_number,
        "旧版collection": fact.key.collection, "旧版record": fact.key.record,
        "旧版version": fact.key.version, "标题": fact.title,
        "身份": "旧版AI派生" if kind == "ai_synthesis" else "本人·待raw写入候选",
        "类型": "综合" if kind == "ai_synthesis" else "用户独立自述候选",
        "接受含义": "仅历史接受动作；不证明事实或用户认知" if kind == "ai_synthesis" else "仅身份已确认；认知仍待确认",
        "正式编号": "未分配", "原文SHA256": sha256(fact.text.encode("utf-8")),
        "原action时间": fact.occurred_at,
    }
    if fact.parent is not None:
        fields["精确父目标"] = _json(asdict(fact.parent)).decode("ascii")
    if kind == "ai_synthesis":
        fields["最终raw谱系"] = _json([
            {"旧来源身份": asdict(e.key), "编号": e.raw_id, "身份": e.raw_identity,
             "引用": e.reference, "SHA256": e.sha256}
            for e in sorted(fact.lineage, key=lambda e: (e.reference, _json(asdict(e.key))))
        ]).decode("ascii")
        fields["旧版谱系上下文SHA256"] = sha256(fact.legacy_context)
    # Every free-text value is a JSON quoted YAML scalar, including embedded LF.
    envelope = "---\n" + "".join(f"{k}: {json.dumps(v, ensure_ascii=True)}\n"
                                      for k, v in fields.items()) + "---\n\n"
    if kind == "ai_synthesis":
        # Fenced source is data, including hostile Markdown/HTML/instructions.
        fence = "`" * max(3, 1 + max((len(s) for s in re.findall(r"`+", fact.text)), default=0))
        body = ("旧版 AI 派生；不能作为其他页面的依据。以下为旧版输出素材。\n\n"
                + fence + "text\n" + fact.text + "\n" + fence + "\n\n"
                + "出处（仅精确来源）：\n" + "".join(f"- [[{ref}]]\n" for ref in refs))
    else:
        # Exact payload is additionally available in private_input; no AI mixed in.
        body = fact.text
    content = (envelope + body).encode("utf-8")
    return Candidate(external_id, kind, relative_path, content, sha256(content),
                     fact.key, fact.legacy_number, refs)


def compile_plan(
    facts: Iterable[LegacyFact], *,
    previous_receipts: Iterable[PrivateReceipt] = (),
    occupied_targets: Iterable[OccupiedTarget] = (),
) -> MappingPlan:
    """Compile only; every blocked item remains privately retained, never deleted.

    Previous receipts are the explicit restart/version ledger. Occupied targets
    are an explicit frozen inventory, NOT a request to read a real destination.
    Missing inventory is fine for offline design but cannot authorize publication.
    """
    prior: dict[str, list[PrivateReceipt]] = defaultdict(list)
    known_previous_versions: dict[tuple[str, str], set[str]] = defaultdict(set)
    for receipt in previous_receipts:
        prior[_id("receipt", receipt.key)].append(receipt)
        known_previous_versions[(receipt.key.collection, receipt.key.record)].add(receipt.key.version)
    occupied: dict[str, list[OccupiedTarget]] = defaultdict(list)
    for target in occupied_targets:
        occupied[target.relative_path].append(target)
    groups: dict[str, dict[str, tuple[LegacyFact, bytes]]] = defaultdict(dict)
    source_versions: dict[LegacyKey, set[str]] = defaultdict(set)
    source_paths: dict[str, set[tuple[LegacyKey, str]]] = defaultdict(set)
    actions_by_key: dict[LegacyKey, set[bytes]] = defaultdict(set)
    known_legacy_keys: set[LegacyKey] = set()
    for fact in facts:
        known_legacy_keys.add(fact.key)
        payload = _json(_value(asdict(fact)))
        groups[_id("receipt", fact.key)][sha256(payload)] = (fact, payload)
        for source in _all_evidence(fact):
            if isinstance(source.content, bytes):
                source_versions[source.key].add(sha256(source.content))
                source_path = source.reference.split("#", 1)[0] if isinstance(source.reference, str) else ""
                source_paths[source_path].add((source.key, sha256(source.content)))
        for action in fact.actions:
            actions_by_key[action.key].add(_json(_value(asdict(action))))
    receipts: list[PrivateReceipt] = []
    for rid, versions in sorted(groups.items()):
        for input_hash, (fact, payload) in sorted(versions.items()):
            errors: set[str] = set()
            status = "blocked"
            candidates: tuple[Candidate, ...] = ()
            if not _key_ok(fact.key):
                errors.add("missing_legacy_identity")
            if not isinstance(fact.legacy_number, str) or not fact.legacy_number:
                errors.add("missing_legacy_number")
            if len(versions) > 1:
                errors.add("conflicting_input_version")
            for previous in prior[rid]:
                if previous.receipt_id != rid:
                    errors.add("invalid_previous_receipt")
                if previous.key != fact.key or previous.input_sha256 != input_hash:
                    errors.add("immutable_input_version_conflict")
                if sha256(previous.private_input) != previous.input_sha256:
                    errors.add("invalid_previous_receipt")
            if not fact.evidence:
                errors.add("missing_input_evidence")
            errors.update(_evidence_errors(fact.evidence))
            # AI supports must terminate at raw/source pages; user/action parents
            # may be private legacy context and never become supporting evidence.
            errors.update(_evidence_errors(fact.lineage, lineage=fact.kind == "ai_insight"))
            if fact.kind == "ai_insight" and any(e.key in known_legacy_keys for e in fact.lineage):
                errors.add("legacy_object_cannot_be_support")
            for source in _all_evidence(fact):
                if len(source_versions[source.key]) > 1:
                    errors.add("conflicting_source_version")
                source_path = source.reference.split("#", 1)[0] if isinstance(source.reference, str) else ""
                if len(source_paths[source_path]) > 1:
                    errors.add("conflicting_source_path")
            if any(len(actions_by_key[a.key]) > 1 for a in fact.actions):
                errors.add("conflicting_action_version")
            # Cross-set collisions matter too: one source version cannot have
            # multiple incompatible byte snapshots or reference assignments.
            errors.update(_evidence_errors(fact.evidence + fact.lineage))
            errors.update(_action_errors(fact))
            if fact.kind not in {"ai_insight", "action", "user_text", "topic"}:
                errors.add("unknown_legacy_kind")
            if fact.kind in {"ai_insight", "user_text"} and not fact.lineage:
                errors.add("missing_lineage")
            if fact.kind in {"ai_insight", "user_text", "action"}:
                if not fact.expected_lineage:
                    errors.add("missing_expected_lineage")
                elif set(fact.expected_lineage) != {e.key for e in fact.lineage}:
                    errors.add("incomplete_lineage_boundary")
                if any(not _key_ok(k) for k in fact.expected_lineage):
                    errors.add("invalid_expected_lineage_identity")
                if fact.key in fact.expected_lineage:
                    errors.add("cyclic_lineage")
            if fact.lineage_issues:
                errors.add("legacy_lineage_anomaly")
            if fact.kind == "ai_insight":
                errors.update(_version_lineage_errors(fact))
                known_versions = known_previous_versions[(fact.key.collection, fact.key.record)]
                if known_versions and fact.key.version not in known_versions and not errors:
                    context = json.loads(fact.legacy_context)
                    if context["previous_version"] is None:
                        errors.add("missing_known_previous_version_lineage")
            if fact.kind in {"action", "user_text"}:
                if not _key_ok(fact.parent):
                    errors.add("missing_parent_identity")
                elif not any(e.key == fact.parent for e in fact.lineage):
                    errors.add("missing_parent_lineage")
            if fact.kind == "user_text" and not _time_ok(fact.occurred_at):
                errors.add("invalid_input_time")
            if fact.kind in {"user_text", "topic"} and fact.actions:
                errors.add("unexpected_actions")
            if fact.kind != "user_text" and fact.confirmation is not None:
                errors.add("unexpected_identity_confirmation")
            if fact.kind == "action" and not fact.actions:
                errors.add("missing_historical_action")
            if fact.kind == "topic" and not fact.topic_complete:
                errors.add("incomplete_topic_snapshot")
            if fact.kind in {"ai_insight", "user_text", "topic"} and not fact.text:
                errors.add("missing_original_text")
            try:
                text_bytes = fact.text.encode("utf-8")
                if not any(e.key == fact.key for e in fact.evidence):
                    errors.add("missing_exact_input_source")
                if fact.kind in {"ai_insight", "user_text", "topic"} and not any(
                        e.key == fact.key and e.content == text_bytes for e in fact.evidence):
                    errors.add("original_text_source_mismatch")
            except (UnicodeEncodeError, AttributeError):
                errors.add("invalid_text_encoding")
            if not errors:
                if fact.kind == "topic":
                    status = "readonly_history"
                elif fact.kind == "action":
                    status = "historical_receipt"
                elif fact.kind == "ai_insight":
                    accepted = any(a.decision in {"interesting", "interesting_after_rethink"}
                                   for a in fact.actions)
                    status = "candidate" if accepted else "readonly_history"
                    if accepted:
                        candidates = (_render(fact, "ai_synthesis"),)
                else:
                    confirmation = fact.confirmation
                    status = "private_retained"
                    if confirmation is not None:
                        errors.update(_evidence_errors((confirmation.evidence,)))
                        if (confirmation.input_key != fact.key or confirmation.parent != fact.parent
                                or confirmation.text_sha256 != sha256(fact.text.encode("utf-8"))):
                            errors.add("identity_confirmation_conflict")
                        if not errors and confirmation.author == "self" and confirmation.expression == "independent":
                            status = "candidate"
                            candidates = (_render(fact, "user_raw"),)
            for candidate in candidates:
                for target in occupied[candidate.relative_path]:
                    if (target.owner_external_id != candidate.external_id
                            or target.sha256 != candidate.sha256):
                        errors.add("occupied_target_conflict")
                for previous in prior[rid]:
                    if previous.candidates and previous.candidates != candidates:
                        errors.add("immutable_candidate_conflict")
            if errors:
                status, candidates = "blocked", ()
            reasons = tuple(sorted(errors)) if errors else ({
                "candidate": ("proposal_only_requires_separate_migration_authorization",),
                "historical_receipt": ("action_is_not_user_expression",),
                "readonly_history": ("retain_legacy_readonly_do_not_regenerate_topic_or_unaccepted_ai",),
                "private_retained": ("author_or_expression_not_confirmed",),
            }[status])
            receipts.append(PrivateReceipt(rid, fact.key, input_hash, status, reasons, payload, candidates))
    return MappingPlan(tuple(receipts))


def _all_evidence(fact: LegacyFact) -> tuple[SourceEvidence, ...]:
    return (fact.evidence + fact.lineage + tuple(a.evidence for a in fact.actions)
            + ((fact.confirmation.evidence,) if fact.confirmation is not None else ()))
