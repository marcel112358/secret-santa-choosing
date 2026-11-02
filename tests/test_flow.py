import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app

ADMIN_PASSWORD = "test-admin-pw"
NAMES = ["Ann", "Ben", "Cy", "Dee"]
LINK_RE = re.compile(r"/r/([A-Za-z0-9_-]+)")
ID_RE = re.compile(r"/admin/events/(\d+)")


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "santa.db")


@pytest.fixture
def client(db_path):
    app = create_app(db_path=db_path, admin_password=ADMIN_PASSWORD, secret_key="test")
    return TestClient(app, follow_redirects=False)


def query(db_path, sql, params=()):
    conn = db.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def event_code(db_path, event_id):
    return query(db_path, "SELECT code FROM event WHERE id = ?", (event_id,))[0]["code"]


def admin_login(client):
    resp = client.post("/admin/login", data={"password": ADMIN_PASSWORD})
    assert resp.status_code == 303


def create_event(client, name="Office Christmas"):
    resp = client.post("/admin/events", data={"name": name})
    assert resp.status_code == 303, resp.text
    return int(ID_RE.search(resp.headers["location"]).group(1))


def add_names(client, event_id, names):
    for name in names:
        resp = client.post(f"/admin/events/{event_id}/participants", data={"name": name})
        assert resp.status_code == 303, resp.text


def claim(client, db_path, event_id, name):
    resp = client.post(
        "/claim", data={"code": event_code(db_path, event_id), "name": name}
    )
    assert resp.status_code == 200, resp.text
    match = LINK_RE.search(resp.text)
    assert match, "claim page should show a link"
    return match.group(1)


def draw(client, event_id):
    return client.post(f"/admin/events/{event_id}/draw")


def setup_full_event(client, db_path, name="Office Christmas", names=NAMES):
    event_id = create_event(client, name)
    add_names(client, event_id, names)
    tokens = {n: claim(client, db_path, event_id, n) for n in names}
    return event_id, tokens


def test_admin_pages_require_login(client):
    assert client.get("/admin").headers["location"] == "/admin/login"
    assert client.get("/admin/events/1").headers["location"] == "/admin/login"
    assert client.post("/admin/events", data={"name": "x"}).status_code == 303


def test_wrong_admin_password(client):
    resp = client.post("/admin/login", data={"password": "nope"})
    assert resp.status_code == 401


def test_create_event_requires_name(client):
    admin_login(client)
    resp = client.post("/admin/events", data={"name": "   "})
    assert resp.status_code == 400
    assert "1–100 characters" in resp.text


def test_admin_home_lists_events(client):
    admin_login(client)
    create_event(client, "Family Gift Swap")
    create_event(client, "Work Party")
    html = client.get("/admin").text
    assert "Family Gift Swap" in html
    assert "Work Party" in html


def test_invite_link_is_on_event_page(client, db_path):
    admin_login(client)
    event_id = create_event(client)
    html = client.get(f"/admin/events/{event_id}").text
    assert f"/join/{event_code(db_path, event_id)}" in html


def test_join_page_shows_event_name_and_form(client, db_path):
    admin_login(client)
    event_id = create_event(client, "Office Christmas")
    code = event_code(db_path, event_id)

    resp = client.get(f"/join/{code}")
    assert resp.status_code == 200
    assert "Office Christmas" in resp.text
    assert f'value="{code}"' in resp.text  # code is embedded, not typed


def test_join_unknown_code_is_404(client):
    assert client.get("/join/not-a-code").status_code == 404


def test_join_after_draw_says_closed(client, db_path):
    admin_login(client)
    event_id, _ = setup_full_event(client, db_path)
    draw(client, event_id)
    resp = client.get(f"/join/{event_code(db_path, event_id)}")
    assert "already happened" in resp.text
    assert "<form" not in resp.text


def test_full_flow(client, db_path):
    admin_login(client)
    event_id, tokens = setup_full_event(client, db_path)

    assert draw(client, event_id).status_code == 303

    receivers = {}
    for name, token in tokens.items():
        resp = client.get(f"/r/{token}")
        assert resp.status_code == 200
        match = re.search(r"<strong>(\w+)</strong>", resp.text)
        assert match, resp.text
        assert "Office Christmas" in resp.text
        receivers[name] = match.group(1)

    assert set(receivers.values()) == set(NAMES)  # everyone is someone's match
    assert all(receivers[n] != n for n in NAMES)  # nobody draws themselves


def test_events_are_independent(client, db_path):
    admin_login(client)
    first, first_tokens = setup_full_event(client, db_path, "Family")
    second, second_tokens = setup_full_event(client, db_path, "Work")

    # Same names in both events, each with its own link.
    assert first_tokens.keys() == second_tokens.keys()
    assert set(first_tokens.values()).isdisjoint(second_tokens.values())

    draw(client, first)
    # Drawing one event doesn't touch the other.
    assert "<strong>" in client.get(f"/r/{first_tokens['Ann']}").text
    assert "The draw hasn't happened yet" in client.get(f"/r/{second_tokens['Ann']}").text
    assert query(db_path, "SELECT drawn FROM event WHERE id = ?", (second,))[0]["drawn"] == 0


