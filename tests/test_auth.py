import hashlib

from sqlalchemy import text

from app.config import COOKIE_NAME, COOKIE_SECURE, LOGIN_EMAIL_MAX_ATTEMPTS, LOGIN_IP_MAX_ATTEMPTS
from tests.helpers import new_seller, violation

PW = "correct-horse-battery"
EMAIL = "ali@example.com"
WRONG = "wrong-password-123"


def signup(client, email=EMAIL, password=PW, seller="Ali Store"):
    return client.post("/auth/signup", json={"seller_name": seller, "email": email, "password": password})


def login(client, email=EMAIL, password=PW):
    return client.post("/auth/login", json={"email": email, "password": password})


def with_cookie(token):
    return {"Cookie": f"{COOKIE_NAME}={token}"}


def count(conn, sql, **params):
    return conn.execute(text(sql), params).scalar_one()


# ---------- signup ----------

def test_signup_happy_path(client, conn):
    r = signup(client)
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == EMAIL
    cookie = client.cookies.get(COOKIE_NAME)
    assert cookie

    pw_hash = conn.execute(text("SELECT password_hash FROM users WHERE email = :e"), {"e": EMAIL}).scalar_one()
    assert pw_hash.startswith("$argon2id$") and PW not in pw_hash

    stored = conn.execute(
        text("SELECT s.token_hash FROM sessions s JOIN users u ON u.id = s.user_id WHERE u.email = :e"),
        {"e": EMAIL},
    ).scalars().all()
    assert stored == [hashlib.sha256(cookie.encode()).hexdigest()]  # only the hash is stored, never the token

    me = client.get("/me")
    assert me.status_code == 200
    assert me.json()["seller_id"] == body["seller_id"] and me.json()["email"] == EMAIL


def test_duplicate_email_differing_only_by_case(client, conn):
    assert signup(client).status_code == 201
    client.cookies.clear()
    r = signup(client, email="ALI@Example.COM")
    assert r.status_code == 409
    for leak in ("constraint", "duplicate", "psycopg", "sql", "uq_users"):
        assert leak not in r.text.lower()
    assert count(conn, "SELECT count(*) FROM sellers WHERE name = 'Ali Store'") == 1  # failed signup left nothing


def test_weak_password_rejected_and_not_echoed(client):
    r = signup(client, password="short-pw9")
    assert r.status_code == 422
    assert "short-pw9" not in r.text


def test_invalid_email_rejected(client):
    assert signup(client, email="not-an-email").status_code == 422


def test_credentials_in_query_string_are_not_accepted(client):
    assert client.post("/auth/signup?seller_name=x&email=a@b.co&password=longenoughpassword").status_code == 422
    assert client.post("/auth/login?email=a@b.co&password=longenoughpassword").status_code == 422


# ---------- login ----------

