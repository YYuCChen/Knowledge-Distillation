"""Schema 21: Feishu quick-note captures (docs/roadmap/handoff-feishu-capture.md).

Captures, transcripts, adjacency and identity events are append-only records:
triggers refuse updates and deletes. Only ``capture_state`` (workflow links and
the audio-release marker) changes.
"""


def _immutable(table):
    return (f"""CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} BEGIN
                SELECT RAISE(ABORT, '{table} is immutable'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} BEGIN
                SELECT RAISE(ABORT, '{table} is immutable'); END""")


STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS captures (
        capture_id INTEGER PRIMARY KEY,
        app_id TEXT NOT NULL, message_id TEXT NOT NULL,
        message_type TEXT NOT NULL CHECK (message_type IN ('text', 'audio')),
        created_ms INTEGER NOT NULL CHECK (created_ms >= 0),
        received_ms INTEGER NOT NULL CHECK (received_ms >= 0),
        text TEXT, file_key TEXT, duration_ms INTEGER,
        raw_id TEXT NOT NULL UNIQUE,
        CHECK ((message_type = 'text' AND text IS NOT NULL AND file_key IS NULL)
            OR (message_type = 'audio' AND text IS NULL AND file_key IS NOT NULL)),
        UNIQUE (app_id, message_id)
    )""",
    *_immutable('captures'),
    """CREATE TABLE IF NOT EXISTS capture_transcripts (
        capture_id INTEGER PRIMARY KEY REFERENCES captures(capture_id),
        text TEXT NOT NULL, engine TEXT NOT NULL, model TEXT NOT NULL, version TEXT NOT NULL,
        chunks_json TEXT NOT NULL, created_at TEXT NOT NULL
    )""",
    *_immutable('capture_transcripts'),
    """CREATE TABLE IF NOT EXISTS delivery_adjacency (
        app_id TEXT NOT NULL, message_id TEXT NOT NULL, earlier_message_id TEXT NOT NULL,
        gap_seconds INTEGER NOT NULL CHECK (gap_seconds >= 0),
        PRIMARY KEY (app_id, message_id, earlier_message_id)
    )""",
    *_immutable('delivery_adjacency'),
    """CREATE TABLE IF NOT EXISTS capture_identity_events (
        event_id INTEGER PRIMARY KEY,
        capture_id INTEGER NOT NULL REFERENCES captures(capture_id),
        result TEXT NOT NULL CHECK (result IN ('my_thought', 'third_party', 'annotation', 'pending')),
        basis TEXT NOT NULL, confidence REAL, target_message_id TEXT,
        created_at TEXT NOT NULL
    )""",
    *_immutable('capture_identity_events'),
    """CREATE TABLE IF NOT EXISTS capture_state (
        capture_id INTEGER PRIMARY KEY REFERENCES captures(capture_id),
        item_id INTEGER REFERENCES distill_items(item_id),
        audio_path TEXT, audio_released_at TEXT
    )""",
)
