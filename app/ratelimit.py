import threading
import time
import uuid
from collections import deque
from typing import Optional

try:
    import redis
except ImportError:
    redis = None

from app import config

_MAX_KEYS = 50_000

# Lua script to perform sliding-window rate limiting with lockout atomically in Redis.
_SLIDING_WINDOW_LUA = """
local hits_key = KEYS[1]
local lockout_key = KEYS[2]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_attempts = tonumber(ARGV[3])
local lockout = tonumber(ARGV[4])
local member = ARGV[5]

-- 1. Check if the key is currently locked out
if redis.call('EXISTS', lockout_key) == 1 then
    return 0
end

-- 2. Prune timestamps outside the sliding window
local cutoff = now - window
redis.call('ZREMRANGEBYSCORE', hits_key, '-inf', cutoff)

-- 3. Check current hit count
local count = redis.call('ZCARD', hits_key)
if count >= max_attempts then
    if lockout > 0 then
        redis.call('SETEX', lockout_key, lockout, '1')
    end
    redis.call('DEL', hits_key)
    return 0
end

-- 4. Record current attempt
redis.call('ZADD', hits_key, now, member)
local ttl = math.max(window, lockout) + 60
redis.call('EXPIRE', hits_key, ttl)
return 1
"""


class InMemoryRateLimiter:
    """Sliding window + lockout in memory (fallback when Redis is not configured)."""

    def __init__(self, max_attempts: int, window_seconds: int, lockout_seconds: int = 0, clock=time.monotonic):
        self._max = max_attempts
        self._window = window_seconds
        self._lockout = lockout_seconds
        self._clock = clock
        self._hits: dict[str, deque] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            if len(self._hits) > _MAX_KEYS:
                self._prune(now)
            until = self._locked_until.get(key)
            if until is not None:
                if now < until:
                    return False
                del self._locked_until[key]
            hits = self._hits.setdefault(key, deque())
            cutoff = now - self._window
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._max:
                if self._lockout > 0:
                    self._locked_until[key] = now + self._lockout
                hits.clear()
                return False
            hits.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
            self._locked_until.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()
            self._locked_until.clear()

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        for key in list(self._hits):
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if not hits:
                del self._hits[key]
        for key in list(self._locked_until):
            if self._locked_until[key] <= now:
                del self._locked_until[key]


class RedisRateLimiter:
    """Distributed sliding window rate limiter backed by Redis with atomic Lua script."""

    def __init__(
        self,
        prefix: str,
        max_attempts: int,
        window_seconds: int,
        lockout_seconds: int = 0,
        redis_url: Optional[str] = None,
    ):
        self.prefix = prefix
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._redis_url = redis_url
        self._redis_client: Optional[redis.Redis] = None
        self._script = None
        self._fallback = InMemoryRateLimiter(max_attempts, window_seconds, lockout_seconds)

    def _get_client(self) -> Optional["redis.Redis"]:
        if redis is None:
            return None
        url = self._redis_url or config.REDIS_URL
        if not url:
            return None
        if self._redis_client is None:
            try:
                self._redis_client = redis.from_url(url, decode_responses=True)
                self._script = self._redis_client.register_script(_SLIDING_WINDOW_LUA)
            except Exception:
                self._redis_client = None
                self._script = None
        return self._redis_client

    def _hits_key(self, key: str) -> str:
        return f"rl:{self.prefix}:hits:{key}"

    def _lockout_key(self, key: str) -> str:
        return f"rl:{self.prefix}:lockout:{key}"

    def allow(self, key: str) -> bool:
        client = self._get_client()
        if client is None or self._script is None:
            return self._fallback.allow(key)
        now = time.time()
        member = f"{now}:{uuid.uuid4().hex}"
        try:
            result = self._script(
                keys=[self._hits_key(key), self._lockout_key(key)],
                args=[now, self.window_seconds, self.max_attempts, self.lockout_seconds, member],
            )
            return bool(result == 1)
        except Exception:
            # Fall back safely if Redis temporarily disconnects
            return self._fallback.allow(key)

    def reset(self, key: str) -> None:
        self._fallback.reset(key)
        client = self._get_client()
        if client is not None:
            try:
                client.delete(self._hits_key(key), self._lockout_key(key))
            except Exception:
                pass

    def clear(self) -> None:
        self._fallback.clear()
        client = self._get_client()
        if client is not None:
            try:
                pattern = f"rl:{self.prefix}:*"
                cursor = 0
                while True:
                    cursor, keys = client.scan(cursor=cursor, match=pattern, count=100)
                    if keys:
                        client.delete(*keys)
                    if cursor == 0:
                        break
            except Exception:
                pass


login_ip_limiter = RedisRateLimiter(
    prefix="login_ip",
    max_attempts=config.LOGIN_IP_MAX_ATTEMPTS,
    window_seconds=config.LOGIN_WINDOW_SECONDS,
    lockout_seconds=config.LOGIN_LOCKOUT_SECONDS,
)

login_email_limiter = RedisRateLimiter(
    prefix="login_email",
    max_attempts=config.LOGIN_EMAIL_MAX_ATTEMPTS,
    window_seconds=config.LOGIN_WINDOW_SECONDS,
    lockout_seconds=config.LOGIN_LOCKOUT_SECONDS,
)

signup_ip_limiter = RedisRateLimiter(
    prefix="signup_ip",
    max_attempts=config.SIGNUP_IP_MAX_ATTEMPTS,
    window_seconds=config.SIGNUP_WINDOW_SECONDS,
    lockout_seconds=config.SIGNUP_WINDOW_SECONDS,
)


def reset_all() -> None:
    for limiter in (login_ip_limiter, login_email_limiter, signup_ip_limiter):
        limiter.clear()