PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- schema_meta is written by the repository's initialize() after migrations;
-- it always holds exactly one row describing the current schema version.

CREATE TABLE IF NOT EXISTS bundles (
    id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    intake_channel_id TEXT NOT NULL,
    submitter_user_id TEXT NOT NULL,
    recipient_user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    -- 1 = recipient-free autonomous capture (submitter owns it; no claim row or
    -- recipient action is ever required); 0 = legacy/manual capture.
    autonomous_mode INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    sealed_at TEXT,
    claim_id TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_capture_per_owner_channel
ON bundles(submitter_user_id, intake_channel_id)
WHERE status = 'CAPTURING';

CREATE TABLE IF NOT EXISTS source_messages (
    external_message_id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL REFERENCES bundles(id),
    platform TEXT NOT NULL,
    author_user_id TEXT NOT NULL,
    author_display_name TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    content TEXT NOT NULL,
    source_url TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    UNIQUE(bundle_id, ordinal)
);

CREATE TABLE IF NOT EXISTS source_units (
    unit_id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL REFERENCES bundles(id),
    source_message_id TEXT NOT NULL REFERENCES source_messages(external_message_id),
    paragraph_ordinal INTEGER NOT NULL,
    exact_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    UNIQUE(source_message_id, paragraph_ordinal)
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL UNIQUE REFERENCES bundles(id),
    raw_topic TEXT NOT NULL,
    claimant_user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- v2 singular selection columns, retained only so databases created before
    -- the plural migration can still be read; new writes leave these NULL.
    selected_candidate_id TEXT,
    selected_candidate_json TEXT,
    -- v3 plural selection columns (the single source of truth for new writes).
    selected_candidate_ids_json TEXT,
    selected_candidates_json TEXT
);

-- Claim-candidate discovery is a distinct pre-claim provider operation. The
-- attempt and its raw response are retained so a malformed or hallucinated
-- candidate set fails safely and remains reviewable.
CREATE TABLE IF NOT EXISTS claim_discovery_attempts (
    id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL REFERENCES bundles(id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    raw_response TEXT,
    candidates_json TEXT,
    validation_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(bundle_id, idempotency_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_running_discovery_per_bundle
ON claim_discovery_attempts(bundle_id)
WHERE status = 'RUNNING';

CREATE TABLE IF NOT EXISTS claim_candidates (
    bundle_id TEXT NOT NULL REFERENCES bundles(id),
    candidate_id TEXT NOT NULL,
    display_order INTEGER NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (bundle_id, candidate_id),
    UNIQUE (bundle_id, display_order)
);

CREATE TABLE IF NOT EXISTS claim_discovery_receipts (
    attempt_id TEXT PRIMARY KEY REFERENCES claim_discovery_attempts(id),
    receipt_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_attempts (
    id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL REFERENCES bundles(id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    raw_response TEXT,
    claim_analysis_json TEXT,
    validation_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(bundle_id, idempotency_key)
);

-- At most one RUNNING analysis attempt per bundle, enforced by the database
-- so concurrent processes cannot start parallel attempts for one bundle.
CREATE UNIQUE INDEX IF NOT EXISTS one_running_analysis_per_bundle
ON analysis_attempts(bundle_id)
WHERE status = 'RUNNING';

CREATE TABLE IF NOT EXISTS provider_receipts (
    attempt_id TEXT PRIMARY KEY REFERENCES analysis_attempts(id),
    receipt_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tendrils (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES analysis_attempts(id),
    bundle_id TEXT NOT NULL REFERENCES bundles(id),
    client_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    type TEXT NOT NULL,
    actionability TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    why_open TEXT NOT NULL,
    suggested_habitat_slug TEXT,
    habitat_reasoning TEXT,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    routed_platform TEXT,
    routed_external_id TEXT,
    snoozed_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(attempt_id, client_id)
);

CREATE TABLE IF NOT EXISTS coverage_entries (
    attempt_id TEXT NOT NULL REFERENCES analysis_attempts(id),
    unit_id TEXT NOT NULL REFERENCES source_units(unit_id),
    source_message_id TEXT NOT NULL REFERENCES source_messages(external_message_id),
    classification TEXT NOT NULL,
    tendril_client_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    confidence REAL NOT NULL,
    PRIMARY KEY(attempt_id, unit_id)
);

CREATE TABLE IF NOT EXISTS unresolved_items (
    attempt_id TEXT NOT NULL REFERENCES analysis_attempts(id),
    ordinal INTEGER NOT NULL,
    description TEXT NOT NULL,
    PRIMARY KEY(attempt_id, ordinal)
);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    prior_state TEXT,
    new_state TEXT NOT NULL,
    actor_user_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE(entity_type, entity_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS habitats (
    id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    canonical_slug TEXT NOT NULL,
    description TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(guild_id, platform, external_id),
    UNIQUE(guild_id, canonical_slug)
);

CREATE TABLE IF NOT EXISTS routes (
    id TEXT PRIMARY KEY,
    tendril_id TEXT NOT NULL UNIQUE REFERENCES tendrils(id),
    platform TEXT NOT NULL,
    external_destination_id TEXT NOT NULL,
    external_message_id TEXT,
    actor_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- Local record of external side effects (Discord posts, channel creation).
-- The operation is reserved BEFORE the external action so an ambiguous
-- failure is never automatically repeated.
CREATE TABLE IF NOT EXISTS external_operations (
    id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    platform TEXT NOT NULL,
    tendril_id TEXT NOT NULL REFERENCES tendrils(id),
    external_destination_id TEXT,
    external_message_id TEXT,
    actor_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

-- One unresolved external operation per tendril and type: a different
-- idempotency key (for example a human retry) must not repeat an ambiguous
-- external side effect; it must reconcile first.
CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_operation_per_tendril
ON external_operations(tendril_id, operation_type)
WHERE status IN ('PENDING', 'EXTERNAL_SUCCEEDED', 'NEEDS_RECONCILIATION');

-- Single-turn ordinary prompt attempts are isolated from capture/claim/tendril
-- state. The idempotency key is the Discord interaction id, so a duplicate
-- delivery replays the persisted attempt instead of making a second model call
-- or re-posting pages. At most one RUNNING prompt per user is enforced at the
-- database boundary. Receipts and the raw textual response are retained for a
-- completed attempt; failures retain only a safe classification (never raw
-- exception text or credentials).
CREATE TABLE IF NOT EXISTS prompt_attempts (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    receipt_json TEXT,
    response_text TEXT,
    failure_classification TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_running_prompt_per_user
ON prompt_attempts(user_id)
WHERE status = 'RUNNING';

-- One durable autonomous job per bundle (recipient-free capture). The phase
-- encodes the paid-request crash boundary: QUEUED means no model request was
-- ever sent; REQUEST_STARTED means one may have been sent, so a resumed job
-- becomes REQUEST_AMBIGUOUS and is never retried automatically. The pending
-- attempt key is consumed atomically when an attempt is created, so a crash
-- can never silently re-send the same request.
CREATE TABLE IF NOT EXISTS autonomous_jobs (
    id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL UNIQUE REFERENCES bundles(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    phase TEXT NOT NULL,
    pending_attempt_key TEXT,
    provider TEXT,
    model TEXT,
    last_notification TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One bounded provider attempt per autonomous sort. Raw responses and safe
-- receipts are retained for review; a provider/validation failure creates no
-- trusted tendrils.
CREATE TABLE IF NOT EXISTS autonomous_attempts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES autonomous_jobs(id),
    bundle_id TEXT NOT NULL REFERENCES bundles(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    raw_response TEXT,
    result_json TEXT,
    validation_json TEXT,
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_running_autonomous_attempt_per_bundle
ON autonomous_attempts(bundle_id)
WHERE status = 'RUNNING';
