"""Transaction-safe enforcement of one active analysis attempt per bundle."""

from __future__ import annotations

import asyncio

import pytest

from nemoir.application.analysis_service import AnalysisService
from nemoir.domain.errors import ConflictError
from nemoir.domain.models import AnalysisResponse, ProviderReceipt
from nemoir.domain.states import BundleState
from nemoir.domain.validation import ValidationReport
from nemoir.providers.fake import FakeAnalysisProvider


class BlockingProvider:
    def __init__(self, result, started: asyncio.Event, release: asyncio.Event) -> None:
        self.result = result
        self.started = started
        self.release = release
        self.calls = 0

    async def analyse(self, request) -> AnalysisResponse:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return AnalysisResponse(
            result=self.result,
            receipt=ProviderReceipt(
                provider="fake",
                model="blocking",
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
                outcome="success",
            ),
        )


@pytest.mark.asyncio
async def test_second_attempt_is_refused_while_first_is_running(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    service = AnalysisService(
        repository,
        BlockingProvider(synthetic_analysis, started, release),
        authorization,
    )
    first = asyncio.create_task(
        service.analyse(
            captured_bundle.id,
            actor_user_id="person-2",
            idempotency_key="single-1",
        )
    )
    await started.wait()
    with pytest.raises(ConflictError):
        await service.analyse(
            captured_bundle.id,
            actor_user_id="person-2",
            idempotency_key="single-2",
        )
    assert repository.get_bundle(captured_bundle.id).status == BundleState.ANALYSING
    release.set()
    outcome = await first
    assert outcome.review is not None


@pytest.mark.asyncio
async def test_same_key_replay_while_running_is_a_replay_not_a_failure(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    service = AnalysisService(
        repository,
        BlockingProvider(synthetic_analysis, started, release),
        authorization,
    )
    first = asyncio.create_task(
        service.analyse(
            captured_bundle.id,
            actor_user_id="person-2",
            idempotency_key="replay-same-key",
        )
    )
    await started.wait()
    replayed = await service.analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="replay-same-key",
    )
    assert replayed.replayed is True
    assert replayed.state == BundleState.ANALYSING
    assert replayed.review is None
    release.set()
    outcome = await first
    assert outcome.replayed is False
    assert outcome.review is not None


def test_database_rejects_two_running_attempts(repository, captured_bundle) -> None:
    attempt_id, created = repository.begin_analysis_attempt(captured_bundle.id, "key-a", "v1")
    assert created
    with pytest.raises(ConflictError):
        repository.begin_analysis_attempt(captured_bundle.id, "key-b", "v1")
    # the same idempotency key is still idempotent
    same_id, same_created = repository.begin_analysis_attempt(captured_bundle.id, "key-a", "v1")
    assert same_id == attempt_id
    assert not same_created


def test_failed_attempt_frees_the_bundle_for_retry(repository, captured_bundle) -> None:
    attempt_id, _ = repository.begin_analysis_attempt(captured_bundle.id, "key-1", "v1")
    repository.persist_analysis_failure(attempt_id, "FAILED", ValidationReport())
    second_id, created = repository.begin_analysis_attempt(captured_bundle.id, "key-2", "v1")
    assert created
    assert second_id != attempt_id


@pytest.mark.asyncio
async def test_retry_after_failure_with_new_key_is_allowed(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    failed = await AnalysisService(
        repository,
        FakeAnalysisProvider(synthetic_analysis, variant="provider_failure"),
        authorization,
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="retry-fail",
    )
    assert failed.state == BundleState.ANALYSIS_FAILED
    ok = await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="retry-ok",
    )
    assert ok.review is not None
