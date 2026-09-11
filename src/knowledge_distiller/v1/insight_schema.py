"""V1 additions: reconsideration grants and append-only personal cognition."""
RECONSIDERATION = '''CREATE TABLE insight_reconsiderations (
    reconsideration_id INTEGER PRIMARY KEY,
    insight_version_id INTEGER NOT NULL UNIQUE REFERENCES insight_versions(insight_version_id),
    judgment_id INTEGER NOT NULL UNIQUE REFERENCES user_insight_judgments(judgment_id),
    operation_id TEXT NOT NULL UNIQUE CHECK(TRIM(operation_id) != ''),
    decision TEXT NOT NULL CHECK(decision='interesting_after_rethink'),
    reconsidered_at TEXT NOT NULL,
    UNIQUE(insight_version_id,judgment_id,reconsideration_id)
)'''
COGNITION = '''CREATE TABLE personal_cognition_entries (
    entry_id INTEGER PRIMARY KEY,
    insight_version_id INTEGER NOT NULL REFERENCES insight_versions(insight_version_id),
    judgment_id INTEGER NOT NULL REFERENCES user_insight_judgments(judgment_id),
    reconsideration_id INTEGER REFERENCES insight_reconsiderations(reconsideration_id),
    entry_kind TEXT NOT NULL CHECK(entry_kind IN ('new_idea','new_view')),
    text TEXT NOT NULL CHECK(TRIM(text) != ''),
    operation_id TEXT NOT NULL UNIQUE CHECK(TRIM(operation_id) != ''),
    created_at TEXT NOT NULL,
    CHECK(entry_kind != 'new_view' OR reconsideration_id IS NOT NULL)
)'''


def extend_accepted_table(statement):
    if 'CREATE TABLE accepted_insight_versions (' not in statement:
        return statement.replace("AND j.decision = 'interesting'", "AND (j.decision = 'interesting' OR EXISTS (SELECT 1 FROM insight_reconsiderations r WHERE r.judgment_id=j.judgment_id AND r.insight_version_id=j.insight_version_id))")
    return statement.replace("judgment_decision TEXT NOT NULL CHECK (judgment_decision = 'interesting'),", """judgment_decision TEXT NOT NULL CHECK (judgment_decision IN ('interesting','rethink')),
    reconsideration_id INTEGER,""").replace('    UNIQUE (insight_version_id, judgment_id),', '''    CHECK ((judgment_decision='interesting' AND reconsideration_id IS NULL)
        OR (judgment_decision='rethink' AND reconsideration_id IS NOT NULL)),
    FOREIGN KEY(insight_version_id,judgment_id,reconsideration_id)
        REFERENCES insight_reconsiderations(insight_version_id,judgment_id,reconsideration_id),
    UNIQUE (insight_version_id, judgment_id),''')


def statements():
    yield RECONSIDERATION
    yield COGNITION
    for table in ('insight_reconsiderations','personal_cognition_entries'):
        for action in ('UPDATE','DELETE'):
            yield f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'personal cognition is append-only'); END"
    yield """CREATE TRIGGER accepted_grant_no_change BEFORE UPDATE OF reconsideration_id ON accepted_insight_versions
        BEGIN SELECT RAISE(ABORT,'Accepted grant is immutable'); END"""
