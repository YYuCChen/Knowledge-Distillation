"""Offline Reddit conversion contracts; never fetch, persist, or infer a grant."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re


_ID = re.compile(r"t[13]_[A-Za-z0-9]+\Z")


class RedditSourceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, order=True)
class SourceRef:
    source_id: str
    version: str

    def __post_init__(self):
        if (not isinstance(self.source_id, str) or not _ID.fullmatch(self.source_id)
                or not isinstance(self.version, str) or not self.version):
            raise RedditSourceError("source_ref_invalid")


@dataclass(frozen=True)
class AccessReceipt:
    receipt_id: str
    grant_reference: str
    provider: str
    owner_id: str
    method: str
    purposes: frozenset[str]
    post_ids: frozenset[str]
    source_version: str
    valid_from: datetime
    expires_at: datetime
    retain_until: datetime
    policy_artifact_id: str
    revision: int = 1
    status: str = "active"
    # None means the explicitly granted post tree; empty means no comments.
    comment_ids: frozenset[str] | None = None

    def __post_init__(self):
        try:
            object.__setattr__(self, "purposes", frozenset(self.purposes))
            object.__setattr__(self, "post_ids", frozenset(self.post_ids))
            if self.comment_ids is not None:
                object.__setattr__(self, "comment_ids", frozenset(self.comment_ids))
        except TypeError:
            raise RedditSourceError("access_scope_invalid") from None
        texts = (self.receipt_id, self.grant_reference, self.provider, self.owner_id,
                 self.source_version, self.policy_artifact_id)
        if any(not isinstance(x, str) or not x.strip() for x in texts):
            raise RedditSourceError("access_receipt_incomplete")
        if self.method not in {"authorized_api", "export"} or self.status not in {"active", "revoked"}:
            raise RedditSourceError("access_receipt_invalid")
        if (not self.post_ids or any(not isinstance(x, str) or not re.fullmatch(r"t3_[A-Za-z0-9]+", x) for x in self.post_ids)
                or not self.purposes or not self.purposes <= {"import", "derive"}
                or isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1):
            raise RedditSourceError("access_scope_invalid")
        if self.comment_ids is not None and any(not isinstance(x, str) or not re.fullmatch(r"t1_[A-Za-z0-9]+", x) for x in self.comment_ids):
            raise RedditSourceError("access_scope_invalid")
        for moment in (self.valid_from, self.expires_at, self.retain_until):
            _aware(moment)
        if self.valid_from >= self.expires_at or self.valid_from >= self.retain_until:
            raise RedditSourceError("access_time_invalid")


@dataclass(frozen=True)
class AccessState:
    receipt_id: str
    revision: int
    status: str = "active"
    stopped_refs: frozenset[SourceRef] = frozenset()
    stopped_node_ids: frozenset[str] = frozenset()

    def __post_init__(self):
        if (not isinstance(self.receipt_id, str) or not self.receipt_id
                or type(self.revision) is not int or self.revision < 1
                or self.status not in {"active", "revoked"}):
            raise RedditSourceError("access_state_invalid")
        object.__setattr__(self, "stopped_refs", frozenset(self.stopped_refs))
        if any(not isinstance(x, SourceRef) for x in self.stopped_refs):
            raise RedditSourceError("access_state_invalid")
        object.__setattr__(self, "stopped_node_ids", frozenset(self.stopped_node_ids))
        if any(not isinstance(x, str) or not _ID.fullmatch(x) for x in self.stopped_node_ids):
            raise RedditSourceError("access_state_invalid")


def _aware(moment: datetime) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise RedditSourceError("access_time_naive")


def check_access(receipt: AccessReceipt, state: AccessState, purpose: str,
                 source_ref: SourceRef, now: datetime) -> None:
    _aware(now)
    if (receipt.receipt_id != state.receipt_id or receipt.revision != state.revision):
        raise RedditSourceError("access_revision_changed")
    if (receipt.status != "active" or state.status != "active" or source_ref in state.stopped_refs
            or source_ref.source_id in state.stopped_node_ids):
        raise RedditSourceError("access_stopped")
    if now < receipt.valid_from or now >= receipt.expires_at or now >= receipt.retain_until:
        raise RedditSourceError("access_expired")
    if purpose not in receipt.purposes:
        raise RedditSourceError("access_purpose_denied")
    if source_ref.version != receipt.source_version:
        raise RedditSourceError("access_version_denied")
    permitted = (source_ref.source_id in receipt.post_ids if source_ref.source_id.startswith("t3_")
                 else receipt.comment_ids is None or source_ref.source_id in receipt.comment_ids)
    if not permitted:
        raise RedditSourceError("access_scope_denied")


@dataclass(frozen=True)
class CoverageRequest:
    post_id: str
    requested_sort: str
    max_nodes: int
    max_depth: int
    max_bytes: int
    observed_sort: str | None = None
    upstream_truncated: bool = False
    complete_claim: bool = False

    def __post_init__(self):
        if (not isinstance(self.post_id, str) or not re.fullmatch(r"t3_[A-Za-z0-9]+", self.post_id)
                or not isinstance(self.requested_sort, str) or not self.requested_sort
                or self.observed_sort is not None and not isinstance(self.observed_sort, str)):
            raise RedditSourceError("coverage_scope_invalid")
        if any(isinstance(x, bool) or not isinstance(x, int) or x < 1
               for x in (self.max_nodes, self.max_depth, self.max_bytes)):
            raise RedditSourceError("coverage_budget_invalid")
        if not isinstance(self.upstream_truncated, bool) or not isinstance(self.complete_claim, bool):
            raise RedditSourceError("coverage_claim_invalid")


@dataclass(frozen=True)
class RedditNode:
    source_ref: SourceRef
    post_id: str
    parent_id: str | None
    author: str | None
    author_state: str
    created_raw: object
    created_at: datetime | None
    edited_raw: object
    edit_state: str
    edited_at: datetime | None
    deletion_state: str
    title: str | None
    body: str | None
    score: int | None
    ordinal: int


@dataclass(frozen=True)
class MoreComments:
    parent_id: str | None
    children: tuple[str, ...]
    count: int | None


@dataclass(frozen=True)
class Coverage:
    request: CoverageRequest
    observed_nodes: int
    observed_depth: int
    observed_bytes: int
    more: tuple[MoreComments, ...]
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class NodeRange:
    source_ref: SourceRef
    field: str
    start: int
    end: int


@dataclass(frozen=True, order=True)
class DeletionNotice:
    """Stable node deletion, observed in one version but affecting retained history."""
    source_id: str
    observed_version: str
    reason: str

    def __post_init__(self):
        SourceRef(self.source_id, self.observed_version)
        if self.reason not in {"deleted", "removed"}:
            raise RedditSourceError("deletion_notice_invalid")


@dataclass(frozen=True)
class RedditCapture:
    source_version: str
    receipt_id: str
    receipt_revision: int
    raw_payload: bytes
    payload_sha256: str
    post: RedditNode | None
    comments: tuple[RedditNode, ...]
    coverage: Coverage
    diagnostics: tuple[str, ...]
    structure_usable: bool
    derivation_allowed: bool
    snapshot: str
    node_ranges: tuple[NodeRange, ...]
    deletion_notices: tuple[DeletionNotice, ...]

    @property
    def erasure_required(self) -> bool:
        return bool(self.deletion_notices)

    def evidence(self, *, receipt: AccessReceipt, state: AccessState, now: datetime) -> dict:
        """Every evidence export includes coverage and rechecks the live grant."""
        if self.receipt_id != receipt.receipt_id or self.receipt_revision != receipt.revision:
            raise RedditSourceError("access_revision_changed")
        refs = tuple(n.source_ref for n in ((self.post,) if self.post else ()) + self.comments)
        for ref in refs:
            check_access(receipt, state, "derive", ref, now)
        if self.erasure_required:
            raise RedditSourceError("erasure_required")
        if not self.derivation_allowed:
            raise RedditSourceError("derivation_blocked")
        return {"snapshot": self.snapshot, "node_ranges": self.node_ranges,
                "coverage": self.coverage, "payload_sha256": self.payload_sha256,
                "interpretation_limit": "observed_scope_only_not_whole_thread_consensus"}


def _decode(payload: bytes) -> object:
    if not isinstance(payload, bytes):
        raise RedditSourceError("payload_requires_bytes")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RedditSourceError("json_duplicate_key")
            result[key] = value
        return result
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(RedditSourceError("json_nonfinite")))
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, RedditSourceError):
            raise
        raise RedditSourceError("payload_invalid_json") from None


def parse_authorized_api(payload_bytes: bytes, *, receipt: AccessReceipt, state: AccessState,
                         coverage: CoverageRequest, now: datetime) -> RedditCapture:
    return _parse(payload_bytes, receipt, state, coverage, now, "authorized_api")


def parse_authorized_export(payload_bytes: bytes, *, receipt: AccessReceipt, state: AccessState,
                            coverage: CoverageRequest, now: datetime) -> RedditCapture:
    return _parse(payload_bytes, receipt, state, coverage, now, "export")


def _epoch(value) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    from datetime import UTC
    try:
        return datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _parse(payload, receipt, state, request, now, method):
    root_ref = SourceRef(request.post_id, receipt.source_version)
    check_access(receipt, state, "import", root_ref, now)
    if receipt.method != method:
        raise RedditSourceError("access_method_denied")
    value = _decode(payload)
    diagnostics: set[str] = set()
    if method == "export":
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or not isinstance(value.get("post"), dict) or not isinstance(value.get("comments"), list)
                or not isinstance(value.get("more", []), list)):
            raise RedditSourceError("export_schema_invalid")
        pending = [( {"kind": "t3", "data": value["post"]}, 0)]
        pending += [({"kind": "t1", "data": x}, 1) for x in value["comments"]]
        pending += [({"kind": "more", "data": x}, 1) for x in value.get("more", [])]
    elif isinstance(value, list):
        pending = [(x, 0) for x in value]
    else:
        pending = [(value, 0)]
    nodes: dict[str, RedditNode] = {}
    raw_nodes: dict[str, dict] = {}
    conflicts: set[str] = set()
    deletion_notices: set[DeletionNotice] = set()
    more = []
    ordinal = 0
    # Reverse stack preserves upstream order; parent depth is verified separately.
    pending.reverse()
    while pending:
        item, depth = pending.pop()
        if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
            diagnostics.add("unknown_structure")
            continue
        kind, data = item.get("kind"), item["data"]
        if kind == "Listing":
            children = data.get("children")
            if not isinstance(children, list):
                diagnostics.add("unknown_structure")
            else:
                pending.extend((child, depth) for child in reversed(children))
            if data.get("after") or data.get("before"):
                more.append(MoreComments(None, (), None))
            continue
        if kind == "more":
            children = data.get("children", [])
            if (not isinstance(children, list) or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9]+", x) for x in children)):
                diagnostics.add("unknown_structure")
                children = []
            more.append(MoreComments(data.get("parent_id"), tuple("t1_" + x for x in children),
                                     data.get("count") if type(data.get("count")) is int else None))
            continue
        if kind not in {"t1", "t3"}:
            diagnostics.add("unknown_structure")
            continue
        name = data.get("name")
        if not isinstance(name, str) or not _ID.fullmatch(name) or not name.startswith(kind + "_"):
            diagnostics.add("node_identity_invalid")
            continue
        ref = SourceRef(name, receipt.source_version)
        check_access(receipt, state, "import", ref, now)
        parent = data.get("parent_id") if kind == "t1" else None
        if kind == "t1" and (not isinstance(parent, str) or not _ID.fullmatch(parent)):
            diagnostics.add("parent_identity_invalid")
            parent = None
        if kind == "t3" and name != request.post_id:
            diagnostics.add("post_scope_mismatch")
        if kind == "t1" and data.get("link_id") is not None and data.get("link_id") != request.post_id:
            diagnostics.add("post_scope_mismatch")
        title = data.get("title") if kind == "t3" else None
        body = data.get("selftext") if kind == "t3" else data.get("body")
        if (not isinstance(body, str) or (kind == "t3" and not isinstance(title, str))):
            diagnostics.add("node_content_unknown")
        author = data.get("author")
        author_state = "deleted" if author == "[deleted]" else "known" if isinstance(author, str) and author else "unknown"
        edited = data.get("edited")
        edit_state = "unedited" if edited is False else "edited" if _epoch(edited) is not None else "unknown"
        explicit_deleted = data.get("deleted")
        deletion = ("deleted" if explicit_deleted is True or body == "[deleted]" else
                    "removed" if body == "[removed]" or data.get("removed") is True or data.get("removed_by_category") else
                    "present" if isinstance(body, str) and explicit_deleted is False else "unknown")
        if deletion in {"deleted", "removed"}:
            deletion_notices.add(DeletionNotice(name, receipt.source_version, deletion))
        node = RedditNode(ref, request.post_id, parent, author if isinstance(author, str) else None,
                          author_state, data.get("created_utc"), _epoch(data.get("created_utc")), edited,
                          edit_state, _epoch(edited), deletion, title if isinstance(title, str) else None,
                          body if isinstance(body, str) else None,
                          data.get("score") if type(data.get("score")) is int else None, ordinal)
        ordinal += 1
        # Replies are separately traversed; identical identity/content is deduplicated.
        comparable = {k: v for k, v in data.items() if k != "replies"}
        if name in raw_nodes:
            if raw_nodes[name] == comparable:
                diagnostics.add("duplicate_identical")
            else:
                diagnostics.add("duplicate_conflict")
                conflicts.add(name)
        else:
            raw_nodes[name], nodes[name] = comparable, node
        replies = data.get("replies")
        if replies not in (None, ""):
            pending.append((replies, depth + 1))
    for name in conflicts:
        nodes.pop(name, None)  # Never choose a winner for a conflicted identity.
    for placeholder in more:
        if placeholder.parent_id is not None and (not isinstance(placeholder.parent_id, str)
                                                  or placeholder.parent_id not in nodes):
            diagnostics.add("more_parent_missing")
    post = nodes.get(request.post_id)
    if post is None:
        diagnostics.add("post_missing")
    comments = tuple(n for n in nodes.values() if n.source_ref.source_id.startswith("t1_"))
    depths = {request.post_id: 0} if post else {}
    for node in comments:
        chain, seen, current = [], set(), node.source_ref.source_id
        while current not in depths:
            if current in seen:
                diagnostics.add("parent_cycle")
                break
            seen.add(current)
            parent_node = nodes.get(current)
            if parent_node is None or parent_node.parent_id not in nodes:
                diagnostics.add("parent_missing")
                break
            chain.append(current)
            current = parent_node.parent_id
        else:
            length = depths[current]
            for member in reversed(chain):
                length += 1
                depths[member] = length
    max_depth = max(depths.values(), default=0)
    if len(raw_nodes) > request.max_nodes or len(payload) > request.max_bytes or max_depth > request.max_depth:
        diagnostics.add("budget_exceeded")
    structural_errors = diagnostics - {"duplicate_identical", "budget_exceeded"}
    usable = not structural_errors
    coverage_reasons = set(diagnostics)
    if more:
        coverage_reasons.add("more_pending")
    if request.upstream_truncated:
        coverage_reasons.add("upstream_truncated")
    if not request.complete_claim:
        coverage_reasons.add("completeness_unverified")
    if request.observed_sort is None:
        coverage_reasons.add("sort_unverified")
    elif request.observed_sort != request.requested_sort:
        coverage_reasons.add("sort_differs")
    known_partial = bool(more) or request.upstream_truncated
    coverage_status = ("partial" if known_partial else "complete_within_declared_scope"
                       if request.complete_claim and usable and "budget_exceeded" not in diagnostics
                       else "unknown")
    cov = Coverage(request, len(raw_nodes), max_depth, len(payload), tuple(more), coverage_status,
                   tuple(sorted(coverage_reasons)))
    pieces, ranges, position = [], [], 0
    for node in ((post,) if post else ()) + comments:
        header = f"[{node.source_ref.source_id}; parent={node.parent_id or '-'}]\n"
        pieces.append(header)
        position += len(header)
        for field in ("title", "body"):
            text = getattr(node, field)
            if text is not None:
                ranges.append(NodeRange(node.source_ref, field, position, position + len(text)))
                pieces.append(text + "\n")
                position += len(text) + 1
    refs = tuple(n.source_ref for n in ((post,) if post else ()) + comments)
    for ref in (root_ref,) + refs:
        check_access(receipt, state, "import", ref, now)
    derivation = (usable and "budget_exceeded" not in diagnostics and "derive" in receipt.purposes
                  and not deletion_notices)
    return RedditCapture(receipt.source_version, receipt.receipt_id, receipt.revision, payload,
                         hashlib.sha256(payload).hexdigest(), post, comments, cov,
                         tuple(sorted(diagnostics)), usable, derivation, "".join(pieces), tuple(ranges),
                         tuple(sorted(deletion_notices)))
