"""Typed errors for the FortyGuard REST client.

Only the error bodies observed live are modelled precisely:
  400 -> {"error": true, "status_code": 400, "message": ..., "field": ..., "code": "NO_COVERAGE"}
  404 -> {"error": true, "status_code": 404, "details": {"message": "Endpoint not found"}}
401/403/429/5xx bodies have not been observed yet, so those are mapped by status code only.
"""

from __future__ import annotations


class FortyGuardError(Exception):
    """Base class. `user_message` is safe to show to an end user."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.field = field

    @property
    def user_message(self) -> str:
        return self.message


class ValidationError(FortyGuardError):
    """400 / 422: the request was rejected. Not retried. Rejected requests are not charged."""


class AuthError(FortyGuardError):
    """401: missing or invalid API key."""

    @property
    def user_message(self) -> str:
        return "The FortyGuard API key is missing or invalid. Check TEMP_API_KEY in your .env."


class PlanAccessError(FortyGuardError):
    """403: the key's plan does not include this endpoint. Never retried."""

    @property
    def user_message(self) -> str:
        return f"Your FortyGuard plan does not include this feature. ({self.message})"


class QuotaError(FortyGuardError):
    """Credits or quota exhausted (detected from status/message; body shape not yet observed)."""

    @property
    def user_message(self) -> str:
        return f"FortyGuard credits or quota are exhausted. ({self.message})"


class RateLimitError(FortyGuardError):
    """429 or a locally detected exhausted rate-limit window. Never retried in a loop."""

    def __init__(
        self, message: str, *, retry_after: float | None = None, status_code: int | None = 429
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after = retry_after

    @property
    def user_message(self) -> str:
        wait = f" Try again in about {int(self.retry_after) + 1}s." if self.retry_after else ""
        return f"FortyGuard rate limit reached.{wait}"


class NotFoundError(FortyGuardError):
    """404 on a non-status path."""


class ServerError(FortyGuardError):
    """5xx after retries were exhausted."""

    @property
    def user_message(self) -> str:
        return "FortyGuard had a server-side problem. Please retry shortly."


class TransportFailure(FortyGuardError):
    """Network error or timeout after retries were exhausted."""

    @property
    def user_message(self) -> str:
        return "Could not reach FortyGuard (network error or timeout)."


class ActivityFailed(FortyGuardError):
    """The activity reached status Failed. Stop polling and record the activity id."""

    def __init__(self, activity_id: str, message: str = "Activity failed") -> None:
        super().__init__(f"{message} (activity {activity_id})")
        self.activity_id = activity_id


class PollTimeout(FortyGuardError):
    """The bounded polling loop ended before the activity completed."""

    def __init__(self, activity_id: str, polls: int) -> None:
        super().__init__(f"Activity {activity_id} not complete after {polls} polls")
        self.activity_id = activity_id
