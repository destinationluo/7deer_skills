"""Domain exceptions for the Steam game radar."""


class RadarError(RuntimeError):
    """Base class for expected radar failures."""


class InputValidationError(RadarError):
    """Raised when an input record does not match its schema."""


class ProviderUnavailableError(RadarError):
    """Raised when an upstream data provider cannot be reached."""


class ConfigurationError(RadarError):
    """Raised when radar configuration is invalid."""


class PersistenceError(RadarError):
    """Raised when a radar artifact cannot be persisted safely."""


class RunBusyError(RadarError):
    """Raised when another radar run already owns the run lock."""
