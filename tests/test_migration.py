import re

from fastapi.testclient import TestClient

from app import db
from app.crypto import lookup_hash, new_token, public_key_for, seal_for
from app.main import create_app

# Schema from the single-event version, kept here so the migration stays testable.
LEGACY_SCHEMA = """
CREATE TABLE event (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    code TEXT NOT NULL,
    drawn INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE participant (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    token_hash TEXT UNIQUE,
    public_key BLOB,
    claimed_at TEXT,
    revealed_at TEXT
);
CREATE TABLE assignment (
    giver_id INTEGER PRIMARY KEY REFERENCES participant(id) ON DELETE CASCADE,
    ciphertext BLOB NOT NULL
);
"""


def build_legacy_db(path):
    """Create a drawn single-event database the way the old version did."""
    conn = db.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO event (id, code, drawn) VALUES (1, 'oldcode', 1)")

    names = ["Ann", "Ben", "Cy"]
    tokens = {}
    for name in names:
        token = new_token()
        tokens[name] = token
        conn.execute(
            "INSERT INTO participant (name, token_hash, public_key, claimed_at) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00')",
            (name, lookup_hash(token), public_key_for(token)),
        )

    ids = {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM participant")}
    # Ann -> Ben -> Cy -> Ann
    cycle = {"Ann": "Ben", "Ben": "Cy", "Cy": "Ann"}
    for giver, receiver in cycle.items():
        token = tokens[giver]
        conn.execute(
            "INSERT INTO assignment (giver_id, ciphertext) VALUES (?, ?)",
            (ids[giver], seal_for(public_key_for(token), str(ids[receiver]))),
        )
    conn.close()
    return tokens, cycle


def test_legacy_database_migrates_and_keeps_links_working(tmp_path):
    path = str(tmp_path / "legacy.db")
    tokens, cycle = build_legacy_db(path)

    app = create_app(db_path=path, admin_password="pw", secret_key="k")
    client = TestClient(app, follow_redirects=False)

    conn = db.connect(path)
    event = conn.execute("SELECT id, name, code, drawn FROM event").fetchone()
    assert (event["id"], event["name"], event["code"], event["drawn"]) == (
        1,
        "Secret Santa",
        "oldcode",
        1,
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM participant").fetchone()["n"] == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM assignment").fetchone()["n"] == 3
    conn.close()

    # Old links still reveal the correct match.
    for giver, receiver in cycle.items():
        resp = client.get(f"/r/{tokens[giver]}")
        assert resp.status_code == 200
        assert re.search(rf"<strong>{receiver}</strong>", resp.text), resp.text


def test_init_is_idempotent_after_migration(tmp_path):
    path = str(tmp_path / "legacy.db")
    build_legacy_db(path)
    db.init_db(path)
    db.init_db(path)  # second run must not touch the migrated data

    conn = db.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM event").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM participant").fetchone()["n"] == 3
    finally:
        conn.close()
