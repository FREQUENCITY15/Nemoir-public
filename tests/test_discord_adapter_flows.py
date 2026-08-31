"""Deterministic adapter tests using fake Discord objects; no live bot."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
from types import SimpleNamespace

import pytest

from nemoir.adapters.discord_bot import create_discord_bot, format_tendril
from nemoir.application.analysis_service import AnalysisService
from nemoir.application.authorization import AuthorizationPolicy
from nemoir.application.capture_service import CaptureService
from nemoir.config import Settings
from nemoir.domain.models import AnalysisResult
from nemoir.domain.states import BundleState, ExternalOperationStatus, TendrilState
from nemoir.providers.fake import FakeAnalysisProvider


class FakePermissions:
    def __init__(self, manage_channels: bool = False) -> None:
        self.manage_channels = manage_channels


class FakeUser:
    def __init__(self, user_id, *, manage_channels: bool = False) -> None:
        self.id = user_id
        self.bot = False
        self.mention = f"<@{user_id}>"
        self.display_name = f"user-{user_id}"
        self.guild_permissions = FakePermissions(manage_channels)


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.done = False
        self.deferred = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.deferred = True
        self.done = True

    async def send_message(self, content=None, *, ephemeral=False, **kwargs) -> None:
        self.sent.append(content)
        self.done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content=None, *, ephemeral=False, **kwargs) -> None:
        self.sent.append(content)


class FakeChannel:
    def __init__(
        self,
        channel_id: int,
        *,
        name: str = "channel",
        category_id: int | None = None,
        guild=None,
    ) -> None:
        self.id = channel_id
        self.name = name
        self.category_id = category_id
        self.guild = guild
        self.sent: list[str] = []
        self.files: list[object] = []

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    async def send(self, content=None, **kwargs) -> SimpleNamespace:
        self.sent.append(content)
        if kwargs.get("file") is not None:
            self.files.append(kwargs["file"])
        return SimpleNamespace(id=f"{self.id}-{len(self.sent)}")


class FakeCategory:
    def __init__(self, category_id: int, text_channels: list[FakeChannel] | None = None) -> None:
        self.id = category_id
        self.text_channels = text_channels or []
        self.created: list[str] = []

    async def create_text_channel(self, name, *, reason=None) -> FakeChannel:
        self.created.append(name)
        channel = FakeChannel(500 + len(self.created), name=name, category_id=self.id)
        self.text_channels.append(channel)
        return channel


class FakeGuild:
    def __init__(self, guild_id: int, category: FakeCategory | None = None) -> None:
        self.id = guild_id
        self.category = category

    def get_channel(self, channel_id: int):
        if self.category is not None and channel_id == self.category.id:
            return self.category
        return None


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        content: str,
        author: FakeUser,
        channel: FakeChannel,
        guild: FakeGuild,
    ) -> None:
        self.id = message_id
        self.content = content
        self.author = author
        self.channel = channel
        self.guild = guild
        self.jump_url = f"https://discord.invalid/{guild.id}/{channel.id}/{message_id}"
        self.created_at = datetime.now(timezone.utc)


class FakeInteraction:
    def __init__(
        self,
        interaction_id: int,
        user: FakeUser,
        guild: FakeGuild,
        *,
        channel_id: int,
        guild_id: int,
    ) -> None:
        self.id = interaction_id
        self.user = user
        self.guild = guild
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def _make_bot(
    repository,
    synthetic_analysis,
    *,
    admins: set[str] | None = None,
    provider=None,
):
    settings = Settings(
        guild_id="1",
        intake_channel_id="2",
        anemone_category_id="3",
        nursery_channel_id="4",
        admin_user_ids=admins or set(),
    )
    return create_discord_bot(
        settings,
        repository,
        provider or FakeAnalysisProvider(synthetic_analysis),
    )


def _cache_channels(bot, channels: dict[int, FakeChannel]) -> None:
    bot.get_channel = lambda channel_id: channels.get(int(channel_id))  # type: ignore[method-assign]


def _sealed_unclaimed(repository, synthetic_messages):
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="1",
        intake_channel_id="2",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
    )
    for message in synthetic_messages:
        capture.capture_message(
            bundle.id,
            actor_user_id="person-1",
            external_message_id=message.external_message_id,
            author_display_name=message.author_display_name,
            channel_id="2",
            content=message.content,
            source_url=message.source_url,
            timestamp=message.timestamp,
        )
    capture.seal(bundle.id, actor_user_id="person-1", idempotency_key="adapter-seal")
    return bundle


def _long_analysis(synthetic_analysis) -> AnalysisResult:
    """Synthetic analysis whose first tendril carries a very long description."""
    payload = synthetic_analysis.model_dump(mode="python")
    payload["tendrils"][0]["description"] = (
        "A deliberately long routed description. " + ("d" * 5000)
    )
    return AnalysisResult.model_validate(payload)


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached in time")


@pytest.mark.asyncio
async def test_on_message_captures_during_active_session(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="1",
        intake_channel_id="2",
        submitter_user_id="10",
        recipient_user_id="20",
    )
    guild = FakeGuild(1)
    channel = FakeChannel(2, guild=guild)
    user = FakeUser(10)

    async def noop(*_args, **_kwargs) -> None:
        return None

    bot.process_commands = noop  # type: ignore[method-assign]
    message = FakeMessage(101, "A deliberately captured line", user, channel, guild)
    await bot.on_message(message)
    stored = repository.get_bundle(bundle.id)
    assert len(stored.source_messages) == 1
    assert stored.source_messages[0].content == "A deliberately captured line"


@pytest.mark.asyncio
async def test_seal_alerts_recipient_and_is_idempotent_across_interactions(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="1",
        intake_channel_id="2",
        submitter_user_id="10",
        recipient_user_id="20",
    )
    for message in synthetic_messages:
        capture.capture_message(
            bundle.id,
            actor_user_id="10",
            external_message_id=message.external_message_id,
            author_display_name=message.author_display_name,
            channel_id="2",
            content=message.content,
            source_url=message.source_url,
            timestamp=message.timestamp,
        )
    guild = FakeGuild(1)
    seal = bot.tree.get_command("seal").callback
    interaction = FakeInteraction(21, FakeUser(10), guild, channel_id=2, guild_id=1)
    await seal(interaction)
    assert any("<@20>" in text for text in interaction.response.sent + interaction.followup.sent)
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_READY

    repeated = FakeInteraction(21, FakeUser(10), guild, channel_id=2, guild_id=1)
    await seal(repeated)
    # A repeated interaction never re-posts the option menu (the original
    # interaction already sealed the bundle, so this one is refused early).
    assert repeated.followup.sent == []
    events = repository._connection.execute(
        "SELECT COUNT(*) AS count FROM lifecycle_events WHERE entity_id = ?",
        (bundle.id,),
    ).fetchone()["count"]
    assert events == 2  # sealed + options-ready, not duplicated


@pytest.mark.asyncio
async def test_claim_command_refuses_non_recipient(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    bundle = _sealed_unclaimed(repository, synthetic_messages)
    guild = FakeGuild(1)
    claim = bot.tree.get_command("claim").callback
    intruder = FakeInteraction(22, FakeUser(99), guild, channel_id=2, guild_id=1)
    await claim(intruder, topic="some topic", bundle_id=bundle.id)
    assert "refused" in intruder.response.sent[0].lower()
    assert repository.get_bundle(bundle.id).claim_id is None


@pytest.mark.asyncio
async def test_claim_command_posts_background_review_to_nursery(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    bundle = _sealed_unclaimed(repository, synthetic_messages)
    nursery = FakeChannel(4)
    _cache_channels(bot, {4: nursery})
    guild = FakeGuild(1)
    claim = bot.tree.get_command("claim").callback
    recipient = FakeInteraction(23, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await claim(recipient, topic="LLM consciousness and learned compassion")
    assert "Claim recorded" in recipient.response.sent[0]

    def posted() -> bool:
        return any("review ready" in text for text in nursery.sent)

    await _wait_for(posted)
    assert repository.get_bundle(bundle.id).status == BundleState.REVIEW_READY
    assert len(nursery.sent) >= 2  # review summary plus per-tendril posts


@pytest.mark.asyncio
async def test_analysis_retry_is_manual_authorized_and_single_call(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    class FailOnceProvider:
        def __init__(self) -> None:
            self.call_count = 0
            self.success_provider = FakeAnalysisProvider(synthetic_analysis)

        async def analyse(self, request):
            self.call_count += 1
            if self.call_count == 1:
                raise RuntimeError("synthetic first-attempt failure")
            return await self.success_provider.analyse(request)

    provider = FailOnceProvider()
    first = await AnalysisService(repository, provider, authorization).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-retry-initial-failure",
    )
    assert first.state == BundleState.ANALYSIS_FAILED
    assert provider.call_count == 1

    bot = _make_bot(repository, synthetic_analysis, provider=provider)
    nursery = FakeChannel(4)
    _cache_channels(bot, {4: nursery})
    guild = FakeGuild(1)
    retry = bot.tree.get_command("analysis-retry").callback

    intruder = FakeInteraction(61, FakeUser(99), guild, channel_id=2, guild_id=1)
    await retry(intruder, bundle_id=captured_bundle.id)
    assert "refused" in intruder.response.sent[0].lower()
    assert provider.call_count == 1

    recipient = FakeInteraction(
        62, FakeUser("person-2"), guild, channel_id=2, guild_id=1
    )
    await retry(recipient, bundle_id=captured_bundle.id)
    assert "retry queued" in recipient.response.sent[0].lower()

    def review_posted() -> bool:
        return any("review ready" in text for text in nursery.sent)

    await _wait_for(review_posted)
    assert repository.get_bundle(captured_bundle.id).status == BundleState.REVIEW_READY
    assert provider.call_count == 2

    repeated = FakeInteraction(
        62, FakeUser("person-2"), guild, channel_id=2, guild_id=1
    )
    await retry(repeated, bundle_id=captured_bundle.id)
    assert "refused" in repeated.response.sent[0].lower()
    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_route_refuses_destination_outside_anemone_category(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-route-analyse",
    )
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    guild = FakeGuild(1)
    bad_channel = FakeChannel(42, category_id=999, guild=guild)
    route = bot.tree.get_command("route").callback
    interaction = FakeInteraction(24, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await route(interaction, tendril_id=tendril.id, channel=bad_channel)
    assert "refused" in interaction.response.sent[0].lower()
    assert repository.get_tendril(tendril.id).status == TendrilState.OPEN


@pytest.mark.asyncio
async def test_habitat_create_split_failure_never_duplicates_the_channel(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-habitat-analyse",
    )
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    habitat_create = bot.tree.get_command("habitat-create").callback

    # First attempt: the channel is created, then the tendril post is ambiguous
    # (the publisher cannot resolve the destination channel).
    _cache_channels(bot, {})
    first = FakeInteraction(
        31, FakeUser("person-2", manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await habitat_create(first, tendril_id=tendril.id, name="Consciousness & Reality")
    assert "refused" in first.response.sent[0].lower()
    assert category.created == ["consciousness-reality"]
    created_channel = category.text_channels[0]
    operations = repository.list_external_operations(tendril_id=tendril.id)
    assert len(operations) == 1
    assert operations[0].status == ExternalOperationStatus.NEEDS_RECONCILIATION
    assert operations[0].external_destination_id == str(created_channel.id)
    assert (
        repository._connection.execute("SELECT COUNT(*) AS c FROM habitats").fetchone()["c"] == 1
    )

    # Same interaction retry: refused, no second channel.
    await habitat_create(first, tendril_id=tendril.id, name="Consciousness & Reality")
    assert category.created == ["consciousness-reality"]
    # A human retry with a new interaction: refused while unresolved, no second channel.
    retry = FakeInteraction(
        32, FakeUser("person-2", manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await habitat_create(retry, tendril_id=tendril.id, name="Consciousness & Reality")
    assert category.created == ["consciousness-reality"]
    assert len(repository.list_external_operations(tendril_id=tendril.id)) == 1

    # Operator confirms the channel exists and completes the operation.
    repository.reconcile_external_operation(operations[0].id)

    # A fresh invocation now resumes into the existing channel without recreating it.
    _cache_channels(bot, {created_channel.id: created_channel})
    resume = FakeInteraction(
        33, FakeUser("person-2", manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await habitat_create(resume, tendril_id=tendril.id, name="Consciousness & Reality")
    assert category.created == ["consciousness-reality"]
    assert repository.get_tendril(tendril.id).status == TendrilState.ROUTED
    assert repository.get_tendril(tendril.id).routed_external_id == str(created_channel.id)
    assert len(created_channel.sent) == 1
    assert len(repository.list_external_operations(tendril_id=tendril.id)) == 2
    assert all(
        item.status == ExternalOperationStatus.COMPLETED
        for item in repository.list_external_operations(tendril_id=tendril.id)
    )


@pytest.mark.asyncio
async def test_cancel_command_closes_own_capture_and_refuses_others(
    repository, synthetic_analysis
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="1",
        intake_channel_id="2",
        submitter_user_id="10",
        recipient_user_id="20",
    )
    guild = FakeGuild(1)
    cancel = bot.tree.get_command("cancel").callback
    owner = FakeInteraction(41, FakeUser(10), guild, channel_id=2, guild_id=1)
    await cancel(owner)
    assert "cancelled" in owner.response.sent[0].lower()
    assert repository.get_bundle(bundle.id).status == BundleState.CANCELLED

    other = capture.start_capture(
        guild_id="1",
        intake_channel_id="2",
        submitter_user_id="10",
        recipient_user_id="20",
    )
    intruder = FakeInteraction(42, FakeUser(99), guild, channel_id=2, guild_id=1)
    await cancel(intruder)
    assert "refused" in intruder.response.sent[0].lower()
    assert repository.get_bundle(other.id).status == BundleState.CAPTURING


@pytest.mark.asyncio
async def test_admin_can_view_any_evidence_and_non_participant_cannot(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-view-analyse",
    )
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    guild = FakeGuild(1)

    bot = _make_bot(repository, synthetic_analysis, admins={"admin-1"})
    tendril_show = bot.tree.get_command("tendril-show").callback
    intruder = FakeInteraction(51, FakeUser(99), guild, channel_id=2, guild_id=1)
    await tendril_show(intruder, tendril_id=tendril.id)
    assert "refused" in intruder.response.sent[0].lower()

    admin = FakeInteraction(52, FakeUser("admin-1"), guild, channel_id=2, guild_id=1)
    await tendril_show(admin, tendril_id=tendril.id)
    assert tendril.title in admin.response.sent[0]

    bundle_status = bot.tree.get_command("bundle-status").callback
    outsider = FakeInteraction(53, FakeUser(98), guild, channel_id=2, guild_id=1)
    await bundle_status(outsider, bundle_id=captured_bundle.id)
    assert "refused" in outsider.response.sent[0].lower()


@pytest.mark.asyncio
async def test_habitat_create_refuses_user_without_manage_channels(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-habitat-perm-analyse",
    )
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    habitat_create = bot.tree.get_command("habitat-create").callback
    participant_without_permission = FakeInteraction(
        121, FakeUser("person-2", manage_channels=False), guild, channel_id=2, guild_id=1
    )
    await habitat_create(
        participant_without_permission, tendril_id=tendril.id, name="Some habitat"
    )
    assert "refused" in participant_without_permission.response.sent[0].lower()
    assert category.created == []
    assert repository.get_tendril(tendril.id).status == TendrilState.OPEN


@pytest.mark.asyncio
async def test_habitat_create_refuses_non_participant_even_with_manage_channels(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-habitat-authz-analyse",
    )
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    habitat_create = bot.tree.get_command("habitat-create").callback
    outsider = FakeInteraction(
        122, FakeUser(99, manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await habitat_create(outsider, tendril_id=tendril.id, name="Some habitat")
    assert "refused" in outsider.response.sent[0].lower()
    assert category.created == []
    assert not repository.list_external_operations(tendril_id=tendril.id)


@pytest.mark.asyncio
async def test_route_refuses_non_participant_without_reserving_anything(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-route-authz-analyse",
    )
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    destination = FakeChannel(42, category_id=3, guild=FakeGuild(1))
    _cache_channels(bot, {42: destination})
    route = bot.tree.get_command("route").callback
    outsider = FakeInteraction(131, FakeUser(99), FakeGuild(1), channel_id=2, guild_id=1)
    await route(outsider, tendril_id=tendril.id, channel=destination)
    assert "refused" in outsider.response.sent[0].lower()
    assert destination.sent == []
    assert repository.get_tendril(tendril.id).status == TendrilState.OPEN
    assert repository.get_route_for_tendril(tendril.id) is None
    assert not repository.list_external_operations(tendril_id=tendril.id)


@pytest.mark.asyncio
async def test_route_refuses_destination_unavailable_to_the_bot(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-route-missing-analyse",
    )
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    # The destination reports a valid anemone category but the bot cannot
    # resolve the channel, so the side effect is ambiguous: the operation is
    # never repeated automatically.
    destination = FakeChannel(42, category_id=3, guild=FakeGuild(1))
    _cache_channels(bot, {})
    route = bot.tree.get_command("route").callback
    interaction = FakeInteraction(132, FakeUser("person-2"), FakeGuild(1), channel_id=2, guild_id=1)
    await route(interaction, tendril_id=tendril.id, channel=destination)
    assert "refused" in interaction.response.sent[0].lower()
    assert repository.get_tendril(tendril.id).status == TendrilState.OPEN
    assert repository.get_route_for_tendril(tendril.id) is None
    operations = repository.list_external_operations(tendril_id=tendril.id)
    assert len(operations) == 1
    assert operations[0].status == ExternalOperationStatus.NEEDS_RECONCILIATION


@pytest.mark.asyncio
async def test_claim_with_repeated_interaction_id_does_not_duplicate_calls_or_posts(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    provider = FakeAnalysisProvider(synthetic_analysis)
    bot = _make_bot(repository, synthetic_analysis, provider=provider)
    bundle = _sealed_unclaimed(repository, synthetic_messages)
    nursery = FakeChannel(4)
    _cache_channels(bot, {4: nursery})
    guild = FakeGuild(1)
    claim = bot.tree.get_command("claim").callback
    recipient = FakeInteraction(23, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await claim(recipient, topic="LLM consciousness and learned compassion", bundle_id=bundle.id)
    await _wait_for(lambda: any("review ready" in text for text in nursery.sent))
    posts_after_first = len(nursery.sent)

    repeated = FakeInteraction(23, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await claim(repeated, topic="LLM consciousness and learned compassion", bundle_id=bundle.id)
    assert "refused" in repeated.response.sent[0].lower()
    assert provider.call_count == 1
    assert len(nursery.sent) == posts_after_first


@pytest.mark.asyncio
async def test_pull_with_repeated_interaction_id_returns_same_tendril_once(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-pull-repeat-analyse",
    )
    guild = FakeGuild(1)
    pull = bot.tree.get_command("pull").callback
    first = FakeInteraction(81, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await pull(first)
    resurfaced = next(
        item
        for item in repository.list_tendrils(bundle_id=captured_bundle.id)
        if item.status == TendrilState.RESURFACED
    )

    repeated = FakeInteraction(81, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await pull(repeated)
    assert repeated.response.sent[0] == first.response.sent[0]
    count = repository._connection.execute(
        "SELECT COUNT(*) AS count FROM lifecycle_events "
        "WHERE entity_id = ? AND idempotency_key = 'discord-pull:81'",
        (resurfaced.id,),
    ).fetchone()["count"]
    assert count == 1

    fresh = FakeInteraction(82, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await pull(fresh)
    assert fresh.response.sent[0] != first.response.sent[0]


@pytest.mark.asyncio
async def test_resolve_with_repeated_interaction_id_appends_one_event(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-resolve-repeat-analyse",
    )
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    guild = FakeGuild(1)
    resolve = bot.tree.get_command("resolve").callback
    first = FakeInteraction(91, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await resolve(first, tendril_id=tendril.id)
    repeated = FakeInteraction(91, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await resolve(repeated, tendril_id=tendril.id)
    assert repeated.response.sent[0] == first.response.sent[0]
    count = repository._connection.execute(
        "SELECT COUNT(*) AS count FROM lifecycle_events "
        "WHERE entity_id = ? AND idempotency_key = 'discord-resolve:91'",
        (tendril.id,),
    ).fetchone()["count"]
    assert count == 1


@pytest.mark.asyncio
async def test_route_with_repeated_interaction_id_posts_once(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-route-repeat-analyse",
    )
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    destination = FakeChannel(42, category_id=3, guild=FakeGuild(1))
    _cache_channels(bot, {42: destination})
    route = bot.tree.get_command("route").callback
    first = FakeInteraction(101, FakeUser("person-2"), FakeGuild(1), channel_id=2, guild_id=1)
    await route(first, tendril_id=tendril.id, channel=destination)
    repeated = FakeInteraction(101, FakeUser("person-2"), FakeGuild(1), channel_id=2, guild_id=1)
    await route(repeated, tendril_id=tendril.id, channel=destination)
    assert len(destination.sent) == 1
    assert repository.get_tendril(tendril.id).status == TendrilState.ROUTED
    routes = repository._connection.execute(
        "SELECT COUNT(*) AS count FROM routes WHERE tendril_id = ?", (tendril.id,)
    ).fetchone()["count"]
    assert routes == 1


@pytest.mark.asyncio
async def test_promote_actionable_command_acknowledges_and_is_idempotent(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-promote-analyse",
    )
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    guild = FakeGuild(1)
    promote = bot.tree.get_command("promote-actionable").callback
    actor = FakeInteraction(111, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await promote(actor, tendril_id=tendril.id)
    assert "PROMOTED_ACTIONABLE" in actor.response.sent[0]
    assert repository.get_tendril(tendril.id).status == TendrilState.PROMOTED_ACTIONABLE

    repeated = FakeInteraction(111, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await promote(repeated, tendril_id=tendril.id)
    count = repository._connection.execute(
        "SELECT COUNT(*) AS count FROM lifecycle_events "
        "WHERE entity_id = ? AND idempotency_key = 'discord-promote-actionable:111'",
        (tendril.id,),
    ).fetchone()["count"]
    assert count == 1


@pytest.mark.asyncio
async def test_promote_actionable_command_refuses_non_participant(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-promote-authz-analyse",
    )
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    guild = FakeGuild(1)
    promote = bot.tree.get_command("promote-actionable").callback
    outsider = FakeInteraction(112, FakeUser(99), guild, channel_id=2, guild_id=1)
    await promote(outsider, tendril_id=tendril.id)
    assert "refused" in outsider.response.sent[0].lower()
    assert repository.get_tendril(tendril.id).status == TendrilState.OPEN


@pytest.mark.asyncio
async def test_route_posts_one_tracked_message_with_full_evidence_attachment(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    long_analysis = _long_analysis(synthetic_analysis)
    bot = _make_bot(repository, synthetic_analysis, provider=FakeAnalysisProvider(long_analysis))
    await AnalysisService(
        repository, FakeAnalysisProvider(long_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-long-route-analyse",
    )
    tendril = next(
        item
        for item in repository.list_tendrils(bundle_id=captured_bundle.id)
        if len(item.description) > 2000
    )
    destination = FakeChannel(42, category_id=3, guild=FakeGuild(1))
    _cache_channels(bot, {42: destination})
    route = bot.tree.get_command("route").callback
    interaction = FakeInteraction(141, FakeUser("person-2"), FakeGuild(1), channel_id=2, guild_id=1)
    await route(interaction, tendril_id=tendril.id, channel=destination)

    # Exactly one tracked Discord message carries the whole routed tendril.
    assert len(destination.sent) == 1
    assert len(destination.files) == 1
    assert len(destination.sent[0]) <= 2000
    assert "attached" in destination.sent[0].lower()
    attachment_text = destination.files[0].fp.read().decode("utf-8")
    assert tendril.title in attachment_text
    assert tendril.description in attachment_text
    assert all(fragment.exact_quote in attachment_text for fragment in tendril.evidence)
    route_row = repository.get_route_for_tendril(tendril.id)
    assert route_row["external_message_id"] == "42-1"


@pytest.mark.asyncio
async def test_tendril_show_paginates_long_content_without_truncation(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    long_analysis = _long_analysis(synthetic_analysis)
    bot = _make_bot(repository, synthetic_analysis, provider=FakeAnalysisProvider(long_analysis))
    await AnalysisService(
        repository, FakeAnalysisProvider(long_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="adapter-long-show-analyse",
    )
    tendril = next(
        item
        for item in repository.list_tendrils(bundle_id=captured_bundle.id)
        if len(item.description) > 2000
    )
    guild = FakeGuild(1)
    tendril_show = bot.tree.get_command("tendril-show").callback
    interaction = FakeInteraction(151, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await tendril_show(interaction, tendril_id=tendril.id)
    pages = interaction.response.sent + interaction.followup.sent
    assert len(pages) > 1
    assert all(len(page) <= 2000 for page in pages)
    reconstructed = "".join(
        re.sub(r"\n— page \d+/\d+ —$", "", page) for page in pages
    )
    assert reconstructed == format_tendril(tendril, repository)
