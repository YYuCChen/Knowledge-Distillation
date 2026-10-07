"""Only new 0700 roots and invented bytes; never resolve application defaults."""
from dataclasses import replace
import json
import os
import stat
import subprocess
import sys

import pytest

from knowledge_distiller.v1 import reddit_erasure as erasure
from knowledge_distiller.v1.reddit_erasure import KINDS, RedditErasureError, SyntheticRoot
from knowledge_distiller.v1.reddit_source import DeletionNotice, SourceRef


A = SourceRef("t1_SYNA", "synthetic_v1")
B = SourceRef("t1_SYNB", "synthetic_v1")
A2 = SourceRef("t1_SYNA", "synthetic_v2")


@pytest.fixture
def root(tmp_path):
    return SyntheticRoot.create(tmp_path.resolve() / "private_synthetic_reddit")


def add(root, identifier, refs=frozenset({A}), parents=(), kind="original", **changes):
    content = changes.pop("content", b"SYNTHETIC_REDDIT_BODY_SENTINEL")
    path = root.path / "copies" / f"{identifier}.bin"
    path.write_bytes(content)
    args = dict(artifact_id=identifier, relative_path=f"copies/{identifier}.bin", kind=kind,
                source_refs=refs, parent_artifact_ids=parents, owner="machine", synthetic=True,
                managed=True, contains_user_content=False, lineage_complete=True,
                deletion_bound_refs=refs)
    args.update(changes)
    root.register_artifact(**args)
    return path


def plan(root, targets=frozenset({A}), **changes):
    root.stop_sources(operation_id="op_SYN", source_refs=targets, reason="authorization_revoked")
    args = dict(operation_id="op_SYN", inventory_complete=True, covered_kinds=KINDS)
    args.update(changes)
    return root.plan_erasure(**args)


def test_seven_copy_kinds_exact_versions_and_no_body_receipt(root):
    original = add(root, "source")
    paths = [original]
    for kind in sorted(KINDS - {"original"}):
        paths.append(add(root, kind, parents=("source",), kind=kind))
    other = add(root, "other", refs=frozenset({B}))
    future = add(root, "future", refs=frozenset({A2}))
    p = plan(root)
    result = root.execute_erasure(p)
    assert result.state == "cleared_registered_synthetic_scope"
    assert result.product_complete is False
    assert all(not x.exists() for x in paths)
    assert other.read_bytes() == future.read_bytes() == b"SYNTHETIC_REDDIT_BODY_SENTINEL"
    assert root.stopped_refs() == frozenset({A})
    journal = (root.path / ".reddit-control.json").read_text()
    assert "SYNTHETIC_REDDIT_BODY_SENTINEL" not in journal
    assert all(word not in journal for word in ("https://", '"title"', '"author"', '"body"'))
    assert stat.S_IMODE(root.path.stat().st_mode) == 0o700
    assert stat.S_IMODE((root.path / ".reddit-control.json").stat().st_mode) == 0o600
    repeat = SyntheticRoot(root.path).resume_erasure("op_SYN")
    assert repeat.state == result.state and repeat.cleared_artifact_ids == result.cleared_artifact_ids


def test_mixed_machine_derived_copy_can_clear_but_independent_user_words_stay(root):
    add(root, "a")
    b = add(root, "b", refs=frozenset({B}))
    mixed = add(root, "mixed", refs=frozenset({A, B}), parents=("a", "b"), kind="ai_derived")
    user = add(root, "user", refs=frozenset({B}), owner="user", contains_user_content=True,
               content=b"SYNTHETIC_INDEPENDENT_USER_WORDS")
    result = root.execute_erasure(plan(root))
    assert result.state == "cleared_registered_synthetic_scope"
    assert not mixed.exists()
    assert b.exists() and user.read_bytes() == b"SYNTHETIC_INDEPENDENT_USER_WORDS"


