"""Platform-level exception hierarchy."""

from enum import StrEnum


class ErrorKind(StrEnum):
    VALIDATION      = "invalid_request_error"
    AUTHENTICATION  = "authentication_error"
    RATE_LIMIT      = "rate_limit_exceeded"
    UPSTREAM        = "upstream_error"
    SERVER          = "server_error"


class AppError(Exception):
    """Base exception for all application errors."""

    def __init__(
        self,
        message:    str,
        *,
        kind:       ErrorKind = ErrorKind.SERVER,
        code:       str       = "internal_error",
        status:     int       = 500,
        details:    dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind    = kind
        self.code    = code
        self.status  = status
        self.details = details or {}

    def to_dict(self) -> dict:
        err = {
            "message": self.message,
            "type":    self.kind,
            "code":    self.code,
        }
        if "param" in self.details:
            err["param"] = self.details["param"]
        return {"error": err}


class ValidationError(AppError):
    def __init__(self, message: str, *, param: str = "", code: str = "invalid_value") -> None:
        super().__init__(
            message, kind=ErrorKind.VALIDATION, code=code, status=400,
            details={"param": param},
        )
        self.param = param


class AuthError(AppError):
    def __init__(self, message: str = "Invalid or missing API key") -> None:
        super().__init__(
            message, kind=ErrorKind.AUTHENTICATION, code="invalid_api_key", status=401,
        )


class RateLimitError(AppError):
    def __init__(self, message: str = "No available accounts") -> None:
        super().__init__(
            message, kind=ErrorKind.RATE_LIMIT, code="rate_limit_exceeded", status=429,
        )


class UpstreamError(AppError):
    def __init__(
        self,
        message: str,
        *,
        status:  int = 502,
        body:    str = "",
    ) -> None:
        super().__init__(
            message, kind=ErrorKind.UPSTREAM, code="upstream_error", status=status,
            details={"body": body},
        )


class StreamIdleTimeout(AppError):
    def __init__(self, timeout_s: float) -> None:
        super().__init__(
            f"Stream idle timeout after {timeout_s}s",
            kind=ErrorKind.UPSTREAM, code="stream_idle_timeout", status=504,
        )


__all__ = [
    "ErrorKind", "AppError",
    "ValidationError", "AuthError", "RateLimitError",
    "UpstreamError", "StreamIdleTimeout",
]
