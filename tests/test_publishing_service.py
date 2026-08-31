"""Provider-neutral publishing service: preview, publish, and reconciliation."""

from __future__ import annotations

import pytest

from nemoir.adapters.channel_naming import derive_channel_slug
from nemoir.application.analysis_service import AnalysisService
from nemoir.application.publishing_service import (
    PublishingService,
    stable_publish_key,
)
from nemoir.domain.errors import ConflictError, NotFoundError
from nemoir.domain.states import ExternalOperationStatus, TendrilState
from nemoir.providers.fake import FakeAnalysisProvider


class FakePublisher:
    platform = "discord"

    def __init__(self, *, fail_tendrils=frozenset(), message_prefix="msg") -> None:
        self.published: list[tuple[str, str, str]] = []
        self.fail_tendrils = set(fail_tendrils)
        self.message_prefix = message_prefix
        self.call_count = 0

    async def publish(self, tendril, external_destination_id):
        self.call_count += 1
        if tendril.id in self.fail_tendrils:
            raise RuntimeError("synthetic publish failure")
        message_id = f"{self.message_prefix}-{self.call_count}"
        self.published.append((tendril.id, external_destination_id, message_id))
        return message_id


class FakeChannelFactory:
    platform = "discord"

    def __init__(self, *, existing=frozenset(), fail_list=False) -> None:
        self.existing = set(existing)
        self.fail_list = fail_list
        self.created: list[tuple[str, str]] = []

    async def list_channel_names(self, category_id):
        if self.fail_list:
            raise NotFoundError("synthetic category unavailable")
        return set(self.existing)

    async def create_channel(self, slug, category_id):
        channel_id = f"ch-{len(self.created) + 1}"
        self.created.append((slug, channel_id))
        self.existing.add(slug)
        return channel_id


async def _review_ready(
    repository, captured_bundle, synthetic_analysis, authorization
):
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="publish-analyse",
    )
    return repository.get_bundle(captured_bundle.id)


def _service(repository, authorization, publisher, factory):
    return PublishingService(repository, authorization, publisher, factory)


