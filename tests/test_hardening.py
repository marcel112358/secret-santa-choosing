import logging
import re
import time

import pytest
from fastapi.testclient import TestClient

from app.limits import RateLimiter
from app.main import RedactTokens, create_app

ADMIN_PASSWORD = "test-admin-pw"


@pytest.fixture
def client(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "santa.db"), admin_password=ADMIN_PASSWORD, secret_key="test"
    )
    return TestClient(app, follow_redirects=False)


def test_limiter_blocks_after_limit_per_key():
    limiter = RateLimiter(limit=2, window_seconds=60)
    assert not limiter.is_limited("a")
    limiter.hit("a")
    limiter.hit("a")
    assert limiter.is_limited("a")
    assert not limiter.is_limited("b")  # other keys have their own budget


def test_limiter_forgets_hits_after_window():
    limiter = RateLimiter(limit=1, window_seconds=0.05)
    limiter.hit("a")
    assert limiter.is_limited("a")
    time.sleep(0.1)
    assert not limiter.is_limited("a")


def test_creator_and_reveal_pages_send_no_referrer_and_no_store(client):
    creator = client.post("/events", data={"name": "Family"}).headers["location"]
    resp = client.get(creator)
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"

    resp = client.get("/r/not-a-real-token")  # 404 still carries the headers
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_other_pages_are_not_marked_no_store(client):
    resp = client.get("/")
    assert "referrer-policy" not in resp.headers


def test_access_log_redacts_creator_and_reveal_tokens():
    redact = RedactTokens()
    for path in ("/manage/AbC-123_xyz", "/r/AbC-123_xyz"):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            0,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5000", "GET", path, "1.1", 200),
            None,
        )
        assert redact.filter(record)
        assert record.args[2] == re.sub(r"[A-Za-z0-9_-]+$", "[redacted]", path)
        assert "AbC-123_xyz" not in record.getMessage()


def test_access_log_filter_is_installed_once(tmp_path):
    for _ in range(2):
        create_app(
            db_path=str(tmp_path / "santa.db"), admin_password=ADMIN_PASSWORD, secret_key="k"
        )
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, RedactTokens) for f in filters) == 1
