import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app.crypto import lookup_hash, new_token, open_with, public_key_for, seal_for
from app.draw import DrawError, make_cycle

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
router = APIRouter()
MIN_PARTICIPANTS = 3
MAX_NAME_LENGTH = 100


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def require_admin(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)


def get_event_by_code(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, name, code, drawn FROM event WHERE code = ?", (code,)
    ).fetchone()


def get_event_or_404(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    event = conn.execute(
        "SELECT id, name, code, drawn FROM event WHERE id = ?", (event_id,)
    ).fetchone()
    if event is None:
        raise HTTPException(status_code=404)
    return event


def render_index(request: Request, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        request, "index.html", {"error": error}, status_code=status_code
    )


def render_join(
    request: Request, event: sqlite3.Row, error: str | None = None, status_code: int = 200
):
    return templates.TemplateResponse(
        request,
        "join.html",
        {"event": event, "error": error},
        status_code=status_code,
    )


def render_admin_home(request: Request, conn, error: str | None = None, status_code: int = 200):
    events = conn.execute(
        "SELECT e.id, e.name, e.drawn, COUNT(p.id) AS total, COUNT(p.token_hash) AS claimed "
        "FROM event e LEFT JOIN participant p ON p.event_id = e.id "
        "GROUP BY e.id ORDER BY e.id DESC"
    ).fetchall()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"error": error, "events": events},
        status_code=status_code,
    )


def render_admin_event(
    request: Request, conn, event_id: int, error: str | None = None, status_code: int = 200
):
    event = get_event_or_404(conn, event_id)
    # Only status columns are selected: no tokens, keys, or assignments.
    people = conn.execute(
        "SELECT id, name, token_hash IS NOT NULL AS claimed, revealed_at IS NOT NULL AS revealed "
        "FROM participant WHERE event_id = ? ORDER BY name COLLATE NOCASE",
        (event_id,),
    ).fetchall()
    couples = conn.execute(
        "SELECT a.id AS a_id, a.name AS a_name, b.id AS b_id, b.name AS b_name "
        "FROM exclusion x "
        "JOIN participant a ON a.id = x.person_a "
        "JOIN participant b ON b.id = x.person_b "
        "WHERE x.event_id = ? ORDER BY a.name COLLATE NOCASE",
        (event_id,),
    ).fetchall()
    total = len(people)
    claimed = sum(1 for person in people if person["claimed"])
    can_draw = not event["drawn"] and total >= MIN_PARTICIPANTS and claimed == total
    return templates.TemplateResponse(
        request,
        "admin_event.html",
        {
            "error": error,
            "event": event,
            "join_link": request.url_for("join", code=event["code"]),
            "people": people,
            "couples": couples,
            "total": total,
            "claimed": claimed,
            "can_draw": can_draw,
            "min_participants": MIN_PARTICIPANTS,
        },
        status_code=status_code,
    )


@router.get("/")
def index(request: Request):
    return render_index(request)


