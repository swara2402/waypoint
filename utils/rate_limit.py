"""In-process and Redis-backed sliding-window rate limiting.

Enforces ``settings.investigation_rate_limit`` (e.g. ``"10/minute"``) per
caller identity.  ``SlidingWindowRateLimiter`` is single-process (matching the
in-process concurrency semaphore); ``RedisRateLimiter`` provides a shared
window across replicas when ``settings.redis_url`` is set.  If the URL is
empty, the in-process limiter is used.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional, Tuple

_UNIT_SECONDS = {
    "second": 1.0,
    "sec": 1.0,
    "s": 1.0,
    "minute": 60.0,
    "min": 60.0,
    "m": 60.0,
    "hour": 3600.0,
    "h": 3600.0,
}


def parse_rate(spec: str) -> Tuple[int, float]:
    """Parse ``"10/minute"`` into ``(limit, window_seconds)``.

    Returns ``(0, 0.0)`` when the spec is empty or malformed, which callers
    treat as "unlimited".
    """
    if not spec or "/" not in spec:
        return 0, 0.0
    count_s, _, unit = spec.partition("/")
    unit = unit.strip().lower()
    try:
        limit = int(count_s.strip())
    except ValueError:
        return 0, 0.0
    window = _UNIT_SECONDS.get(unit)
    if window is None or limit <= 0:
        return 0, 0.0
    return limit, window


class SlidingWindowRateLimiter:
    """Async-safe sliding-window limiter keyed by caller identity."""

    def __init__(self, spec: str) -> None:
        self.limit, self.window = parse_rate(spec)
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self.window > 0

    async def check(self, key: str) -> Tuple[bool, float]:
        """Record an attempt for ``key``.

        Returns ``(allowed, retry_after_seconds)``.  When disabled, always
        allows and reports ``0.0``.
        """
        if not self.enabled:
            return True, 0.0

        now = time.monotonic()
        cutoff = now - self.window
        async with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(0.0, bucket[0] + self.window - now)
                return False, retry_after
            bucket.append(now)
            return True, 0.0

    async def reset(self) -> None:
        async with self._lock:
            self._hits.clear()


class RedisRateLimiter:
    """Shared sliding-window limiter backed by a Redis sorted set.

    Enables consistent limits across multiple API replicas.  Requires the
    optional ``redis`` dependency and a reachable ``settings.redis_url``.
    """

    def __init__(
        self,
        spec: str,
        redis_url: str,
        key_prefix: str = "waypoint:rl:",
        client: Optional[Any] = None,
    ) -> None:
        self.limit, self.window = parse_rate(spec)
        self._url = redis_url
        self._prefix = key_prefix
        self._client = client

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self.window > 0 and bool(self._url or self._client)

    def _get_client(self) -> Any:
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(
                self._url, encoding="utf-8", decode_responses=True
            )
        return self._client

    async def check(self, key: str) -> Tuple[bool, float]:
        if not self.enabled:
            return True, 0.0

        client = self._get_client()
        redis_key = f"{self._prefix}{key}"
        now = time.time()
        cutoff = now - self.window

        # Keep prune/count/add in one Redis-side operation. A pipeline alone
        # allows concurrent replicas to observe the same count before ZADD.
        script = """
        redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
        local count = redis.call('ZCARD', KEYS[1])
        if count >= tonumber(ARGV[3]) then
          local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
          if #oldest > 0 then
            return {0, tonumber(oldest[2])}
          end
          return {0, tonumber(ARGV[2])}
        end
        redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
        redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[3])) + 1)
        return {1, 0}
        """
        member = f"{now}:{uuid.uuid4().hex}"
        result = await client.eval(
            script, 1, redis_key, cutoff, now, self.limit, member
        )
        if not bool(int(result[0])):
            retry_after = max(0.0, float(result[1]) + self.window - now)
            return False, retry_after
        return True, 0.0

    async def reset(self) -> None:
        if self._client is None:
            return
        keys = await self._client.keys(f"{self._prefix}*")
        if keys:
            await self._client.delete(*keys)


def build_rate_limiter(spec: str, redis_url: str) -> Any:
    """Return a Redis-backed limiter when a URL is configured, else in-process."""
    if redis_url:
        return RedisRateLimiter(spec, redis_url)
    return SlidingWindowRateLimiter(spec)


class RedisConcurrencyLimiter:
    """Distributed investigation slots backed by short-lived Redis keys."""

    def __init__(self, limit: int, redis_url: str, lease_seconds: int = 300) -> None:
        self.limit = max(1, int(limit))
        self.lease_seconds = max(5, int(lease_seconds))
        self._url = redis_url
        self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            import redis.asyncio as aioredis
            self._client = aioredis.from_url(
                self._url, encoding="utf-8", decode_responses=True
            )
        return self._client

    async def acquire(self, owner: str) -> str | None:
        if not self._url:
            return None
        client = self._get_client()
        for slot in range(self.limit):
            key = f"waypoint:investigation-slot:{slot}"
            if await client.set(key, owner, nx=True, ex=self.lease_seconds):
                return key
        return None

    async def release(self, slot_key: str | None, owner: str) -> None:
        if not slot_key:
            return
        client = self._get_client()
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
          return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        await client.eval(script, 1, slot_key, owner)