def test_mixed_user_and_hand_edit_are_manual_not_success(root):
    add(root, "a")
    add(root, "b", refs=frozenset({B}))
    mixed = add(root, "mixed", refs=frozenset({A, B}), parents=("a", "b"), kind="ai_derived",
                contains_user_content=True, content=b"SYNTHETIC_USER_AND_REDDIT_QUOTE")
    edited = add(root, "edited", parents=("a",), kind="staging")
    edited.write_bytes(b"SYNTHETIC_HAND_EDITED_NOTE")
    result = root.execute_erasure(plan(root))
    assert result.state == "partial_manual" and not result.product_complete
    assert set(result.manual_artifact_ids) == {"mixed", "edited"}
    assert mixed.read_bytes() == b"SYNTHETIC_USER_AND_REDDIT_QUOTE"
    assert edited.read_bytes() == b"SYNTHETIC_HAND_EDITED_NOTE"
    assert set(result.invalidated_refs) == {"a", "mixed", "edited"}


def test_parent_union_mismatch_cannot_be_hidden_by_lineage_true(root):
    add(root, "a")
    add(root, "b", refs=frozenset({B}))
    # Declares B only, but A is a registered parent: computed closure selects it.
    child = add(root, "dishonest", refs=frozenset({B}), parents=("a", "b"), kind="ai_derived")
    descendant = add(root, "descendant", refs=frozenset({A, B}), parents=("dishonest",), kind="backup")
    p = plan(root)
    reasons = {x.artifact_id: x.reasons for x in p.items}
    assert "parent_refs_mismatch" in reasons["dishonest"]
    assert "parent_lineage_invalid" in reasons["descendant"]
    result = root.execute_erasure(p)
    assert result.state == "partial_manual"
    assert child.exists() and descendant.exists() and "lineage_gap" in result.coverage_gaps


@pytest.mark.parametrize("parents,lineage", [(('missing',), True), (('cycle',), True), ((), False)])
def test_unknown_or_cyclic_lineage_cannot_claim_cleared(root, parents, lineage):
    path = add(root, "cycle", parents=parents, kind="ai_derived", lineage_complete=lineage)
    result = root.execute_erasure(plan(root))
    assert result.state == "partial_manual" and path.exists()
    assert "lineage_gap" in result.coverage_gaps


def test_self_expression_is_protected_by_default(root):
    path = root.path / "copies" / "self.bin"
    path.write_bytes(b"SYNTHETIC_SELF_EXPRESSION")
    root.register_artifact(artifact_id="self", relative_path="copies/self.bin", kind="original", source_refs=frozenset({A}))
    result = root.execute_erasure(plan(root))
    assert result.state == "partial_manual" and path.read_bytes() == b"SYNTHETIC_SELF_EXPRESSION"


@pytest.mark.parametrize("change,gap", [(dict(inventory_complete=False), "inventory_not_complete"),
                                      (dict(covered_kinds=frozenset({"original"})), "copy_kinds_not_covered")])
def test_incomplete_inventory_not_complete(root, change, gap):
    add(root, "a")
    result = root.execute_erasure(plan(root, **change))
    assert result.state == "partial_manual" and gap in result.coverage_gaps


def test_unregistered_copy_is_never_silently_deleted(root):
    add(root, "a")
    unknown = root.path / "copies" / "unknown.bin"
    unknown.write_bytes(b"SYNTHETIC_UNKNOWN")
    result = root.execute_erasure(plan(root))
    assert result.state == "partial_manual" and "unregistered_copy" in result.coverage_gaps
    assert unknown.read_bytes() == b"SYNTHETIC_UNKNOWN"


@pytest.mark.parametrize("unsafe", ["../outside.bin", "/outside.bin", "copies/../a.bin", "copies\\a.bin", "copies/a\x00.bin"])
def test_path_boundary_rejected(root, unsafe):
    with pytest.raises(RedditErasureError, match="artifact_path_invalid"):
        root.register_artifact(artifact_id="a", relative_path=unsafe, kind="original", source_refs=frozenset({A}))


