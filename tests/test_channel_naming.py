"""Deterministic channel slug derivation, sanitisation, and collision rules."""

from __future__ import annotations

import pytest

from nemoir.adapters.channel_naming import (
    derive_channel_slug,
    resolve_channel_slug,
    sanitize_channel_slug,
)
from nemoir.domain.models import SourceFragment, Tendril
from nemoir.domain.states import Actionability, TendrilState, TendrilType


def _tendril(*, title: str, slug: str | None = None) -> Tendril:
    return Tendril(
        id="t-1",
        bundle_id="b-1",
        title=title,
        description="description",
        type=TendrilType.OPEN_QUESTION,
        actionability=Actionability.NOT_ACTIONABLE,
        evidence=[SourceFragment(source_message_id="m-1", exact_quote="quote")],
        why_open="open",
        suggested_habitat_slug=slug,
        confidence=0.9,
        status=TendrilState.OPEN,
    )


def test_derive_prefers_suggested_habitat_slug() -> None:
    assert derive_channel_slug(_tendril(title="Ignored Title", slug="Preferred & Slug")) == "preferred-slug"


def test_derive_falls_back_to_title() -> None:
    assert derive_channel_slug(_tendril(title="  Consciousness & Reality  ", slug=None)) == "consciousness-reality"


def test_derive_falls_back_when_both_unsanitisable() -> None:
    assert derive_channel_slug(_tendril(title="🦋", slug="🦋")) == "tendril"


def test_resolve_keeps_free_base() -> None:
    assert resolve_channel_slug("free-will-and-consequence", set()) == "free-will-and-consequence"


def test_resolve_suffixes_deterministically_and_never_adopts() -> None:
    base = "consciousness-and-reality"
    existing = {"consciousness-and-reality", "consciousness-and-reality-2"}
    assert resolve_channel_slug(base, existing) == "consciousness-and-reality-3"
    # The same input always resolves the same way.
    assert resolve_channel_slug(base, existing) == "consciousness-and-reality-3"


def test_resolve_trim_base_to_fit_suffix() -> None:
    base = "x" * 90
    resolved = resolve_channel_slug(base, {base})
    assert resolved == ("x" * 88) + "-2"


def test_sanitize_rejects_empty() -> None:
    with pytest.raises(ValueError):
        sanitize_channel_slug("🦋")
