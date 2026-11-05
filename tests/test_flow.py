import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.crypto import lookup_hash
from app.limits import RateLimiter
from app.main import create_app

ADMIN_PASSWORD = "test-admin-pw"
NAMES = ["Ann", "Ben", "Cy", "Dee"]
LINK_RE = re.compile(r"/r/([A-Za-z0-9_-]+)")
MANAGE_RE = re.compile(r"/manage/([A-Za-z0-9_-]+)")


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "santa.db")


@pytest.fixture
def app(db_path):
    return create_app(db_path=db_path, admin_password=ADMIN_PASSWORD, secret_key="test")


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


def query(db_path, sql, params=()):
    conn = db.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def event_code(db_path, event_id):
    return query(db_path, "SELECT code FROM event WHERE id = ?", (event_id,))[0]["code"]


def super_login(client):
    resp = client.post("/super/login", data={"password": ADMIN_PASSWORD})
    assert resp.status_code == 303


def create_event(client, db_path, name="Office Christmas"):
    """Create an event the public way. Returns (event_id, creator_token)."""
    resp = client.post("/events", data={"name": name})
    assert resp.status_code == 303, resp.text
    token = MANAGE_RE.search(resp.headers["location"]).group(1)
    # The database only keeps the hash, so the id is found through it.
    event_id = query(
        db_path, "SELECT id FROM event WHERE creator_token_hash = ?", (lookup_hash(token),)
    )[0]["id"]
    return event_id, token


def add_names(client, token, names):
    for name in names:
        resp = client.post(f"/manage/{token}/participants", data={"name": name})
        assert resp.status_code == 303, resp.text


def claim(client, db_path, event_id, name):
    resp = client.post(
        "/claim", data={"code": event_code(db_path, event_id), "name": name}
    )
    assert resp.status_code == 200, resp.text
    match = LINK_RE.search(resp.text)
    assert match, "claim page should show a link"
    return match.group(1)


def draw(client, token):
    return client.post(f"/manage/{token}/draw")


def setup_full_event(client, db_path, name="Office Christmas", names=NAMES):
    event_id, creator = create_event(client, db_path, name)
    add_names(client, creator, names)
    tokens = {n: claim(client, db_path, event_id, n) for n in names}
    return event_id, creator, tokens


def person_id(db_path, event_id, name):
    rows = query(
        db_path,
        "SELECT id FROM participant WHERE event_id = ? AND name = ?",
        (event_id, name),
    )
    return rows[0]["id"]


def add_couple(client, token, a_id, b_id):
    return client.post(f"/manage/{token}/couples", data={"person_a": a_id, "person_b": b_id})


def reveal_receiver(client, token):
    match = re.search(r"<strong>(\w+)</strong>", client.get(f"/r/{token}").text)
    return match.group(1)


def test_creator_pages_need_a_real_token(client):
    assert client.get("/manage/not-a-real-token").status_code == 404
    assert client.post("/manage/not-a-real-token/draw").status_code == 404
    assert client.post("/manage/not-a-real-token/delete").status_code == 404


def test_super_pages_require_login(client):
    assert client.get("/super").headers["location"] == "/super/login"
    assert client.post("/super/events/1/delete").headers["location"] == "/super/login"
    assert client.post("/super/events/1/rotate-creator").headers["location"] == "/super/login"


def test_creator_link_does_not_open_super_admin(client, db_path):
    _, creator = create_event(client, db_path)
    assert client.get("/super").headers["location"] == "/super/login"
    assert client.get(f"/manage/{creator}").status_code == 200


def test_wrong_super_password(client):
    resp = client.post("/super/login", data={"password": "nope"})
    assert resp.status_code == 401


def test_super_logout_ends_session(client):
    super_login(client)
    assert client.get("/super").status_code == 200
    client.post("/super/logout")
    assert client.get("/super").headers["location"] == "/super/login"


def test_index_has_create_form(client):
    html = client.get("/").text
    assert 'action="/events"' in html
    assert "/admin" not in html


