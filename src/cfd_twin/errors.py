class CfdTwinError(Exception):
    """Base error for expected, user-facing failures."""


class ConfigError(CfdTwinError):
    """Raised when a configuration violates the canonical model."""


class ProfileError(CfdTwinError):
    """Raised when a heat-source profile is malformed or out of range."""


class DependencyUnavailable(CfdTwinError):
    """Raised when an external solver or optional visualization tool is absent."""