@pytest.mark.parametrize("replacement", ["symlink", "hardlink", "inode", "same_size_edit", "directory"])
def test_registered_path_replacement_cannot_delete_other_object(root, tmp_path, replacement):
    path = add(root, "a", content=b"AAAA")
    p = plan(root)
    outside = tmp_path / "synthetic_outside_sentinel"
    outside.write_bytes(b"BBBB")
    if replacement == "same_size_edit":
        path.write_bytes(b"BBBB")
    elif replacement == "inode":
        path.rename(root.path / "synthetic_old_inode")
        path.write_bytes(b"AAAA")
    else:
        path.unlink()
        if replacement == "symlink":
            path.symlink_to(outside)
        elif replacement == "hardlink":
            os.link(outside, path)
        elif replacement == "directory":
            path.mkdir()
    result = root.execute_erasure(p)
    assert result.state == "partial_manual"
    assert path.exists() and outside.read_bytes() == b"BBBB"


@pytest.mark.parametrize("boundary", ["prepared", "before_unlink", "unlinked", "directory_synced", "receipt_synced"])
def test_crash_at_each_removal_boundary_recovers_idempotently(root, monkeypatch, boundary):
    path = add(root, "a")
    p = plan(root)
    def crash(name, identifier):
        if name == boundary:
            raise RuntimeError("synthetic interruption")
    monkeypatch.setattr(erasure, "_event", crash)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        root.execute_erasure(p)
    assert root.stopped_refs() == frozenset({A})
    monkeypatch.setattr(erasure, "_event", lambda *_: None)
    result = SyntheticRoot(root.path).resume_erasure("op_SYN")
    assert result.state == "cleared_registered_synthetic_scope" and not path.exists()
    assert SyntheticRoot(root.path).resume_erasure("op_SYN").state == result.state


def test_new_inode_after_crash_is_not_erased(root, monkeypatch):
    path = add(root, "a")
    p = plan(root)
    def crash(name, identifier):
        if name == "unlinked":
            raise RuntimeError("synthetic interruption")
    monkeypatch.setattr(erasure, "_event", crash)
    with pytest.raises(RuntimeError):
        root.execute_erasure(p)
    path.write_bytes(b"SYNTHETIC_NEW_FILE")
    monkeypatch.setattr(erasure, "_event", lambda *_: None)
    result = SyntheticRoot(root.path).resume_erasure("op_SYN")
    assert result.state == "partial_manual" and path.read_bytes() == b"SYNTHETIC_NEW_FILE"


def test_edit_at_final_boundary_is_preserved(root, monkeypatch):
    path = add(root, "a", content=b"AAAA")
    p = plan(root)
    monkeypatch.setattr(erasure, "_event", lambda name, _: path.write_bytes(b"BBBB") if name == "before_unlink" else None)
    result = root.execute_erasure(p)
    assert result.state == "partial_manual" and path.read_bytes() == b"BBBB"


def test_manual_checkpoint_is_not_automatically_erased_after_restoring_bytes(root):
    path = add(root, "a", content=b"AAAA")
    p = plan(root)
    path.write_bytes(b"BBBB")
    assert root.execute_erasure(p).state == "partial_manual"
    path.write_bytes(b"AAAA")
    assert SyntheticRoot(root.path).resume_erasure("op_SYN").state == "partial_manual"
    assert path.read_bytes() == b"AAAA"


def test_cancel_after_prepared_keeps_stop_and_has_resumable_checkpoint(root, monkeypatch):
    path = add(root, "a")
    p = plan(root)
    cancelled = [False]
    def cancel_at_boundary(name, _):
        if name == "prepared":
            cancelled[0] = True
    monkeypatch.setattr(erasure, "_event", cancel_at_boundary)
    result = root.execute_erasure(p, cancel_check=lambda: cancelled[0])
    assert result.state == "cancelled" and path.exists() and root.stopped_refs() == frozenset({A})
    monkeypatch.setattr(erasure, "_event", lambda *_: None)
    assert SyntheticRoot(root.path).resume_erasure("op_SYN").state == "cleared_registered_synthetic_scope"


def test_missing_without_prepared_is_manual(root):
    path = add(root, "a")
    p = plan(root)
    path.unlink()
    result = root.execute_erasure(p)
    assert result.state == "partial_manual" and result.manual_artifact_ids == ("a",)


def test_root_permission_change_stops_execution(root):
    add(root, "a")
    p = plan(root)
    root.path.chmod(0o755)
    with pytest.raises(RedditErasureError, match="root_changed"):
        root.execute_erasure(p)


