"""Thin discord.py adapter over tested platform-neutral application services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import logging
import os
from typing import Any

from nemoir.runtime import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    RuntimeStatusStore,
    ShutdownRequest,
)

from nemoir.adapters.channel_naming import sanitize_channel_slug
from nemoir.adapters.pagination import (
    DISCORD_MESSAGE_LIMIT,
    bounded_page,
    discord_text_units,
    paginate_text,
)
from nemoir.application.analysis_service import AnalysisService
from nemoir.application.autonomous_service import AutonomousJobService
from nemoir.application.authorization import AuthorizationPolicy
from nemoir.application.capture_service import CaptureService
from nemoir.application.claim_options_service import ClaimOptionsService, parse_option_numbers
from nemoir.application.prompt_service import PromptService
from nemoir.application.publishing_service import (
    PublishPreview,
    PublishOutcome,
    PublishingService,
)
from nemoir.application.resurfacing_service import ResurfacingService
from nemoir.application.routing_service import RoutingService
from nemoir.config import Settings
from nemoir.domain.errors import AuthorizationError, ConflictError, NemoirError, NotFoundError
from nemoir.domain.models import (
    ClaimCandidate,
    ConversationBundle,
    ProviderReceipt,
    Tendril,
)
from nemoir.domain.states import (
    BundleState,
    ExternalOperationStatus,
    PromptAttemptStatus,
    TendrilState,
)
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.base import AnalysisProvider, AutonomousSortProvider

logger = logging.getLogger(__name__)

try:  # Optional dependency: helpers remain importable without discord.py.
    import discord
    from discord import app_commands
    from discord.ext import commands
except ImportError:  # pragma: no cover - exercised in minimal installations
    discord = None
    app_commands = None
    commands = None


@dataclass(frozen=True)
class DiscordScopePolicy:
    guild_id: str
    intake_channel_id: str
    anemone_category_id: str
    nursery_channel_id: str
    additional_intake_channel_ids: frozenset[str] = frozenset()

    @property
    def intake_channel_ids(self) -> frozenset[str]:
        return frozenset({self.intake_channel_id, *self.additional_intake_channel_ids})

    def require_guild(self, guild_id: str | int | None) -> None:
        if guild_id is None or str(guild_id) != self.guild_id:
            raise PermissionError("Nemoir is restricted to its configured development guild")

    def require_intake(self, guild_id: str | int | None, channel_id: str | int | None) -> None:
        self.require_guild(guild_id)
        if channel_id is None or str(channel_id) not in self.intake_channel_ids:
            raise PermissionError("This command is restricted to the configured intake channels")

    def require_anemone_destination(
        self,
        guild_id: str | int | None,
        category_id: str | int | None,
    ) -> None:
        self.require_guild(guild_id)
        if category_id is None or str(category_id) != self.anemone_category_id:
            raise PermissionError("Destination must be inside the configured anemone category")


def format_tendril(tendril: Tendril, repository: SQLiteRepository) -> str:
    bundle = repository.get_bundle(tendril.bundle_id)
    messages = {item.external_message_id: item for item in bundle.source_messages}
    lines = [
        f"**{tendril.title}** (`{tendril.id}`)",
        f"{tendril.description}",
        f"Type: `{tendril.type.value}` | State: `{tendril.status.value}` | Confidence: {tendril.confidence:.2f}",
        "**Evidence**",
    ]
    for fragment in tendril.evidence:
        message = messages.get(fragment.source_message_id)
        source = f" [source]({message.source_url})" if message else ""
        lines.append(f"> {fragment.exact_quote}{source}")
    lines.append(f"Open because: {tendril.why_open}")
    return "\n".join(lines)


def format_claim_options(
    bundle: ConversationBundle,
    candidates: list[ClaimCandidate],
) -> str:
    """Render the compact claim-option menu with exact source-linked evidence.

    Titles and summaries are Nemoir interpretations and are labelled as such;
    the quoted fragments are exact source text with a jump link per source
    message. Callers paginate the returned text; nothing here truncates it.
    """
    messages = {item.external_message_id: item for item in bundle.source_messages}
    lines = [
        f"<@{bundle.recipient_user_id}> bundle `{bundle.id}` is sealed. "
        "Claim options (titles are Nemoir interpretations; evidence is exact source):"
    ]
    for candidate in candidates:
        lines.append(f"**{candidate.display_order}. {candidate.title}**")
        lines.append(candidate.summary)
        for fragment in candidate.evidence:
            message = messages.get(fragment.source_message_id)
            source = f" [source]({message.source_url})" if message else ""
            lines.append(f"> {fragment.exact_quote}{source}")
    lines.append(
        'Select with `/claim options:"<n>"` (comma-separate several, e.g. `/claim options:"2,3"`) '
        'or supply a custom topic with `/claim topic:"..."`.'
    )
    return "\n".join(lines)


def _claim_options_failure_text(bundle_id: str) -> str:
    return (
        f"Bundle `{bundle_id}` is sealed, but claim options could not be generated. "
        "Use `/claim topic:\"...\"` with a custom topic, or retry discovery with "
        f"`/claim-options-retry bundle_id:{bundle_id}`."
    )


PROMPT_FAILURE_TEXT = "Nemoir could not answer that question. Please try again."


def format_prompt_response(answer: str, receipt: ProviderReceipt | None) -> str:
    """Label an ordinary prompt answer as a Nemoir/AI response on its first page.

    The label reflects the actual provider (DeepSeek in live mode, the
    deterministic synthetic provider in pilot mode) so the first page is never
    ambiguous about who produced the text. The answer itself is untrusted
    display content and is returned verbatim for the caller to paginate.
    """
    if receipt is not None and receipt.provider == "deepseek":
        label = "**Nemoir · DeepSeek response**"
    elif receipt is not None:
        label = f"**Nemoir response** (`{receipt.provider}`)"
    else:
        label = "**Nemoir response**"
    return f"{label}\n{answer}"


def _truncate_to_units(text: str, max_units: int) -> str:
    """Return the longest prefix of ``text`` of at most ``max_units`` UTF-16 units."""
    if max_units <= 0:
        return ""
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > max_units:
            return text[:index]
    return text


class _MentionTarget:
    """Minimal snowflake-like object for ``AllowedMentions(users=[...])``.

    Real Discord user IDs are numeric and stay numeric; non-numeric IDs (used
    by fake-adapter tests) pass through unchanged so the mention payload never
    breaks. Only the deliberate author target is ever listed.
    """

    __slots__ = ("id",)

    def __init__(self, value: str) -> None:
        self.id: int | str = int(value) if value.isdigit() else value


def format_routed_summary(
    tendril: Tendril,
    *,
    limit: int = DISCORD_MESSAGE_LIMIT,
) -> str:
    """Bounded single-message summary for a routed tendril post.

    Routed posts are tracked and reconciled through exactly one external
    message ID, so they are never split into several untracked messages.
    When the full evidence text cannot fit inside the platform limit, this
    summary carries a bounded, explicitly marked preview and the complete
    evidence text travels as a file attachment on the same message. The
    summary always fits inside ``limit`` (measured in UTF-16 code units) and
    never discards text silently.
    """
    note = "\nFull evidence with source links is attached (`tendril.txt`)."
    budget = limit - discord_text_units(note)
    if budget < 64:
        raise ValueError("Cannot fit a routed summary inside the platform limit")
    parts = [
        f"**{tendril.title}** (`{tendril.id}`)",
        tendril.description,
        (
            f"Type: `{tendril.type.value}` | State: `{tendril.status.value}` | "
            f"Confidence: {tendril.confidence:.2f}"
        ),
        f"Open because: {tendril.why_open}",
    ]
    lines: list[str] = []
    used = 0
    for part in parts:
        prefix = "\n" if lines else ""
        prefix_units = discord_text_units(prefix)
        part_units = discord_text_units(part)
        if used + prefix_units + part_units <= budget:
            lines.append(part)
            used += prefix_units + part_units
            continue
        room = budget - used - prefix_units
        if room > 1 and part:
            lines.append(_truncate_to_units(part, room - 1) + "…")
        break
    return "\n".join(lines) + note


async def publish_tendril_message(channel: Any, tendril: Tendril, repository: SQLiteRepository) -> Any:
    """Send exactly one tracked Discord message carrying the complete evidence.

    Short tendrils are posted in full. Longer tendrils post a bounded summary
    with the exact evidence text attached as a file on the same message, so a
    single external message ID represents the whole routed tendril for
    routing, habitat creation, and reconciliation. The limit is measured in
    UTF-16 code units, so astral characters (most emoji) cannot overflow a
    message that Python ``len()`` would report as fitting.
    """
    full = format_tendril(tendril, repository)
    # Tendril titles, descriptions, and evidence are model-derived content: they
    # must never ping @everyone/@here/<@user>/<@&role>. Legitimate workflow
    # mentions (for example the recipient mention in the sealed menu) live on
    # other, non-published messages and are unaffected.
    no_mentions = discord.AllowedMentions.none()
    if discord_text_units(full) <= DISCORD_MESSAGE_LIMIT:
        return await channel.send(full, allowed_mentions=no_mentions)
    summary = format_routed_summary(tendril)
    attachment = discord.File(io.BytesIO(full.encode("utf-8")), filename="tendril.txt")
    return await channel.send(summary, file=attachment, allowed_mentions=no_mentions)


def format_publish_preview(preview: PublishPreview) -> str:
    """Render the read-only channel-split preview (no channels are created).

    Items are grouped by the planned action so an already-routed or terminal
    tendril is never presented as a channel that will definitely be created.
    """
    lines = [f"Publish preview for bundle `{preview.bundle_id}`:"]
    if not preview.items:
        lines.append("No tendrils to publish.")

    def render(item: Any) -> str:
        return (
            f"`{item.tendril_id}` **{item.title}** · `{item.type}` · "
            f"{item.evidence_count} evidence"
        )

    for item in preview.of_status("will_publish"):
        lines.append(f"- {render(item)} · will publish · proposed channel `{item.channel_slug}`")
    for item in preview.of_status("already_published"):
        target = f"<#{item.channel_id}>" if item.channel_id else "an existing channel"
        lines.append(f"- {render(item)} · already published in {target}")
    for item in preview.of_status("skipped"):
        lines.append(f"- {render(item)} · will be skipped ({item.reason})")
    for item in preview.of_status("reconciliation_required"):
        lines.append(f"- {render(item)} · requires reconciliation ({item.reason})")

    lines.append(
        "Proposed channel names are base names and may receive a collision suffix "
        "when execution inspects the real category. "
        "Run `/publish-bundle bundle_id:<id> confirm:true` to create the channels."
    )
    return "\n".join(lines)


def format_publish_report(outcome: PublishOutcome) -> str:
    """Render a compact, grouped publish report across all item statuses."""
    labels = {
        "published": "Published",
        "already_published": "Already published",
        "skipped": "Skipped",
        "reconciliation_required": "Reconciliation required",
        "failed": "Failed",
    }
    lines = [f"Publish report for bundle `{outcome.bundle_id}`:"]
    for status, label in labels.items():
        items = outcome.of_status(status)
        lines.append(f"**{label}: {len(items)}**")
        for item in items:
            detail = f"`{item.tendril_id}` {item.title}"
            if item.channel_slug:
                detail += f" · channel `{item.channel_slug}`"
            if item.channel_id:
                detail += f" · <#{item.channel_id}>"
            if item.reason:
                detail += f" · {item.reason}"
            lines.append(f"- {detail}")
    return "\n".join(lines)


def _require_discord_settings(settings: Settings) -> DiscordScopePolicy:
    missing = [
        name
        for name, value in {
            "NEMOIR_GUILD_ID": settings.guild_id,
            "NEMOIR_INTAKE_CHANNEL_ID": settings.intake_channel_id,
            "NEMOIR_ANEMONE_CATEGORY_ID": settings.anemone_category_id,
            "NEMOIR_NURSERY_CHANNEL_ID": settings.nursery_channel_id,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"Missing Discord scope configuration: {', '.join(missing)}")
    return DiscordScopePolicy(
        guild_id=settings.guild_id or "",
        intake_channel_id=settings.intake_channel_id or "",
        anemone_category_id=settings.anemone_category_id or "",
        nursery_channel_id=settings.nursery_channel_id or "",
        additional_intake_channel_ids=frozenset(
            settings.additional_intake_channel_ids
        ),
    )


def create_discord_bot(
    settings: Settings,
    repository: SQLiteRepository,
    provider: AnalysisProvider,
    *,
    pilot_mode: bool = False,
    channel_test: bool = False,
    autonomous_mode: str | None = None,
    autonomous_sort_provider: AutonomousSortProvider | None = None,
    status_store: RuntimeStatusStore | None = None,
    shutdown_request: ShutdownRequest | None = None,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> Any:
    """Construct the bot without connecting it; importing discord.py is optional.

    ``autonomous_mode`` is one of ``"test"`` or ``"live"``: the recipient-free
    autonomous workflow (recipient-free ``/tend``, queued ``/seal``, background
    sort and publish) is only registered and only ever processed in these
    modes, so ordinary synthetic/channel-test/live modes can never trigger
    autonomous sorting or publishing. Autonomous Test uses the deterministic
    synthetic ``autonomous_sort_provider`` (never DeepSeek); Autonomous Live
    uses the gated DeepSeek sort method.

    When ``status_store`` is supplied the bot records its runtime status
    (ONLINE on ``on_ready``, RECONNECTING on disconnect, ONLINE on resume,
    ERROR with a safe class on event errors) and runs a heartbeat loop that
    also honours an instance-scoped ``shutdown_request`` for graceful stop.
    Without a store the adapter behaves exactly as before, so the existing
    fake-adapter tests remain unaffected.
    """
    if discord is None or app_commands is None or commands is None:
        raise RuntimeError("Install Nemoir with the 'discord' optional dependency")
    if autonomous_mode not in (None, "test", "live"):
        raise ValueError("autonomous_mode must be None, 'test', or 'live'")
    autonomous_enabled = autonomous_mode is not None
    if autonomous_enabled and autonomous_sort_provider is None:
        raise ValueError("An autonomous mode requires an autonomous sort provider")

    scope = _require_discord_settings(settings)
    authorization = AuthorizationPolicy(repository, settings.admin_user_ids)
    capture = CaptureService(repository, settings.admin_user_ids)
    analysis = AnalysisService(repository, provider, authorization)
    claim_options_service = ClaimOptionsService(repository, provider, authorization)
    resurfacing = ResurfacingService(repository, authorization)
    prompt_service = PromptService(
        repository,
        provider,  # the adapter also implements the PromptProvider contract
        max_input_chars=settings.prompt_max_input_chars,
        max_output_tokens=settings.prompt_max_output_tokens,
    )
    intents = discord.Intents.none()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
    background_tasks: set[asyncio.Task[Any]] = set()
    synced = False
    heartbeat_task: asyncio.Task[Any] | None = None

    async def heartbeat_loop() -> None:
        while True:
            if status_store is not None:
                status_store.record_heartbeat()
            if shutdown_request is not None and shutdown_request.is_requested():
                if status_store is not None:
                    status_store.record_stopping()
                shutdown_request.clear()
                await bot.close()
                return
            await asyncio.sleep(heartbeat_interval)

    def remember(task: asyncio.Task[Any]) -> None:
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    async def reject(interaction: discord.Interaction, error: Exception) -> None:
        text = f"Nemoir refused that operation: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def prompt_error(interaction: discord.Interaction, text: str) -> None:
        """Post a concise, non-leaking failure for the prompt command."""
        if interaction.response.is_done():
            await interaction.followup.send(text)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def post_analysis(bundle_id: str, actor_user_id: str, interaction_id: str) -> None:
        try:
            outcome = await analysis.analyse(
                bundle_id,
                actor_user_id=actor_user_id,
                idempotency_key=f"discord-analysis:{interaction_id}",
            )
            if outcome.replayed:
                # A duplicate interaction with the same id: the original
                # interaction owns the provider call and the posting, so no
                # second review (or spurious failure notice) is emitted.
                return
            channel = bot.get_channel(int(scope.nursery_channel_id))
            if channel is None:
                raise RuntimeError("Configured nursery channel is unavailable")
            if outcome.review is None:
                text = (
                    f"Bundle `{bundle_id}` requires review: `{outcome.state.value}`. "
                    f"Validation errors: {len(outcome.validation.errors)}; "
                    f"warnings: {len(outcome.validation.warnings)}."
                )
                for page in paginate_text(text):
                    await channel.send(page)
                return
            summary = (
                f"**Nemoir review ready** for bundle `{bundle_id}`\n"
                f"Claimed: **{outcome.review.claim.normalized_label}**\n"
                f"Coverage: {len(outcome.review.coverage)} units | "
                f"Tendrils: {len(outcome.review.tendrils)}"
            )
            for page in paginate_text(summary):
                await channel.send(page)
            for tendril in outcome.review.tendrils:
                for page in paginate_text(format_tendril(tendril, repository)):
                    await channel.send(page)
        except Exception as exc:  # background boundary: retain a visible failure
            channel = bot.get_channel(int(scope.nursery_channel_id))
            if channel is not None:
                await channel.send(
                    f"Nemoir analysis failed for bundle `{bundle_id}`: {type(exc).__name__}. "
                    "Use `/bundle-status` and inspect local logs; source content was not echoed."
                )

    class DiscordPublisher:
        platform = "discord"

        async def publish(self, tendril: Tendril, external_destination_id: str) -> str | None:
            channel = bot.get_channel(int(external_destination_id))
            if channel is None:
                raise NotFoundError("Discord destination channel is unavailable")
            message = await publish_tendril_message(channel, tendril, repository)
            return str(message.id)

        async def list_channel_names(self, category_id: str) -> set[str]:
            category = bot.get_channel(int(category_id))
            if category is None or not hasattr(category, "text_channels"):
                raise NotFoundError("Configured anemone category is unavailable or is not a category")
            return {item.name for item in category.text_channels}

        async def create_channel(self, slug: str, category_id: str) -> str:
            category = bot.get_channel(int(category_id))
            if category is None or not hasattr(category, "create_text_channel"):
                raise NotFoundError("Configured anemone category is unavailable or is not a category")
            channel = await category.create_text_channel(
                slug, reason=f"Nemoir tendril channel ({slug})"
            )
            return str(channel.id)

    publisher = DiscordPublisher()
    routing = RoutingService(repository, publisher, authorization)
    publishing = PublishingService(repository, authorization, publisher, publisher)

    # -- autonomous workflow (test/live modes only) -----------------------
    autonomous_jobs: AutonomousJobService | None = None
    if autonomous_enabled:
        autonomous_jobs = AutonomousJobService(
            repository,
            autonomous_sort_provider,  # type: ignore[arg-type]
            publishing,
            authorization,
            settings.admin_user_ids,
        )

        async def notify_author(bundle: ConversationBundle, text: str) -> None:
            """Notify the sealing user with only the deliberate author mention.

            The nursery channel is preferred (existing review traffic lives
            there) with the intake channel as fallback, so a user who walked
            away never has to keep an interaction open. Only the author's
            mention may ping; model-derived titles and text never can.
            """
            author_mentions = discord.AllowedMentions(
                everyone=False,
                users=[_MentionTarget(bundle.submitter_user_id)],
                roles=False,
                replied_user=False,
            )
            target = bot.get_channel(int(scope.nursery_channel_id))
            if target is None:
                target = bot.get_channel(int(bundle.intake_channel_id))
            if target is None:
                logger.warning(
                    "No notification channel available for bundle %s", bundle.id
                )
                return
            # Include the deliberate mention in the content (permission alone
            # does not notify anyone). Only the first page needs the mention;
            # model-derived text remains restricted by the explicit allowlist.
            pages = paginate_text(f"<@{bundle.submitter_user_id}> {text}")
            for page in pages:
                await target.send(page, allowed_mentions=author_mentions)

        # Bounded concurrency: one seal can never launch an uncontrolled number
        # of paid provider requests, and a job is processed at most once at a
        # time (the durable attempt row enforces that across processes too).
        semaphore = asyncio.Semaphore(max(1, settings.autonomous_max_concurrent))
        processing: set[str] = set()

        async def process_autonomous_bundle(job_bundle_id: str) -> None:
            """Advance one job one phase; exceptions are contained, never raised."""
            try:
                async with semaphore:
                    await autonomous_jobs.process_job(
                        job_bundle_id,
                        guild_id=scope.guild_id,
                        category_id=scope.anemone_category_id,
                        allow_channel_write=settings.allow_channel_write,
                        notify=notify_author,
                    )
            except Exception:
                # Background exceptions are contained and reported, never
                # allowed to kill the Discord bot.
                logger.exception(
                    "Autonomous job processing failed for bundle %s", job_bundle_id
                )
            finally:
                processing.discard(job_bundle_id)

        async def autonomous_worker_loop() -> None:
            while True:
                if shutdown_request is not None and shutdown_request.is_requested():
                    return
                try:
                    for job in autonomous_jobs.actionable_jobs():
                        if job.bundle_id in processing:
                            continue
                        processing.add(job.bundle_id)
                        remember(asyncio.create_task(process_autonomous_bundle(job.bundle_id)))
                except Exception:
                    logger.exception("Autonomous worker scan failed")
                await asyncio.sleep(max(0.1, settings.autonomous_poll_seconds))

        # Exposed for the worker loop and for fake-adapter tests.
        bot.process_autonomous_bundle = process_autonomous_bundle  # type: ignore[attr-defined]

    @bot.event
    async def on_ready() -> None:
        nonlocal synced, heartbeat_task
        # ``on_ready`` is Discord's real READY signal: only record ONLINE here,
        # never during construction, so the status boundary is truthful.
        if status_store is not None:
            status_store.record_online()
        if heartbeat_task is None and status_store is not None:
            heartbeat_task = asyncio.create_task(heartbeat_loop())
        if not synced:
            guild = discord.Object(id=int(scope.guild_id))
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            synced = True
        if autonomous_enabled and not any(
            getattr(task, "_nemoir_autonomous_worker", False)
            for task in background_tasks
        ):
            worker = asyncio.create_task(autonomous_worker_loop())
            worker._nemoir_autonomous_worker = True  # type: ignore[attr-defined]
            remember(worker)

    @bot.event
    async def on_disconnect() -> None:
        if status_store is not None:
            status_store.record_reconnecting()

    @bot.event
    async def on_resumed() -> None:
        if status_store is not None:
            status_store.record_online()

    @bot.event
    async def on_error(event_method: Any, *_args: Any, **_kwargs: Any) -> None:
        if status_store is not None:
            name = getattr(event_method, "__name__", "discord_event")
            status_store.record_error(f"DISCORD_EVENT:{name}")

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        try:
            scope.require_intake(message.guild.id, message.channel.id)
        except PermissionError:
            return
        bundle = repository.find_open_capture(str(message.author.id), str(message.channel.id))
        if bundle is not None and message.content:
            try:
                capture.capture_message(
                    bundle.id,
                    actor_user_id=str(message.author.id),
                    external_message_id=str(message.id),
                    author_display_name=message.author.display_name,
                    channel_id=str(message.channel.id),
                    content=message.content,
                    source_url=message.jump_url,
                    timestamp=message.created_at,
                )
            except NemoirError:
                pass
        await bot.process_commands(message)

    @bot.tree.command(name="tend", description="Start a deliberate multi-message capture")
    @app_commands.describe(
        recipient=(
            "Person who receives first right of selection; omit to capture your "
            "own messages autonomously (Autonomous Test/Live modes only)"
        )
    )
    async def tend(
        interaction: discord.Interaction,
        recipient: discord.Member | None = None,
    ) -> None:
        try:
            scope.require_intake(interaction.guild_id, interaction.channel_id)
            actor = str(interaction.user.id)
            autonomous = recipient is None
            if autonomous and not autonomous_enabled:
                raise PermissionError(
                    "Recipient-free capture requires Autonomous Test or Autonomous "
                    "Live mode; ordinary modes keep the legacy recipient workflow"
                )
            # A duplicate Discord delivery with the same interaction id replays
            # the original capture instead of creating a second session.
            prior = repository.find_lifecycle_event(
                "bundle", f"discord-tend:{interaction.id}"
            )
            if prior is not None:
                bundle = repository.get_bundle(prior["entity_id"])
                await interaction.response.send_message(
                    f"Capture `{bundle.id}` is already open. Post messages here, "
                    "then use `/seal`.",
                    ephemeral=True,
                )
                return
            bundle = capture.start_capture(
                guild_id=str(interaction.guild_id),
                intake_channel_id=str(interaction.channel_id),
                submitter_user_id=actor,
                recipient_user_id=str(recipient.id) if recipient else actor,
                autonomous=autonomous,
                idempotency_key=f"discord-tend:{interaction.id}",
            )
            if autonomous:
                text = (
                    f"Autonomous capture `{bundle.id}` opened. Post your messages "
                    "here, then use `/seal` to queue automatic sorting and "
                    "publishing; you can walk away after sealing."
                )
            else:
                text = (
                    f"Capture `{bundle.id}` opened for {recipient.mention}. "
                    "Post messages here, then use `/seal`."
                )
            await interaction.response.send_message(text, ephemeral=True)
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(
        name="seal",
        description="Seal your active capture, then queue autonomous sorting or claim options",
    )
    async def seal(interaction: discord.Interaction) -> None:
        try:
            scope.require_intake(interaction.guild_id, interaction.channel_id)
            actor = str(interaction.user.id)
            bundle = repository.find_open_capture(actor, str(interaction.channel_id))
            replayed_delivery = False
            if bundle is None:
                # A duplicate delivery after the seal already completed: replay
                # the original seal acknowledgement instead of erroring.
                prior = repository.find_lifecycle_event(
                    "bundle", f"discord-seal:{interaction.id}:sealed"
                )
                if prior is not None:
                    bundle = repository.get_bundle(prior["entity_id"])
                    replayed_delivery = True
                else:
                    raise NotFoundError("You have no open capture in this channel")

            if bundle.autonomous_mode:
                if not autonomous_enabled:
                    raise PermissionError(
                        "Autonomous bundles require an Autonomous Test or Live bot"
                    )
                if bundle.status == BundleState.CAPTURING:
                    capture.seal(
                        bundle.id,
                        actor_user_id=actor,
                        idempotency_key=f"discord-seal:{interaction.id}",
                    )
                # Queue the durable background job. Duplicate events replay the
                # existing job, so this can never create a second job or a
                # second provider attempt. The acknowledgement is immediate:
                # /seal never waits for sorting or channel creation.
                outcome = autonomous_jobs.queue(
                    bundle.id,
                    actor_user_id=actor,
                    idempotency_key=f"discord-autonomous-seal:{interaction.id}",
                    pending_attempt_key=f"autonomous-attempt:{interaction.id}",
                )
                phase_text = outcome.job.phase.value.replace("_", " ").lower()
                if outcome.created:
                    text = (
                        f"Bundle `{bundle.id}` sealed and queued. Autonomous "
                        "sorting and publishing will run in the background; you "
                        "can walk away. A completion notice will mention you."
                    )
                else:
                    text = (
                        f"Bundle `{bundle.id}` is already sealed and queued "
                        f"(job {phase_text}); no second request was started."
                    )
                await interaction.response.send_message(text, ephemeral=True)
                return

            if replayed_delivery:
                # Legacy manual duplicate: acknowledge without re-running
                # discovery or leaving a dangling deferred interaction.
                await interaction.response.send_message(
                    f"Bundle `{bundle.id}` is already sealed; claim options were "
                    "already generated (or use `/claim-options` to re-display them).",
                    ephemeral=True,
                )
                return

            # Legacy manual flow: seal, generate claim options, alert the
            # recipient. Discovery may take tens of seconds with a live
            # provider, so acknowledge immediately.
            await interaction.response.defer()
            capture.seal(
                bundle.id,
                actor_user_id=actor,
                idempotency_key=f"discord-seal:{interaction.id}",
            )
            outcome = await claim_options_service.discover(
                bundle.id,
                actor_user_id=actor,
                idempotency_key=f"discord-claim-options:{interaction.id}",
            )
            if outcome.replayed:
                # A duplicate interaction owns no new provider call and posts
                # nothing; the original interaction already delivered the menu.
                return
            sealed = repository.get_bundle(bundle.id)
            if outcome.candidates:
                pages = paginate_text(format_claim_options(sealed, outcome.candidates))
                await interaction.followup.send(pages[0])
                for page in pages[1:]:
                    await interaction.followup.send(page)
            else:
                await interaction.followup.send(_claim_options_failure_text(sealed.id))
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(name="prompt", description="Ask Nemoir one ordinary question (single turn)")
    @app_commands.describe(question="A single question; no conversation memory is kept")
    async def prompt_command(interaction: discord.Interaction, question: str) -> None:
        try:
            scope.require_intake(interaction.guild_id, interaction.channel_id)
        except PermissionError as exc:
            await interaction.response.send_message(
                f"Nemoir refused that operation: {exc}", ephemeral=True
            )
            return

        cleaned = (question or "").strip()
        if not cleaned:
            await interaction.response.send_message(
                "Question must not be empty.", ephemeral=True
            )
            return
        if len(cleaned) > settings.prompt_max_input_chars:
            await interaction.response.send_message(
                f"Question is too long; the maximum is "
                f"{settings.prompt_max_input_chars} characters.",
                ephemeral=True,
            )
            return

        # Acknowledge immediately: the model call can exceed Discord's
        # interaction window, so defer before awaiting the provider.
        await interaction.response.defer()
        try:
            outcome = await prompt_service.prompt(
                user_id=str(interaction.user.id),
                guild_id=str(interaction.guild_id),
                channel_id=str(interaction.channel_id),
                question=cleaned,
                idempotency_key=f"discord-prompt:{interaction.id}",
            )
        except ConflictError:
            await prompt_error(
                interaction,
                "You already have a prompt in progress; wait for it to finish.",
            )
            return
        except Exception:
            logger.exception("prompt command failed")
            await prompt_error(interaction, PROMPT_FAILURE_TEXT)
            return

        if outcome.replayed:
            # A duplicate interaction delivery: the original attempt owns the
            # model call and the posting, so nothing is posted again.
            return
        if outcome.status == PromptAttemptStatus.SUCCEEDED and outcome.answer is not None:
            pages = paginate_text(format_prompt_response(outcome.answer, outcome.receipt))
            # Model-generated text is untrusted display content: suppress every
            # mention so @everyone/@here/<@user>/<@&role> stay literal text and
            # never notify anyone. Capture/claim posts keep their own mentions.
            no_mentions = discord.AllowedMentions.none()
            await interaction.followup.send(pages[0], allowed_mentions=no_mentions)
            for page in pages[1:]:
                await interaction.followup.send(page, allowed_mentions=no_mentions)
        else:
            await interaction.followup.send(PROMPT_FAILURE_TEXT)

    @bot.tree.command(name="cancel", description="Cancel your open capture session")
    async def cancel(interaction: discord.Interaction) -> None:
        try:
            scope.require_intake(interaction.guild_id, interaction.channel_id)
            bundle = repository.find_open_capture(str(interaction.user.id), str(interaction.channel_id))
            if bundle is None:
                raise NotFoundError("You have no open capture in this channel")
            cancelled = capture.cancel(
                bundle.id,
                actor_user_id=str(interaction.user.id),
                idempotency_key=f"discord-cancel:{interaction.id}",
            )
            await interaction.response.send_message(
                f"Capture `{cancelled.id}` cancelled; no source messages were deleted.",
                ephemeral=True,
            )
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(
        name="claim",
        description="Select claim options (comma-separated) or supply a custom topic",
    )
    @app_commands.describe(
        options='Comma-separated claim option numbers, e.g. "1" or "2,3"',
        topic="Custom plain-language subject (instead of options)",
        bundle_id="Required when several bundles await you",
    )
    async def claim(
        interaction: discord.Interaction,
        options: str | None = None,
        topic: str | None = None,
        bundle_id: str | None = None,
    ) -> None:
        try:
            scope.require_intake(interaction.guild_id, interaction.channel_id)
            if bundle_id is None:
                pending = repository.list_pending_bundles(str(interaction.user.id))
                if len(pending) != 1:
                    raise ValueError(
                        "Supply bundle_id because the number of pending bundles is not exactly one"
                    )
                bundle_id = pending[0].id
            option_numbers = parse_option_numbers(options) if options is not None else None
            claim_options_service.select(
                bundle_id,
                actor_user_id=str(interaction.user.id),
                option_numbers=option_numbers,
                custom_topic=topic,
            )
            await interaction.response.send_message(
                f"Claim recorded for `{bundle_id}`. Analysis is running; live conversation may continue."
            )
            remember(
                asyncio.create_task(
                    post_analysis(bundle_id, str(interaction.user.id), str(interaction.id))
                )
            )
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(name="claim-options", description="Show the claim options for a sealed bundle")
    @app_commands.describe(bundle_id="Required when several bundles await you")
    async def claim_options(interaction: discord.Interaction, bundle_id: str | None = None) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            if bundle_id is None:
                pending = repository.list_pending_bundles(str(interaction.user.id))
                if len(pending) != 1:
                    raise ValueError(
                        "Supply bundle_id because the number of pending bundles is not exactly one"
                    )
                bundle_id = pending[0].id
            candidates = claim_options_service.get_options(bundle_id, actor_user_id=str(interaction.user.id))
            bundle = repository.get_bundle(bundle_id)
            pages = paginate_text(format_claim_options(bundle, candidates))
            await interaction.response.send_message(pages[0], ephemeral=True)
            for page in pages[1:]:
                await interaction.followup.send(page, ephemeral=True)
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(
        name="claim-options-retry",
        description="Regenerate claim options for a bundle whose discovery failed",
    )
    @app_commands.describe(bundle_id="Bundle in CLAIM_OPTIONS_FAILED")
    async def claim_options_retry(interaction: discord.Interaction, bundle_id: str) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            bundle = repository.get_bundle(bundle_id)
            actor = str(interaction.user.id)
            if actor != bundle.submitter_user_id and actor not in settings.admin_user_ids:
                raise AuthorizationError(
                    "Only the capture owner or an administrator may retry claim options"
                )
            if bundle.status != BundleState.CLAIM_OPTIONS_FAILED:
                raise ConflictError(
                    "Claim-options retry is available only for CLAIM_OPTIONS_FAILED bundles"
                )
            await interaction.response.defer()
            outcome = await claim_options_service.discover(
                bundle.id,
                actor_user_id=actor,
                idempotency_key=f"discord-claim-options:{interaction.id}",
            )
            if outcome.replayed:
                return
            sealed = repository.get_bundle(bundle.id)
            if outcome.candidates:
                pages = paginate_text(format_claim_options(sealed, outcome.candidates))
                await interaction.followup.send(pages[0])
                for page in pages[1:]:
                    await interaction.followup.send(page)
            else:
                await interaction.followup.send(_claim_options_failure_text(sealed.id))
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(
        name="autonomous-retry",
        description="Retry a failed or ambiguous autonomous sort with a fresh attempt",
    )
    @app_commands.describe(bundle_id="Autonomous bundle whose sort failed or stopped ambiguously")
    async def autonomous_retry(interaction: discord.Interaction, bundle_id: str) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            if not autonomous_enabled:
                raise PermissionError(
                    "Autonomous retry requires an Autonomous Test or Live bot"
                )
            job = autonomous_jobs.retry(
                bundle_id,
                actor_user_id=str(interaction.user.id),
                idempotency_key=f"autonomous-retry:{interaction.id}",
            )
            await interaction.response.send_message(
                f"Autonomous sort retry queued for `{bundle_id}` with a fresh "
                "attempt key. The sealed source was not changed and no paid call "
                "has been made yet.",
                ephemeral=True,
            )
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(name="bundle-status", description="Show compact bundle state without source text")
    async def bundle_status(interaction: discord.Interaction, bundle_id: str) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            authorization.require_bundle_participant(bundle_id, str(interaction.user.id))
            bundle = repository.get_bundle(bundle_id)
            tendrils = repository.list_tendrils(bundle_id=bundle_id)
            line = (
                f"`{bundle.id}` | `{bundle.status.value}` | messages: {len(bundle.source_messages)} | "
                f"units: {len(bundle.source_units)} | claim: {'yes' if bundle.claim_id else 'no'} | "
                f"tendrils: {len(tendrils)}"
            )
            if bundle.autonomous_mode:
                try:
                    job = repository.get_autonomous_job(bundle_id)
                except NotFoundError:
                    job = None
                if job is not None:
                    line += f" | autonomous job: `{job.phase.value}`"
            await interaction.response.send_message(line, ephemeral=True)
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(
        name="analysis-retry",
        description="Manually retry a failed or review-blocked analysis",
    )
    async def analysis_retry(interaction: discord.Interaction, bundle_id: str) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            bundle = authorization.require_bundle_recipient(
                bundle_id, str(interaction.user.id)
            )
            if bundle.status not in {
                BundleState.ANALYSIS_FAILED,
                BundleState.NEEDS_REVIEW,
            }:
                raise ConflictError(
                    "Analysis retry is available only for ANALYSIS_FAILED or NEEDS_REVIEW bundles"
                )
            await interaction.response.send_message(
                f"Manual analysis retry queued for `{bundle_id}`. No automatic repair calls will follow.",
                ephemeral=True,
            )
            remember(
                asyncio.create_task(
                    post_analysis(bundle_id, str(interaction.user.id), str(interaction.id))
                )
            )
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(name="tendrils", description="List concise tendril records")
    async def tendrils(interaction: discord.Interaction, status: str = "OPEN") -> None:
        try:
            scope.require_guild(interaction.guild_id)
            state = TendrilState(status.upper())
            items = repository.list_tendrils(statuses={state})
            if not items:
                await interaction.response.send_message(
                    f"No `{state.value}` tendrils.", ephemeral=True
                )
                return
            text, _omitted = bounded_page(
                items,
                render=lambda item: f"`{item.id}` **{item.title}** · `{item.type.value}`",
                footer=f"… more remain; filter with `/tendrils status:{state.value}`.",
            )
            await interaction.response.send_message(text, ephemeral=True)
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(name="tendril-show", description="Show one tendril with exact evidence")
    async def tendril_show(interaction: discord.Interaction, tendril_id: str) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            authorization.require_tendril_participant(tendril_id, str(interaction.user.id))
            item = repository.get_tendril(tendril_id)
            pages = paginate_text(format_tendril(item, repository))
            await interaction.response.send_message(pages[0], ephemeral=True)
            for page in pages[1:]:
                await interaction.followup.send(page, ephemeral=True)
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(name="pull", description="Resurface one eligible open tendril")
    async def pull(interaction: discord.Interaction) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            item = resurfacing.pull(
                actor_user_id=str(interaction.user.id),
                idempotency_key=f"discord-pull:{interaction.id}",
            )
            pages = paginate_text(format_tendril(item, repository))
            await interaction.response.send_message(pages[0])
            for page in pages[1:]:
                await interaction.followup.send(page)
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(name="snooze", description="Snooze a tendril for a number of hours")
    async def snooze(interaction: discord.Interaction, tendril_id: str, hours: int) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            if hours < 1 or hours > 8760:
                raise ValueError("hours must be between 1 and 8760")
            item = resurfacing.snooze(
                tendril_id,
                actor_user_id=str(interaction.user.id),
                idempotency_key=f"discord-snooze:{interaction.id}",
                until=datetime.now(timezone.utc) + timedelta(hours=hours),
            )
            await interaction.response.send_message(
                f"`{item.id}` snoozed until {item.snoozed_until.isoformat()}", ephemeral=True
            )
        except Exception as exc:
            await reject(interaction, exc)

    async def lifecycle_command(
        interaction: discord.Interaction,
        tendril_id: str,
        action: str,
    ) -> None:
        scope.require_guild(interaction.guild_id)
        method = getattr(resurfacing, action)
        item = method(
            tendril_id,
            actor_user_id=str(interaction.user.id),
            idempotency_key=f"discord-{action}:{interaction.id}",
        )
        await interaction.response.send_message(
            f"`{item.id}` -> `{item.status.value}`", ephemeral=True
        )

    @bot.tree.command(name="resolve", description="Mark a tendril resolved")
    async def resolve(interaction: discord.Interaction, tendril_id: str) -> None:
        try:
            await lifecycle_command(interaction, tendril_id, "resolve")
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(name="release", description="Deliberately release a tendril")
    async def release(interaction: discord.Interaction, tendril_id: str) -> None:
        try:
            await lifecycle_command(interaction, tendril_id, "release")
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(name="merge", description="Merge one tendril into another")
    async def merge(
        interaction: discord.Interaction, source_tendril_id: str, target_tendril_id: str
    ) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            item = resurfacing.merge(
                source_tendril_id,
                target_tendril_id,
                actor_user_id=str(interaction.user.id),
                idempotency_key=f"discord-merge:{interaction.id}",
            )
            await interaction.response.send_message(
                f"`{item.id}` merged into `{target_tendril_id}`", ephemeral=True
            )
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(
        name="promote-actionable",
        description="Promote an eligible tendril to PROMOTED_ACTIONABLE",
    )
    async def promote_actionable(interaction: discord.Interaction, tendril_id: str) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            item = resurfacing.promote_actionable(
                tendril_id,
                actor_user_id=str(interaction.user.id),
                idempotency_key=f"discord-promote-actionable:{interaction.id}",
            )
            await interaction.response.send_message(
                f"`{item.id}` -> `{item.status.value}`; evidence and lifecycle history "
                "preserved.",
                ephemeral=True,
            )
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(name="route", description="Route a tendril into an existing anemone channel")
    async def route(
        interaction: discord.Interaction,
        tendril_id: str,
        channel: discord.TextChannel,
    ) -> None:
        try:
            scope.require_anemone_destination(channel.guild.id, channel.category_id)
            item = await routing.route(
                tendril_id,
                external_destination_id=str(channel.id),
                actor_user_id=str(interaction.user.id),
                idempotency_key=f"discord-route:{interaction.id}",
            )
            await interaction.response.send_message(
                f"`{item.id}` routed to {channel.mention}", ephemeral=True
            )
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(name="habitat-create", description="Create one approved habitat and route a tendril")
    async def habitat_create(
        interaction: discord.Interaction,
        tendril_id: str,
        name: str,
    ) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            authorization.require_tendril_participant(tendril_id, str(interaction.user.id))
            permissions = getattr(interaction.user, "guild_permissions", None)
            if not permissions or not permissions.manage_channels:
                raise PermissionError("Manage Channels permission is required")
            guild = interaction.guild
            category = guild.get_channel(int(scope.anemone_category_id)) if guild else None
            if category is None or not hasattr(category, "create_text_channel"):
                raise NotFoundError(
                    "Configured anemone category is unavailable or is not a category"
                )
            slug = sanitize_channel_slug(name)
            actor = str(interaction.user.id)
            key = f"discord-habitat:{interaction.id}"
            tendril = repository.get_tendril(tendril_id)

            operation, created = repository.reserve_external_operation(
                operation_type="habitat_create",
                idempotency_key=key,
                platform="discord",
                tendril_id=tendril_id,
                actor_user_id=actor,
                external_destination_id=slug,
                metadata={"requested_slug": slug},
            )

            if not created:
                if operation.idempotency_key != key:
                    raise ConflictError(
                        "Tendril already has an unresolved habitat operation; "
                        "reconcile it before retrying"
                    )
                if operation.status == ExternalOperationStatus.COMPLETED:
                    channel_id = operation.external_destination_id
                    if channel_id is None:
                        raise ConflictError("Completed habitat operation retained no channel ID")
                    channel = guild.get_channel(int(channel_id)) if guild else None
                    if channel is None:
                        raise NotFoundError("Created habitat channel is unavailable")
                    await interaction.response.send_message(
                        f"Habitat already created: {channel.mention}.", ephemeral=True
                    )
                    return
                if operation.status == ExternalOperationStatus.EXTERNAL_SUCCEEDED:
                    channel_id = operation.external_destination_id
                    if channel_id is None:
                        raise ConflictError("Habitat operation retained no channel ID")
                    channel = guild.get_channel(int(channel_id)) if guild else None
                    if channel is None:
                        repository.mark_operation_needs_reconciliation(
                            operation.id, reason="retained channel is no longer resolvable"
                        )
                        raise NotFoundError("Created habitat channel is unavailable")
                    if operation.external_message_id is None:
                        repository.mark_operation_needs_reconciliation(
                            operation.id, reason="tendril post result was never retained"
                        )
                        raise ConflictError(
                            "The tendril post result is unknown; an ambiguous external side "
                            "effect is never repeated automatically. Reconcile the operation."
                        )
                    repository.record_route(
                        tendril_id,
                        "discord",
                        channel_id,
                        operation.external_message_id,
                        actor,
                        f"discord-habitat-route:{interaction.id}",
                    )
                    repository.mark_operation_completed(operation.id)
                    item = repository.get_tendril(tendril_id)
                    await interaction.response.send_message(
                        f"Habitat ready: {channel.mention}; routed `{item.id}`.", ephemeral=True
                    )
                    return
                raise ConflictError(
                    f"Habitat operation is {operation.status.value}; an ambiguous external "
                    "side effect is never repeated automatically. Reconcile it first."
                )

            existing_channel = next(
                (item for item in category.text_channels if item.name == slug), None
            )
            if existing_channel is not None:
                prior = repository.list_external_operations(tendril_id=tendril_id)
                owned = any(
                    item.operation_type == "habitat_create"
                    and item.status == ExternalOperationStatus.COMPLETED
                    and item.external_destination_id == str(existing_channel.id)
                    for item in prior
                )
                if not owned:
                    raise ValueError("A habitat with that channel name already exists")
                channel = existing_channel
            else:
                try:
                    channel = await category.create_text_channel(
                        slug,
                        reason=f"Nemoir habitat for tendril {tendril_id}; requested by {actor}",
                    )
                except Exception as exc:
                    repository.mark_operation_needs_reconciliation(
                        operation.id,
                        reason=f"channel creation raised {type(exc).__name__}: {exc}",
                    )
                    raise
                repository.register_habitat(
                    guild_id=str(interaction.guild_id),
                    platform="discord",
                    external_id=str(channel.id),
                    canonical_slug=slug,
                    description=f"Habitat for tendril {tendril_id}",
                )
            repository.mark_operation_external_succeeded(
                operation.id, external_destination_id=str(channel.id)
            )
            try:
                message_id = await publisher.publish(tendril, str(channel.id))
            except Exception as exc:
                repository.mark_operation_needs_reconciliation(
                    operation.id, reason=f"publish raised {type(exc).__name__}: {exc}"
                )
                raise
            if message_id is None:
                repository.mark_operation_needs_reconciliation(
                    operation.id, reason="publisher returned no external message ID"
                )
                raise ConflictError("Publisher did not return an external message ID")
            repository.mark_operation_external_succeeded(
                operation.id, external_message_id=message_id
            )
            repository.record_route(
                tendril_id,
                "discord",
                str(channel.id),
                message_id,
                actor,
                f"discord-habitat-route:{interaction.id}",
            )
            repository.mark_operation_completed(operation.id)
            item = repository.get_tendril(tendril_id)
            await interaction.response.send_message(
                f"Created {channel.mention} and routed `{item.id}`.", ephemeral=True
            )
        except Exception as exc:
            await reject(interaction, exc)

    @bot.tree.command(
        name="publish-bundle",
        description="Preview or publish a bundle's unclaimed tendrils as channels",
    )
    @app_commands.describe(
        bundle_id="Bundle in REVIEW_READY",
        confirm="true creates the channels; false/omitted is a read-only preview",
    )
    async def publish_bundle(
        interaction: discord.Interaction,
        bundle_id: str,
        confirm: bool = False,
    ) -> None:
        try:
            scope.require_guild(interaction.guild_id)
            no_mentions = discord.AllowedMentions.none()
            if not confirm:
                preview = publishing.preview(
                    bundle_id, actor_user_id=str(interaction.user.id)
                )
                pages = paginate_text(format_publish_preview(preview))
                await interaction.response.send_message(
                    pages[0], ephemeral=True, allowed_mentions=no_mentions
                )
                for page in pages[1:]:
                    await interaction.followup.send(
                        page, ephemeral=True, allowed_mentions=no_mentions
                    )
                return

            # Confirmed publish: every server-side safety gate is enforced here,
            # independent of any GUI affordance that launched the process.
            if not settings.allow_channel_write:
                raise PermissionError(
                    "Channel-write gate is disabled; set NEMOIR_ALLOW_CHANNEL_WRITE=true"
                )
            permissions = getattr(interaction.user, "guild_permissions", None)
            if not permissions or not permissions.manage_channels:
                raise PermissionError("Manage Channels permission is required")
            guild = interaction.guild
            category = guild.get_channel(int(scope.anemone_category_id)) if guild else None
            if category is None or not hasattr(category, "create_text_channel"):
                raise NotFoundError(
                    "Configured anemone category is unavailable or is not a category"
                )

            # Acknowledge immediately: publishing several channels and posts can
            # exceed Discord's interaction window.
            await interaction.response.defer()
            outcome = await publishing.publish_bundle(
                bundle_id,
                actor_user_id=str(interaction.user.id),
                guild_id=str(interaction.guild_id),
                category_id=scope.anemone_category_id,
            )
            pages = paginate_text(format_publish_report(outcome))
            await interaction.followup.send(pages[0], allowed_mentions=no_mentions)
            for page in pages[1:]:
                await interaction.followup.send(page, allowed_mentions=no_mentions)
        except Exception as exc:
            await reject(interaction, exc)

    if pilot_mode:
        bot.tree.remove_command("route")
        bot.tree.remove_command("habitat-create")
        bot.tree.remove_command("publish-bundle")

    return bot


def run_discord_bot(
    settings: Settings,
    repository: SQLiteRepository,
    provider: AnalysisProvider,
    *,
    pilot_mode: bool = False,
    channel_test: bool = False,
    autonomous_mode: str | None = None,
    autonomous_sort_provider: AutonomousSortProvider | None = None,
    status_store: RuntimeStatusStore | None = None,
    shutdown_request: ShutdownRequest | None = None,
    instance_id: str | None = None,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    if not settings.discord_bot_token:
        raise ValueError("DISCORD_BOT_TOKEN is not configured")
    if autonomous_mode == "test":
        mode = "autonomous-test"
    elif autonomous_mode == "live":
        mode = "autonomous-live"
    elif channel_test:
        mode = "channel-test"
    elif pilot_mode:
        mode = "synthetic"
    else:
        mode = "live"
    model = None if (pilot_mode or channel_test or autonomous_mode == "test") else settings.deepseek_model
    resolved_instance_id = instance_id or f"pid-{os.getpid()}"
    if status_store is not None:
        status_store.record_starting(
            mode=mode,
            model=model,
            instance_id=resolved_instance_id,
            pid=os.getpid(),
        )
    bot = create_discord_bot(
        settings,
        repository,
        provider,
        pilot_mode=pilot_mode,
        channel_test=channel_test,
        autonomous_mode=autonomous_mode,
        autonomous_sort_provider=autonomous_sort_provider,
        status_store=status_store,
        shutdown_request=shutdown_request,
        heartbeat_interval=heartbeat_interval,
    )
    try:
        bot.run(settings.discord_bot_token, log_handler=None)
    except KeyboardInterrupt:
        if status_store is not None:
            status_store.record_offline()
    except BaseException as exc:
        if status_store is not None:
            status_store.record_error(type(exc).__name__)
        raise
    else:
        if status_store is not None:
            status_store.record_offline()
