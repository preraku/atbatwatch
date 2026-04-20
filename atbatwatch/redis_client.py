import redis.asyncio as aioredis


def make_redis(redis_url: str) -> aioredis.Redis:
    return aioredis.from_url(redis_url, decode_responses=True)
