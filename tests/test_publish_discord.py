"""Adapter tests for the /publish-bundle command using fake Discord objects."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nemoir.adapters.discord_bot import (
    create_discord_bot,
    format_tendril,
    publish_tendril_message,
)
from nemoir.adapters.pagination import discord_text_units
from nemoir.application.analysis_service import AnalysisService
from nemoir.application.authorization import AuthorizationPolicy
from nemoir.config import Settings
from nemoir.domain.models import AnalysisResult, SourceFragment, Tendril
from nemoir.domain.states import Actionability, BundleState, TendrilState, TendrilType
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
        self.allowed_mentions: list[object] = []
        self.done = False
        self.deferred = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.deferred = True
        self.done = True

    async def send_message(self, content=None, *, ephemeral=False, **kwargs) -> None:
        self.sent.append(content)
        self.allowed_mentions.append(kwargs.get("allowed_mentions"))
        self.done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.allowed_mentions: list[object] = []

    async def send(self, content=None, *, ephemeral=False, **kwargs) -> None:
        self.sent.append(content)
        self.allowed_mentions.append(kwargs.get("allowed_mentions"))


class FakeChannel:
    def __init__(self, channel_id, *, name="channel", category_id=None, guild=None) -> None:
        self.id = channel_id
        self.name = name
        self.category_id = category_id
        self.guild = guild
        self.sent: list[str] = []
        self.files: list[object] = []
        self.allowed_mentions: list[object] = []

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    async def send(self, content=None, **kwargs) -> SimpleNamespace:
        self.sent.append(content)
        self.allowed_mentions.append(kwargs.get("allowed_mentions"))
        if kwargs.get("file") is not None:
            self.files.append(kwargs["file"])
        return SimpleNamespace(id=f"{self.id}-{len(self.sent)}")


class FakeCategory:
    def __init__(self, category_id, text_channels=None) -> None:
        self.id = category_id
        self.text_channels = text_channels or []
        self.created: list[str] = []

    async def create_text_channel(self, name, *, reason=None) -> FakeChannel:
        self.created.append(name)
        channel = FakeChannel(500 + len(self.created), name=name, category_id=self.id)
        self.text_channels.append(channel)
        return channel


class FakeGuild:
    def __init__(self, guild_id, category=None) -> None:
        self.id = guild_id
        self.category = category

    def get_channel(self, channel_id):
        if self.category is not None and channel_id == self.category.id:
            return self.category
        return None


class FakeInteraction:
    def __init__(self, interaction_id, user, guild, *, channel_id, guild_id) -> None:
        self.id = interaction_id
        self.user = user
        self.guild = guild
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def _make_bot(repository, synthetic_analysis, *, admins=None, allow_channel_write=False, provider=None):
    settings = Settings(
        guild_id="1",
        intake_channel_id="2",
        anemone_category_id="3",
        nursery_channel_id="4",
        admin_user_ids=admins or set(),
        allow_channel_write=allow_channel_write,
    )
    return create_discord_bot(
        settings,
        repository,
        provider or FakeAnalysisProvider(synthetic_analysis),
    )


def _cache_category(bot, category) -> None:
    def get_channel(channel_id):
        cid = int(channel_id)
        if category is not None and cid == category.id:
            return category
        if category is not None:
            for channel in category.text_channels:
                if channel.id == cid:
                    return channel
        return None

    bot.get_channel = get_channel  # type: ignore[method-assign]


async def _review_ready(repository, captured_bundle, synthetic_analysis, authorization):
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="publish-adapter-analyse",
    )
    return repository.get_bundle(captured_bundle.id)


def _long_analysis(synthetic_analysis) -> AnalysisResult:
    payload = synthetic_analysis.model_dump(mode="python")
    payload["tendrils"][0]["description"] = (
        "A deliberately long routed description. " + ("d" * 5000)
    )
    return AnalysisResult.model_validate(payload)


@pytest.mark.asyncio
async def test_preview_performs_zero_writes_and_zero_discord_calls(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    bot = _make_bot(repository, synthetic_analysis)
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    _cache_category(bot, category)

    publish = bot.tree.get_command("publish-bundle").callback
    interaction = FakeInteraction(201, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await publish(interaction, bundle_id=captured_bundle.id)

    assert "preview" in interaction.response.sent[0].lower()
    assert category.created == []
    assert category.text_channels == []
    assert not repository.list_external_operations()


@pytest.mark.asyncio
async def test_successful_publish_creates_one_channel_per_unclaimed_tendril(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    bot = _make_bot(repository, synthetic_analysis, allow_channel_write=True)
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    _cache_category(bot, category)

    publish = bot.tree.get_command("publish-bundle").callback
    actor = FakeUser("person-2", manage_channels=True)
    interaction = FakeInteraction(202, actor, guild, channel_id=2, guild_id=1)
    await publish(interaction, bundle_id=captured_bundle.id, confirm=True)

    report = interaction.followup.sent[0]
    assert "Published: 5" in report
    assert len(category.created) == 5
    assert {channel.name for channel in category.text_channels} == {
        "cosmology-and-life",
        "consciousness-and-reality",
        "free-will-and-consequence",
        "human-ai-collaboration",
        "nemoir",
    }
    for channel in category.text_channels:
        assert len(channel.sent) == 1  # one tracked message each


@pytest.mark.asyncio
async def test_claimed_evidence_is_never_published_in_channels(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    review = repository.get_review(captured_bundle.id)
    claimed_quotes = {fragment.exact_quote for fragment in review.claim.matching_fragments}

    bot = _make_bot(repository, synthetic_analysis, allow_channel_write=True)
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    _cache_category(bot, category)
    publish = bot.tree.get_command("publish-bundle").callback
    interaction = FakeInteraction(
        203, FakeUser("person-2", manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await publish(interaction, bundle_id=captured_bundle.id, confirm=True)

    all_content = "\n".join(channel.sent[0] for channel in category.text_channels)
    for quote in claimed_quotes:
        assert quote not in all_content
    # The unclaimed tendril quote is published with its source link.
    assert "Free will matters only if choices have consequences." in all_content


@pytest.mark.asyncio
async def test_confirm_requires_manage_channels(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    bot = _make_bot(repository, synthetic_analysis, allow_channel_write=True)
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    _cache_category(bot, category)
    publish = bot.tree.get_command("publish-bundle").callback
    interaction = FakeInteraction(
        204, FakeUser("person-2", manage_channels=False), guild, channel_id=2, guild_id=1
    )
    await publish(interaction, bundle_id=captured_bundle.id, confirm=True)
    assert "refused" in interaction.response.sent[0].lower()
    assert category.created == []


@pytest.mark.asyncio
async def test_confirm_requires_channel_write_gate(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    bot = _make_bot(repository, synthetic_analysis, allow_channel_write=False)
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    _cache_category(bot, category)
    publish = bot.tree.get_command("publish-bundle").callback
    interaction = FakeInteraction(
        205, FakeUser("person-2", manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await publish(interaction, bundle_id=captured_bundle.id, confirm=True)
    assert "refused" in interaction.response.sent[0].lower()
    assert category.created == []


@pytest.mark.asyncio
async def test_confirm_rejects_wrong_guild_or_category(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    bot = _make_bot(repository, synthetic_analysis, allow_channel_write=True)
    publish = bot.tree.get_command("publish-bundle").callback

    # Wrong guild.
    wrong_guild = FakeInteraction(
        206, FakeUser("person-2", manage_channels=True), FakeGuild(99), channel_id=2, guild_id=99
    )
    await publish(wrong_guild, bundle_id=captured_bundle.id, confirm=True)
    assert "refused" in wrong_guild.response.sent[0].lower()

    # Right guild, but the configured anemone category cannot be resolved.
    guild_without_category = FakeGuild(1, category=None)
    missing_category = FakeInteraction(
        207, FakeUser("person-2", manage_channels=True), guild_without_category, channel_id=2, guild_id=1
    )
    await publish(missing_category, bundle_id=captured_bundle.id, confirm=True)
    assert "refused" in missing_category.response.sent[0].lower()


@pytest.mark.asyncio
async def test_confirm_rejects_non_participant(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    bot = _make_bot(repository, synthetic_analysis, allow_channel_write=True)
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    _cache_category(bot, category)
    publish = bot.tree.get_command("publish-bundle").callback
    interaction = FakeInteraction(
        208, FakeUser("intruder", manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await publish(interaction, bundle_id=captured_bundle.id, confirm=True)
    assert "refused" in interaction.followup.sent[0].lower()
    assert category.created == []


@pytest.mark.asyncio
async def test_duplicate_interaction_delivery_creates_no_duplicate_channels(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    bot = _make_bot(repository, synthetic_analysis, allow_channel_write=True)
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    _cache_category(bot, category)
    publish = bot.tree.get_command("publish-bundle").callback

    first = FakeInteraction(
        209, FakeUser("person-2", manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await publish(first, bundle_id=captured_bundle.id, confirm=True)
    assert len(category.created) == 5

    duplicate = FakeInteraction(
        209, FakeUser("person-2", manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await publish(duplicate, bundle_id=captured_bundle.id, confirm=True)
    assert "Already published: 5" in duplicate.followup.sent[0]
    assert len(category.created) == 5
    assert all(len(channel.sent) == 1 for channel in category.text_channels)


@pytest.mark.asyncio
async def test_long_evidence_uses_single_message_attachment_path(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    long_analysis = _long_analysis(synthetic_analysis)
    bot = _make_bot(
        repository, synthetic_analysis, allow_channel_write=True, provider=FakeAnalysisProvider(long_analysis)
    )
    await AnalysisService(
        repository, FakeAnalysisProvider(long_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="publish-long-analyse",
    )
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    _cache_category(bot, category)
    publish = bot.tree.get_command("publish-bundle").callback
    interaction = FakeInteraction(
        210, FakeUser("person-2", manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await publish(interaction, bundle_id=captured_bundle.id, confirm=True)

    long_channel = next(
        channel for channel in category.text_channels if channel.name == "cosmology-and-life"
    )
    assert len(long_channel.sent) == 1
    assert len(long_channel.files) == 1
    attachment_text = long_channel.files[0].fp.read().decode("utf-8")
    assert "deliberately long routed description" in attachment_text


# -- Discord UTF-16 length boundary --------------------------------------


def _mentions_suppressed(allowed_mentions) -> bool:
    if allowed_mentions is None:
        return False
    return (
        allowed_mentions.everyone is False
        and allowed_mentions.users is False
        and allowed_mentions.roles is False
        and allowed_mentions.replied_user is False
    )


def _force_long_title(repository, tendril_id: str, title: str) -> None:
    repository._connection.execute(
        "UPDATE tendrils SET title = ? WHERE id = ?", (title, tendril_id)
    )
    repository._connection.commit()


@pytest.mark.asyncio
async def test_astral_emoji_uses_attachment_path_and_stays_within_limit(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    # ~1500 code points but ~3000 UTF-16 units, so len() says it fits while
    # Discord would reject it.
    tendril = Tendril(
        id="t-astral",
        bundle_id=captured_bundle.id,
        title="Astral title",
        description="🦋" * 1500,
        type=TendrilType.INTERESTING,
        actionability=Actionability.NOT_ACTIONABLE,
        evidence=[
            SourceFragment(
                source_message_id=captured_bundle.source_messages[0].external_message_id,
                exact_quote="exact source quote",
            )
        ],
        why_open="The branch remains open.",
        confidence=0.9,
        status=TendrilState.OPEN,
    )
    full = format_tendril(tendril, repository)
    assert len(full) < 2000
    assert discord_text_units(full) > 2000

    channel = FakeChannel(999)
    await publish_tendril_message(channel, tendril, repository)

    assert len(channel.sent) == 1
    assert len(channel.files) == 1  # attachment path, not an over-long inline message
    assert discord_text_units(channel.sent[0]) <= 2000
    assert _mentions_suppressed(channel.allowed_mentions[0])


@pytest.mark.asyncio
async def test_preview_pages_suppress_mentions_and_preserve_literal_syntax(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    _force_long_title(repository, tendril.id, "@everyone <@123> @here " + ("x" * 4000))

    bot = _make_bot(repository, synthetic_analysis)
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    _cache_category(bot, category)
    publish = bot.tree.get_command("publish-bundle").callback
    interaction = FakeInteraction(211, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await publish(interaction, bundle_id=captured_bundle.id)

    pages = interaction.response.sent + interaction.followup.sent
    allowed = interaction.response.allowed_mentions + interaction.followup.allowed_mentions
    assert len(pages) >= 2
    assert len(allowed) == len(pages)
    # Literal mention syntax stays visible verbatim...
    assert "@everyone" in "".join(pages)
    assert "<@123>" in "".join(pages)
    # ...but every page suppresses every mention class.
    assert all(_mentions_suppressed(item) for item in allowed)


@pytest.mark.asyncio
async def test_report_pages_suppress_mentions(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    _force_long_title(repository, tendril.id, "@everyone <@123> " + ("y" * 4000))

    bot = _make_bot(repository, synthetic_analysis, allow_channel_write=True)
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    _cache_category(bot, category)
    publish = bot.tree.get_command("publish-bundle").callback
    interaction = FakeInteraction(
        212, FakeUser("person-2", manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await publish(interaction, bundle_id=captured_bundle.id, confirm=True)

    pages = interaction.followup.sent
    allowed = interaction.followup.allowed_mentions
    assert len(pages) >= 2
    assert len(allowed) == len(pages)
    assert "@everyone" in "".join(pages)
    assert all(_mentions_suppressed(item) for item in allowed)


@pytest.mark.asyncio
async def test_routed_tendril_channels_suppress_mentions(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    bot = _make_bot(repository, synthetic_analysis, allow_channel_write=True)
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category)
    _cache_category(bot, category)
    publish = bot.tree.get_command("publish-bundle").callback
    interaction = FakeInteraction(
        213, FakeUser("person-2", manage_channels=True), guild, channel_id=2, guild_id=1
    )
    await publish(interaction, bundle_id=captured_bundle.id, confirm=True)

    assert len(category.text_channels) == 5
    for channel in category.text_channels:
        assert len(channel.sent) == 1
        assert _mentions_suppressed(channel.allowed_mentions[0])
