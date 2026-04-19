class ConfigError(ValueError):
    """Raised when configuration is invalid."""


class EndpointSelectionError(RuntimeError):
    """Raised when no endpoint can be selected for a request."""


class ProviderRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retriable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retriable = retriable
        self.status_code = status_code


class AllRetriesFailedError(RuntimeError):
    """Raised when all retries are exhausted across endpoints."""
