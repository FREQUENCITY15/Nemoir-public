"""Discord adapter tests for the autonomous workflow with fake Discord objects."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from nemoir.adapters.discord_bot import create_discord_bot
from nemoir.config import Settings
from nemoir.domain.states import AutonomousJobPhase, BundleState
from nemoir.providers.synthetic import SyntheticAutonomousSortProvider


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
        self.create_kwargs: list[dict] = []

    async def create_text_channel(self, name, **kwargs) -> FakeChannel:
        self.create_kwargs.append(kwargs)
        self.created.append(name)
        channel = FakeChannel(500 + len(self.created), name=name, category_id=self.id)
        self.text_channels.append(channel)
        return channel


class FakeGuild:
    def __init__(self, guild_id, category=None, nursery=None, intake=None) -> None:
        self.id = guild_id
        self.category = category
        self.nursery = nursery
        self.intake = intake

    def get_channel(self, channel_id):
        for candidate in (self.category, self.nursery, self.intake):
            if candidate is not None and channel_id == candidate.id:
                return candidate
        if self.category is not None:
            for channel in self.category.text_channels:
                if channel.id == channel_id:
                    return channel
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


class FakeMessage:
    def __init__(self, message_id, author, content, channel_id, guild_id, jump_url) -> None:
        self.id = message_id
        self.author = author
        self.content = content
        self.channel = SimpleNamespace(id=channel_id)
        self.guild = SimpleNamespace(id=guild_id)
        self.jump_url = jump_url
        self.created_at = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
        self._state = None  # discord.py Context construction only stores it


def _make_bot(
    repository,
    *,
    autonomous=None,
    allow_channel_write=False,
    sort_variant="valid",
    additional_intakes=None,
):
    settings = Settings(
        guild_id="1",
        intake_channel_id="2",
        additional_intake_channel_ids=additional_intakes or set(),
        anemone_category_id="3",
        nursery_channel_id="4",
        admin_user_ids={"admin-1"},
        allow_channel_write=allow_channel_write,
    )
    bot = create_discord_bot(
        settings,
        repository,
        SyntheticAutonomousSortProvider(),  # analysis provider stand-in
        autonomous_mode=autonomous,
        autonomous_sort_provider=(
            SyntheticAutonomousSortProvider(variant=sort_variant) if autonomous else None
        ),
    )
    # Fake messages are not real discord.Message objects, so the prefix
    # command dispatch (which needs connection state) is stubbed out.
    async def _noop_process(_message):
        return None

    bot.process_commands = _noop_process  # type: ignore[method-assign]
    return bot


def _cache(bot, guild) -> None:
    bot.get_channel = guild.get_channel  # type: ignore[method-assign]


async def _capture_messages(bot, repository, user_id, messages, *, guild_id="1", channel_id="2"):
    on_message = bot.on_message
    for index, message in enumerate(messages):
        fake = FakeMessage(
            f"msg-{user_id}-{index}",
            FakeUser(user_id),
            message.content,
            channel_id,
            guild_id,
            f"https://discord.invalid/{user_id}-{index}",
        )
        await on_message(fake)
    return repository.find_open_capture(user_id, channel_id)


@pytest.mark.asyncio
async def test_ordinary_mode_refuses_recipient_free_tend(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(repository)
    guild = FakeGuild(1, category=FakeCategory(3))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    interaction = FakeInteraction(1, FakeUser("person-1"), guild, channel_id=2, guild_id=1)
    await tend(interaction, recipient=None)
    assert "refused" in interaction.response.sent[0].lower()
    assert repository.find_open_capture("person-1", "2") is None


@pytest.mark.asyncio
async def test_autonomous_tend_without_recipient_marks_bundle(
    repository,
) -> None:
    bot = _make_bot(repository, autonomous="test")
    guild = FakeGuild(1, category=FakeCategory(3))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    interaction = FakeInteraction(2, FakeUser("person-1"), guild, channel_id=2, guild_id=1)
    await tend(interaction, recipient=None)
    bundle = repository.find_open_capture("person-1", "2")
    assert bundle is not None
    assert bundle.autonomous_mode is True
    assert "autonomous" in interaction.response.sent[0].lower()


@pytest.mark.asyncio
async def test_additional_intake_supports_tend_capture_and_seal(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(
        repository,
        autonomous="test",
        additional_intakes={"22"},
    )
    guild = FakeGuild(1, category=FakeCategory(3))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    seal = bot.tree.get_command("seal").callback

    await tend(
        FakeInteraction(3, FakeUser("person-1"), guild, channel_id=22, guild_id=1),
        recipient=None,
    )
    captured = await _capture_messages(
        bot,
        repository,
        "person-1",
        synthetic_messages,
        channel_id="22",
    )
    assert captured is not None
    assert captured.intake_channel_id == "22"
    assert len(repository.get_bundle(captured.id).source_messages) == len(
        synthetic_messages
    )

    interaction = FakeInteraction(
        4, FakeUser("person-1"), guild, channel_id=22, guild_id=1
    )
    await seal(interaction)
    assert "sealed and queued" in interaction.response.sent[0]
    assert repository.get_autonomous_job(captured.id).phase == AutonomousJobPhase.QUEUED


@pytest.mark.asyncio
async def test_two_users_capture_independently_through_adapter(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(repository, autonomous="test")
    guild = FakeGuild(1, category=FakeCategory(3))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    for index, user in enumerate(("person-1", "person-2")):
        interaction = FakeInteraction(
            10 + index, FakeUser(user), guild, channel_id=2, guild_id=1
        )
        await tend(interaction, recipient=None)

    first = await _capture_messages(bot, repository, "person-1", synthetic_messages[:2])
    second = await _capture_messages(bot, repository, "person-2", synthetic_messages[2:])
    assert first.id != second.id
    assert len(repository.get_bundle(first.id).source_messages) == 2
    assert len(repository.get_bundle(second.id).source_messages) == 1
    assert all(
        item.author_user_id == "person-1"
        for item in repository.get_bundle(first.id).source_messages
    )
    assert all(
        item.author_user_id == "person-2"
        for item in repository.get_bundle(second.id).source_messages
    )


@pytest.mark.asyncio
async def test_duplicate_tend_delivery_replays_same_capture(repository) -> None:
    bot = _make_bot(repository, autonomous="test")
    guild = FakeGuild(1, category=FakeCategory(3))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    first = FakeInteraction(20, FakeUser("person-1"), guild, channel_id=2, guild_id=1)
    await tend(first, recipient=None)
    duplicate = FakeInteraction(20, FakeUser("person-1"), guild, channel_id=2, guild_id=1)
    await tend(duplicate, recipient=None)
    assert first.response.sent[0].split("`")[1] == duplicate.response.sent[0].split("`")[1]
    assert len(repository.list_autonomous_jobs()) == 0  # tend creates no job yet
    bundles = repository._connection.execute("SELECT COUNT(*) AS n FROM bundles").fetchone()["n"]
    assert bundles == 1


@pytest.mark.asyncio
async def test_seal_acknowledges_immediately_before_provider_completion(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(repository, autonomous="test")
    guild = FakeGuild(1, category=FakeCategory(3))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    seal = bot.tree.get_command("seal").callback
    await tend(FakeInteraction(30, FakeUser("person-1"), guild, channel_id=2, guild_id=1), recipient=None)
    await _capture_messages(bot, repository, "person-1", synthetic_messages)
    bundle = repository.find_open_capture("person-1", "2")

    interaction = FakeInteraction(31, FakeUser("person-1"), guild, channel_id=2, guild_id=1)
    await seal(interaction)
    # The acknowledgement arrived immediately; the provider was NOT invoked by
    # the seal handler itself (the background worker owns that).
    assert "sealed and queued" in interaction.response.sent[0]
    assert interaction.response.deferred is False
    job = repository.get_autonomous_job(bundle.id)
    assert job.phase == AutonomousJobPhase.QUEUED

    # Background processing completes sort and publishing without the user.
    await bot.process_autonomous_bundle(bundle.id)
    assert repository.get_autonomous_job(bundle.id).phase == AutonomousJobPhase.RESULT_PERSISTED
    assert repository.get_bundle(bundle.id).status == BundleState.REVIEW_READY
    assert repository.list_tendrils(bundle_id=bundle.id)


@pytest.mark.asyncio
async def test_duplicate_seal_delivery_creates_no_second_job(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(repository, autonomous="test")
    guild = FakeGuild(1, category=FakeCategory(3))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    seal = bot.tree.get_command("seal").callback
    await tend(FakeInteraction(40, FakeUser("person-1"), guild, channel_id=2, guild_id=1), recipient=None)
    await _capture_messages(bot, repository, "person-1", synthetic_messages)
    bundle = repository.find_open_capture("person-1", "2")

    first = FakeInteraction(41, FakeUser("person-1"), guild, channel_id=2, guild_id=1)
    await seal(first)
    duplicate = FakeInteraction(41, FakeUser("person-1"), guild, channel_id=2, guild_id=1)
    await seal(duplicate)
    assert "sealed and queued" in first.response.sent[0]
    assert "already sealed and queued" in duplicate.response.sent[0]
    assert len(repository.list_autonomous_jobs()) == 1
    assert len(repository.list_autonomous_attempts(bundle.id)) == 0


@pytest.mark.asyncio
async def test_full_autonomous_flow_publishes_and_notifies_author_only(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(repository, autonomous="test", allow_channel_write=True)
    category = FakeCategory(3)
    nursery = FakeChannel(4, name="nursery")
    intake = FakeChannel(2, name="intake")
    guild = FakeGuild(1, category=category, nursery=nursery, intake=intake)
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    seal = bot.tree.get_command("seal").callback
    await tend(FakeInteraction(50, FakeUser("person-1"), guild, channel_id=2, guild_id=1), recipient=None)
    await _capture_messages(bot, repository, "person-1", synthetic_messages)
    bundle = repository.find_open_capture("person-1", "2")
    await seal(FakeInteraction(51, FakeUser("person-1"), guild, channel_id=2, guild_id=1))

    await bot.process_autonomous_bundle(bundle.id)  # sort
    await bot.process_autonomous_bundle(bundle.id)  # publish -> COMPLETED

    assert repository.get_autonomous_job(bundle.id).phase == AutonomousJobPhase.COMPLETED
    tendrils = repository.list_tendrils(bundle_id=bundle.id)
    assert len(category.created) == len(tendrils)
    # Every channel inherits the category permissions: no private overwrites.
    for kwargs in category.create_kwargs:
        assert kwargs.get("overwrites") in (None, {})
    assert nursery.sent
    completion = "\n".join(nursery.sent)
    assert "complete" in completion.lower() and bundle.id in completion
    assert "<@person-1>" in completion
    # Only the deliberate author mention may ping; every page restricted.
    for mentions in nursery.allowed_mentions:
        assert mentions.everyone is False
        assert mentions.roles is False
        assert getattr(mentions, "replied_user", False) is False
        assert mentions.users is not False
        assert [target.id for target in mentions.users] == ["person-1"]
    # Model-derived tendril content posted with every mention suppressed.
    for channel in category.text_channels:
        for mentions in channel.allowed_mentions:
            assert mentions.everyone is False and mentions.users is False and mentions.roles is False


@pytest.mark.asyncio
async def test_model_derived_mentions_never_ping_anyone(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(repository, autonomous="test", allow_channel_write=True)
    category = FakeCategory(3)
    nursery = FakeChannel(4, name="nursery")
    guild = FakeGuild(1, category=category, nursery=nursery, intake=FakeChannel(2))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    seal = bot.tree.get_command("seal").callback
    await tend(FakeInteraction(60, FakeUser("person-1"), guild, channel_id=2, guild_id=1), recipient=None)
    await _capture_messages(bot, repository, "person-1", synthetic_messages)
    bundle = repository.find_open_capture("person-1", "2")
    await seal(FakeInteraction(61, FakeUser("person-1"), guild, channel_id=2, guild_id=1))
    await bot.process_autonomous_bundle(bundle.id)

    # A model could emit mention syntax into a title: it must stay literal text.
    tendril = repository.list_tendrils(bundle_id=bundle.id)[0]
    repository._connection.execute(
        "UPDATE tendrils SET title = ? WHERE id = ?",
        ("@everyone <@123> <@&456> hostile title", tendril.id),
    )
    repository._connection.commit()

    await bot.process_autonomous_bundle(bundle.id)  # publish with poisoned titles
    assert repository.get_autonomous_job(bundle.id).phase == AutonomousJobPhase.COMPLETED
    for channel in category.text_channels:
        for mentions in channel.allowed_mentions:
            assert mentions.everyone is False and mentions.users is False and mentions.roles is False
    for mentions in nursery.allowed_mentions:
        assert mentions.everyone is False and mentions.roles is False


@pytest.mark.asyncio
async def test_autonomous_test_never_contacts_deepseek(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(repository, autonomous="test")
    guild = FakeGuild(1, category=FakeCategory(3))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    seal = bot.tree.get_command("seal").callback
    await tend(FakeInteraction(70, FakeUser("person-1"), guild, channel_id=2, guild_id=1), recipient=None)
    await _capture_messages(bot, repository, "person-1", synthetic_messages)
    bundle = repository.find_open_capture("person-1", "2")
    await seal(FakeInteraction(71, FakeUser("person-1"), guild, channel_id=2, guild_id=1))
    await bot.process_autonomous_bundle(bundle.id)
    attempts = repository.list_autonomous_attempts(bundle.id)
    assert attempts and attempts[-1].provider == "synthetic-autonomous"
    assert "deepseek" not in attempts[-1].provider.lower()


@pytest.mark.asyncio
async def test_autonomous_retry_authorization_and_fresh_key(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(repository, autonomous="test", sort_variant="provider_failure")
    guild = FakeGuild(1, category=FakeCategory(3))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    seal = bot.tree.get_command("seal").callback
    await tend(FakeInteraction(80, FakeUser("person-1"), guild, channel_id=2, guild_id=1), recipient=None)
    await _capture_messages(bot, repository, "person-1", synthetic_messages)
    bundle = repository.find_open_capture("person-1", "2")
    await seal(FakeInteraction(81, FakeUser("person-1"), guild, channel_id=2, guild_id=1))
    await bot.process_autonomous_bundle(bundle.id)
    assert repository.get_autonomous_job(bundle.id).phase == AutonomousJobPhase.FAILED

    retry = bot.tree.get_command("autonomous-retry").callback
    refused = FakeInteraction(82, FakeUser("person-2"), guild, channel_id=2, guild_id=1)
    await retry(refused, bundle_id=bundle.id)
    assert "refused" in refused.response.sent[0].lower()

    owner = FakeInteraction(83, FakeUser("person-1"), guild, channel_id=2, guild_id=1)
    await retry(owner, bundle_id=bundle.id)
    job = repository.get_autonomous_job(bundle.id)
    assert job.phase == AutonomousJobPhase.QUEUED
    assert job.pending_attempt_key == "autonomous-retry:83"
    # The sealed source was never mutated or resealed.
    assert len(repository.get_bundle(bundle.id).source_messages) == 3


@pytest.mark.asyncio
async def test_ordinary_bot_autonomous_retry_refused(repository) -> None:
    bot = _make_bot(repository)
    guild = FakeGuild(1, category=FakeCategory(3))
    _cache(bot, guild)
    retry = bot.tree.get_command("autonomous-retry").callback
    interaction = FakeInteraction(90, FakeUser("person-1"), guild, channel_id=2, guild_id=1)
    await retry(interaction, bundle_id="any-bundle")
    assert "refused" in interaction.response.sent[0].lower()


@pytest.mark.asyncio
async def test_channel_write_gate_keeps_autonomous_publishing_closed(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(repository, autonomous="test", allow_channel_write=False)
    category = FakeCategory(3)
    guild = FakeGuild(1, category=category, nursery=FakeChannel(4), intake=FakeChannel(2))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    seal = bot.tree.get_command("seal").callback
    await tend(FakeInteraction(100, FakeUser("person-1"), guild, channel_id=2, guild_id=1), recipient=None)
    await _capture_messages(bot, repository, "person-1", synthetic_messages)
    bundle = repository.find_open_capture("person-1", "2")
    await seal(FakeInteraction(101, FakeUser("person-1"), guild, channel_id=2, guild_id=1))
    await bot.process_autonomous_bundle(bundle.id)
    await bot.process_autonomous_bundle(bundle.id)
    assert category.created == []
    assert repository.get_autonomous_job(bundle.id).phase == AutonomousJobPhase.RESULT_PERSISTED


@pytest.mark.asyncio
async def test_bundle_status_reports_autonomous_job_phase(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(repository, autonomous="test")
    guild = FakeGuild(1, category=FakeCategory(3))
    _cache(bot, guild)
    tend = bot.tree.get_command("tend").callback
    seal = bot.tree.get_command("seal").callback
    await tend(FakeInteraction(110, FakeUser("person-1"), guild, channel_id=2, guild_id=1), recipient=None)
    await _capture_messages(bot, repository, "person-1", synthetic_messages)
    bundle = repository.find_open_capture("person-1", "2")
    await seal(FakeInteraction(111, FakeUser("person-1"), guild, channel_id=2, guild_id=1))
    status = bot.tree.get_command("bundle-status").callback
    interaction = FakeInteraction(112, FakeUser("person-1"), guild, channel_id=2, guild_id=1)
    await status(interaction, bundle_id=bundle.id)
    assert "AUTONOMOUS_SORTING" in interaction.response.sent[0]
    assert "autonomous job: `QUEUED`" in interaction.response.sent[0]
