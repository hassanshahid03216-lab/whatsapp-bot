import hmac
import ipaddress
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.database import get_db
from app.deps import (
    Auth,
    _get_trusted_networks,
    _inet,
    _is_ip_trusted,
    client_ip,
    csrf_protect,
    get_auth,
    resolve_client_ip,
    utcnow,
)
from app.embeddings import get_embedding_provider
from app.models import AuditLog, Sellers, Sessions, Users
from app.ratelimit import login_email_limiter, login_ip_limiter, signup_ip_limiter
from app.security import (
    csrf_token_for, hash_password, hash_token, new_session_token, verify_password,
)

config.secret_key()  # fail fast at startup if SECRET_KEY is missing or weak


# ---------- structured JSON logging ----------

class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line — parseable by Datadog, GCP, etc."""
    _BUILTIN_ATTRS = frozenset({
        "name", "msg", "args", "created", "relativeCreated", "exc_info",
        "exc_text", "stack_info", "lineno", "funcName", "msecs", "pathname",
        "filename", "module", "levelno", "levelname", "thread", "threadName",
        "process", "processName", "taskName", "message",
    })

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Attach only user-supplied extra fields
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN_ATTRS and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Silence noisy libraries
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("uvicorn.error").propagate = True


_configure_logging()
logger = logging.getLogger("sellerbot")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Validate provider selection and configured dimension before accepting traffic.
    validate_embedding_provider_at_startup()
    yield


app = FastAPI(
    title="SellerBot MVP", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
)

STATIC_DIR = Path(__file__).parent / "static"


def validate_embedding_provider_at_startup() -> None:
    get_embedding_provider().validate_dimension()


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
GENERIC_LOGIN_ERROR = "Invalid email or password"
TOO_MANY = "Too many attempts. Try again later."


# ---------- trusted proxies & security headers ----------


@app.middleware("http")
async def trusted_proxy_middleware(request: Request, call_next):
    request.state.client_ip = resolve_client_ip(request)
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Propagate or generate X-Request-ID so every request is traceable end-to-end."""
    req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = req_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    return response


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    """Emit a structured JSON access log line for every request."""
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    logger.info(
        "request",
        extra={
            "request_id": getattr(request.state, "request_id", "-"),
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
            "client_ip": getattr(request.state, "client_ip", "-"),
        },
    )
    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ---------- helpers ----------

def audit(request: Request, seller_id, user_id, action: str) -> AuditLog:
    """changes is always empty: never store passwords, tokens, emails or message bodies here."""
    return AuditLog(
        seller_id=seller_id, user_id=user_id, action=action, entity_type="user",
        entity_id=user_id, changes={}, ip_address=_inet(client_ip(request)),
        user_agent=(request.headers.get("user-agent") or "")[:200] or None,
    )


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=config.COOKIE_NAME, value=token, max_age=config.SESSION_TTL_HOURS * 3600,
        httponly=True, samesite="lax", secure=config.COOKIE_SECURE, path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=config.COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=config.COOKIE_SECURE,
    )


# ---------- schemas (request bodies only: never credentials in the URL) ----------

class SignupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seller_name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=254)
    password: str = Field(min_length=10, max_length=128)

    @field_validator("seller_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("seller_name is required")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_RE.fullmatch(v):
            raise ValueError("invalid email")
        return v


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.strip().lower()


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    # FastAPI's default 422 echoes the submitted input (including the password). Strip it.
    errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


# ---------- routes ----------

@app.get("/health")
def health(db: Session = Depends(get_db)):
    # --- Database check (critical — 503 if unreachable) ---
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="db_unavailable")

    # --- Redis check (non-critical — app has in-memory fallback) ---
    redis_status = "not_configured"
    if config.REDIS_URL:
        try:
            import redis as _redis
            r = _redis.from_url(config.REDIS_URL, socket_connect_timeout=1)
            r.ping()
            r.close()
            redis_status = "connected"
        except Exception:
            redis_status = "unavailable"

    return {"status": "ok", "db": "connected", "redis": redis_status}


@app.post("/auth/signup", status_code=201)
def signup(payload: SignupIn, request: Request, response: Response, db: Session = Depends(get_db)):
    if not signup_ip_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail=TOO_MANY)

    password_hash = hash_password(payload.password)  # slow: do it before opening the transaction
    now = utcnow()
    token = new_session_token()
    seller = Sellers(id=uuid.uuid4(), name=payload.seller_name, is_active=True)
    user = Users(
        id=uuid.uuid4(), seller_id=seller.id, email=payload.email,
        password_hash=password_hash, is_active=True, last_login=now,
    )
    try:  # seller + user + session + audit commit together or not at all
        db.add(seller)
        db.flush()
        db.add(user)
        db.flush()
        db.add(Sessions(
            id=uuid.uuid4(), token_hash=hash_token(token), user_id=user.id, seller_id=seller.id,
            expires_at=now + timedelta(hours=config.SESSION_TTL_HOURS),
        ))
        db.add(audit(request, seller.id, user.id, "create"))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Could not create account")

    set_session_cookie(response, token)
    return {"user_id": str(user.id), "seller_id": str(seller.id), "email": user.email}


@app.post("/auth/login")
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    if not login_ip_limiter.allow(client_ip(request)) or not login_email_limiter.allow(payload.email):
        raise HTTPException(status_code=429, detail=TOO_MANY)

    user = db.execute(select(Users).where(Users.email == payload.email)).scalar_one_or_none()
    password_ok = verify_password(user.password_hash if user else None, payload.password)  # always runs
    seller = db.get(Sellers, user.seller_id) if user else None
    if not (user and password_ok and user.is_active and seller and seller.is_active):
        raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)  # same answer for every failure

    login_email_limiter.reset(payload.email)
    now = utcnow()

    # Rotation: drop the session this browser presented (if it is this user's) and any expired ones.
    presented = request.cookies.get(config.COOKIE_NAME)
    if presented:
        db.execute(delete(Sessions).where(
            Sessions.token_hash == hash_token(presented), Sessions.user_id == user.id))
    db.execute(delete(Sessions).where(Sessions.user_id == user.id, Sessions.expires_at <= now))

    token = new_session_token()
    db.add(Sessions(
        id=uuid.uuid4(), token_hash=hash_token(token), user_id=user.id, seller_id=user.seller_id,
        expires_at=now + timedelta(hours=config.SESSION_TTL_HOURS),
    ))
    user.last_login = now
    db.add(audit(request, user.seller_id, user.id, "login"))
    db.commit()

    set_session_cookie(response, token)
    return {"user_id": str(user.id), "seller_id": str(user.seller_id), "email": user.email}


@app.get("/auth/csrf")
def csrf(response: Response, auth: Auth = Depends(get_auth)):
    response.headers["Cache-Control"] = "no-store"
    return {"csrf_token": csrf_token_for(auth.token)}


@app.post("/auth/logout")
def logout(
    request: Request, response: Response,
    auth: Auth = Depends(csrf_protect), db: Session = Depends(get_db),
):
    db.execute(delete(Sessions).where(Sessions.id == auth.session_id))
    db.add(audit(request, auth.seller_id, auth.user_id, "logout"))
    db.commit()
    clear_session_cookie(response)
    return {"detail": "logged out"}


@app.get("/me")
def me(auth: Auth = Depends(get_auth)):
    return {"user_id": str(auth.user_id), "seller_id": str(auth.seller_id), "email": auth.email}


from app.catalog import router as catalog_router
from app.faqs import router as faqs_router
from app.whatsapp import router as whatsapp_router

app.include_router(catalog_router)
app.include_router(faqs_router)
app.include_router(whatsapp_router)
