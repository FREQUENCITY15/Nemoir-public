from __future__ import annotations

import pytest

from nemoir.adapters.discord_bot import (
    DiscordScopePolicy,
    create_discord_bot,
    format_routed_summary,
    sanitize_channel_slug,
)
from nemoir.adapters.pagination import DISCORD_MESSAGE_LIMIT
from nemoir.config import Settings
from nemoir.domain.models import SourceFragment, Tendril
from nemoir.domain.states import Actionability, TendrilState, TendrilType
from nemoir.providers.fake import FakeAnalysisProvider


def _scope() -> DiscordScopePolicy:
    return DiscordScopePolicy(
        guild_id="1",
        intake_channel_id="2",
        anemone_category_id="3",
        nursery_channel_id="4",
        additional_intake_channel_ids=frozenset({"5"}),
    )


def test_discord_scope_restricts_guild_intake_and_category() -> None:
    scope = _scope()
    scope.require_intake(1, 2)
    scope.require_intake(1, 5)
    scope.require_anemone_destination("1", "3")
    with pytest.raises(PermissionError):
        scope.require_intake(99, 2)
    with pytest.raises(PermissionError):
        scope.require_intake(1, 99)
    with pytest.raises(PermissionError):
        scope.require_anemone_destination(1, 99)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Consciousness & Reality", "consciousness-reality"),
        ("  project---seed  ", "project-seed"),
        ("UPPER_case", "upper-case"),
    ],
)
def test_channel_slug_is_sanitized(raw: str, expected: str) -> None:
    assert sanitize_channel_slug(raw) == expected


def test_empty_channel_slug_is_rejected() -> None:
    with pytest.raises(ValueError):
        sanitize_channel_slug("🦋")


def _tendril(description: str) -> Tendril:
    return Tendril(
        id="t-1",
        bundle_id="b-1",
        title="Long routed tendril",
        description=description,
        type=TendrilType.PROJECT_SEED,
        actionability=Actionability.CANDIDATE,
        evidence=[SourceFragment(source_message_id="m-1", exact_quote="exact source quote")],
        why_open="The branch remains open.",
        confidence=0.9,
        status=TendrilState.OPEN,
    )


def test_routed_summary_is_bounded_and_announces_the_attachment() -> None:
    summary = format_routed_summary(_tendril("x" * 5000))
    assert len(summary) <= DISCORD_MESSAGE_LIMIT
    assert summary.endswith("Full evidence with source links is attached (`tendril.txt`).")
    assert "…" in summary


def test_routed_summary_keeps_short_content_untruncated() -> None:
    summary = format_routed_summary(_tendril("A short description."))
    assert len(summary) <= DISCORD_MESSAGE_LIMIT
    assert "A short description." in summary
    assert "…" not in summary
    assert "attached" in summary


def test_routed_summary_is_deterministic() -> None:
    tendril = _tendril("y" * 4000)
    assert format_routed_summary(tendril) == format_routed_summary(tendril)


def test_bot_constructs_and_registers_expected_commands(repository, synthetic_analysis) -> None:
    pytest.importorskip("discord")
    settings = Settings(
        guild_id="1",
        intake_channel_id="2",
        anemone_category_id="3",
        nursery_channel_id="4",
    )
    bot = create_discord_bot(settings, repository, FakeAnalysisProvider(synthetic_analysis))
    assert {command.name for command in bot.tree.get_commands()} == {
        "tend",
        "seal",
        "cancel",
        "claim",
        "claim-options",
        "claim-options-retry",
        "bundle-status",
        "analysis-retry",
        "autonomous-retry",
        "tendrils",
        "tendril-show",
        "pull",
        "snooze",
        "resolve",
        "release",
        "merge",
        "promote-actionable",
        "route",
        "habitat-create",
        "publish-bundle",
        "prompt",
    }


def test_pilot_bot_omits_external_routing_commands(repository, synthetic_analysis) -> None:
    pytest.importorskip("discord")
    settings = Settings(
        guild_id="1",
        intake_channel_id="2",
        anemone_category_id="3",
        nursery_channel_id="4",
    )
    bot = create_discord_bot(
        settings,
        repository,
        FakeAnalysisProvider(synthetic_analysis),
        pilot_mode=True,
    )
    commands = {command.name for command in bot.tree.get_commands()}
    assert "route" not in commands
    assert "habitat-create" not in commands
    assert "publish-bundle" not in commands
    assert {"tend", "seal", "claim", "bundle-status", "prompt"} <= commands
