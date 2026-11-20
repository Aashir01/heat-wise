"""Pydantic models built from real FortyGuard responses saved in results/raw/.

Unknown fields are kept (`extra="allow"`) so new API fields do not break parsing.
Missing numeric values are JSON null; legacy -999 is normalised to None (never treated as 0).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

LEGACY_MISSING = -999


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class SubmitData(ApiModel):
    activity_id: str


class SubmitResponse(ApiModel):
    error: bool
    status_code: int
    message: str
    data: SubmitData


class StatusData(ApiModel):
    activity_id: str
    status: str
    result: dict[str, Any] | None = None

    @property
    def is_completed(self) -> bool:
        return self.status.lower() in {"completed", "succeeded"}

    @property
    def is_failed(self) -> bool:
        return self.status.lower() in {"failed", "error"}


class StatusResponse(ApiModel):
    error: bool
    status_code: int
    message: str
    data: StatusData


class ApiErrorBody(ApiModel):
    """Error body. 400s carry message/field/code; 404 carries details.message."""

    error: bool = True
    status_code: int | None = None
    message: str | None = None
    field: str | None = None
    code: str | None = None
    details: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        if self.message:
            return self.message
        if self.details and isinstance(self.details.get("message"), str):
            return str(self.details["message"])
        return "Unknown error"


# ---- env_params -----------------------------------------------------------------------------


class TimeRange(ApiModel):
    start: str
    end: str
    interval: str
    count: int


class EnvMetadata(ApiModel):
    timezone: str
    timezone_offset_hours: float
    time_range: TimeRange
    timestamps: list[str]


class ClearSky(ApiModel):
    ghi: float | None = None
    dni: float | None = None
    dhi: float | None = None


class SolarIrradiance(ApiModel):
    clear_sky: ClearSky | None = None
    description: str | None = None


class EnvLocation(ApiModel):
    lat: float
    lon: float
    elevation: float | None = None
    # NOTE: this is the caller-supplied reference temperature echoed back, not an observation.
    temperature: float | None = None
    parameters: dict[str, list[float | None]]
    solar_irradiance: SolarIrradiance | None = None

    @field_validator("parameters", mode="before")
    @classmethod
    def _normalise_missing(cls, v: dict[str, list[Any]]) -> dict[str, list[float | None]]:
        return {
            k: [None if (x is None or x == LEGACY_MISSING) else float(x) for x in vals]
            for k, vals in v.items()
        }


class EnvParamsResult(ApiModel):
    metadata: EnvMetadata
    locations: list[EnvLocation]

    def series(self, name: str, location: int = 0) -> list[float | None]:
        """Hourly series for one parameter; empty list if the parameter was not returned."""
        return self.locations[location].parameters.get(name, [])


# ---- heatmap --------------------------------------------------------------------------------


class TemperatureStats(ApiModel):
    minimum: float
    maximum: float
    mean: float
    standard_deviation: float


class HeatmapStats(ApiModel):
    temperature_stats: TemperatureStats


class HeatmapResult(ApiModel):
    map_data: dict[str, Any]
    stats_data: HeatmapStats


# ---- routing --------------------------------------------------------------------------------


class Thermal(ApiModel):
    mean_c: float
    time_weighted_c: float | None = None
    dist_weighted_c: float | None = None
    peak_c: float
    degree_minutes_above_threshold: float | None = None
    minutes_above_threshold: float | None = None
    threshold_c: float | None = None


class RouteProperties(ApiModel):
    label: str | None = None
    selected: bool | None = None
    score: float | None = None
    distance_km: float
    duration_min: float
    thermal: Thermal
    segment_temp_c: list[float] = []


class RouteFeature(ApiModel):
    properties: RouteProperties
    geometry: dict[str, Any] | None = None


class RouteCollection(ApiModel):
    features: list[RouteFeature]


class RoutingResult(ApiModel):
    result_routing: RouteCollection
