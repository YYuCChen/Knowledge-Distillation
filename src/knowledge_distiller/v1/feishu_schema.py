"""A bound private conversation, durable receipts, and their existing tasks."""

STATEMENTS = (
    """CREATE TABLE collection_previews (
        token TEXT PRIMARY KEY, preview_json TEXT NOT NULL,
        expires_at REAL
    )""",
    """CREATE TABLE feishu_binding (
        app_id TEXT PRIMARY KEY, bot_open_id TEXT NOT NULL,
        user_open_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
        history_until_ms INTEGER NOT NULL CHECK(history_until_ms >= start_ms)
    )""",
    """CREATE TABLE feishu_receipts (
        app_id TEXT NOT NULL REFERENCES feishu_binding(app_id),
        message_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
        raw_json TEXT NOT NULL, text TEXT NOT NULL,
        same_topic INTEGER NOT NULL CHECK(same_topic IN (0,1)),
        content_kind TEXT CHECK(content_kind IN ('text','links')),
        state TEXT NOT NULL CHECK(state IN ('received','waiting_input','accepted','rejected','needs_desktop')),
        error TEXT, card_id TEXT, card_signature TEXT, card_attempted_ms INTEGER,
        PRIMARY KEY(app_id,message_id)
    )""",
    """CREATE TABLE feishu_parts (
        app_id TEXT NOT NULL, message_id TEXT NOT NULL, position INTEGER NOT NULL,
        item_id INTEGER REFERENCES distill_items(item_id),
        error TEXT, preview_json TEXT,
        PRIMARY KEY(app_id,message_id,position),
        FOREIGN KEY(app_id,message_id) REFERENCES feishu_receipts(app_id,message_id)
    )""",
)
