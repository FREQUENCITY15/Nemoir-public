"""Single-turn prompt service: validation, idempotency, persistence, isolation."""

from __future__ import annotations

import asyncio

import pytest

from nemoir.application.capture_service import CaptureService
from nemoir.application.prompt_service import PromptService
from nemoir.domain.errors import ConflictError, LiveOperationDisabled
from nemoir.domain.models import PromptResponse, ProviderReceipt
from nemoir.domain.states import PromptAttemptStatus, PromptFailureClass
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.synthetic import SyntheticPromptProvider


class BlockingPromptProvider:
    def __init__(self, answer: str, started: asyncio.Event, release: asyncio.Event) -> None:
        self.answer = answer
        self.started = started
        self.release = release
        self.call_count = 0

    async def prompt(self, request):
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        return PromptResponse(
            answer=self.answer,
            receipt=ProviderReceipt(
                provider="fake",
                model="blocking-prompt",
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
                outcome="success",
            ),
        )


def _service(repository, provider=None, *, max_input_chars=2000, max_output_tokens=1024):
    return PromptService(
        repository,
        provider or SyntheticPromptProvider(),
        max_input_chars=max_input_chars,
        max_output_tokens=max_output_tokens,
    )


@pytest.mark.asyncio
async def test_short_question_returns_and_persists_answer_and_receipt(repository) -> None:
    provider = SyntheticPromptProvider()
    service = _service(repository, provider)
    outcome = await service.prompt(
        user_id="user-1",
        guild_id="guild-1",
        channel_id="channel-1",
        question="How do aeroplanes work?",
        idempotency_key="prompt-1",
    )
    assert outcome.status == PromptAttemptStatus.SUCCEEDED
    assert outcome.replayed is False
    assert "synthetic" in outcome.answer.lower()

    attempt = repository.get_prompt_attempt(outcome.attempt_id)
    assert attempt.question == "How do aeroplanes work?"
    assert attempt.status == PromptAttemptStatus.SUCCEEDED
    assert attempt.response_text == outcome.answer
    assert attempt.receipt is not None
    assert attempt.receipt.provider == "synthetic"
    assert attempt.receipt.model == "deterministic-prompt"
    assert attempt.user_id == "user-1"
    assert attempt.guild_id == "guild-1"
    assert attempt.channel_id == "channel-1"
    assert attempt.completed_at is not None


@pytest.mark.asyncio
async def test_empty_question_is_rejected_before_provider(repository) -> None:
    provider = SyntheticPromptProvider()
    service = _service(repository, provider)
    with pytest.raises(ValueError):
        await service.prompt(
            user_id="user-1",
            guild_id="guild-1",
            channel_id="channel-1",
            question="   ",
            idempotency_key="prompt-empty",
        )
    assert provider.call_count == 0
    count = repository._connection.execute(
        "SELECT COUNT(*) AS c FROM prompt_attempts"
    ).fetchone()["c"]
    assert count == 0


@pytest.mark.asyncio
async def test_oversized_question_is_rejected_before_provider(repository) -> None:
    provider = SyntheticPromptProvider()
    service = _service(repository, provider, max_input_chars=10)
    with pytest.raises(ValueError):
        await service.prompt(
            user_id="user-1",
            guild_id="guild-1",
            channel_id="channel-1",
            question="x" * 11,
            idempotency_key="prompt-oversized",
        )
    assert provider.call_count == 0
    count = repository._connection.execute(
        "SELECT COUNT(*) AS c FROM prompt_attempts"
    ).fetchone()["c"]
    assert count == 0


@pytest.mark.asyncio
async def test_one_running_prompt_per_user_is_enforced(repository) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    service = _service(repository, BlockingPromptProvider("answer", started, release))
    first = asyncio.create_task(
        service.prompt(
            user_id="user-1",
            guild_id="guild-1",
            channel_id="channel-1",
            question="first?",
            idempotency_key="prompt-run-1",
        )
    )
    await started.wait()
    with pytest.raises(ConflictError):
        await service.prompt(
            user_id="user-1",
            guild_id="guild-1",
            channel_id="channel-1",
            question="second?",
            idempotency_key="prompt-run-2",
        )
    release.set()
    outcome = await first
    assert outcome.status == PromptAttemptStatus.SUCCEEDED


def test_database_rejects_two_running_prompts_for_one_user(repository) -> None:
    attempt_id, created = repository.begin_prompt_attempt(
        user_id="user-1",
        guild_id="guild-1",
        channel_id="channel-1",
        question="one?",
        idempotency_key="prompt-db-1",
    )
    assert created
    with pytest.raises(ConflictError):
        repository.begin_prompt_attempt(
            user_id="user-1",
            guild_id="guild-1",
            channel_id="channel-1",
            question="two?",
            idempotency_key="prompt-db-2",
        )
    same_id, same_created = repository.begin_prompt_attempt(
        user_id="user-1",
        guild_id="guild-1",
        channel_id="channel-1",
        question="one?",
        idempotency_key="prompt-db-1",
    )
    assert same_id == attempt_id
    assert not same_created


