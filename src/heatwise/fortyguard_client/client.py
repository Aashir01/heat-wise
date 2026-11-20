"""Async FortyGuard REST client (httpx).

Behaviour, all covered by offline tests:
- retries with exponential backoff and full jitter on transport errors and 5xx
- never retries 400/401/403/404/422/429; a 429 raises RateLimitError
- rate-limit awareness: reads x-ratelimit-* headers (per-minute windows were observed) and raises
  RateLimitError locally instead of sending a request into an exhausted window
- optional TTL cache for completed results (identical re-runs are charged again by the API)
- structured request logging; headers, keys and bodies are never logged
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, TypeVar

import httpx
import structlog
from pydantic import BaseModel

from .errors import (
    ActivityFailed,
    AuthError,
    FortyGuardError,
    NotFoundError,
    PlanAccessError,
    PollTimeout,
    QuotaError,
    RateLimitError,
    ServerError,
    TransportFailure,
    ValidationError,
)
from .models import (
    ApiErrorBody,
    EnvParamsResult,
    HeatmapResult,
    RoutingResult,
    StatusData,
    StatusResponse,
    SubmitResponse,
)

log = structlog.get_logger("fortyguard_client")

DEFAULT_BASE_URL = "https://api.fortyguard.com/v1"
T = TypeVar("T", bound=BaseModel)
_QUOTA_WORDS = re.compile(r"(?i)\b(credit|credits|quota|insufficient)\b")


@dataclass
class RateLimitState:
    limit: int | None = None
    remaining: int | None = None
    reset_epoch: int | None = None  # observed: epoch seconds of the next minute boundary

    def seconds_until_reset(self, now: float) -> float:
        return max(0.0, (self.reset_epoch or 0) - now)


class TTLCache:
    """Tiny in-memory TTL cache keyed by endpoint + canonical JSON payload."""

    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl_seconds
        self._clock = clock
        self._items: dict[str, tuple[float, Any]] = {}

    @staticmethod
    def key(endpoint: str, payload: dict[str, Any]) -> str:
        blob = json.dumps([endpoint, payload], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        stored_at, value = item
        if self._clock() - stored_at > self.ttl:
            del self._items[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._items[key] = (self._clock(), value)


class FortyGuardClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        cache_ttl: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.time,
        max_rate_limit_wait: float = 0.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self._rng = rng
        self._clock = clock
        self.max_rate_limit_wait = max_rate_limit_wait
        self.rate_limit: dict[str, RateLimitState] = {}  # keyed by "submit" / "status"
        self.cache = TTLCache(cache_ttl) if cache_ttl else None

    async def __aenter__(self) -> FortyGuardClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- low level -----------------------------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        return float(self._rng() * min(self.backoff_cap, self.backoff_base * (2**attempt)))

    async def _guard_rate_limit(self, bucket: str) -> None:
        state = self.rate_limit.get(bucket)
        if state is None or state.remaining is None or state.remaining > 0:
            return
        wait = state.seconds_until_reset(self._clock())
        if wait <= 0:
            return
        if wait <= self.max_rate_limit_wait:
            log.info("rate_limit_wait", bucket=bucket, seconds=round(wait, 1))
            await self._sleep(wait)
            return
        raise RateLimitError("Local rate-limit window exhausted", retry_after=wait)

    def _record_rate_limit(self, bucket: str, resp: httpx.Response) -> None:
        h = resp.headers

        def _int(name: str) -> int | None:
            v = h.get(name)
            return int(v) if v and v.isdigit() else None

        if "x-ratelimit-remaining" in h:
            self.rate_limit[bucket] = RateLimitState(
                _int("x-ratelimit-limit"), _int("x-ratelimit-remaining"), _int("x-ratelimit-reset")
            )

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            body = ApiErrorBody.model_validate(resp.json())
        except (ValueError, TypeError):
            body = ApiErrorBody(message=resp.text[:200] or None)
        text, code, field = body.text, body.code, body.field
        status = resp.status_code
        if status == 429:
            ra = resp.headers.get("retry-after")
            retry_after = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
            if retry_after is None:
                reset = resp.headers.get("x-ratelimit-reset")
                if reset and reset.isdigit():
                    retry_after = max(0.0, int(reset) - self._clock())
            raise RateLimitError(text, retry_after=retry_after)
        if status in (400, 422):
            raise ValidationError(text, status_code=status, code=code, field=field)
        if status == 401:
            raise AuthError(text, status_code=status, code=code)
        if status == 403:
            raise PlanAccessError(text, status_code=status, code=code)
        if status == 402 or _QUOTA_WORDS.search(text):
            raise QuotaError(text, status_code=status, code=code)
        if status == 404:
            raise NotFoundError(text, status_code=status, code=code)
        raise ServerError(text, status_code=status, code=code)

    async def _request(
        self, method: str, path: str, *, bucket: str, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        last_exc: FortyGuardError | None = None
        for attempt in range(self.max_retries + 1):
            await self._guard_rate_limit(bucket)
            started = time.perf_counter()
            try:
                resp = await self._client.request(method, path, json=json_body)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                log.warning(
                    "request_transport_error",
                    method=method,
                    path=path,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
                last_exc = TransportFailure(f"{type(exc).__name__}: {exc}")
            else:
                self._record_rate_limit(bucket, resp)
                log.info(
                    "request",
                    method=method,
                    path=path,
                    status=resp.status_code,
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                    attempt=attempt,
                    ratelimit_remaining=resp.headers.get("x-ratelimit-remaining"),
                )
                if resp.status_code < 500:
                    self._raise_for_status(resp)
                    return resp
                try:
                    self._raise_for_status(resp)
                except ServerError as exc:
                    last_exc = exc
            if attempt < self.max_retries:
                await self._sleep(self._backoff_delay(attempt))
        assert last_exc is not None
        raise last_exc

    # ---- activities ----------------------------------------------------------------------

    async def submit(self, endpoint: str, payload: dict[str, Any]) -> str:
        """POST /<endpoint>; returns the activity_id."""
        resp = await self._request("POST", f"/{endpoint}", bucket="submit", json_body=payload)
        return SubmitResponse.model_validate(resp.json()).data.activity_id

    async def status(self, activity_id: str) -> StatusData:
        resp = await self._request("GET", f"/status/{activity_id}", bucket="status")
        return StatusResponse.model_validate(resp.json()).data

    async def wait(
        self,
        activity_id: str,
        *,
        max_polls: int = 60,
        interval: float = 5.0,
        not_found_grace: int = 3,
    ) -> StatusData:
        """Bounded polling. A 404 just after submission is tolerated `not_found_grace` times."""
        not_found = 0
        for _ in range(max_polls):
            try:
                data = await self.status(activity_id)
            except NotFoundError:
                not_found += 1
                if not_found > not_found_grace:
                    raise
                await self._sleep(interval)
                continue
            if data.is_failed:
                raise ActivityFailed(activity_id)
            if data.is_completed:
                return data
            await self._sleep(interval)
        raise PollTimeout(activity_id, max_polls)

    async def _run(
        self,
        endpoint: str,
        payload: dict[str, Any],
        model: type[T],
        *,
        poll_interval: float,
        max_polls: int,
        use_cache: bool,
    ) -> T:
        key = TTLCache.key(endpoint, payload)
        if use_cache and self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                log.info("cache_hit", endpoint=endpoint)
                return hit  # type: ignore[no-any-return]
        activity_id = await self.submit(endpoint, payload)
        data = await self.wait(activity_id, max_polls=max_polls, interval=poll_interval)
        result = model.model_validate(data.result or {})
        if use_cache and self.cache is not None:
            self.cache.put(key, result)
        return result

    # ---- typed endpoints -----------------------------------------------------------------

    async def env_params(
        self,
        latitude: float,
        longitude: float,
        reference_temperature_c: float,
        date: str,
        *,
        hour: str | None = None,
        end_hour: str | None = None,
        analysis: list[str] | None = None,
        poll_interval: float = 5.0,
        max_polls: int = 60,
        use_cache: bool = True,
    ) -> EnvParamsResult:
        """Point conditions. hour: one hour; hour+end_hour: range; neither: whole day."""
        if hour and end_hour:
            date_time: dict[str, Any] = {
                "start_date": date,
                "start_time": hour,
                "end_time": end_hour,
                "filter_type": 2,
            }
        elif hour:
            date_time = {"start_date": date, "start_time": hour, "filter_type": 1}
        else:
            date_time = {"start_date": date, "filter_type": 3}
        payload: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "temperature": reference_temperature_c,
            "date_time": date_time,
        }
        if analysis:
            payload["analysis"] = analysis
        return await self._run(
            "env_params",
            payload,
            EnvParamsResult,
            poll_interval=poll_interval,
            max_polls=max_polls,
            use_cache=use_cache,
        )

    async def heatmap(
        self,
        polygon: dict[str, Any],
        date: str,
        hour: str,
        *,
        granularity: int = 100,
        poll_interval: float = 5.0,
        max_polls: int = 60,
        use_cache: bool = True,
    ) -> HeatmapResult:
        payload = {
            "polygon_aoi": polygon,
            "date_time": {"start_date": date, "start_time": hour, "filter_type": 1},
            "granularity": granularity,
        }
        return await self._run(
            "heatmap",
            payload,
            HeatmapResult,
            poll_interval=poll_interval,
            max_polls=max_polls,
            use_cache=use_cache,
        )

    async def routing(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure_time: str,
        *,
        poll_interval: float = 5.0,
        max_polls: int = 60,
        use_cache: bool = True,
    ) -> RoutingResult:
        payload = {
            "origin": {"lat": origin[0], "lon": origin[1]},
            "destination": {"lat": destination[0], "lon": destination[1]},
            "departure_time": departure_time,
        }
        return await self._run(
            "routing",
            payload,
            RoutingResult,
            poll_interval=poll_interval,
            max_polls=max_polls,
            use_cache=use_cache,
        )