@pytest.mark.asyncio
async def test_preview_is_read_only(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    service = _service(repository, authorization, FakePublisher(), FakeChannelFactory())
    preview = service.preview(
        captured_bundle.id, actor_user_id="person-2"
    )
    assert len(preview.items) == 5
    assert {item.channel_slug for item in preview.items} == {
        "cosmology-and-life",
        "consciousness-and-reality",
        "free-will-and-consequence",
        "human-ai-collaboration",
        "nemoir",
    }
    assert all(item.evidence_count >= 1 for item in preview.items)
    # Preview performs zero writes and zero external calls.
    assert not repository.list_external_operations()
    assert repository.get_route_for_tendril(preview.items[0].tendril_id) is None
    assert repository.list_tendrils(bundle_id=captured_bundle.id)[0].status == TendrilState.OPEN


@pytest.mark.asyncio
async def test_successful_multi_tendril_publishing(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    publisher = FakePublisher()
    factory = FakeChannelFactory()
    service = _service(repository, authorization, publisher, factory)

    outcome = await service.publish_bundle(
        captured_bundle.id,
        actor_user_id="person-2",
        guild_id="guild-1",
        category_id="cat-1",
    )

    assert {item.status for item in outcome.items} == {"published"}
    assert len(outcome.of_status("published")) == 5
    assert len(factory.created) == 5
    assert len(publisher.published) == 5
    # Claimed material is never published: only the five tendril rows get
    # channels, and every published tendril is ROUTED with a single message.
    for item in outcome.items:
        tendril = repository.get_tendril(item.tendril_id)
        assert tendril.status == TendrilState.ROUTED
        assert tendril.routed_external_id is not None
    assert repository.get_bundle(captured_bundle.id).status.value == "REVIEW_READY"


@pytest.mark.asyncio
async def test_claimed_evidence_is_never_published(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    review = repository.get_review(captured_bundle.id)
    claimed_quotes = {fragment.exact_quote for fragment in review.claim.matching_fragments}
    assert claimed_quotes

    publisher = FakePublisher()
    factory = FakeChannelFactory()
    service = _service(repository, authorization, publisher, factory)
    outcome = await service.publish_bundle(
        captured_bundle.id,
        actor_user_id="person-2",
        guild_id="guild-1",
        category_id="cat-1",
    )
    published_ids = {item.tendril_id for item in outcome.of_status("published")}
    # Every published channel is a tendril; the claim has no tendril id and no channel.
    assert published_ids == {item.id for item in repository.list_tendrils(bundle_id=captured_bundle.id)}
    for tendril_id in published_ids:
        tendril = repository.get_tendril(tendril_id)
        assert all(fragment.exact_quote not in claimed_quotes for fragment in tendril.evidence)


@pytest.mark.asyncio
async def test_publish_requires_review_ready(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    from nemoir.domain.errors import ConflictError

    service = _service(repository, authorization, FakePublisher(), FakeChannelFactory())
    with pytest.raises(ConflictError):
        await service.publish_bundle(
            captured_bundle.id,
            actor_user_id="person-2",
            guild_id="guild-1",
            category_id="cat-1",
        )


@pytest.mark.asyncio
async def test_deterministic_collision_resolution(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    # A pre-existing channel already holds the free-will slug.
    factory = FakeChannelFactory(existing={"free-will-and-consequence"})
    service = _service(repository, authorization, FakePublisher(), factory)
    outcome = await service.publish_bundle(
        captured_bundle.id,
        actor_user_id="person-2",
        guild_id="guild-1",
        category_id="cat-1",
    )
    free_will = next(
        item
        for item in outcome.items
        if repository.get_tendril(item.tendril_id).suggested_habitat_slug == "free-will-and-consequence"
    )
    assert free_will.channel_slug == "free-will-and-consequence-2"


@pytest.mark.asyncio
async def test_repeated_publish_replays_as_already_published(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    publisher = FakePublisher()
    factory = FakeChannelFactory()
    service = _service(repository, authorization, publisher, factory)
    await service.publish_bundle(
        captured_bundle.id,
        actor_user_id="person-2",
        guild_id="guild-1",
        category_id="cat-1",
    )
    first_created = len(factory.created)

    outcome = await service.publish_bundle(
        captured_bundle.id,
        actor_user_id="person-2",
        guild_id="guild-1",
        category_id="cat-1",
    )
    assert len(outcome.of_status("already_published")) == 5
    assert len(factory.created) == first_created  # no new channels
    assert publisher.call_count == 5  # no new posts


@pytest.mark.asyncio
async def test_one_failing_item_does_not_duplicate_or_roll_back_siblings(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendrils = repository.list_tendrils(bundle_id=captured_bundle.id)
    failing = tendrils[1]  # t-time

    publisher = FakePublisher(fail_tendrils={failing.id})
    factory = FakeChannelFactory()
    service = _service(repository, authorization, publisher, factory)
    outcome = await service.publish_bundle(
        captured_bundle.id,
        actor_user_id="person-2",
        guild_id="guild-1",
        category_id="cat-1",
    )

    assert len(outcome.of_status("published")) == 4
    reconciliation = outcome.of_status("reconciliation_required")
    assert [item.tendril_id for item in reconciliation] == [failing.id]
    assert len(factory.created) == 5  # failing channel was created, then post failed

    # A second run never duplicates a sibling or repeats the ambiguous post.
    publisher2 = FakePublisher()
    outcome2 = await service.publish_bundle(
        captured_bundle.id,
        actor_user_id="person-2",
        guild_id="guild-1",
        category_id="cat-1",
    )
    assert len(outcome2.of_status("already_published")) == 4
    assert [item.tendril_id for item in outcome2.of_status("reconciliation_required")] == [failing.id]
    assert len(factory.created) == 5
    assert publisher2.call_count == 0  # ambiguous post never repeated


@pytest.mark.asyncio
async def test_channel_created_message_failed_split_recovery(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendrils = repository.list_tendrils(bundle_id=captured_bundle.id)
    failing = tendrils[0]

    publisher = FakePublisher(fail_tendrils={failing.id})
    factory = FakeChannelFactory()
    service = _service(repository, authorization, publisher, factory)
    await service.publish_bundle(
        captured_bundle.id,
        actor_user_id="person-2",
        guild_id="guild-1",
        category_id="cat-1",
    )
    ops = repository.list_external_operations(tendril_id=failing.id)
    assert ops and ops[0].status == ExternalOperationStatus.NEEDS_RECONCILIATION
    assert ops[0].external_destination_id is not None

    # Operator confirms the post exists and completes the local record, which
    # registers the habitat and route (never reposting).
    repository.reconcile_publish_operation(ops[0].id, external_message_id="observed-1")
    assert repository.get_tendril(failing.id).status == TendrilState.ROUTED
    assert repository.get_route_for_tendril(failing.id) is not None

    # A fresh run (simulated restart) replays as already published.
    factory2 = FakeChannelFactory()
    service2 = _service(repository, authorization, FakePublisher(), factory2)
    outcome = await service2.publish_bundle(
        captured_bundle.id,
        actor_user_id="person-2",
        guild_id="guild-1",
        category_id="cat-1",
    )
    recovered = next(item for item in outcome.items if item.tendril_id == failing.id)
    assert recovered.status == "already_published"
    assert factory2.created == []  # no new channel
    assert repository.get_tendril(failing.id).status == TendrilState.ROUTED


@pytest.mark.asyncio
async def test_destination_category_unavailable_marks_items_failed(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    factory = FakeChannelFactory(fail_list=True)
    service = _service(repository, authorization, FakePublisher(), factory)
    outcome = await service.publish_bundle(
        captured_bundle.id,
        actor_user_id="person-2",
        guild_id="guild-1",
        category_id="cat-1",
    )
    assert len(outcome.of_status("failed")) == 5
    assert not repository.list_external_operations()


@pytest.mark.asyncio
async def test_participant_and_admin_authorization(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    from nemoir.domain.errors import AuthorizationError

    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    service = _service(repository, authorization, FakePublisher(), FakeChannelFactory())

    with pytest.raises(AuthorizationError):
        await service.publish_bundle(
            captured_bundle.id,
            actor_user_id="intruder",
            guild_id="guild-1",
            category_id="cat-1",
        )

    # Submit participant and admin are both authorised.
    outcome = await service.publish_bundle(
        captured_bundle.id,
        actor_user_id="person-1",
        guild_id="guild-1",
        category_id="cat-1",
    )
    assert len(outcome.of_status("published")) == 5


def test_stable_publish_key_is_derived_from_bundle_and_tendril() -> None:
    assert stable_publish_key("b-1", "t-1") == stable_publish_key("b-1", "t-1")
    assert stable_publish_key("b-1", "t-1") != stable_publish_key("b-1", "t-2")
    assert stable_publish_key("b-1", "t-1") != stable_publish_key("b-2", "t-1")


# -- preview correctness -------------------------------------------------


@pytest.mark.asyncio
async def test_preview_requires_review_ready(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    service = _service(repository, authorization, FakePublisher(), FakeChannelFactory())
    with pytest.raises(ConflictError):
        service.preview(captured_bundle.id, actor_user_id="person-2")


@pytest.mark.asyncio
async def test_preview_classifies_plan_without_writes(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendrils = repository.list_tendrils(bundle_id=captured_bundle.id)
    # tendril[0] already routed, tendril[1] resolved (skipped), the rest will publish.
    repository.record_route(tendrils[0].id, "discord", "ch-99", "msg-99", "person-2", "manual-route")
    repository.transition_tendril(tendrils[1].id, TendrilState.RESOLVED, "person-2", "resolve-1")

    service = _service(repository, authorization, FakePublisher(), FakeChannelFactory())
    preview = service.preview(captured_bundle.id, actor_user_id="person-2")
    assert [item.tendril_id for item in preview.of_status("already_published")] == [tendrils[0].id]
    assert [item.tendril_id for item in preview.of_status("skipped")] == [tendrils[1].id]
    assert len(preview.of_status("will_publish")) == 3
    already = preview.of_status("already_published")[0]
    # The already-published item carries its channel id, not a proposed slug.
    assert already.channel_slug is None
    assert already.channel_id == "ch-99"
    # Preview remains read-only: it never reserves a new operation.
    assert not repository.list_external_operations()


@pytest.mark.asyncio
async def test_preview_resolves_duplicate_slugs_deterministically(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    # Force two tendrils to share the same suggested slug.
    tendrils = repository.list_tendrils(bundle_id=captured_bundle.id)
    repository._connection.execute(
        "UPDATE tendrils SET suggested_habitat_slug = 'shared-topic' WHERE id IN (?, ?)",
        (tendrils[0].id, tendrils[1].id),
    )
    repository._connection.commit()

    service = _service(repository, authorization, FakePublisher(), FakeChannelFactory())
    preview = service.preview(captured_bundle.id, actor_user_id="person-2")
    slugs = [item.channel_slug for item in preview.of_status("will_publish")]
    assert slugs.count("shared-topic") == 1
    assert "shared-topic-2" in slugs


@pytest.mark.asyncio
async def test_preview_reports_reconciliation_required(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendrils = repository.list_tendrils(bundle_id=captured_bundle.id)
    repository.reserve_external_operation(
        operation_type="habitat_create",
        idempotency_key=stable_publish_key(captured_bundle.id, tendrils[0].id),
        platform="discord",
        tendril_id=tendrils[0].id,
        actor_user_id="person-2",
        metadata={
            "bundle_id": captured_bundle.id,
            "publish": "true",
            "requested_slug": derive_channel_slug(tendrils[0]),
            "guild_id": "guild-1",
            "category_id": "cat-1",
            "phase": "reserved",
        },
    )
    service = _service(repository, authorization, FakePublisher(), FakeChannelFactory())
    preview = service.preview(captured_bundle.id, actor_user_id="person-2")
    assert preview.of_status("reconciliation_required")[0].tendril_id == tendrils[0].id


# -- durable crash/restart boundaries ------------------------------------


def _reserve_publish(repository, tendril, *, guild_id="guild-1", category_id="cat-1"):
    return repository.reserve_external_operation(
        operation_type="habitat_create",
        idempotency_key=stable_publish_key(tendril.bundle_id, tendril.id),
        platform="discord",
        tendril_id=tendril.id,
        actor_user_id="person-2",
        metadata={
            "bundle_id": tendril.bundle_id,
            "publish": "true",
            "requested_slug": derive_channel_slug(tendril),
            "guild_id": guild_id,
            "category_id": category_id,
            "phase": "reserved",
        },
    )


async def _restart_publish(repository, authorization, bundle_id):
    factory = FakeChannelFactory()
    publisher = FakePublisher()
    service = _service(repository, authorization, publisher, factory)
    outcome = await service.publish_bundle(
        bundle_id, actor_user_id="person-2", guild_id="guild-1", category_id="cat-1"
    )
    return outcome, factory, publisher


@pytest.mark.asyncio
async def test_restart_after_reserved_before_channel_creation(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    _reserve_publish(repository, tendril)

    outcome, factory, publisher = await _restart_publish(
        repository, authorization, captured_bundle.id
    )
    item = next(item for item in outcome.items if item.tendril_id == tendril.id)
    assert item.status == "reconciliation_required"
    assert len(factory.created) == 4  # only the four untouched siblings
    assert all(published[0] != tendril.id for published in publisher.published)
    assert repository.get_tendril(tendril.id).status == TendrilState.OPEN


@pytest.mark.asyncio
async def test_restart_after_channel_created_before_post(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    op, _created = _reserve_publish(repository, tendril)
    repository.mark_operation_external_succeeded(op.id, external_destination_id="ch-observed")
    repository.update_operation_metadata(op.id, phase="channel_created")

    outcome, factory, publisher = await _restart_publish(
        repository, authorization, captured_bundle.id
    )
    item = next(item for item in outcome.items if item.tendril_id == tendril.id)
    assert item.status == "reconciliation_required"
    assert item.channel_id == "ch-observed"
    assert len(factory.created) == 4  # never re-create the observed channel
    assert all(published[0] != tendril.id for published in publisher.published)  # never re-post
    assert repository.get_tendril(tendril.id).status == TendrilState.OPEN


@pytest.mark.asyncio
async def test_restart_after_message_persisted_before_route_recording(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    op, _created = _reserve_publish(repository, tendril)
    repository.mark_operation_external_succeeded(op.id, external_destination_id="ch-observed")
    repository.mark_operation_external_succeeded(op.id, external_message_id="msg-observed")
    repository.update_operation_metadata(op.id, phase="message_published")

    outcome, factory, publisher = await _restart_publish(
        repository, authorization, captured_bundle.id
    )
    item = next(item for item in outcome.items if item.tendril_id == tendril.id)
    # Confirmed external success resumes locally: route recorded, no new channel/post.
    assert item.status == "published"
    assert item.channel_id == "ch-observed"
    assert item.message_id == "msg-observed"
    assert len(factory.created) == 4
    assert all(published[0] != tendril.id for published in publisher.published)
    assert repository.get_tendril(tendril.id).status == TendrilState.ROUTED


@pytest.mark.asyncio
async def test_restart_after_route_recorded_before_completion(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    op, _created = _reserve_publish(repository, tendril)
    repository.mark_operation_external_succeeded(op.id, external_destination_id="ch-observed")
    repository.mark_operation_external_succeeded(op.id, external_message_id="msg-observed")
    repository.record_route(tendril.id, "discord", "ch-observed", "msg-observed", "person-2", op.idempotency_key)
    # operation not yet completed

    outcome, factory, publisher = await _restart_publish(
        repository, authorization, captured_bundle.id
    )
    item = next(item for item in outcome.items if item.tendril_id == tendril.id)
    assert item.status == "already_published"
    assert len(factory.created) == 4
    assert all(published[0] != tendril.id for published in publisher.published)


# -- operator reconciliation --------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_publish_requires_both_ids(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    op, _created = _reserve_publish(repository, tendril)

    with pytest.raises(ConflictError, match="channel ID"):
        repository.reconcile_publish_operation(op.id, external_message_id="msg-only")

    repository.mark_operation_external_succeeded(op.id, external_destination_id="ch-observed")
    with pytest.raises(ConflictError, match="message ID"):
        repository.reconcile_publish_operation(op.id)


@pytest.mark.asyncio
async def test_reconcile_publish_completes_habitat_and_route_then_replays(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    # Channel was created (and its ID retained), but the post result was lost.
    op, _created = _reserve_publish(repository, tendril)
    repository.mark_operation_external_succeeded(op.id, external_destination_id="ch-observed")
    repository.mark_operation_needs_reconciliation(op.id, reason="publish result unknown")

    reconciled = repository.reconcile_publish_operation(op.id, external_message_id="msg-observed")
    assert reconciled.status == ExternalOperationStatus.COMPLETED
    # Reconciliation registered the habitat and route where appropriate.
    assert repository.get_route_for_tendril(tendril.id) is not None
    assert repository.get_tendril(tendril.id).status == TendrilState.ROUTED

    # A subsequent publish replays as already published with no new side effect.
    outcome, factory, publisher = await _restart_publish(
        repository, authorization, captured_bundle.id
    )
    item = next(item for item in outcome.items if item.tendril_id == tendril.id)
    assert item.status == "already_published"
    assert len(factory.created) == 4
    assert all(published[0] != tendril.id for published in publisher.published)


@pytest.mark.asyncio
async def test_abandon_frees_tendril_for_retry(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    op, _created = _reserve_publish(repository, tendril)
    repository.mark_operation_needs_reconciliation(op.id, reason="channel creation ambiguous")

    repository.reconcile_publish_operation(op.id, abandon=True)
    assert repository.list_external_operations(tendril_id=tendril.id) == []

    # The tendril can now be retried from scratch.
    outcome, factory, publisher = await _restart_publish(
        repository, authorization, captured_bundle.id
    )
    item = next(item for item in outcome.items if item.tendril_id == tendril.id)
    assert item.status == "published"
    assert len(factory.created) == 5


@pytest.mark.asyncio
async def test_abandon_refused_when_channel_id_retained(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    op, _created = _reserve_publish(repository, tendril)
    repository.mark_operation_external_succeeded(op.id, external_destination_id="ch-observed")
    with pytest.raises(ConflictError, match="channel ID"):
        repository.reconcile_publish_operation(op.id, abandon=True)
