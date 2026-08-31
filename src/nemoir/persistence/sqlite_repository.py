"""Small SQLite repository with evidence-preserving, restart-safe operations."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from nemoir.domain.errors import ConflictError, NotFoundError
from nemoir.domain.models import (
    AnalysisResult,
    AutonomousAttempt,
    AutonomousJob,
    AutonomousSortResult,
    Claim,
    ClaimCandidate,
    ClaimCandidateSet,
    ClaimAnalysis,
    ConversationBundle,
    CoverageEntry,
    ExternalOperation,
    LifecycleEvent,
    PromptAttempt,
    ProviderReceipt,
    ReviewResult,
    SourceFragment,
    SourceMessage,
    SourceUnit,
    Tendril,
    new_id,
    utc_now,
)
from nemoir.domain.states import (
    AutonomousAttemptStatus,
    AutonomousJobPhase,
    BundleState,
    ClaimStatus,
    ExternalOperationStatus,
    PromptAttemptStatus,
    PromptFailureClass,
    TendrilState,
    require_autonomous_job_transition,
    require_bundle_transition,
    require_tendril_transition,
)
from nemoir.domain.validation import ValidationReport


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _claim_candidate_ids(row: sqlite3.Row) -> list[str]:
    """Read plural selected ids, falling back to the legacy singular column."""
    if row["selected_candidate_ids_json"]:
        return json.loads(row["selected_candidate_ids_json"])
    if row["selected_candidate_id"]:
        return [row["selected_candidate_id"]]
    return []


def _claim_candidates(row: sqlite3.Row) -> list[ClaimCandidate]:
    """Read plural selected snapshots, falling back to the legacy singular one."""
    if row["selected_candidates_json"]:
        return [
            ClaimCandidate.model_validate(item)
            for item in json.loads(row["selected_candidates_json"])
        ]
    if row["selected_candidate_json"]:
        return [ClaimCandidate.model_validate_json(row["selected_candidate_json"])]
    return []


def _json(value: Any) -> str:
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


class SQLiteRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self.initialize()

    def initialize(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        self._connection.executescript(schema_path.read_text(encoding="utf-8"))
        self._migrate_legacy_claims()
        self._migrate_autonomous_mode()
        self._connection.execute("DELETE FROM schema_meta")
        self._connection.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (4, ?)",
            (_iso(utc_now()),),
        )
        self._connection.commit()

    def _migrate_autonomous_mode(self) -> None:
        """Add the autonomous-capture marker to databases created before v4.

        ``CREATE TABLE IF NOT EXISTS`` will not add columns to an existing
        ``bundles`` table, so the additive migration inspects the live schema
        and adds ``autonomous_mode`` (default 0: legacy/manual) idempotently.
        Existing bundles therefore replay exactly as they did before.
        """
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(bundles)").fetchall()
        }
        if "autonomous_mode" not in columns:
            self._connection.execute(
                "ALTER TABLE bundles ADD COLUMN autonomous_mode INTEGER NOT NULL DEFAULT 0"
            )

    def _migrate_legacy_claims(self) -> None:
        """Add and backfill the plural claim-selection columns.

        ``CREATE TABLE IF NOT EXISTS`` will not add columns to an existing
        ``claims`` table, so the additive migration is performed explicitly
        and idempotently by inspecting the live schema. Databases created
        before v2 gain the legacy singular columns; every database gains the
        v3 plural columns, and any legacy singular selection is backfilled into
        a one-element plural selection without data loss.
        """
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(claims)").fetchall()
        }
        for column in (
            "selected_candidate_id",
            "selected_candidate_json",
            "selected_candidate_ids_json",
            "selected_candidates_json",
        ):
            if column not in columns:
                self._connection.execute(f"ALTER TABLE claims ADD COLUMN {column} TEXT")

        rows = self._connection.execute(
            """
            SELECT id, selected_candidate_id, selected_candidate_json,
                   selected_candidate_ids_json
            FROM claims
            """
        ).fetchall()
        for row in rows:
            if row["selected_candidate_ids_json"] is not None:
                continue
            if row["selected_candidate_id"] is not None:
                snapshot = (
                    json.loads(row["selected_candidate_json"])
                    if row["selected_candidate_json"]
                    else None
                )
                ids_json = _json([row["selected_candidate_id"]])
                candidates_json = _json([snapshot]) if snapshot is not None else _json([])
            else:
                ids_json = _json([])
                candidates_json = _json([])
            self._connection.execute(
                """
                UPDATE claims
                SET selected_candidate_ids_json = ?, selected_candidates_json = ?
                WHERE id = ?
                """,
                (ids_json, candidates_json, row["id"]),
            )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield self._connection
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def create_bundle(
        self,
        bundle: ConversationBundle,
        *,
        idempotency_key: str | None = None,
    ) -> ConversationBundle:
        """Create one capture bundle, or replay it for a repeated key.

        With an ``idempotency_key`` the creation is recorded as a lifecycle
        event, so a duplicate Discord `/tend` delivery returns the original
        bundle instead of creating (or erroring on) a second one.
        """
        try:
            with self.transaction() as connection:
                if idempotency_key is not None:
                    existing_event = connection.execute(
                        """
                        SELECT entity_id FROM lifecycle_events
                        WHERE entity_type = 'bundle' AND idempotency_key = ?
                        """,
                        (idempotency_key,),
                    ).fetchone()
                    if existing_event is not None:
                        return self.get_bundle(existing_event["entity_id"])
                connection.execute(
                    """
                    INSERT INTO bundles(
                        id, guild_id, intake_channel_id, submitter_user_id,
                        recipient_user_id, status, autonomous_mode, created_at,
                        sealed_at, claim_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bundle.id,
                        bundle.guild_id,
                        bundle.intake_channel_id,
                        bundle.submitter_user_id,
                        bundle.recipient_user_id,
                        bundle.status.value,
                        1 if bundle.autonomous_mode else 0,
                        _iso(bundle.created_at),
                        _iso(bundle.sealed_at),
                        bundle.claim_id,
                    ),
                )
                if idempotency_key is not None:
                    self._insert_event(
                        connection,
                        LifecycleEvent(
                            entity_type="bundle",
                            entity_id=bundle.id,
                            prior_state=None,
                            new_state=BundleState.CAPTURING.value,
                            actor_user_id=bundle.submitter_user_id,
                            idempotency_key=idempotency_key,
                            metadata={"autonomous": bundle.autonomous_mode},
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("A conflicting bundle or capture session already exists") from exc
        return bundle

    def get_bundle(self, bundle_id: str) -> ConversationBundle:
        row = self._connection.execute("SELECT * FROM bundles WHERE id = ?", (bundle_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Bundle not found: {bundle_id}")
        messages = self.list_source_messages(bundle_id)
        units = self.list_source_units(bundle_id)
        return ConversationBundle(
            id=row["id"],
            guild_id=row["guild_id"],
            intake_channel_id=row["intake_channel_id"],
            submitter_user_id=row["submitter_user_id"],
            recipient_user_id=row["recipient_user_id"],
            status=BundleState(row["status"]),
            autonomous_mode=bool(row["autonomous_mode"]),
            created_at=_dt(row["created_at"]),
            sealed_at=_dt(row["sealed_at"]),
            source_messages=messages,
            source_units=units,
            claim_id=row["claim_id"],
        )

    def find_open_capture(
        self, submitter_user_id: str, intake_channel_id: str
    ) -> ConversationBundle | None:
        row = self._connection.execute(
            """
            SELECT id FROM bundles
            WHERE submitter_user_id = ? AND intake_channel_id = ? AND status = 'CAPTURING'
            ORDER BY created_at DESC LIMIT 1
            """,
            (submitter_user_id, intake_channel_id),
        ).fetchone()
        return self.get_bundle(row["id"]) if row else None

    def list_pending_bundles(self, recipient_user_id: str) -> list[ConversationBundle]:
        """Bundles that await this recipient's claim (option or custom topic)."""
        rows = self._connection.execute(
            """
            SELECT id FROM bundles
            WHERE recipient_user_id = ? AND claim_id IS NULL
              AND status IN ('SEALED', 'CLAIM_OPTIONS_READY', 'CLAIM_OPTIONS_FAILED', 'AWAITING_CLAIM')
            ORDER BY created_at
            """,
            (recipient_user_id,),
        ).fetchall()
        return [self.get_bundle(row["id"]) for row in rows]

    def add_source_message(self, bundle_id: str, message: SourceMessage) -> SourceMessage:
        bundle = self.get_bundle(bundle_id)
        if bundle.status != BundleState.CAPTURING:
            raise ConflictError("Messages can only be added while a bundle is CAPTURING")
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO source_messages(
                        external_message_id, bundle_id, platform, author_user_id,
                        author_display_name, channel_id, content, source_url,
                        timestamp, ordinal
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.external_message_id,
                        bundle_id,
                        message.platform,
                        message.author_user_id,
                        message.author_display_name,
                        message.channel_id,
                        message.content,
                        message.source_url,
                        _iso(message.timestamp),
                        message.ordinal,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            existing = self._connection.execute(
                "SELECT bundle_id, content, ordinal FROM source_messages WHERE external_message_id = ?",
                (message.external_message_id,),
            ).fetchone()
            if (
                existing
                and existing["bundle_id"] == bundle_id
                and existing["content"] == message.content
                and existing["ordinal"] == message.ordinal
            ):
                return message
            raise ConflictError("Source message conflicts with existing evidence") from exc
        return message

    def list_source_messages(self, bundle_id: str) -> list[SourceMessage]:
        rows = self._connection.execute(
            "SELECT * FROM source_messages WHERE bundle_id = ? ORDER BY ordinal", (bundle_id,)
        ).fetchall()
        return [
            SourceMessage(
                platform=row["platform"],
                external_message_id=row["external_message_id"],
                author_user_id=row["author_user_id"],
                author_display_name=row["author_display_name"],
                channel_id=row["channel_id"],
                content=row["content"],
                source_url=row["source_url"],
                timestamp=_dt(row["timestamp"]),
                ordinal=row["ordinal"],
            )
            for row in rows
        ]

    def store_source_units(self, bundle_id: str, units: list[SourceUnit]) -> list[SourceUnit]:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT COUNT(*) AS count FROM source_units WHERE bundle_id = ?", (bundle_id,)
            ).fetchone()["count"]
            if existing:
                current = self.list_source_units(bundle_id)
                if [item.model_dump() for item in current] == [item.model_dump() for item in units]:
                    return current
                raise ConflictError("Stored source segmentation differs from the deterministic result")
            for unit in units:
                connection.execute(
                    """
                    INSERT INTO source_units(
                        unit_id, bundle_id, source_message_id, paragraph_ordinal,
                        exact_text, normalized_text, start_offset, end_offset
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        unit.unit_id,
                        bundle_id,
                        unit.source_message_id,
                        unit.paragraph_ordinal,
                        unit.exact_text,
                        unit.normalized_text,
                        unit.start_offset,
                        unit.end_offset,
                    ),
                )
        return units

    def list_source_units(self, bundle_id: str) -> list[SourceUnit]:
        rows = self._connection.execute(
            """
            SELECT source_units.*
            FROM source_units
            JOIN source_messages ON source_messages.external_message_id = source_units.source_message_id
            WHERE source_units.bundle_id = ?
            ORDER BY source_messages.ordinal, source_units.paragraph_ordinal
            """,
            (bundle_id,),
        ).fetchall()
        return [
            SourceUnit(
                unit_id=row["unit_id"],
                source_message_id=row["source_message_id"],
                paragraph_ordinal=row["paragraph_ordinal"],
                exact_text=row["exact_text"],
                normalized_text=row["normalized_text"],
                start_offset=row["start_offset"],
                end_offset=row["end_offset"],
            )
            for row in rows
        ]

    def transition_bundle(
        self,
        bundle_id: str,
        new_state: BundleState,
        actor_user_id: str,
        idempotency_key: str,
        metadata: dict[str, str | int | float | bool | None] | None = None,
    ) -> ConversationBundle:
        with self.transaction() as connection:
            existing_event = connection.execute(
                """
                SELECT new_state FROM lifecycle_events
                WHERE entity_type = 'bundle' AND entity_id = ? AND idempotency_key = ?
                """,
                (bundle_id, idempotency_key),
            ).fetchone()
            if existing_event:
                if existing_event["new_state"] != new_state.value:
                    raise ConflictError("Idempotency key already represents another bundle transition")
                return self.get_bundle(bundle_id)

            row = connection.execute("SELECT status FROM bundles WHERE id = ?", (bundle_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Bundle not found: {bundle_id}")
            prior = BundleState(row["status"])
            require_bundle_transition(prior, new_state)
            sealed_at = _iso(utc_now()) if new_state == BundleState.SEALED else None
            if sealed_at:
                connection.execute(
                    "UPDATE bundles SET status = ?, sealed_at = ? WHERE id = ?",
                    (new_state.value, sealed_at, bundle_id),
                )
            else:
                connection.execute(
                    "UPDATE bundles SET status = ? WHERE id = ?", (new_state.value, bundle_id)
                )
            event = LifecycleEvent(
                entity_type="bundle",
                entity_id=bundle_id,
                prior_state=prior.value,
                new_state=new_state.value,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                metadata=metadata or {},
            )
            self._insert_event(connection, event)
        return self.get_bundle(bundle_id)

    def create_claim(self, claim: Claim) -> Claim:
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO claims(
                        id, bundle_id, raw_topic, claimant_user_id, status, created_at,
                        selected_candidate_ids_json, selected_candidates_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim.id,
                        claim.bundle_id,
                        claim.raw_topic,
                        claim.claimant_user_id,
                        claim.status.value,
                        _iso(claim.created_at),
                        _json(claim.selected_candidate_ids),
                        _json(claim.selected_candidates),
                    ),
                )
                connection.execute(
                    "UPDATE bundles SET claim_id = ? WHERE id = ?", (claim.id, claim.bundle_id)
                )
        except sqlite3.IntegrityError as exc:
            existing = self.get_claim_for_bundle(claim.bundle_id)
            if (
                existing.raw_topic == claim.raw_topic
                and existing.claimant_user_id == claim.claimant_user_id
                and existing.selected_candidate_ids == claim.selected_candidate_ids
            ):
                return existing
            raise ConflictError("Bundle already has a different claim") from exc
        return claim

    def get_claim_for_bundle(self, bundle_id: str) -> Claim:
        row = self._connection.execute("SELECT * FROM claims WHERE bundle_id = ?", (bundle_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Claim not found for bundle: {bundle_id}")
        candidate_ids = _claim_candidate_ids(row)
        candidates = _claim_candidates(row)
        return Claim(
            id=row["id"],
            bundle_id=row["bundle_id"],
            raw_topic=row["raw_topic"],
            claimant_user_id=row["claimant_user_id"],
            status=ClaimStatus(row["status"]),
            created_at=_dt(row["created_at"]),
            selected_candidate_ids=candidate_ids,
            selected_candidates=candidates,
        )

    def update_claim_status(self, bundle_id: str, status: ClaimStatus) -> None:
        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE claims SET status = ? WHERE bundle_id = ?", (status.value, bundle_id)
            )
            if result.rowcount != 1:
                raise NotFoundError(f"Claim not found for bundle: {bundle_id}")

    def begin_claim_discovery_attempt(
        self,
        bundle_id: str,
        idempotency_key: str,
        prompt_version: str,
    ) -> tuple[str, bool]:
        existing = self._connection.execute(
            "SELECT id FROM claim_discovery_attempts WHERE bundle_id = ? AND idempotency_key = ?",
            (bundle_id, idempotency_key),
        ).fetchone()
        if existing:
            return existing["id"], False
        attempt_id = new_id("discovery")
        try:
            with self.transaction() as connection:
                running = connection.execute(
                    "SELECT id FROM claim_discovery_attempts WHERE bundle_id = ? AND status = 'RUNNING'",
                    (bundle_id,),
                ).fetchone()
                if running is not None:
                    raise ConflictError(
                        "A bundle may only have one running claim-discovery attempt; "
                        f"attempt {running['id']} is still RUNNING"
                    )
                connection.execute(
                    """
                    INSERT INTO claim_discovery_attempts(
                        id, bundle_id, idempotency_key, status, prompt_version, created_at
                    ) VALUES (?, ?, ?, 'RUNNING', ?, ?)
                    """,
                    (attempt_id, bundle_id, idempotency_key, prompt_version, _iso(utc_now())),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "A bundle may only have one running claim-discovery attempt (database constraint)"
            ) from exc
        return attempt_id, True

    def persist_claim_discovery_success(
        self,
        attempt_id: str,
        bundle_id: str,
        candidate_set: ClaimCandidateSet,
        receipt: ProviderReceipt,
        report: ValidationReport,
    ) -> list[ClaimCandidate]:
        if not report.valid:
            raise ConflictError("Cannot persist invalid claim candidates as a success")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT status FROM claim_discovery_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if existing is None:
                raise NotFoundError(f"Claim-discovery attempt not found: {attempt_id}")
            if existing["status"] == "SUCCEEDED":
                return self.list_claim_candidates(bundle_id)
            for candidate in candidate_set.candidates:
                connection.execute(
                    """
                    INSERT INTO claim_candidates(
                        bundle_id, candidate_id, display_order, title, summary,
                        evidence_json, generated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bundle_id,
                        candidate.candidate_id,
                        candidate.display_order,
                        candidate.title,
                        candidate.summary,
                        _json(candidate.evidence),
                        _iso(utc_now()),
                    ),
                )
            connection.execute(
                "INSERT INTO claim_discovery_receipts(attempt_id, receipt_json) VALUES (?, ?)",
                (attempt_id, _json(receipt)),
            )
            connection.execute(
                """
                UPDATE claim_discovery_attempts
                SET status = 'SUCCEEDED', provider = ?, model = ?, raw_response = ?,
                    candidates_json = ?, validation_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    receipt.provider,
                    receipt.model,
                    None,
                    _json(candidate_set),
                    _json(report),
                    _iso(utc_now()),
                    attempt_id,
                ),
            )
        return self.list_claim_candidates(bundle_id)

    def persist_claim_discovery_failure(
        self,
        attempt_id: str,
        status: str,
        report: ValidationReport,
        receipt: ProviderReceipt | None = None,
        raw_response: str | None = None,
    ) -> None:
        if status != "FAILED":
            raise ValueError("Unsupported claim-discovery failure status")
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE claim_discovery_attempts
                SET status = ?, provider = ?, model = ?, raw_response = ?,
                    validation_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    receipt.provider if receipt else None,
                    receipt.model if receipt else None,
                    raw_response,
                    _json(report),
                    _iso(utc_now()),
                    attempt_id,
                ),
            )
            if receipt:
                connection.execute(
                    "INSERT OR REPLACE INTO claim_discovery_receipts(attempt_id, receipt_json) VALUES (?, ?)",
                    (attempt_id, _json(receipt)),
                )

    def get_claim_discovery_attempt(self, attempt_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM claim_discovery_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Claim-discovery attempt not found: {attempt_id}")
        return row

    def list_claim_candidates(self, bundle_id: str) -> list[ClaimCandidate]:
        rows = self._connection.execute(
            """
            SELECT candidate_id, display_order, title, summary, evidence_json
            FROM claim_candidates WHERE bundle_id = ? ORDER BY display_order
            """,
            (bundle_id,),
        ).fetchall()
        return [
            ClaimCandidate(
                candidate_id=row["candidate_id"],
                display_order=row["display_order"],
                title=row["title"],
                summary=row["summary"],
                evidence=[
                    SourceFragment.model_validate(item)
                    for item in json.loads(row["evidence_json"])
                ],
            )
            for row in rows
        ]

    def get_candidate_by_option(self, bundle_id: str, option_number: int) -> ClaimCandidate:
        row = self._connection.execute(
            "SELECT * FROM claim_candidates WHERE bundle_id = ? AND display_order = ?",
            (bundle_id, option_number),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"Claim option {option_number} is not available for bundle {bundle_id}"
            )
        return ClaimCandidate(
            candidate_id=row["candidate_id"],
            display_order=row["display_order"],
            title=row["title"],
            summary=row["summary"],
            evidence=[
                SourceFragment.model_validate(item) for item in json.loads(row["evidence_json"])
            ],
        )

    def begin_prompt_attempt(
        self,
        *,
        user_id: str,
        guild_id: str,
        channel_id: str,
        question: str,
        idempotency_key: str,
    ) -> tuple[str, bool]:
        """Reserve one RUNNING prompt attempt, or replay an existing key.

        Returns ``(attempt_id, created)``. A repeated idempotency key returns
        the existing attempt with ``created=False`` so a duplicate Discord
        delivery cannot start a second model call. A second RUNNING attempt
        for the same user is refused inside the transaction and, as a
        backstop, by the partial unique index.
        """
        existing = self._connection.execute(
            "SELECT id FROM prompt_attempts WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            return existing["id"], False
        attempt_id = new_id("prompt")
        try:
            with self.transaction() as connection:
                running = connection.execute(
                    "SELECT id FROM prompt_attempts WHERE user_id = ? AND status = 'RUNNING'",
                    (user_id,),
                ).fetchone()
                if running is not None:
                    raise ConflictError(
                        "A user may only have one running prompt; "
                        f"attempt {running['id']} is still RUNNING"
                    )
                connection.execute(
                    """
                    INSERT INTO prompt_attempts(
                        id, idempotency_key, user_id, guild_id, channel_id,
                        question, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?)
                    """,
                    (
                        attempt_id,
                        idempotency_key,
                        user_id,
                        guild_id,
                        channel_id,
                        question,
                        _iso(utc_now()),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "A user may only have one running prompt (database constraint)"
            ) from exc
        return attempt_id, True

    def persist_prompt_success(
        self,
        attempt_id: str,
        response_text: str,
        receipt: ProviderReceipt,
    ) -> None:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT status FROM prompt_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if existing is None:
                raise NotFoundError(f"Prompt attempt not found: {attempt_id}")
            if existing["status"] == "SUCCEEDED":
                return
            connection.execute(
                """
                UPDATE prompt_attempts
                SET status = 'SUCCEEDED', provider = ?, model = ?,
                    receipt_json = ?, response_text = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    receipt.provider,
                    receipt.model,
                    _json(receipt),
                    response_text,
                    _iso(utc_now()),
                    attempt_id,
                ),
            )

    def persist_prompt_failure(
        self,
        attempt_id: str,
        failure_classification: PromptFailureClass,
        receipt: ProviderReceipt | None = None,
    ) -> None:
        """Record a failed attempt with only a safe classification.

        Raw exception text and credentials are never stored; the receipt is
        retained when a paid/observed response existed before the failure.
        """
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT status FROM prompt_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if existing is None:
                raise NotFoundError(f"Prompt attempt not found: {attempt_id}")
            if existing["status"] == "SUCCEEDED":
                return
            connection.execute(
                """
                UPDATE prompt_attempts
                SET status = 'FAILED', provider = ?, model = ?, receipt_json = ?,
                    failure_classification = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    receipt.provider if receipt else None,
                    receipt.model if receipt else None,
                    _json(receipt) if receipt else None,
                    failure_classification.value,
                    _iso(utc_now()),
                    attempt_id,
                ),
            )

    def get_prompt_attempt(self, attempt_id: str) -> PromptAttempt:
        row = self._connection.execute(
            "SELECT * FROM prompt_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Prompt attempt not found: {attempt_id}")
        return self._row_to_prompt_attempt(row)

    def get_prompt_attempt_by_key(self, idempotency_key: str) -> PromptAttempt:
        row = self._connection.execute(
            "SELECT * FROM prompt_attempts WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Prompt attempt not found for key: {idempotency_key}")
        return self._row_to_prompt_attempt(row)

    def create_autonomous_job(
        self,
        bundle_id: str,
        *,
        idempotency_key: str,
        pending_attempt_key: str,
    ) -> tuple[AutonomousJob, bool]:
        """Create (or replay) the one durable autonomous job for a bundle.

        Duplicate Discord events resolve to the existing job (created=False),
        so they can never produce a second job or a second provider attempt.
        """
        existing = self._connection.execute(
            "SELECT * FROM autonomous_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            return self._row_to_autonomous_job(existing), False
        job = AutonomousJob(
            id=new_id("autojob"),
            bundle_id=bundle_id,
            idempotency_key=idempotency_key,
            phase=AutonomousJobPhase.QUEUED,
            pending_attempt_key=pending_attempt_key,
        )
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO autonomous_jobs(
                        id, bundle_id, idempotency_key, phase, pending_attempt_key,
                        provider, model, last_notification, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.id,
                        job.bundle_id,
                        job.idempotency_key,
                        job.phase.value,
                        job.pending_attempt_key,
                        job.provider,
                        job.model,
                        job.last_notification,
                        _iso(job.created_at),
                        _iso(job.updated_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            # One job per bundle: a second seal event for the same bundle with a
            # different key replays the existing job instead of failing.
            existing = self._connection.execute(
                "SELECT * FROM autonomous_jobs WHERE bundle_id = ?", (bundle_id,)
            ).fetchone()
            if existing is not None:
                return self._row_to_autonomous_job(existing), False
            raise ConflictError("A conflicting autonomous job already exists") from exc
        return job, True

    def get_autonomous_job(self, bundle_id: str) -> AutonomousJob:
        row = self._connection.execute(
            "SELECT * FROM autonomous_jobs WHERE bundle_id = ?", (bundle_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Autonomous job not found for bundle: {bundle_id}")
        return self._row_to_autonomous_job(row)

    def list_autonomous_jobs(
        self, phases: set[AutonomousJobPhase] | None = None
    ) -> list[AutonomousJob]:
        clauses: list[str] = []
        params: list[Any] = []
        if phases:
            placeholders = ",".join("?" for _ in phases)
            clauses.append(f"phase IN ({placeholders})")
            params.extend(item.value for item in phases)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM autonomous_jobs {where} ORDER BY created_at, id", params
        ).fetchall()
        return [self._row_to_autonomous_job(row) for row in rows]

    def begin_autonomous_attempt(
        self,
        job_id: str,
        bundle_id: str,
        idempotency_key: str,
    ) -> tuple[str, bool]:
        """Create one RUNNING attempt and move the job QUEUED -> REQUEST_STARTED.

        The attempt insert and the phase flip commit in one transaction, which
        is the paid-request crash boundary: a job found in REQUEST_STARTED
        after a restart may already have sent the request and must become
        REQUEST_AMBIGUOUS (never retried automatically); a QUEUED job provably
        never sent one. A repeated key replays the existing attempt.
        """
        existing = self._connection.execute(
            "SELECT id FROM autonomous_attempts WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            return existing["id"], False
        attempt_id = new_id("autosort")
        try:
            with self.transaction() as connection:
                running = connection.execute(
                    "SELECT id FROM autonomous_attempts WHERE bundle_id = ? AND status = 'RUNNING'",
                    (bundle_id,),
                ).fetchone()
                if running is not None:
                    raise ConflictError(
                        "A bundle may only have one running autonomous-sort attempt; "
                        f"attempt {running['id']} is still RUNNING"
                    )
                job_row = connection.execute(
                    "SELECT phase FROM autonomous_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if job_row is None:
                    raise NotFoundError(f"Autonomous job not found: {job_id}")
                require_autonomous_job_transition(
                    AutonomousJobPhase(job_row["phase"]),
                    AutonomousJobPhase.REQUEST_STARTED,
                )
                connection.execute(
                    """
                    INSERT INTO autonomous_attempts(
                        id, job_id, bundle_id, idempotency_key, status,
                        prompt_version, created_at
                    ) VALUES (?, ?, ?, ?, 'RUNNING', 'nemoir-autonomous-sort-v1', ?)
                    """,
                    (attempt_id, job_id, bundle_id, idempotency_key, _iso(utc_now())),
                )
                connection.execute(
                    """
                    UPDATE autonomous_jobs
                    SET phase = 'REQUEST_STARTED', pending_attempt_key = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (_iso(utc_now()), job_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "A bundle may only have one running autonomous-sort attempt (database constraint)"
            ) from exc
        return attempt_id, True

    def persist_autonomous_success(
        self,
        *,
        attempt_id: str,
        job_id: str,
        bundle_id: str,
        result: AutonomousSortResult,
        receipt: ProviderReceipt,
        report: ValidationReport,
    ) -> list[Tendril]:
        """Persist a validated autonomous sort as ordinary tendrils.

        One transaction writes: a marker analysis-attempt row (so the existing
        tendril/coverage/receipt schema keeps its foreign keys), the topic
        tendrils, the full-source coverage entries, the provider receipt, the
        successful attempt record, and the job phase RESULT_PERSISTED. No claim
        row is created. An invalid report is refused before any write.
        """
        if not report.valid:
            raise ConflictError("Cannot persist an invalid autonomous sort as a success")
        tendrils: list[Tendril] = []
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT status FROM autonomous_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if existing is None:
                raise NotFoundError(f"Autonomous attempt not found: {attempt_id}")
            if existing["status"] == "SUCCEEDED":
                return self.list_tendrils(bundle_id=bundle_id)
            marker_id = new_id("analysis")
            claim_json = _json(
                ClaimAnalysis(
                    raw_topic="Autonomous sort",
                    normalized_label="Autonomous sort",
                    matching_fragments=[],
                    confidence=0.0,
                    rationale=(
                        "Autonomous recipient-free bundle: no claim row exists; "
                        "topics are validated autonomous-sort output."
                    ),
                )
            )
            connection.execute(
                """
                INSERT INTO analysis_attempts(
                    id, bundle_id, idempotency_key, status, prompt_version,
                    provider, model, raw_response, claim_analysis_json,
                    validation_json, created_at, completed_at
                ) VALUES (?, ?, ?, 'SUCCEEDED', 'nemoir-autonomous-sort-v1', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    marker_id,
                    bundle_id,
                    f"autonomous:{attempt_id}",
                    receipt.provider,
                    receipt.model,
                    None,
                    claim_json,
                    _json(report),
                    _iso(utc_now()),
                    _iso(utc_now()),
                ),
            )
            for topic in result.topics:
                tendril = Tendril(
                    id=new_id("tendril"),
                    bundle_id=bundle_id,
                    title=topic.title,
                    description=topic.summary,
                    type=topic.tendril_type,
                    actionability=topic.actionability,
                    evidence=topic.evidence,
                    why_open=topic.why_open,
                    suggested_habitat_slug=topic.suggested_habitat_slug,
                    habitat_reasoning=None,
                    confidence=topic.confidence,
                )
                connection.execute(
                    """
                    INSERT INTO tendrils(
                        id, attempt_id, bundle_id, client_id, title, description, type,
                        actionability, evidence_json, why_open, suggested_habitat_slug,
                        habitat_reasoning, confidence, status, routed_platform,
                        routed_external_id, snoozed_until, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tendril.id,
                        marker_id,
                        bundle_id,
                        topic.provider_client_id,
                        tendril.title,
                        tendril.description,
                        tendril.type.value,
                        tendril.actionability.value,
                        _json(tendril.evidence),
                        tendril.why_open,
                        tendril.suggested_habitat_slug,
                        None,
                        tendril.confidence,
                        tendril.status.value,
                        None,
                        None,
                        None,
                        _iso(tendril.created_at),
                        _iso(tendril.updated_at),
                    ),
                )
                tendrils.append(tendril)
            for entry in result.coverage:
                connection.execute(
                    """
                    INSERT INTO coverage_entries(
                        attempt_id, unit_id, source_message_id, classification,
                        tendril_client_ids_json, reason, confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        marker_id,
                        entry.unit_id,
                        entry.source_message_id,
                        entry.classification.value,
                        _json(entry.tendril_client_ids),
                        entry.reason,
                        entry.confidence,
                    ),
                )
            connection.execute(
                "INSERT INTO provider_receipts(attempt_id, receipt_json) VALUES (?, ?)",
                (marker_id, _json(receipt)),
            )
            job_row = connection.execute(
                "SELECT phase FROM autonomous_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise NotFoundError(f"Autonomous job not found: {job_id}")
            require_autonomous_job_transition(
                AutonomousJobPhase(job_row["phase"]),
                AutonomousJobPhase.RESULT_PERSISTED,
            )
            connection.execute(
                """
                UPDATE autonomous_attempts
                SET status = 'SUCCEEDED', provider = ?, model = ?,
                    result_json = ?, validation_json = ?, receipt_json = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (
                    receipt.provider,
                    receipt.model,
                    _json(result),
                    _json(report),
                    _json(receipt),
                    _iso(utc_now()),
                    attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE autonomous_jobs
                SET phase = 'RESULT_PERSISTED', provider = ?, model = ?, updated_at = ?
                WHERE id = ?
                """,
                (receipt.provider, receipt.model, _iso(utc_now()), job_id),
            )
        return tendrils

    def persist_autonomous_failure(
        self,
        *,
        attempt_id: str,
        job_id: str,
        report: ValidationReport,
        receipt: ProviderReceipt | None = None,
        raw_response: str | None = None,
    ) -> None:
        """Record a failed autonomous attempt with safe receipts/raw output.

        Creates no tendrils and no claim row; the job moves to FAILED so only a
        deliberate owner/admin retry (fresh idempotency key) can continue.
        """
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT status FROM autonomous_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if existing is None:
                raise NotFoundError(f"Autonomous attempt not found: {attempt_id}")
            if existing["status"] == "SUCCEEDED":
                return
            job_row = connection.execute(
                "SELECT phase FROM autonomous_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise NotFoundError(f"Autonomous job not found: {job_id}")
            require_autonomous_job_transition(
                AutonomousJobPhase(job_row["phase"]),
                AutonomousJobPhase.FAILED,
            )
            connection.execute(
                """
                UPDATE autonomous_attempts
                SET status = 'FAILED', provider = ?, model = ?, raw_response = ?,
                    validation_json = ?, receipt_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    receipt.provider if receipt else None,
                    receipt.model if receipt else None,
                    raw_response,
                    _json(report),
                    _json(receipt) if receipt else None,
                    _iso(utc_now()),
                    attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE autonomous_jobs
                SET phase = 'FAILED', provider = ?, model = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    receipt.provider if receipt else None,
                    receipt.model if receipt else None,
                    _iso(utc_now()),
                    job_id,
                ),
            )

    def mark_autonomous_attempt_ambiguous(self, job_id: str, attempt_id: str) -> None:
        """Crash boundary: a resumed REQUEST_STARTED attempt becomes AMBIGUOUS.

        The model request may already have been sent, so no automatic second
        request is ever made; only a deliberate owner/admin retry with a fresh
        idempotency key may continue.
        """
        with self.transaction() as connection:
            attempt = connection.execute(
                "SELECT status FROM autonomous_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise NotFoundError(f"Autonomous attempt not found: {attempt_id}")
            if attempt["status"] == "RUNNING":
                connection.execute(
                    """
                    UPDATE autonomous_attempts
                    SET status = 'AMBIGUOUS', completed_at = ?
                    WHERE id = ?
                    """,
                    (_iso(utc_now()), attempt_id),
                )
            job_row = connection.execute(
                "SELECT phase FROM autonomous_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise NotFoundError(f"Autonomous job not found: {job_id}")
            require_autonomous_job_transition(
                AutonomousJobPhase(job_row["phase"]),
                AutonomousJobPhase.REQUEST_AMBIGUOUS,
            )
            connection.execute(
                """
                UPDATE autonomous_jobs
                SET phase = 'REQUEST_AMBIGUOUS', pending_attempt_key = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (_iso(utc_now()), job_id),
            )

    def retry_autonomous_job(self, job_id: str, pending_attempt_key: str) -> AutonomousJob:
        """Requeue a FAILED/REQUEST_AMBIGUOUS job with a fresh attempt key.

        The source is never resealed or mutated; any stale RUNNING attempt is
        closed as AMBIGUOUS so the one-running-attempt invariant holds.
        """
        with self.transaction() as connection:
            job_row = connection.execute(
                "SELECT phase, bundle_id FROM autonomous_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise NotFoundError(f"Autonomous job not found: {job_id}")
            bundle_id = job_row["bundle_id"]
            prior = AutonomousJobPhase(job_row["phase"])
            if prior not in {AutonomousJobPhase.FAILED, AutonomousJobPhase.REQUEST_AMBIGUOUS}:
                raise ConflictError(
                    f"Autonomous retry is available only for FAILED or REQUEST_AMBIGUOUS "
                    f"jobs, not {prior.value}"
                )
            connection.execute(
                """
                UPDATE autonomous_attempts SET status = 'AMBIGUOUS', completed_at = ?
                WHERE job_id = ? AND status = 'RUNNING'
                """,
                (_iso(utc_now()), job_id),
            )
            connection.execute(
                """
                UPDATE autonomous_jobs
                SET phase = 'QUEUED', pending_attempt_key = ?,
                    last_notification = NULL, updated_at = ?
                WHERE id = ?
                """,
                (pending_attempt_key, _iso(utc_now()), job_id),
            )
        return self.get_autonomous_job(bundle_id)

    def update_autonomous_job_phase(
        self, job_id: str, phase: AutonomousJobPhase
    ) -> AutonomousJob:
        with self.transaction() as connection:
            job_row = connection.execute(
                "SELECT phase, bundle_id FROM autonomous_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise NotFoundError(f"Autonomous job not found: {job_id}")
            bundle_id = job_row["bundle_id"]
            prior = AutonomousJobPhase(job_row["phase"])
            if prior == phase:
                return self.get_autonomous_job(bundle_id)
            require_autonomous_job_transition(prior, phase)
            connection.execute(
                "UPDATE autonomous_jobs SET phase = ?, updated_at = ? WHERE id = ?",
                (phase.value, _iso(utc_now()), job_id),
            )
        return self.get_autonomous_job(bundle_id)

    def record_autonomous_job_notification(self, job_id: str, phase: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE autonomous_jobs SET last_notification = ?, updated_at = ? WHERE id = ?",
                (phase, _iso(utc_now()), job_id),
            )

    def get_autonomous_attempt(self, attempt_id: str) -> AutonomousAttempt:
        row = self._connection.execute(
            "SELECT * FROM autonomous_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Autonomous attempt not found: {attempt_id}")
        return self._row_to_autonomous_attempt(row)

    def list_autonomous_attempts(self, bundle_id: str) -> list[AutonomousAttempt]:
        rows = self._connection.execute(
            "SELECT * FROM autonomous_attempts WHERE bundle_id = ? ORDER BY created_at, id",
            (bundle_id,),
        ).fetchall()
        return [self._row_to_autonomous_attempt(row) for row in rows]

    def has_unresolved_publish_operations(self, bundle_id: str) -> bool:
        """True while any tendril of the bundle has an unresolved publish op."""
        row = self._connection.execute(
            """
            SELECT 1 FROM external_operations
            WHERE tendril_id IN (SELECT id FROM tendrils WHERE bundle_id = ?)
              AND operation_type = 'habitat_create'
              AND status IN ('PENDING', 'EXTERNAL_SUCCEEDED', 'NEEDS_RECONCILIATION')
            LIMIT 1
            """,
            (bundle_id,),
        ).fetchone()
        return row is not None

    def find_bundle_by_event_key(
        self, entity_type: str, idempotency_key: str
    ) -> ConversationBundle | None:
        """Return the bundle an idempotency-keyed lifecycle event created/acted on."""
        event = self.find_lifecycle_event(entity_type, idempotency_key)
        if event is None:
            return None
        return self.get_bundle(event["entity_id"])

    @staticmethod
    def _row_to_autonomous_job(row: sqlite3.Row) -> AutonomousJob:
        return AutonomousJob(
            id=row["id"],
            bundle_id=row["bundle_id"],
            idempotency_key=row["idempotency_key"],
            phase=AutonomousJobPhase(row["phase"]),
            pending_attempt_key=row["pending_attempt_key"],
            provider=row["provider"],
            model=row["model"],
            last_notification=row["last_notification"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _row_to_autonomous_attempt(row: sqlite3.Row) -> AutonomousAttempt:
        return AutonomousAttempt(
            id=row["id"],
            job_id=row["job_id"],
            bundle_id=row["bundle_id"],
            idempotency_key=row["idempotency_key"],
            status=AutonomousAttemptStatus(row["status"]),
            prompt_version=row["prompt_version"],
            provider=row["provider"],
            model=row["model"],
            raw_response=row["raw_response"],
            result_json=row["result_json"],
            validation_json=row["validation_json"],
            receipt=(
                ProviderReceipt.model_validate_json(row["receipt_json"])
                if row["receipt_json"]
                else None
            ),
            created_at=_dt(row["created_at"]),
            completed_at=_dt(row["completed_at"]),
        )

    def begin_analysis_attempt(
        self,
        bundle_id: str,
        idempotency_key: str,
        prompt_version: str,
    ) -> tuple[str, bool]:
        existing = self._connection.execute(
            "SELECT id FROM analysis_attempts WHERE bundle_id = ? AND idempotency_key = ?",
            (bundle_id, idempotency_key),
        ).fetchone()
        if existing:
            return existing["id"], False
        attempt_id = new_id("analysis")
        try:
            with self.transaction() as connection:
                running = connection.execute(
                    "SELECT id FROM analysis_attempts WHERE bundle_id = ? AND status = 'RUNNING'",
                    (bundle_id,),
                ).fetchone()
                if running is not None:
                    raise ConflictError(
                        "A bundle may only have one running analysis attempt; "
                        f"attempt {running['id']} is still RUNNING"
                    )
                connection.execute(
                    """
                    INSERT INTO analysis_attempts(
                        id, bundle_id, idempotency_key, status, prompt_version, created_at
                    ) VALUES (?, ?, ?, 'RUNNING', ?, ?)
                    """,
                    (attempt_id, bundle_id, idempotency_key, prompt_version, _iso(utc_now())),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "A bundle may only have one running analysis attempt (database constraint)"
            ) from exc
        return attempt_id, True

    def persist_analysis_success(
        self,
        attempt_id: str,
        bundle_id: str,
        result: AnalysisResult,
        receipt: ProviderReceipt,
        report: ValidationReport,
    ) -> list[Tendril]:
        if not report.valid:
            raise ConflictError("Cannot persist invalid analysis as a success")
        tendrils: list[Tendril] = []
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT status FROM analysis_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if existing is None:
                raise NotFoundError(f"Analysis attempt not found: {attempt_id}")
            if existing["status"] == "SUCCEEDED":
                return self.list_tendrils(bundle_id=bundle_id)
            for candidate in result.tendrils:
                tendril = Tendril(
                    id=new_id("tendril"),
                    bundle_id=bundle_id,
                    title=candidate.title,
                    description=candidate.description,
                    type=candidate.type,
                    actionability=candidate.actionability,
                    evidence=candidate.evidence,
                    why_open=candidate.why_open,
                    suggested_habitat_slug=candidate.suggested_habitat_slug,
                    habitat_reasoning=candidate.habitat_reasoning,
                    confidence=candidate.confidence,
                )
                connection.execute(
                    """
                    INSERT INTO tendrils(
                        id, attempt_id, bundle_id, client_id, title, description, type,
                        actionability, evidence_json, why_open, suggested_habitat_slug,
                        habitat_reasoning, confidence, status, routed_platform,
                        routed_external_id, snoozed_until, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tendril.id,
                        attempt_id,
                        bundle_id,
                        candidate.client_id,
                        tendril.title,
                        tendril.description,
                        tendril.type.value,
                        tendril.actionability.value,
                        _json(tendril.evidence),
                        tendril.why_open,
                        tendril.suggested_habitat_slug,
                        tendril.habitat_reasoning,
                        tendril.confidence,
                        tendril.status.value,
                        None,
                        None,
                        None,
                        _iso(tendril.created_at),
                        _iso(tendril.updated_at),
                    ),
                )
                tendrils.append(tendril)
            for entry in result.coverage:
                connection.execute(
                    """
                    INSERT INTO coverage_entries(
                        attempt_id, unit_id, source_message_id, classification,
                        tendril_client_ids_json, reason, confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        entry.unit_id,
                        entry.source_message_id,
                        entry.classification.value,
                        _json(entry.tendril_client_ids),
                        entry.reason,
                        entry.confidence,
                    ),
                )
            for ordinal, description in enumerate(result.unresolved):
                connection.execute(
                    "INSERT INTO unresolved_items(attempt_id, ordinal, description) VALUES (?, ?, ?)",
                    (attempt_id, ordinal, description),
                )
            connection.execute(
                "INSERT INTO provider_receipts(attempt_id, receipt_json) VALUES (?, ?)",
                (attempt_id, _json(receipt)),
            )
            connection.execute(
                """
                UPDATE analysis_attempts
                SET status = 'SUCCEEDED', provider = ?, model = ?, raw_response = ?,
                    claim_analysis_json = ?, validation_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    receipt.provider,
                    receipt.model,
                    None,
                    _json(result.claim),
                    _json(report),
                    _iso(utc_now()),
                    attempt_id,
                ),
            )
        return tendrils

    def persist_analysis_failure(
        self,
        attempt_id: str,
        status: str,
        report: ValidationReport,
        receipt: ProviderReceipt | None = None,
        raw_response: str | None = None,
    ) -> None:
        if status not in {"FAILED", "NEEDS_REVIEW"}:
            raise ValueError("Unsupported failure status")
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE analysis_attempts
                SET status = ?, provider = ?, model = ?, raw_response = ?,
                    validation_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    receipt.provider if receipt else None,
                    receipt.model if receipt else None,
                    raw_response,
                    _json(report),
                    _iso(utc_now()),
                    attempt_id,
                ),
            )
            if receipt:
                connection.execute(
                    "INSERT OR REPLACE INTO provider_receipts(attempt_id, receipt_json) VALUES (?, ?)",
                    (attempt_id, _json(receipt)),
                )

    def latest_attempt(self, bundle_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM analysis_attempts WHERE bundle_id = ? ORDER BY created_at DESC LIMIT 1",
            (bundle_id,),
        ).fetchone()

    def get_analysis_attempt(self, attempt_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM analysis_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Analysis attempt not found: {attempt_id}")
        return row

    def get_review(self, bundle_id: str) -> ReviewResult:
        attempt = self._connection.execute(
            """
            SELECT * FROM analysis_attempts
            WHERE bundle_id = ? AND status = 'SUCCEEDED'
            ORDER BY created_at DESC LIMIT 1
            """,
            (bundle_id,),
        ).fetchone()
        if attempt is None:
            raise NotFoundError(f"No validated analysis exists for bundle: {bundle_id}")
        bundle = self.get_bundle(bundle_id)
        coverage = self._load_coverage(attempt["id"])
        receipt_row = self._connection.execute(
            "SELECT receipt_json FROM provider_receipts WHERE attempt_id = ?", (attempt["id"],)
        ).fetchone()
        unresolved_rows = self._connection.execute(
            "SELECT description FROM unresolved_items WHERE attempt_id = ? ORDER BY ordinal",
            (attempt["id"],),
        ).fetchall()
        return ReviewResult(
            bundle_id=bundle_id,
            bundle_status=bundle.status,
            claim=json.loads(attempt["claim_analysis_json"]),
            tendrils=self.list_tendrils(bundle_id=bundle_id),
            coverage=coverage,
            unresolved=[row["description"] for row in unresolved_rows],
            receipt=ProviderReceipt.model_validate_json(receipt_row["receipt_json"]),
        )

    def _load_coverage(self, attempt_id: str) -> list[CoverageEntry]:
        rows = self._connection.execute(
            "SELECT * FROM coverage_entries WHERE attempt_id = ? ORDER BY unit_id", (attempt_id,)
        ).fetchall()
        return [
            CoverageEntry(
                source_message_id=row["source_message_id"],
                unit_id=row["unit_id"],
                classification=row["classification"],
                tendril_client_ids=json.loads(row["tendril_client_ids_json"]),
                reason=row["reason"],
                confidence=row["confidence"],
            )
            for row in rows
        ]

    def list_tendrils(
        self,
        bundle_id: str | None = None,
        statuses: set[TendrilState] | None = None,
    ) -> list[Tendril]:
        clauses: list[str] = []
        params: list[Any] = []
        if bundle_id:
            clauses.append("bundle_id = ?")
            params.append(bundle_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(item.value for item in statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM tendrils {where} ORDER BY created_at, id", params
        ).fetchall()
        return [self._row_to_tendril(row) for row in rows]

    def get_tendril(self, tendril_id: str) -> Tendril:
        row = self._connection.execute("SELECT * FROM tendrils WHERE id = ?", (tendril_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Tendril not found: {tendril_id}")
        return self._row_to_tendril(row)

    def find_lifecycle_event(
        self, entity_type: str, idempotency_key: str
    ) -> sqlite3.Row | None:
        """Return the first lifecycle event written for an idempotency key.

        Used by dynamic-selection operations (for example pull, whose target
        entity is not part of the caller's request) so a repeated interaction
        replays the original outcome instead of selecting a new entity.
        """
        return self._connection.execute(
            """
            SELECT * FROM lifecycle_events
            WHERE entity_type = ? AND idempotency_key = ?
            ORDER BY timestamp LIMIT 1
            """,
            (entity_type, idempotency_key),
        ).fetchone()

    def transition_tendril(
        self,
        tendril_id: str,
        new_state: TendrilState,
        actor_user_id: str,
        idempotency_key: str,
        *,
        snoozed_until: datetime | None = None,
        metadata: dict[str, str | int | float | bool | None] | None = None,
    ) -> Tendril:
        with self.transaction() as connection:
            existing_event = connection.execute(
                """
                SELECT new_state FROM lifecycle_events
                WHERE entity_type = 'tendril' AND entity_id = ? AND idempotency_key = ?
                """,
                (tendril_id, idempotency_key),
            ).fetchone()
            if existing_event:
                if existing_event["new_state"] != new_state.value:
                    raise ConflictError("Idempotency key already represents another tendril transition")
                return self.get_tendril(tendril_id)
            row = connection.execute("SELECT status FROM tendrils WHERE id = ?", (tendril_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Tendril not found: {tendril_id}")
            prior = TendrilState(row["status"])
            require_tendril_transition(prior, new_state)
            connection.execute(
                """
                UPDATE tendrils SET status = ?, snoozed_until = ?, updated_at = ? WHERE id = ?
                """,
                (new_state.value, _iso(snoozed_until), _iso(utc_now()), tendril_id),
            )
            event = LifecycleEvent(
                entity_type="tendril",
                entity_id=tendril_id,
                prior_state=prior.value,
                new_state=new_state.value,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                metadata=metadata or {},
            )
            self._insert_event(connection, event)
        return self.get_tendril(tendril_id)

    def record_route(
        self,
        tendril_id: str,
        platform: str,
        external_destination_id: str,
        external_message_id: str | None,
        actor_user_id: str,
        idempotency_key: str,
    ) -> Tendril:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM routes WHERE idempotency_key = ? OR tendril_id = ?",
                (idempotency_key, tendril_id),
            ).fetchone()
            if existing:
                if (
                    existing["tendril_id"] == tendril_id
                    and existing["platform"] == platform
                    and existing["external_destination_id"] == external_destination_id
                ):
                    return self.get_tendril(tendril_id)
                raise ConflictError("Tendril or idempotency key is already routed elsewhere")
            row = connection.execute("SELECT status FROM tendrils WHERE id = ?", (tendril_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Tendril not found: {tendril_id}")
            prior = TendrilState(row["status"])
            require_tendril_transition(prior, TendrilState.ROUTED)
            connection.execute(
                """
                INSERT INTO routes(
                    id, tendril_id, platform, external_destination_id,
                    external_message_id, actor_user_id, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("route"),
                    tendril_id,
                    platform,
                    external_destination_id,
                    external_message_id,
                    actor_user_id,
                    idempotency_key,
                    _iso(utc_now()),
                ),
            )
            connection.execute(
                """
                UPDATE tendrils SET status = 'ROUTED', routed_platform = ?,
                    routed_external_id = ?, updated_at = ? WHERE id = ?
                """,
                (platform, external_destination_id, _iso(utc_now()), tendril_id),
            )
            self._insert_event(
                connection,
                LifecycleEvent(
                    entity_type="tendril",
                    entity_id=tendril_id,
                    prior_state=prior.value,
                    new_state=TendrilState.ROUTED.value,
                    actor_user_id=actor_user_id,
                    idempotency_key=idempotency_key,
                    metadata={"platform": platform, "destination": external_destination_id},
                ),
            )
        return self.get_tendril(tendril_id)

    def get_route_for_tendril(self, tendril_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM routes WHERE tendril_id = ?", (tendril_id,)
        ).fetchone()

    def reserve_external_operation(
        self,
        *,
        operation_type: str,
        idempotency_key: str,
        platform: str,
        tendril_id: str,
        actor_user_id: str,
        external_destination_id: str | None = None,
        metadata: dict[str, str | int | float | bool | None] | None = None,
    ) -> tuple[ExternalOperation, bool]:
        """Reserve an external side effect before it happens.

        Returns (operation, created). A reservation that already exists is
        returned with created=False; the caller decides how to continue based
        on its status and idempotency key.
        """
        existing = self._connection.execute(
            "SELECT * FROM external_operations WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            return self._row_to_external_operation(existing), False
        operation = ExternalOperation(
            id=new_id("op"),
            operation_type=operation_type,  # type: ignore[arg-type]
            idempotency_key=idempotency_key,
            status=ExternalOperationStatus.PENDING,
            platform=platform,
            tendril_id=tendril_id,
            external_destination_id=external_destination_id,
            actor_user_id=actor_user_id,
            metadata=metadata or {},
        )
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO external_operations(
                        id, operation_type, idempotency_key, status, platform,
                        tendril_id, external_destination_id, external_message_id,
                        actor_user_id, created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation.id,
                        operation.operation_type,
                        operation.idempotency_key,
                        operation.status.value,
                        operation.platform,
                        operation.tendril_id,
                        operation.external_destination_id,
                        None,
                        operation.actor_user_id,
                        _iso(operation.created_at),
                        _iso(operation.updated_at),
                        _json(operation.metadata),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            unresolved = self._connection.execute(
                """
                SELECT * FROM external_operations
                WHERE tendril_id = ? AND operation_type = ?
                  AND status IN ('PENDING', 'EXTERNAL_SUCCEEDED', 'NEEDS_RECONCILIATION')
                ORDER BY created_at LIMIT 1
                """,
                (tendril_id, operation_type),
            ).fetchone()
            if unresolved is not None:
                raise ConflictError(
                    "Tendril already has an unresolved external operation "
                    f"({unresolved['id']}, {unresolved['status']}); reconcile it before retrying"
                ) from exc
            raise ConflictError("External operation conflicts with existing state") from exc
        return operation, True

    def get_external_operation_by_key(self, idempotency_key: str) -> ExternalOperation:
        row = self._connection.execute(
            "SELECT * FROM external_operations WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"External operation not found: {idempotency_key}")
        return self._row_to_external_operation(row)

    def get_external_operation_by_id(self, operation_id: str) -> ExternalOperation:
        row = self._connection.execute(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"External operation not found: {operation_id}")
        return self._row_to_external_operation(row)

    def list_external_operations(
        self,
        statuses: set[ExternalOperationStatus] | None = None,
        tendril_id: str | None = None,
    ) -> list[ExternalOperation]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(item.value for item in statuses)
        if tendril_id:
            clauses.append("tendril_id = ?")
            params.append(tendril_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM external_operations {where} ORDER BY created_at, id", params
        ).fetchall()
        return [self._row_to_external_operation(row) for row in rows]

    def mark_operation_external_succeeded(
        self,
        operation_id: str,
        *,
        external_destination_id: str | None = None,
        external_message_id: str | None = None,
    ) -> ExternalOperation:
        """Record that the external side effect succeeded, retaining its IDs.

        Refuses to run after the operation is COMPLETED.
        """
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM external_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"External operation not found: {operation_id}")
            if row["status"] == ExternalOperationStatus.COMPLETED.value:
                raise ConflictError(f"External operation is already completed: {operation_id}")
            if external_destination_id is None and external_message_id is None:
                raise ValueError("At least one external ID must be supplied")
            connection.execute(
                """
                UPDATE external_operations
                SET status = 'EXTERNAL_SUCCEEDED',
                    external_destination_id = COALESCE(?, external_destination_id),
                    external_message_id = COALESCE(?, external_message_id),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    external_destination_id,
                    external_message_id,
                    _iso(utc_now()),
                    operation_id,
                ),
            )
        return self._row_to_external_operation(
            self._connection.execute(
                "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
            ).fetchone()
        )

    def mark_operation_completed(self, operation_id: str) -> ExternalOperation:
        with self.transaction() as connection:
            result = connection.execute(
                """
                UPDATE external_operations SET status = 'COMPLETED', updated_at = ?
                WHERE id = ? AND status != 'COMPLETED'
                """,
                (_iso(utc_now()), operation_id),
            )
            if result.rowcount != 1:
                raise NotFoundError(f"External operation not found: {operation_id}")
        return self._row_to_external_operation(
            self._connection.execute(
                "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
            ).fetchone()
        )

    def mark_operation_needs_reconciliation(
        self, operation_id: str, reason: str
    ) -> ExternalOperation:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM external_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"External operation not found: {operation_id}")
            metadata = json.loads(row["metadata_json"])
            trail = json.loads(metadata.get("reconciliation_trail") or "[]")
            trail.append({"reason": reason, "at": _iso(utc_now())})
            metadata["reconciliation_trail"] = json.dumps(trail)
            connection.execute(
                """
                UPDATE external_operations
                SET status = 'NEEDS_RECONCILIATION', metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json(metadata), _iso(utc_now()), operation_id),
            )
        return self._row_to_external_operation(
            self._connection.execute(
                "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
            ).fetchone()
        )

    def update_operation_metadata(self, operation_id: str, **fields: str) -> ExternalOperation:
        """Merge fields into an operation's metadata (phase markers, context)."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM external_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"External operation not found: {operation_id}")
            metadata = json.loads(row["metadata_json"])
            metadata.update(fields)
            connection.execute(
                """
                UPDATE external_operations SET metadata_json = ?, updated_at = ? WHERE id = ?
                """,
                (_json(metadata), _iso(utc_now()), operation_id),
            )
        return self.get_external_operation_by_id(operation_id)

    def reconcile_publish_operation(
        self,
        operation_id: str,
        *,
        external_destination_id: str | None = None,
        external_message_id: str | None = None,
        abandon: bool = False,
    ) -> ExternalOperation:
        """Operator-confirmed completion (or abandonment) of a publish operation.

        A publish creates a channel AND posts one tracked message, so completion
        requires both a channel ID and a message ID (either may already be
        retained on the operation). Completion registers the habitat and route
        idempotently before marking the operation COMPLETED.

        ``abandon=True`` asserts that no external side effect ever occurred and
        discards the reservation so the tendril can be retried; it is refused
        when a channel ID is already retained (abandoning then could orphan a
        real channel).
        """
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"External operation not found: {operation_id}")
            operation = self._row_to_external_operation(row)
            if operation.operation_type != "habitat_create":
                raise ConflictError("Only a publish/habitat operation can be reconciled this way")
            if operation.status == ExternalOperationStatus.COMPLETED:
                return operation
            if abandon:
                if operation.external_destination_id is not None:
                    raise ConflictError(
                        "Cannot abandon: a channel ID is already retained; supply the "
                        "observed channel/message IDs instead of abandoning"
                    )
                connection.execute("DELETE FROM external_operations WHERE id = ?", (operation_id,))
                return operation

            channel_id = external_destination_id or operation.external_destination_id
            message_id = external_message_id or operation.external_message_id
            if channel_id is None:
                raise ConflictError("Observed channel ID is required to complete this publish")
            if message_id is None:
                raise ConflictError("Observed message ID is required to complete this publish")
            metadata = json.loads(row["metadata_json"])
            trail = json.loads(metadata.get("reconciliation_trail") or "[]")
            trail.append(
                {
                    "action": "operator_reconciled",
                    "channel_id": channel_id,
                    "message_id": message_id,
                    "at": _iso(utc_now()),
                }
            )
            metadata["reconciliation_trail"] = json.dumps(trail)
            connection.execute(
                """
                UPDATE external_operations
                SET external_destination_id = COALESCE(?, external_destination_id),
                    external_message_id = COALESCE(?, external_message_id),
                    metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (channel_id, message_id, _json(metadata), _iso(utc_now()), operation_id),
            )

        guild_id = operation.metadata.get("guild_id")
        slug = operation.metadata.get("requested_slug")
        if not guild_id:
            raise ConflictError("Operation metadata lacks the guild context; cannot reconcile")
        if not slug:
            raise ConflictError("Operation metadata lacks the requested slug; cannot reconcile")
        self.register_habitat(
            guild_id=guild_id,
            platform=operation.platform,
            external_id=channel_id,
            canonical_slug=slug,
            description=f"Habitat for tendril {operation.tendril_id}",
        )
        self.record_route(
            operation.tendril_id,
            operation.platform,
            channel_id,
            message_id,
            operation.actor_user_id,
            operation.idempotency_key,
        )
        self.mark_operation_completed(operation_id)
        return self.get_external_operation_by_id(operation_id)

    def reconcile_external_operation(
        self,
        operation_id: str,
        *,
        external_message_id: str | None = None,
    ) -> ExternalOperation:
        """Operator-confirmed completion of an unresolved external operation.

        The operator attests that the external side effect is known and will
        not be repeated; the local record is completed without re-publishing.
        """
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"External operation not found: {operation_id}")
            if row["status"] == ExternalOperationStatus.COMPLETED.value:
                return self._row_to_external_operation(row)
            if external_message_id is not None:
                connection.execute(
                    "UPDATE external_operations SET external_message_id = ? WHERE id = ?",
                    (external_message_id, operation_id),
                )
            metadata = json.loads(row["metadata_json"])
            trail = json.loads(metadata.get("reconciliation_trail") or "[]")
            trail.append({"action": "operator_confirmed", "at": _iso(utc_now())})
            metadata["reconciliation_trail"] = json.dumps(trail)
            connection.execute(
                """
                UPDATE external_operations
                SET status = 'COMPLETED', metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json(metadata), _iso(utc_now()), operation_id),
            )
        return self._row_to_external_operation(
            self._connection.execute(
                "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
            ).fetchone()
        )

    def register_habitat(
        self,
        *,
        guild_id: str,
        platform: str,
        external_id: str,
        canonical_slug: str,
        description: str,
        aliases: list[str] | None = None,
        status: str = "ACTIVE",
    ) -> None:
        """Idempotently register a habitat row for a created Discord channel."""
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO habitats(
                        id, guild_id, platform, external_id, canonical_slug,
                        description, aliases_json, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("habitat"),
                        guild_id,
                        platform,
                        external_id,
                        canonical_slug,
                        description,
                        _json(aliases or []),
                        status,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    "SELECT external_id, canonical_slug FROM habitats WHERE guild_id = ? AND platform = ?",
                    (guild_id, platform),
                ).fetchall()
                if any(
                    item["external_id"] == external_id or item["canonical_slug"] == canonical_slug
                    for item in existing
                ):
                    return
                raise

    @staticmethod
    def _row_to_external_operation(row: sqlite3.Row) -> ExternalOperation:
        return ExternalOperation(
            id=row["id"],
            operation_type=row["operation_type"],
            idempotency_key=row["idempotency_key"],
            status=ExternalOperationStatus(row["status"]),
            platform=row["platform"],
            tendril_id=row["tendril_id"],
            external_destination_id=row["external_destination_id"],
            external_message_id=row["external_message_id"],
            actor_user_id=row["actor_user_id"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def export_bundle(self, bundle_id: str) -> dict[str, Any]:
        bundle = self.get_bundle(bundle_id)
        claim: Claim | None
        try:
            claim = self.get_claim_for_bundle(bundle_id)
        except NotFoundError:
            claim = None
        attempts = [
            dict(row)
            for row in self._connection.execute(
                "SELECT * FROM analysis_attempts WHERE bundle_id = ? ORDER BY created_at", (bundle_id,)
            ).fetchall()
        ]
        events = [
            dict(row)
            for row in self._connection.execute(
                """
                SELECT * FROM lifecycle_events
                WHERE (entity_type = 'bundle' AND entity_id = ?)
                   OR (entity_type = 'tendril' AND entity_id IN (SELECT id FROM tendrils WHERE bundle_id = ?))
                ORDER BY timestamp, id
                """,
                (bundle_id, bundle_id),
            ).fetchall()
        ]
        for attempt in attempts:
            attempt.pop("raw_response", None)
        autonomous_job: AutonomousJob | None = None
        try:
            autonomous_job = self.get_autonomous_job(bundle_id)
        except NotFoundError:
            pass
        autonomous_attempts = [
            attempt.model_dump(mode="json", exclude={"raw_response"})
            for attempt in self.list_autonomous_attempts(bundle_id)
        ]
        return {
            "schema_version": "1.0",
            "exported_at": _iso(utc_now()),
            "bundle": bundle.model_dump(mode="json"),
            "claim": claim.model_dump(mode="json") if claim else None,
            "claim_candidates": [
                item.model_dump(mode="json") for item in self.list_claim_candidates(bundle_id)
            ],
            "attempts": attempts,
            "tendrils": [item.model_dump(mode="json") for item in self.list_tendrils(bundle_id)],
            "events": events,
            "autonomous_job": (
                autonomous_job.model_dump(mode="json") if autonomous_job else None
            ),
            "autonomous_attempts": autonomous_attempts,
        }

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: LifecycleEvent) -> None:
        connection.execute(
            """
            INSERT INTO lifecycle_events(
                id, entity_type, entity_id, prior_state, new_state,
                actor_user_id, timestamp, idempotency_key, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.entity_type,
                event.entity_id,
                event.prior_state,
                event.new_state,
                event.actor_user_id,
                _iso(event.timestamp),
                event.idempotency_key,
                _json(event.metadata),
            ),
        )

    @staticmethod
    def _row_to_tendril(row: sqlite3.Row) -> Tendril:
        return Tendril(
            id=row["id"],
            bundle_id=row["bundle_id"],
            title=row["title"],
            description=row["description"],
            type=row["type"],
            actionability=row["actionability"],
            evidence=json.loads(row["evidence_json"]),
            why_open=row["why_open"],
            suggested_habitat_slug=row["suggested_habitat_slug"],
            habitat_reasoning=row["habitat_reasoning"],
            confidence=row["confidence"],
            status=row["status"],
            routed_platform=row["routed_platform"],
            routed_external_id=row["routed_external_id"],
            snoozed_until=_dt(row["snoozed_until"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _row_to_prompt_attempt(row: sqlite3.Row) -> PromptAttempt:
        return PromptAttempt(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            user_id=row["user_id"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            question=row["question"],
            status=PromptAttemptStatus(row["status"]),
            receipt=(
                ProviderReceipt.model_validate_json(row["receipt_json"])
                if row["receipt_json"]
                else None
            ),
            response_text=row["response_text"],
            failure_classification=(
                PromptFailureClass(row["failure_classification"])
                if row["failure_classification"]
                else None
            ),
            created_at=_dt(row["created_at"]),
            completed_at=_dt(row["completed_at"]),
        )