def test_cancel_does_not_resume_access_and_second_run_can_clear(root):
    path = add(root, "a")
    p = plan(root)
    result = root.execute_erasure(p, cancel_check=lambda: True)
    assert result.state == "cancelled" and path.exists() and root.stopped_refs() == frozenset({A})
    with pytest.raises(RedditErasureError, match="inventory_frozen"):
        add(root, "new")
    # The unregistered synthetic file introduced above is honestly a gap.
    result = SyntheticRoot(root.path).resume_erasure("op_SYN")
    assert not path.exists() and result.state == "partial_manual"


def test_stop_precedes_plan_and_immediately_invalidates_parent_descendants(root):
    add(root, "a")
    add(root, "child", refs=frozenset({B}), parents=("a",), kind="ai_derived")
    root.stop_sources(operation_id="op_SYN", source_refs=frozenset({A}), reason="authorization_expired")
    journal = json.loads((root.path / ".reddit-control.json").read_bytes())
    assert journal["operations"]["op_SYN"]["state"] == "stopped"
    assert journal["operations"]["op_SYN"]["invalidated"] == ["a", "child"]
    with pytest.raises(RedditErasureError, match="operation_conflict"):
        root.stop_sources(operation_id="op_SYN", source_refs=frozenset({B}), reason="authorization_expired")


def test_plan_tamper_and_real_mode_rejected(root):
    add(root, "a")
    p = plan(root)
    with pytest.raises(RedditErasureError, match="plan_changed"):
        root.execute_erasure(replace(p, items=()))
    with pytest.raises(RedditErasureError, match="synthetic_only_required"):
        root.execute_erasure(p, synthetic_only=False)


def test_root_and_copies_identity_substitution_rejected(root):
    add(root, "a")
    p = plan(root)
    copies = root.path / "copies"
    copies.rename(root.path / "old_synthetic_copies")
    copies.mkdir(mode=0o700)
    replacement = copies / "a.bin"
    replacement.write_bytes(b"SYNTHETIC_REPLACEMENT")
    result = root.execute_erasure(p)
    assert result.state == "partial_manual" and replacement.exists()
    root.path.rename(root.path.with_name("old_synthetic_root"))
    root.path.mkdir(mode=0o700)
    with pytest.raises(RedditErasureError, match="root_changed"):
        root.resume_erasure("op_SYN")


def test_existing_directory_and_symlink_root_cannot_be_adopted(tmp_path):
    existing = tmp_path.resolve() / "existing_synthetic"
    existing.mkdir(mode=0o700)
    with pytest.raises(RedditErasureError, match="root_must_be_new"):
        SyntheticRoot.create(existing)
    linked = tmp_path.resolve() / "linked_synthetic"
    linked.symlink_to(existing, target_is_directory=True)
    with pytest.raises(RedditErasureError, match="root_symlink"):
        SyntheticRoot(linked)


@pytest.mark.parametrize("source_id", ["t1_SYNA", "t3_SYNPOST"])
def test_deletion_notice_expands_proven_node_history_and_preserves_independent_user(root, source_id):
    old_ref = SourceRef(source_id, "synthetic_v1")
    current_ref = SourceRef(source_id, "synthetic_v2")
    old = add(root, "old", refs=frozenset({old_ref}))
    current = add(root, "current", refs=frozenset({current_ref}))
    derived = add(root, "derived", refs=frozenset({old_ref, current_ref}),
                  parents=("old", "current"), kind="ai_derived")
    user = add(root, "independent_user", refs=frozenset({current_ref}), deletion_bound_refs=frozenset(),
               owner="user", contains_user_content=True, content=b"SYNTHETIC_INDEPENDENT_USER_WORDS")
    unrelated = add(root, "other_comment", refs=frozenset({B}))
    # Observation is deliberately a version absent from the retained inventory.
    notices = (DeletionNotice(source_id, "observation_v99", "deleted"),)
    stopped = root.stop_deletion_notices(operation_id="notice_SYN", notices=notices,
                                          inventory_complete=True, covered_kinds=KINDS)
    assert stopped.source_refs == frozenset({old_ref, current_ref})
    assert SourceRef(source_id, "observation_v99") not in stopped.source_refs
    assert root.stopped_node_ids() == frozenset({source_id})
    assert set(stopped.invalidated_refs) == {"old", "current", "derived", "independent_user"}
    assert all(path.exists() for path in (old, current, derived))  # stop never purges.
    p = root.plan_erasure(operation_id="notice_SYN", inventory_complete=True, covered_kinds=KINDS)
    result = root.execute_erasure(p)
    assert result.state == "cleared_registered_synthetic_scope" and result.product_complete is False
    assert not any(path.exists() for path in (old, current, derived))
    assert user.read_bytes() == b"SYNTHETIC_INDEPENDENT_USER_WORDS" and unrelated.exists()


