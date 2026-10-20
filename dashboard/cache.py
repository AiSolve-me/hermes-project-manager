"""
In-Memory Cache with TTL
========================

Lightweight thread-safe cache for frequently accessed data.
No external dependencies — pure Python.

Usage:
    from cache import cache

    # Set with TTL (seconds)
    cache.set("channels", data, ttl=300)

    # Get
    data = cache.get("channels")

    # Delete / invalidate
    cache.delete("channels")
    cache.delete_pattern("videos:*")

    # Clear all
    cache.clear()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


class _CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl: Optional[int] = None):
        self.value = value
        self.expires_at = time.time() + ttl if ttl else None

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


class Cache:
    """Thread-safe in-memory cache with optional TTL."""

    def __init__(self):
        self._store: dict[str, _CacheEntry] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Core ops
    # ------------------------------------------------------------------

    def get(self, key: str) -> Any:
        """Retrieve a value. Returns None if missing or expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.is_expired():
                del self._store[key]
                return None
            return entry.value

    def set(self, key: str, value: Any, ttl: Optional[int] = 60) -> None:
        """Store a value with optional TTL (seconds)."""
        with self._lock:
            self._store[key] = _CacheEntry(value, ttl)

    def delete(self, key: str) -> bool:
        """Delete a single key. Returns True if it existed."""
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def delete_pattern(self, pattern: str) -> int:
        """Delete keys containing a substring. Returns count deleted."""
        with self._lock:
            to_delete = [k for k in self._store if pattern in k]
            for k in to_delete:
                del self._store[k]
            return len(to_delete)

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._store.clear()

    def keys(self) -> list[str]:
        """Return all non-expired keys."""
        with self._lock:
            now = time.time()
            alive = []
            expired = []
            for k, entry in self._store.items():
                if entry.expires_at and now > entry.expires_at:
                    expired.append(k)
                else:
                    alive.append(k)
            for k in expired:
                del self._store[k]
            return alive

    def stats(self) -> dict[str, Any]:
        """Return cache statistics."""
        with self._lock:
            total = len(self._store)
            expired = sum(1 for e in self._store.values() if e.is_expired())
            return {
                "total_keys": total,
                "expired_keys": expired,
                "active_keys": total - expired,
            }

    # ------------------------------------------------------------------
    # Decorator — cache function results
    # ------------------------------------------------------------------

    def cached(self, key_fn: Callable[..., str], ttl: Optional[int] = None):
        """Decorator that caches a function's result.

        Args:
            key_fn: Callable that receives the same *args, **kwargs and
                    returns a cache key string.
            ttl: Time-to-live in seconds.
        """

        def decorator(fn: Callable) -> Callable:
            def wrapper(*args, **kwargs):
                key = key_fn(*args, **kwargs)
                cached = self.get(key)
                if cached is not None:
                    return cached
                result = fn(*args, **kwargs)
                self.set(key, result, ttl=ttl)
                return result

            return wrapper

        return decorator

    # ------------------------------------------------------------------
    # Convenience: get-or-compute
    # ------------------------------------------------------------------

    def get_or_set(
        self, key: str, factory: Callable[[], Any], ttl: Optional[int] = None
    ) -> Any:
        """Get cached value or compute, store, and return it."""
        cached = self.get(key)
        if cached is not None:
            return cached
        value = factory()
        self.set(key, value, ttl=ttl)
        return value


# Global singleton instance
cache = Cache()
