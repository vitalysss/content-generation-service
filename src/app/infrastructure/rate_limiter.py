from dataclasses import dataclass
from uuid import UUID

from redis.asyncio import Redis

RATE_LIMIT_SCRIPT = """
local blocked_ttl = redis.call('TTL', KEYS[2])
if blocked_ttl > 0 then
    return {0, blocked_ttl}
end

local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
end

if count > tonumber(ARGV[1]) then
    redis.call('SET', KEYS[2], '1', 'EX', ARGV[3])
    redis.call('DEL', KEYS[1])
    return {0, tonumber(ARGV[3])}
end

return {1, redis.call('TTL', KEYS[1])}
"""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int


class RedisRateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        limit: int,
        window_seconds: int,
        block_seconds: int,
    ) -> None:
        self._redis = redis
        self._limit = limit
        self._window_seconds = window_seconds
        self._block_seconds = block_seconds

    async def check(self, user_id: UUID) -> RateLimitDecision:
        prefix = f"rate_limit:user:{user_id}"
        result = await self._redis.eval(
            RATE_LIMIT_SCRIPT,
            2,
            f"{prefix}:window",
            f"{prefix}:blocked",
            self._limit,
            self._window_seconds,
            self._block_seconds,
        )
        allowed, retry_after = int(result[0]), int(result[1])
        return RateLimitDecision(bool(allowed), max(retry_after, 1))
