"""Explicit source -> verified raw backend. No application/worker hooks."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import yaml

from . import raw
from .captures import Captures
from .database import connect, INGESTION_CONTRACT, RAW_OWNER_COLUMNS, RAW_TERMINAL_TRIGGERS
from .wiki_lock import VaultWriteLock, session_fd_holds_lock

CONTRACT = INGESTION_CONTRACT
RESERVED_SCHEMA_VERSION = 25  # explicit migration; no implicit guard replacement


class IngestionError(ValueError):
    pass


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class _EnvelopeLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise IngestionError("raw_envelope_invalid")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_EnvelopeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def envelope_fields(content: bytes) -> dict:
    try:
        text = content.decode("utf-8")
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            raise IngestionError("raw_envelope_invalid")
        fields = yaml.load(text[4:].split("\n---\n", 1)[0], Loader=_EnvelopeLoader)
        if not isinstance(fields, dict):
            raise IngestionError("raw_envelope_invalid")
        return fields
    except (yaml.YAMLError, UnicodeError) as error:
        raise IngestionError("raw_envelope_invalid") from error


def read_regular(root: Path, relative: str) -> bytes:
    """Read without following symlinks, detecting replacement during the read."""
    if (not isinstance(relative, str) or "\\" in relative or ":" in relative or "\0" in relative
            or any(p in {"", ".", ".."} for p in relative.split("/"))
            or PurePosixPath(relative).is_absolute()):
        raise IngestionError("path_unsafe")
    root = Path(root).absolute()
    try:
        parents = []
        for part in (*reversed(root.parents), root):
            info = part.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise IngestionError("path_unsafe")
            parents.append((part, info))
        target = root
        components = relative.split("/")
        for index, part in enumerate(components):
            target /= part
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise IngestionError("path_unsafe")
            if index < len(components) - 1:
                if not stat.S_ISDIR(info.st_mode):
                    raise IngestionError("path_unsafe")
                parents.append((target, info))
        before = target.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise IngestionError("path_unsafe")
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        try:
            opened = os.fstat(fd)
            key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                             s.st_ctime_ns, s.st_mode, s.st_nlink)
            parent_key = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_nlink)
            def check_parents():
                for path, prior in parents:
                    now = path.lstat()
                    if not stat.S_ISDIR(now.st_mode) or parent_key(now) != parent_key(prior):
                        raise IngestionError("readback_changed")
            if not stat.S_ISREG(opened.st_mode) or key(before) != key(opened):
                raise IngestionError("readback_changed")
            check_parents()
            chunks = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(fd)
            if key(before) != key(opened) or key(opened) != key(after) or key(after) != key(target.lstat()):
                raise IngestionError("readback_changed")
            check_parents()
            return b"".join(chunks)
        finally:
            os.close(fd)
    except OSError as error:
        raise IngestionError("readback_unavailable") from error


@dataclass(frozen=True)
class RawReceipt:
    subject_kind: str
    subject_id: int
    raw_id: str
    relative_path: str
    identity: str
    format_version: int
    content_sha256: str
    byte_count: int
    source_version: str
    attachments: tuple[tuple[str, str], ...]


def verify_record(ledger: raw.RawLedger, record, vault: Path,
                  *, source_version: str, _released=False) -> RawReceipt:
    if record is None or not source_version:
        raise IngestionError("binding_missing")
    content = record["content"].encode("utf-8")
    if digest(content) != record["content_sha256"]:
        raise IngestionError("ledger_hash_mismatch")
    if raw._duplicate_id(Path(vault), record["raw_id"], record["relative_path"]):
        raise IngestionError("raw_id_collision")
    if read_regular(vault, record["relative_path"]) != content:
        raise IngestionError("raw_bytes_mismatch")
    try:
        envelope = envelope_fields(content)
    except (ValueError, UnicodeError) as error:
        raise IngestionError("raw_envelope_invalid") from error
    identity, raw_id = record["identity"], record["raw_id"]
    path = PurePosixPath(record["relative_path"])
    if (raw.ID_RE.fullmatch(raw_id) is None or identity not in raw.DIRECTORIES
            or len(path.parts) != 5 or path.parts[:2] != ("raw", raw.DIRECTORIES[identity])
            or path.name != raw_id + ".md" or envelope.get("编号") != raw_id
            or envelope.get("身份") != identity or type(envelope.get("格式版本")) is not int
            or envelope["格式版本"] != raw.FORMAT_VERSION
            or envelope.get("未保留附件")):
        raise IngestionError("raw_envelope_mismatch")
    application = envelope.get("应用记录", {})
    key = "capture_id" if record["subject_kind"] == "capture" else "material_id"
    if not isinstance(application, dict) or application.get(key) != record["subject_id"]:
        raise IngestionError("raw_subject_mismatch")
    attachments = []
    manifest = json.loads(record["attachments_json"])
    if record["subject_kind"] == "material":
        with connect(ledger.store.path) as db:
            retained = {(r["member_id"], r["sha256"], r["mime_type"]) for r in db.execute(
                "SELECT member_id,sha256,mime_type FROM source_media WHERE material_id=?", (record["subject_id"],))}
        declared = {(a["member_id"], a["sha256"], a["mime_type"]) for a in manifest}
        if retained != declared or len(declared) != len(manifest):
            raise IngestionError("attachment_manifest_incomplete")
    for attachment in manifest:
        relative = f"附件/raw/{raw_id}/{attachment['filename']}"
        content = read_regular(vault, relative)
        # Only the internal release path may use previously proved immutable
        # attachment identities after owned source bytes have been cleared.
        # This read-only result is never an API success/release capability.
        expected = content if _released else raw._attachment_bytes(ledger.store, record, attachment)
        if content != expected or digest(content) != attachment["sha256"]:
            raise IngestionError("attachment_bytes_mismatch")
        attachments.append((relative, attachment["sha256"]))
    return RawReceipt(record["subject_kind"], record["subject_id"], raw_id,
                      record["relative_path"], identity, raw.FORMAT_VERSION,
                      record["content_sha256"], len(record["content"].encode("utf-8")),
                      source_version, tuple(attachments))


class Ingestion:
    """Explicit raw-verified-v1 backend, using the application's process ledger.

    No constructor/initialize call migrates or backfills a database. No product
    caller is changed. Initial delivery hashes remain immutable; final proof
    separately binds actual source, current decision, heads and resolved links.
    """

    def __init__(self, store):
        self.store = store
        self.path = Path(store.path)
        self.ledger = raw.RawLedger(store)
        self.captures = Captures(store)

    def initialize(self):
        # Validate only. An earlier candidate24 must be explicitly rebuilt.
        if not self.path.is_file():
            raise IngestionError("candidate_schema_rebuild_required")
        with connect(self.path) as db:
            guard = db.execute("SELECT sql FROM sqlite_master WHERE name='ingestion_events_proof_unavailable'").fetchone()
            if guard is None or "ingestion_proof" not in guard[0]:
                raise IngestionError("candidate_schema_rebuild_required")

    def events(self, subject: str):
        kind, subject_id = subject.split(":", 1)
        with connect(self.path) as db:
            return [tuple(row) for row in db.execute(
                "SELECT kind,binding_sha256,detail_json FROM ingestion_events WHERE subject_kind=? AND subject_id=? ORDER BY event_id",
                (kind, int(subject_id)))]

    def _terminal_owner(self, db, item_id, subject_kind, subject_id, expected_revision):
        row = db.execute('SELECT * FROM distill_items WHERE item_id=?', (item_id,)).fetchone()
        if row is None or row['review_revision'] != expected_revision:
            raise IngestionError('raw_terminal_stale')
        if (row['ingestion_contract'] != CONTRACT or row['material_id'] is None
                or row['confirmation_json'] is not None or row['dismissed_at'] is not None
                or row['error_code'] is not None or row['rejection_reason'] is not None
                or not row['source_binding_sha256'] or not row['relation_binding_sha256']
                or not ((row['state'] == 'working' and row['phase'] in {'collecting', 'reviewing'})
                        or (row['state'] == 'raw_saved' and row['phase'] == 'done'))):
            raise IngestionError('raw_terminal_owner_pending')
        if (db.execute('SELECT 1 FROM collection_members WHERE item_id=?', (item_id,)).fetchone()
                or db.execute('SELECT count(*) FROM distill_items WHERE material_id=?',
                              (row['material_id'],)).fetchone()[0] != 1
                or db.execute('SELECT count(*) FROM capture_state WHERE item_id=?', (item_id,)).fetchone()[0] > 1):
            raise IngestionError('raw_terminal_owner_ambiguous')
        if subject_kind == 'material':
            if row['material_id'] != subject_id:
                raise IngestionError('raw_terminal_subject_mismatch')
        else:
            capture = self.captures.get(subject_id)
            decision = self.captures.identity(subject_id) if capture else None
            if (capture is None or capture['item_id'] != item_id or capture['message_type'] != 'audio'
                    or decision is None or decision['result'] not in {'my_thought', 'annotation'}):
                raise IngestionError('raw_terminal_capture_pending')
        return {key: row[key] for key in RAW_OWNER_COLUMNS}

    def complete_raw_owner(self, item_id: int, vault: Path, *, subject_kind: str,
                           subject_id: int, expected_revision: int) -> RawReceipt:
        """Explicit backend-only writer terminal; no knowledge, release or UI caller.

        A queued owner must first be claimed through the original FIFO. A replay
        requires the current revision and fresh bytes, never a previous receipt.
        """
        if (type(item_id) is not int or item_id <= 0 or type(subject_id) is not int or subject_id <= 0
                or type(expected_revision) is not int or expected_revision < 0 or type(subject_kind) is not str
                or subject_kind not in {'material', 'capture'}):
            raise IngestionError('raw_terminal_input_invalid')
        self.initialize()
        with connect(self.path) as db:
            guards = dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE name LIKE 'distill_items_raw_terminal_%'"))
            expected = {s.split()[2]: s for s in RAW_TERMINAL_TRIGGERS}
            if db.execute('PRAGMA user_version').fetchone()[0] != 25 or guards != expected:
                raise IngestionError('raw_terminal_schema_required')
        if not Path(vault).is_dir():
            raise IngestionError('vault_unavailable')
        with VaultWriteLock.acquire(vault) as lock:
            with connect(self.path) as db:
                owner = self._terminal_owner(db, item_id, subject_kind, subject_id, expected_revision)
            # Capture the full source/relation state before the writer can assign
            # a raw head. Exclude heads only for this pre-assignment comparison.
            with connect(self.path) as db:
                before_source = self._source_state(db, subject_kind, subject_id, heads_required=False)
            def without_heads(value):
                # Only this subject can acquire its first head. Nested material
                # and relationship heads remain part of the source CAS.
                return {k: v for k, v in value.items() if k != 'heads'}
            def source_hash(value):
                return digest(b'raw-terminal-source-v1\0' + encoded(without_heads(value)).encode())
            source_snapshot = source_hash(before_source)
            if owner['state'] != 'raw_saved':
                if subject_kind == 'material':
                    self._material(subject_id, lock.vault, item_id=item_id, lock=lock)
                else:
                    self._capture(subject_id, lock.vault, lock)
            with connect(self.path) as db:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                current = self._terminal_owner(db, item_id, subject_kind, subject_id, expected_revision)
                if current != owner:
                    raise IngestionError('raw_terminal_stale')
                source = self._source_state(db, subject_kind, subject_id)
                if (source_hash(source) != source_snapshot
                        or (before_source['heads'] and source['heads'] != before_source['heads'])):
                    raise IngestionError('source_binding_changed')
                record = db.execute('SELECT * FROM raw_records WHERE raw_id=?',
                                    (source['heads'][0]['raw_id'],)).fetchone()
                binding = self._binding(db, record, item_id)
                # This performs current context and every attachment readback
                # even when an identical raw_verified event already exists.
                receipt = self._insert_proven(db, record, item_id, 'raw_verified', binding, lock)
                if (not isinstance(receipt, RawReceipt) or receipt.raw_id != record['raw_id']
                        or receipt.source_version != binding or receipt.subject_kind != subject_kind
                        or receipt.subject_id != subject_id or receipt.content_sha256 != record['content_sha256']):
                    raise IngestionError('raw_terminal_proof_invalid')
                if (self._terminal_owner(db, item_id, subject_kind, subject_id, expected_revision) != owner
                        or self._binding(db, record, item_id) != binding
                        or source_hash(self._source_state(db, subject_kind, subject_id)) != source_snapshot):
                    raise IngestionError('raw_terminal_stale')
                if owner['state'] == 'raw_saved':
                    return receipt
                capability = (item_id, expected_revision, 'raw_saved', 'done')
                db.create_function('ingestion_raw_terminal', 4, lambda *args: int(
                    args == capability and db.in_transaction and session_fd_holds_lock(lock.vault, lock.descriptor)))
                try:
                    # All original columns participate, not only the source hash
                    # which intentionally excludes workflow state/revision.
                    predicate = ' AND '.join(f'"{key}" IS ?' for key in RAW_OWNER_COLUMNS)
                    changed = db.execute(f"""UPDATE distill_items SET state='raw_saved',phase='done',updated_at=?
                        WHERE {predicate}""", (datetime.now(UTC).isoformat(), *(owner[k] for k in RAW_OWNER_COLUMNS))).rowcount
                    if changed != 1:
                        raise IngestionError('raw_terminal_stale')
                finally:
                    db.create_function('ingestion_raw_terminal', 4, lambda *_: 0)
                final = dict(db.execute('SELECT * FROM distill_items WHERE item_id=?', (item_id,)).fetchone())
                if (final['state'] != 'raw_saved' or final['phase'] != 'done'
                        or final['review_revision'] != expected_revision + 1
                        or any(final[k] != owner[k] for k in RAW_OWNER_COLUMNS
                               if k not in {'state', 'phase', 'updated_at', 'review_revision'})):
                    raise IngestionError('raw_terminal_commit_invalid')
                return receipt

    def _owner(self, material_id, item_id=None):
        with connect(self.path) as db:
            owners = db.execute("SELECT item_id FROM distill_items WHERE material_id=? AND ingestion_contract=? ORDER BY item_id",
                                (material_id, CONTRACT)).fetchall()
        if item_id is None:
            if len(owners) != 1:
                raise IngestionError("ingestion_owner_required")
            item_id = owners[0]["item_id"]
        item = self.store.item_bundle(item_id)
        if (item is None or item["material_id"] != material_id or item["ingestion_contract"] != CONTRACT
                or item["confirmation_json"] is not None or item["dismissed_at"] is not None or item["state"] == "failed"):
            raise IngestionError("source_not_ready")
        return item_id

    def _pending(self, item_id, code):
        if item_id is not None:
            item = self.store.item_bundle(item_id)
            if item is not None and item["ingestion_contract"] == CONTRACT:
                self.store.append_ingestion_event(item_id, kind="raw_pending", code=code)

    @staticmethod
    def _rows(db, sql, parameters=()):
        # Hash byte inputs before canonicalization; never copy them into events.
        return [{k: digest(v) if isinstance(v, bytes) else v for k, v in dict(row).items()}
                for row in db.execute(sql, parameters)]

    def _item_state(self, db, item_id):
        rows = self._rows(db, """SELECT item_id,submitted_url,material_id,confirmation_json,dismissed_at,
            ingestion_contract,source_binding_sha256,relation_binding_sha256
            FROM distill_items WHERE item_id=?""", (item_id,))
        if not rows:
            raise IngestionError("source_not_ready")
        return {"owner": rows[0], "submitted": self._rows(db,
            "SELECT * FROM submitted_sources WHERE item_id=?", (item_id,))}

    def _source_state(self, db, kind, subject_id, *, heads_required=True):
        if kind == "material":
            source = self._rows(db, "SELECT m.*,sf.* FROM materials m JOIN source_facts sf USING(material_id) WHERE material_id=?",
                                (subject_id,))
            if len(source) != 1:
                raise IngestionError("source_not_ready")
            owner_ids = [r[0] for r in db.execute("SELECT item_id FROM distill_items WHERE material_id=? ORDER BY item_id", (subject_id,))]
            result = {"source": source, "owners": [self._item_state(db, i) for i in owner_ids],
                      "media": self._rows(db, "SELECT member_id,position,mime_type,sha256 FROM source_media WHERE material_id=? ORDER BY position,member_id",
                                          (subject_id,))}
        else:
            capture = self.captures.get(subject_id)
            if capture is None:
                raise IngestionError("capture_source_pending")
            # Released-at is an outcome, not a new source revision.
            capture = {k: v for k, v in capture.items() if k != "audio_released_at"}
            result = {"capture": capture, "decision": self.captures.identity(subject_id),
                      "transcript": self.captures.transcript(subject_id)}
            if capture["item_id"]:
                item = self.store.item_bundle(capture["item_id"])
                if item is None or item["source_fact_id"] is None:
                    raise IngestionError("capture_source_pending")
                result["material"] = self._source_state(db, "material", item["material_id"], heads_required=False)
        heads = self._rows(db, """SELECT raw_id,identity,content_sha256,relative_path,attachments_json,supersedes FROM raw_records r
            WHERE subject_kind=? AND subject_id=? AND NOT EXISTS(SELECT 1 FROM raw_records n WHERE n.supersedes=r.raw_id)
            ORDER BY raw_id""", (kind, subject_id))
        if len(heads) > 1 or (heads_required and len(heads) != 1):
            raise IngestionError("raw_heads_ambiguous")
        result["heads"] = heads
        # Capture and message routing are authoritative even when the identity
        # stays unchanged. Record their complete current rows in the CAS hash.
        messages = self._rows(db, """SELECT c.app_id,c.message_id FROM captures c JOIN capture_state s USING(capture_id)
            WHERE (?='capture' AND c.capture_id=?) OR (?='material' AND s.item_id IN
                (SELECT item_id FROM distill_items WHERE material_id=?))
            UNION SELECT p.app_id,p.message_id FROM feishu_parts p JOIN distill_items i USING(item_id)
                WHERE ?='material' AND i.material_id=? ORDER BY app_id,message_id""",
                             (kind, subject_id, kind, subject_id, kind, subject_id))
        result["messages"] = []
        for message in messages:
            args = (message["app_id"], message["message_id"])
            capture_rows = self._rows(db, "SELECT * FROM captures WHERE app_id=? AND message_id=?", args)
            message_captures = []
            for c in capture_rows:
                message_captures.append({"capture": c,
                    "state": self._rows(db, "SELECT capture_id,item_id,audio_path FROM capture_state WHERE capture_id=?", (c["capture_id"],)),
                    "decision": self._rows(db, "SELECT * FROM capture_identity_events WHERE capture_id=? ORDER BY event_id DESC LIMIT 1", (c["capture_id"],))})
            result["messages"].append({
                "message": message,
                "captures": message_captures,
                "receipt": self._rows(db, "SELECT * FROM feishu_receipts WHERE app_id=? AND message_id=?", args),
                "parts": self._rows(db, "SELECT * FROM feishu_parts WHERE app_id=? AND message_id=? ORDER BY position", args),
                "adjacency": self._rows(db, "SELECT * FROM delivery_adjacency WHERE app_id=? AND message_id=? ORDER BY gap_seconds DESC,earlier_message_id", args)})
        return result

    def _binding(self, db, record, item_id):
        state = self._source_state(db, record["subject_kind"], record["subject_id"])
        if state["heads"][0]["raw_id"] != record["raw_id"]:
            raise IngestionError("raw_head_changed")
        if item_id is not None:
            item = self.store.item_bundle(item_id)
            if item is None or item["confirmation_json"] is not None or item["dismissed_at"] is not None or item["state"] == "failed":
                raise IngestionError("source_not_ready")
            if ((record["subject_kind"] == "material" and item["material_id"] != record["subject_id"])
                    or (record["subject_kind"] == "capture" and state["capture"]["item_id"] != item_id)):
                raise IngestionError("ingestion_owner_required")
            state["requested_owner"] = self._item_state(db, item_id)
        fields = envelope_fields(record["content"].encode())
        ids = [link["编号"] for link in fields.get("邻接", [])]
        if fields.get("附言对象"):
            ids.append(fields["附言对象"])
        # One frozen dependency level; nested/cyclic source dependencies remain
        # pending at the semantic guard. No live recursive relationship builder.
        dependencies = []
        for raw_id in sorted(set(ids)):
            row = db.execute("SELECT * FROM raw_records WHERE raw_id=?", (raw_id,)).fetchone()
            if row is None:
                raise IngestionError("capture_adjacency_pending")
            dependencies.append(self._source_state(db, row["subject_kind"], row["subject_id"]))
        state["dependencies"] = dependencies
        return digest(encoded(state).encode())

    def _insert_proven(self, db, record, item_id, kind, binding, lock):
        # No RawReceipt/caller manifest argument: compute fresh proof here.
        if (not isinstance(lock, VaultWriteLock) or not db.in_transaction
                or not session_fd_holds_lock(lock.vault, lock.descriptor)):
            raise IngestionError("filesystem_proof_unavailable")
        vault = lock.vault
        if self._binding(db, record, item_id) != binding:
            raise IngestionError("source_binding_changed")
        self._validate_context(record, vault, item_id)
        receipt = self._readback(db, record, vault, binding)
        manifest = asdict(receipt)
        manifest.pop("source_version")  # No arbitrary metadata/decision strings.
        state = self._source_state(db, record["subject_kind"], record["subject_id"])
        source = state if record["subject_kind"] == "material" else state.get("material")
        fact = source["source"][0] if source else None
        decision = state.get("decision")
        item = self.store.item_bundle(item_id) if item_id is not None else None
        fields = envelope_fields(record["content"].encode())
        manifest.update({
            "owner_item_id": item_id,
            "source_fact_id": fact["source_fact_id"] if fact else None,
            "source_snapshot_sha256": digest(fact["snapshot"].encode()) if fact else None,
            "capture_revision": decision["event_id"] if decision else None,
            "decision_sha256": digest(encoded(decision).encode()) if decision else None,
            "source_binding_sha256": (item["source_binding_sha256"] if item is not None and item["source_binding_sha256"] is not None
                                      else digest(encoded(state.get("capture")).encode())),
            "relation_binding_sha256": (item["relation_binding_sha256"] if item is not None and item["relation_binding_sha256"] is not None
                                        else digest(encoded([state["messages"], decision["target_message_id"] if decision else None]).encode())),
            "input_sha256": digest(encoded(state).encode()),
            "relation_manifest_sha256": digest(encoded([fields.get("邻接", []), fields.get("附言对象")]).encode()),
            "attachment_manifest": [{**a, "byte_count": len(read_regular(vault, f"附件/raw/{record['raw_id']}/{a['filename']}"))}
                                    for a in json.loads(record["attachments_json"])]})
        if record["subject_kind"] == "capture" and state["capture"]["message_type"] == "audio":
            path = state["capture"]["audio_path"]
            audio = None
            if path is not None and (Path(path).exists() or Path(path).is_symlink()):
                content = read_regular(Path(path).parent, Path(path).name)
                audio = {"path_sha256": digest(os.fsencode(path)), "sha256": digest(content), "byte_count": len(content)}
            for prior in db.execute("""SELECT kind,detail_json FROM ingestion_events WHERE subject_kind='capture'
                AND subject_id=? AND binding_sha256=? AND kind IN ('raw_verified','release_authorized','media_released')
                ORDER BY event_id""", (record["subject_id"], binding)):
                known = json.loads(prior["detail_json"])["manifest"].get("audio_manifest")
                if known is not None:
                    if audio is not None and audio != known:
                        raise IngestionError("capture_source_changed")
                    if audio is None and prior["kind"] in {"release_authorized", "media_released"}:
                        audio = known
            manifest["audio_manifest"] = audio
        detail = encoded({"code": kind, "manifest": manifest, "vault_sha256": digest(os.fsencode(vault)),
                          "final_binding_sha256": binding})
        if self._binding(db, record, item_id) != binding:
            raise IngestionError("source_binding_changed")
        key = digest(encoded([CONTRACT, record["subject_kind"], record["subject_id"], item_id,
                              kind, binding, detail]).encode())
        expected = (kind, binding, detail)
        db.create_function("ingestion_proof", 3, lambda *args: int(args == expected and db.in_transaction))
        try:
            db.execute("""INSERT INTO ingestion_events
                (event_key,contract,subject_kind,subject_id,item_id,kind,binding_sha256,detail_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
                       (key, CONTRACT, record["subject_kind"], record["subject_id"], item_id, kind,
                        binding, detail, datetime.now(UTC).isoformat()))
        finally:
            db.create_function("ingestion_proof", 3, lambda *_: 0)
        return receipt

    def _proven_release(self, db, record, binding, vault):
        rows = db.execute("""SELECT detail_json FROM ingestion_events WHERE subject_kind=? AND subject_id=?
            AND kind='media_released' AND binding_sha256=?""",
                          (record["subject_kind"], record["subject_id"], binding)).fetchall()
        for row in rows:
            detail = json.loads(row[0])
            manifest = detail["manifest"]
            if (detail["vault_sha256"] == digest(os.fsencode(vault))
                    and manifest["raw_id"] == record["raw_id"]
                    and manifest["content_sha256"] == record["content_sha256"]):
                return True
        return False

    def _readback(self, db, record, vault, binding):
        released = self._proven_release(db, record, binding, vault)
        return verify_record(self.ledger, record, vault, source_version=binding,
                             _released= released)

    def _material_body(self, db, record, vault, adjacency):
        row = raw.material_row(db, record["subject_id"])
        if row is None:
            raise IngestionError("source_not_ready")
        available = raw.media(db, record["subject_id"])
        # Released identities may be rendered only when the exact final
        # binding has a durable, internally proven release at this Vault.
        prior = db.execute("""SELECT item_id,binding_sha256 FROM ingestion_events WHERE subject_kind='material'
            AND subject_id=? AND kind='media_released' ORDER BY event_id DESC""", (record["subject_id"],)).fetchall()
        for proof in prior:
            binding = self._binding(db, record, proof["item_id"])
            if binding == proof["binding_sha256"] and self._proven_release(db, record, binding, vault):
                for a in json.loads(record["attachments_json"]):
                    entry = available.get(a["member_id"])
                    if entry is None or entry["sha256"] != a["sha256"] or entry["mime_type"] != a["mime_type"]:
                        raise IngestionError("attachment_binding_changed")
                    entry["content_available"] = True
                break
        expected = raw.render_material(row, available, record["raw_id"], app_version=self.ledger.version,
                                       migrated=False, current_asr=raw._asr_label(db), adjacency=adjacency,
                                       supersedes=record["supersedes"])
        actual_fields = envelope_fields(record["content"].encode())
        expected_fields = envelope_fields(expected.content.encode())
        keys = ("编号", "格式版本", "身份", "标题", "作者", "渠道", "原链接", "产生于", "订正", "存疑",
                "未保留附件", "邻接", "取代")
        application = actual_fields.get("应用记录", {})
        if (any(actual_fields.get(k) != expected_fields.get(k) for k in keys)
                or application.get("source_fact_id") != row["source_fact_id"]
                or any(actual_fields.get("取得方式", {}).get(k) != expected_fields.get("取得方式", {}).get(k) for k in ("采集", "识别"))
                or record["content"].split("\n---\n", 1)[1] != expected.content.split("\n---\n", 1)[1]
                or json.loads(record["attachments_json"]) != [asdict(a) for a in expected.attachments]):
            raise IngestionError("source_fact_binding_invalid")

    def _validate_context(self, record, vault, item_id):
        if record["subject_kind"] == "capture":
            capture = self.captures.get(record["subject_id"])
            records = self._message_records(capture["app_id"], capture["message_id"], vault)
            if record["raw_id"] not in {r["raw_id"] for r in records}:
                raise IngestionError("capture_source_pending")
            return
        # Do not derive relationships from the legacy material_hints projection.
        with connect(self.path) as db:
            messages = db.execute("""SELECT app_id,message_id FROM feishu_parts WHERE item_id IN
                (SELECT item_id FROM distill_items WHERE material_id=?) UNION
                SELECT c.app_id,c.message_id FROM captures c JOIN capture_state s USING(capture_id)
                JOIN distill_items i USING(item_id) WHERE i.material_id=?""",
                                  (record["subject_id"], record["subject_id"])).fetchall()
            if len(messages) > 1:
                raise IngestionError("material_message_ambiguous")
            adjacency = self._relations_ready(dict(messages[0]), vault, _leaf_targets=True) if messages else []
            for message in messages:
                capture = self.captures.for_message(message["app_id"], message["message_id"])
                if capture is not None:
                    decision = self.captures.identity(capture["capture_id"])
                    if decision is None or decision["result"] != "third_party":
                        raise IngestionError("capture_route_required")
                    if self.ledger.heads("capture", capture["capture_id"]):
                        raise IngestionError("message_raw_pending")
            self._material_body(db, record, vault, adjacency)

    def _complete(self, record, lock, item_id):
        # Caller holds VaultWriteLock over assignment, placement, proof commit.
        if not session_fd_holds_lock(lock.vault, lock.descriptor):
            raise IngestionError("filesystem_proof_unavailable")
        vault = lock.vault
        with connect(self.path) as db:
            before = self._binding(db, record, item_id)
            released = self._proven_release(db, record, before, vault)
        self._pending(item_id, "writer_pending")
        if not released:
            result = self.ledger.write(record, vault)
            if result not in {"placed", "already"}:
                self._pending(item_id, "writer_pending")
                allowed = {"vault_unavailable", "raw_id_collision", "raw_attachment_conflict",
                           "raw_target_conflict", "raw_write_failed", "raw_attachment_unavailable"}
                raise IngestionError(result if result in allowed else "raw_write_failed")
        try:
            with connect(self.path) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                fresh = db.execute("SELECT * FROM raw_records WHERE raw_id=?", (record["raw_id"],)).fetchone()
                binding = self._binding(db, fresh, item_id)
                if binding != before:
                    raise IngestionError("source_binding_changed")
                self._validate_context(fresh, vault, item_id)
                # Recheck after the last read, before granting this connection
                # authority. All cooperating DB writers are excluded by BEGIN.
                receipt = self._insert_proven(db, fresh, item_id, "raw_verified", binding, lock)
                return receipt
        except (IngestionError, raw.RawError):
            self._pending(item_id, "readback_pending")
            raise

    def material(self, material_id: int, vault: Path, *, item_id: int | None = None):
        self.initialize()
        item_id = self._owner(material_id, item_id)
        if not Path(vault).is_dir():
            self._pending(item_id, "writer_pending")
            raise IngestionError("vault_unavailable")
        with VaultWriteLock.acquire(vault) as lock:
            return self._material(material_id, lock.vault, item_id=item_id, lock=lock)

    def _material(self, material_id, vault, *, item_id, lock):
        with connect(self.store.path) as db:
            row = db.execute("""SELECT m.source_kind,m.snapshot_key,m.metadata_json,sf.source_fact_id
                FROM materials m JOIN source_facts sf USING(material_id) WHERE m.material_id=?""",
                             (material_id,)).fetchone()
        if row is None:
            raise IngestionError("source_not_ready")
        if row["source_kind"] == "feishu_voice":
            raise IngestionError("capture_route_required")
        expected_adjacency = None
        if item_id is not None:
            item = self.store.item_bundle(item_id)
            if item is None or item["material_id"] != material_id or item["confirmation_json"] is not None:
                raise IngestionError("source_not_ready")
            capture = self.captures.for_item(item_id)
            if capture is not None:
                decision = self.captures.identity(capture["capture_id"])
                if decision is None or decision["result"] != "third_party":
                    raise IngestionError("capture_route_required")
                expected_adjacency = self._relations_ready(capture, vault)
            else:
                with connect(self.store.path) as db:
                    owners = db.execute("SELECT DISTINCT app_id,message_id FROM feishu_parts WHERE item_id=?",
                                        (item_id,)).fetchall()
                if len(owners) > 1:
                    raise IngestionError("material_message_ambiguous")
                if owners:
                    expected_adjacency = self._relations_ready(dict(owners[0]), vault)
        hints = self.captures.material_hints(item_id) if item_id is not None else {}
        if expected_adjacency is not None:
            # The existing envelope already has a list; freeze the complete
            # verified set rather than its legacy first-ID projection.
            hints["adjacency"] = expected_adjacency
        if len(self.ledger.heads("material", material_id)) > 1:
            raise IngestionError("raw_heads_ambiguous")
        def check_source(db):
            self._owner(material_id, item_id)
            if len(self.ledger.heads("material", material_id)) > 1:
                raise IngestionError("raw_heads_ambiguous")
            fresh = db.execute("""SELECT m.source_kind,m.snapshot_key,m.metadata_json,sf.source_fact_id
                FROM materials m JOIN source_facts sf USING(material_id) WHERE m.material_id=?""", (material_id,)).fetchone()
            if fresh is None or dict(fresh) != dict(row):
                raise IngestionError("source_binding_changed")
            if expected_adjacency is not None:
                fresh_adjacency = self._relations_ready(capture if capture is not None else dict(owners[0]), vault)
                if fresh_adjacency != expected_adjacency:
                    raise IngestionError("source_binding_changed")
        record = self.ledger.ensure_material(material_id, check_source=check_source, **hints)
        fields = envelope_fields(record["content"].encode())
        if expected_adjacency is not None and (fields.get("邻接", []) != expected_adjacency or fields.get("邻接未定")):
            raise IngestionError("capture_adjacency_mismatch")
        application = fields.get("应用记录")
        if not isinstance(application, dict) or application.get("source_fact_id") != row["source_fact_id"]:
            raise IngestionError("source_fact_binding_invalid")
        return self._complete(record, lock, item_id)

    def _message_records(self, app_id, message_id, vault, *, _leaf=False):
        """Verify Captures' complete ordered set; never allocate dependencies."""
        with connect(self.path) as db:
            objects, pending = self.captures.message_raws(db, app_id, message_id)
            if pending:
                raise IngestionError(pending)
            has_relations = db.execute("SELECT 1 FROM delivery_adjacency WHERE app_id=? AND message_id=?",
                                       (app_id, message_id)).fetchone()
            if _leaf and has_relations:
                raise IngestionError("message_raw_pending")
            capture = self.captures.for_message(app_id, message_id)
            if capture is not None:
                decision = self.captures.identity(capture["capture_id"])
                if (decision is None or decision["result"] not in {"my_thought", "annotation", "third_party"}
                        or (decision["result"] == "third_party" and any(o["record"]["subject_kind"] == "capture" for o in objects))):
                    raise IngestionError("message_raw_pending")
                if decision["result"] in {"my_thought", "annotation"}:
                    if _leaf and decision["result"] == "annotation":
                        raise IngestionError("message_raw_pending")
                    try:
                        adjacency = self._relations_ready(capture, vault, _leaf_targets=True)
                        target = (self._message_raw(app_id, decision["target_message_id"], vault, _leaf=True)
                                  if decision["result"] == "annotation" else None)
                        own = [o["record"] for o in objects if o["record"]["subject_kind"] == "capture"]
                        if len(own) != 1:
                            raise IngestionError("message_raw_pending")
                        record = own[0]
                        document = self.captures.render(capture, decision, adjacency, 0, target,
                                                       raw_id=record["raw_id"], supersedes=record["supersedes"])
                        fields = envelope_fields(record["content"].encode())
                        expected = envelope_fields(document.content.encode())
                        # Collection time and app build can differ on legitimate
                        # supersession. Source, identity and relationships cannot.
                        keys = ("编号", "格式版本", "身份", "标题", "作者", "渠道", "产生于", "订正", "存疑",
                                "身份判定", "邻接", "邻接未定", "附言对象", "取代", "应用记录")
                        if (any(fields.get(k) != expected.get(k) for k in keys)
                                or fields.get("取得方式", {}).get("识别") != expected.get("取得方式", {}).get("识别")
                                or record["content"].split("\n---\n", 1)[1] != document.content.split("\n---\n", 1)[1]):
                            raise IngestionError("message_raw_pending")
                    except (IngestionError, raw.RawError, KeyError, TypeError, AttributeError, IndexError):
                        raise IngestionError("message_raw_pending") from None
            records = tuple(o["record"] for o in objects)
            for record in records:
                if record["subject_kind"] == "material":
                    self._validate_context(record, vault, None)
                self._message_readback(db, record, Path(vault))
            return records

    def _message_raw(self, app_id, message_id, vault, *, _leaf=False):
        records = self._message_records(app_id, message_id, vault, _leaf=_leaf)
        if len(records) != 1:
            raise IngestionError("message_raw_ambiguous")
        return records[0]["raw_id"]

    def _message_readback(self, db, record, vault):
        for event in db.execute("""SELECT item_id,binding_sha256 FROM ingestion_events
            WHERE subject_kind=? AND subject_id=? AND kind='media_released' ORDER BY event_id DESC""",
                                (record["subject_kind"], record["subject_id"])):
            binding = self._binding(db, record, event["item_id"])
            if binding == event["binding_sha256"] and self._proven_release(db, record, binding, vault):
                return self._readback(db, record, vault, binding)
        return verify_record(self.ledger, record, vault, source_version="message-readback")

    def _relations_ready(self, capture, vault, *, _leaf_targets=False):
        with connect(self.store.path) as db:
            relations = db.execute("SELECT earlier_message_id,gap_seconds FROM delivery_adjacency WHERE app_id=? AND message_id=? ORDER BY gap_seconds DESC,earlier_message_id",
                                   (capture["app_id"], capture["message_id"])).fetchall()
        adjacency = []
        for relation in relations:
            try:
                records = self._message_records(capture["app_id"], relation["earlier_message_id"], vault,
                                               _leaf=_leaf_targets)
            except IngestionError as error:
                code = "capture_adjacency_ambiguous" if error.args == ("message_raw_ambiguous",) else "capture_adjacency_pending"
                raise IngestionError(code) from error
            adjacency.extend({"编号": record["raw_id"], "间隔秒": relation["gap_seconds"]} for record in records)
        return adjacency

    def capture(self, capture_id: int, vault: Path):
        self.initialize()
        with VaultWriteLock.acquire(vault) as lock:
            return self._capture(capture_id, lock.vault, lock)

    def _capture(self, capture_id, vault, lock):
        capture = self.captures.get(capture_id)
        decision = self.captures.identity(capture_id) if capture else None
        if decision is None or decision["result"] not in {"my_thought", "annotation"}:
            raise IngestionError("capture_identity_pending")
        adjacency = self._relations_ready(capture, vault)
        target = None
        if decision["result"] == "annotation":
            try:
                target = self._message_raw(capture["app_id"], decision["target_message_id"], vault)
            except IngestionError as error:
                code = "capture_target_ambiguous" if error.args == ("message_raw_ambiguous",) else "capture_target_pending"
                raise IngestionError(code) from error
        if capture["message_type"] == "audio":
            item = self.store.item_bundle(capture["item_id"]) if capture["item_id"] else None
            if (item is None or item["source_fact_id"] is None or item["confirmation_json"] is not None
                    or item["state"] == "failed" or item["dismissed_at"] is not None):
                raise IngestionError("capture_source_pending")
        def check_source(db):
            if self._relations_ready(capture, vault) != adjacency:
                raise IngestionError("source_binding_changed")
            if decision["result"] == "annotation" and self._message_raw(capture["app_id"], decision["target_message_id"], vault) != target:
                raise IngestionError("source_binding_changed")
            if capture["message_type"] == "audio":
                fresh = self.store.item_bundle(capture["item_id"])
                if (fresh is None or fresh["confirmation_json"] is not None or fresh["state"] == "failed"
                        or fresh["dismissed_at"] is not None or fresh["source_fact_id"] != item["source_fact_id"]
                        or fresh["snapshot"] != item["snapshot"] or fresh["lineage_json"] != item["lineage_json"]
                        or fresh["uncertainties_json"] != item["uncertainties_json"]):
                    raise IngestionError("source_binding_changed")
        record = self.captures.ensure_raw(capture, decision, adjacency, target, ledger=self.ledger, check_source=check_source)
        expected = "本人附言" if decision["result"] == "annotation" else "本人"
        fields = envelope_fields(record["content"].encode())
        if record["identity"] != expected or (expected == "本人附言" and fields.get("附言对象") != target):
            raise IngestionError("capture_identity_mismatch")
        if fields.get("邻接", []) != adjacency or fields.get("邻接未定"):
            raise IngestionError("capture_adjacency_mismatch")
        expected_document = self.captures.render(capture, decision, adjacency, 0, target,
                                                raw_id=record["raw_id"], supersedes=record["supersedes"])
        actual_body = record["content"].split("\n---\n", 1)[1]
        expected_body = expected_document.content.split("\n---\n", 1)[1]
        if actual_body != expected_body:
            raise IngestionError("capture_source_mismatch")
        application = fields.get("应用记录")
        if capture["message_type"] == "audio" and (not isinstance(application, dict)
                                                  or application.get("material_id") != item["material_id"]):
            raise IngestionError("capture_source_mismatch")
        return self._complete(record, lock, capture["item_id"])

    def release_material(self, material_id: int, vault: Path):
        """Explicit checked release, never called by legacy sweeps or product.

        Preserve the legacy terminal-state veto in addition to actual proof.
        A3 must decide its approved terminal projection; A2 does not fake it.
        """
        self.initialize()
        with VaultWriteLock.acquire(vault) as lock, connect(self.path) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            from .media_lifecycle import RELEASABLE
            eligible = db.execute(f"""SELECT m.material_id FROM materials m WHERE m.material_id=?
                AND m.material_id>(SELECT legacy_material_id FROM media_lifecycle WHERE singleton=1)
                AND {RELEASABLE}""", (material_id,)).fetchone()
            if eligible is None:
                raise IngestionError("release_owner_pending")
            owners = db.execute("SELECT * FROM distill_items WHERE material_id=? ORDER BY item_id", (material_id,)).fetchall()
            new_owners = [i for i in owners if i["ingestion_contract"] == CONTRACT]
            if not new_owners or any(i["dismissed_at"] is not None for i in new_owners):
                raise IngestionError("release_owner_pending")
            records = self.ledger.heads("material", material_id)
            if len(records) != 1:
                raise IngestionError("raw_heads_ambiguous")
            record = records[0]
            proofs = []
            for item in new_owners:
                binding = self._binding(db, record, item["item_id"])
                prior = db.execute("""SELECT 1 FROM ingestion_events WHERE subject_kind='material' AND subject_id=?
                    AND item_id=? AND kind='raw_verified' AND binding_sha256=?""",
                                   (material_id, item["item_id"], binding)).fetchone()
                if prior is None:
                    raise IngestionError("release_proof_pending")
                self._validate_context(record, lock.vault, item["item_id"])
                receipt = self._readback(db, record, lock.vault, binding)
                if self._binding(db, record, item["item_id"]) != binding:
                    raise IngestionError("source_binding_changed")
                proofs.append((item["item_id"], binding, receipt))
            members = db.execute("SELECT member_id,sha256,length(content) AS bytes FROM source_media WHERE material_id=? ORDER BY position", (material_id,)).fetchall()
            allowed = {(material_id, m["member_id"], m["sha256"], b"") for m in members if m["bytes"] > 0}
            released = sum(m["bytes"] for m in members)
            if not released:
                return 0  # Still performed locked exact readback and owner CAS.
            for item_id, binding, receipt in proofs:
                self._insert_proven(db, record, item_id, "release_authorized", binding, lock)
                # Event and clearing commit atomically. Any failure below rolls
                # both back; computing proof while source bytes still exist
                # avoids accepting a supplied post-release receipt.
                self._insert_proven(db, record, item_id, "media_released", binding, lock)
            db.create_function("ingestion_release", 4, lambda *args: int(args in allowed and db.in_transaction))
            try:
                db.execute("UPDATE source_media SET content=X'' WHERE material_id=? AND length(content)>0", (material_id,))
            finally:
                db.create_function("ingestion_release", 4, lambda *_: 0)
            db.execute("UPDATE media_lifecycle SET released_bytes=released_bytes+? WHERE singleton=1", (released,))
            return released

    def release_capture(self, capture_id: int, vault: Path):
        """Checked original-audio release; durable intent precedes unlink.

        Retry after unlink/DB crash rechecks the same raw/source/authorization.
        Shared recordings remain pending rather than guessing ownership.
        """
        self.initialize()
        with VaultWriteLock.acquire(vault) as lock:
            with connect(self.path) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                capture = self.captures.get(capture_id)
                item = self.store.item_bundle(capture["item_id"]) if capture and capture["item_id"] else None
                if (capture is None or capture["message_type"] != "audio" or not capture["audio_path"]
                        or item is None or item["ingestion_contract"] != CONTRACT or item["state"] != "succeeded"
                        or item["confirmation_json"] is not None or item["dismissed_at"] is not None):
                    raise IngestionError("release_owner_pending")
                expected_path = self.captures.audio_root.absolute() / f"{capture_id}.opus"
                if Path(capture["audio_path"]).absolute() != expected_path:
                    raise IngestionError("capture_audio_owner_pending")
                owners = db.execute("SELECT item_id FROM distill_items WHERE material_id=?", (item["material_id"],)).fetchall()
                shared = db.execute("SELECT 1 FROM capture_state WHERE audio_path=? AND capture_id!=?",
                                    (capture["audio_path"], capture_id)).fetchone()
                if len(owners) != 1 or shared is not None:
                    raise IngestionError("release_owner_pending")
                heads = self.ledger.heads("capture", capture_id)
                if len(heads) != 1:
                    raise IngestionError("raw_heads_ambiguous")
                record = heads[0]
                binding = self._binding(db, record, item["item_id"])
                if db.execute("""SELECT 1 FROM ingestion_events WHERE subject_kind='capture' AND subject_id=?
                    AND item_id=? AND kind='raw_verified' AND binding_sha256=?""",
                              (capture_id, item["item_id"], binding)).fetchone() is None:
                    raise IngestionError("release_proof_pending")
                self._insert_proven(db, record, item["item_id"], "release_authorized", binding, lock)
                authorized = db.execute("""SELECT detail_json FROM ingestion_events WHERE subject_kind='capture'
                    AND subject_id=? AND item_id=? AND kind='release_authorized' AND binding_sha256=? ORDER BY event_id DESC LIMIT 1""",
                                        (capture_id, item["item_id"], binding)).fetchone()
                audio = json.loads(authorized[0])["manifest"].get("audio_manifest")
                if audio is None:
                    raise IngestionError("capture_audio_unavailable")
            # Durable intent was committed with complete readback. Hold both
            # locks again while checking bindings and deleting the exact file.
            with connect(self.path) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                fresh_item = self.store.item_bundle(item["item_id"])
                shared = db.execute("SELECT 1 FROM capture_state WHERE audio_path=? AND capture_id!=?",
                                    (capture["audio_path"], capture_id)).fetchone()
                count = db.execute("SELECT count(*) FROM distill_items WHERE material_id=?", (item["material_id"],)).fetchone()[0]
                if (fresh_item is None or fresh_item["state"] != "succeeded" or fresh_item["confirmation_json"] is not None
                        or fresh_item["dismissed_at"] is not None or shared is not None or count != 1):
                    raise IngestionError("release_owner_pending")
                if self._binding(db, record, item["item_id"]) != binding:
                    raise IngestionError("source_binding_changed")
                self._validate_context(record, lock.vault, item["item_id"])
                self._readback(db, record, lock.vault, binding)
                if capture["audio_released_at"] is not None:
                    return 0
                path = Path(capture["audio_path"])
                if path.exists() or path.is_symlink():
                    content = read_regular(path.parent, path.name)
                    if digest(content) != audio["sha256"] or len(content) != audio["byte_count"]:
                        raise IngestionError("capture_source_changed")
                    path.unlink()
                self._insert_proven(db, record, item["item_id"], "media_released", binding, lock)
                stamp = datetime.now(UTC).isoformat()
                expected = ("capture", capture_id, capture["audio_path"], stamp)
                db.create_function("ingestion_release", 4, lambda *args: int(args == expected and db.in_transaction))
                try:
                    db.execute("UPDATE capture_state SET audio_released_at=? WHERE capture_id=? AND audio_released_at IS NULL", (stamp, capture_id))
                finally:
                    db.create_function("ingestion_release", 4, lambda *_: 0)
                return audio["byte_count"]
