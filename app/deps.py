import hmac
import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.database import get_db
from app.models import Sellers, Sessions, Users
from app.security import csrf_token_for, hash_token


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_trusted_networks():
    raw = config.TRUSTED_PROXIES
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item or item.lower() == "testclient":
            continue
        try:
            if "/" in item:
                networks.append(ipaddress.ip_network(item, strict=False))
            else:
                ip = ipaddress.ip_address(item)
                networks.append(ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}", strict=False))
        except ValueError:
            pass
    return networks


def _is_ip_trusted(ip_str: str) -> bool:
    if not ip_str:
        return False
    if ip_str.lower() == "testclient" and "testclient" in config.TRUSTED_PROXIES.lower():
        return True
    if config.TRUSTED_PROXIES.strip() == "*":
        return True
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for net in _get_trusted_networks():
        if ip in net:
            return True
    return False


def resolve_client_ip(request: Request) -> str:
    """Safely extracts true client IP by traversing X-Forwarded-For right-to-left through trusted proxies."""
    peer_ip = request.client.host if request.client else "unknown"
    if peer_ip == "unknown" or not _is_ip_trusted(peer_ip):
        return peer_ip

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer_ip

    candidates = [p.strip() for p in forwarded.split(",") if p.strip()]
    client = peer_ip
    for candidate in reversed(candidates):
        try:
            ipaddress.ip_address(candidate)
            client = candidate
            if not _is_ip_trusted(candidate):
                break
        except ValueError:
            break
    return client


def client_ip(request: Request) -> str:
    return getattr(request.state, "client_ip", None) or resolve_client_ip(request)


def _inet(value: str) -> str | None:
    try:
        ipaddress.ip_address(value)
        return value
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Auth:
    user_id: uuid.UUID
    seller_id: uuid.UUID
    email: str
    session_id: uuid.UUID
    token: str


def get_auth(request: Request, db: Session = Depends(get_db)) -> Auth:
    """Every protected route must depend on this. It raises 401 itself, so it cannot be forgotten."""
    token = request.cookies.get(config.COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = db.execute(
        select(Sessions).where(Sessions.token_hash == hash_token(token))
    ).scalar_one_or_none()
    if session is None or session.expires_at <= utcnow():
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get(Users, session.user_id)
    seller = db.get(Sellers, session.seller_id)
    if not (user and user.is_active and seller and seller.is_active):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return Auth(user.id, seller.id, user.email, session.id, token)


def csrf_protect(request: Request, auth: Auth = Depends(get_auth)) -> Auth:
    """Use on every state-changing authenticated route (header X-CSRF-Token)."""
    supplied = request.headers.get("x-csrf-token", "")
    expected = csrf_token_for(auth.token)
    if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    return auth