def test_create_event_requires_name(client):
    resp = client.post("/events", data={"name": "   "})
    assert resp.status_code == 400
    assert "1–100 characters" in resp.text


def test_create_event_opens_creator_page(client, db_path):
    event_id, creator = create_event(client, db_path, "Family Gift Swap")
    resp = client.get(f"/manage/{creator}")
    assert resp.status_code == 200
    assert "Family Gift Swap" in resp.text
    assert "Keep this page's link private" in resp.text


def test_creator_page_shows_invite_link(client, db_path):
    event_id, creator = create_event(client, db_path)
    html = client.get(f"/manage/{creator}").text
    assert f"/join/{event_code(db_path, event_id)}" in html


def test_super_lists_all_events_with_detail(client, db_path):
    _, first = create_event(client, db_path, "Family Gift Swap")
    _, second = create_event(client, db_path, "Work Party")
    super_login(client)
    html = client.get("/super").text
    assert "Family Gift Swap" in html
    assert "Work Party" in html
    assert "0 / 0 claimed" in html


def test_super_sees_participant_names_but_no_tokens(client, db_path):
    event_id, creator, tokens = setup_full_event(client, db_path)
    super_login(client)
    html = client.get("/super").text
    assert "Ann" in html
    assert "0 / 4 claimed" not in html  # all four claimed
    assert "4 / 4 claimed" in html
    assert all(token not in html for token in tokens.values())
    assert creator not in html


def test_join_page_shows_event_name_and_form(client, db_path):
    event_id, _ = create_event(client, db_path, "Office Christmas")
    code = event_code(db_path, event_id)

    resp = client.get(f"/join/{code}")
    assert resp.status_code == 200
    assert "Office Christmas" in resp.text
    assert f'value="{code}"' in resp.text  # code is embedded, not typed


def test_join_unknown_code_is_404(client):
    assert client.get("/join/not-a-code").status_code == 404


def test_join_after_draw_says_closed(client, db_path):
    event_id, creator, _ = setup_full_event(client, db_path)
    draw(client, creator)
    resp = client.get(f"/join/{event_code(db_path, event_id)}")
    assert "already happened" in resp.text
    assert "<form" not in resp.text


def test_full_flow(client, db_path):
    event_id, creator, tokens = setup_full_event(client, db_path)

    assert draw(client, creator).status_code == 303

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
    first, first_creator, first_tokens = setup_full_event(client, db_path, "Family")
    second, second_creator, second_tokens = setup_full_event(client, db_path, "Work")

    # Same names in both events, each with its own link.
    assert first_tokens.keys() == second_tokens.keys()
    assert set(first_tokens.values()).isdisjoint(second_tokens.values())

    draw(client, first_creator)
    # Drawing one event doesn't touch the other.
    assert "<strong>" in client.get(f"/r/{first_tokens['Ann']}").text
    assert "The draw hasn't happened yet" in client.get(f"/r/{second_tokens['Ann']}").text
    assert query(db_path, "SELECT drawn FROM event WHERE id = ?", (second,))[0]["drawn"] == 0


def test_creator_token_only_reaches_its_own_event(client, db_path):
    first, first_creator = create_event(client, db_path, "Family")
    second, second_creator = create_event(client, db_path, "Work")
    add_names(client, first_creator, NAMES)
    add_names(client, second_creator, ["Zoe", "Yan", "Xi"])
    ann = person_id(db_path, first, "Ann")
    zoe = person_id(db_path, second, "Zoe")

    # The first creator can't couple or unclaim someone from the second event.
    assert add_couple(client, first_creator, ann, zoe).status_code == 400
    resp = client.post(f"/manage/{first_creator}/participants/{zoe}/unclaim")
    assert resp.status_code == 400
    assert "Zoe" not in client.get(f"/manage/{first_creator}").text


