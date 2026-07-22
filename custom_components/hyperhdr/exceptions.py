"""Exceptions for the HyperHDR integration."""

from __future__ import annotations


class HyperHdrError(Exception):
    """Base exception for all HyperHDR errors."""


class HyperHdrConnectionError(HyperHdrError):
    """Raised when the connection to the HyperHDR server fails."""


class HyperHdrApiError(HyperHdrError):
    """Raised when a HyperHDR JSON-RPC command returns ``success: false``.

    Carries the offending command name and the server-provided error string
    so callers can surface a meaningful message.
    """

    def __init__(self, command: str, error: str) -> None:
        """Initialize the error with the failing command and server message."""
        super().__init__(f"{command}: {error}")
        self.command = command
        self.error = error


class HyperHdrAuthError(HyperHdrError):
    """Raised when authentication fails.

    Covers a missing/invalid API token or a failed admin login.
    """
