"""Free deterministic provider for development and regression tests."""

from __future__ import annotations

from copy import deepcopy

from nemoir.domain.models import (
    AnalysisRequest,
    AnalysisResponse,
    AnalysisResult,
    ClaimAnalysis,
    ClaimCandidate,
    ClaimDiscoveryRequest,
    ClaimDiscoveryResponse,
    CoverageEntry,
    PromptRequest,
    PromptResponse,
    ProviderReceipt,
    SourceFragment,
    SourceMessage,
    TendrilCandidate,
    authoritative_claim_boundary,
    join_selected_titles,
)
from nemoir.domain.segmentation import segment_messages
from nemoir.domain.states import Actionability, CoverageClassification, TendrilType
from nemoir.providers.synthetic import (
    SyntheticClaimDiscoveryProvider,
    SyntheticPromptProvider,
    significant_stems,
    synthetic_claim_candidates,
)


class FakeAnalysisProvider:
    def __init__(
        self,
        result: AnalysisResult,
        *,
        variant: str = "valid",
        prompt_answer: str | None = None,
        prompt_variant: str = "valid",
    ) -> None:
        self.result = result
        self.variant = variant
        self.call_count = 0
        self.last_request: AnalysisRequest | None = None
        self.discovery = SyntheticClaimDiscoveryProvider()
        self.prompt_answer = prompt_answer
        self.prompt_variant = prompt_variant
        self.prompt_call_count = 0
        self.last_prompt_request: PromptRequest | None = None

    async def discover_claim_candidates(
        self, request: ClaimDiscoveryRequest
    ) -> ClaimDiscoveryResponse:
        return await self.discovery.discover_claim_candidates(request)

    async def prompt(self, request: PromptRequest) -> PromptResponse:
        self.prompt_call_count += 1
        self.last_prompt_request = request
        if self.prompt_variant == "provider_failure":
            raise RuntimeError("Synthetic prompt failure")
        if self.prompt_variant != "valid":
            raise ValueError(f"Unknown fake prompt variant: {self.prompt_variant}")
        text = (
            self.prompt_answer
            if self.prompt_answer is not None
            else f"Synthetic answer to: {request.question}"
        )
        return PromptResponse(
            answer=text,
            receipt=ProviderReceipt(
                provider="fake",
                model=f"fixture-prompt-{self.prompt_variant}",
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
                outcome="success",
                request_id=f"fake-prompt-{self.prompt_call_count}",
            ),
        )

    async def analyse(self, request: AnalysisRequest) -> AnalysisResponse:
        self.call_count += 1
        self.last_request = request
        payload = deepcopy(self.result.model_dump(mode="python"))
        if self.variant == "invented_quote":
            payload["tendrils"][0]["evidence"][0]["exact_quote"] = "This quotation never existed."
        elif self.variant == "unknown_message":
            payload["tendrils"][0]["evidence"][0]["source_message_id"] = "missing-message"
        elif self.variant == "missing_coverage":
            payload["coverage"] = payload["coverage"][:-1]
        elif self.variant == "claim_leakage":
            claimed_unit = payload["claim"]["matching_fragments"][0]["unit_ids"][0]
            for entry in payload["coverage"]:
                if entry["unit_id"] == claimed_unit:
                    entry["classification"] = "TENDRIL"
                    entry["tendril_client_ids"] = [payload["tendrils"][0]["client_id"]]
                    break
        elif self.variant == "provider_failure":
            raise RuntimeError("Synthetic provider failure")
        elif self.variant != "valid":
            raise ValueError(f"Unknown fake-provider variant: {self.variant}")
        result = AnalysisResult.model_validate(payload)
        if request.selected_candidates:
            result = adapt_analysis_to_selected_candidates(result, request.selected_candidates)
        elif request.discovered_candidates:
            inferred = infer_candidate_for_claim(request.raw_claim, request.discovered_candidates)
            if inferred is not None:
                result = adapt_analysis_to_selected_candidates(
                    result, [inferred], claim_label=request.raw_claim
                )
        return AnalysisResponse(
            result=result,
            receipt=ProviderReceipt(
                provider="fake",
                model=f"fixture-{self.variant}",
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
                outcome="success",
                request_id=f"fake-{self.call_count}",
            ),
        )