def test_claim_code_is_scoped_to_its_event(client, db_path):
    first, first_creator = create_event(client, db_path, "Family")
    second, second_creator = create_event(client, db_path, "Work")
    add_names(client, first_creator, ["Ann", "Ben", "Cy"])
    add_names(client, second_creator, ["Zoe", "Yan", "Xi"])

    # "Zoe" exists in the second event only, so the first event's code must reject her.
    resp = client.post(
        "/claim", data={"code": event_code(db_path, first), "name": "Zoe"}
    )
    assert resp.status_code == 400
    assert "on the list" in resp.text  # apostrophe is HTML-escaped in the page


def test_same_name_can_exist_in_two_events(client, db_path):
    _, first_creator = create_event(client, db_path, "Family")
    _, second_creator = create_event(client, db_path, "Work")
    add_names(client, first_creator, ["Ann"])
    add_names(client, second_creator, ["Ann"])
    assert len(query(db_path, "SELECT id FROM participant WHERE name = 'Ann'")) == 2


def test_duplicate_name_in_same_event_rejected(client, db_path):
    _, creator = create_event(client, db_path)
    add_names(client, creator, ["Ann"])
    resp = client.post(f"/manage/{creator}/participants", data={"name": "ann"})
    assert resp.status_code == 400
    assert "already on this list" in resp.text


def test_draw_blocked_until_everyone_claims(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, NAMES)
    claim(client, db_path, event_id, "Ann")

    resp = draw(client, creator)
    assert resp.status_code == 400
    assert "Everyone must claim" in resp.text


def test_draw_needs_three_people(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, ["Ann", "Ben"])
    claim(client, db_path, event_id, "Ann")
    claim(client, db_path, event_id, "Ben")

    resp = draw(client, creator)
    assert resp.status_code == 400
    assert "at least 3" in resp.text


def test_claim_rules(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, NAMES)

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


def test_unknown_reveal_token_is_404(client):
    assert client.get("/r/not-a-real-token").status_code == 404


def test_no_pairing_is_visible_in_db_or_pages(client, db_path):
    event_id, creator, tokens = setup_full_event(client, db_path)
    draw(client, creator)

    blobs = [row["ciphertext"] for row in query(db_path, "SELECT ciphertext FROM assignment")]
    assert len(blobs) == len(NAMES)
    for blob in blobs:
        assert not any(name.encode() in blob for name in NAMES)

    creator_html = client.get(f"/manage/{creator}").text
    assert "Match opened" in creator_html
    assert "You are the Secret Santa" not in creator_html

    super_login(client)
    super_html = client.get("/super").text
    assert "Match opened" in super_html
    assert "You are the Secret Santa" not in super_html


def test_opening_match_records_only_a_timestamp(client, db_path):
    event_id, creator, tokens = setup_full_event(client, db_path)
    draw(client, creator)

    client.get(f"/r/{tokens['Ann']}")
    ann = query(
        db_path, "SELECT revealed_at, public_key FROM participant WHERE name = 'Ann'"
    )[0]
    assert ann["revealed_at"] is not None
    assert len(ann["public_key"]) == 32  # public key only, no token stored


def test_draw_is_idempotent(client, db_path):
    event_id, creator, _ = setup_full_event(client, db_path)

    draw(client, creator)
    resp = draw(client, creator)
    assert resp.status_code == 400
    assert "already happened" in resp.text

    count = query(db_path, "SELECT COUNT(*) AS n FROM assignment")[0]["n"]
    assert count == len(NAMES)


def test_reset_only_affects_its_event(client, db_path):
    first, first_creator, first_tokens = setup_full_event(client, db_path, "Family")
    second, second_creator, second_tokens = setup_full_event(client, db_path, "Work")
    draw(client, first_creator)
    draw(client, second_creator)

    client.post(f"/manage/{first_creator}/reset")

    assert client.get(f"/r/{first_tokens['Ann']}").status_code == 404
    assert "<strong>" in client.get(f"/r/{second_tokens['Ann']}").text
    assert query(db_path, "SELECT drawn FROM event WHERE id = ?", (second,))[0]["drawn"] == 1
    super_login(client)
    super_html = client.get("/super").text
    assert "0 / 4 claimed" in super_html  # the reset event has no claims left
    assert "4 / 4 claimed" in super_html  # the other event keeps its claims


