"""Opt-in, one-call DeepSeek contract check with receipt-only console output."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from nemoir.config import Settings
from nemoir.domain.errors import ProviderOutputValidationError
from nemoir.domain.models import AnalysisRequest, ConversationBundle, SourceMessage
from nemoir.domain.segmentation import segment_messages
from nemoir.domain.validation import validate_analysis
from nemoir.providers.deepseek import DeepSeekAnalysisProvider


async def check(fixture: Path, *, confirm_live: bool) -> int:
    settings = Settings.from_environment()
    if not confirm_live:
        raise SystemExit("Refusing live call without --confirm-live")
    if not settings.allow_live_deepseek:
        raise SystemExit("Refusing live call unless NEMOIR_ALLOW_LIVE_DEEPSEEK=true")
    if not settings.deepseek_api_key:
        raise SystemExit("DEEPSEEK_API_KEY is not configured")

    payload = json.loads(fixture.read_text(encoding="utf-8"))
    messages = [SourceMessage.model_validate(item) for item in payload["messages"]]
    units = segment_messages(messages)
    bundle = ConversationBundle(
        id="opt-in-contract-check",
        guild_id="offline-contract-check",
        intake_channel_id="offline-contract-check",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
        source_messages=messages,
        source_units=units,
    )
    provider = DeepSeekAnalysisProvider(
        api_key=settings.deepseek_api_key,
        model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        allow_live=True,
    )
    try:
        response = await provider.analyse(
            AnalysisRequest(
                bundle_id=bundle.id,
                messages=messages,
                source_units=units,
                raw_claim=payload["claim"],
            )
        )
    except ProviderOutputValidationError as exc:
        print(
            json.dumps(
                {
                    "provider": exc.receipt.provider,
                    "model": exc.receipt.model,
                    "request_id": exc.receipt.request_id,
                    "latency_ms": exc.receipt.latency_ms,
                    "input_tokens": exc.receipt.input_tokens,
                    "output_tokens": exc.receipt.output_tokens,
                    "outcome": exc.receipt.outcome,
                    "output_contract_valid": False,
                    "validation_error_count": exc.validation_error_count,
                },
                indent=2,
            )
        )
        return 1
    report = validate_analysis(bundle, units, response.result)
    print(
        json.dumps(
            {
                "provider": response.receipt.provider,
                "model": response.receipt.model,
                "request_id": response.receipt.request_id,
                "latency_ms": response.receipt.latency_ms,
                "input_tokens": response.receipt.input_tokens,
                "output_tokens": response.receipt.output_tokens,
                "outcome": response.receipt.outcome,
                "output_contract_valid": True,
                "valid": report.valid,
                "needs_review": report.needs_review,
                "error_codes": [item.code for item in report.errors],
                "warning_codes": [item.code for item in report.warnings],
                "coverage_count": len(response.result.coverage),
                "tendril_count": len(response.result.tendrils),
            },
            indent=2,
        )
    )
    return 0 if report.valid else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--confirm-live", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(check(args.fixture, confirm_live=args.confirm_live))


if __name__ == "__main__":
    raise SystemExit(main())
