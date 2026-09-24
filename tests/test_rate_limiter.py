from uuid import uuid4

import pytest

from app.infrastructure.rate_limiter import RedisRateLimiter


class RedisStub:
    def __init__(self, result: list[int]) -> None:
        self.result = result
        self.arguments: tuple | None = None

    async def eval(self, *arguments):
        self.arguments = arguments
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redis_result", "allowed", "retry_after"),
    [([1, 42], True, 42), ([0, 60], False, 60), ([1, 0], True, 1)],
)
async def test_redis_rate_limiter_decision(
    redis_result: list[int], allowed: bool, retry_after: int
) -> None:
    redis = RedisStub(redis_result)
    limiter = RedisRateLimiter(
        redis,  # type: ignore[arg-type]
        limit=10,
        window_seconds=60,
        block_seconds=60,
    )
    user_id = uuid4()

    decision = await limiter.check(user_id)

    assert decision.allowed is allowed
    assert decision.retry_after == retry_after
    assert redis.arguments is not None
    assert redis.arguments[1] == 2
    assert str(user_id) in redis.arguments[2]
    assert str(user_id) in redis.arguments[3]