def test_claim_closed_after_draw(client, db_path):
    event_id, creator, _ = setup_full_event(client, db_path)
    draw(client, creator)

    resp = client.post(
        "/claim", data={"code": event_code(db_path, event_id), "name": "Ann"}
    )
    assert resp.status_code == 400
    assert "already happened" in resp.text


def test_unknown_token_cannot_draw_or_reset(client):
    assert client.post("/manage/nope/draw").status_code == 404
    assert client.post("/manage/nope/reset").status_code == 404


def test_couples_never_draw_each_other(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, NAMES)
    tokens = {n: claim(client, db_path, event_id, n) for n in NAMES}
    ann, ben, cy, dee = (person_id(db_path, event_id, n) for n in NAMES)
    assert add_couple(client, creator, ann, ben).status_code == 303
    assert add_couple(client, creator, cy, dee).status_code == 303

    assert draw(client, creator).status_code == 303

    receivers = {n: reveal_receiver(client, tokens[n]) for n in NAMES}
    assert receivers["Ann"] != "Ben" and receivers["Ben"] != "Ann"
    assert receivers["Cy"] != "Dee" and receivers["Dee"] != "Cy"


def test_couple_validation(client, db_path):
    event_id, creator = create_event(client, db_path, "Family")
    other_id, other_creator = create_event(client, db_path, "Work")
    add_names(client, creator, NAMES)
    add_names(client, other_creator, ["Zoe"])
    ann, ben = person_id(db_path, event_id, "Ann"), person_id(db_path, event_id, "Ben")
    zoe = person_id(db_path, other_id, "Zoe")

    assert add_couple(client, creator, ann, ann).status_code == 400
    assert add_couple(client, creator, ann, zoe).status_code == 400  # other event
    assert add_couple(client, creator, ann, ben).status_code == 303
    resp = add_couple(client, creator, ben, ann)  # same couple, reversed
    assert resp.status_code == 400
    assert "already set" in resp.text


def test_couple_removal(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, NAMES)
    ann, ben = person_id(db_path, event_id, "Ann"), person_id(db_path, event_id, "Ben")
    add_couple(client, creator, ann, ben)

    client.post(
        f"/manage/{creator}/couples/remove",
        data={"person_a": ben, "person_b": ann},
    )

    assert query(db_path, "SELECT COUNT(*) AS n FROM exclusion")[0]["n"] == 0


def test_couples_cannot_change_after_draw(client, db_path):
    event_id, creator, _ = setup_full_event(client, db_path)
    draw(client, creator)
    ann, ben = person_id(db_path, event_id, "Ann"), person_id(db_path, event_id, "Ben")

    resp = add_couple(client, creator, ann, ben)
    assert resp.status_code == 400
    assert "already happened" in resp.text


def test_impossible_couples_block_the_draw(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, ["Ann", "Ben", "Cy"])
    for name in ["Ann", "Ben", "Cy"]:
        claim(client, db_path, event_id, name)
    add_couple(
        client,
        creator,
        person_id(db_path, event_id, "Ann"),
        person_id(db_path, event_id, "Ben"),
    )

    resp = draw(client, creator)
    assert resp.status_code == 400
    assert "fewer than two valid partners" in resp.text
    assert query(db_path, "SELECT drawn FROM event WHERE id = ?", (event_id,))[0]["drawn"] == 0
    assert query(db_path, "SELECT COUNT(*) AS n FROM assignment")[0]["n"] == 0


def test_creator_can_unclaim_one_person(client, db_path):
    event_id, creator, tokens = setup_full_event(client, db_path)
    ann = person_id(db_path, event_id, "Ann")

    resp = client.post(f"/manage/{creator}/participants/{ann}/unclaim")
    assert resp.status_code == 303

    assert client.get(f"/r/{tokens['Ann']}").status_code == 404  # old link is dead
    assert "3 of 4" in client.get(f"/manage/{creator}").text
    assert "<strong>" not in client.get(f"/r/{tokens['Ben']}").text  # others unaffected

    new_token = claim(client, db_path, event_id, "Ann")
    assert new_token != tokens["Ann"]
    assert client.get(f"/r/{new_token}").status_code == 200


