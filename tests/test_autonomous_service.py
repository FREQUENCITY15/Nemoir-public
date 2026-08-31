"""Autonomous job service: durable queue, crash boundaries, restart, publishing."""

from __future__ import annotations

import pytest

from nemoir.application.autonomous_service import (
    AutonomousJobService,
    stable_autonomous_key,
)
from nemoir.application.authorization import AuthorizationPolicy
from nemoir.application.capture_service import CaptureService
from nemoir.application.publishing_service import PublishingService
from nemoir.domain.errors import AuthorizationError, ConflictError, NotFoundError
from nemoir.domain.models import AutonomousSortRequest, SourceMessage
from nemoir.domain.states import (
    AutonomousAttemptStatus,
    AutonomousJobPhase,
    BundleState,
    TendrilState,
)
from nemoir.domain.validation import validate_autonomous_sort
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.synthetic import SyntheticAutonomousSortProvider


class FakePublisher:
    platform = "discord"

    def __init__(self, *, fail_first: bool = False) -> None:
        self.published: list[tuple[str, str, str]] = []
        self.fail_first = fail_first
        self.call_count = 0

    async def publish(self, tendril, external_destination_id):
        self.call_count += 1
        if self.fail_first and self.call_count == 1:
            raise RuntimeError("synthetic publish failure")
        message_id = f"msg-{self.call_count}"
        self.published.append((tendril.id, external_destination_id, message_id))
        return message_id


class FakeChannelFactory:
    platform = "discord"

    def __init__(self, *, existing=frozenset(), fail_creates: int = 0) -> None:
        self.existing = set(existing)
        self.fail_creates = fail_creates
        self.created: list[tuple[str, str]] = []

    async def list_channel_names(self, category_id):
        return set(self.existing)

    async def create_channel(self, slug, category_id):
        if self.fail_creates > 0:
            self.fail_creates -= 1
            raise RuntimeError("synthetic channel creation failure")
        channel_id = f"ch-{len(self.created) + 1}"
        self.created.append((slug, channel_id))
        self.existing.add(slug)
        return channel_id


def _seal_autonomous(
    repository: SQLiteRepository,
    messages: list[SourceMessage],
    *,
    user: str = "person-1",
) -> object:
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id=user,
        recipient_user_id="unused",
        autonomous=True,
    )
    for message in messages:
        capture.capture_message(
            bundle.id,
            actor_user_id=user,
            external_message_id=message.external_message_id,
            author_display_name=message.author_display_name,
            channel_id="intake-1",
            content=message.content,
            source_url=message.source_url,
            timestamp=message.timestamp,
        )
    capture.seal(bundle.id, actor_user_id=user, idempotency_key="auto-seal-1")
    return repository.get_bundle(bundle.id)


class Harness:
    def __init__(self, repository, sort_provider, publisher, factory, *, admins=None):
        self.repository = repository
        self.authorization = AuthorizationPolicy(repository, set(admins or {"admin-1"}))
        self.publisher = publisher
        self.factory = factory
        self.notifications: list[tuple[str, str]] = []
        self.service = AutonomousJobService(
            repository,
            sort_provider,
            PublishingService(
                repository, self.authorization, publisher, factory
            ),
            self.authorization,
            admins or {"admin-1"},
        )

    async def notify(self, bundle, text):
        self.notifications.append((bundle.submitter_user_id, text))

    async def process(self, bundle_id, *, allow_channel_write=True):
        return await self.service.process_job(
            bundle_id,
            guild_id="guild-1",
            category_id="cat-1",
            allow_channel_write=allow_channel_write,
            notify=self.notify,
        )


def _make_harness(repository, *, variant="valid", fail_creates=0, fail_first=False):
    return Harness(
        repository,
        SyntheticAutonomousSortProvider(variant=variant),
        FakePublisher(fail_first=fail_first),
        FakeChannelFactory(fail_creates=fail_creates),
    )


# -- queue/idempotency ----------------------------------------------------


def test_queue_creates_exactly_one_job_per_bundle(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    first = harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    assert first.created is True
    assert first.job.phase == AutonomousJobPhase.QUEUED
    assert repository.get_bundle(bundle.id).status == BundleState.AUTONOMOUS_SORTING

    # Duplicate Discord delivery with the same interaction key.
    duplicate = harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    assert duplicate.created is False
    assert duplicate.job.id == first.job.id

    # A second event with a different key still resolves to the same job.
    other_key = harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-2",
        pending_attempt_key="attempt-2",
    )
    assert other_key.created is False
    assert other_key.job.id == first.job.id
    assert len(repository.list_autonomous_jobs()) == 1


