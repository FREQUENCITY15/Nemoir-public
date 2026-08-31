from __future__ import annotations

from copy import deepcopy

from nemoir.domain.models import AnalysisResult, ConversationBundle
from nemoir.domain.segmentation import segment_messages
from nemoir.domain.validation import validate_analysis


def _bundle(synthetic_messages) -> ConversationBundle:
    return ConversationBundle(
        id="bundle-validation",
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
        source_messages=synthetic_messages,
        source_units=segment_messages(synthetic_messages),
    )


def test_valid_analysis_has_complete_evidence(synthetic_messages, synthetic_analysis) -> None:
    bundle = _bundle(synthetic_messages)
    report = validate_analysis(bundle, bundle.source_units, synthetic_analysis)
    assert report.valid
    assert not report.needs_review
    assert not report.errors


def test_invented_quote_fails_closed(synthetic_messages, synthetic_analysis) -> None:
    payload = deepcopy(synthetic_analysis.model_dump(mode="python"))
    payload["tendrils"][0]["evidence"][0]["exact_quote"] = "Invented sentence."
    result = AnalysisResult.model_validate(payload)
    bundle = _bundle(synthetic_messages)
    report = validate_analysis(bundle, bundle.source_units, result)
    assert not report.valid
    assert "INVENTED_QUOTE" in {item.code for item in report.errors}


def test_missing_coverage_fails_closed(synthetic_messages, synthetic_analysis) -> None:
    payload = deepcopy(synthetic_analysis.model_dump(mode="python"))
    payload["coverage"].pop()
    result = AnalysisResult.model_validate(payload)
    bundle = _bundle(synthetic_messages)
    report = validate_analysis(bundle, bundle.source_units, result)
    assert not report.valid
    assert "MISSING_COVERAGE" in {item.code for item in report.errors}


def test_claimed_unit_leakage_requires_review(synthetic_messages, synthetic_analysis) -> None:
    payload = deepcopy(synthetic_analysis.model_dump(mode="python"))
    for entry in payload["coverage"]:
        if entry["unit_id"] == "synthetic-102:p2":
            entry["classification"] = "TENDRIL"
            entry["tendril_client_ids"] = ["t-time"]
    result = AnalysisResult.model_validate(payload)
    bundle = _bundle(synthetic_messages)
    report = validate_analysis(bundle, bundle.source_units, result)
    assert report.valid
    assert report.needs_review
    assert "CLAIMED_UNIT_LEAKAGE" in {item.code for item in report.warnings}


def test_unknown_unit_is_rejected(synthetic_messages, synthetic_analysis) -> None:
    payload = deepcopy(synthetic_analysis.model_dump(mode="python"))
    payload["claim"]["matching_fragments"][0]["unit_ids"] = ["unknown:p1"]
    result = AnalysisResult.model_validate(payload)
    bundle = _bundle(synthetic_messages)
    report = validate_analysis(bundle, bundle.source_units, result)
    assert not report.valid
    assert "UNKNOWN_FRAGMENT_UNIT" in {item.code for item in report.errors}
