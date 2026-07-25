"""Per-IP rate limiter with exponential backoff.

Tracks failed login attempts per client IP in a sliding time window and
imposes an exponentially increasing delay before allowing the next attempt.
The delay resets to zero on a successful login or when the sliding window
empties (i.e. the IP stops hammering the endpoint).

The rate limiter is purely in-memory - no persistent state, no database
writes, no inter-process coordination. On restart the slate is clean,
which is fine: an attacker cannot pre-seed state from a previous run.

Thread safety
-------------
All public methods acquire ``_lock``.  The lock is *not* re-entrant so no
public method calls another public method.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class IPRateLimiter:
    """Per-IP exponential-backoff rate limiter.

    Parameters
    ----------
    max_attempts
        Number of failed attempts allowed in *window_seconds* before
        backoff begins.  Default 5 (matches the auth service's
        ``max_login_attempts`` default).
    window_seconds
        Sliding time window for counting attempts.  Older attempts are
        discarded.  Default 60.
    base_delay
        Initial backoff delay in seconds (applied on the first excess
        attempt).  Default 2.0.
    max_delay
        Maximum backoff delay in seconds (capped so a burst of attempts
        does not produce an absurd wait).  Default 300.0 (5 minutes).

    Backoff formula
    ---------------
    ``delay = min(base_delay * 2 ** excess, max_delay)``

    where *excess* = (total failed attempts in the window) - *max_attempts*.
    The first excess attempt gets *base_delay* seconds, the second gets
    ``base_delay * 2``, the third ``base_delay * 4``, etc.

    The caller must wait *delay* seconds since their **last** attempt.
    If that time has already passed the remaining wait is 0.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 60.0,
        base_delay: float = 2.0,
        max_delay: float = 300.0,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.base_delay = base_delay
        self.max_delay = max_delay
        # ip_address -> [attempt_timestamp, ...]  (newest appended)
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_wait_seconds(self, ip: str) -> float:
        """Return the number of seconds the caller of *ip* should wait
        before attempting again.  0 means the request can proceed."""
        with self._lock:
            self._evict_stale(ip)
            attempts = self._attempts.get(ip, [])
            if len(attempts) < self.max_attempts:
                return 0.0
            excess = len(attempts) - self.max_attempts  # 0-indexed
            delay = min(self.base_delay * (2 ** excess), self.max_delay)
            elapsed = time.time() - attempts[-1]
            return max(0.0, delay - elapsed)

    def is_rate_limited(self, ip: str) -> bool:
        """Convenience: ``True`` iff *ip* must wait before the next attempt."""
        return self.get_wait_seconds(ip) > 0

    def record_failure(self, ip: str) -> int:
        """Record a failed attempt from *ip*.

        Returns the total number of failed attempts for *ip* within the
        sliding window (useful for logging / diagnostics).
        """
        now = time.time()
        with self._lock:
            self._evict_stale(ip)
            self._attempts.setdefault(ip, []).append(now)
            return len(self._attempts[ip])

    def record_success(self, ip: str) -> None:
        """Reset the failure counter for *ip* on a successful login."""
        with self._lock:
            self._attempts.pop(ip, None)

    def state(self) -> dict[str, Any]:
        """Return a snapshot of internal state for diagnostics / tests.

        Returns a dict mapping each tracked IP to ``{'attempts': count,
        'oldest': iso_timestamp, 'newest': iso_timestamp}``.
        """
        now = time.time()
        with self._lock:
            self._evict_stale(None)
            snapshot: dict[str, Any] = {}
            for ip, timestamps in self._attempts.items():
                snapshot[ip] = {
                    'attempts': len(timestamps),
                    'oldest': _ts_to_iso(timestamps[0]) if timestamps else None,
                    'newest': _ts_to_iso(timestamps[-1]) if timestamps else None,
                }
            return snapshot

    def apply_config(self, config: dict[str, Any]) -> None:
        """Update rate-limiter parameters at runtime from a config dict.

        Accepted keys (with defaults):

        - ``rate_limit_max_attempts`` (5)
        - ``rate_limit_window_seconds`` (60)
        - ``rate_limit_base_delay`` (2.0)
        - ``rate_limit_max_delay`` (300.0)

        Missing keys leave the current value unchanged.  Values are
        coerced to the expected types so callers can pass values from
        the auth-settings endpoint without pre-processing.
        """
        if 'rate_limit_max_attempts' in config:
            self.max_attempts = int(config['rate_limit_max_attempts'])
        if 'rate_limit_window_seconds' in config:
            self.window_seconds = float(config['rate_limit_window_seconds'])
        if 'rate_limit_base_delay' in config:
            self.base_delay = float(config['rate_limit_base_delay'])
        if 'rate_limit_max_delay' in config:
            self.max_delay = float(config['rate_limit_max_delay'])

    def clear(self) -> None:
        """Reset all state (used in tests)."""
        with self._lock:
            self._attempts.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_stale(self, ip: str | None) -> None:
        """Remove attempts outside the sliding window for *ip* (or all IPs
        when *ip* is ``None``).  Caller must hold ``_lock``."""
        cutoff = time.time() - self.window_seconds
        if ip is not None:
            timestamps = self._attempts.get(ip)
            if timestamps is None:
                return
            keep = [t for t in timestamps if t > cutoff]
            if keep:
                self._attempts[ip] = keep
            else:
                del self._attempts[ip]
            return
        # Global eviction
        stale_ips = [k for k, v in self._attempts.items() if all(t <= cutoff for t in v)]
        for k in stale_ips:
            del self._attempts[k]


