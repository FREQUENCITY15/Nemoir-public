"""Single-turn ordinary-question answering, isolated from the analysis workflow.

This service is deliberately separate from capture, claim discovery, analysis,
tendrils, habitats, and routing. It sends one plain prompt and returns one
unstructured textual answer. There is no conversation memory, no source
capture, no claim or tendril state, and no retry that could duplicate model
cost. The provider's output is treated as untrusted display content.
"""

from __future__ import annotations

from dataclasses import dataclass

from nemoir.domain.errors import LiveOperationDisabled
from nemoir.domain.models import PromptRequest, ProviderReceipt
from nemoir.domain.states import PromptAttemptStatus, PromptFailureClass
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.base import PromptProvider


@dataclass(frozen=True)
class PromptOutcome:
    attempt_id: str
    status: PromptAttemptStatus
    answer: str | None = None
    receipt: ProviderReceipt | None = None
    failure_classification: PromptFailureClass | None = None
    replayed: bool = False


class PromptService:
    def __init__(
        self,
        repository: SQLiteRepository,
        provider: PromptProvider,
        *,
        max_input_chars: int = 2000,
        max_output_tokens: int = 1024,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.max_input_chars = max_input_chars
        self.max_output_tokens = max_output_tokens

    async def prompt(
        self,
        *,
        user_id: str,
        guild_id: str,
        channel_id: str,
        question: str,
        idempotency_key: str,
    ) -> PromptOutcome:
        question = (question or "").strip()
        if not question:
            raise ValueError("Question must not be empty")
        if len(question) > self.max_input_chars:
            raise ValueError(
                f"Question exceeds the maximum of {self.max_input_chars} characters"
            )

        attempt_id, created = self.repository.begin_prompt_attempt(
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            question=question,
            idempotency_key=idempotency_key,
        )
        if not created:
            attempt = self.repository.get_prompt_attempt(attempt_id)
            if attempt.status == PromptAttemptStatus.RUNNING:
                return PromptOutcome(
                    attempt_id, PromptAttemptStatus.RUNNING, replayed=True
                )
            if attempt.status == PromptAttemptStatus.SUCCEEDED:
                return PromptOutcome(
                    attempt_id,
                    PromptAttemptStatus.SUCCEEDED,
                    answer=attempt.response_text,
                    receipt=attempt.receipt,
                    replayed=True,
                )
            # A FAILED attempt is never automatically retried; the recorded
            # classification is the answer and remains inspectable.
            return PromptOutcome(
                attempt_id,
                PromptAttemptStatus.FAILED,
                failure_classification=attempt.failure_classification,
                replayed=True,
            )

        request = PromptRequest(
            question=question, max_output_tokens=self.max_output_tokens
        )
        try:
            response = await self.provider.prompt(request)
        except LiveOperationDisabled:
            self.repository.persist_prompt_failure(
                attempt_id, PromptFailureClass.PROVIDER_DISABLED
            )
            return PromptOutcome(
                attempt_id,
                PromptAttemptStatus.FAILED,
                failure_classification=PromptFailureClass.PROVIDER_DISABLED,
            )
        except Exception:
            # Never store or surface raw exception details; classify only.
            self.repository.persist_prompt_failure(
                attempt_id, PromptFailureClass.PROVIDER_FAILURE
            )
            return PromptOutcome(
                attempt_id,
                PromptAttemptStatus.FAILED,
                failure_classification=PromptFailureClass.PROVIDER_FAILURE,
            )

        if not response.answer or not response.answer.strip():
            self.repository.persist_prompt_failure(
                attempt_id,
                PromptFailureClass.INVALID_RESPONSE,
                receipt=response.receipt,
            )
            return PromptOutcome(
                attempt_id,
                PromptAttemptStatus.FAILED,
                failure_classification=PromptFailureClass.INVALID_RESPONSE,
            )

        self.repository.persist_prompt_success(
            attempt_id, response.answer, response.receipt
        )
        return PromptOutcome(
            attempt_id,
            PromptAttemptStatus.SUCCEEDED,
            answer=response.answer,
            receipt=response.receipt,
        )
