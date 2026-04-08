"""
Gemini API rate limiter — simple token-bucket for direct LLM extraction calls.
Browser-use ChatGoogle handles its own retries (max_retries=5).
Browser Use Cloud handles its own rate limiting for company/person agents.
"""

import asyncio
import logging
import os
import time

logger = logging.getLogger("fdd-agent")

# Gemini Tier 1: 300 RPM for Flash. We default to 200 to leave headroom
# for concurrent browser-use ChatGoogle calls.
GEMINI_RPM_LIMIT = int(os.getenv("GEMINI_RPM_LIMIT", "200"))


class RateLimiter:
    """Simple token-bucket rate limiter for Gemini extraction calls."""

    def __init__(self, rpm: int):
        self.rate = rpm
        self.max_tokens = float(rpm)
        self.tokens = float(rpm)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        logger.info(f"Rate limiter initialized: {rpm} RPM")

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


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(GEMINI_RPM_LIMIT)
    return _limiter
