"""Typed async client for the FortyGuard REST API."""

from .client import DEFAULT_BASE_URL, FortyGuardClient, RateLimitState, TTLCache
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
from .models import EnvParamsResult, HeatmapResult, RoutingResult

__all__ = [
    "DEFAULT_BASE_URL",
    "ActivityFailed",
    "AuthError",
    "EnvParamsResult",
    "FortyGuardClient",
    "FortyGuardError",
    "HeatmapResult",
    "NotFoundError",
    "PlanAccessError",
    "PollTimeout",
    "QuotaError",
    "RateLimitError",
    "RateLimitState",
    "RoutingResult",
    "ServerError",
    "TTLCache",
    "TransportFailure",
    "ValidationError",
]