@pytest.mark.parametrize("unknown", ["coverage", "constraint", "parent_bound_mismatch"])
def test_deletion_history_cannot_complete_when_scope_or_constraint_is_unproven(root, unknown):
    add(root, "old")
    if unknown == "constraint":
        protected = add(root, "unknown_version", refs=frozenset({A2}), deletion_bound_refs=None)
    elif unknown == "parent_bound_mismatch":
        add(root, "current", refs=frozenset({A2}))
        protected = add(root, "mismatch", refs=frozenset({A, A2}), parents=("old", "current"),
                        kind="backup", deletion_bound_refs=frozenset({A}))
    else:
        protected = None
    stopped = root.stop_deletion_notices(operation_id="notice_SYN",
                                          notices=(DeletionNotice(A.source_id, A2.version, "removed"),),
                                          inventory_complete=unknown != "coverage", covered_kinds=KINDS)
    assert stopped.coverage_gaps
    # A later optimistic inventory flag cannot erase the original unresolved gap.
    p = root.plan_erasure(operation_id="notice_SYN", inventory_complete=True, covered_kinds=KINDS)
    result = SyntheticRoot(root.path).execute_erasure(p)
    assert result.state == "partial_manual" and result.coverage_gaps
    if protected:
        assert protected.exists()


def test_authorization_revocation_keeps_precise_version_and_does_not_stop_future_node(root):
    old = add(root, "old")
    future = add(root, "future", refs=frozenset({A2}))
    p = plan(root)
    result = root.execute_erasure(p)
    assert result.state == "cleared_registered_synthetic_scope" and not old.exists() and future.exists()
    assert root.stopped_refs() == frozenset({A}) and not root.stopped_node_ids()
    with pytest.raises(RedditErasureError, match="stop_request_invalid"):
        root.stop_sources(operation_id="narrow_node_delete", source_refs=frozenset({A}), reason="node_deleted")


def test_capture_notice_caller_stops_history_before_planning_without_auto_purge(root):
    from datetime import UTC, datetime, timedelta
    from knowledge_distiller.v1.reddit_source import (
        AccessReceipt, AccessState, CoverageRequest, RedditSourceError, parse_authorized_export,
    )
    old = add(root, "old")
    current = add(root, "current", refs=frozenset({A2}))
    now = datetime(2099, 1, 1, tzinfo=UTC)
    grant = AccessReceipt("receipt_SYN", "grant_SYN", "synthetic_provider", "owner_SYN", "export",
                          frozenset({"import", "derive"}), frozenset({"t3_SYN1"}), A2.version,
                          now - timedelta(days=1), now + timedelta(days=1), now + timedelta(days=2), "policy_SYN")
    payload = json.dumps({"schema_version": 1,
                          "post": {"name": "t3_SYN1", "title": "Synthetic", "selftext": "Synthetic post", "deleted": False},
                          "comments": [{"name": A.source_id, "parent_id": "t3_SYN1", "deleted": True,
                                        "body": "SYNTHETIC_RETAINED_DELETED_BODY"}]}).encode()
    request = CoverageRequest("t3_SYN1", "new", 10, 10, 10000)
    capture = parse_authorized_export(payload, receipt=grant, state=AccessState("receipt_SYN", 1),
                                      coverage=request, now=now)
    assert capture.erasure_required and capture.raw_payload == payload
    stopped = root.stop_deletion_notices(operation_id="notice_SYN", notices=capture.deletion_notices,
                                          inventory_complete=True, covered_kinds=KINDS)
    assert stopped.source_refs == frozenset({A, A2}) and old.exists() and current.exists()
    with pytest.raises(RedditSourceError, match="erasure_required"):
        capture.evidence(receipt=grant, state=AccessState("receipt_SYN", 1), now=now)
    # Bridge the durable stop into the caller's live gate; parser must now stop.
    state = AccessState("receipt_SYN", 1, stopped_refs=root.stopped_refs(),
                        stopped_node_ids=root.stopped_node_ids())
    with pytest.raises(RedditSourceError, match="access_stopped"):
        parse_authorized_export(payload, receipt=grant, state=state, coverage=request, now=now)