@pytest.mark.asyncio
async def test_duplicate_interaction_is_idempotent(repository) -> None:
    provider = SyntheticPromptProvider()
    service = _service(repository, provider)
    first = await service.prompt(
        user_id="user-1",
        guild_id="guild-1",
        channel_id="channel-1",
        question="why?",
        idempotency_key="prompt-dup",
    )
    assert first.status == PromptAttemptStatus.SUCCEEDED
    second = await service.prompt(
        user_id="user-1",
        guild_id="guild-1",
        channel_id="channel-1",
        question="why?",
        idempotency_key="prompt-dup",
    )
    assert second.replayed is True
    assert second.attempt_id == first.attempt_id
    assert second.answer == first.answer
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_provider_failure_is_classified_without_leaking_details(repository) -> None:
    provider = SyntheticPromptProvider(variant="provider_failure")
    service = _service(repository, provider)
    outcome = await service.prompt(
        user_id="user-1",
        guild_id="guild-1",
        channel_id="channel-1",
        question="will this fail?",
        idempotency_key="prompt-fail",
    )
    assert outcome.status == PromptAttemptStatus.FAILED
    assert outcome.failure_classification == PromptFailureClass.PROVIDER_FAILURE
    attempt = repository.get_prompt_attempt(outcome.attempt_id)
    assert attempt.status == PromptAttemptStatus.FAILED
    assert attempt.failure_classification == PromptFailureClass.PROVIDER_FAILURE
    assert attempt.response_text is None
    # Raw exception text is never persisted.
    assert "Synthetic prompt failure" not in attempt.question
    assert attempt.receipt is None


@pytest.mark.asyncio
async def test_disabled_provider_is_classified_as_disabled(repository) -> None:
    class DisabledPromptProvider:
        async def prompt(self, request):
            raise LiveOperationDisabled("live prompt disabled")

    service = _service(repository, DisabledPromptProvider())
    outcome = await service.prompt(
        user_id="user-1",
        guild_id="guild-1",
        channel_id="channel-1",
        question="anything?",
        idempotency_key="prompt-disabled",
    )
    assert outcome.failure_classification == PromptFailureClass.PROVIDER_DISABLED
    attempt = repository.get_prompt_attempt(outcome.attempt_id)
    assert attempt.failure_classification == PromptFailureClass.PROVIDER_DISABLED


@pytest.mark.asyncio
async def test_empty_answer_is_classified_invalid_with_receipt(repository) -> None:
    provider = SyntheticPromptProvider(answer="   ")
    service = _service(repository, provider)
    outcome = await service.prompt(
        user_id="user-1",
        guild_id="guild-1",
        channel_id="channel-1",
        question="anything?",
        idempotency_key="prompt-empty-answer",
    )
    assert outcome.status == PromptAttemptStatus.FAILED
    assert outcome.failure_classification == PromptFailureClass.INVALID_RESPONSE
    attempt = repository.get_prompt_attempt(outcome.attempt_id)
    assert attempt.receipt is not None
    assert attempt.receipt.provider == "synthetic"


@pytest.mark.asyncio
async def test_restart_replay_keeps_answer_and_receipt_without_second_call(tmp_path) -> None:
    db = tmp_path / "restart.sqlite3"
    provider = SyntheticPromptProvider(answer="persisted answer")
    with SQLiteRepository(db) as repo:
        service = _service(repo, provider)
        outcome = await service.prompt(
            user_id="user-1",
            guild_id="guild-1",
            channel_id="channel-1",
            question="persist me?",
            idempotency_key="prompt-restart",
        )
        attempt_id = outcome.attempt_id

    with SQLiteRepository(db) as repo:
        attempt = repo.get_prompt_attempt(attempt_id)
        assert attempt.response_text == "persisted answer"
        assert attempt.receipt is not None
        service = _service(repo, provider)
        replayed = await service.prompt(
            user_id="user-1",
            guild_id="guild-1",
            channel_id="channel-1",
            question="persist me?",
            idempotency_key="prompt-restart",
        )
        assert replayed.replayed is True
        assert replayed.answer == "persisted answer"
        assert provider.call_count == 1


@pytest.mark.asyncio
async def test_prompt_does_not_mutate_capture_claim_or_tendril_state(
    repository, captured_bundle
) -> None:
    before_status = repository.get_bundle(captured_bundle.id).status
    before_tendrils = repository.list_tendrils(bundle_id=captured_bundle.id)
    before_claim = repository.get_claim_for_bundle(captured_bundle.id)

    service = _service(repository)
    outcome = await service.prompt(
        user_id="person-2",
        guild_id="guild-1",
        channel_id="intake-1",
        question="How do planes fly?",
        idempotency_key="prompt-isolation",
    )
    assert outcome.status == PromptAttemptStatus.SUCCEEDED
    assert repository.get_bundle(captured_bundle.id).status == before_status
    assert repository.list_tendrils(bundle_id=captured_bundle.id) == before_tendrils
    after_claim = repository.get_claim_for_bundle(captured_bundle.id)
    assert after_claim.raw_topic == before_claim.raw_topic
    assert after_claim.id == before_claim.id


@pytest.mark.asyncio
async def test_prompt_capture_service_state_is_unchanged_by_prompt(repository) -> None:
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
    )
    service = _service(repository)
    await service.prompt(
        user_id="person-1",
        guild_id="guild-1",
        channel_id="intake-1",
        question="just a question?",
        idempotency_key="prompt-capture-isolation",
    )
    # The open capture session is untouched by a prompt attempt.
    assert repository.get_bundle(bundle.id).status.value == "CAPTURING"
    assert repository.list_source_messages(bundle.id) == []
