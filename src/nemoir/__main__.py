"""Small offline operator commands; no live integrations are started implicitly."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from nemoir.runtime import InstanceLock, RuntimeStatusStore, ShutdownRequest
from nemoir.application.analysis_service import AnalysisService
from nemoir.application.authorization import AuthorizationPolicy
from nemoir.application.capture_service import CaptureService
from nemoir.application.claim_options_service import ClaimOptionsService
from nemoir.domain.models import AnalysisResult, SourceMessage
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.fake import (
    FakeAnalysisProvider,
    SyntheticFixtureAnalysisProvider,
    remap_analysis_message_ids,
)


def _fixture_path(name: str) -> Path:
    candidates = [
        Path.cwd() / "tests" / "fixtures" / name,
        Path(__file__).resolve().parents[2] / "tests" / "fixtures" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Synthetic fixture not found: {name}")


async def _demo_offline(database: Path) -> int:
    bundle_payload = json.loads(_fixture_path("synthetic_bundle.json").read_text(encoding="utf-8"))
    analysis = AnalysisResult.model_validate_json(
        _fixture_path("synthetic_analysis.json").read_text(encoding="utf-8")
    )
    messages = [SourceMessage.model_validate(item) for item in bundle_payload["messages"]]
    with SQLiteRepository(database) as repository:
        capture = CaptureService(repository)
        bundle = capture.start_capture(
            guild_id="synthetic-guild",
            intake_channel_id="intake-1",
            submitter_user_id="person-1",
            recipient_user_id="person-2",
        )
        for message in messages:
            capture.capture_message(
                bundle.id,
                actor_user_id=message.author_user_id,
                external_message_id=f"{message.external_message_id}-{bundle.id[-6:]}",
                author_display_name=message.author_display_name,
                channel_id=message.channel_id,
                content=message.content,
                source_url=message.source_url,
                timestamp=message.timestamp,
            )
        capture.seal(bundle.id, actor_user_id="person-1", idempotency_key="demo-seal")
        persisted = repository.get_bundle(bundle.id)
        message_id_map = {
            fixture.external_message_id: stored.external_message_id
            for stored, fixture in zip(persisted.source_messages, messages, strict=True)
        }
        remapped = remap_analysis_message_ids(analysis, message_id_map)
        provider = FakeAnalysisProvider(remapped)
        authorization = AuthorizationPolicy(repository)
        discovery = await ClaimOptionsService(
            repository, provider, authorization
        ).discover(
            bundle.id,
            actor_user_id="person-1",
            idempotency_key="demo-discover",
        )
        if not discovery.candidates:
            raise RuntimeError(f"Claim-option discovery produced no candidates: {discovery.state}")
        ClaimOptionsService(repository, provider, authorization).select(
            bundle.id,
            actor_user_id="person-2",
            option_numbers=[1],
        )
        outcome = await AnalysisService(repository, provider, authorization).analyse(
            bundle.id,
            actor_user_id="person-2",
            idempotency_key="demo-analysis",
        )
        if outcome.review is None:
            raise RuntimeError(f"Offline analysis did not produce a review: {outcome.state}")
        print(
            json.dumps(
                {
                    "fixture_status": bundle_payload["fixture_status"],
                    "status": outcome.state.value,
                    "bundle_id": bundle.id,
                    "messages": len(persisted.source_messages),
                    "units": len(persisted.source_units),
                    "claim": outcome.review.claim.normalized_label,
                    "coverage_entries": len(outcome.review.coverage),
                    "tendrils": [
                        {
                            "id": item.id,
                            "title": item.title,
                            "type": item.type.value,
                            "status": item.status.value,
                        }
                        for item in outcome.review.tendrils
                    ],
                    "provider": outcome.review.receipt.provider,
                },
                indent=2,
            )
        )
    return 0


def _synthetic_pilot_provider() -> SyntheticFixtureAnalysisProvider:
    bundle_payload = json.loads(
        _fixture_path("synthetic_bundle.json").read_text(encoding="utf-8")
    )
    analysis = AnalysisResult.model_validate_json(
        _fixture_path("synthetic_analysis.json").read_text(encoding="utf-8")
    )
    messages = [
        SourceMessage.model_validate(item) for item in bundle_payload["messages"]
    ]
    return SyntheticFixtureAnalysisProvider(
        messages,
        fixture_claim=bundle_payload["claim"],
        result=analysis,
    )


def _export(database: Path, bundle_id: str, output: Path) -> int:
    with SQLiteRepository(database) as repository:
        payload = repository.export_bundle(bundle_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(output)
    return 0


def _list_operations(database: Path) -> int:
    with SQLiteRepository(database) as repository:
        operations = repository.list_external_operations()
    for item in operations:
        slug = item.metadata.get("requested_slug") or "-"
        phase = item.metadata.get("phase") or "-"
        print(
            f"{item.id} | {item.operation_type} | {item.status.value} | "
            f"phase={phase} | slug={slug} | "
            f"tendril={item.tendril_id} | destination={item.external_destination_id or '-'} | "
            f"message={item.external_message_id or '-'} | key={item.idempotency_key}"
        )
    if not operations:
        print("No external operations recorded.")
    return 0


def _reconcile_operation(
    database: Path,
    operation_id: str,
    message_id: str | None,
    channel_id: str | None,
    abandon: bool,
) -> int:
    if abandon and (message_id is not None or channel_id is not None):
        raise SystemExit("--abandon cannot be combined with --channel-id or --message-id")
    with SQLiteRepository(database) as repository:
        operation = repository.get_external_operation_by_id(operation_id)
        if abandon:
            result = repository.reconcile_publish_operation(operation_id, abandon=True)
            print(
                f"Abandoned {result.id}: no external side effect recorded; "
                "the tendril can now be retried."
            )
            return 0
        if operation.operation_type == "habitat_create":
            result = repository.reconcile_publish_operation(
                operation_id,
                external_destination_id=channel_id,
                external_message_id=message_id,
            )
        else:
            result = repository.reconcile_external_operation(
                operation_id, external_message_id=message_id
            )
    print(f"Reconciled {result.id}: {result.status.value}")
    return 0


def _run_bot_process(
    settings,
    repository,
    provider,
    *,
    pilot_mode: bool,
    channel_test: bool = False,
    autonomous_mode: str | None = None,
    autonomous_sort_provider=None,
) -> int:
    """Run a Discord bot process with a single-instance lock and runtime status.

    The lock is OS-released, so a crashed bot never blocks a later start. The
    status store and instance-scoped shutdown request live in the configured
    runtime directory and carry only safe operational data.
    """
    from nemoir.adapters.discord_bot import run_discord_bot

    runtime_dir = settings.runtime_dir
    runtime_dir.mkdir(parents=True, exist_ok=True)
    instance_id = os.getenv("NEMOIR_INSTANCE_ID") or f"pid-{os.getpid()}"

    lock = InstanceLock(runtime_dir / "bot.lock")
    if not lock.acquire():
        raise SystemExit(
            "Another Nemoir bot instance is already running; stop it first "
            "(a stale status file alone never blocks startup)."
        )
    status_store = RuntimeStatusStore(runtime_dir)
    shutdown_request = ShutdownRequest(runtime_dir, instance_id)
    try:
        run_discord_bot(
            settings,
            repository,
            provider,
            pilot_mode=pilot_mode,
            channel_test=channel_test,
            autonomous_mode=autonomous_mode,
            autonomous_sort_provider=autonomous_sort_provider,
            status_store=status_store,
            shutdown_request=shutdown_request,
            instance_id=instance_id,
        )
    finally:
        shutdown_request.clear()
        lock.release()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nemoir")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo-offline", help="Capture and claim the synthetic fixture")
    demo.add_argument("--database", type=Path, default=Path("data/demo.sqlite3"))
    export = subparsers.add_parser("export", help="Export one stored bundle")
    export.add_argument("--database", type=Path, required=True)
    export.add_argument("--bundle-id", required=True)
    export.add_argument("--output", type=Path, required=True)
    ops = subparsers.add_parser(
        "ops",
        help="Inspect or reconcile external operations after an ambiguous failure",
    )
    ops.add_argument("--database", type=Path, required=True)
    ops.add_argument("--list", action="store_true", help="List all external operations")
    ops.add_argument(
        "--reconcile",
        metavar="OPERATION_ID",
        help="Mark an operation COMPLETED after operator confirmation",
    )
    ops.add_argument(
        "--message-id", help="Observed external message ID for a confirmed post"
    )
    ops.add_argument(
        "--channel-id", help="Observed external channel ID for a confirmed publish channel"
    )
    ops.add_argument(
        "--abandon",
        action="store_true",
        help="Confirm no external side effect occurred; discard a reserved publish operation",
    )
    discord = subparsers.add_parser("discord", help="Start the explicitly approved Discord bot")
    discord.add_argument("--confirm-live", action="store_true")
    pilot = subparsers.add_parser(
        "discord-pilot",
        help="Start Discord with the exact synthetic fixture and no DeepSeek access",
    )
    pilot.add_argument("--confirm-live", action="store_true")
    channel_test = subparsers.add_parser(
        "discord-channel-test",
        help=(
            "Start Discord with the synthetic fixture and channel-write permission "
            "(creates real channels in the configured development category)"
        ),
    )
    channel_test.add_argument("--confirm-live", action="store_true")
    autonomous_test = subparsers.add_parser(
        "discord-autonomous-test",
        aliases=["autonomous-test"],
        help=(
            "Start the autonomous workflow with the deterministic synthetic provider "
            "and real Discord channel writes (never reads or calls DeepSeek)"
        ),
    )
    autonomous_test.add_argument("--confirm-live", action="store_true")
    autonomous_live = subparsers.add_parser(
        "discord-autonomous-live",
        aliases=["autonomous-live"],
        help=(
            "Start the autonomous workflow with real DeepSeek sorting and real "
            "Discord channel writes (paid model calls per sealed capture)"
        ),
    )
    autonomous_live.add_argument("--confirm-live", action="store_true")
    subparsers.add_parser("gui", help="Open the Nemoir Control Panel desktop window")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "demo-offline":
        return asyncio.run(_demo_offline(args.database))
    if args.command == "export":
        return _export(args.database, args.bundle_id, args.output)
    if args.command == "ops":
        if args.reconcile and args.list:
            raise SystemExit("Choose either --list or --reconcile, not both")
        if args.reconcile:
            return _reconcile_operation(
                args.database, args.reconcile, args.message_id, args.channel_id, args.abandon
            )
        if args.channel_id or args.abandon or args.message_id:
            raise SystemExit("--channel-id, --message-id and --abandon require --reconcile OPERATION_ID")
        return _list_operations(args.database)
    if args.command == "discord":
        if not args.confirm_live:
            raise SystemExit("Refusing Discord connection without --confirm-live")
        from nemoir.config import Settings
        from nemoir.providers.deepseek import DeepSeekAnalysisProvider

        settings = Settings.from_environment()
        if not settings.allow_live_deepseek:
            raise SystemExit(
                "Discord startup requires NEMOIR_ALLOW_LIVE_DEEPSEEK=true in this build"
            )
        if not settings.deepseek_api_key:
            raise SystemExit("DEEPSEEK_API_KEY is not configured")
        repository = SQLiteRepository(settings.database_path)
        provider = DeepSeekAnalysisProvider(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            allow_live=settings.allow_live_deepseek,
        )
        try:
            return _run_bot_process(settings, repository, provider, pilot_mode=False)
        finally:
            repository.close()
    if args.command == "discord-pilot":
        if not args.confirm_live:
            raise SystemExit("Refusing Discord pilot connection without --confirm-live")
        from nemoir.config import Settings

        settings = Settings.from_environment(include_deepseek=False)
        repository = SQLiteRepository(settings.database_path)
        try:
            return _run_bot_process(
                settings,
                repository,
                _synthetic_pilot_provider(),
                pilot_mode=True,
            )
        finally:
            repository.close()
    if args.command == "discord-channel-test":
        if not args.confirm_live:
            raise SystemExit("Refusing Discord channel-test connection without --confirm-live")
        from nemoir.config import Settings

        settings = Settings.from_environment(include_deepseek=False)
        if not settings.allow_channel_write:
            raise SystemExit(
                "Discord channel-test requires NEMOIR_ALLOW_CHANNEL_WRITE=true; "
                "it creates real Discord channels"
            )
        repository = SQLiteRepository(settings.database_path)
        try:
            return _run_bot_process(
                settings,
                repository,
                _synthetic_pilot_provider(),
                pilot_mode=False,
                channel_test=True,
            )
        finally:
            repository.close()
    if args.command in ("discord-autonomous-test", "autonomous-test"):
        if not args.confirm_live:
            raise SystemExit(
                "Refusing Discord autonomous-test connection without --confirm-live"
            )
        from nemoir.config import Settings
        from nemoir.providers.synthetic import SyntheticAutonomousSortProvider

        settings = Settings.from_environment(include_deepseek=False)
        if not settings.allow_channel_write:
            raise SystemExit(
                "Discord autonomous-test requires NEMOIR_ALLOW_CHANNEL_WRITE=true; "
                "sealed captures publish into real Discord channels"
            )
        repository = SQLiteRepository(settings.database_path)
        try:
            return _run_bot_process(
                settings,
                repository,
                _synthetic_pilot_provider(),
                pilot_mode=False,
                autonomous_mode="test",
                autonomous_sort_provider=SyntheticAutonomousSortProvider(),
            )
        finally:
            repository.close()
    if args.command in ("discord-autonomous-live", "autonomous-live"):
        if not args.confirm_live:
            raise SystemExit(
                "Refusing Discord autonomous-live connection without --confirm-live"
            )
        from nemoir.config import Settings
        from nemoir.providers.deepseek import DeepSeekAnalysisProvider

        settings = Settings.from_environment()
        if not settings.allow_live_deepseek:
            raise SystemExit(
                "Discord autonomous-live requires NEMOIR_ALLOW_LIVE_DEEPSEEK=true "
                "(paid model calls per sealed capture)"
            )
        if not settings.deepseek_api_key:
            raise SystemExit("DEEPSEEK_API_KEY is not configured")
        if not settings.allow_channel_write:
            raise SystemExit(
                "Discord autonomous-live requires NEMOIR_ALLOW_CHANNEL_WRITE=true; "
                "sealed captures publish into real Discord channels"
            )
        repository = SQLiteRepository(settings.database_path)
        provider = DeepSeekAnalysisProvider(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            allow_live=settings.allow_live_deepseek,
        )
        try:
            return _run_bot_process(
                settings,
                repository,
                provider,
                pilot_mode=False,
                autonomous_mode="live",
                autonomous_sort_provider=provider,
            )
        finally:
            repository.close()
    if args.command == "gui":
        from nemoir.gui.panel import run_gui

        return run_gui()
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