def test_fifo_lock_rejected_without_hanging_or_touching_copy(root):
    path = add(root, "a")
    live = root.path / ".reddit-lock"
    live.rename(root.path / ".synthetic-retained-lock")
    os.mkfifo(live, mode=0o600)
    # A bounded child catches regressions where open(O_RDONLY) blocks before fstat.
    script = (
        "import sys; from pathlib import Path; "
        "from knowledge_distiller.v1.reddit_erasure import SyntheticRoot, RedditErasureError\n"
        "try: SyntheticRoot(Path(sys.argv[1]))\n"
        "except RedditErasureError as error: print(error.code); sys.exit(0)\n"
        "sys.exit(2)\n"
    )
    completed = subprocess.run([sys.executable, "-c", script, str(root.path)],
                               capture_output=True, text=True, timeout=5, check=False)
    assert completed.returncode == 0 and "lock_changed_or_unsafe" in completed.stdout
    assert path.read_bytes() == b"SYNTHETIC_REDDIT_BODY_SENTINEL" and stat.S_ISFIFO(live.lstat().st_mode)


@pytest.mark.parametrize("boundary", ["lock_before_flock", "lock_after_flock", "before_unlink"])
def test_named_lock_inode_replacement_at_flock_or_unlink_boundary_rejected(root, monkeypatch, boundary):
    path = add(root, "a")
    p = plan(root)
    replaced = [False]
    def swap(name, _):
        if name == boundary and not replaced[0]:
            replaced[0] = True
            lock_path = root.path / ".reddit-lock"
            lock_path.rename(root.path / ".synthetic-retained-live-lock")
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
    monkeypatch.setattr(erasure, "_event", swap)
    with pytest.raises(RedditErasureError, match="lock_changed_or_unsafe"):
        root.execute_erasure(p)
    assert replaced[0] and path.read_bytes() == b"SYNTHETIC_REDDIT_BODY_SENTINEL"
    assert (root.path / ".synthetic-retained-live-lock").exists() and (root.path / ".reddit-lock").exists()
    monkeypatch.setattr(erasure, "_event", lambda *_: None)
    with pytest.raises(RedditErasureError, match="lock_identity_changed"):
        SyntheticRoot(root.path)  # Never adopt a replacement as another valid lock.


@pytest.mark.parametrize("boundary,change", [("journal_before_read", "mode"),
                                             ("journal_after_read", "mode"),
                                             ("journal_before_read", "inode"),
                                             ("journal_after_read", "inode")])
def test_control_journal_fd_mode_and_named_inode_verified_before_after_read(root, monkeypatch, boundary, change):
    path = add(root, "a")
    p = plan(root)
    changed = [False]
    def swap(name, _):
        if name != boundary or changed[0]:
            return
        changed[0] = True
        journal = root.path / ".reddit-control.json"
        if change == "mode":
            journal.chmod(0o644)
        else:
            original_bytes = journal.read_bytes()
            journal.rename(root.path / ".synthetic-retained-journal")
            journal.write_bytes(original_bytes)
            journal.chmod(0o600)
    monkeypatch.setattr(erasure, "_event", swap)
    with pytest.raises(RedditErasureError, match="file_unsafe"):
        root.execute_erasure(p)
    assert changed[0] and path.read_bytes() == b"SYNTHETIC_REDDIT_BODY_SENTINEL"
