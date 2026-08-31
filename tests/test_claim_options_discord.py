"""Discord-adapter tests for the two-stage claim-option workflow (no live bot)."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from nemoir.__main__ import _synthetic_pilot_provider
from nemoir.adapters.discord_bot import create_discord_bot, format_claim_options
from nemoir.adapters.pagination import DISCORD_MESSAGE_LIMIT, paginate_text
from nemoir.application.capture_service import CaptureService
from nemoir.config import Settings
from nemoir.domain.models import (
    ClaimCandidate,
    ClaimCandidateSet,
    ClaimDiscoveryResponse,
    ProviderReceipt,
    SourceFragment,
)
from nemoir.domain.states import BundleState
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

    async def send(self, content=None, *, ephemeral=False, **kwargs) -> None:
        self.sent.append(content)


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: list[str] = []
        self.files: list[object] = []

    async def send(self, content=None, **kwargs) -> SimpleNamespace:
        self.sent.append(content)
        if kwargs.get("file") is not None:
            self.files.append(kwargs["file"])
        return SimpleNamespace(id=f"{self.id}-{len(self.sent)}")


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


def _make_bot(repository, provider, *, pilot_mode: bool = False, admins: set[str] | None = None):
    settings = Settings(
        guild_id="1",
        intake_channel_id="2",
        anemone_category_id="3",
        nursery_channel_id="4",
        admin_user_ids=admins or set(),
    )
    return create_discord_bot(settings, repository, provider, pilot_mode=pilot_mode)


def _cache_channels(bot, channels: dict[int, FakeChannel]) -> None:
    bot.get_channel = lambda channel_id: channels.get(int(channel_id))  # type: ignore[method-assign]


def _capture(repository, synthetic_messages, *, submitter="10", recipient="20", prefix=""):
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="1",
        intake_channel_id="2",
        submitter_user_id=submitter,
        recipient_user_id=recipient,
    )
    for message in synthetic_messages:
        capture.capture_message(
            bundle.id,
            actor_user_id=submitter,
            external_message_id=f"{prefix}{message.external_message_id}",
            author_display_name=message.author_display_name,
            channel_id="2",
            content=message.content,
            source_url=message.source_url,
            timestamp=message.timestamp,
        )
    return bundle


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached in time")


@pytest.mark.asyncio
async def test_seal_renders_option_menu_and_mentions_recipient(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    bot = _make_bot(repository, FakeAnalysisProvider(synthetic_analysis))
    bundle = _capture(repository, synthetic_messages)
    guild = FakeGuild(1)
    seal = bot.tree.get_command("seal").callback
    interaction = FakeInteraction(11, FakeUser(10), guild, channel_id=2, guild_id=1)
    await seal(interaction)

    pages = interaction.response.sent + interaction.followup.sent
    joined = "\n".join(pages)
    assert any("<@20>" in text for text in pages)
    assert "interpretations" in joined
    assert 'options:"<n>"' in joined
    assert "Free will matters only if choices have consequences." in joined
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_READY


@pytest.mark.asyncio
async def test_claim_option_selection_end_to_end(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    bot = _make_bot(repository, FakeAnalysisProvider(synthetic_analysis))
    bundle = _capture(repository, synthetic_messages)
    nursery = FakeChannel(4)
    _cache_channels(bot, {4: nursery})
    guild = FakeGuild(1)

    await bot.tree.get_command("seal").callback(
        FakeInteraction(11, FakeUser(10), guild, channel_id=2, guild_id=1)
    )
    recipient = FakeInteraction(12, FakeUser("20"), guild, channel_id=2, guild_id=1)
    await bot.tree.get_command("claim").callback(recipient, options="1")
    assert "Claim recorded" in recipient.response.sent[0]

    await _wait_for(lambda: any("review ready" in text for text in nursery.sent))
    claim = repository.get_claim_for_bundle(bundle.id)
    assert claim.selected_candidate_ids
    assert claim.selected_candidates
    assert claim.raw_topic == claim.selected_candidates[0].title


@pytest.mark.asyncio
async def test_claim_options_command_redisplays_and_authorizes(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    bot = _make_bot(repository, FakeAnalysisProvider(synthetic_analysis))
    bundle = _capture(repository, synthetic_messages)
    guild = FakeGuild(1)
    await bot.tree.get_command("seal").callback(
        FakeInteraction(11, FakeUser(10), guild, channel_id=2, guild_id=1)
    )

    claim_options = bot.tree.get_command("claim-options").callback
    intruder = FakeInteraction(12, FakeUser(99), guild, channel_id=2, guild_id=1)
    await claim_options(intruder, bundle_id=bundle.id)
    assert "refused" in intruder.response.sent[0].lower()

    recipient = FakeInteraction(13, FakeUser("20"), guild, channel_id=2, guild_id=1)
    await claim_options(recipient, bundle_id=bundle.id)
    pages = recipient.response.sent + recipient.followup.sent
    assert any("options:" in page for page in pages)


@pytest.mark.asyncio
async def test_claim_requires_bundle_id_when_ambiguous(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    bot = _make_bot(repository, FakeAnalysisProvider(synthetic_analysis))
    capture = CaptureService(repository)
    first = _capture(repository, synthetic_messages, prefix="a-")
    capture.seal(first.id, actor_user_id="10", idempotency_key="ambiguous-seal-a")
    second = _capture(repository, synthetic_messages, prefix="b-")
    capture.seal(second.id, actor_user_id="10", idempotency_key="ambiguous-seal-b")
    guild = FakeGuild(1)
    recipient = FakeInteraction(12, FakeUser("20"), guild, channel_id=2, guild_id=1)
    await bot.tree.get_command("claim").callback(recipient, options="1")
    assert "refused" in recipient.response.sent[0].lower()
    assert "bundle_id" in recipient.response.sent[0].lower()


@pytest.mark.asyncio
async def test_complete_synthetic_pilot_flow(
    repository, synthetic_messages
) -> None:
    bot = _make_bot(repository, _synthetic_pilot_provider(), pilot_mode=True)
    bundle = _capture(repository, synthetic_messages)
    nursery = FakeChannel(4)
    _cache_channels(bot, {4: nursery})
    guild = FakeGuild(1)

    await bot.tree.get_command("seal").callback(
        FakeInteraction(21, FakeUser(10), guild, channel_id=2, guild_id=1)
    )
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_READY

    recipient = FakeInteraction(22, FakeUser("20"), guild, channel_id=2, guild_id=1)
    await bot.tree.get_command("claim").callback(recipient, options="1")
    assert "Claim recorded" in recipient.response.sent[0]

    await _wait_for(lambda: any("review ready" in text for text in nursery.sent))
    assert repository.get_bundle(bundle.id).status == BundleState.REVIEW_READY
    persisted = repository.get_claim_for_bundle(bundle.id)
    assert persisted.selected_candidate_ids
    assert persisted.selected_candidates


def test_option_menu_pagination_never_truncates() -> None:
    bundle = _capture_like_bundle()
    candidates = [
        ClaimCandidate(
            candidate_id=f"cand-{index}",
            title=f"Option title {index}",
            summary=f"Summary {index} " + ("s" * 400),
            evidence=[
                SourceFragment(
                    source_message_id="m-1",
                    exact_quote=f"Exact evidence quotation {index} " + ("q" * 400),
                    unit_ids=["m-1:p1"],
                )
            ],
            display_order=index,
        )
        for index in range(1, 6)
    ]
    text = format_claim_options(bundle, candidates)
    pages = paginate_text(text)
    assert len(pages) > 1
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    reconstructed = "".join(
        re.sub(r"\n— page \d+/\d+ —$", "", page) for page in pages
    )
    assert reconstructed == text
    for candidate in candidates:
        assert candidate.title in reconstructed
        assert candidate.evidence[0].exact_quote in reconstructed


def _capture_like_bundle():
    from nemoir.domain.models import ConversationBundle

    return ConversationBundle(
        id="bundle-menu",
        guild_id="1",
        intake_channel_id="2",
        submitter_user_id="10",
        recipient_user_id="20",
        source_messages=[],
        source_units=[],
    )


class _FailOnceDiscovery(FakeAnalysisProvider):
    def __init__(self, result) -> None:
        super().__init__(result)
        self.fail_next_discovery = True

    async def discover_claim_candidates(self, request):
        if self.fail_next_discovery:
            self.fail_next_discovery = False
            raise RuntimeError("synthetic discovery failure")
        return await self.discovery.discover_claim_candidates(request)


class _LongMenuDiscovery(FakeAnalysisProvider):
    async def discover_claim_candidates(self, request):
        units = request.source_units[:2]
        candidates = [
            ClaimCandidate(
                candidate_id=f"cand-{index}",
                title=f"Option {index}",
                summary="Summary " + ("s" * 1500),
                evidence=[
                    SourceFragment(
                        source_message_id=unit.source_message_id,
                        exact_quote=unit.exact_text,
                        unit_ids=[unit.unit_id],
                        start_offset=unit.start_offset,
                        end_offset=unit.end_offset,
                    )
                ],
                display_order=index,
            )
            for index, unit in enumerate(units, start=1)
        ]
        return ClaimDiscoveryResponse(
            result=ClaimCandidateSet(bundle_id=request.bundle_id, candidates=candidates),
            receipt=ProviderReceipt(
                provider="fake",
                model="long-menu",
                latency_ms=0,
                outcome="success",
            ),
        )


@pytest.mark.asyncio
async def test_seal_defers_before_awaiting_provider(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingDiscovery(FakeAnalysisProvider):
        async def discover_claim_candidates(self, request):
            started.set()
            await release.wait()
            return await self.discovery.discover_claim_candidates(request)

    bot = _make_bot(repository, BlockingDiscovery(synthetic_analysis))
    bundle = _capture(repository, synthetic_messages)
    guild = FakeGuild(1)
    interaction = FakeInteraction(31, FakeUser(10), guild, channel_id=2, guild_id=1)
    task = asyncio.create_task(bot.tree.get_command("seal").callback(interaction))

    await started.wait()
    assert interaction.response.deferred is True  # deferred before discovery ran
    release.set()
    await task
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_READY


@pytest.mark.asyncio
async def test_seal_delivers_multi_page_options_after_deferral(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    bot = _make_bot(repository, _LongMenuDiscovery(synthetic_analysis))
    bundle = _capture(repository, synthetic_messages)
    guild = FakeGuild(1)
    interaction = FakeInteraction(32, FakeUser(10), guild, channel_id=2, guild_id=1)
    await bot.tree.get_command("seal").callback(interaction)

    assert interaction.response.deferred is True
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_READY
    pages = interaction.followup.sent
    assert len(pages) > 1
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)


@pytest.mark.asyncio
async def test_seal_delivers_visible_failure_after_deferral(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    class FailDiscovery(FakeAnalysisProvider):
        async def discover_claim_candidates(self, request):
            raise RuntimeError("synthetic discovery failure")

    bot = _make_bot(repository, FailDiscovery(synthetic_analysis))
    bundle = _capture(repository, synthetic_messages)
    guild = FakeGuild(1)
    interaction = FakeInteraction(33, FakeUser(10), guild, channel_id=2, guild_id=1)
    await bot.tree.get_command("seal").callback(interaction)

    assert interaction.response.deferred is True
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_FAILED
    assert interaction.response.sent == []  # result went through the follow-up path
    assert any("claim-options-retry" in text for text in interaction.followup.sent)


@pytest.mark.asyncio
async def test_claim_options_retry_regenerates_from_failed(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    provider = _FailOnceDiscovery(synthetic_analysis)
    bot = _make_bot(repository, provider)
    bundle = _capture(repository, synthetic_messages)
    guild = FakeGuild(1)

    first = FakeInteraction(34, FakeUser(10), guild, channel_id=2, guild_id=1)
    await bot.tree.get_command("seal").callback(first)
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_FAILED
    assert any("claim-options-retry" in text for text in first.followup.sent)

    retry = bot.tree.get_command("claim-options-retry").callback
    retry_int = FakeInteraction(35, FakeUser(10), guild, channel_id=2, guild_id=1)
    await retry(retry_int, bundle_id=bundle.id)
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_READY
    assert any('options:"<n>"' in text for text in retry_int.followup.sent)


@pytest.mark.asyncio
async def test_claim_options_retry_authorization_and_state_guard(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    provider = _FailOnceDiscovery(synthetic_analysis)
    bot = _make_bot(repository, provider, admins={"admin-1"})
    bundle = _capture(repository, synthetic_messages)
    guild = FakeGuild(1)
    await bot.tree.get_command("seal").callback(
        FakeInteraction(36, FakeUser(10), guild, channel_id=2, guild_id=1)
    )
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_FAILED

    retry = bot.tree.get_command("claim-options-retry").callback

    # Non-submitter and non-admin are refused.
    intruder = FakeInteraction(37, FakeUser(99), guild, channel_id=2, guild_id=1)
    await retry(intruder, bundle_id=bundle.id)
    assert "refused" in intruder.response.sent[0].lower()
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_FAILED

    # A bundle that is not CLAIM_OPTIONS_FAILED is refused.
    guard = _capture(repository, synthetic_messages, prefix="guard-")
    CaptureService(repository).seal(guard.id, actor_user_id="10", idempotency_key="guard-seal")
    guard_int = FakeInteraction(38, FakeUser(10), guild, channel_id=2, guild_id=1)
    await retry(guard_int, bundle_id=guard.id)
    assert "refused" in guard_int.response.sent[0].lower()

    # The administrator may retry.
    admin = FakeInteraction(39, FakeUser("admin-1"), guild, channel_id=2, guild_id=1)
    await retry(admin, bundle_id=bundle.id)
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_READY
