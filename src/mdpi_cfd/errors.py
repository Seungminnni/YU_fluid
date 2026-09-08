class MdpiCfdError(Exception):
    """Base error for expected, user-facing failures."""


class ConfigError(MdpiCfdError):
    """Raised when a configuration violates the canonical model."""


class ProfileError(MdpiCfdError):
    """Raised when a heat-source profile is malformed or out of range."""


class DependencyUnavailable(MdpiCfdError):
    """Raised when an external solver or optional visualization tool is absent."""

