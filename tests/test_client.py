"""Offline tests for fortyguard_client.

Fixtures in tests/fixtures are real response bodies (see _build.py). Tests marked "synthetic" use
hand-written bodies for responses that have not been observed live (401, 403, 429, 5xx).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from heatwise.fortyguard_client import (
    ActivityFailed,
    AuthError,
    FortyGuardClient,
    NotFoundError,
    PlanAccessError,
    PollTimeout,
    RateLimitError,
    ServerError,
    TransportFailure,
    ValidationError,
)
from heatwise.fortyguard_client.models import EnvLocation

FIX = Path(__file__).parent / "fixtures"
BASE = "https://api.fortyguard.com/v1"
FAKE_KEY = "fg_unit_test_key_123456"


def fx(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


class Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make(sleeper: Sleeper | None = None, **kw: Any) -> FortyGuardClient:
    return FortyGuardClient(FAKE_KEY, sleep=sleeper or Sleeper(), rng=lambda: 1.0, **kw)


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


def mock_env_flow(api: Any, rounds: int = 1) -> tuple[Any, Any]:
    submit = api.post("/env_params").respond(200, json=fx("env_submit.json"))
    status = api.get(url__regex=r"/status/.+").mock(
        side_effect=[
            httpx.Response(200, json=fx("status_processing.json")),
            httpx.Response(200, json=fx("env_status_completed.json")),
        ]
        * rounds
    )
    return submit, status


async def test_env_params_submit_poll_parse(api: Any) -> None:
    submit, status = mock_env_flow(api)
    sleeper = Sleeper()
    async with make(sleeper) as c:
        res = await c.env_params(33.4484, -112.074, 35, "2026-11-17", hour="14:00")
    assert submit.call_count == 1 and status.call_count == 2
    assert sleeper.delays == [5.0]  # one wait between the two polls
    assert res.metadata.timezone == "America/Phoenix"
    assert res.metadata.timestamps == ["2026-11-17T14:00:00-07:00"]
    assert res.series("heat_index_celsius") == [38.7]
    assert res.locations[0].temperature == 35.0  # echo of the input, not an observation
    assert "air_temperature" not in res.locations[0].parameters
    body = json.loads(submit.calls[0].request.content)
    assert body["date_time"] == {
        "start_date": "2026-11-17",
        "start_time": "14:00",
        "filter_type": 1,
    }
    assert submit.calls[0].request.headers["api-key"] == FAKE_KEY


async def test_cache_avoids_second_paid_call(api: Any) -> None:
    submit, _ = mock_env_flow(api, rounds=2)
    async with make(cache_ttl=60) as c:
        a = await c.env_params(33.4484, -112.074, 35, "2026-11-17", hour="14:00")
        b = await c.env_params(33.4484, -112.074, 35, "2026-11-17", hour="14:00")
        c3 = await c.env_params(33.4484, -112.074, 25, "2026-11-17", hour="14:00", use_cache=False)
    assert a is b
    assert submit.call_count == 2  # first call + the explicit uncached call
    assert c3 is not a


async def test_validation_error_future_date_is_not_retried(api: Any) -> None:
    route = api.post("/env_params").respond(400, json=fx("error_400_future_date.json"))
    sleeper = Sleeper()
    async with make(sleeper) as c:
        with pytest.raises(ValidationError) as ei:
            await c.env_params(33.4, -112.0, 35, "2026-11-21", hour="14:00")
    assert route.call_count == 1 and sleeper.delays == []
    assert ei.value.code == "NO_COVERAGE" and ei.value.field == "date_time.start_date"
    assert "future" in ei.value.user_message


async def test_before_2021_message_is_surfaced(api: Any) -> None:
    api.post("/env_params").respond(400, json=fx("error_400_before_2021.json"))
    async with make() as c:
        with pytest.raises(ValidationError, match="2021-01-01"):
            await c.env_params(33.4, -112.0, 35, "2020-06-15", hour="14:00")


async def test_unknown_path_404_is_not_found(api: Any) -> None:
    api.post("/nope").respond(404, json=fx("error_404_endpoint.json"))
    async with make() as c:
        with pytest.raises(NotFoundError, match="Endpoint not found"):
            await c.submit("nope", {})


# ---- synthetic error bodies (not yet observed live) --------------------------------------------


async def test_synthetic_401_auth_error(api: Any) -> None:
    api.post("/env_params").respond(401, json={"error": True, "message": "Invalid API key"})
    async with make() as c:
        with pytest.raises(AuthError) as ei:
            await c.submit("env_params", {})
    assert "TEMP_API_KEY" in ei.value.user_message


async def test_synthetic_403_plan_error_not_retried(api: Any) -> None:
    route = api.post("/routing").respond(403, json={"error": True, "message": "Upgrade your plan"})
    async with make() as c:
        with pytest.raises(PlanAccessError) as ei:
            await c.submit("routing", {})
    assert route.call_count == 1
    assert "plan" in ei.value.user_message.lower()


async def test_synthetic_429_raises_with_retry_after_and_no_retry(api: Any) -> None:
    route = api.post("/env_params").respond(
        429, json={"error": True, "message": "slow down"}, headers={"retry-after": "12"}
    )
    sleeper = Sleeper()
    async with make(sleeper) as c:
        with pytest.raises(RateLimitError) as ei:
            await c.submit("env_params", {})
    assert route.call_count == 1 and sleeper.delays == []
    assert ei.value.retry_after == 12.0


async def test_synthetic_5xx_retries_with_backoff_then_succeeds(api: Any) -> None:
    route = api.post("/env_params").mock(
        side_effect=[
            httpx.Response(500, json={"error": True, "message": "boom"}),
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json=fx("env_submit.json")),
        ]
    )
    sleeper = Sleeper()
    async with make(sleeper, backoff_base=0.5, backoff_cap=8.0) as c:
        activity_id = await c.submit("env_params", {})
    assert route.call_count == 3 and activity_id
    # rng()==1.0 -> delay equals the exponential ceiling: 0.5 then 1.0
    assert sleeper.delays == [0.5, 1.0]


async def test_synthetic_5xx_exhausts_retries(api: Any) -> None:
    route = api.post("/env_params").respond(503, json={"error": True, "message": "down"})
    async with make(max_retries=2) as c:
        with pytest.raises(ServerError):
            await c.submit("env_params", {})
    assert route.call_count == 3


async def test_transport_errors_retry_then_fail(api: Any) -> None:
    route = api.post("/env_params").mock(side_effect=httpx.ConnectTimeout("t"))
    async with make(max_retries=1) as c:
        with pytest.raises(TransportFailure):
            await c.submit("env_params", {})
    assert route.call_count == 2


async def test_jitter_is_bounded_and_random(api: Any) -> None:
    api.post("/env_params").respond(500, json={"error": True, "message": "x"})
    sleeper = Sleeper()
    c = FortyGuardClient(FAKE_KEY, sleep=sleeper, rng=lambda: 0.25, max_retries=2, backoff_base=1.0)
    async with c:
        with pytest.raises(ServerError):
            await c.submit("env_params", {})
    assert sleeper.delays == [0.25, 0.5]


# ---- polling --------------------------------------------------------------------------------


async def test_wait_failed_raises_activity_failed(api: Any) -> None:
    failed = fx("status_processing.json")
    failed["data"]["status"] = "Failed"  # synthetic: the Failed shape was not observed live
    api.get(url__regex=r"/status/.+").respond(200, json=failed)
    async with make() as c:
        with pytest.raises(ActivityFailed):
            await c.wait("abc")


async def test_wait_is_bounded(api: Any) -> None:
    status = api.get(url__regex=r"/status/.+").respond(200, json=fx("status_processing.json"))
    async with make() as c:
        with pytest.raises(PollTimeout):
            await c.wait("abc", max_polls=3)
    assert status.call_count == 3


async def test_transient_404_right_after_submit_is_tolerated(api: Any) -> None:
    api.get(url__regex=r"/status/.+").mock(
        side_effect=[
            httpx.Response(404, json=fx("error_404_endpoint.json")),
            httpx.Response(200, json=fx("env_status_completed.json")),
        ]
    )
    async with make() as c:
        data = await c.wait("abc")
    assert data.is_completed


async def test_persistent_404_on_status_eventually_raises(api: Any) -> None:
    api.get(url__regex=r"/status/.+").respond(404, json=fx("error_404_endpoint.json"))
    async with make() as c:
        with pytest.raises(NotFoundError):
            await c.wait("abc", not_found_grace=2)


# ---- rate-limit awareness -------------------------------------------------------------------


async def test_exhausted_window_raises_locally_without_network(api: Any) -> None:
    headers = {
        "x-ratelimit-limit": "300",
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "2000",
    }
    route = api.post("/env_params").respond(200, json=fx("env_submit.json"), headers=headers)
    c = FortyGuardClient(FAKE_KEY, sleep=Sleeper(), clock=lambda: 1990.0)
    async with c:
        await c.submit("env_params", {})
        assert c.rate_limit["submit"].remaining == 0
        with pytest.raises(RateLimitError) as ei:
            await c.submit("env_params", {})
    assert route.call_count == 1
    assert ei.value.retry_after == 10.0


async def test_status_bucket_is_separate_from_submit_bucket(api: Any) -> None:
    api.post("/env_params").respond(
        200,
        json=fx("env_submit.json"),
        headers={
            "x-ratelimit-limit": "300",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": "2000",
        },
    )
    api.get(url__regex=r"/status/.+").respond(
        200,
        json=fx("env_status_completed.json"),
        headers={
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "999",
            "x-ratelimit-reset": "2000",
        },
    )
    c = FortyGuardClient(FAKE_KEY, sleep=Sleeper(), clock=lambda: 1990.0)
    async with c:
        await c.submit("env_params", {})
        assert (await c.status("abc")).is_completed


async def test_short_wait_is_honoured_when_allowed(api: Any) -> None:
    headers = {
        "x-ratelimit-limit": "300",
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "2000",
    }
    route = api.post("/env_params").respond(200, json=fx("env_submit.json"), headers=headers)
    sleeper = Sleeper()
    c = FortyGuardClient(FAKE_KEY, sleep=sleeper, clock=lambda: 1995.0, max_rate_limit_wait=30)
    async with c:
        await c.submit("env_params", {})
        await c.submit("env_params", {})
    assert route.call_count == 2 and sleeper.delays == [5.0]


# ---- models ---------------------------------------------------------------------------------


def test_legacy_minus_999_becomes_none() -> None:
    loc = EnvLocation.model_validate(
        {"lat": 1, "lon": 2, "parameters": {"co2_ppm": [-999, 440.5, None]}}
    )
    assert loc.parameters["co2_ppm"] == [None, 440.5, None]


async def test_heatmap_and_routing_parse_real_fixtures(api: Any) -> None:
    api.post("/heatmap").respond(200, json=fx("env_submit.json"))
    api.post("/routing").respond(200, json=fx("env_submit.json"))
    api.get(url__regex=r"/status/.+").mock(
        side_effect=[
            httpx.Response(200, json=fx("heatmap_status_completed.json")),
            httpx.Response(200, json=fx("routing_status_completed.json")),
        ]
    )
    square = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    async with make() as c:
        hm = await c.heatmap(square, "2026-11-17", "14:00")
        rt = await c.routing((33.4484, -112.074), (33.4255, -111.94), "2026-11-17T21:00:00Z")
    assert round(hm.stats_data.temperature_stats.mean, 2) == 19.19
    route = rt.result_routing.features[0].properties
    assert route.distance_km == 16.185 and round(route.thermal.mean_c, 1) == 41.8


async def test_logs_never_contain_the_api_key(api: Any) -> None:
    mock_env_flow(api)
    with capture_logs() as logs:
        async with make() as c:
            await c.env_params(33.4484, -112.074, 35, "2026-11-17", hour="14:00")
    assert logs and FAKE_KEY not in json.dumps(logs, default=str)