class SyntheticFixtureAnalysisProvider:
    """Remap the fixed synthetic result onto matching live Discord message IDs."""

    def __init__(
        self,
        fixture_messages: list[SourceMessage],
        fixture_claim: str,
        result: AnalysisResult,
    ) -> None:
        if not fixture_messages:
            raise ValueError("Synthetic pilot fixture must contain at least one message")
        self.fixture_messages = fixture_messages
        self.fixture_claim = fixture_claim
        self.result = result
        self.call_count = 0
        self.last_request: AnalysisRequest | None = None
        self.discovery = SyntheticClaimDiscoveryProvider()
        self.prompt_provider = SyntheticPromptProvider()
        # The two-stage flow claims through a discovered option, so the pilot
        # accepts the fixture claim or any title its own deterministic
        # discovery produces from the same fixture content.
        self.allowed_claims = {fixture_claim}
        units = segment_messages(fixture_messages)
        if units:
            discovered = synthetic_claim_candidates(
                ClaimDiscoveryRequest(
                    bundle_id="synthetic-pilot",
                    messages=fixture_messages,
                    source_units=units,
                )
            )
            self.allowed_claims |= {candidate.title for candidate in discovered.candidates}

    async def discover_claim_candidates(
        self, request: ClaimDiscoveryRequest
    ) -> ClaimDiscoveryResponse:
        return await self.discovery.discover_claim_candidates(request)

    async def analyse(self, request: AnalysisRequest) -> AnalysisResponse:
        self.call_count += 1
        self.last_request = request
        expected_content = [item.content for item in self.fixture_messages]
        actual_content = [item.content for item in request.messages]
        if actual_content != expected_content:
            raise ValueError(
                "Discord pilot fake provider accepts only the exact ordered synthetic fixture"
            )
        message_id_map = {
            fixture.external_message_id: actual.external_message_id
            for fixture, actual in zip(
                self.fixture_messages, request.messages, strict=True
            )
        }
        remapped = remap_analysis_message_ids(self.result, message_id_map)
        if request.selected_candidates:
            if request.raw_claim.strip() != join_selected_titles(request.selected_candidates):
                raise ValueError(
                    "Discord pilot fake provider rejects a combined claim whose "
                    "topic does not match its selected candidates"
                )
            remapped = adapt_analysis_to_selected_candidates(
                remapped, request.selected_candidates
            )
        else:
            if request.raw_claim.strip() not in self.allowed_claims:
                raise ValueError(
                    "Discord pilot fake provider accepts only a synthetic fixture claim"
                )
            if request.discovered_candidates:
                inferred = infer_candidate_for_claim(
                    request.raw_claim, request.discovered_candidates
                )
                if inferred is not None:
                    remapped = adapt_analysis_to_selected_candidates(
                        remapped, [inferred], claim_label=request.raw_claim
                    )
        return AnalysisResponse(
            result=remapped,
            receipt=ProviderReceipt(
                provider="fake",
                model="synthetic-discord-pilot",
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
                outcome="success",
                request_id=f"fake-pilot-{self.call_count}",
            ),
        )

    async def prompt(self, request: PromptRequest) -> PromptResponse:
        """Deterministic synthetic single-turn answer; never touches DeepSeek."""
        return await self.prompt_provider.prompt(request)


def infer_candidate_for_claim(
    raw_claim: str, candidates: list[ClaimCandidate]
) -> ClaimCandidate | None:
    """Deterministically map a custom claim onto the best-matching candidate.

    Used by the synthetic analysis adapter for custom-topic claims: the claim
    text is matched against each candidate's title, summary, and evidence via
    significant-stem overlap, and the highest-scoring candidate (ties resolved
    by display order) is treated as the inferred claim boundary.
    """
    claim_stems = significant_stems(raw_claim)
    if not claim_stems:
        return None
    best: ClaimCandidate | None = None
    best_score = 0
    for candidate in sorted(candidates, key=lambda item: item.display_order):
        text = " ".join(
            [candidate.title, candidate.summary]
            + [fragment.exact_quote for fragment in candidate.evidence]
        )
        score = len(claim_stems & significant_stems(text))
        if score > best_score:
            best_score = score
            best = candidate
    return best


def _slug(text: str) -> str:
    """Deterministic lowercase slug for a stable, readable tendril client id."""
    parts: list[str] = []
    for char in text.lower():
        if char.isalnum():
            parts.append(char)
        elif parts and parts[-1] != "-":
            parts.append("-")
    return "".join(parts).strip("-") or "former-claim"


def _unique_client_id(base: str, taken: set[str]) -> str:
    """Return ``base``, or a deterministic ``-N`` suffix, until it is unused."""
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _former_claim_tendril(
    claim: ClaimAnalysis,
    evidence: list[SourceFragment],
    *,
    taken_client_ids: set[str],
) -> TendrilCandidate:
    """Demote evidence that used to be inside the base claim into a tendril.

    The base claim's label, rationale, and confidence are reused so the
    resulting tendril is deterministic and stable across replays; only the
    evidence is the new (unselected) subset of the former claim.
    """
    client_id = _unique_client_id(
        f"t-{_slug(claim.normalized_label)}", taken_client_ids
    )
    return TendrilCandidate(
        client_id=client_id,
        title=claim.normalized_label[:160] or "Former claim material",
        description=claim.rationale or "Material formerly inside the selected claim.",
        type=TendrilType.INTERESTING,
        actionability=Actionability.NOT_ACTIONABLE,
        evidence=evidence,
        why_open=(
            "This material sat inside a previously declared claim but now falls "
            "outside the newly selected claim boundary."
        ),
        confidence=claim.confidence,
    )