def test_wrong_password_and_unknown_email_look_identical(client):
    signup(client)
    client.cookies.clear()
    wrong = login(client, password=WRONG)
    unknown = login(client, email="nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert client.cookies.get(COOKIE_NAME) is None  # no session for a failed login


def test_login_cookie_flags(client):
    signup(client)
    client.cookies.clear()
    r = login(client)
    assert r.status_code == 200
    header = r.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=lax" in header
    assert ("secure" in header) == COOKIE_SECURE
    assert client.get("/me").status_code == 200


def test_session_rotates_on_login(client):
    signup(client)
    a = client.cookies.get(COOKIE_NAME)
    assert login(client).status_code == 200  # cookie `a` is presented
    b = client.cookies.get(COOKIE_NAME)
    assert b and b != a
    client.cookies.clear()
    assert client.get("/me", headers=with_cookie(a)).status_code == 401  # old session is dead
    assert client.get("/me", headers=with_cookie(b)).status_code == 200


def test_unauthenticated_me_rejected(client):
    assert client.get("/me").status_code == 401


def test_inactive_user_rejected(client, conn, db_session):
    signup(client)
    conn.execute(text("UPDATE users SET is_active = false WHERE email = :e"), {"e": EMAIL})
    db_session.expire_all()
    assert client.get("/me").status_code == 401
    client.cookies.clear()
    assert login(client).status_code == 401


def test_inactive_seller_rejected(client, conn, db_session):
    signup(client)
    conn.execute(text("UPDATE sellers SET is_active = false WHERE name = 'Ali Store'"))
    db_session.expire_all()
    assert client.get("/me").status_code == 401


def test_expired_session_rejected(client, conn, db_session):
    signup(client)
    conn.execute(text("UPDATE sessions SET expires_at = now() - interval '1 hour'"))
    db_session.expire_all()
    assert client.get("/me").status_code == 401


# ---------- CSRF + logout ----------

def test_logout_requires_csrf_and_invalidates_session(client, conn):
    signup(client)
    token = client.cookies.get(COOKIE_NAME)
    assert client.post("/auth/logout").status_code == 403
    assert client.post("/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.get("/me").status_code == 200  # failed attempts did not log us out

    csrf = client.get("/auth/csrf").json()["csrf_token"]
    assert client.post("/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    client.cookies.clear()
    assert client.get("/me", headers=with_cookie(token)).status_code == 401  # dead server-side, not just cookie-cleared
    assert count(conn, "SELECT count(*) FROM sessions s JOIN users u ON u.id = s.user_id WHERE u.email = :e", e=EMAIL) == 0


def test_csrf_token_is_bound_to_the_session(client):
    signup(client)
    csrf_a = client.get("/auth/csrf").json()["csrf_token"]
    login(client)  # rotates the session
    csrf_b = client.get("/auth/csrf").json()["csrf_token"]
    assert csrf_a != csrf_b
    assert client.post("/auth/logout", headers={"X-CSRF-Token": csrf_a}).status_code == 403


# ---------- rate limiting ----------

def test_login_rate_limit_per_email_and_lockout(client):
    signup(client)
    client.cookies.clear()
    for _ in range(LOGIN_EMAIL_MAX_ATTEMPTS):
        assert login(client, password=WRONG).status_code == 401
    assert login(client, password=WRONG).status_code == 429
    assert login(client).status_code == 429  # even the right password is blocked while locked


def test_successful_login_resets_email_counter(client):
    signup(client)
    client.cookies.clear()
    for _ in range(LOGIN_EMAIL_MAX_ATTEMPTS - 1):
        assert login(client, password=WRONG).status_code == 401
    assert login(client).status_code == 200
    for _ in range(LOGIN_EMAIL_MAX_ATTEMPTS - 1):
        assert login(client, password=WRONG).status_code == 401


def test_login_rate_limit_per_ip(client):
    for i in range(LOGIN_IP_MAX_ATTEMPTS):
        assert login(client, email=f"user{i}@example.com").status_code == 401
    assert login(client, email="another@example.com").status_code == 429


# ---------- tenancy ----------

def test_sessions_never_cross_tenants(client):
    a = signup(client, email="a@example.com", seller="Seller A").json()
    cookie_a = client.cookies.get(COOKIE_NAME)
    client.cookies.clear()
    b = signup(client, email="b@example.com", seller="Seller B").json()
    cookie_b = client.cookies.get(COOKIE_NAME)
    client.cookies.clear()

    me_a = client.get("/me", headers=with_cookie(cookie_a)).json()
    me_b = client.get("/me", headers=with_cookie(cookie_b)).json()
    assert a["seller_id"] != b["seller_id"]
    assert me_a["seller_id"] == a["seller_id"] and me_a["email"] == "a@example.com"
    assert me_b["seller_id"] == b["seller_id"] and me_b["email"] == "b@example.com"


def test_session_composite_fk_rejects_user_seller_mismatch(conn):
    a, b = new_seller(conn, "a"), new_seller(conn, "b")
    user_a = conn.execute(
        text("INSERT INTO users (seller_id, email, password_hash) VALUES (:s, 'u@example.com', 'x') RETURNING id"),
        {"s": a},
    ).scalar_one()
    assert violation(
        conn,
        "INSERT INTO sessions (token_hash, user_id, seller_id, expires_at) "
        "VALUES ('h', :u, :s, now() + interval '1 hour')",
        u=user_a, s=b,
    ) == ("23503", "fk_sessions_user_seller")


# ---------- audit ----------

def test_audit_rows_exist_and_contain_no_secrets(client, conn):
    body = signup(client).json()
    login(client)
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    client.post("/auth/logout", headers={"X-CSRF-Token": csrf})

    rows = conn.execute(
        text("SELECT action, changes::text AS changes FROM audit_log WHERE seller_id = :s"),
        {"s": body["seller_id"]},
    ).all()
    assert {"create", "login", "logout"} <= {r.action for r in rows}
    for r in rows:
        low = r.changes.lower()
        assert PW not in low and EMAIL not in low and "password" not in low and "token" not in low