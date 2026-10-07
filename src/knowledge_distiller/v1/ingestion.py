"""Isolated source -> verified raw candidate. No application/worker hooks.

Call initialize explicitly with a private candidate database. Retained bytes
have no TTL; neither this module nor its retries release application media.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import yaml

from . import raw
from .captures import Captures
from .database import connect

CONTRACT = "r01-ingestion-v1"
RESERVED_SCHEMA_VERSION = 24  # registration only; no production migration


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
                  *, source_version: str) -> RawReceipt:
    if record is None or not source_version:
        raise IngestionError("binding_missing")
    content = record["content"].encode("utf-8")
    if digest(content) != record["content_sha256"]:
        raise IngestionError("ledger_hash_mismatch")
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
    for attachment in json.loads(record["attachments_json"]):
        relative = f"附件/raw/{raw_id}/{attachment['filename']}"
        content = read_regular(vault, relative)
        expected = raw._attachment_bytes(ledger.store, record, attachment)
        if content != expected or digest(content) != attachment["sha256"]:
            raise IngestionError("attachment_bytes_mismatch")
        attachments.append((relative, attachment["sha256"]))
    return RawReceipt(record["subject_kind"], record["subject_id"], raw_id,
                      record["relative_path"], identity, raw.FORMAT_VERSION,
                      record["content_sha256"], len(record["content"].encode("utf-8")),
                      source_version, tuple(attachments))


class Ingestion:
    def __init__(self, store, candidate_database: Path):
        self.store = store
        self.path = Path(candidate_database).absolute()
        if self.path.resolve() == Path(store.path).resolve():
            raise IngestionError("candidate_database_required")
        self.ledger = raw.RawLedger(store)
        self.captures = Captures(store)

    @contextmanager
    def _db(self):
        # Parent must already be an explicitly supplied isolated directory.
        if not self.path.parent.is_dir() or self.path.is_symlink():
            raise IngestionError("candidate_path_invalid")
        for parent in self.path.parents:
            if parent.is_symlink():
                raise IngestionError("candidate_path_invalid")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise IngestionError("candidate_path_invalid")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise IngestionError("candidate_permissions_invalid")
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=FULL")
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"ingest_events", "ingest_retained"}:
                raise IngestionError("candidate_database_required")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ingest_events(
                    event_key TEXT PRIMARY KEY, subject TEXT NOT NULL,
                    kind TEXT NOT NULL, binding TEXT NOT NULL, detail TEXT NOT NULL,
                    occurred_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ingest_retained(
                    binding TEXT NOT NULL, relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, content BLOB NOT NULL,
                    PRIMARY KEY(binding,relative_path));
                CREATE TRIGGER IF NOT EXISTS ingest_events_no_update BEFORE UPDATE ON ingest_events
                    BEGIN SELECT RAISE(ABORT,'immutable event'); END;
                CREATE TRIGGER IF NOT EXISTS ingest_events_no_delete BEFORE DELETE ON ingest_events
                    BEGIN SELECT RAISE(ABORT,'immutable event'); END;
                CREATE TRIGGER IF NOT EXISTS ingest_retained_no_update BEFORE UPDATE ON ingest_retained
                    BEGIN SELECT RAISE(ABORT,'immutable bytes'); END;
                CREATE TRIGGER IF NOT EXISTS ingest_retained_no_delete BEFORE DELETE ON ingest_retained
                    BEGIN SELECT RAISE(ABORT,'immutable bytes'); END;
            """)

    def _event(self, subject, kind, binding, detail):
        detail = encoded({"contract": CONTRACT, **detail})
        key = digest(encoded([subject, kind, binding, detail]).encode())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO ingest_events VALUES(?,?,?,?,?,?)",
                       (key, subject, kind, binding, detail, datetime.now(UTC).isoformat()))

    def events(self, subject: str):
        with self._db() as db:
            return db.execute("SELECT kind,binding,detail FROM ingest_events WHERE subject=? ORDER BY rowid",
                              (subject,)).fetchall()

    def _retain(self, record):
        binding = digest(encoded([record["raw_id"], record["content_sha256"]]).encode())
        parts = [(record["relative_path"], record["content"].encode("utf-8"))]
        for attachment in json.loads(record["attachments_json"]):
            parts.append((f"附件/raw/{record['raw_id']}/{attachment['filename']}",
                          raw._attachment_bytes(self.store, record, attachment)))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for relative, content in parts:
                old = db.execute("SELECT content FROM ingest_retained WHERE binding=? AND relative_path=?",
                                 (binding, relative)).fetchone()
                if old is not None and old[0] != content:
                    raise IngestionError("retained_bytes_conflict")
                db.execute("INSERT OR IGNORE INTO ingest_retained VALUES(?,?,?,?)",
                           (binding, relative, digest(content), content))
        return binding

    def _complete(self, record, vault, source_version):
        subject = f"{record['subject_kind']}:{record['subject_id']}"
        binding = self._retain(record)  # durable bytes before any filesystem placement
        self._event(subject, "raw_pending", binding, {"source_version": source_version})
        result = self.ledger.write(record, vault)
        if result not in {"placed", "already"}:
            code = result if result in {"vault_unavailable", "raw_id_collision", "raw_attachment_conflict",
                                        "raw_target_conflict", "raw_write_failed", "raw_attachment_unavailable"} else "raw_write_failed"
            self._event(subject, "raw_pending", binding, {"error_code": code})
            raise IngestionError(code)
        try:
            receipt = verify_record(self.ledger, self.ledger.record(record["raw_id"]),
                                    vault, source_version=source_version)
        except (IngestionError, raw.RawError) as error:
            allowed = {"path_unsafe", "readback_changed", "readback_unavailable", "binding_missing",
                       "ledger_hash_mismatch", "raw_bytes_mismatch", "raw_envelope_invalid",
                       "raw_envelope_mismatch", "raw_subject_mismatch", "attachment_bytes_mismatch"}
            code = error.args[0] if isinstance(error, IngestionError) and error.args and isinstance(error.args[0], str) and error.args[0] in allowed else "raw_readback_failed"
            self._event(subject, "raw_pending", binding, {"error_code": code})
            raise IngestionError(code) from None
        self._event(subject, "raw_verified", binding, asdict(receipt))
        return receipt

    def material(self, material_id: int, vault: Path, *, item_id: int | None = None):
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
            if item is None or item["material_id"] != material_id or item["confirmation_json"]:
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
        if expected_adjacency is not None and hints.get("adjacency", []) != expected_adjacency:
            raise IngestionError("capture_adjacency_pending")
        record = self.ledger.ensure_material(material_id, **hints)
        fields = envelope_fields(record["content"].encode())
        if expected_adjacency is not None and (fields.get("邻接", []) != expected_adjacency or fields.get("邻接未定")):
            raise IngestionError("capture_adjacency_mismatch")
        application = fields.get("应用记录")
        if not isinstance(application, dict) or application.get("source_fact_id") != row["source_fact_id"]:
            raise IngestionError("source_fact_binding_invalid")
        version = encoded({"source_fact_id": row["source_fact_id"],
                           "snapshot_key": row["snapshot_key"],
                           "native_version": json.loads(row["metadata_json"]).get("native_content_version")})
        return self._complete(record, Path(vault), version)

    def _message_raw(self, app_id, message_id, vault, *, _leaf=False):
        """Read-only completeness guard before the existing single-ID projection.

        A failed/dismissed item is not evidence that its source was retained.
        Check every effective part and every current head, never ids[0].
        """
        with connect(self.store.path) as db:
            capture = db.execute("""SELECT c.*,s.item_id FROM captures c
                JOIN capture_state s USING(capture_id) WHERE c.app_id=? AND c.message_id=?""",
                                 (app_id, message_id)).fetchone()
            parts = db.execute("SELECT item_id,error FROM feishu_parts WHERE app_id=? AND message_id=? ORDER BY position",
                               (app_id, message_id)).fetchall()
            receipt = db.execute("SELECT state FROM feishu_receipts WHERE app_id=? AND message_id=?",
                                 (app_id, message_id)).fetchone()
            if receipt is not None and receipt["state"] != "accepted":
                raise IngestionError("message_raw_pending")
            if any(p["item_id"] is None and not p["error"] for p in parts):
                raise IngestionError("message_raw_pending")
            item_ids = {p["item_id"] for p in parts if p["item_id"] is not None}
            if capture is not None and capture["item_id"] is not None:
                item_ids.add(capture["item_id"])
            records = []
            if capture is not None:
                # Latest identity is authoritative; a reserved capture ID is not raw.
                decision = db.execute("SELECT * FROM capture_identity_events WHERE capture_id=? ORDER BY event_id DESC LIMIT 1",
                                      (capture["capture_id"],)).fetchone()
                if decision is None or decision["result"] not in {"my_thought", "annotation", "third_party"}:
                    raise IngestionError("message_raw_pending")
                records.extend(db.execute("""SELECT r.* FROM raw_records r WHERE subject_kind='capture' AND subject_id=?
                    AND NOT EXISTS(SELECT 1 FROM raw_records n WHERE n.supersedes=r.raw_id)""",
                                          (capture["capture_id"],)).fetchall())
                # A third-party correction cannot reuse an old personal head,
                # even if its material_id still matches the attached source.
                if decision["result"] == "third_party" and records:
                    raise IngestionError("message_raw_pending")
                if decision["result"] in {"my_thought", "annotation"}:
                    expected = "本人" if decision["result"] == "my_thought" else "本人附言"
                    if not records or any(r["identity"] != expected for r in records):
                        raise IngestionError("message_raw_pending")
                    relations = db.execute("SELECT 1 FROM delivery_adjacency WHERE app_id=? AND message_id=?",
                                           (app_id, message_id)).fetchone()
                    # One dependency level only: nested annotations/adjacency
                    # remain pending rather than building a recursive resolver.
                    if _leaf and (relations or decision["result"] == "annotation"):
                        raise IngestionError("message_raw_pending")
                    try:
                        adjacency = self._relations_ready(capture, vault, _leaf_targets=True) if relations else []
                        target = (self._message_raw(app_id, decision["target_message_id"], vault, _leaf=True)
                                  if decision["result"] == "annotation" else None)
                        if capture["message_type"] == "audio":
                            item = self.store.item_bundle(capture["item_id"]) if capture["item_id"] else None
                            if (item is None or item["source_fact_id"] is None or item["confirmation_json"]
                                    or item["state"] == "failed" or item["dismissed_at"] is not None):
                                raise IngestionError("message_raw_pending")
                        for record in records:
                            fields = envelope_fields(record["content"].encode())
                            document = self.captures.render(capture, decision, adjacency, 0, target,
                                                           raw_id=record["raw_id"], supersedes=record["supersedes"])
                            expected_fields = envelope_fields(document.content.encode())
                            # Collection time/app build can differ after a
                            # legitimate supersession; source/decision cannot.
                            keys = ("编号", "格式版本", "身份", "标题", "作者", "渠道", "产生于",
                                    "订正", "存疑", "身份判定", "邻接", "邻接未定", "附言对象", "取代", "应用记录")
                            if (any(fields.get(k) != expected_fields.get(k) for k in keys)
                                    or fields.get("取得方式", {}).get("识别") != expected_fields.get("取得方式", {}).get("识别")
                                    or record["content"].split("\n---\n", 1)[1] != document.content.split("\n---\n", 1)[1]):
                                raise IngestionError("message_raw_pending")
                    except (IngestionError, raw.RawError, KeyError, TypeError, AttributeError, IndexError):
                        raise IngestionError("message_raw_pending") from None
                if not item_ids and decision["result"] == "third_party":
                    raise IngestionError("message_raw_pending")
            for item_id in sorted(item_ids):
                item = db.execute("""SELECT i.material_id,i.state,i.dismissed_at,i.confirmation_json,sf.source_fact_id
                    FROM distill_items i LEFT JOIN source_facts sf ON sf.material_id=i.material_id WHERE i.item_id=?""",
                                  (item_id,)).fetchone()
                if (item is None or item["source_fact_id"] is None or item["confirmation_json"]
                        or item["state"] == "failed" or item["dismissed_at"] is not None):
                    raise IngestionError("message_raw_pending")
                heads = db.execute("""SELECT r.* FROM raw_records r WHERE subject_kind='material' AND subject_id=?
                    AND NOT EXISTS(SELECT 1 FROM raw_records n WHERE n.supersedes=r.raw_id)""",
                                   (item["material_id"],)).fetchall()
                # Voice keeps one capture raw, not an extra third-party raw.
                own = []
                for r in records:
                    application = envelope_fields(r["content"].encode()).get("应用记录")
                    if r["subject_kind"] == "capture" and isinstance(application, dict) and application.get("material_id") == item["material_id"]:
                        own.append(r)
                if not heads and not own:
                    raise IngestionError("message_raw_pending")
                records.extend(heads)
            unique = {r["raw_id"]: r for r in records}
            if not unique:
                raise IngestionError("message_raw_pending")
            # Read all records before deciding a single-ID contract is ambiguous.
            for record in unique.values():
                verify_record(self.ledger, record, Path(vault), source_version="message-readback")
            if len(unique) != 1:
                raise IngestionError("message_raw_ambiguous")
            chosen = next(iter(unique))
            if db.execute("SELECT 1 FROM raw_records WHERE supersedes=?", (chosen,)).fetchone():
                raise IngestionError("message_raw_pending")
            return chosen

    def _relations_ready(self, capture, vault, *, _leaf_targets=False):
        with connect(self.store.path) as db:
            relations = db.execute("SELECT earlier_message_id,gap_seconds FROM delivery_adjacency WHERE app_id=? AND message_id=? ORDER BY gap_seconds DESC,earlier_message_id",
                                   (capture["app_id"], capture["message_id"])).fetchall()
        adjacency = []
        for relation in relations:
            try:
                raw_id = self._message_raw(capture["app_id"], relation["earlier_message_id"], vault,
                                           _leaf=_leaf_targets)
            except IngestionError as error:
                code = "capture_adjacency_ambiguous" if error.args == ("message_raw_ambiguous",) else "capture_adjacency_pending"
                raise IngestionError(code) from error
            adjacency.append({"编号": raw_id, "间隔秒": relation["gap_seconds"]})
        projected, unsettled = self.captures.adjacency(capture["app_id"], capture["message_id"])
        if unsettled or projected != adjacency:
            raise IngestionError("capture_adjacency_pending")
        return adjacency

    def capture(self, capture_id: int, vault: Path):
        capture = self.captures.get(capture_id)
        decision = self.captures.identity(capture_id) if capture else None
        if decision is None or decision["result"] not in {"my_thought", "annotation"}:
            raise IngestionError("capture_identity_pending")
        adjacency = self._relations_ready(capture, vault)
        target = None
        source_fact_id = None
        if decision["result"] == "annotation":
            try:
                target = self._message_raw(capture["app_id"], decision["target_message_id"], vault)
            except IngestionError as error:
                code = "capture_target_ambiguous" if error.args == ("message_raw_ambiguous",) else "capture_target_pending"
                raise IngestionError(code) from error
        if capture["message_type"] == "audio":
            item = self.store.item_bundle(capture["item_id"]) if capture["item_id"] else None
            if item is None or item["source_fact_id"] is None or item["confirmation_json"]:
                raise IngestionError("capture_source_pending")
            source_fact_id = item["source_fact_id"]
        record = self.ledger.current("capture", capture_id)
        if record is None:
            # Current Captures.ready requires succeeded first. Do not copy its
            # render/allocation business or invoke write_ready (releases audio).
            raise IngestionError("capture_precise_writer_required")
        expected = "本人附言" if decision["result"] == "annotation" else "本人"
        fields = envelope_fields(record["content"].encode())
        if record["identity"] != expected or (expected == "本人附言" and fields.get("附言对象") != target):
            raise IngestionError("capture_identity_mismatch")
        if fields.get("邻接", []) != adjacency or fields.get("邻接未定"):
            raise IngestionError("capture_adjacency_mismatch")
        expected_document = self.captures.render(capture, decision, adjacency, 0, target,
                                                raw_id=record["raw_id"])
        actual_body = record["content"].split("\n---\n", 1)[1]
        expected_body = expected_document.content.split("\n---\n", 1)[1]
        if actual_body != expected_body:
            raise IngestionError("capture_source_mismatch")
        application = fields.get("应用记录")
        if capture["message_type"] == "audio" and (not isinstance(application, dict)
                                                  or application.get("material_id") != item["material_id"]):
            raise IngestionError("capture_source_mismatch")
        version = encoded({"identity_event": dict(decision), "source_fact_id": source_fact_id,
                           "body_sha256": digest(expected_body.encode())})
        return self._complete(record, Path(vault), version)