@router.get("/join/{code}", name="join")
def join(code: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    event = get_event_by_code(conn, code)
    if event is None:
        raise HTTPException(status_code=404)
    return render_join(request, event)


@router.post("/claim")
def claim(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    event = get_event_by_code(conn, code.strip())
    if event is None:
        return render_index(request, "That event code is wrong.", 400)

    def fail(message: str):
        return render_join(request, event, message, 400)

    name = name.strip()
    if event["drawn"]:
        return fail("The draw has already happened.")
    person = conn.execute(
        "SELECT id, token_hash FROM participant WHERE event_id = ? AND name = ?",
        (event["id"], name),
    ).fetchone()
    if person is None:
        return fail("That name isn't on the list.")
    if person["token_hash"] is not None:
        return fail("That name is already claimed. Use your personal link.")

    token = new_token()
    # Single statement, so a draw can't slip in between the check and the write.
    cur = conn.execute(
        "UPDATE participant SET token_hash = ?, public_key = ?, claimed_at = ? "
        "WHERE id = ? AND token_hash IS NULL "
        "AND (SELECT drawn FROM event WHERE id = ?) = 0",
        (lookup_hash(token), public_key_for(token), now(), person["id"], event["id"]),
    )
    if cur.rowcount == 0:
        return fail("Claiming is closed for this name.")

    return templates.TemplateResponse(
        request,
        "claimed.html",
        {
            "link": request.url_for("reveal", token=token),
            "name": name,
            "event_name": event["name"],
        },
    )


@router.get("/r/{token}", name="reveal")
def reveal(token: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    person = conn.execute(
        "SELECT p.id, p.name, p.revealed_at, e.name AS event_name, e.drawn "
        "FROM participant p JOIN event e ON e.id = p.event_id WHERE p.token_hash = ?",
        (lookup_hash(token),),
    ).fetchone()
    if person is None:
        raise HTTPException(status_code=404)

    context = {"name": person["name"], "event_name": person["event_name"], "receiver": None}
    if not person["drawn"]:
        return templates.TemplateResponse(request, "reveal.html", context)

    assignment = conn.execute(
        "SELECT ciphertext FROM assignment WHERE giver_id = ?", (person["id"],)
    ).fetchone()
    if assignment is None:
        raise HTTPException(status_code=404)

    receiver_id = int(open_with(token, assignment["ciphertext"]))
    receiver = conn.execute(
        "SELECT name FROM participant WHERE id = ?", (receiver_id,)
    ).fetchone()
    if person["revealed_at"] is None:
        conn.execute(
            "UPDATE participant SET revealed_at = ? WHERE id = ?", (now(), person["id"])
        )
    context["receiver"] = receiver["name"]
    return templates.TemplateResponse(request, "reveal.html", context)


@router.get("/admin/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "admin_login.html", {"error": None})


@router.post("/admin/login")
def login(request: Request, password: str = Form(...)):
    expected = request.app.state.admin_password.encode()
    if not hmac.compare_digest(password.encode(), expected):
        return templates.TemplateResponse(
            request,
            "admin_login.html",
            {"error": "Wrong password."},
            status_code=401,
        )
    request.session["admin"] = True
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/admin", dependencies=[Depends(require_admin)])
def admin_home(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return render_admin_home(request, conn)


@router.post("/admin/events", dependencies=[Depends(require_admin)])
def create_event(
    request: Request,
    name: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    name = name.strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        return render_admin_home(
            request, conn, f"Event name must be 1–{MAX_NAME_LENGTH} characters.", 400
        )
    cur = conn.execute(
        "INSERT INTO event (name, code, created_at) VALUES (?, ?, ?)",
        (name, db.new_event_code(), now()),
    )
    return RedirectResponse(f"/admin/events/{cur.lastrowid}", status_code=303)


@router.get("/admin/events/{event_id}", dependencies=[Depends(require_admin)])
def admin_event(
    event_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    return render_admin_event(request, conn, event_id)


@router.post("/admin/events/{event_id}/participants", dependencies=[Depends(require_admin)])
def add_participant(
    event_id: int,
    request: Request,
    name: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    event = get_event_or_404(conn, event_id)
    name = name.strip()
    if event["drawn"]:
        return render_admin_event(request, conn, event_id, "The draw has already happened.", 400)
    if not name or len(name) > MAX_NAME_LENGTH:
        return render_admin_event(
            request, conn, event_id, f"Name must be 1–{MAX_NAME_LENGTH} characters.", 400
        )
    try:
        conn.execute(
            "INSERT INTO participant (event_id, name) VALUES (?, ?)", (event_id, name)
        )
    except sqlite3.IntegrityError:
        return render_admin_event(
            request, conn, event_id, f"{name} is already on this list.", 400
        )
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@router.post(
    "/admin/events/{event_id}/participants/{person_id}/unclaim",
    dependencies=[Depends(require_admin)],
)
def unclaim_participant(
    event_id: int,
    person_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    # Their old link stops working. They can claim their name again for a new one.
    event = get_event_or_404(conn, event_id)
    if event["drawn"]:
        return render_admin_event(request, conn, event_id, "The draw has already happened.", 400)
    cur = conn.execute(
        "UPDATE participant SET token_hash = NULL, public_key = NULL, claimed_at = NULL "
        "WHERE id = ? AND event_id = ? "
        "AND (SELECT drawn FROM event WHERE id = ?) = 0",
        (person_id, event_id, event_id),
    )
    if cur.rowcount == 0:
        return render_admin_event(request, conn, event_id, "Couldn't unclaim that person.", 400)
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


def _draw(conn: sqlite3.Connection, event_id: int) -> str | None:
    """Run the draw inside an open transaction. Returns an error message or None.
    Pairs exist only in this function's memory; only ciphertext is written."""
    event = get_event_or_404(conn, event_id)
    if event["drawn"]:
        return "The draw has already happened."
    people = conn.execute(
        "SELECT id, token_hash, public_key FROM participant WHERE event_id = ? ORDER BY id",
        (event_id,),
    ).fetchall()
    if len(people) < MIN_PARTICIPANTS:
        return f"Need at least {MIN_PARTICIPANTS} participants."
    if any(person["token_hash"] is None for person in people):
        return "Everyone must claim their link before the draw."

    couples = conn.execute(
        "SELECT person_a, person_b FROM exclusion WHERE event_id = ?", (event_id,)
    ).fetchall()
    forbidden = {frozenset((c["person_a"], c["person_b"])) for c in couples}
    try:
        pairs = make_cycle([person["id"] for person in people], forbidden)
    except DrawError as exc:
        return str(exc)
    keys = {person["id"]: person["public_key"] for person in people}
    for giver, receiver in pairs.items():
        conn.execute(
            "INSERT INTO assignment (giver_id, ciphertext) VALUES (?, ?)",
            (giver, seal_for(keys[giver], str(receiver))),
        )
    conn.execute("UPDATE event SET drawn = 1 WHERE id = ?", (event_id,))
    return None


def _members(conn: sqlite3.Connection, event_id: int) -> set[int]:
    rows = conn.execute("SELECT id FROM participant WHERE event_id = ?", (event_id,))
    return {row["id"] for row in rows}


@router.post("/admin/events/{event_id}/couples", dependencies=[Depends(require_admin)])
def add_couple(
    event_id: int,
    request: Request,
    person_a: int = Form(...),
    person_b: int = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    event = get_event_or_404(conn, event_id)
    if event["drawn"]:
        return render_admin_event(request, conn, event_id, "The draw has already happened.", 400)
    if person_a == person_b:
        return render_admin_event(request, conn, event_id, "Pick two different people.", 400)
    if not {person_a, person_b} <= _members(conn, event_id):
        return render_admin_event(request, conn, event_id, "Unknown participant.", 400)
    low, high = sorted((person_a, person_b))
    try:
        conn.execute(
            "INSERT INTO exclusion (event_id, person_a, person_b) VALUES (?, ?, ?)",
            (event_id, low, high),
        )
    except sqlite3.IntegrityError:
        return render_admin_event(request, conn, event_id, "That couple is already set.", 400)
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@router.post("/admin/events/{event_id}/couples/remove", dependencies=[Depends(require_admin)])
def remove_couple(
    event_id: int,
    request: Request,
    person_a: int = Form(...),
    person_b: int = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    event = get_event_or_404(conn, event_id)
    if event["drawn"]:
        return render_admin_event(request, conn, event_id, "The draw has already happened.", 400)
    low, high = sorted((person_a, person_b))
    conn.execute(
        "DELETE FROM exclusion WHERE event_id = ? AND person_a = ? AND person_b = ?",
        (event_id, low, high),
    )
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@router.post("/admin/events/{event_id}/draw", dependencies=[Depends(require_admin)])
def run_draw(event_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("BEGIN IMMEDIATE")
    try:
        error = _draw(conn, event_id)
        conn.execute("ROLLBACK" if error else "COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if error:
        return render_admin_event(request, conn, event_id, error, 400)
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@router.post("/admin/events/{event_id}/reset", dependencies=[Depends(require_admin)])
def reset(event_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    # Clears claims as well as the draw: a lost link can't be recovered any other way
    # without the admin ever holding a token. Everyone claims again afterwards.
    get_event_or_404(conn, event_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM assignment WHERE giver_id IN "
            "(SELECT id FROM participant WHERE event_id = ?)",
            (event_id,),
        )
        conn.execute(
            "UPDATE participant SET token_hash = NULL, public_key = NULL, "
            "claimed_at = NULL, revealed_at = NULL WHERE event_id = ?",
            (event_id,),
        )
        conn.execute("UPDATE event SET drawn = 0 WHERE id = ?", (event_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


def create_app(
    db_path: str | None = None,
    admin_password: str | None = None,
    secret_key: str | None = None,
) -> FastAPI:
    db_path = db_path or os.environ.get("DATABASE_PATH", "santa.db")
    admin_password = admin_password or os.environ.get("ADMIN_PASSWORD")
    if not admin_password:
        raise RuntimeError("Set ADMIN_PASSWORD before starting the app.")
    secret_key = secret_key or os.environ.get("SECRET_KEY") or secrets.token_urlsafe(32)

    db.init_db(db_path)

    app = FastAPI(title="Secret Santa")
    app.state.db_path = db_path
    app.state.admin_password = admin_password
    app.add_middleware(SessionMiddleware, secret_key=secret_key)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException):
        if exc.status_code == 401:
            return RedirectResponse("/admin/login", status_code=303)
        return await http_exception_handler(request, exc)

    app.include_router(router)
    return app