# Module-level singleton - imported by auth_router directly.
login_limiter = IPRateLimiter()


class SlidingWindowRateLimiter:
    """Plain sliding-window counter. No exponential backoff, no penalty beyond drop.

    Round-5 (M1 + M3): a primitive API/endpoint throttling limiter that
    intentionally differs from :class:`IPRateLimiter` (which is an
    exponential-backoff limiter designed for *failed* authentication
    attempts). The sliding-window shape fits the use case:

      * admin mutations (M1): a stolen admin cookie should not be able
        to whale create/delete/write endpoints at wire speed -- an
        honest admin might legitimately issue a string of POSTs in 30
        seconds; a sliding-window cap of e.g. 60 per minute throttles a
        burst without penalising an active admin the way an exponential
        backoff would.
      * setup endpoint brute-force (M3): same shape -- 10 setup POSTs
        per 5 minutes, drop the rest.

    NOT thread-affine-friendly for large key fan-out (in-process only)
    but that matches Daygle's single-writer deployment shape.
    """

    def __init__(self, *, max_requests: int, window_seconds: float, name: str = 'sw') -> None:
        self._max = int(max_requests)
        self._window = float(window_seconds)
        self._name = name
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def is_rate_limited(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            self._evict_stale(key, now)
            dq = self._hits.get(key)
            return dq is not None and len(dq) >= self._max

    def record(self, key: str) -> int:
        """Insert a hit for *key* and return the current hit-count in-window."""
        with self._lock:
            now = time.monotonic()
            self._evict_stale(key, now)
            dq = self._hits.setdefault(key, deque())
            dq.append(now)
            return len(dq)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def _evict_stale(self, key: str, now: float) -> None:
        dq = self._hits.get(key)
        if not dq:
            return
        cutoff = now - self._window
        while dq and dq[0] < cutoff:
            dq.popleft()
        if not dq:
            self._hits.pop(key, None)


# Round-5 / M1: admin endpoint throttling, default 60 requests / 60 sec
# per-IP. Tunable via auth.admin_rate_limit_* config keys (see
# ``apply_config`` below).
admin_limiter = SlidingWindowRateLimiter(
    max_requests=60, window_seconds=60, name='admin',
)

# Round-5 / M3: setup-endpoint brute-force throttle, 10 POSTs per 5
# minutes per IP. Reasonable for LAN first-admin bootstrap (typos,
# password recalculation) while still bounding the brute-force window.
setup_limiter = SlidingWindowRateLimiter(
    max_requests=10, window_seconds=300, name='setup',
)


def _ts_to_iso(ts: float) -> str:
    """Format a Unix timestamp as an ISO-8601 string for diagnostics."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
