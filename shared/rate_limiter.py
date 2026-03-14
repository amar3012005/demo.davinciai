"""
Rate Limiting Middleware for TARA Services

Uses Redis for distributed rate limiting with sliding window algorithm.
"""

import time
import logging
from typing import Optional, Callable
from fastapi import Request, HTTPException, WebSocket
from fastapi.middleware.base import BaseHTTPMiddleware
import redis.asyncio as redis
import os

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Redis-backed rate limiter using sliding window algorithm.
    """

    def __init__(
        self,
        redis_client: Optional[redis.Redis] = None,
        default_requests: int = 100,
        default_window: int = 60,  # seconds
        key_prefix: str = "ratelimit"
    ):
        self.redis = redis_client
        self.default_requests = default_requests
        self.default_window = default_window
        self.key_prefix = key_prefix
        self._local_cache: dict = {}  # Fallback if Redis unavailable

    def _get_key(self, identifier: str, endpoint: str) -> str:
        """Generate rate limit key."""
        return f"{self.key_prefix}:{identifier}:{endpoint}"

    def _get_identifier(self, request: Request) -> str:
        """Extract identifier from request (IP + optional session)."""
        # Get client IP
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = request.client.host if request.client else "unknown"

        # Add session ID if available
        session_id = request.query_params.get("session_id") or \
                     request.headers.get("X-Session-ID", "")

        if session_id:
            return f"{client_ip}:{session_id}"
        return client_ip

    async def is_allowed(
        self,
        identifier: str,
        endpoint: str,
        max_requests: Optional[int] = None,
        window: Optional[int] = None
    ) -> tuple[bool, dict]:
        """
        Check if request is allowed under rate limit.

        Returns: (allowed: bool, info: dict)
        """
        max_req = max_requests or self.default_requests
        win = window or self.default_window
        key = self._get_key(identifier, endpoint)
        now = time.time()

        # If no Redis, use in-memory (per-instance only)
        if not self.redis:
            return self._check_local(key, now, max_req, win)

        try:
            # Use Redis sorted set for sliding window
            pipe = self.redis.pipeline()

            # Remove entries outside the window
            pipe.zremrangebyscore(key, 0, now - win)

            # Count current entries
            pipe.zcard(key)

            # Add current request
            pipe.zadd(key, {str(now): now})

            # Set expiry on the key
            pipe.expire(key, win)

            results = await pipe.execute()
            current_count = results[1]

            # Check if over limit (current_count includes the new request)
            if current_count > max_req:
                # Remove the request we just added
                await self.redis.zrem(key, str(now))
                return False, {
                    "limit": max_req,
                    "remaining": 0,
                    "reset_time": int(now + win),
                    "window": win
                }

            return True, {
                "limit": max_req,
                "remaining": max_req - current_count,
                "reset_time": int(now + win),
                "window": win
            }

        except Exception as e:
            logger.warning(f"Rate limiter Redis error: {e}, allowing request")
            return True, {"limit": max_req, "remaining": max_req, "window": win}

    def _check_local(self, key: str, now: float, max_req: int, win: int) -> tuple[bool, dict]:
        """In-memory fallback when Redis unavailable."""
        if key not in self._local_cache:
            self._local_cache[key] = []

        # Clean old entries
        self._local_cache[key] = [
            ts for ts in self._local_cache[key]
            if ts > now - win
        ]

        current_count = len(self._local_cache[key])

        if current_count >= max_req:
            return False, {
                "limit": max_req,
                "remaining": 0,
                "reset_time": int(now + win),
                "window": win
            }

        self._local_cache[key].append(now)
        return True, {
            "limit": max_req,
            "remaining": max_req - current_count - 1,
            "reset_time": int(now + win),
            "window": win
        }


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware for HTTP rate limiting.
    """

    def __init__(
        self,
        app,
        redis_url: Optional[str] = None,
        default_requests: int = 100,
        default_window: int = 60,
        exempt_paths: Optional[list] = None
    ):
        super().__init__(app)
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://redis:6379/0")
        self.exempt_paths = exempt_paths or ["/health", "/metrics", "/"]
        self.limiter = None
        self.default_requests = default_requests
        self.default_window = default_window

    async def dispatch(self, request: Request, call_next: Callable):
        """Process request with rate limiting."""
        path = request.url.path

        # Skip rate limiting for exempt paths
        if any(path.startswith(exempt) for exempt in self.exempt_paths):
            return await call_next(request)

        # Initialize limiter on first use
        if not self.limiter:
            try:
                redis_client = redis.from_url(self.redis_url, decode_responses=True)
                self.limiter = RateLimiter(
                    redis_client=redis_client,
                    default_requests=self.default_requests,
                    default_window=self.default_window
                )
            except Exception as e:
                logger.error(f"Failed to initialize rate limiter: {e}")
                # Allow request if rate limiter fails
                return await call_next(request)

        # Check rate limit
        identifier = self.limiter._get_identifier(request)
        allowed, info = await self.limiter.is_allowed(identifier, path)

        if not allowed:
            logger.warning(f"Rate limit exceeded for {identifier} on {path}")
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "Rate limit exceeded",
                    "retry_after": info["reset_time"] - int(time.time()),
                    "limit": info["limit"],
                    "window": info["window"]
                }
            )

        # Process request and add rate limit headers
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(info["limit"])
        response.headers["X-RateLimit-Remaining"] = str(info["remaining"])
        response.headers["X-RateLimit-Reset"] = str(info["reset_time"])

        return response


class WebSocketRateLimiter:
    """
    Rate limiter for WebSocket connections.
    Tracks connections per IP and messages per session.
    """

    def __init__(
        self,
        redis_client: Optional[redis.Redis] = None,
        max_connections_per_ip: int = 10,
        max_messages_per_minute: int = 60
    ):
        self.redis = redis_client
        self.max_connections_per_ip = max_connections_per_ip
        self.max_messages_per_minute = max_messages_per_minute
        self._connections: dict = {}  # ip -> count

    def can_connect(self, client_ip: str) -> bool:
        """Check if new WebSocket connection is allowed."""
        current = self._connections.get(client_ip, 0)
        if current >= self.max_connections_per_ip:
            logger.warning(f"WebSocket connection limit exceeded for {client_ip}")
            return False
        self._connections[client_ip] = current + 1
        return True

    def disconnect(self, client_ip: str):
        """Decrement connection count."""
        if client_ip in self._connections:
            self._connections[client_ip] = max(0, self._connections[client_ip] - 1)

    async def check_message_rate(self, session_id: str) -> bool:
        """Check if message is within rate limit."""
        if not self.redis:
            return True

        key = f"ws_msg_rate:{session_id}"
        now = time.time()

        try:
            pipe = self.redis.pipeline()
            pipe.zremrangebyscore(key, 0, now - 60)
            pipe.zcard(key)
            pipe.zadd(key, {str(now): now})
            pipe.expire(key, 60)
            results = await pipe.execute()

            return results[1] < self.max_messages_per_minute
        except Exception:
            return True


def get_rate_limit_headers(info: dict) -> dict:
    """Generate rate limit header dict for responses."""
    return {
        "X-RateLimit-Limit": str(info["limit"]),
        "X-RateLimit-Remaining": str(info["remaining"]),
        "X-RateLimit-Reset": str(info["reset_time"])
    }
