"""Typed errors. Every message here is safe to show to the user."""


class GoveeLightsError(Exception):
    """Base class for everything this package raises on purpose."""


class ConfigError(GoveeLightsError):
    """Missing API key or a bad config.yaml."""


class TargetError(GoveeLightsError):
    """A target did not match a light or group, or matched ambiguously."""


class ValidationError(GoveeLightsError, ValueError):
    """A value was rejected locally, before any API call."""


class SnapshotError(GoveeLightsError):
    """A snapshot could not be taken or found."""


class GoveeError(GoveeLightsError):
    """The Govee API refused or failed a request."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class BadRequestError(GoveeError):
    """400: missing, malformed or out-of-range parameter."""


class AuthError(GoveeError):
    """401: the API key was rejected."""


class NotFoundError(GoveeError):
    """404: device or instance not found."""


class RateLimitedError(GoveeError):
    """429: still rate limited after all retries."""


class GoveeConnectionError(GoveeError):
    """The Govee cloud could not be reached at all."""
