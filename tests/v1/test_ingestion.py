"""Synthetic candidate tests; never use AppPaths/system defaults or credentials."""
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from knowledge_distiller.v1 import raw
from knowledge_distiller.v1.captures import record_capture
from knowledge_distiller.v1.database import connect, INGESTION_CONTRACT
from knowledge_distiller.v1.ingestion import Ingestion, IngestionError, verify_record, read_regular
from knowledge_distiller.v1.store import Store
from .test_raw import material as _material, PNG


def material(store, *args, **kwargs):
    """Explicit new-contract synthetic owner; production defaults stay legacy."""
    mid = _material(store, *args, **kwargs)
    item = store.create_item(f"synthetic://raw/{mid}", ingestion_contract=INGESTION_CONTRACT,
                             source_binding_sha256=hashlib.sha256(f"input:{mid}".encode()).hexdigest(),
                             relation_binding_sha256=hashlib.sha256(b"synthetic frozen no-selection plan").hexdigest())
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET material_id=? WHERE item_id=?", (mid, item))
    return mid


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "source.sqlite3")
    store.initialize()  # explicit disposable fixture only
    vault = tmp_path / "vault"
    vault.mkdir()
    store.set_setting("vault_path", str(vault))
    candidate = Ingestion(store)
    candidate.initialize()
    return store, vault, candidate


