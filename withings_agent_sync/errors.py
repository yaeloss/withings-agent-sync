"""Errors that the CLI can report without exposing response bodies."""


class WithingsError(RuntimeError):
    """Base error for expected application failures."""


class ConfigError(WithingsError):
    """Missing or invalid local configuration (CLI exit code 2)."""


class APIError(WithingsError):
    """An unsuccessful or malformed Withings response."""


class BusyError(WithingsError):
    """Another process holds a lock on the requested files."""
