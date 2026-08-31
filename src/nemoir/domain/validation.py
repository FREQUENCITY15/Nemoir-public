"""Deterministic validation of inferred provider output against source evidence."""

from __future__ import annotations

from collections import Counter

from pydantic import Field

from .models import (
    AnalysisResult,
    AutonomousSortResult,
    ClaimCandidate,
    ClaimCandidateSet,
    ConversationBundle,
    SourceFragment,
    SourceMessage,
    SourceUnit,
    StrictModel,
    authoritative_claim_boundary,
)
from .states import CoverageClassification


class ValidationIssue(StrictModel):
    code: str
    message: str
    unit_id: str | None = None
    tendril_client_id: str | None = None


class ValidationReport(StrictModel):
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    needs_review: bool = False

    @property
    def valid(self) -> bool:
        return not self.errors


def _quote_spans(text: str, quote: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = text.find(quote, cursor)
        if start < 0:
            break
        spans.append((start, start + len(quote)))
        cursor = start + 1
    return spans


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return max(left[0], right[0]) < min(left[1], right[1])


def _validate_fragment_evidence(
    fragment: SourceFragment,
    messages: dict[str, SourceMessage],
    units_by_id: dict[str, SourceUnit],
    *,
    role: str,
    tendril_id: str | None = None,
) -> list[ValidationIssue]:
    """Deterministic exact-quote/span validation for one source fragment."""
    issues: list[ValidationIssue] = []
    message = messages.get(fragment.source_message_id)
    if message is None:
        issues.append(
            ValidationIssue(
                code="UNKNOWN_SOURCE_MESSAGE",
                message=f"{role} fragment references unknown message {fragment.source_message_id}",
                tendril_client_id=tendril_id,
            )
        )
        return issues
    spans = _quote_spans(message.content, fragment.exact_quote)
    if not spans:
        issues.append(
            ValidationIssue(
                code="INVENTED_QUOTE",
                message=f"Quote is not an exact substring of message {fragment.source_message_id}",
                tendril_client_id=tendril_id,
            )
        )
        return issues
    if fragment.start_offset is not None:
        supplied = (fragment.start_offset, fragment.end_offset or 0)
        if supplied not in spans:
            issues.append(
                ValidationIssue(
                    code="INVALID_FRAGMENT_OFFSETS",
                    message="Supplied offsets do not identify the exact quotation",
                    tendril_client_id=tendril_id,
                )
            )
    for unit_id in fragment.unit_ids:
        unit = units_by_id.get(unit_id)
        if unit is None or unit.source_message_id != fragment.source_message_id:
            issues.append(
                ValidationIssue(
                    code="UNKNOWN_FRAGMENT_UNIT",
                    message=f"Fragment references invalid unit {unit_id}",
                    unit_id=unit_id,
                    tendril_client_id=tendril_id,
                )
            )
        elif fragment.exact_quote not in unit.exact_text:
            issues.append(
                ValidationIssue(
                    code="QUOTE_OUTSIDE_UNIT",
                    message=f"Exact quote is not contained by declared unit {unit_id}",
                    unit_id=unit_id,
                    tendril_client_id=tendril_id,
                )
            )
    return issues


def validate_analysis(
    bundle: ConversationBundle,
    units: list[SourceUnit],
    result: AnalysisResult,
    *,
    selected_candidates: list[ClaimCandidate] | None = None,
    discovered_candidates: list[ClaimCandidate] | None = None,
) -> ValidationReport:
    report = ValidationReport()
    messages = {item.external_message_id: item for item in bundle.source_messages}
    units_by_id = {item.unit_id: item for item in units}
    client_ids = {item.client_id for item in result.tendrils}

    if len(client_ids) != len(result.tendrils):
        report.errors.append(
            ValidationIssue(code="DUPLICATE_TENDRIL_CLIENT_ID", message="Tendril client IDs must be unique")
        )

    fragment_records: list[tuple[str, SourceFragment, str | None]] = []
    for fragment in result.claim.matching_fragments:
        fragment_records.append(("claim", fragment, None))
    for tendril in result.tendrils:
        if not tendril.evidence:
            report.errors.append(
                ValidationIssue(
                    code="TENDRIL_WITHOUT_EVIDENCE",
                    message="Every tendril requires source evidence",
                    tendril_client_id=tendril.client_id,
                )
            )
        for fragment in tendril.evidence:
            fragment_records.append(("tendril", fragment, tendril.client_id))

    for role, fragment, tendril_id in fragment_records:
        report.errors.extend(
            _validate_fragment_evidence(
                fragment,
                messages,
                units_by_id,
                role=role,
                tendril_id=tendril_id,
            )
        )

    coverage_counts = Counter(entry.unit_id for entry in result.coverage)
    for unit_id in units_by_id:
        count = coverage_counts.get(unit_id, 0)
        if count == 0:
            report.errors.append(
                ValidationIssue(
                    code="MISSING_COVERAGE",
                    message=f"Source unit {unit_id} has no coverage entry",
                    unit_id=unit_id,
                )
            )
        elif count > 1:
            report.errors.append(
                ValidationIssue(
                    code="DUPLICATE_COVERAGE",
                    message=f"Source unit {unit_id} has {count} coverage entries",
                    unit_id=unit_id,
                )
            )

    for entry in result.coverage:
        unit = units_by_id.get(entry.unit_id)
        if unit is None:
            report.errors.append(
                ValidationIssue(
                    code="UNKNOWN_COVERAGE_UNIT",
                    message=f"Coverage references unknown unit {entry.unit_id}",
                    unit_id=entry.unit_id,
                )
            )
            continue
        if unit.source_message_id != entry.source_message_id:
            report.errors.append(
                ValidationIssue(
                    code="COVERAGE_MESSAGE_MISMATCH",
                    message=f"Coverage message does not own {entry.unit_id}",
                    unit_id=entry.unit_id,
                )
            )
        unknown_targets = set(entry.tendril_client_ids) - client_ids
        if unknown_targets:
            report.errors.append(
                ValidationIssue(
                    code="UNKNOWN_COVERAGE_TENDRIL",
                    message=f"Coverage references unknown tendrils: {sorted(unknown_targets)}",
                    unit_id=entry.unit_id,
                )
            )
        if entry.classification == CoverageClassification.TENDRIL and not entry.tendril_client_ids:
            report.errors.append(
                ValidationIssue(
                    code="TENDRIL_COVERAGE_WITHOUT_TARGET",
                    message="TENDRIL coverage requires at least one target",
                    unit_id=entry.unit_id,
                )
            )
        if entry.classification != CoverageClassification.TENDRIL and entry.tendril_client_ids:
            report.errors.append(
                ValidationIssue(
                    code="NON_TENDRIL_COVERAGE_WITH_TARGET",
                    message="Only TENDRIL coverage may target tendrils",
                    unit_id=entry.unit_id,
                )
            )

    claim_units = {
        unit_id for fragment in result.claim.matching_fragments for unit_id in fragment.unit_ids
    }
    for entry in result.coverage:
        if entry.unit_id in claim_units and entry.classification == CoverageClassification.TENDRIL:
            report.warnings.append(
                ValidationIssue(
                    code="CLAIMED_UNIT_LEAKAGE",
                    message=f"Claimed unit {entry.unit_id} is independently classified as a tendril",
                    unit_id=entry.unit_id,
                )
            )
            report.needs_review = True

    claim_spans: dict[str, list[tuple[int, int]]] = {}
    for fragment in result.claim.matching_fragments:
        message = messages.get(fragment.source_message_id)
        if message:
            claim_spans.setdefault(fragment.source_message_id, []).extend(
                _quote_spans(message.content, fragment.exact_quote)
            )

    for tendril in result.tendrils:
        for fragment in tendril.evidence:
            message = messages.get(fragment.source_message_id)
            if not message:
                continue
            tendril_spans = _quote_spans(message.content, fragment.exact_quote)
            overlaps = any(
                _spans_overlap(claim_span, tendril_span)
                for claim_span in claim_spans.get(fragment.source_message_id, [])
                for tendril_span in tendril_spans
            )
            if overlaps:
                report.warnings.append(
                    ValidationIssue(
                        code="CLAIM_TENDRIL_OVERLAP",
                        message=(
                            tendril.overlap_explanation
                            or "Tendril evidence overlaps material matched to the recipient claim"
                        ),
                        tendril_client_id=tendril.client_id,
                    )
                )
                report.needs_review = True

    if selected_candidates:
        _validate_selected_claim_boundary(report, result, selected_candidates)

    if discovered_candidates:
        _validate_discovered_evidence_continuity(report, result, discovered_candidates)

    return report


def _validate_discovered_evidence_continuity(
    report: ValidationReport,
    result: AnalysisResult,
    discovered_candidates: list[ClaimCandidate],
) -> None:
    """Every discovered candidate evidence fragment must survive analysis.

    Each exact fragment surfaced during claim discovery must end up in the
    final claim or in at least one tendril. It must never silently become
    CONTEXT, and TENDRIL coverage must reference a tendril that actually
    contains the fragment's quotation.
    """
    claim_keys = {
        (fragment.source_message_id, fragment.exact_quote)
        for fragment in result.claim.matching_fragments
    }
    tendril_keys_by_client = {
        tendril.client_id: {
            (fragment.source_message_id, fragment.exact_quote)
            for fragment in tendril.evidence
        }
        for tendril in result.tendrils
    }
    all_tendril_keys = set().union(*tendril_keys_by_client.values()) if tendril_keys_by_client else set()
    coverage_by_unit = {entry.unit_id: entry for entry in result.coverage}

    for candidate in discovered_candidates:
        for fragment in candidate.evidence:
            key = (fragment.source_message_id, fragment.exact_quote)
            represented = key in claim_keys or key in all_tendril_keys

            # Candidate evidence must never be classified CONTEXT, even when it
            # is represented elsewhere (an internal coverage inconsistency).
            entries = [
                coverage_by_unit[unit_id]
                for unit_id in fragment.unit_ids
                if unit_id in coverage_by_unit
            ]
            context_entries = [
                entry for entry in entries
                if entry.classification == CoverageClassification.CONTEXT
            ]
            if context_entries:
                report.errors.append(
                    ValidationIssue(
                        code="DISCOVERED_EVIDENCE_MISCLASSIFIED",
                        message=(
                            f"Discovered candidate evidence {key[0]}:{key[1]!r} "
                            "was surfaced but classified CONTEXT"
                        ),
                        unit_id=context_entries[0].unit_id,
                    )
                )
                continue

            if represented:
                continue

            # Not represented in claim or any tendril evidence.
            tendril_entries = [
                entry for entry in entries
                if entry.classification == CoverageClassification.TENDRIL
            ]
            if tendril_entries:
                report.errors.append(
                    ValidationIssue(
                        code="DISCOVERED_EVIDENCE_MISCLASSIFIED",
                        message=(
                            f"Discovered candidate evidence {key[0]}:{key[1]!r} "
                            "is classified TENDRIL but no tendril contains the quotation"
                        ),
                        unit_id=tendril_entries[0].unit_id,
                    )
                )
            else:
                report.errors.append(
                    ValidationIssue(
                        code="DISCOVERED_EVIDENCE_DROPPED",
                        message=(
                            f"Discovered candidate evidence {key[0]}:{key[1]!r} "
                            "was neither claimed nor preserved in a tendril"
                        ),
                        unit_id=entries[0].unit_id if entries else None,
                    )
                )


def _validate_selected_claim_boundary(
    report: ValidationReport,
    result: AnalysisResult,
    selected_candidates: list[ClaimCandidate],
) -> None:
    """The combined selected-candidate evidence is the authoritative boundary.

    The second-stage decomposition must not reinterpret which quotations the
    recipient claimed: its ``claim.matching_fragments`` must preserve exactly
    the deterministic union of the selected candidates' evidence, without
    dropping, inventing, or replacing any quotation. Overlapping fragments are
    deduplicated in the boundary but every exact quotation must survive.
    """
    expected = {
        (fragment.source_message_id, fragment.exact_quote)
        for fragment in authoritative_claim_boundary(selected_candidates)
    }
    actual = {
        (fragment.source_message_id, fragment.exact_quote)
        for fragment in result.claim.matching_fragments
    }
    dropped = expected - actual
    replaced = actual - expected
    if dropped:
        report.errors.append(
            ValidationIssue(
                code="SELECTED_EVIDENCE_DROPPED",
                message=(
                    "Analysis claim dropped selected-candidate evidence: "
                    + ", ".join(sorted(f"{mid}:{quote!r}" for mid, quote in dropped))
                ),
            )
        )
    if replaced:
        report.errors.append(
            ValidationIssue(
                code="SELECTED_EVIDENCE_REPLACED",
                message=(
                    "Analysis claim invented or replaced selected-candidate evidence: "
                    + ", ".join(sorted(f"{mid}:{quote!r}" for mid, quote in replaced))
                ),
            )
        )


def validate_claim_candidates(
    bundle: ConversationBundle,
    units: list[SourceUnit],
    candidate_set: ClaimCandidateSet,
) -> ValidationReport:
    """Validate a provider-produced claim-candidate set against source truth.

    Candidate titles and summaries are AI interpretations and are only checked
    to be non-empty. Evidence quotations are source truth: every fragment must
    reference a known message, quote it exactly, supply valid offsets when
    offsets are present, and belong only to declared units. Candidate IDs must
    be unique and display order must be exactly 1..N with no gaps so the menu
    and the persisted selection are deterministic and stable.
    """
    report = ValidationReport()
    messages = {item.external_message_id: item for item in bundle.source_messages}
    units_by_id = {item.unit_id: item for item in units}
    candidates = candidate_set.candidates

    if candidate_set.bundle_id != bundle.id:
        report.errors.append(
            ValidationIssue(
                code="CANDIDATE_BUNDLE_MISMATCH",
                message="Candidate set references a different bundle",
            )
        )

    if len(candidates) < 2 or len(candidates) > 5:
        report.errors.append(
            ValidationIssue(
                code="CANDIDATE_COUNT",
                message=f"Expected 2..5 claim candidates, got {len(candidates)}",
            )
        )

    candidate_ids = [item.candidate_id for item in candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        report.errors.append(
            ValidationIssue(code="DUPLICATE_CANDIDATE_ID", message="Candidate IDs must be unique")
        )

    expected_order = list(range(1, len(candidates) + 1))
    if sorted(item.display_order for item in candidates) != expected_order:
        report.errors.append(
            ValidationIssue(
                code="CANDIDATE_ORDER",
                message="Candidate display order must be exactly 1..N with no gaps",
            )
        )

    for candidate in candidates:
        if not candidate.title.strip():
            report.errors.append(
                ValidationIssue(
                    code="EMPTY_CANDIDATE_TITLE",
                    message=f"Candidate {candidate.candidate_id} has an empty title",
                    unit_id=candidate.candidate_id,
                )
            )
        if not candidate.summary.strip():
            report.errors.append(
                ValidationIssue(
                    code="EMPTY_CANDIDATE_SUMMARY",
                    message=f"Candidate {candidate.candidate_id} has an empty summary",
                    unit_id=candidate.candidate_id,
                )
            )
        if not candidate.evidence:
            report.errors.append(
                ValidationIssue(
                    code="CANDIDATE_WITHOUT_EVIDENCE",
                    message=f"Candidate {candidate.candidate_id} has no evidence",
                    unit_id=candidate.candidate_id,
                )
            )
        for fragment in candidate.evidence:
            report.errors.extend(
                _validate_fragment_evidence(
                    fragment,
                    messages,
                    units_by_id,
                    role=f"candidate {candidate.candidate_id}",
                )
            )

    return report


def validate_autonomous_sort(
    bundle: ConversationBundle,
    units: list[SourceUnit],
    result: AutonomousSortResult,
) -> ValidationReport:
    """Deterministic full-source validation of an autonomous sort result.

    Autonomous sorting has no claim stage, so the contract is strict: every
    sealed source unit must be assigned to exactly one primary topic, no unit
    may become unaccounted CONTEXT, every topic must be non-empty and
    source-backed with exact quotations, offsets, and unit IDs that match
    source truth, and no evidence may be invented, silently dropped, or
    duplicated across primary topics. Topic IDs and display order must be
    unique and deterministic (display order exactly 1..N with no gaps).

    Any error fails the result closed: the caller must not create tendrils,
    channels, or a claim row from invalid output.
    """
    report = ValidationReport()
    messages = {item.external_message_id: item for item in bundle.source_messages}
    units_by_id = {item.unit_id: item for item in units}
    topics = result.topics

    if result.bundle_id != bundle.id:
        report.errors.append(
            ValidationIssue(
                code="AUTONOMOUS_BUNDLE_MISMATCH",
                message="Autonomous sort result references a different bundle",
            )
        )
    if not topics:
        report.errors.append(
            ValidationIssue(code="AUTONOMOUS_NO_TOPICS", message="The sort produced no topics")
        )

    client_ids = [topic.provider_client_id for topic in topics]
    if len(set(client_ids)) != len(client_ids):
        report.errors.append(
            ValidationIssue(
                code="DUPLICATE_TOPIC_CLIENT_ID",
                message="Autonomous topic provider/client IDs must be unique",
            )
        )
    expected_order = list(range(1, len(topics) + 1))
    if sorted(topic.display_order for topic in topics) != expected_order:
        report.errors.append(
            ValidationIssue(
                code="AUTONOMOUS_TOPIC_ORDER",
                message="Autonomous topic display order must be exactly 1..N with no gaps",
            )
        )

    topic_by_client = {topic.provider_client_id: topic for topic in topics}

    # Every topic is non-empty and every evidence fragment is source truth.
    seen_fragments: dict[tuple[str, str], str] = {}
    for topic in topics:
        client_id = topic.provider_client_id
        if not topic.title.strip():
            report.errors.append(
                ValidationIssue(
                    code="EMPTY_TOPIC_TITLE",
                    message=f"Topic {client_id} has an empty title",
                    tendril_client_id=client_id,
                )
            )
        if not topic.summary.strip():
            report.errors.append(
                ValidationIssue(
                    code="EMPTY_TOPIC_SUMMARY",
                    message=f"Topic {client_id} has an empty summary",
                    tendril_client_id=client_id,
                )
            )
        if not topic.why_open.strip():
            report.errors.append(
                ValidationIssue(
                    code="EMPTY_TOPIC_WHY_OPEN",
                    message=f"Topic {client_id} has no open/meaningful explanation",
                    tendril_client_id=client_id,
                )
            )
        if not topic.evidence:
            report.errors.append(
                ValidationIssue(
                    code="TOPIC_WITHOUT_EVIDENCE",
                    message=f"Topic {client_id} has no evidence",
                    tendril_client_id=client_id,
                )
            )
        for fragment in topic.evidence:
            report.errors.extend(
                _validate_fragment_evidence(
                    fragment,
                    messages,
                    units_by_id,
                    role=f"topic {client_id}",
                    tendril_id=client_id,
                )
            )
            # The autonomous contract requires exact offsets and unit IDs so
            # evidence is precisely anchored to source truth.
            if not fragment.unit_ids:
                report.errors.append(
                    ValidationIssue(
                        code="AUTONOMOUS_FRAGMENT_WITHOUT_UNITS",
                        message=f"Topic {client_id} evidence declares no source unit IDs",
                        tendril_client_id=client_id,
                    )
                )
            if fragment.start_offset is None or fragment.end_offset is None:
                report.errors.append(
                    ValidationIssue(
                        code="AUTONOMOUS_FRAGMENT_WITHOUT_OFFSETS",
                        message=f"Topic {client_id} evidence supplies no exact offsets",
                        tendril_client_id=client_id,
                    )
                )
            key = (fragment.source_message_id, fragment.exact_quote)
            prior_owner = seen_fragments.get(key)
            if prior_owner is not None and prior_owner != client_id:
                report.errors.append(
                    ValidationIssue(
                        code="AUTONOMOUS_DUPLICATE_EVIDENCE",
                        message=(
                            f"Evidence {key[0]}:{key[1]!r} is duplicated across "
                            f"primary topics {prior_owner} and {client_id}"
                        ),
                        tendril_client_id=client_id,
                    )
                )
            else:
                seen_fragments[key] = client_id

    # Full-source coverage: exactly one coverage entry per unit, every entry
    # classified TENDRIL targeting exactly one topic, and that topic must
    # actually contain the unit's text in a declared, validated fragment.
    coverage_counts = Counter(entry.unit_id for entry in result.coverage)
    for unit_id in units_by_id:
        count = coverage_counts.get(unit_id, 0)
        if count == 0:
            report.errors.append(
                ValidationIssue(
                    code="MISSING_COVERAGE",
                    message=f"Source unit {unit_id} has no coverage entry",
                    unit_id=unit_id,
                )
            )
        elif count > 1:
            report.errors.append(
                ValidationIssue(
                    code="DUPLICATE_COVERAGE",
                    message=f"Source unit {unit_id} has {count} coverage entries",
                    unit_id=unit_id,
                )
            )

    for entry in result.coverage:
        unit = units_by_id.get(entry.unit_id)
        if unit is None:
            report.errors.append(
                ValidationIssue(
                    code="UNKNOWN_COVERAGE_UNIT",
                    message=f"Coverage references unknown unit {entry.unit_id}",
                    unit_id=entry.unit_id,
                )
            )
            continue
        if unit.source_message_id != entry.source_message_id:
            report.errors.append(
                ValidationIssue(
                    code="COVERAGE_MESSAGE_MISMATCH",
                    message=f"Coverage message does not own {entry.unit_id}",
                    unit_id=entry.unit_id,
                )
            )
        if entry.classification != CoverageClassification.TENDRIL:
            report.errors.append(
                ValidationIssue(
                    code="AUTONOMOUS_CONTEXT_COVERAGE",
                    message=(
                        f"Unit {entry.unit_id} is classified {entry.classification.value}; "
                        "autonomous sorts must assign every unit to exactly one "
                        "primary topic and never leave CONTEXT material"
                    ),
                    unit_id=entry.unit_id,
                )
            )
        if len(entry.tendril_client_ids) != 1:
            report.errors.append(
                ValidationIssue(
                    code="AUTONOMOUS_PRIMARY_TOPIC_ASSIGNMENT",
                    message=(
                        f"Unit {entry.unit_id} must be assigned to exactly one "
                        f"primary topic, got {len(entry.tendril_client_ids)}"
                    ),
                    unit_id=entry.unit_id,
                )
            )
        unknown_targets = set(entry.tendril_client_ids) - set(client_ids)
        if unknown_targets:
            report.errors.append(
                ValidationIssue(
                    code="UNKNOWN_COVERAGE_TOPIC",
                    message=f"Coverage references unknown topics: {sorted(unknown_targets)}",
                    unit_id=entry.unit_id,
                )
            )
            continue
        if len(entry.tendril_client_ids) == 1:
            topic = topic_by_client.get(entry.tendril_client_ids[0])
            if topic is not None:
                backed = any(
                    unit.unit_id in fragment.unit_ids
                    and unit.exact_text in fragment.exact_quote
                    for fragment in topic.evidence
                )
                if not backed:
                    report.errors.append(
                        ValidationIssue(
                            code="AUTONOMOUS_UNIT_NOT_SOURCE_BACKED",
                            message=(
                                f"Topic {topic.provider_client_id} is the primary topic for "
                                f"unit {entry.unit_id} but none of its evidence declares "
                                "the unit and quotes its exact text"
                            ),
                            unit_id=entry.unit_id,
                            tendril_client_id=topic.provider_client_id,
                        )
                    )

    return report
