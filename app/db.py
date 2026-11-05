import secrets
import sqlite3
from datetime import datetime, timezone

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        code TEXT NOT NULL UNIQUE,
        drawn INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        creator_token_hash TEXT,
        signup_mode TEXT NOT NULL DEFAULT 'closed' CHECK (signup_mode IN ('closed', 'open'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS participant (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
        name TEXT NOT NULL COLLATE NOCASE,
        token_hash TEXT UNIQUE,
        public_key BLOB,
        claimed_at TEXT,
        revealed_at TEXT,
        UNIQUE (event_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS exclusion (
        event_id INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
        person_a INTEGER NOT NULL REFERENCES participant(id) ON DELETE CASCADE,
        person_b INTEGER NOT NULL REFERENCES participant(id) ON DELETE CASCADE,
        PRIMARY KEY (event_id, person_a, person_b),
        CHECK (person_a < person_b)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assignment (
        giver_id INTEGER PRIMARY KEY REFERENCES participant(id) ON DELETE CASCADE,
        ciphertext BLOB NOT NULL
    )
    """,
]

# Created after _ensure_column, because existing databases get the column added late.
INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS event_creator_hash ON event(creator_token_hash)",
]


def connect(path: str) -> sqlite3.Connection:
    # Autocommit mode; transactions are opened explicitly where needed.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def new_event_code() -> str:
    return secrets.token_urlsafe(8)


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        if _is_single_event_schema(conn):
            _migrate_single_event(conn)
        for statement in SCHEMA:
            conn.execute(statement)
        # Multi-event databases created before creator links lack this column.
        _ensure_column(conn, "event", "creator_token_hash", "TEXT")
        _ensure_column(
            conn,
            "event",
            "signup_mode",
            "TEXT NOT NULL DEFAULT 'closed' CHECK (signup_mode IN ('closed', 'open'))",
        )
        for statement in INDEXES:
            conn.execute(statement)
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _is_single_event_schema(conn: sqlite3.Connection) -> bool:
    # The first version had one event row with no name column.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(event)")}
    return bool(columns) and "name" not in columns


def _migrate_single_event(conn: sqlite3.Connection) -> None:
    """Move the old single event into the multi-event schema, keeping ids and ciphertext
    so existing links keep working. Everything happens in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        old_event = conn.execute("SELECT code, drawn FROM event WHERE id = 1").fetchone()
        old_people = conn.execute(
            "SELECT id, name, token_hash, public_key, claimed_at, revealed_at FROM participant"
        ).fetchall()
        old_assignments = conn.execute(
            "SELECT giver_id, ciphertext FROM assignment"
        ).fetchall()

        for table in ("assignment", "participant", "event"):
            conn.execute(f"DROP TABLE {table}")
        for statement in SCHEMA:
            conn.execute(statement)

        if old_event is not None:
            conn.execute(
                "INSERT INTO event (id, name, code, drawn, created_at) VALUES (1, ?, ?, ?, ?)",
                (
                    "Secret Santa",
                    old_event["code"],
                    old_event["drawn"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            for person in old_people:
                conn.execute(
                    "INSERT INTO participant (id, event_id, name, token_hash, public_key, "
                    "claimed_at, revealed_at) VALUES (?, 1, ?, ?, ?, ?, ?)",
                    (
                        person["id"],
                        person["name"],
                        person["token_hash"],
                        person["public_key"],
                        person["claimed_at"],
                        person["revealed_at"],
                    ),
                )
            for row in old_assignments:
                conn.execute(
                    "INSERT INTO assignment (giver_id, ciphertext) VALUES (?, ?)",
                    (row["giver_id"], row["ciphertext"]),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
