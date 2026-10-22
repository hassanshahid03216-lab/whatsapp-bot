import os

COOKIE_NAME = "sb_session"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").strip().lower() not in ("0", "false", "no")
SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "168"))
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
TRUSTED_PROXIES = os.environ.get(
    "TRUSTED_PROXIES",
    "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,testclient",
).strip()

LOGIN_EMAIL_MAX_ATTEMPTS = 5
LOGIN_IP_MAX_ATTEMPTS = 20
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCKOUT_SECONDS = 15 * 60
SIGNUP_IP_MAX_ATTEMPTS = 10
SIGNUP_WINDOW_SECONDS = 60 * 60


def secret_key() -> str:
    key = os.environ.get("SECRET_KEY", "")
    if len(key) < 32 or key.lower().startswith(("change", "your-", "secret")):
        raise RuntimeError("SECRET_KEY must be a random string of at least 32 characters")
    return key