def adapt_analysis_to_selected_candidates(
    analysis: AnalysisResult,
    selected_candidates: list[ClaimCandidate],
    *,
    claim_label: str | None = None,
) -> AnalysisResult:
    """Rewrite a fixed analysis so the selected candidates are the boundary.

    Multi-option adaptation is a complete deterministic repartition of the
    discovered evidence already present in the base analysis (its claim
    fragments plus its tendril evidence). The union of the selected candidates'
    evidence becomes the claim's matching fragments. Every remaining discovered
    quotation stays represented in exactly one source-backed tendril: evidence
    already inside a base tendril is kept there, and evidence that used to be
    inside the base claim but now falls outside the new boundary is demoted into
    a deterministic tendril rather than dropped. Coverage reclassifies the
    selected units as CLAIMED and every unselected discovered unit as TENDRIL
    referencing the tendril that actually contains its quotation.
    """
    boundary = authoritative_claim_boundary(selected_candidates)
    selected_keys = {
        (fragment.source_message_id, fragment.exact_quote) for fragment in boundary
    }
    selected_unit_ids = {
        unit_id for fragment in boundary for unit_id in fragment.unit_ids
    }

    # Keep base tendrils, removing any evidence the selection now claims. A
    # tendril that loses all of its evidence disappears (its units are claimed).
    kept_tendrils: list[TendrilCandidate] = []
    for tendril in analysis.tendrils:
        remaining_evidence = [
            fragment
            for fragment in tendril.evidence
            if (fragment.source_message_id, fragment.exact_quote) not in selected_keys
        ]
        if remaining_evidence:
            kept_tendrils.append(
                tendril.model_copy(update={"evidence": remaining_evidence})
            )

    # Demote former base-claim evidence that the selection no longer claims.
    represented_keys = {
        (fragment.source_message_id, fragment.exact_quote)
        for tendril in kept_tendrils
        for fragment in tendril.evidence
    }
    former_claim_evidence = [
        fragment
        for fragment in analysis.claim.matching_fragments
        if (fragment.source_message_id, fragment.exact_quote) not in selected_keys
        and (fragment.source_message_id, fragment.exact_quote) not in represented_keys
    ]
    if former_claim_evidence:
        kept_tendrils.append(
            _former_claim_tendril(
                analysis.claim,
                former_claim_evidence,
                taken_client_ids={tendril.client_id for tendril in kept_tendrils},
            )
        )

    tendril_units = {
        tendril.client_id: {
            unit_id for fragment in tendril.evidence for unit_id in fragment.unit_ids
        }
        for tendril in kept_tendrils
    }

    coverage: list[CoverageEntry] = []
    for entry in analysis.coverage:
        if entry.unit_id in selected_unit_ids:
            coverage.append(
                CoverageEntry(
                    source_message_id=entry.source_message_id,
                    unit_id=entry.unit_id,
                    classification=CoverageClassification.CLAIMED,
                    tendril_client_ids=[],
                    reason="Matched to the selected claim options",
                    confidence=1.0,
                )
            )
            continue
        refs = sorted(
            client_id
            for client_id, unit_ids in tendril_units.items()
            if entry.unit_id in unit_ids
        )
        if refs:
            coverage.append(
                entry.model_copy(
                    update={
                        "classification": CoverageClassification.TENDRIL,
                        "tendril_client_ids": refs,
                    }
                )
            )
        else:
            # Not discovered evidence and not selected: preserve as-is.
            coverage.append(entry.model_copy())

    label = claim_label or join_selected_titles(selected_candidates)
    claim = ClaimAnalysis(
        raw_topic=label,
        normalized_label=label,
        matching_fragments=boundary,
        confidence=1.0,
        rationale=(
            "Authoritative claim boundary from selected options "
            + ", ".join(candidate.candidate_id for candidate in sorted(
                selected_candidates, key=lambda item: item.display_order
            ))
        ),
    )
    return AnalysisResult(
        schema_version=analysis.schema_version,
        claim=claim,
        tendrils=kept_tendrils,
        coverage=coverage,
        unresolved=analysis.unresolved,
    )


def remap_analysis_message_ids(
    analysis: AnalysisResult, message_id_map: dict[str, str]
) -> AnalysisResult:
    payload = deepcopy(analysis.model_dump(mode="python"))

    def map_unit(unit_id: str) -> str:
        source_id, separator, suffix = unit_id.partition(":")
        if not separator or source_id not in message_id_map:
            raise ValueError(f"Cannot remap synthetic source unit: {unit_id}")
        return f"{message_id_map[source_id]}:{suffix}"

    fragments = list(payload["claim"]["matching_fragments"])
    for tendril in payload["tendrils"]:
        fragments.extend(tendril["evidence"])
    for fragment in fragments:
        fragment["source_message_id"] = message_id_map[fragment["source_message_id"]]
        fragment["unit_ids"] = [map_unit(item) for item in fragment["unit_ids"]]
    for entry in payload["coverage"]:
        entry["source_message_id"] = message_id_map[entry["source_message_id"]]
        entry["unit_id"] = map_unit(entry["unit_id"])
    return AnalysisResult.model_validate(payload)