def test_queue_requires_owner_or_admin(repository, synthetic_messages) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    with pytest.raises(AuthorizationError):
        harness.service.queue(
            bundle.id,
            actor_user_id="person-2",
            idempotency_key="seal-event-1",
            pending_attempt_key="attempt-1",
        )
    harness.service.queue(
        bundle.id,
        actor_user_id="admin-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )


def test_queue_refuses_manual_bundles(repository, synthetic_messages) -> None:
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
        autonomous=False,
    )
    harness = _make_harness(repository)
    with pytest.raises(ConflictError):
        harness.service.queue(
            bundle.id,
            actor_user_id="person-1",
            idempotency_key="seal-event-1",
            pending_attempt_key="attempt-1",
        )


# -- sort success and restart --------------------------------------------


@pytest.mark.asyncio
async def test_queued_job_sorts_validates_and_persists_topics(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    outcome = await harness.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.RESULT_PERSISTED
    # Topics persisted as ordinary tendrils with full-source coverage; no claim.
    tendrils = repository.list_tendrils(bundle_id=bundle.id)
    assert tendrils
    assert all(item.status == TendrilState.OPEN for item in tendrils)
    units = repository.get_bundle(bundle.id).source_units
    assert {unit.unit_id for unit in units} <= {
        entry.unit_id for entry in repository._load_coverage(
            repository.latest_attempt(bundle.id)["id"]
        )
    }
    with pytest.raises(NotFoundError):
        repository.get_claim_for_bundle(bundle.id)
    assert repository.get_bundle(bundle.id).status == BundleState.REVIEW_READY


@pytest.mark.asyncio
async def test_queued_job_resumes_after_restart(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    provider = SyntheticAutonomousSortProvider()
    first = Harness(repository, provider, FakePublisher(), FakeChannelFactory())
    first.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    # "Restart": a brand-new service picks the durable QUEUED job up.
    second = _make_harness(repository)
    outcome = await second.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.RESULT_PERSISTED
    assert second.service.sort_provider.call_count == 1
    assert provider.call_count == 0  # the old process never made the call


@pytest.mark.asyncio
async def test_crash_after_model_request_boundary_never_auto_retries(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    queued = harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    job = queued.job
    # Simulate the worker having crossed the paid-request boundary and crashed:
    # the attempt row and the REQUEST_STARTED phase commit atomically.
    attempt_id, created = repository.begin_autonomous_attempt(
        job.id, bundle.id, "attempt-1"
    )
    assert created
    # A restarted worker must discover this durable crash boundary so it can
    # mark the request ambiguous instead of leaving the job stuck forever.
    assert [item.id for item in harness.service.actionable_jobs()] == [job.id]
    outcome = await harness.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.REQUEST_AMBIGUOUS
    assert harness.service.sort_provider.call_count == 0
    attempt = repository.get_autonomous_attempt(attempt_id)
    assert attempt.status == AutonomousAttemptStatus.AMBIGUOUS
    # A second scan never makes a paid request either.
    await harness.process(bundle.id)
    assert harness.service.sort_provider.call_count == 0
    assert repository.get_autonomous_job(bundle.id).phase == AutonomousJobPhase.REQUEST_AMBIGUOUS
    assert repository.get_bundle(bundle.id).status == BundleState.AUTONOMOUS_FAILED


@pytest.mark.asyncio
async def test_crash_before_model_request_boundary_resumes_normally(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    # QUEUED means the request was provably never sent: resume sorts safely.
    outcome = await harness.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.RESULT_PERSISTED
    assert harness.service.sort_provider.call_count == 1


@pytest.mark.asyncio
async def test_queue_crash_window_between_job_and_bundle_transition_recovers(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    # Simulate a crash after queue()'s job insert committed but before its
    # SEALED -> AUTONOMOUS_SORTING transition: the durable job exists while the
    # bundle is still SEALED. The worker must repair the gap (no second job, no
    # duplicate provider request) and the flow must still reach publishing.
    _job, created = repository.create_autonomous_job(
        bundle.id,
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    assert created
    assert repository.get_bundle(bundle.id).status == BundleState.SEALED

    outcome = await harness.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.RESULT_PERSISTED
    assert harness.service.sort_provider.call_count == 1
    assert repository.get_bundle(bundle.id).status == BundleState.REVIEW_READY

    completed = await harness.process(bundle.id)
    assert completed.phase == AutonomousJobPhase.COMPLETED
    assert repository.get_bundle(bundle.id).status == BundleState.COMPLETED
    assert len(harness.factory.created) == len(
        repository.list_tendrils(bundle_id=bundle.id)
    )


@pytest.mark.asyncio
async def test_validated_sort_restart_resumes_into_publishing(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    queued = harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    # Run the worker exactly up to the point after the validated topics were
    # persisted but BEFORE the bundle transition to REVIEW_READY (the crash
    # window between the two writes).
    attempt_id, created = repository.begin_autonomous_attempt(
        queued.job.id, bundle.id, "attempt-1"
    )
    assert created
    request = AutonomousSortRequest(
        bundle_id=bundle.id,
        messages=bundle.source_messages,
        source_units=bundle.source_units,
    )
    response = await harness.service.sort_provider.sort(request)
    report = validate_autonomous_sort(bundle, bundle.source_units, response.result)
    assert report.valid
    repository.persist_autonomous_success(
        attempt_id=attempt_id,
        job_id=queued.job.id,
        bundle_id=bundle.id,
        result=response.result,
        receipt=response.receipt,
        report=report,
    )
    # Crash here: topics persisted, job RESULT_PERSISTED, bundle still
    # AUTONOMOUS_SORTING, and no new provider request may be made on resume.
    assert repository.get_bundle(bundle.id).status == BundleState.AUTONOMOUS_SORTING

    restarted = _make_harness(repository)
    resumed = await restarted.process(bundle.id)
    assert resumed.phase == AutonomousJobPhase.COMPLETED
    assert restarted.service.sort_provider.call_count == 0
    assert repository.get_bundle(bundle.id).status == BundleState.COMPLETED
    assert len(restarted.factory.created) == len(
        repository.list_tendrils(bundle_id=bundle.id)
    )


# -- failures, receipts, retry -------------------------------------------


@pytest.mark.asyncio
async def test_provider_failure_fails_closed_without_tendrils(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository, variant="provider_failure")
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    outcome = await harness.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.FAILED
    assert repository.list_tendrils(bundle_id=bundle.id) == []
    assert repository.get_bundle(bundle.id).status == BundleState.AUTONOMOUS_FAILED
    # The author was notified once with retry instructions.
    assert len(harness.notifications) == 1
    assert "/autonomous-retry" in harness.notifications[0][1]
    attempts = repository.list_autonomous_attempts(bundle.id)
    assert attempts[0].status == AutonomousAttemptStatus.FAILED


@pytest.mark.asyncio
async def test_invalid_response_retains_receipt_and_raw_output(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository, variant="invalid_schema")
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    outcome = await harness.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.FAILED
    assert repository.list_tendrils(bundle_id=bundle.id) == []
    attempts = repository.list_autonomous_attempts(bundle.id)
    assert len(attempts) == 1
    assert attempts[0].raw_response == '{"not": "an autonomous sort"}'
    assert attempts[0].receipt is not None
    assert attempts[0].receipt.outcome == "invalid_response"


@pytest.mark.asyncio
async def test_validation_failure_rejected_with_no_trusted_tendrils(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository, variant="invented_quote")
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    outcome = await harness.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.FAILED
    assert repository.list_tendrils(bundle_id=bundle.id) == []
    attempts = repository.list_autonomous_attempts(bundle.id)
    assert attempts[0].validation_json is not None
    assert "INVENTED_QUOTE" in attempts[0].validation_json
    # The bundle failed closed: nothing publishable.
    assert repository.get_bundle(bundle.id).status == BundleState.AUTONOMOUS_FAILED


@pytest.mark.asyncio
async def test_owner_retry_after_failure_uses_fresh_key_without_resealing(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository, variant="provider_failure")
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    await harness.process(bundle.id)
    assert repository.get_autonomous_job(bundle.id).phase == AutonomousJobPhase.FAILED

    before = repository.list_source_messages(bundle.id)
    retried = harness.service.retry(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="autonomous-retry:interaction-99",
    )
    assert retried.phase == AutonomousJobPhase.QUEUED
    assert retried.pending_attempt_key == "autonomous-retry:interaction-99"
    # The sealed source was never resealed or mutated.
    assert repository.list_source_messages(bundle.id) == before
    assert repository.get_bundle(bundle.id).status == BundleState.AUTONOMOUS_SORTING

    # A now-valid provider completes the retried job end to end.
    good = _make_harness(repository)
    outcome = await good.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.RESULT_PERSISTED
    assert good.service.sort_provider.call_count == 1


def test_retry_authorization_and_phase_guards(repository, synthetic_messages) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    with pytest.raises(AuthorizationError):
        harness.service.retry(
            bundle.id, actor_user_id="person-2", idempotency_key="retry-1"
        )
    with pytest.raises(ConflictError):
        harness.service.retry(
            bundle.id, actor_user_id="person-1", idempotency_key="retry-1"
        )


@pytest.mark.asyncio
async def test_duplicate_retry_delivery_replays_queued_job(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository, variant="provider_failure")
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    await harness.process(bundle.id)

    first = harness.service.retry(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="retry-delivery-1",
    )
    replayed = harness.service.retry(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="retry-delivery-1",
    )

    assert first == replayed
    assert replayed.phase == AutonomousJobPhase.QUEUED
    assert len(repository.list_autonomous_attempts(bundle.id)) == 1


@pytest.mark.asyncio
async def test_retry_resumes_if_bundle_transition_committed_before_requeue(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository, variant="provider_failure")
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    await harness.process(bundle.id)
    job = repository.get_autonomous_job(bundle.id)
    retry_key = stable_autonomous_key(bundle.id, "retry-sorting:retry-crash-1")

    # Simulate a process exit after the safer bundle-first commit and before
    # retry_autonomous_job. Replaying the interaction must finish the requeue.
    repository.transition_bundle(
        bundle.id,
        BundleState.AUTONOMOUS_SORTING,
        "person-1",
        retry_key,
        {"job_id": job.id},
    )
    assert repository.get_autonomous_job(bundle.id).phase == AutonomousJobPhase.FAILED

    recovered = harness.service.retry(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="retry-crash-1",
    )
    assert recovered.phase == AutonomousJobPhase.QUEUED
    assert repository.get_bundle(bundle.id).status == BundleState.AUTONOMOUS_SORTING


@pytest.mark.asyncio
async def test_multiple_failed_retries_transition_and_notify_each_time(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository, variant="provider_failure")
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )

    first = await harness.process(bundle.id)
    assert first.phase == AutonomousJobPhase.FAILED
    assert len(harness.notifications) == 1

    harness.service.retry(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="retry-attempt-2",
    )
    second = await harness.process(bundle.id)
    assert second.phase == AutonomousJobPhase.FAILED
    assert repository.get_bundle(bundle.id).status == BundleState.AUTONOMOUS_FAILED
    assert len(harness.notifications) == 2

    # A later deliberate retry can still reach validated output and publishing;
    # prior lifecycle keys do not strand the bundle in AUTONOMOUS_FAILED.
    harness.service.retry(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="retry-attempt-3",
    )
    good = _make_harness(repository)
    sorted_outcome = await good.process(bundle.id)
    assert sorted_outcome.phase == AutonomousJobPhase.RESULT_PERSISTED
    completed = await good.process(bundle.id)
    assert completed.phase == AutonomousJobPhase.COMPLETED
    assert repository.get_bundle(bundle.id).status == BundleState.COMPLETED


@pytest.mark.asyncio
async def test_ambiguous_job_requires_retry_and_never_repeats_paid_call(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    queued = harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    repository.begin_autonomous_attempt(queued.job.id, bundle.id, "attempt-1")
    await harness.process(bundle.id)
    assert harness.service.sort_provider.call_count == 0

    retried = harness.service.retry(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="retry-fresh-2",
    )
    assert retried.phase == AutonomousJobPhase.QUEUED
    outcome = await harness.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.RESULT_PERSISTED
    assert harness.service.sort_provider.call_count == 1


# -- publishing -----------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_automatic_multi_channel_publishing(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    await harness.process(bundle.id)  # sort -> RESULT_PERSISTED
    outcome = await harness.process(bundle.id)  # publish -> COMPLETED
    assert outcome.phase == AutonomousJobPhase.COMPLETED
    tendrils = repository.list_tendrils(bundle_id=bundle.id)
    assert len(harness.factory.created) == len(tendrils)
    assert len(harness.publisher.published) == len(tendrils)
    assert all(
        repository.get_tendril(item.id).status == TendrilState.ROUTED
        for item in tendrils
    )
    assert repository.get_bundle(bundle.id).status == BundleState.COMPLETED
    # Completion notification went to the author with channel links.
    assert len(harness.notifications) == 1
    author_id, text = harness.notifications[0]
    assert author_id == "person-1"
    assert "ch-" in text and "complete" in text.lower()
    assert "<#" in text


@pytest.mark.asyncio
async def test_completed_job_publishing_never_duplicates_channels_or_posts(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    await harness.process(bundle.id)
    await harness.process(bundle.id)
    created = list(harness.factory.created)
    published = list(harness.publisher.published)
    notifications = len(harness.notifications)
    # A repeated scan (restart) reports already published and duplicates nothing.
    again = await harness.process(bundle.id)
    assert again.phase == AutonomousJobPhase.COMPLETED
    assert harness.factory.created == created
    assert harness.publisher.published == published
    assert len(harness.notifications) == notifications


@pytest.mark.asyncio
async def test_partial_channel_failure_requires_reconciliation_and_resumes(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository, fail_creates=1)
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    await harness.process(bundle.id)
    outcome = await harness.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.PUBLISH_RECONCILIATION_REQUIRED
    assert outcome.publish is not None
    assert outcome.publish.of_status("reconciliation_required")
    assert outcome.publish.of_status("published")
    assert len(harness.notifications) == 1

    # A repeat scan must not repeat the ambiguous side effect.
    created_before = len(harness.factory.created)
    await harness.process(bundle.id)
    assert len(harness.factory.created) == created_before

    # Operator confirms no external side effect occurred and abandons.
    ops = repository.list_external_operations()
    pending = [op for op in ops if op.status.value != "COMPLETED"]
    assert len(pending) == 1
    repository.reconcile_publish_operation(pending[0].id, abandon=True)

    # The job resumes publishing without a new provider call.
    provider_calls = harness.service.sort_provider.call_count
    resumed = await harness.process(bundle.id)
    assert resumed.phase == AutonomousJobPhase.COMPLETED
    assert harness.service.sort_provider.call_count == provider_calls
    assert repository.get_bundle(bundle.id).status == BundleState.COMPLETED
    assert len(harness.factory.created) == len(
        repository.list_tendrils(bundle_id=bundle.id)
    )


@pytest.mark.asyncio
async def test_channel_created_but_post_unknown_needs_operator_ids(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository, fail_first=True)
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    await harness.process(bundle.id)
    outcome = await harness.process(bundle.id)
    assert outcome.phase == AutonomousJobPhase.PUBLISH_RECONCILIATION_REQUIRED

    pending = [
        op
        for op in repository.list_external_operations()
        if op.status.value != "COMPLETED"
    ]
    assert len(pending) == 1
    # The channel was created (its ID is retained) but the post is unknown.
    assert pending[0].external_destination_id is not None

    # Operator supplies the observed message ID; completion registers the route.
    repository.reconcile_publish_operation(
        pending[0].id, external_message_id="observed-msg-1"
    )
    resumed = await harness.process(bundle.id)
    assert resumed.phase == AutonomousJobPhase.COMPLETED
    assert repository.get_bundle(bundle.id).status == BundleState.COMPLETED


@pytest.mark.asyncio
async def test_channel_write_gate_blocks_publishing_but_keeps_topics(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    await harness.process(bundle.id)
    outcome = await harness.process(bundle.id, allow_channel_write=False)
    # Nothing published; validated topics stay persisted for a gated retry.
    assert outcome.phase == AutonomousJobPhase.RESULT_PERSISTED
    assert harness.factory.created == []
    assert repository.list_tendrils(bundle_id=bundle.id)
    # Once the gate is enabled, the same scan path completes publishing.
    resumed = await harness.process(bundle.id, allow_channel_write=True)
    assert resumed.phase == AutonomousJobPhase.COMPLETED


@pytest.mark.asyncio
async def test_cancelled_bundle_is_never_sorted(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository)
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    CaptureService(repository).cancel(
        bundle.id, actor_user_id="person-1", idempotency_key="cancel-1"
    )
    outcome = await harness.process(bundle.id)
    # No provider request for a cancelled bundle; the job waits for operators.
    assert harness.service.sort_provider.call_count == 0
    assert outcome.phase == AutonomousJobPhase.QUEUED


# -- export / evidence views ---------------------------------------------


@pytest.mark.asyncio
async def test_export_includes_autonomous_job_without_raw_output(
    repository, synthetic_messages
) -> None:
    bundle = _seal_autonomous(repository, synthetic_messages)
    harness = _make_harness(repository, variant="invalid_schema")
    harness.service.queue(
        bundle.id,
        actor_user_id="person-1",
        idempotency_key="seal-event-1",
        pending_attempt_key="attempt-1",
    )
    await harness.process(bundle.id)
    exported = repository.export_bundle(bundle.id)
    assert exported["autonomous_job"]["phase"] == "FAILED"
    assert exported["bundle"]["autonomous_mode"] is True
    for attempt in exported["autonomous_attempts"]:
        assert "raw_response" not in attempt
