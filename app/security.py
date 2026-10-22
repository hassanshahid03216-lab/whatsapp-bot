import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app import config

_ph = PasswordHasher()  # argon2id, library defaults
_DUMMY_HASH = _ph.hash("dummy-password-for-timing-only")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Always runs one argon2 verification, even for unknown users (timing)."""
    try:
        matched = _ph.verify(stored_hash or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return bool(matched) and stored_hash is not None


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token_for(session_token: str) -> str:
    return hmac.new(
        config.secret_key().encode(), b"csrf:" + session_token.encode(), hashlib.sha256
    ).hexdigest()