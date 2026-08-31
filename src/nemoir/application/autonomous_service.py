"""Durable recipient-free autonomous workflow: queue, sort, publish, retry.

``/seal`` on an autonomous bundle acknowledges immediately and persists one
durable job. A bounded background worker later:

1. moves the job QUEUED -> REQUEST_STARTED in the same transaction that
   creates the provider attempt (the paid-request crash boundary);
2. calls the separate ``AutonomousSortProvider`` (never the claim/selected-
   candidate model);
3. validates the result strictly with ``validate_autonomous_sort`` — invalid
   output fails closed (receipt and raw output retained, no tendrils);
4. persists validated topics as ordinary tendrils and moves the bundle to
   ``REVIEW_READY`` (the state the existing ``PublishingService`` requires);
5. publishes every tendril through the existing publishing machinery and
   completes the bundle.

A resumed REQUEST_STARTED job (crash after the request may have been sent)
becomes REQUEST_AMBIGUOUS and is never retried automatically; only the
owner/admin retry command with a fresh idempotency key may continue. Publishing
resumes safely because the existing per-tendril publish operations are durable
and never repeat ambiguous side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from nemoir.application.authorization import AuthorizationPolicy
from nemoir.application.publishing_service import PublishOutcome, PublishingService
from nemoir.domain.errors import (
    AuthorizationError,
    ConflictError,
    ProviderOutputValidationError,
)
from nemoir.domain.models import (
    AutonomousJob,
    AutonomousSortRequest,
    ConversationBundle,
    ProviderReceipt,
)
from nemoir.domain.states import AutonomousJobPhase, BundleState
from nemoir.domain.validation import (
    ValidationIssue,
    ValidationReport,
    validate_autonomous_sort,
)
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.base import AutonomousSortProvider

# Phases the background worker may act on. REQUEST_STARTED is actionable only
# so a fresh process can convert an in-flight request left by a crashed worker
# to REQUEST_AMBIGUOUS; process_job never repeats the provider call from that
# phase. FAILED and REQUEST_AMBIGUOUS wait for a deliberate owner/admin retry.
_ACTIONABLE_PHASES = frozenset(
    {
        AutonomousJobPhase.QUEUED,
        AutonomousJobPhase.REQUEST_STARTED,
        AutonomousJobPhase.RESULT_PERSISTED,
        AutonomousJobPhase.PUBLISHING,
        AutonomousJobPhase.PUBLISH_RECONCILIATION_REQUIRED,
    }
)

# A stable, interaction-independent transition key per bundle+step so repeated
# or resumed runs replay the same lifecycle event instead of appending noise.
_STABLE_KEY_PREFIX = "autonomous"


def stable_autonomous_key(bundle_id: str, step: str) -> str:
    return f"{_STABLE_KEY_PREFIX}:{bundle_id}:{step}"


@dataclass(frozen=True)
class AutonomousQueueOutcome:
    job: AutonomousJob
    created: bool


@dataclass(frozen=True)
class AutonomousStepOutcome:
    phase: AutonomousJobPhase
    notified: bool = False
    publish: PublishOutcome | None = None


class AutonomousJobService:
    """One durable job per autonomous bundle; bounded, restart-safe processing."""

    def __init__(
        self,
        repository: SQLiteRepository,
        sort_provider: AutonomousSortProvider,
        publishing: PublishingService,
        authorization: AuthorizationPolicy,
        admin_user_ids: set[str] | None = None,
    ) -> None:
        self.repository = repository
        self.sort_provider = sort_provider
        self.publishing = publishing
        self.authorization = authorization
        self.admin_user_ids = admin_user_ids or set()

    # -- queue / retry ----------------------------------------------------

    def queue(
        self,
        bundle_id: str,
        *,
        actor_user_id: str,
        idempotency_key: str,
        pending_attempt_key: str,
    ) -> AutonomousQueueOutcome:
        """Create (or replay) the one durable job for a sealed autonomous bundle.

        Duplicate Discord events with any key resolve to the existing job, so
        they can never create a second job or a second provider attempt. The
        bundle moves SEALED -> AUTONOMOUS_SORTING with a stable key.
        """
        bundle = self.repository.get_bundle(bundle_id)
        if not bundle.autonomous_mode:
            raise ConflictError("Only autonomous bundles may queue autonomous jobs")
        if actor_user_id != bundle.submitter_user_id and actor_user_id not in self.admin_user_ids:
            raise AuthorizationError("Only the capture owner or an administrator may queue the job")
        job, created = self.repository.create_autonomous_job(
            bundle_id,
            idempotency_key=idempotency_key,
            pending_attempt_key=pending_attempt_key,
        )
        if bundle.status == BundleState.SEALED:
            self.repository.transition_bundle(
                bundle_id,
                BundleState.AUTONOMOUS_SORTING,
                bundle.submitter_user_id,
                stable_autonomous_key(bundle_id, "sorting"),
                {"job_id": job.id},
            )
        return AutonomousQueueOutcome(job=job, created=created)

    def retry(
        self,
        bundle_id: str,
        *,
        actor_user_id: str,
        idempotency_key: str,
    ) -> AutonomousJob:
        """Deliberate owner/admin retry for a FAILED or REQUEST_AMBIGUOUS job.

        Uses a fresh attempt idempotency key and never reseals or mutates the
        captured source. The bundle moves back to AUTONOMOUS_SORTING with a
        stable key so the worker can pick the requeued job up.
        """
        bundle = self.repository.get_bundle(bundle_id)
        if actor_user_id != bundle.submitter_user_id and actor_user_id not in self.admin_user_ids:
            raise AuthorizationError("Only the capture owner or an administrator may retry")
        job = self.repository.get_autonomous_job(bundle_id)
        retry_transition_key = stable_autonomous_key(
            bundle_id, f"retry-sorting:{idempotency_key}"
        )
        prior_retry = self.repository.find_lifecycle_event(
            "bundle", retry_transition_key
        )
        retry_already_queued_or_started = (
            job.pending_attempt_key == idempotency_key
            or any(
                attempt.idempotency_key == idempotency_key
                for attempt in self.repository.list_autonomous_attempts(bundle_id)
            )
        )
        # Discord may redeliver the same interaction after the retry has
        # already committed. Replay the durable result instead of rejecting a
        # now-QUEUED/RUNNING job. If the bundle transition committed but the
        # job requeue did not, the failed phase below deliberately continues
        # so the interrupted retry can finish.
        if retry_already_queued_or_started or (
            prior_retry is not None
            and job.phase
            not in {
                AutonomousJobPhase.FAILED,
                AutonomousJobPhase.REQUEST_AMBIGUOUS,
            }
        ):
            return job
        if job.phase not in {
            AutonomousJobPhase.FAILED,
            AutonomousJobPhase.REQUEST_AMBIGUOUS,
        }:
            raise ConflictError(
                f"Autonomous retry is available only for FAILED or REQUEST_AMBIGUOUS "
                f"jobs, not {job.phase.value}"
            )
        if bundle.status not in {BundleState.AUTONOMOUS_FAILED, BundleState.AUTONOMOUS_SORTING}:
            raise ConflictError(f"Bundle cannot be retried from {bundle.status.value}")
        # Move the bundle first. A crash here leaves a non-actionable failed
        # job which the owner can safely retry; requeueing first could let the
        # worker persist a result while the bundle was still marked failed.
        if bundle.status == BundleState.AUTONOMOUS_FAILED:
            self.repository.transition_bundle(
                bundle_id,
                BundleState.AUTONOMOUS_SORTING,
                bundle.submitter_user_id,
                retry_transition_key,
                {"job_id": job.id},
            )
        self.repository.retry_autonomous_job(job.id, pending_attempt_key=idempotency_key)
        return self.repository.get_autonomous_job(bundle_id)

    def actionable_jobs(self) -> list[AutonomousJob]:
        """Jobs the background worker may act on right now."""
        return self.repository.list_autonomous_jobs(phases=_ACTIONABLE_PHASES)

    # -- worker step ------------------------------------------------------

    async def process_job(
        self,
        bundle_id: str,
        *,
        guild_id: str,
        category_id: str,
        allow_channel_write: bool,
        notify: Callable[[ConversationBundle, str], Awaitable[None]],
    ) -> AutonomousStepOutcome:
        """Advance one job one durable phase; never raises provider exceptions."""
        job = self.repository.get_autonomous_job(bundle_id)
        if job.phase == AutonomousJobPhase.QUEUED:
            return await self._run_sort(job, notify=notify)
        if job.phase == AutonomousJobPhase.REQUEST_STARTED:
            return await self._mark_ambiguous(job, notify=notify)
        if job.phase in {
            AutonomousJobPhase.RESULT_PERSISTED,
            AutonomousJobPhase.PUBLISHING,
        }:
            return await self._run_publish(
                job,
                guild_id=guild_id,
                category_id=category_id,
                allow_channel_write=allow_channel_write,
                notify=notify,
            )
        if job.phase == AutonomousJobPhase.PUBLISH_RECONCILIATION_REQUIRED:
            if self.repository.has_unresolved_publish_operations(bundle_id):
                # An ambiguous external side effect must be reconciled by an
                # operator; nothing is repeated automatically.
                return AutonomousStepOutcome(job.phase)
            self.repository.update_autonomous_job_phase(
                job.id, AutonomousJobPhase.PUBLISHING
            )
            job = self.repository.get_autonomous_job(bundle_id)
            return await self._run_publish(
                job,
                guild_id=guild_id,
                category_id=category_id,
                allow_channel_write=allow_channel_write,
                notify=notify,
            )
        return AutonomousStepOutcome(job.phase)

    # -- sorting ----------------------------------------------------------

    async def _run_sort(
        self,
        job: AutonomousJob,
        *,
        notify: Callable[[ConversationBundle, str], Awaitable[None]],
    ) -> AutonomousStepOutcome:
        bundle = self.repository.get_bundle(job.bundle_id)
        if bundle.status not in {
            BundleState.SEALED,
            BundleState.AUTONOMOUS_SORTING,
            BundleState.AUTONOMOUS_FAILED,
        }:
            # A cancelled (or otherwise moved) bundle is never sorted: no
            # provider request is made and the job waits for operator action.
            return AutonomousStepOutcome(job.phase)
        # queue() creates the job and transitions SEALED -> AUTONOMOUS_SORTING
        # in two separate commits: a crash between them leaves a SEALED bundle
        # with a QUEUED job. Sorting may still proceed, but publishing (and the
        # later failure/retry paths) require AUTONOMOUS_SORTING, so repair the
        # gap first with the queue's own stable transition key.
        self._ensure_bundle_state(
            job.bundle_id,
            BundleState.SEALED,
            BundleState.AUTONOMOUS_SORTING,
            step="sorting",
        )
        if job.pending_attempt_key is None:
            # Nothing may have been requested yet, but there is no fresh key to
            # use: fail safely so an owner/admin retry can supply one.
            return self._fail_without_attempt(job, "no pending attempt key", notify=notify)
        attempt_id, created = self.repository.begin_autonomous_attempt(
            job.id, job.bundle_id, job.pending_attempt_key
        )
        if not created:
            # A repeated key: the original attempt owns the provider call.
            attempt = self.repository.get_autonomous_attempt(attempt_id)
            if attempt.status.value == "RUNNING":
                return AutonomousStepOutcome(AutonomousJobPhase.REQUEST_STARTED)
            return AutonomousStepOutcome(self.repository.get_autonomous_job(job.bundle_id).phase)

        request = AutonomousSortRequest(
            bundle_id=job.bundle_id,
            messages=bundle.source_messages,
            source_units=bundle.source_units,
        )
        try:
            response = await self.sort_provider.sort(request)
        except ProviderOutputValidationError as exc:
            report = ValidationReport(
                errors=[
                    ValidationIssue(
                        code="PROVIDER_OUTPUT_INVALID",
                        message=(
                            "Provider autonomous-sort output failed the contract with "
                            f"{exc.validation_error_count} validation error(s)"
                        ),
                    )
                ]
            )
            return await self._fail(
                job, attempt_id, report, exc.receipt, exc.raw_response, notify=notify
            )
        except Exception as exc:
            report = ValidationReport(
                errors=[
                    ValidationIssue(
                        code="PROVIDER_FAILURE",
                        # Provider exception text may contain transport or
                        # account details. Persist only the safe class name.
                        message=f"Provider raised {type(exc).__name__}",
                    )
                ]
            )
            return await self._fail(job, attempt_id, report, None, None, notify=notify)

        report = validate_autonomous_sort(bundle, bundle.source_units, response.result)
        if not report.valid:
            return await self._fail(
                job,
                attempt_id,
                report,
                response.receipt,
                response.raw_response,
                notify=notify,
            )

        # Validated: persist topics as ordinary tendrils (one transaction), then
        # move the bundle to the state the existing PublishingService requires.
        # A crash between the two steps is repaired by _run_publish below.
        self.repository.persist_autonomous_success(
            attempt_id=attempt_id,
            job_id=job.id,
            bundle_id=job.bundle_id,
            result=response.result,
            receipt=response.receipt,
            report=report,
        )
        self._ensure_bundle_state(
            job.bundle_id,
            BundleState.AUTONOMOUS_SORTING,
            BundleState.REVIEW_READY,
            step=f"review-ready:{attempt_id}",
        )
        return AutonomousStepOutcome(AutonomousJobPhase.RESULT_PERSISTED)

    async def _fail(
        self,
        job: AutonomousJob,
        attempt_id: str,
        report: ValidationReport,
        receipt: ProviderReceipt | None,
        raw_response: str | None,
        *,
        notify: Callable[[ConversationBundle, str], Awaitable[None]],
    ) -> AutonomousStepOutcome:
        self.repository.persist_autonomous_failure(
            attempt_id=attempt_id,
            job_id=job.id,
            report=report,
            receipt=receipt,
            raw_response=raw_response,
        )
        self._ensure_bundle_state(
            job.bundle_id,
            BundleState.AUTONOMOUS_SORTING,
            BundleState.AUTONOMOUS_FAILED,
            step=f"failed:{attempt_id}",
        )
        errors = len(report.errors)
        codes = sorted({issue.code for issue in report.errors})
        notified = await self._notify_once(
            job,
            notify,
            AutonomousJobPhase.FAILED.value,
            (
                f"Bundle `{job.bundle_id}` could not be sorted automatically "
                f"({errors} validation error{'s' if errors != 1 else ''}: "
                f"{', '.join(codes[:5])}). No channels were created and no paid "
                "retry was made. Retry deliberately with "
                f"`/autonomous-retry bundle_id:{job.bundle_id}`."
            ),
        )
        return AutonomousStepOutcome(AutonomousJobPhase.FAILED, notified=notified)

    async def _fail_without_attempt(
        self,
        job: AutonomousJob,
        reason: str,
        *,
        notify: Callable[[ConversationBundle, str], Awaitable[None]],
    ) -> AutonomousStepOutcome:
        # A QUEUED job whose fresh attempt key was lost (defensive path): no
        # provider call is made, and only an explicit retry supplies a new key.
        report = ValidationReport(
            errors=[ValidationIssue(code="NO_PENDING_ATTEMPT_KEY", message=reason)]
        )
        attempt_id, created = self.repository.begin_autonomous_attempt(
            job.id,
            job.bundle_id,
            f"autonomous-no-key:{job.id}",
        )
        if created:
            return await self._fail(job, attempt_id, report, None, None, notify=notify)
        return AutonomousStepOutcome(self.repository.get_autonomous_job(job.bundle_id).phase)

    async def _mark_ambiguous(
        self,
        job: AutonomousJob,
        *,
        notify: Callable[[ConversationBundle, str], Awaitable[None]],
    ) -> AutonomousStepOutcome:
        """Crash boundary: a resumed REQUEST_STARTED job is never auto-retried."""
        all_attempts = self.repository.list_autonomous_attempts(job.bundle_id)
        attempts = [
            attempt
            for attempt in all_attempts
            if attempt.status.value == "RUNNING"
        ]
        if attempts:
            self.repository.mark_autonomous_attempt_ambiguous(job.id, attempts[0].id)
        else:
            self.repository.update_autonomous_job_phase(
                job.id, AutonomousJobPhase.REQUEST_AMBIGUOUS
            )
        self._ensure_bundle_state(
            job.bundle_id,
            BundleState.AUTONOMOUS_SORTING,
            BundleState.AUTONOMOUS_FAILED,
            step=f"failed:{all_attempts[-1].id if all_attempts else job.id}",
        )
        notified = await self._notify_once(
            job,
            notify,
            AutonomousJobPhase.REQUEST_AMBIGUOUS.value,
            (
                f"Bundle `{job.bundle_id}` stopped after a model request may already "
                "have been sent. No automatic paid retry was made and no channels were "
                "created. Retry deliberately with "
                f"`/autonomous-retry bundle_id:{job.bundle_id}`."
            ),
        )
        return AutonomousStepOutcome(
            AutonomousJobPhase.REQUEST_AMBIGUOUS, notified=notified
        )

    # -- publishing -------------------------------------------------------

    async def _run_publish(
        self,
        job: AutonomousJob,
        *,
        guild_id: str,
        category_id: str,
        allow_channel_write: bool,
        notify: Callable[[ConversationBundle, str], Awaitable[None]],
    ) -> AutonomousStepOutcome:
        attempts = self.repository.list_autonomous_attempts(job.bundle_id)
        attempt_marker = attempts[-1].id if attempts else job.id
        bundle = self._ensure_bundle_state(
            job.bundle_id,
            BundleState.AUTONOMOUS_SORTING,
            BundleState.REVIEW_READY,
            step=f"review-ready:{attempt_marker}",
        )
        if bundle.status != BundleState.REVIEW_READY:
            return AutonomousStepOutcome(job.phase)
        if not allow_channel_write:
            # Server-side gate: without NEMOIR_ALLOW_CHANNEL_WRITE the validated
            # topics stay persisted but nothing is published (fail closed).
            return AutonomousStepOutcome(job.phase)
        if job.phase != AutonomousJobPhase.PUBLISHING:
            self.repository.update_autonomous_job_phase(
                job.id, AutonomousJobPhase.PUBLISHING
            )

        outcome = await self.publishing.publish_bundle(
            job.bundle_id,
            actor_user_id=bundle.submitter_user_id,
            guild_id=guild_id,
            category_id=category_id,
        )

        if outcome.of_status("reconciliation_required"):
            self.repository.update_autonomous_job_phase(
                job.id, AutonomousJobPhase.PUBLISH_RECONCILIATION_REQUIRED
            )
            notified = await self._notify_once(
                job,
                notify,
                AutonomousJobPhase.PUBLISH_RECONCILIATION_REQUIRED.value,
                (
                    f"Bundle `{job.bundle_id}` published partially and needs operator "
                    "reconciliation; ambiguous items were never repeated. Inspect with "
                    "`nemoir ops --list` and reconcile or abandon each unresolved "
                    "operation before Nemoir resumes publishing."
                ),
            )
            return AutonomousStepOutcome(
                AutonomousJobPhase.PUBLISH_RECONCILIATION_REQUIRED,
                notified=notified,
                publish=outcome,
            )

        if outcome.of_status("failed"):
            # Deterministic failures (for example the category being
            # unavailable) reserved nothing, so the next scan may retry safely.
            return AutonomousStepOutcome(AutonomousJobPhase.PUBLISHING, publish=outcome)

        # Everything resolved to published / already_published / skipped.
        self.repository.transition_bundle(
            job.bundle_id,
            BundleState.COMPLETED,
            bundle.submitter_user_id,
            stable_autonomous_key(job.bundle_id, "completed"),
            {"job_id": job.id},
        )
        self.repository.update_autonomous_job_phase(job.id, AutonomousJobPhase.COMPLETED)
        notified = await self._notify_once(
            job,
            notify,
            AutonomousJobPhase.COMPLETED.value,
            _format_completion(job.bundle_id, outcome),
        )
        return AutonomousStepOutcome(
            AutonomousJobPhase.COMPLETED, notified=notified, publish=outcome
        )

    # -- helpers ----------------------------------------------------------

    def _ensure_bundle_state(
        self,
        bundle_id: str,
        expected: BundleState,
        target: BundleState,
        *,
        step: str,
    ) -> ConversationBundle:
        """Idempotently move a bundle that is still in ``expected`` to ``target``."""
        bundle = self.repository.get_bundle(bundle_id)
        if bundle.status == expected:
            return self.repository.transition_bundle(
                bundle_id,
                target,
                bundle.submitter_user_id,
                stable_autonomous_key(bundle_id, step),
            )
        return bundle

    async def _notify_once(
        self,
        job: AutonomousJob,
        notify: Callable[[ConversationBundle, str], Awaitable[None]],
        marker: str,
        text: str,
    ) -> bool:
        if job.last_notification == marker:
            return False
        bundle = self.repository.get_bundle(job.bundle_id)
        await notify(bundle, text)
        self.repository.record_autonomous_job_notification(job.id, marker)
        return True


def _format_completion(bundle_id: str, outcome: PublishOutcome) -> str:
    """Compact completion report distinguishing every publish classification."""
    counts: dict[str, int] = {}
    for item in outcome.items:
        counts[item.status] = counts.get(item.status, 0) + 1
    parts = [f"**Nemoir autonomous publish complete** for bundle `{bundle_id}`"]
    labels = {
        "published": "published",
        "already_published": "already published",
        "skipped": "skipped",
        "failed": "failed",
        "reconciliation_required": "reconciliation required",
    }
    for status, label in labels.items():
        parts.append(f"{label}: {counts.get(status, 0)}")
    links = [
        f"<#{item.channel_id}>"
        for item in outcome.items
        if item.channel_id is not None
    ]
    if links:
        parts.append("Channels: " + " ".join(links))
    return "\n".join(parts)
