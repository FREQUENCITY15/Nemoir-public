"""Explicit domain and application failures."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ProviderReceipt


class NemoirError(Exception):
    """Base error for expected Nemoir failures."""


class NotFoundError(NemoirError):
    """A requested entity does not exist."""


class AuthorizationError(NemoirError):
    """An actor is not authorised for the requested operation."""


class ConflictError(NemoirError):
    """An operation conflicts with existing state or an idempotency key."""


class InvalidTransitionError(NemoirError):
    """A lifecycle transition is outside the closed state machine."""


class ValidationFailure(NemoirError):
    """Provider output failed deterministic validation."""


class LiveOperationDisabled(NemoirError):
    """A live external operation was attempted without an explicit gate."""


class ProviderOutputValidationError(NemoirError):
    """A paid provider response was received but failed the output contract."""

    def __init__(
        self,
        *,
        receipt: ProviderReceipt,
        raw_response: str,
        validation_error_count: int,
    ) -> None:
        super().__init__("Provider output did not match the AnalysisResult contract")
        self.receipt = receipt
        self.raw_response = raw_response
        self.validation_error_count = validation_error_count
