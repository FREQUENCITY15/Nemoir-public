from __future__ import annotations

import pytest

from nemoir.domain.errors import InvalidTransitionError
from nemoir.domain.segmentation import segment_message, segment_messages
from nemoir.domain.states import (
    BundleState,
    TendrilState,
    require_bundle_transition,
    require_tendril_transition,
)


def test_allowed_and_rejected_bundle_transitions() -> None:
    require_bundle_transition(BundleState.CAPTURING, BundleState.SEALED)
    require_bundle_transition(BundleState.SEALED, BundleState.AWAITING_CLAIM)
    with pytest.raises(InvalidTransitionError):
        require_bundle_transition(BundleState.CAPTURING, BundleState.REVIEW_READY)


def test_allowed_and_rejected_tendril_transitions() -> None:
    require_tendril_transition(TendrilState.OPEN, TendrilState.RESURFACED)
    require_tendril_transition(TendrilState.RESURFACED, TendrilState.RESOLVED)
    with pytest.raises(InvalidTransitionError):
        require_tendril_transition(TendrilState.RESOLVED, TendrilState.OPEN)


def test_segmentation_is_deterministic_and_preserves_offsets(synthetic_messages) -> None:
    first = segment_messages(synthetic_messages)
    second = segment_messages(synthetic_messages)
    assert first == second
    assert [item.unit_id for item in first] == [
        "synthetic-101:p1",
        "synthetic-101:p2",
        "synthetic-102:p1",
        "synthetic-102:p2",
        "synthetic-102:p3",
        "synthetic-103:p1",
        "synthetic-103:p2",
    ]
    by_message = {item.external_message_id: item for item in synthetic_messages}
    for unit in first:
        source = by_message[unit.source_message_id].content
        assert source[unit.start_offset : unit.end_offset] == unit.exact_text


def test_empty_and_whitespace_paragraphs_are_not_units(synthetic_messages) -> None:
    message = synthetic_messages[0].model_copy(
        update={"external_message_id": "whitespace", "content": "  \n\nFirst\n\n  \n\nSecond  "}
    )
    units = segment_message(message)
    assert [item.exact_text for item in units] == ["First", "Second"]
