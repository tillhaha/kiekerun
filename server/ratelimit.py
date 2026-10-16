"""In-memory rate limiting for the backend (single process, which is how the Docker app runs).

FailLimiter   locks an IP out after too many failed logins / wrong connector tokens.
RateLimiter   caps requests per IP per window (protects the intervals.icu API from loops).
State lives in memory, so a restart clears it; that is fine for brute-force protection.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable

MAX_KEYS = 10_000  # bound memory if someone sprays from many addresses


class FailLimiter:
    def __init__(self, max_fails: int = 10, window_s: float = 900, lockout_s: float = 900,
                 clock: Callable[[], float] = time.monotonic):
        self.max_fails, self.window_s, self.lockout_s, self.clock = max_fails, window_s, lockout_s, clock
        self._fails: dict[str, deque] = {}
        self._locked: dict[str, float] = {}

    def retry_after(self, key: str) -> int:
        """Seconds until the key may try again; 0 = allowed."""
        until = self._locked.get(key)
        if until is None:
            return 0
        left = until - self.clock()
        if left <= 0:
            self._locked.pop(key, None)
            self._fails.pop(key, None)
            return 0
        return max(1, math.ceil(left))

    def fail(self, key: str) -> None:
        now = self.clock()
        if len(self._fails) >= MAX_KEYS:
            self._prune(now)
        q = self._fails.setdefault(key, deque())
        q.append(now)
        while q and now - q[0] > self.window_s:
            q.popleft()
        if len(q) >= self.max_fails:
            self._locked[key] = now + self.lockout_s

    def ok(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked.pop(key, None)

    def reset(self) -> None:
        self._fails.clear()
        self._locked.clear()

    def _prune(self, now: float) -> None:
        for k in [k for k, q in self._fails.items() if not q or now - q[-1] > self.window_s]:
            self._fails.pop(k, None)
        for k in [k for k, t in self._locked.items() if t <= now]:
            self._locked.pop(k, None)
        if len(self._fails) >= MAX_KEYS:  # still full: drop the oldest half
            for k in list(self._fails)[: MAX_KEYS // 2]:
                self._fails.pop(k, None)
                self._locked.pop(k, None)


class RateLimiter:
    def __init__(self, limit: int = 60, window_s: float = 60, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self._hits: dict[str, deque] = {}

    def hit(self, key: str) -> int:
        """Counts a request. Returns 0 if allowed, else seconds to wait."""
        now = self.clock()
        if len(self._hits) >= MAX_KEYS:
            for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window_s]:
                self._hits.pop(k, None)
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            return max(1, math.ceil(self.window_s - (now - q[0])))
        q.append(now)
        return 0

    def reset(self) -> None:
        self._hits.clear()
