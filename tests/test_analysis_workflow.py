from __future__ import annotations

import json

import pytest

from nemoir.application.analysis_service import AnalysisService
from nemoir.domain.errors import ProviderOutputValidationError
from nemoir.domain.models import ProviderReceipt
from nemoir.domain.states import BundleState, ClaimStatus
from nemoir.providers.fake import FakeAnalysisProvider


@pytest.mark.asyncio
async def test_valid_analysis_persists_review_and_is_idempotent(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    provider = FakeAnalysisProvider(synthetic_analysis)
    service = AnalysisService(repository, provider, authorization)
    outcome = await service.analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="analysis-valid-1",
    )
    assert outcome.state == BundleState.REVIEW_READY
    assert outcome.validation.valid
    assert outcome.review is not None
    assert len(outcome.review.tendrils) == 5
    assert len(outcome.review.coverage) == 7
    assert provider.call_count == 1
    assert repository.get_claim_for_bundle(captured_bundle.id).status == ClaimStatus.MATCHED

    repeated = await service.analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="analysis-valid-1",
    )
    assert repeated.review is not None
    assert repeated.replayed is True
    assert outcome.replayed is False
    assert provider.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["invented_quote", "unknown_message", "missing_coverage"])
async def test_invalid_provider_results_fail_closed(
    repository, captured_bundle, synthetic_analysis, variant, authorization
) -> None:
    service = AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis, variant=variant), authorization
    )
    outcome = await service.analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key=f"analysis-{variant}",
    )
    assert outcome.state == BundleState.ANALYSIS_FAILED
    assert not outcome.validation.valid
    assert not repository.list_tendrils(bundle_id=captured_bundle.id)


@pytest.mark.asyncio
async def test_claim_leakage_is_reviewable_not_silently_accepted(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    service = AnalysisService(
        repository,
        FakeAnalysisProvider(synthetic_analysis, variant="claim_leakage"),
        authorization,
    )
    outcome = await service.analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="analysis-leakage",
    )
    assert outcome.state == BundleState.NEEDS_REVIEW
    assert outcome.validation.valid
    assert outcome.validation.needs_review
    assert not repository.list_tendrils(bundle_id=captured_bundle.id)


@pytest.mark.asyncio
async def test_provider_failure_remains_recoverable(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    service = AnalysisService(
        repository,
        FakeAnalysisProvider(synthetic_analysis, variant="provider_failure"),
        authorization,
    )
    outcome = await service.analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="analysis-provider-failure",
    )
    assert outcome.state == BundleState.ANALYSIS_FAILED
    assert repository.get_bundle(captured_bundle.id).status == BundleState.ANALYSIS_FAILED


@pytest.mark.asyncio
async def test_invalid_provider_output_persists_receipt_and_raw_response(
    repository, captured_bundle, authorization
) -> None:
    class InvalidOutputProvider:
        async def analyse(self, request):
            raise ProviderOutputValidationError(
                receipt=ProviderReceipt(
                    provider="deepseek",
                    model="deepseek-test",
                    latency_ms=42,
                    input_tokens=100,
                    output_tokens=200,
                    outcome="invalid_response",
                    request_id="request-invalid-1",
                ),
                raw_response='{"invalid":true}',
                validation_error_count=3,
            )

    service = AnalysisService(repository, InvalidOutputProvider(), authorization)
    outcome = await service.analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="analysis-invalid-provider-output",
    )

    assert outcome.state == BundleState.ANALYSIS_FAILED
    assert [item.code for item in outcome.validation.errors] == ["PROVIDER_OUTPUT_INVALID"]
    attempt = repository.get_analysis_attempt(outcome.attempt_id)
    assert attempt["provider"] == "deepseek"
    assert attempt["model"] == "deepseek-test"
    assert attempt["raw_response"] == '{"invalid":true}'
    receipt_row = repository._connection.execute(
        "SELECT receipt_json FROM provider_receipts WHERE attempt_id = ?",
        (outcome.attempt_id,),
    ).fetchone()
    assert receipt_row is not None
    assert json.loads(receipt_row["receipt_json"])["output_tokens"] == 200


@pytest.mark.asyncio
async def test_claim_exists_before_provider_call(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    class InspectingProvider(FakeAnalysisProvider):
        async def analyse(self, request):
            persisted = repository.get_claim_for_bundle(request.bundle_id)
            assert persisted.raw_topic == request.raw_claim
            return await super().analyse(request)

    service = AnalysisService(repository, InspectingProvider(synthetic_analysis), authorization)
    await service.analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="analysis-ordering",
    )


@pytest.mark.asyncio
async def test_export_omits_raw_provider_response(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    service = AnalysisService(repository, FakeAnalysisProvider(synthetic_analysis), authorization)
    await service.analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="analysis-export",
    )
    exported = repository.export_bundle(captured_bundle.id)
    encoded = json.dumps(exported)
    assert exported["schema_version"] == "1.0"
    assert len(exported["tendrils"]) == 5
    assert "raw_response" not in encoded