def test_claim_code_is_scoped_to_its_event(client, db_path):
    admin_login(client)
    first = create_event(client, "Family")
    second = create_event(client, "Work")
    add_names(client, first, ["Ann", "Ben", "Cy"])
    add_names(client, second, ["Zoe", "Yan", "Xi"])

    # "Zoe" exists in the second event only, so the first event's code must reject her.
    resp = client.post(
        "/claim", data={"code": event_code(db_path, first), "name": "Zoe"}
    )
    assert resp.status_code == 400
    assert "on the list" in resp.text  # apostrophe is HTML-escaped in the page


def test_same_name_can_exist_in_two_events(client, db_path):
    admin_login(client)
    first = create_event(client, "Family")
    second = create_event(client, "Work")
    add_names(client, first, ["Ann"])
    add_names(client, second, ["Ann"])
    assert len(query(db_path, "SELECT id FROM participant WHERE name = 'Ann'")) == 2


def test_duplicate_name_in_same_event_rejected(client, db_path):
    admin_login(client)
    event_id = create_event(client)
    add_names(client, event_id, ["Ann"])
    resp = client.post(f"/admin/events/{event_id}/participants", data={"name": "ann"})
    assert resp.status_code == 400
    assert "already on this list" in resp.text


def test_draw_blocked_until_everyone_claims(client, db_path):
    admin_login(client)
    event_id = create_event(client)
    add_names(client, event_id, NAMES)
    claim(client, db_path, event_id, "Ann")

    resp = draw(client, event_id)
    assert resp.status_code == 400
    assert "Everyone must claim" in resp.text


def test_draw_needs_three_people(client, db_path):
    admin_login(client)
    event_id = create_event(client)
    add_names(client, event_id, ["Ann", "Ben"])
    claim(client, db_path, event_id, "Ann")
    claim(client, db_path, event_id, "Ben")

    resp = draw(client, event_id)
    assert resp.status_code == 400
    assert "at least 3" in resp.text


def test_claim_rules(client, db_path):
    admin_login(client)
    event_id = create_event(client)
    add_names(client, event_id, NAMES)

    assert client.post("/claim", data={"code": "wrong", "name": "Ann"}).status_code == 400
    assert client.post(
        "/claim", data={"code": event_code(db_path, event_id), "name": "Zed"}
    ).status_code == 400

    claim(client, db_path, event_id, "Ann")
    resp = client.post(
        "/claim", data={"code": event_code(db_path, event_id), "name": "Ann"}
    )
    assert resp.status_code == 400
    assert "already claimed" in resp.text


def test_unknown_token_is_404(client):
    assert client.get("/r/not-a-real-token").status_code == 404


def test_no_pairing_is_visible_in_db_or_admin(client, db_path):
    admin_login(client)
    event_id, _ = setup_full_event(client, db_path)
    draw(client, event_id)

    blobs = [row["ciphertext"] for row in query(db_path, "SELECT ciphertext FROM assignment")]
    assert len(blobs) == len(NAMES)
    for blob in blobs:
        assert not any(name.encode() in blob for name in NAMES)

    admin_html = client.get(f"/admin/events/{event_id}").text
    assert "Match opened" in admin_html
    assert "You are the Secret Santa" not in admin_html


def test_opening_match_records_only_a_timestamp(client, db_path):
    admin_login(client)
    event_id, tokens = setup_full_event(client, db_path)
    draw(client, event_id)

    client.get(f"/r/{tokens['Ann']}")
    ann = query(
        db_path, "SELECT revealed_at, public_key FROM participant WHERE name = 'Ann'"
    )[0]
    assert ann["revealed_at"] is not None
    assert len(ann["public_key"]) == 32  # public key only, no token stored


def test_draw_is_idempotent(client, db_path):
    admin_login(client)
    event_id, _ = setup_full_event(client, db_path)

    draw(client, event_id)
    resp = draw(client, event_id)
    assert resp.status_code == 400
    assert "already happened" in resp.text

    count = query(db_path, "SELECT COUNT(*) AS n FROM assignment")[0]["n"]
    assert count == len(NAMES)


def test_reset_only_affects_its_event(client, db_path):
    admin_login(client)
    first, first_tokens = setup_full_event(client, db_path, "Family")
    second, second_tokens = setup_full_event(client, db_path, "Work")
    draw(client, first)
    draw(client, second)

    client.post(f"/admin/events/{first}/reset")

    assert client.get(f"/r/{first_tokens['Ann']}").status_code == 404
    assert "<strong>" in client.get(f"/r/{second_tokens['Ann']}").text
    assert query(db_path, "SELECT drawn FROM event WHERE id = ?", (second,))[0]["drawn"] == 1
    admin_home = client.get("/admin").text
    assert "0 / 4" in admin_home  # the reset event has no claims left
    assert "4 / 4" in admin_home  # the other event keeps its claims


def test_claim_closed_after_draw(client, db_path):
    admin_login(client)
    event_id, _ = setup_full_event(client, db_path)
    draw(client, event_id)

    resp = client.post(
        "/claim", data={"code": event_code(db_path, event_id), "name": "Ann"}
    )
    assert resp.status_code == 400
    assert "already happened" in resp.text


def test_unknown_event_is_404(client):
    admin_login(client)
    assert client.get("/admin/events/999").status_code == 404
    assert client.post("/admin/events/999/draw").status_code == 404