def test_unclaim_closed_after_draw(client, db_path):
    event_id, creator, tokens = setup_full_event(client, db_path)
    draw(client, creator)
    ann = person_id(db_path, event_id, "Ann")

    resp = client.post(f"/manage/{creator}/participants/{ann}/unclaim")
    assert resp.status_code == 400
    assert "already happened" in resp.text
    assert "<strong>" in client.get(f"/r/{tokens['Ann']}").text  # Ann's link still works


def test_unclaim_unknown_person_is_rejected(client, db_path):
    event_id, creator, _ = setup_full_event(client, db_path)
    resp = client.post(f"/manage/{creator}/participants/999/unclaim")
    assert resp.status_code == 400
    assert "unclaim that person" in resp.text  # apostrophe is HTML-escaped in the page


def test_creator_can_delete_event_and_everything_in_it(client, db_path):
    event_id, creator, tokens = setup_full_event(client, db_path)
    ann, ben = person_id(db_path, event_id, "Ann"), person_id(db_path, event_id, "Ben")
    add_couple(client, creator, ann, ben)
    draw(client, creator)

    resp = client.post(f"/manage/{creator}/delete")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    assert client.get(f"/manage/{creator}").status_code == 404
    assert client.get(f"/r/{tokens['Ann']}").status_code == 404
    for table in ("event", "participant", "exclusion", "assignment"):
        assert query(db_path, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == 0


def test_super_can_delete_any_event(client, db_path):
    event_id, creator, tokens = setup_full_event(client, db_path)
    super_login(client)

    resp = client.post(f"/super/events/{event_id}/delete")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/super"

    assert client.get(f"/manage/{creator}").status_code == 404
    assert client.get(f"/r/{tokens['Ann']}").status_code == 404
    assert query(db_path, "SELECT COUNT(*) AS n FROM participant")[0]["n"] == 0


def test_super_rotate_replaces_creator_link(client, db_path):
    event_id, old_creator, _ = setup_full_event(client, db_path)
    super_login(client)

    resp = client.post(f"/super/events/{event_id}/rotate-creator")
    assert resp.status_code == 200
    new_creator = MANAGE_RE.search(resp.text).group(1)

    assert new_creator != old_creator
    assert client.get(f"/manage/{old_creator}").status_code == 404
    assert client.get(f"/manage/{new_creator}").status_code == 200
    assert "3 of 4" not in client.get(f"/manage/{new_creator}").text  # claims are kept
    assert "4 of 4" in client.get(f"/manage/{new_creator}").text


def test_rotate_unknown_event_is_404(client):
    super_login(client)
    assert client.post("/super/events/999/rotate-creator").status_code == 404
    assert client.post("/super/events/999/delete").status_code == 404


def test_event_creation_is_rate_limited(app, client):
    app.state.create_limiter = RateLimiter(limit=2, window_seconds=3600)
    assert client.post("/events", data={"name": "One"}).status_code == 303
    assert client.post("/events", data={"name": "Two"}).status_code == 303

    resp = client.post("/events", data={"name": "Three"})
    assert resp.status_code == 429
    assert "Too many events" in resp.text


def test_super_login_is_rate_limited(app, client):
    app.state.login_limiter = RateLimiter(limit=2, window_seconds=900)
    assert client.post("/super/login", data={"password": "nope"}).status_code == 401
    assert client.post("/super/login", data={"password": "nope"}).status_code == 401

    resp = client.post("/super/login", data={"password": ADMIN_PASSWORD})
    assert resp.status_code == 429  # even the right password waits


def set_signup(client, token, mode):
    return client.post(f"/manage/{token}/signup-mode", data={"mode": mode})


def test_events_default_to_closed_signup(client, db_path):
    event_id, _ = create_event(client, db_path)
    assert query(db_path, "SELECT signup_mode FROM event WHERE id = ?", (event_id,))[0][
        "signup_mode"
    ] == "closed"


def test_closed_signup_rejects_names_not_on_list(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, NAMES)
    resp = client.post("/claim", data={"code": event_code(db_path, event_id), "name": "Zed"})
    assert resp.status_code == 400
    assert "on the list" in resp.text


def test_open_signup_lets_anyone_join_by_name(client, db_path):
    event_id, creator = create_event(client, db_path)
    assert set_signup(client, creator, "open").status_code == 303

    resp = client.post("/claim", data={"code": event_code(db_path, event_id), "name": "Ann"})
    assert resp.status_code == 200
    link = LINK_RE.search(resp.text).group(1)

    assert client.get(f"/r/{link}").status_code == 200
    assert "1 of 1" in client.get(f"/manage/{creator}").text
    assert person_id(db_path, event_id, "Ann")  # the creator never added Ann


def test_open_signup_claims_a_name_the_creator_added(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, ["Ann", "Ben"])
    set_signup(client, creator, "open")

    link = claim(client, db_path, event_id, "Ann")  # goes through /claim in open mode
    assert link
    assert len(query(db_path, "SELECT id FROM participant WHERE name = 'Ann'")) == 1


def test_open_signup_rejects_a_taken_name(client, db_path):
    event_id, creator = create_event(client, db_path)
    set_signup(client, creator, "open")
    claim(client, db_path, event_id, "Ann")

    resp = client.post("/claim", data={"code": event_code(db_path, event_id), "name": "ann"})
    assert resp.status_code == 400
    assert "already taken" in resp.text
    assert len(query(db_path, "SELECT id FROM participant")) == 1


def test_open_signup_rejects_blank_and_long_names(client, db_path):
    event_id, creator = create_event(client, db_path)
    set_signup(client, creator, "open")
    code = event_code(db_path, event_id)

    assert client.post("/claim", data={"code": code, "name": "   "}).status_code == 400
    resp = client.post("/claim", data={"code": code, "name": "x" * 101})
    assert resp.status_code == 400
    assert "1–100 characters" in resp.text
    assert query(db_path, "SELECT COUNT(*) AS n FROM participant")[0]["n"] == 0


def test_open_signup_stops_after_draw(client, db_path):
    event_id, creator, _ = setup_full_event(client, db_path)
    set_signup(client, creator, "open")
    draw(client, creator)

    resp = client.post("/claim", data={"code": event_code(db_path, event_id), "name": "Zoe"})
    assert resp.status_code == 400
    assert "already happened" in resp.text
    assert query(db_path, "SELECT COUNT(*) AS n FROM participant")[0]["n"] == 4


def test_closing_signup_stops_new_names(client, db_path):
    event_id, creator = create_event(client, db_path)
    set_signup(client, creator, "open")
    claim(client, db_path, event_id, "Ann")
    assert set_signup(client, creator, "closed").status_code == 303

    resp = client.post("/claim", data={"code": event_code(db_path, event_id), "name": "Ben"})
    assert resp.status_code == 400
    assert "on the list" in resp.text
    assert "1 of 1" in client.get(f"/manage/{creator}").text  # Ann keeps her link


def test_signup_mode_cannot_change_after_draw(client, db_path):
    event_id, creator, _ = setup_full_event(client, db_path)
    draw(client, creator)

    resp = set_signup(client, creator, "open")
    assert resp.status_code == 400
    assert "already happened" in resp.text
    assert query(db_path, "SELECT signup_mode FROM event WHERE id = ?", (event_id,))[0][
        "signup_mode"
    ] == "closed"


def test_signup_mode_rejects_unknown_values(client, db_path):
    _, creator = create_event(client, db_path)
    resp = set_signup(client, creator, "maybe")
    assert resp.status_code == 400
    assert "open or closed" in resp.text


def test_signup_mode_needs_the_right_creator_token(client, db_path):
    _, creator = create_event(client, db_path)
    assert set_signup(client, "not-a-real-token", "open").status_code == 404


def test_join_page_text_follows_mode(client, db_path):
    event_id, creator = create_event(client, db_path)
    code = event_code(db_path, event_id)
    assert "Pick your name from the list" in client.get(f"/join/{code}").text

    set_signup(client, creator, "open")
    html = client.get(f"/join/{code}").text
    assert "Type your name to join" in html
    assert 'name="name"' in html


def test_super_shows_signup_mode(client, db_path):
    _, creator = create_event(client, db_path, "Family")
    set_signup(client, creator, "open")
    super_login(client)
    assert "sign-up open" in client.get("/super").text


def remove_person(client, token, person):
    return client.post(f"/manage/{token}/participants/{person}/remove")


def test_creator_can_remove_an_unclaimed_person(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, NAMES)
    dee = person_id(db_path, event_id, "Dee")

    resp = remove_person(client, creator, dee)
    assert resp.status_code == 303
    assert "Dee" not in client.get(f"/manage/{creator}").text
    assert "0 of 3" in client.get(f"/manage/{creator}").text


def test_removing_a_claimed_person_kills_their_link(client, db_path):
    event_id, creator, tokens = setup_full_event(client, db_path)
    ann = person_id(db_path, event_id, "Ann")

    assert remove_person(client, creator, ann).status_code == 303
    assert client.get(f"/r/{tokens['Ann']}").status_code == 404
    assert "3 of 3" in client.get(f"/manage/{creator}").text


def test_removing_a_person_also_removes_their_couples(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, NAMES)
    ann, ben, cy, dee = (person_id(db_path, event_id, n) for n in NAMES)
    add_couple(client, creator, ann, ben)
    add_couple(client, creator, cy, dee)

    remove_person(client, creator, ben)

    assert query(db_path, "SELECT COUNT(*) AS n FROM exclusion")[0]["n"] == 1
    assert "Ann &amp; Ben" not in client.get(f"/manage/{creator}").text


def test_removed_name_can_be_added_again(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, ["Ann", "Ben"])
    remove_person(client, creator, person_id(db_path, event_id, "Ann"))

    add_names(client, creator, ["Ann"])
    assert len(query(db_path, "SELECT id FROM participant WHERE name = 'Ann'")) == 1


def test_remove_is_blocked_after_draw(client, db_path):
    event_id, creator, tokens = setup_full_event(client, db_path)
    draw(client, creator)
    ann = person_id(db_path, event_id, "Ann")

    resp = remove_person(client, creator, ann)
    assert resp.status_code == 400
    assert "already happened" in resp.text
    assert "<strong>" in client.get(f"/r/{tokens['Ann']}").text


def test_creator_cannot_remove_someone_from_another_event(client, db_path):
    first, first_creator = create_event(client, db_path, "Family")
    second, second_creator = create_event(client, db_path, "Work")
    add_names(client, first_creator, NAMES)
    add_names(client, second_creator, ["Zoe"])
    zoe = person_id(db_path, second, "Zoe")

    resp = remove_person(client, first_creator, zoe)
    assert resp.status_code == 400
    assert "remove that person" in resp.text
    assert len(query(db_path, "SELECT id FROM participant WHERE name = 'Zoe'")) == 1


def test_remove_unknown_person_is_rejected(client, db_path):
    _, creator = create_event(client, db_path)
    resp = remove_person(client, creator, 999)
    assert resp.status_code == 400
    assert "remove that person" in resp.text


def test_creator_page_has_remove_button_only_before_draw(client, db_path):
    event_id, creator = create_event(client, db_path)
    add_names(client, creator, ["Ann"])
    ann = person_id(db_path, event_id, "Ann")
    assert f"/manage/{creator}/participants/{ann}/remove" in client.get(f"/manage/{creator}").text

    claim(client, db_path, event_id, "Ann")
    add_names(client, creator, ["Ben", "Cy"])
    for name in ["Ben", "Cy"]:
        claim(client, db_path, event_id, name)
    draw(client, creator)
    assert "/remove" not in client.get(f"/manage/{creator}").text
