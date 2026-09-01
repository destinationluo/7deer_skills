"""Expected failures and process exit codes for the unified game radar."""


EXIT_SUCCESS = 0
EXIT_INPUT_ERROR = 2
EXIT_PROVIDER_UNAVAILABLE = 3
EXIT_CONFIGURATION_ERROR = 4
EXIT_PERSISTENCE_ERROR = 5
EXIT_RUN_LOCKED = 6
EXIT_IDEMPOTENCY_CONFLICT = 7


class RadarError(RuntimeError):
    """Base class for expected radar failures."""

    exit_code = 1


class InputValidationError(RadarError):
    """Raised when an input record does not match its declared schema."""

    exit_code = EXIT_INPUT_ERROR


class ProviderUnavailableError(RadarError):
    """Raised when an upstream provider cannot supply usable data."""

    exit_code = EXIT_PROVIDER_UNAVAILABLE


class ConfigurationError(RadarError):
    """Raised when radar configuration is invalid."""

    exit_code = EXIT_CONFIGURATION_ERROR


class PersistenceError(RadarError):
    """Raised when canonical radar state cannot be persisted safely."""

    exit_code = EXIT_PERSISTENCE_ERROR


class ReportError(PersistenceError):
    """Raised when a report cannot be rendered or published safely."""


class RunBusyError(RadarError):
    """Raised when another radar run owns the project-scoped lock."""

    exit_code = EXIT_RUN_LOCKED


class IdempotencyConflictError(RadarError):
    """Raised when an idempotency key is reused with changed content."""

    exit_code = EXIT_IDEMPOTENCY_CONFLICT
