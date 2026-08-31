"""Discord-adapter tests for the single-turn /prompt command (no live bot)."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from nemoir.__main__ import _synthetic_pilot_provider
from nemoir.adapters.discord_bot import (
    PROMPT_FAILURE_TEXT,
    create_discord_bot,
    format_prompt_response,
)
from nemoir.adapters.pagination import (
    DISCORD_MESSAGE_LIMIT,
    discord_text_units,
    paginate_text,
)
from nemoir.config import Settings
from nemoir.domain.models import ProviderReceipt
from nemoir.providers.fake import FakeAnalysisProvider


class FakeUser:
    def __init__(self, user_id) -> None:
        self.id = user_id
        self.bot = False
        self.mention = f"<@{user_id}>"
        self.display_name = f"user-{user_id}"
        self.guild_permissions = None


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
        self.mentions: list[object] = []

    async def send(self, content=None, *, ephemeral=False, **kwargs) -> None:
        self.sent.append(content)
        self.mentions.append(kwargs.get("allowed_mentions"))


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id

    def get_channel(self, channel_id: int):
        return None


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
    synthetic_analysis=None,
    *,
    provider=None,
    pilot_mode: bool = False,
    max_input_chars: int = 2000,
) -> None:
    settings = Settings(
        guild_id="1",
        intake_channel_id="2",
        anemone_category_id="3",
        nursery_channel_id="4",
        prompt_max_input_chars=max_input_chars,
    )
    return create_discord_bot(
        settings,
        repository,
        provider or FakeAnalysisProvider(synthetic_analysis),
        pilot_mode=pilot_mode,
    )


def _strip_footer(page: str) -> str:
    return re.sub(r"\n— page \d+/\d+ —$", "", page)


def _reconstruct(pages: list[str]) -> str:
    return "".join(_strip_footer(page) for page in pages)


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached in time")


def _assert_no_mentions(allowed_mentions) -> None:
    assert allowed_mentions is not None
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False
    assert allowed_mentions.replied_user is False


@pytest.mark.asyncio
async def test_prompt_short_public_response(repository, synthetic_analysis) -> None:
    provider = FakeAnalysisProvider(
        synthetic_analysis, prompt_answer="Aeroplanes fly by generating lift with their wings."
    )
    bot = _make_bot(repository, synthetic_analysis, provider=provider)
    interaction = FakeInteraction(1, FakeUser("10"), FakeGuild(1), channel_id=2, guild_id=1)
    await bot.tree.get_command("prompt").callback(
        interaction, question="How do aeroplanes work?"
    )

    assert interaction.response.deferred is True
    assert interaction.response.sent == []  # not ephemeral; public follow-up
    assert len(interaction.followup.sent) == 1
    page = interaction.followup.sent[0]
    assert page.startswith("**Nemoir response**")
    assert "Aeroplanes fly by generating lift" in page


@pytest.mark.asyncio
async def test_prompt_multi_page_response_reconstructs_exactly(
    repository, synthetic_analysis
) -> None:
    answer = "\n\n".join(f"Section {index}: " + ("content " * 120) for index in range(40))
    provider = FakeAnalysisProvider(synthetic_analysis, prompt_answer=answer)
    bot = _make_bot(repository, synthetic_analysis, provider=provider)
    interaction = FakeInteraction(2, FakeUser("10"), FakeGuild(1), channel_id=2, guild_id=1)
    await bot.tree.get_command("prompt").callback(interaction, question="long answer?")

    pages = interaction.followup.sent
    assert len(pages) > 1
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    attempt = repository.get_prompt_attempt_by_key("discord-prompt:2")
    expected = format_prompt_response(answer, attempt.receipt)
    assert _reconstruct(pages) == expected
    assert pages[0].startswith("**Nemoir response**")


def test_prompt_exact_2000_character_boundary_stays_one_page() -> None:
    receipt = ProviderReceipt(provider="deepseek", model="m", latency_ms=0, outcome="success")
    label = "**Nemoir · DeepSeek response**\n"
    answer = "x" * (DISCORD_MESSAGE_LIMIT - len(label))
    full = format_prompt_response(answer, receipt)
    assert len(full) == DISCORD_MESSAGE_LIMIT
    pages = paginate_text(full)
    assert pages == [full]
    assert len(pages[0]) == DISCORD_MESSAGE_LIMIT


def test_prompt_one_character_over_limit_paginates_without_loss() -> None:
    receipt = ProviderReceipt(provider="deepseek", model="m", latency_ms=0, outcome="success")
    label = "**Nemoir · DeepSeek response**\n"
    answer = "x" * (DISCORD_MESSAGE_LIMIT - len(label) + 1)
    full = format_prompt_response(answer, receipt)
    assert len(full) == DISCORD_MESSAGE_LIMIT + 1
    pages = paginate_text(full)
    assert len(pages) == 2
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    assert _reconstruct(pages) == full


def test_prompt_long_unbroken_line_is_split_not_dropped() -> None:
    receipt = ProviderReceipt(provider="deepseek", model="m", latency_ms=0, outcome="success")
    answer = "x" * (DISCORD_MESSAGE_LIMIT * 2 + 300)
    full = format_prompt_response(answer, receipt)
    pages = paginate_text(full)
    assert len(pages) > 2
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    assert _reconstruct(pages) == full


def test_prompt_fenced_code_spanning_pages_is_preserved() -> None:
    receipt = ProviderReceipt(provider="deepseek", model="m", latency_ms=0, outcome="success")
    long_line = "    data = " + ("y" * (DISCORD_MESSAGE_LIMIT + 400))
    answer = "Here is a code block:\n```python\n" + long_line + "\n```\nEnd."
    full = format_prompt_response(answer, receipt)
    pages = paginate_text(full)
    assert len(pages) > 1
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    reconstructed = _reconstruct(pages)
    assert reconstructed == full
    assert "```python" in reconstructed
    assert reconstructed.endswith("End.")


def test_prompt_reconstruction_has_no_lost_or_duplicated_markers() -> None:
    receipt = ProviderReceipt(provider="deepseek", model="m", latency_ms=0, outcome="success")
    answer = "\n".join(f"marker-{index:03d} " + ("q" * 80) for index in range(150))
    full = format_prompt_response(answer, receipt)
    pages = paginate_text(full)
    assert len(pages) > 1
    reconstructed = _reconstruct(pages)
    assert reconstructed == full
    for index in range(150):
        assert reconstructed.count(f"marker-{index:03d}") == 1


@pytest.mark.asyncio
async def test_prompt_defers_before_awaiting_provider(repository, synthetic_analysis) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingPrompt(FakeAnalysisProvider):
        async def prompt(self, request):
            started.set()
            await release.wait()
            return await super().prompt(request)

    provider = BlockingPrompt(synthetic_analysis, prompt_answer="answer after block")
    bot = _make_bot(repository, synthetic_analysis, provider=provider)
    interaction = FakeInteraction(7, FakeUser("10"), FakeGuild(1), channel_id=2, guild_id=1)
    task = asyncio.create_task(
        bot.tree.get_command("prompt").callback(interaction, question="block?")
    )
    await started.wait()
    assert interaction.response.deferred is True
    release.set()
    await task
    assert interaction.followup.sent


@pytest.mark.asyncio
async def test_prompt_rejects_empty_and_oversized_before_provider(
    repository, synthetic_analysis
) -> None:
    provider = FakeAnalysisProvider(synthetic_analysis)
    bot = _make_bot(repository, synthetic_analysis, provider=provider, max_input_chars=20)
    guild = FakeGuild(1)

    empty = FakeInteraction(8, FakeUser("10"), guild, channel_id=2, guild_id=1)
    await bot.tree.get_command("prompt").callback(empty, question="   ")
    assert "empty" in empty.response.sent[0].lower()
    assert empty.response.deferred is False
    assert provider.prompt_call_count == 0

    oversized = FakeInteraction(9, FakeUser("10"), guild, channel_id=2, guild_id=1)
    await bot.tree.get_command("prompt").callback(oversized, question="x" * 21)
    assert "too long" in oversized.response.sent[0].lower()
    assert oversized.response.deferred is False
    assert provider.prompt_call_count == 0


@pytest.mark.asyncio
async def test_prompt_is_scoped_to_guild_and_intake_channel(
    repository, synthetic_analysis
) -> None:
    bot = _make_bot(repository, synthetic_analysis)
    prompt = bot.tree.get_command("prompt").callback

    wrong_guild = FakeInteraction(10, FakeUser("10"), FakeGuild(1), channel_id=2, guild_id=99)
    await prompt(wrong_guild, question="hi?")
    assert "refused" in wrong_guild.response.sent[0].lower()

    wrong_channel = FakeInteraction(11, FakeUser("10"), FakeGuild(1), channel_id=99, guild_id=1)
    await prompt(wrong_channel, question="hi?")
    assert "refused" in wrong_channel.response.sent[0].lower()


@pytest.mark.asyncio
async def test_prompt_one_running_per_user_via_command(repository, synthetic_analysis) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingPrompt(FakeAnalysisProvider):
        async def prompt(self, request):
            started.set()
            await release.wait()
            return await super().prompt(request)

    provider = BlockingPrompt(synthetic_analysis, prompt_answer="done")
    bot = _make_bot(repository, synthetic_analysis, provider=provider)
    guild = FakeGuild(1)
    prompt = bot.tree.get_command("prompt").callback

    first = FakeInteraction(12, FakeUser("10"), guild, channel_id=2, guild_id=1)
    task = asyncio.create_task(prompt(first, question="first?"))
    await started.wait()

    second = FakeInteraction(13, FakeUser("10"), guild, channel_id=2, guild_id=1)
    await prompt(second, question="second?")
    assert "in progress" in second.followup.sent[0].lower()

    release.set()
    await task
    assert provider.prompt_call_count == 1


@pytest.mark.asyncio
async def test_prompt_duplicate_interaction_does_not_duplicate(
    repository, synthetic_analysis
) -> None:
    provider = FakeAnalysisProvider(synthetic_analysis, prompt_answer="one answer")
    bot = _make_bot(repository, synthetic_analysis, provider=provider)
    guild = FakeGuild(1)
    prompt = bot.tree.get_command("prompt").callback

    first = FakeInteraction(14, FakeUser("10"), guild, channel_id=2, guild_id=1)
    await prompt(first, question="why?")
    pages_after_first = len(first.followup.sent)
    assert pages_after_first == 1

    repeated = FakeInteraction(14, FakeUser("10"), guild, channel_id=2, guild_id=1)
    await prompt(repeated, question="why?")
    assert repeated.followup.sent == []
    assert provider.prompt_call_count == 1
    assert len(first.followup.sent) == pages_after_first


@pytest.mark.asyncio
async def test_prompt_provider_failure_is_concise(repository, synthetic_analysis) -> None:
    provider = FakeAnalysisProvider(synthetic_analysis, prompt_variant="provider_failure")
    bot = _make_bot(repository, synthetic_analysis, provider=provider)
    interaction = FakeInteraction(15, FakeUser("10"), FakeGuild(1), channel_id=2, guild_id=1)
    await bot.tree.get_command("prompt").callback(interaction, question="fail?")

    assert interaction.response.deferred is True
    assert interaction.followup.sent == [PROMPT_FAILURE_TEXT]
    assert "RuntimeError" not in interaction.followup.sent[0]
    assert "Synthetic prompt failure" not in interaction.followup.sent[0]


@pytest.mark.asyncio
async def test_prompt_never_echoes_credentials_or_exception_details(
    repository, synthetic_analysis
) -> None:
    class LeakyProvider(FakeAnalysisProvider):
        async def prompt(self, request):
            raise RuntimeError(
                "Authorization: Bearer sk-secret-value DEEPSEEK_API_KEY=sk-abcdefghijk"
            )

    bot = _make_bot(repository, synthetic_analysis, provider=LeakyProvider(synthetic_analysis))
    interaction = FakeInteraction(16, FakeUser("10"), FakeGuild(1), channel_id=2, guild_id=1)
    await bot.tree.get_command("prompt").callback(interaction, question="leak?")

    text = interaction.followup.sent[0]
    assert text == PROMPT_FAILURE_TEXT
    assert "sk-secret-value" not in text
    assert "sk-abcdefghijk" not in text
    assert "Bearer" not in text
    assert "RuntimeError" not in text


@pytest.mark.asyncio
async def test_pilot_prompt_uses_synthetic_provider_never_deepseek(
    repository, synthetic_messages
) -> None:
    # The autouse `forbid_network` fixture denies socket connections, so this
    # test also fails if the pilot provider attempts any live call.
    bot = _make_bot(repository, provider=_synthetic_pilot_provider(), pilot_mode=True)
    interaction = FakeInteraction(17, FakeUser("10"), FakeGuild(1), channel_id=2, guild_id=1)
    await bot.tree.get_command("prompt").callback(
        interaction, question="How do aeroplanes work?"
    )

    assert interaction.response.deferred is True
    assert len(interaction.followup.sent) >= 1
    assert "synthetic" in interaction.followup.sent[0].lower()
    attempt = repository.get_prompt_attempt_by_key("discord-prompt:17")
    assert attempt.receipt is not None
    assert attempt.receipt.provider == "synthetic"
    assert attempt.receipt.model == "deterministic-prompt"


@pytest.mark.asyncio
async def test_prompt_suppresses_mentions_on_single_page(
    repository, synthetic_analysis
) -> None:
    answer = "Hey @everyone and @here: ping <@123> or the <@&456> role."
    provider = FakeAnalysisProvider(synthetic_analysis, prompt_answer=answer)
    bot = _make_bot(repository, synthetic_analysis, provider=provider)
    interaction = FakeInteraction(20, FakeUser("10"), FakeGuild(1), channel_id=2, guild_id=1)
    await bot.tree.get_command("prompt").callback(interaction, question="mentions?")

    pages = interaction.followup.sent
    assert len(pages) == 1
    # Mentions stay visible as literal text...
    assert "@everyone" in pages[0]
    assert "@here" in pages[0]
    assert "<@123>" in pages[0]
    assert "<@&456>" in pages[0]
    # ...but the exact allowed_mentions argument suppresses every notification.
    assert len(interaction.followup.mentions) == 1
    _assert_no_mentions(interaction.followup.mentions[0])


@pytest.mark.asyncio
async def test_prompt_suppresses_mentions_on_every_page(
    repository, synthetic_analysis
) -> None:
    answer = "\n".join(
        f"line {index} @everyone @here <@{1000 + index}> <@&{2000 + index}> "
        + ("m" * 1500)
        for index in range(3)
    )
    provider = FakeAnalysisProvider(synthetic_analysis, prompt_answer=answer)
    bot = _make_bot(repository, synthetic_analysis, provider=provider)
    interaction = FakeInteraction(21, FakeUser("10"), FakeGuild(1), channel_id=2, guild_id=1)
    await bot.tree.get_command("prompt").callback(interaction, question="mentions?")

    pages = interaction.followup.sent
    assert len(pages) > 1
    assert len(interaction.followup.mentions) == len(pages)
    for allowed_mentions in interaction.followup.mentions:
        _assert_no_mentions(allowed_mentions)
    reconstructed = _reconstruct(pages)
    assert "@everyone" in reconstructed
    assert "@here" in reconstructed
    assert "<@1000>" in reconstructed
    assert "<@&2000>" in reconstructed


@pytest.mark.asyncio
async def test_prompt_emoji_heavy_pages_stay_within_utf16_unit_limit(
    repository, synthetic_analysis
) -> None:
    # 1500 astral emoji: len() is 1500 (fits under 2000 code points) but the
    # UTF-16 unit length is 3000, so pagination must still split into pages.
    answer = "😀" * 1500
    provider = FakeAnalysisProvider(synthetic_analysis, prompt_answer=answer)
    bot = _make_bot(repository, synthetic_analysis, provider=provider)
    interaction = FakeInteraction(22, FakeUser("10"), FakeGuild(1), channel_id=2, guild_id=1)
    await bot.tree.get_command("prompt").callback(interaction, question="emoji?")

    pages = interaction.followup.sent
    assert len(pages) > 1
    assert all(discord_text_units(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    assert _reconstruct(pages) == format_prompt_response(
        answer, repository.get_prompt_attempt_by_key("discord-prompt:22").receipt
    )
