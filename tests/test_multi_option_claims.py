"""Multi-option claim selection: service, validation, migration, and Discord."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from nemoir.adapters.discord_bot import create_discord_bot, format_claim_options
from nemoir.application.analysis_service import AnalysisService
from nemoir.application.authorization import AuthorizationPolicy
from nemoir.application.capture_service import CaptureService
from nemoir.application.claim_options_service import ClaimOptionsService, parse_option_numbers
from nemoir.config import Settings
from nemoir.domain.errors import AuthorizationError, ConflictError
from nemoir.domain.models import (
    ClaimCandidate,
    ClaimCandidateSet,
    ClaimDiscoveryResponse,
    ProviderReceipt,
    SourceFragment,
    authoritative_claim_boundary,
    join_selected_titles,
)
from nemoir.domain.states import BundleState, CoverageClassification
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.fake import FakeAnalysisProvider, adapt_analysis_to_selected_candidates

FREE_WILL = "Free will matters only if choices have consequences."
AI_COMPASSION = (
    "If an AI only repeats compassionate language, has it learned compassion or "
    "merely reproduced its shape?"
)
WAR_STRESS = (
    "A system choosing between a devastating short war and a longer war with "
    "greater suffering would stress-test what consequential compassion means."
)
UNFAMILIAR_LIFE = (
    "Could unfamiliar life sit outside the assumptions built into how we observe "
    "the universe?"
)
TIME = "I keep wondering whether time can be both eternal and an illusion."
AI_LENS = "Maybe AI should be one lens rather than the only perspective."
CATALOGUE = (
    "We should build a tool that catalogues long human and AI chats, separates "
    "unfinished branches, and lets us return to them."
)

ALL_SELECTED_QUOTES = {FREE_WILL, AI_COMPASSION, WAR_STRESS}
ALL_TENDRIL_QUOTES = {UNFAMILIAR_LIFE, TIME, AI_LENS, CATALOGUE}


def _seal(repository, synthetic_messages, *, prefix: str = ""):
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
    )
    for message in synthetic_messages:
        capture.capture_message(
            bundle.id,
            actor_user_id="person-1",
            external_message_id=f"{prefix}{message.external_message_id}",
            author_display_name=message.author_display_name,
            channel_id="intake-1",
            content=message.content,
            source_url=message.source_url,
            timestamp=message.timestamp,
        )
    return capture.seal(
        bundle.id, actor_user_id="person-1", idempotency_key=f"multi-seal-{prefix}"
    )


class SplitDiscoveryProvider:
    """Deterministic, source-backed discovery that splits the free-will and
    compassion material into separate candidates (the live-pilot shape)."""

    async def discover_claim_candidates(self, request):
        unit_by_text = {unit.exact_text: unit for unit in request.source_units}

        def frag(text: str) -> SourceFragment:
            unit = unit_by_text[text]
            return SourceFragment(
                source_message_id=unit.source_message_id,
                exact_quote=unit.exact_text,
                unit_ids=[unit.unit_id],
                start_offset=unit.start_offset,
                end_offset=unit.end_offset,
            )

        candidates = [
            ClaimCandidate(
                candidate_id="cand-1",
                title="Unfamiliar life outside observation",
                summary="Whether unfamiliar life could sit outside our observational assumptions.",
                evidence=[frag(UNFAMILIAR_LIFE)],
                display_order=1,
            ),
            ClaimCandidate(
                candidate_id="cand-2",
                title="Free will and consequential choice",
                summary="Free will requires choices to carry consequences.",
                evidence=[frag(FREE_WILL)],
                display_order=2,
            ),
            ClaimCandidate(
                candidate_id="cand-3",
                title="Testing AI compassion through hard choices",
                summary="Hard consequential choices stress-test what learned compassion means.",
                evidence=[frag(AI_COMPASSION), frag(WAR_STRESS)],
                display_order=3,
            ),
            ClaimCandidate(
                candidate_id="cand-4",
                title="Time as eternal and illusory",
                summary="A tension between time being eternal and an illusion.",
                evidence=[frag(TIME)],
                display_order=4,
            ),
            ClaimCandidate(
                candidate_id="cand-5",
                title="AI as a lens and a chat catalogue",
                summary="AI as one perspective and a tool to catalogue unfinished chats.",
                evidence=[frag(AI_LENS), frag(CATALOGUE)],
                display_order=5,
            ),
        ]
        return ClaimDiscoveryResponse(
            result=ClaimCandidateSet(bundle_id=request.bundle_id, candidates=candidates),
            receipt=ProviderReceipt(
                provider="synthetic",
                model="split-discovery",
                latency_ms=0,
                outcome="success",
            ),
        )


class SplitAnalysisProvider(FakeAnalysisProvider):
    """Fake analysis provider whose discovery path also splits the candidates."""

    def __init__(self, result) -> None:
        super().__init__(result)
        self.discovery = SplitDiscoveryProvider()


async def _discover_split(repository, synthetic_messages, authorization):
    bundle = _seal(repository, synthetic_messages)
    options = ClaimOptionsService(repository, SplitDiscoveryProvider(), authorization)
    await options.discover(bundle.id, actor_user_id="person-1", idempotency_key="split-disc")
    return bundle, options


# --- Parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", [1]),
        ("2,3", [2, 3]),
        ("1, 3, 4", [1, 3, 4]),
        (" 2 , 3 ", [2, 3]),
    ],
)
def test_parse_option_numbers_accepts_valid_input(value, expected) -> None:
    assert parse_option_numbers(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "   ", "1,,2", ",1", "1,", "a", "1.5", "one,two", "1;2"],
)
def test_parse_option_numbers_rejects_malformed_input(value) -> None:
    with pytest.raises(ValueError):
        parse_option_numbers(value)


# --- Service selection -----------------------------------------------------


@pytest.mark.asyncio
async def test_one_option_via_plural_syntax(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle, options = await _discover_split(repository, synthetic_messages, authorization)
    claim = options.select(bundle.id, actor_user_id="person-2", option_numbers=[2])
    assert claim.selected_candidate_ids == ["cand-2"]
    assert [candidate.title for candidate in claim.selected_candidates] == [
        "Free will and consequential choice"
    ]
    assert claim.raw_topic == "Free will and consequential choice"


@pytest.mark.asyncio
async def test_two_and_several_options_and_canonical_order(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle, options = await _discover_split(repository, synthetic_messages, authorization)
    # Input order 3,2 must be stored canonically in display order 2,3.
    claim = options.select(bundle.id, actor_user_id="person-2", option_numbers=[3, 2])
    assert claim.selected_candidate_ids == ["cand-2", "cand-3"]
    assert [candidate.display_order for candidate in claim.selected_candidates] == [2, 3]
    assert claim.raw_topic == (
        "Free will and consequential choice + Testing AI compassion through hard choices"
    )

    # Selecting several options (including whitespace-surviving order) works.
    bundle2 = _seal(repository, synthetic_messages, prefix="b-")
    options2 = ClaimOptionsService(repository, SplitDiscoveryProvider(), authorization)
    await options2.discover(
        bundle2.id, actor_user_id="person-1", idempotency_key="several-disc"
    )
    claim2 = options2.select(
        bundle2.id, actor_user_id="person-2", option_numbers=[5, 1, 3]
    )
    assert claim2.selected_candidate_ids == ["cand-1", "cand-3", "cand-5"]


@pytest.mark.asyncio
async def test_selecting_every_available_option(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle, options = await _discover_split(repository, synthetic_messages, authorization)
    claim = options.select(bundle.id, actor_user_id="person-2", option_numbers=[5, 4, 3, 2, 1])
    assert claim.selected_candidate_ids == ["cand-1", "cand-2", "cand-3", "cand-4", "cand-5"]
    assert len(claim.selected_candidates) == 5


@pytest.mark.asyncio
async def test_duplicate_zero_negative_and_out_of_range_rejected(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle, options = await _discover_split(repository, synthetic_messages, authorization)
    for bad in ([1, 1], [0], [-1], [2, -1], [99], [2, 99]):
        with pytest.raises(ConflictError):
            options.select(bundle.id, actor_user_id="person-2", option_numbers=bad)


@pytest.mark.asyncio
async def test_options_topic_mutual_exclusivity(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle, options = await _discover_split(repository, synthetic_messages, authorization)
    with pytest.raises(ConflictError):
        options.select(bundle.id, actor_user_id="person-2")
    with pytest.raises(ConflictError):
        options.select(bundle.id, actor_user_id="person-2", option_numbers=[2], custom_topic="x")
    with pytest.raises(ConflictError):
        options.select(bundle.id, actor_user_id="person-2", option_numbers=[])


@pytest.mark.asyncio
async def test_selection_authorization(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle, options = await _discover_split(repository, synthetic_messages, authorization)
    with pytest.raises(AuthorizationError):
        options.select(bundle.id, actor_user_id="person-1", option_numbers=[2])
    with pytest.raises(AuthorizationError):
        options.select(bundle.id, actor_user_id="intruder", option_numbers=[2])
    # The designated recipient and an administrator may select.
    assert options.select(bundle.id, actor_user_id="person-2", option_numbers=[2])


@pytest.mark.asyncio
async def test_idempotent_and_conflicting_second_selection(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle, options = await _discover_split(repository, synthetic_messages, authorization)
    options.select(bundle.id, actor_user_id="person-2", option_numbers=[2, 3])
    with pytest.raises(ConflictError):
        options.select(bundle.id, actor_user_id="person-2", option_numbers=[2, 3])
    with pytest.raises(ConflictError):
        options.select(bundle.id, actor_user_id="person-2", option_numbers=[1])


# --- Authoritative union ---------------------------------------------------


def _fragment(source_message_id: str, exact_quote: str, unit_ids: list[str]) -> SourceFragment:
    return SourceFragment(
        source_message_id=source_message_id,
        exact_quote=exact_quote,
        unit_ids=unit_ids,
    )


def test_authoritative_boundary_dedupes_overlapping_fragments_without_losing_attribution() -> None:
    first = ClaimCandidate(
        candidate_id="cand-a",
        title="A",
        summary="s",
        evidence=[_fragment("m-1", "shared quote", ["m-1:p1"])],
        display_order=1,
    )
    second = ClaimCandidate(
        candidate_id="cand-b",
        title="B",
        summary="s",
        evidence=[
            _fragment("m-1", "shared quote", ["m-1:p2"]),
            _fragment("m-1", "distinct quote", ["m-1:p3"]),
        ],
        display_order=2,
    )
    boundary = authoritative_claim_boundary([first, second])
    keys = {(fragment.source_message_id, fragment.exact_quote) for fragment in boundary}
    assert keys == {("m-1", "shared quote"), ("m-1", "distinct quote")}
    shared = next(fragment for fragment in boundary if fragment.exact_quote == "shared quote")
    assert shared.unit_ids == ["m-1:p1", "m-1:p2"]


def test_join_selected_titles_uses_display_order() -> None:
    candidates = [
        ClaimCandidate(
            candidate_id="cand-2",
            title="Two",
            summary="s",
            evidence=[_fragment("m", "q", ["m:p1"])],
            display_order=2,
        ),
        ClaimCandidate(
            candidate_id="cand-1",
            title="One",
            summary="s",
            evidence=[_fragment("m", "q", ["m:p1"])],
            display_order=1,
        ),
    ]
    assert join_selected_titles(candidates) == "One + Two"


def test_adapt_analysis_to_selected_candidates_produces_union(synthetic_analysis) -> None:
    selected = [
        ClaimCandidate(
            candidate_id="cand-2",
            title="Free will",
            summary="s",
            evidence=[_fragment("synthetic-102", FREE_WILL, ["synthetic-102:p1"])],
            display_order=2,
        ),
        ClaimCandidate(
            candidate_id="cand-3",
            title="Compassion",
            summary="s",
            evidence=[
                _fragment("synthetic-102", AI_COMPASSION, ["synthetic-102:p2"]),
                _fragment("synthetic-102", WAR_STRESS, ["synthetic-102:p3"]),
            ],
            display_order=3,
        ),
    ]
    adapted = adapt_analysis_to_selected_candidates(synthetic_analysis, selected)
    assert {fragment.exact_quote for fragment in adapted.claim.matching_fragments} == (
        ALL_SELECTED_QUOTES
    )
    assert {fragment.exact_quote for t in adapted.tendrils for fragment in t.evidence} == (
        ALL_TENDRIL_QUOTES
    )
    claimed_units = {
        entry.unit_id
        for entry in adapted.coverage
        if entry.classification == CoverageClassification.CLAIMED
    }
    assert claimed_units == {"synthetic-102:p1", "synthetic-102:p2", "synthetic-102:p3"}


# --- Source-backed regression ----------------------------------------------


@pytest.mark.asyncio
async def test_combined_option_regression_authoritative_boundary_and_continuity(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle, options = await _discover_split(repository, synthetic_messages, authorization)
    claim = options.select(bundle.id, actor_user_id="person-2", option_numbers=[3, 2])

    # One claim with both candidate IDs and snapshots, canonically ordered.
    assert claim.selected_candidate_ids == ["cand-2", "cand-3"]
    assert [candidate.candidate_id for candidate in claim.selected_candidates] == [
        "cand-2",
        "cand-3",
    ]

    outcome = await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="combined-analysis")
    assert outcome.state == BundleState.REVIEW_READY
    assert outcome.validation.valid
    review = outcome.review
    assert review is not None

    # Exact authoritative union is the claim boundary.
    assert {fragment.exact_quote for fragment in review.claim.matching_fragments} == (
        ALL_SELECTED_QUOTES
    )
    # All three selected quotations are classified CLAIMED.
    selected_units = {
        unit_id
        for fragment in review.claim.matching_fragments
        for unit_id in fragment.unit_ids
    }
    for entry in review.coverage:
        if entry.unit_id in selected_units:
            assert entry.classification == CoverageClassification.CLAIMED
    # Unselected candidates remain tendrils, never CONTEXT.
    assert {fragment.exact_quote for t in review.tendrils for fragment in t.evidence} == (
        ALL_TENDRIL_QUOTES
    )
    assert all(
        entry.classification != CoverageClassification.CONTEXT for entry in review.coverage
    )

    # Persisted plural selection survives a repository restart.
    path = repository.database_path
    repository.close()
    reopened = SQLiteRepository(path)
    try:
        persisted = reopened.get_claim_for_bundle(bundle.id)
        assert persisted.selected_candidate_ids == ["cand-2", "cand-3"]
        assert [candidate.candidate_id for candidate in persisted.selected_candidates] == [
            "cand-2",
            "cand-3",
        ]
        # Analysis replay with a fresh idempotency key passes the same boundary.
        reopened_auth = AuthorizationPolicy(reopened, admin_user_ids={"admin-1"})
        replayed = await AnalysisService(
            reopened, FakeAnalysisProvider(synthetic_analysis), reopened_auth
        ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="combined-replay")
        assert replayed.review is not None
        assert replayed.validation.valid
        assert {fragment.exact_quote for fragment in replayed.review.claim.matching_fragments} == (
            ALL_SELECTED_QUOTES
        )
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_export_includes_plural_selection(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle, options = await _discover_split(repository, synthetic_messages, authorization)
    options.select(bundle.id, actor_user_id="person-2", option_numbers=[2, 3])
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="export-analysis")
    exported = repository.export_bundle(bundle.id)
    claim = exported["claim"]
    assert claim["selected_candidate_ids"] == ["cand-2", "cand-3"]
    assert [candidate["candidate_id"] for candidate in claim["selected_candidates"]] == [
        "cand-2",
        "cand-3",
    ]
    assert claim["selected_candidates"][0]["evidence"][0]["exact_quote"] == FREE_WILL


# --- Migration -------------------------------------------------------------


def _legacy_singular_snapshot() -> dict:
    return {
        "candidate_id": "cand-legacy",
        "title": "Legacy candidate",
        "summary": "A persisted singular selection.",
        "evidence": [
            {
                "source_message_id": "m-1",
                "exact_quote": "a legacy quote",
                "unit_ids": ["m-1:p1"],
                "start_offset": None,
                "end_offset": None,
            }
        ],
        "display_order": 1,
    }


def _create_v2_database(path, *, with_selection: bool) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_meta(version, applied_at) VALUES (2, '2026-08-27T00:00:00Z');
        CREATE TABLE claims (
            id TEXT PRIMARY KEY,
            bundle_id TEXT NOT NULL UNIQUE,
            raw_topic TEXT NOT NULL,
            claimant_user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            selected_candidate_id TEXT,
            selected_candidate_json TEXT
        );
        """
    )
    snapshot = json.dumps(_legacy_singular_snapshot())
    if with_selection:
        conn.execute(
            """
            INSERT INTO claims(
                id, bundle_id, raw_topic, claimant_user_id, status, created_at,
                selected_candidate_id, selected_candidate_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "claim-singular",
                "bundle-singular",
                "Legacy candidate",
                "person-2",
                "DECLARED",
                "2026-08-27T08:00:00+00:00",
                "cand-legacy",
                snapshot,
            ),
        )
    conn.execute(
        """
        INSERT INTO claims(
            id, bundle_id, raw_topic, claimant_user_id, status, created_at,
            selected_candidate_id, selected_candidate_json
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
        """,
        (
            "claim-topic",
            "bundle-topic",
            "A custom topic",
            "person-2",
            "DECLARED",
            "2026-08-27T08:01:00+00:00",
        ),
    )
    conn.commit()
    conn.close()


def test_migration_backfills_legacy_singular_and_empty_selections(tmp_path) -> None:
    db_path = tmp_path / "v2.sqlite3"
    _create_v2_database(db_path, with_selection=True)
    repository = SQLiteRepository(db_path)
    try:
        singular = repository.get_claim_for_bundle("bundle-singular")
        assert singular.selected_candidate_ids == ["cand-legacy"]
        assert [candidate.candidate_id for candidate in singular.selected_candidates] == [
            "cand-legacy"
        ]
        assert singular.selected_candidates[0].evidence[0].exact_quote == "a legacy quote"

        custom = repository.get_claim_for_bundle("bundle-topic")
        assert custom.selected_candidate_ids == []
        assert custom.selected_candidates == []

        version = repository._connection.execute(
            "SELECT version FROM schema_meta"
        ).fetchone()["version"]
        assert version == 4
    finally:
        repository.close()


def test_migration_backfills_legacy_no_selection_claim(tmp_path) -> None:
    """A pre-v2 claim with no selection columns reads as an empty selection."""
    db_path = tmp_path / "v1.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_meta(version, applied_at) VALUES (1, '2026-08-27T00:00:00Z');
        CREATE TABLE claims (
            id TEXT PRIMARY KEY,
            bundle_id TEXT NOT NULL UNIQUE,
            raw_topic TEXT NOT NULL,
            claimant_user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO claims(id, bundle_id, raw_topic, claimant_user_id, status, created_at)
        VALUES ('claim-legacy', 'bundle-legacy', 'Legacy topic', 'person-2', 'DECLARED', '2026-08-27T08:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    repository = SQLiteRepository(db_path)
    try:
        claim = repository.get_claim_for_bundle("bundle-legacy")
        assert claim.selected_candidate_ids == []
        assert claim.selected_candidates == []
        columns = {
            row["name"]
            for row in repository._connection.execute("PRAGMA table_info(claims)").fetchall()
        }
        assert "selected_candidate_ids_json" in columns
        assert "selected_candidates_json" in columns
    finally:
        repository.close()


# --- Discord adapter -------------------------------------------------------


class _FakeUser:
    def __init__(self, user_id) -> None:
        self.id = user_id
        self.bot = False
        self.mention = f"<@{user_id}>"
        self.display_name = f"user-{user_id}"
        self.guild_permissions = None


class _FakeResponse:
    def __init__(self) -> None:
        self.sent = []
        self.done = False
        self.deferred = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, *, ephemeral=False, thinking=False) -> None:
        self.deferred = True
        self.done = True

    async def send_message(self, content=None, *, ephemeral=False, **kwargs) -> None:
        self.sent.append(content)
        self.done = True


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, content=None, *, ephemeral=False, **kwargs) -> None:
        self.sent.append(content)


class _FakeChannel:
    def __init__(self, channel_id) -> None:
        self.id = channel_id
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        from types import SimpleNamespace

        return SimpleNamespace(id=f"{self.id}-{len(self.sent)}")


class _FakeGuild:
    def __init__(self, guild_id) -> None:
        self.id = guild_id

    def get_channel(self, channel_id):
        return None


class _FakeInteraction:
    def __init__(self, interaction_id, user, guild, *, channel_id, guild_id) -> None:
        self.id = interaction_id
        self.user = user
        self.guild = guild
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached in time")


def _discord_capture(repository, synthetic_messages, submitter="10", recipient="20"):
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="1",
        intake_channel_id="2",
        submitter_user_id=submitter,
        recipient_user_id=recipient,
    )
    for message in synthetic_messages:
        capture.capture_message(
            bundle.id,
            actor_user_id=submitter,
            external_message_id=message.external_message_id,
            author_display_name=message.author_display_name,
            channel_id="2",
            content=message.content,
            source_url=message.source_url,
            timestamp=message.timestamp,
        )
    return bundle


def test_discord_claim_registers_options_parameter_and_help(
    repository, synthetic_analysis
) -> None:
    settings = Settings(
        guild_id="1",
        intake_channel_id="2",
        anemone_category_id="3",
        nursery_channel_id="4",
    )
    bot = create_discord_bot(settings, repository, FakeAnalysisProvider(synthetic_analysis))
    command = bot.tree.get_command("claim")
    params = {parameter.name for parameter in command.parameters}
    assert "options" in params
    assert "option" not in params
    assert "topic" in params
    assert "bundle_id" in params
    assert "comma-separated" in command.description.lower()


def test_format_claim_options_renders_plural_hint() -> None:
    from nemoir.domain.models import ConversationBundle

    bundle = ConversationBundle(
        id="bundle-menu",
        guild_id="1",
        intake_channel_id="2",
        submitter_user_id="10",
        recipient_user_id="20",
        source_messages=[],
        source_units=[],
    )
    text = format_claim_options(bundle, [])
    assert 'options:"<n>"' in text
    assert "/claim option:" not in text


@pytest.mark.asyncio
async def test_discord_multi_option_claim_end_to_end(
    repository, synthetic_messages, synthetic_analysis
) -> None:
    settings = Settings(
        guild_id="1",
        intake_channel_id="2",
        anemone_category_id="3",
        nursery_channel_id="4",
    )
    bot = create_discord_bot(
        settings, repository, SplitAnalysisProvider(synthetic_analysis)
    )
    nursery = _FakeChannel(4)
    bot.get_channel = lambda channel_id: nursery if int(channel_id) == 4 else None  # type: ignore[method-assign]
    bundle = _discord_capture(repository, synthetic_messages)
    guild = _FakeGuild(1)

    await bot.tree.get_command("seal").callback(
        _FakeInteraction(11, _FakeUser(10), guild, channel_id=2, guild_id=1)
    )
    recipient = _FakeInteraction(12, _FakeUser("20"), guild, channel_id=2, guild_id=1)
    await bot.tree.get_command("claim").callback(recipient, options="2,3")
    assert "Claim recorded" in recipient.response.sent[0]

    await _wait_for(lambda: any("review ready" in text for text in nursery.sent))
    claim = repository.get_claim_for_bundle(bundle.id)
    assert claim.selected_candidate_ids == ["cand-2", "cand-3"]
    assert claim.raw_topic == (
        "Free will and consequential choice + Testing AI compassion through hard choices"
    )
