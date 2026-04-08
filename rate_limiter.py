"""
OpenAI API rate limiter — adaptive token-bucket for direct LLM calls only.
Browser Use Cloud handles its own rate limiting for browser agents.
"""

import asyncio
import logging
import os
import time

logger = logging.getLogger("fdd-agent")

OPENAI_RPM_LIMIT = int(os.getenv("OPENAI_RPM_LIMIT", "500"))


class AdaptiveRateLimiter:
    """Token-bucket rate limiter that adapts from OpenAI response headers."""

    def __init__(self, initial_rpm: int):
        self.rate = initial_rpm
        self.max_tokens = float(initial_rpm)
        self.tokens = float(initial_rpm)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._learned_limit: int | None = None
        logger.info(f"Rate limiter initialized: {initial_rpm} RPM (adaptive)")

    async def acquire(self):
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.max_tokens, self.tokens + elapsed * (self.rate / 60.0))
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
            wait = 60.0 / self.rate
            await asyncio.sleep(wait)

    def update_from_headers(self, headers: dict):
        """Adapt rate from OpenAI response headers."""
        try:
            # OpenAI uses: x-ratelimit-limit-requests, x-ratelimit-remaining-requests
            limit = int(headers.get("x-ratelimit-limit-requests", 0))
            remaining = int(headers.get("x-ratelimit-remaining-requests", -1))
            if limit > 0 and (self._learned_limit is None or limit != self._learned_limit):
                self._learned_limit = limit
                new_rate = int(limit * 0.8)
                if new_rate != self.rate:
                    logger.info(f"Rate limiter adapted: {self.rate} → {new_rate} RPM (API reports {limit} RPM)")
                    self.rate = new_rate
                    self.max_tokens = float(new_rate)
            if 0 <= remaining < 5:
                self.tokens = min(self.tokens, float(remaining))
        except (ValueError, TypeError):
            pass


_limiter: AdaptiveRateLimiter | None = None


def get_rate_limiter() -> AdaptiveRateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = AdaptiveRateLimiter(OPENAI_RPM_LIMIT)
    return _limiter