def test_exact_receipt_no_sql_knowledge_or_wiki_task(world, monkeypatch):
    store, vault, candidate = world
    mid = material(store, "x", "完整定义与原文。", media=[("image-1", PNG)],
                   metadata={"native_content_version": "v7"})
    monkeypatch.setattr(store, "establish_knowledge", lambda *a: pytest.fail("old knowledge called"))
    receipt = candidate.material(mid, vault)
    assert receipt.subject_kind == "material" and receipt.identity == "第三方"
    assert receipt.format_version == 1 and len(receipt.source_version) == 64
    assert (vault / receipt.relative_path).read_bytes() == candidate.ledger.record(receipt.raw_id)["content"].encode()
    assert receipt.attachments and (vault / receipt.attachments[0][0]).read_bytes() == PNG
    with connect(store.path) as db:
        for table in ("knowledge_results", "collection_results", "wiki_tasks"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert candidate.material(mid, vault) == receipt
    assert [e[0] for e in candidate.events(f"material:{mid}")].count("raw_verified") == 1


def test_main_process_storage_does_not_initialize_or_backfill_history(world, tmp_path):
    store, vault, _ = world
    material(store, "x", "历史原件")
    candidate = Ingestion(store)
    candidate.initialize()
    assert candidate.events("material:1") == []
    assert not (vault / "raw").exists()
    assert candidate.path == store.path and not (tmp_path / "candidate.sqlite3").exists()
    unopened = Ingestion(Store(tmp_path / "not-initialized.sqlite3"))
    with pytest.raises(IngestionError, match="candidate_schema_rebuild_required"):
        unopened.initialize()


def test_unavailable_retains_bytes_and_retries_same_raw(world, tmp_path):
    store, _, candidate = world
    mid = material(store, "x", "必须保留的原件", media=[("image-1", PNG)])
    unavailable = tmp_path / "unmounted"
    with pytest.raises(IngestionError, match="vault_unavailable"):
        candidate.material(mid, unavailable)
    assert candidate.ledger.current("material", mid) is None  # No lockable Vault, no assignment.
    with connect(store.path) as db:
        assert db.execute("SELECT content FROM source_media WHERE material_id=?", (mid,)).fetchone()[0] == PNG
        assert db.execute("SELECT snapshot FROM source_facts WHERE material_id=?", (mid,)).fetchone()[0] == "必须保留的原件"
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'ingest_retained%'").fetchone()[0] == 0
    assert not any(e[0] == "raw_verified" for e in candidate.events(f"material:{mid}"))
    unavailable.mkdir()
    receipt = candidate.material(mid, unavailable)
    assert receipt.raw_id == candidate.ledger.current("material", mid)["raw_id"]
    assert [e[0] for e in candidate.events(f"material:{mid}")] == ["raw_verified"]


@pytest.mark.parametrize("damage", ["raw", "attachment", "missing", "symlink"])
def test_written_at_never_bypasses_readback(world, tmp_path, damage):
    store, vault, candidate = world
    mid = material(store, "x", "原件", media=[("image-1", PNG)])
    receipt = candidate.material(mid, vault)
    record = candidate.ledger.record(receipt.raw_id)
    assert record["written_at"]
    target = vault / (receipt.attachments[0][0] if damage == "attachment" else receipt.relative_path)
    if damage == "missing":
        target.unlink()
    elif damage == "symlink":
        other = tmp_path / "other"
        other.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(other)
    else:
        target.write_bytes(b"changed bytes")
    with pytest.raises((IngestionError, raw.RawError)):
        verify_record(candidate.ledger, record, vault, source_version="actual-source-version")


def test_receipt_checks_identity_format_id_and_subject(world):
    store, vault, candidate = world
    mid = material(store, "x", "原件")
    record = dict(candidate.ledger.ensure_material(mid))
    for old, new in [("格式版本: 1", "格式版本: 2"),
                     ("身份: 第三方", "身份: 本人"),
                     (record["raw_id"], "R-20000101-9999")]:
        bad = {**record, "content": record["content"].replace(old, new)}
        bad["content_sha256"] = hashlib.sha256(bad["content"].encode()).hexdigest()
        target = vault / bad["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bad["content"], encoding="utf-8")
        with pytest.raises(IngestionError, match="raw_envelope_mismatch"):
            verify_record(candidate.ledger, bad, vault, source_version="source-v1")


def _capture(store, candidate, *, voice=False, decision="my_thought", target=None):
    with connect(store.path) as db:
        record_capture(db, "app-synthetic", "m1", message_type="audio" if voice else "text",
                       created_ms=1790000000000, received_ms=1790000000000,
                       text=None if voice else "本人原话", file_key="audio-test" if voice else None)
        cid = db.execute("SELECT capture_id FROM captures").fetchone()[0]
    candidate.captures._event(cid, decision, "用户", 1.0, target)
    return cid


def test_capture_uses_extracted_writer_without_success_or_release(world):
    store, vault, candidate = world
    cid = _capture(store, candidate)
    receipt = candidate.capture(cid, vault)
    with connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 1
    assert receipt.raw_id == candidate.captures.get(cid)["raw_id"]
    assert [e[0] for e in candidate.events(f"capture:{cid}")] == ["raw_verified"]


def test_capture_uses_own_identity_and_waits_for_actual_target(world):
    store, vault, candidate = world
    cid = _capture(store, candidate, decision="annotation", target="missing-message")
    with pytest.raises(IngestionError, match="capture_target_pending"):
        candidate.capture(cid, vault)
    candidate.captures._event(cid, "my_thought", "用户", 1.0)
    capture = candidate.captures.get(cid)
    document = candidate.captures.render(capture, candidate.captures.identity(cid), [], 0, None)
    with connect(store.path) as db:
        raw.insert(db, capture["raw_id"], "capture", cid, "本人", document, origin="app")
    receipt = candidate.capture(cid, vault)
    assert receipt.subject_kind == "capture" and receipt.identity == "本人"
    assert "raw/自述/" in receipt.relative_path
    assert candidate._message_raw("app-synthetic", "m1", vault) == receipt.raw_id


def test_reserved_but_unwritten_target_is_not_settled(world):
    store, vault, candidate = world
    cid = _capture(store, candidate, decision="annotation", target="m0")
    with connect(store.path) as db:
        record_capture(db, "app-synthetic", "m0", message_type="text", created_ms=1789999999999,
                       received_ms=1789999999999, text="待身份原话")
    # Existing raw_id_of_message calls a reserved ID settled; candidate checks actual raw.
    with pytest.raises(IngestionError, match="capture_target_pending"):
        candidate.capture(cid, vault)


def test_reserved_adjacency_blocks_capture_and_does_not_assign_raw(world):
    store, vault, candidate = world
    cid = _capture(store, candidate)
    with connect(store.path) as db:
        record_capture(db, "app-synthetic", "m0", message_type="text", created_ms=1789999999999,
                       received_ms=1789999999999, text="前一条待处理")
        db.execute("INSERT INTO delivery_adjacency VALUES(?,?,?,?)", ("app-synthetic", "m1", "m0", 1))
    with pytest.raises(IngestionError, match="capture_adjacency_pending"):
        candidate.capture(cid, vault)
    assert candidate.ledger.current("capture", cid) is None


def test_write_conflict_or_attachment_failure_never_verifies(world, monkeypatch):
    store, vault, candidate = world
    mid = material(store, "x", "原件", media=[("image-1", PNG)])
    monkeypatch.setattr(raw, "place", lambda *a: "conflict")
    with pytest.raises(IngestionError, match="raw_attachment_conflict"):
        candidate.material(mid, vault)
    assert not any(e[0] == "raw_verified" for e in candidate.events(f"material:{mid}"))
    with connect(store.path) as db:
        assert db.execute("SELECT content FROM source_media WHERE material_id=?", (mid,)).fetchone()[0] == PNG


def _part(world, message, position, *, written=True, state="working"):
    store, vault, candidate = world
    mid = material(store, "x", f"完整原件{message}-{position}", key=f"{message}-{position}")
    with connect(store.path) as db:
        item = db.execute("SELECT item_id FROM distill_items WHERE material_id=? AND ingestion_contract=?",
                          (mid, INGESTION_CONTRACT)).fetchone()[0]
    with connect(store.path) as db:
        db.execute("INSERT OR IGNORE INTO feishu_binding VALUES(?,?,?,?,0,0)",
                   ("app-synthetic", "bot", "owner", "chat"))
        db.execute("""INSERT OR IGNORE INTO feishu_receipts
            (app_id,message_id,created_ms,raw_json,text,same_topic,content_kind,state)
            VALUES(?,?,0,'{}','synthetic',0,'links','accepted')""", ("app-synthetic", message))
        db.execute("UPDATE distill_items SET state=? WHERE item_id=?", ("working" if written else state, item))
        db.execute("INSERT INTO feishu_parts(app_id,message_id,position,item_id) VALUES(?,?,?,?)",
                   ("app-synthetic", message, position, item))
    receipt = candidate.material(mid, vault) if written else None
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET state=? WHERE item_id=?", (state, item))
    return mid, item, receipt


@pytest.mark.parametrize("second_written,second_state,code", [(True, "working", "capture_target_ambiguous"),
                                                               (False, "working", "capture_target_pending"),
                                                               (False, "failed", "capture_target_pending"),
                                                               (True, "failed", "capture_target_pending")])
def test_annotation_all_parts_not_first_raw(world, second_written, second_state, code):
    store, vault, candidate = world
    _part(world, "target", 0)
    _part(world, "target", 1, written=second_written, state=second_state)
    cid = _capture(store, candidate, decision="annotation", target="target")
    with pytest.raises(IngestionError, match=code):
        candidate.capture(cid, vault)
    assert candidate.ledger.current("capture", cid) is None


def test_multiple_current_heads_of_one_part_are_ambiguous(world):
    store, vault, candidate = world
    mid, _, target = _part(world, "target", 0)
    old = candidate.ledger.record(target.raw_id)
    raw_id = "R-20200101-9999"
    document = raw.RawDocument(f"raw/外部/2020/01/{raw_id}.md", old["content"].replace(target.raw_id, raw_id))
    with connect(store.path) as db:
        raw.insert(db, raw_id, "material", mid, "第三方", document, origin="vault")
    assert candidate.ledger.write(candidate.ledger.record(raw_id), vault) in {"placed", "already"}
    with pytest.raises(IngestionError, match="message_raw_ambiguous"):
        candidate._message_raw("app-synthetic", "target", vault)


def test_adjacency_checks_second_part_despite_first_raw_and_no_reported_unsettled(world):
    store, vault, candidate = world
    _part(world, "target", 0)
    _part(world, "target", 1, written=False)
    cid = _capture(store, candidate)
    with connect(store.path) as db:
        db.execute("INSERT INTO delivery_adjacency VALUES(?,?,?,1)", ("app-synthetic", "m1", "target"))
    listed, unsettled = candidate.captures.adjacency("app-synthetic", "m1")
    assert listed and unsettled == 0  # Demonstrate the inherited projection's actual hole.
    with pytest.raises(IngestionError, match="capture_adjacency_pending"):
        candidate.capture(cid, vault)


def test_non_capture_material_checks_feishu_adjacency_before_allocation(world):
    _, vault, candidate = world
    _part(world, "earlier", 0)
    _part(world, "earlier", 1, written=False)
    mid, item, _ = _part(world, "current", 0, written=False)
    with connect(candidate.store.path) as db:
        db.execute("INSERT INTO delivery_adjacency VALUES(?,?,?,1)", ("app-synthetic", "current", "earlier"))
    with pytest.raises(IngestionError, match="capture_adjacency_pending"):
        candidate.material(mid, vault, item_id=item)
    assert candidate.ledger.current("material", mid) is None


def test_single_target_success_then_supersession_rejects_old_annotation(world):
    store, vault, candidate = world
    _, _, target = _part(world, "target", 0)
    cid = _capture(store, candidate, decision="annotation", target="target")
    capture = candidate.captures.get(cid)
    doc = candidate.captures.render(capture, candidate.captures.identity(cid), [], 0, target.raw_id)
    with connect(store.path) as db:
        raw.insert(db, capture["raw_id"], "capture", cid, "本人附言", doc, origin="app")
    assert candidate.capture(cid, vault).identity == "本人附言"
    assert candidate._message_raw("app-synthetic", "m1", vault) == capture["raw_id"]
    old = candidate.ledger.record(target.raw_id)
    def replacement(raw_id, moment):
        content = old["content"].replace(old["raw_id"], raw_id)
        content = content.replace("\n---\n\n", f"\n取代: {old['raw_id']}\n---\n\n", 1)
        return raw.RawDocument(raw.relative_path(raw_id, "第三方", moment), content)
    newer = candidate.ledger.supersede(old["raw_id"], replacement, identity="第三方")
    assert candidate.ledger.write(newer, vault) in {"placed", "already"}
    assert candidate._message_raw("app-synthetic", "target", vault) == newer["raw_id"]
    with pytest.raises(IngestionError, match="capture_identity_mismatch"):
        candidate.capture(cid, vault)
    assert (vault / old["relative_path"]).read_bytes() == old["content"].encode()


@pytest.mark.parametrize("relative", ["C:/absolute", "C:drive-relative"])
def test_drive_paths_rejected_before_filesystem_access(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(Path, "lstat", lambda *a: pytest.fail("drive path reached filesystem"))
    with pytest.raises(IngestionError, match="path_unsafe"):
        read_regular(tmp_path, relative)


@pytest.mark.parametrize("old_identity", ["my_thought", "annotation"])
def test_third_party_correction_cannot_reuse_personal_capture_material(world, old_identity):
    store, vault, candidate = world
    target = _part(world, "target", 0)[2] if old_identity == "annotation" else None
    cid = _capture(store, candidate, voice=True, decision=old_identity,
                   target="target" if target else None)
    mid = material(store, "feishu_voice", "当前音频来源全文")
    item = store.create_item("feishu-voice://synthetic/correction")
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET material_id=?,state='working' WHERE item_id=?", (mid, item))
        db.execute("UPDATE capture_state SET item_id=? WHERE capture_id=?", (item, cid))
    capture = candidate.captures.get(cid)
    doc = candidate.captures.render(capture, candidate.captures.identity(cid), [], 0,
                                    target.raw_id if target else None)
    with connect(store.path) as db:
        raw.insert(db, capture["raw_id"], "capture", cid,
                   "本人附言" if target else "本人", doc, origin="app")
    receipt = candidate.capture(cid, vault)
    assert candidate._message_raw("app-synthetic", "m1", vault) == receipt.raw_id
    candidate.captures._event(cid, "third_party", "用户", 1.0)
    with pytest.raises(IngestionError, match="^message_raw_pending$"):
        candidate._message_raw("app-synthetic", "m1", vault)
    assert candidate.ledger.current("material", mid) is None
    assert (vault / receipt.relative_path).read_bytes() == doc.content.encode()


def test_annotation_same_identity_new_target_rejects_existing_head(world):
    store, vault, candidate = world
    first = _part(world, "first", 0)[2]
    _part(world, "second", 0)
    cid = _capture(store, candidate, decision="annotation", target="first")
    capture = candidate.captures.get(cid)
    doc = candidate.captures.render(capture, candidate.captures.identity(cid), [], 0, first.raw_id)
    with connect(store.path) as db:
        raw.insert(db, capture["raw_id"], "capture", cid, "本人附言", doc, origin="app")
    receipt = candidate.capture(cid, vault)
    assert candidate._message_raw("app-synthetic", "m1", vault) == receipt.raw_id
    candidate.captures._event(cid, "annotation", "用户", 1.0, "second")
    with pytest.raises(IngestionError, match="^message_raw_pending$"):
        candidate._message_raw("app-synthetic", "m1", vault)
    assert (vault / receipt.relative_path).read_bytes() == doc.content.encode()


@pytest.mark.parametrize("voice", [False, True])
def test_message_capture_rejects_changed_source_body(world, voice, monkeypatch):
    store, vault, candidate = world
    cid = _capture(store, candidate, voice=voice)
    if voice:
        mid = material(store, "feishu_voice", "原始音频完整正文")
        item = store.create_item("feishu-voice://synthetic/body")
        with connect(store.path) as db:
            db.execute("UPDATE distill_items SET material_id=?,state='working' WHERE item_id=?", (mid, item))
            db.execute("UPDATE capture_state SET item_id=? WHERE capture_id=?", (item, cid))
    capture = candidate.captures.get(cid)
    doc = candidate.captures.render(capture, candidate.captures.identity(cid), [], 0, None)
    with connect(store.path) as db:
        raw.insert(db, capture["raw_id"], "capture", cid, "本人", doc, origin="app")
    receipt = candidate.capture(cid, vault)
    assert candidate._message_raw("app-synthetic", "m1", vault) == receipt.raw_id
    if voice:
        # SourceFacts are immutable: a correction is a new material binding,
        # never a fixture that silently drops the production trigger.
        replacement = material(store, "feishu_voice", "更正后的完整正文", key="corrected")
        with connect(store.path) as db:
            db.execute("UPDATE distill_items SET material_id=? WHERE item_id=?", (replacement, item))
    else:
        # Text captures are immutable. Inject only a divergent render body for
        # the same capture; this is a projection diagnostic, not a platform
        # retransmission or permission to update the authoritative source.
        original_render = candidate.captures.render
        def divergent_render(current, *args, **kwargs):
            assert current["capture_id"] == cid and current["text"] == capture["text"]
            actual = original_render(current, *args, **kwargs)
            projected = original_render({**dict(current), "text": "受控不一致投影正文"}, *args, **kwargs)
            header = actual.content.split("\n---\n", 1)[0]
            body = projected.content.split("\n---\n", 1)[1]
            assert body != actual.content.split("\n---\n", 1)[1]
            return raw.RawDocument(actual.relative_path, header + "\n---\n" + body)
        monkeypatch.setattr(candidate.captures, "render", divergent_render)
    with pytest.raises(IngestionError, match="^message_raw_pending$"):
        candidate._message_raw("app-synthetic", "m1", vault)
    assert (vault / receipt.relative_path).read_bytes() == doc.content.encode()
    assert candidate.captures.get(cid)["text"] == capture["text"]


def test_nested_capture_dependency_remains_pending_without_recursion(world):
    store, vault, candidate = world
    cid = _capture(store, candidate)
    capture = candidate.captures.get(cid)
    doc = candidate.captures.render(capture, candidate.captures.identity(cid), [], 0, None)
    with connect(store.path) as db:
        raw.insert(db, capture["raw_id"], "capture", cid, "本人", doc, origin="app")
    candidate.capture(cid, vault)
    with connect(store.path) as db:
        # A self-edge is rejected at the bounded leaf, never recursively walked.
        db.execute("INSERT INTO delivery_adjacency VALUES(?,?,?,1)", ("app-synthetic", "m1", "m1"))
    with pytest.raises(IngestionError, match="^message_raw_pending$"):
        candidate._message_raw("app-synthetic", "m1", vault)


def test_opened_fifo_is_rejected_before_read(tmp_path, monkeypatch):
    target = tmp_path / "raw.txt"
    target.write_bytes(b"original")
    original_open = os.open
    def replace_at_open(path, flags, *args, **kwargs):
        if Path(path) == target:
            target.unlink()
            os.mkfifo(target)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", replace_at_open)
    monkeypatch.setattr(os, "read", lambda *args: pytest.fail("read called on substituted FIFO"))
    with pytest.raises(IngestionError, match="readback_changed"):
        read_regular(tmp_path, "raw.txt")


@pytest.mark.parametrize("change", ["mode", "nlink", "parent"])
def test_readback_detects_metadata_and_parent_replacement(tmp_path, monkeypatch, change):
    parent = tmp_path / "directory"
    parent.mkdir()
    target = parent / "raw.txt"
    target.write_bytes(b"original")
    target.chmod(0o644)
    original_read = os.read
    changed = False
    def read_then_change(fd, size):
        nonlocal changed
        content = original_read(fd, size)
        if not changed:
            changed = True
            if change == "mode":
                target.chmod(0o600)
            elif change == "nlink":
                os.link(target, tmp_path / "another-name")
            else:
                parent.rename(tmp_path / "old-directory")
                parent.mkdir()
                target.write_bytes(b"original")
        return content
    monkeypatch.setattr(os, "read", read_then_change)
    with pytest.raises(IngestionError, match="readback_changed"):
        read_regular(tmp_path, "directory/raw.txt")


def test_external_exception_text_is_not_persisted_as_pending_reason(world, monkeypatch):
    store, vault, candidate = world
    mid = material(store, "x", "原件")
    candidate.material(mid, vault)
    from knowledge_distiller.v1 import ingestion
    def fail(*args, **kwargs):
        raise IngestionError("external private source text")
    monkeypatch.setattr(ingestion, "verify_record", fail)
    with pytest.raises(IngestionError):
        candidate.material(mid, vault)
    with connect(store.path) as db:
        details = " ".join(r[0] for r in db.execute("SELECT detail_json FROM ingestion_events"))
    assert "external private source text" not in details and "readback_pending" in details


def test_voice_does_not_need_success_or_release_audio(world, tmp_path):
    store, vault, candidate = world
    cid = _capture(store, candidate, voice=True)
    mid = material(store, "feishu_voice", "原始ASR完整自述")
    item = store.create_item("feishu-voice://synthetic/1")
    audio = tmp_path / "synthetic-audio.bin"
    audio.write_bytes(b"synthetic audio bytes, not real media")
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET material_id=?,state='working' WHERE item_id=?", (mid, item))
        db.execute("UPDATE capture_state SET item_id=?,audio_path=? WHERE capture_id=?", (item, str(audio), cid))
    with pytest.raises(IngestionError, match="capture_route_required"):
        candidate.material(mid, vault)
    receipt = candidate.capture(cid, vault)
    assert receipt.identity == "本人"
    assert candidate._message_raw("app-synthetic", "m1", vault) == receipt.raw_id
    assert store.item_bundle(item)["state"] == "working"
    assert audio.read_bytes().startswith(b"synthetic")
    assert candidate.captures.get(cid)["audio_released_at"] is None


CHILD = """
import os,sys,time
from pathlib import Path
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.ingestion import Ingestion
c=Ingestion(Store(Path(sys.argv[1])));c.initialize()
if sys.argv[4]=='crash':
 original=c._insert_proven
 def event(db,record,item_id,kind,binding,lock):
  if kind=='raw_verified': os._exit(23)
  return original(db,record,item_id,kind,binding,lock)
 c._insert_proven=event
for attempt in range(100):
 try:
  c.material(int(sys.argv[2]),Path(sys.argv[3]));break
 except BlockingIOError:
  if attempt==99: raise
  time.sleep(.02)
"""


def test_multiprocess_same_subject_and_crash_after_placement(world):
    store, vault, candidate = world
    mid = material(store, "x", "跨进程原件", media=[("image-1", PNG)])
    args = [sys.executable, "-c", CHILD, str(store.path), str(mid), str(vault)]
    child = subprocess.run(args + ["crash"], capture_output=True, timeout=30)
    assert child.returncode == 23, child.stderr
    assert not any(e[0] == "raw_verified" for e in candidate.events(f"material:{mid}"))
    children = [subprocess.Popen(args + ["normal"], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for child in children:
        _, err = child.communicate(timeout=30)
        assert child.returncode == 0, err
    assert [e[0] for e in candidate.events(f"material:{mid}")].count("raw_verified") == 1
    assert len(list((vault / "raw").rglob("*.md"))) == 1
    with sqlite3.connect(candidate.path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE ingestion_events SET detail_json='{}'")


def test_full_adjacency_preserves_two_objects_but_annotation_does_not_choose(world):
    store, vault, candidate = world
    first = _part(world, "target", 0)[2]
    second = _part(world, "target", 1)[2]
    cid = _capture(store, candidate)
    with connect(store.path) as db:
        db.execute("INSERT INTO delivery_adjacency VALUES(?,?,?,7)", ("app-synthetic", "m1", "target"))
        objects, code = candidate.captures.message_raws(db, "app-synthetic", "target")
    assert code is None and [o["ordinal"] for o in objects] == [0, 1]
    assert [o["record"]["raw_id"] for o in objects] == [first.raw_id, second.raw_id]
    receipt = candidate.capture(cid, vault)
    from knowledge_distiller.v1.ingestion import envelope_fields
    fields = envelope_fields((vault / receipt.relative_path).read_bytes())
    assert fields["邻接"] == [{"编号": first.raw_id, "间隔秒": 7}, {"编号": second.raw_id, "间隔秒": 7}]
    candidate.captures._event(cid, "annotation", "用户", 1.0, "target")
    with pytest.raises(IngestionError, match="capture_target_ambiguous"):
        candidate.capture(cid, vault)
    assert (vault / receipt.relative_path).read_bytes() == candidate.ledger.record(receipt.raw_id)["content"].encode()


def test_partial_error_is_pending_even_if_another_part_is_written(world):
    store, vault, candidate = world
    _part(world, "target", 0)
    with connect(store.path) as db:
        db.execute("INSERT INTO feishu_parts(app_id,message_id,position,error) VALUES(?,?,1,?)",
                   ("app-synthetic", "target", "source_input_unsupported"))
        objects, code = candidate.captures.message_raws(db, "app-synthetic", "target")
    assert objects == () and code == "message_raw_pending"
    with pytest.raises(IngestionError, match="message_raw_pending"):
        candidate._message_raw("app-synthetic", "target", vault)


def test_material_source_projection_cannot_validate_arbitrary_ledger_body(world):
    store, vault, candidate = world
    mid = material(store, "x", "真实完整来源")
    record = candidate.ledger.ensure_material(mid)
    # Use the existing writer/supersession interface, no source mutation or
    # disabled triggers. A fabricated ledger body is not a source certificate.
    forged = candidate.ledger.supersede(record["raw_id"], lambda rid, moment: raw.RawDocument(
        raw.relative_path(rid, "第三方", moment), record["content"].replace(record["raw_id"], rid).replace("真实完整来源", "伪造正文")),
        identity="第三方")
    assert candidate.ledger.write(forged, vault) in {"placed", "already"}
    with pytest.raises(IngestionError, match="source_fact_binding_invalid"):
        candidate.material(mid, vault)
    assert candidate.events(f"material:{mid}") == []


def test_material_cycle_is_pending_at_bounded_leaf(world):
    store, vault, candidate = world
    _part(world, "self", 0)
    with connect(store.path) as db:
        db.execute("INSERT INTO delivery_adjacency VALUES(?,?,?,1)", ("app-synthetic", "self", "self"))
    with pytest.raises(IngestionError, match="capture_adjacency_pending"):
        candidate._message_raw("app-synthetic", "self", vault)


def test_current_decision_changed_after_write_cannot_commit_raw_verified(world, monkeypatch):
    store, vault, candidate = world
    cid = _capture(store, candidate)
    original = candidate.ledger.write
    def write_then_change(record, destination):
        result = original(record, destination)
        candidate.captures._event(cid, "annotation", "用户", 1.0, "new-target")
        return result
    monkeypatch.setattr(candidate.ledger, "write", write_then_change)
    with pytest.raises(IngestionError, match="source_binding_changed"):
        candidate.capture(cid, vault)
    assert candidate.events(f"capture:{cid}") == []
    record = candidate.ledger.current("capture", cid)
    assert (vault / record["relative_path"]).read_bytes() == record["content"].encode()


def test_changed_adjacency_is_rejected_inside_assignment_transaction(world, monkeypatch):
    store, vault, candidate = world
    _part(world, "earlier", 0)
    mid, item, _ = _part(world, "current", 0, written=False)
    original = candidate.ledger.ensure_material
    def change_before_assignment(material_id, **kwargs):
        with connect(store.path) as db:
            db.execute("INSERT INTO delivery_adjacency VALUES(?,?,?,1)", ("app-synthetic", "current", "earlier"))
        return original(material_id, **kwargs)
    monkeypatch.setattr(candidate.ledger, "ensure_material", change_before_assignment)
    with pytest.raises(IngestionError, match='source_binding_changed'):
        candidate.material(mid, vault, item_id=item)
    assert candidate.ledger.heads('material', mid) == ()


def test_nonnull_empty_confirmation_still_blocks_raw(world):
    store, vault, candidate = world
    mid = material(store, 'x', 'synthetic source')
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET confirmation_json='' WHERE material_id=?", (mid,))
    with pytest.raises(IngestionError, match='source_not_ready'):
        candidate.material(mid, vault)
    assert candidate.ledger.heads('material', mid) == ()


def test_unknown_identity_keeps_reserved_capture_without_raw(world):
    store, vault, candidate = world
    cid = _capture(store, candidate, decision='pending')
    with pytest.raises(IngestionError, match='capture_identity_pending'):
        candidate.capture(cid, vault)
    assert candidate.captures.get(cid)['raw_id']
    assert candidate.ledger.heads('capture', cid) == ()
    assert candidate.events(f'capture:{cid}') == []
