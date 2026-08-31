<p align="center">
  <img src="docs/assets/nemoir-hero.png" width="620" alt="A friendly robot among branching pink and purple coral tendrils, with three clownfish" />
</p>

# Nemoir

> Experimental open-source reference implementation. Discord is currently the
> primary test surface, and the central conversational model is still being
> validated with real users.

Nemoir preserves worthwhile conversational branches that were raised but not
meaningfully taken up. Its defining model is claim-relative recovery:

```text
deposit
-> recipient claim
-> semantic exclusion of claimed material
-> tendril recovery from the unclaimed remainder
-> human review
-> later return or resurfacing
```

![The Nemoir tendril lifecycle: deposit, recipient claim, semantic exclusion, tendril recovery, human review, and later return](docs/assets/nemoir-lifecycle.svg)

A **Tendril** is a meaningful, unfinished conversational edge - for example a
question, disagreement, research lead, decision, task candidate, or project
seed - retained with evidence showing where it came from. Human attention and
judgement remain primary. Nemoir is not generic summarisation, topic
classification, or automatic task extraction. See [CONCEPT.md](CONCEPT.md)
for the implementation-independent model.

The Python codebase contains a platform-neutral domain/application core plus
reference adapters for Discord, DeepSeek, synthetic providers, SQLite, and a
small Windows control panel. The current reference implementation also has an
experimental recipient-free autonomous mode. That mode is implemented and is
the default workflow in the current Discord UI, but it is an extension of the
reference implementation, not a replacement definition for the core
recipient-claim model.

The standalone mascot artwork is available at
[`docs/assets/nemoir-mascot.png`](docs/assets/nemoir-mascot.png) for the Discord
bot avatar and other small-format reference-implementation surfaces. Visual
asset provenance and reuse notes are in
[`docs/assets/README.md`](docs/assets/README.md).

## Project status and boundaries

- This is a private-alpha-quality reference implementation, not production
  SaaS and not a demonstrated multi-tenant service.
- Discord is the current proof surface, not the conceptual product boundary.
- DeepSeek is optional, external, paid, and disabled unless explicit live
  gates are enabled. Deterministic tests and the offline demo do not require
  Discord, DeepSeek, network access, or API credits.
- Semantic accuracy, safe resurfacing timing, multi-user deployment, and the
  value of the autonomous extension remain experimental.
- The repository intentionally does not provide billing, accounts, a hosted
  dashboard, enterprise authentication, or deployment infrastructure.

## Privacy before use

Nemoir deliberately persists sensitive material. Its SQLite databases can
contain submitted message text, Discord user/display identifiers, source URLs,
prompt questions and responses, exact evidence, provider receipts, and raw
model output. In live provider modes, deliberately submitted conversational
text leaves Discord/local infrastructure and is processed by the configured
model provider. Do not assume local-only processing unless you have supplied a
genuinely local provider.

Never commit `.env`, database files, logs, exports, credentials, or real
private conversations. Keep `data/` private, inspect it before sharing a copy
of a working directory, and use synthetic material for tests and issues. See
[SECURITY.md](SECURITY.md) for reporting and credential-response guidance.

## What is implemented

The current repository provides:

- deliberate multi-message bundle capture;
- the autonomous default workflow: recipient-free `/tend` → post your own
  messages → `/seal` (immediate “sealed and queued” acknowledgement) →
  durable background sorting through a strict full-source coverage contract →
  automatic publishing into shared channels → a completion notification that
  mentions only the author;
- the legacy recipient/claim workflow (optional manual mode): `/tend
  recipient:<member>` keeps the two-stage claim selection
  (`/claim options:"<n>[,<n>...]"` or `/claim topic:"..."`) and manual
  `/publish-bundle`;
- a separate autonomous-sorting provider boundary (deterministic synthetic
  provider for Autonomous Test; gated DeepSeek provider for Autonomous Live),
  distinct from the claim/selected-candidate model;
- immutable source evidence and deterministic paragraph units;
- deterministic exact-quote, span, coverage, and claim-exclusion validation for
  claim candidates, the final decomposition, and autonomous topics;
- SQLite persistence and append-only lifecycle events (the selected candidates
  and their full snapshots are persisted on the claim, not reduced to titles);
- manual resurfacing, snoozing, resolution, release, and idempotent routing;
- explicit human promotion of an eligible tendril to `PROMOTED_ACTIONABLE`
  (`/promote-actionable`) that preserves evidence, provenance, and lifecycle
  history; the AI-inferred actionability is never rewritten;
- deterministic Discord-safe pagination: evidence is never silently
  truncated, list views are bounded pages, and routed posts carry the
  complete evidence as one tracked message (with a file attachment when the
  text exceeds the 2000-character platform limit);
- a disabled-by-default DeepSeek adapter and synthetic regression fixture.

The supplied fixture is explicitly synthetic and does not prove general
semantic accuracy.

## Repository map

- `src/nemoir/domain/` - platform-neutral models, states, segmentation, and
  semantic validation.
- `src/nemoir/application/` - capture, claim, analysis, lifecycle,
  resurfacing, routing, and publishing orchestration.
- `src/nemoir/adapters/` and `src/nemoir/providers/` - Discord and model
  provider boundaries.
- `src/nemoir/persistence/` - the SQLite reference store. This stores source
  content and raw provider output; it is not a public-data store.
- `tests/` - deterministic tests and explicitly synthetic fixtures.

Contributor guidance is in [CONTRIBUTING.md](CONTRIBUTING.md). The project is
licensed under [Apache License 2.0](LICENSE), carries attribution in
[NOTICE](NOTICE), and can be cited using [CITATION.cff](CITATION.cff).

## Windows PowerShell setup

From the repository root:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

Install optional adapters only when needed:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[all]"
```

Do not create a real `.env` until a human-observed integration step. Never paste tokens into source files, prompts, screenshots, or logs.

## Nemoir Control Panel (Windows desktop)

A small, independent Tkinter desktop window shows the bot's real runtime state —
it stays open whether or not the bot is running, so it can truthfully show
`OFFLINE` when nothing is connected. It never equates "a process exists" with
"Discord is online": the panel reads an atomic status file that the bot itself
writes, and treats a stale heartbeat or a dead process as offline.

Start it from PowerShell:

```powershell
.\.venv\Scripts\python.exe -m nemoir gui
```

Or double-click `Nemoir Control Panel.vbs` in the repository root. The launcher
resolves `.venv\Scripts\pythonw.exe` relative to its own location, so it keeps
no terminal window visible and embeds no user-specific paths.

The window shows:

- a large coloured status indicator: `OFFLINE`, `STARTING`,
  `ONLINE — SYNTHETIC`, `ONLINE — CHANNEL TEST`, `ONLINE — LIVE`,
  `ONLINE — AUTONOMOUS TEST`, `ONLINE — AUTONOMOUS LIVE`,
  `RECONNECTING`, `STOPPING`, or `ERROR`;
- the current mode (synthetic, channel-test, live, autonomous-test, or
  autonomous-live);
- the configured live model name (never credentials);
- bot uptime and the last successful Discord heartbeat;
- one short, safe error description when relevant;
- `Start Synthetic`, `Start Channel Test`, `Start Live`,
  `Start Autonomous Test`, `Start Autonomous Live`, and `Stop Bot` buttons.

`Start Synthetic` launches `python -m nemoir discord-pilot --confirm-live` with
the current interpreter, hidden console, and no DeepSeek access. `Start Live`
first asks for confirmation (connecting to Discord is free, but `/prompt`,
claim discovery, and analysis can incur DeepSeek cost), checks that safe
configuration is present without revealing credentials, and sets
`NEMOIR_ALLOW_LIVE_DEEPSEEK=true` only in the child process environment — it
never changes your environment variables and never makes a model request just
by starting.

`Start Channel Test` is the explicit, clearly labelled way to run the
synthetic channel-writing test. It first shows a confirmation explaining that
it will create real Discord text channels in the configured development
category. On confirmation it launches `python -m nemoir discord-channel-test
--confirm-live`, which uses the deterministic synthetic provider (never
DeepSeek) and enables `NEMOIR_ALLOW_CHANNEL_WRITE=true` only in that child
process — the default remains unchanged and ordinary `Start Synthetic` can
never create channels.

`Start Autonomous Test` runs the default autonomous product workflow with the
deterministic synthetic sort and a real Discord connection. Its confirmation
warns that sealed captures automatically create **real shared channels**; it
never reads or calls DeepSeek (DeepSeek configuration is stripped from the
child environment) and enables `NEMOIR_ALLOW_CHANNEL_WRITE=true` only in that
child process.

`Start Autonomous Live` runs the autonomous workflow with real DeepSeek
sorting. Its confirmation warns about **paid model calls** (one per sealed
capture) and **real Discord channels**; it sets
`NEMOIR_ALLOW_LIVE_DEEPSEEK=true` and `NEMOIR_ALLOW_CHANNEL_WRITE=true` only in
the child process.

`Stop Bot` requests a graceful Discord shutdown, waits for the
`OFFLINE` status, and falls back to a bounded termination only if graceful
shutdown does not finish. If you close the panel while its own bot is running,
it asks whether to stop the bot first.

Only one bot instance may run at a time. The bot holds an OS-released
single-instance lock, so a crashed process cannot permanently block a later
start, and the panel refuses to launch a second bot when one is already live
(including one started from PowerShell).

The runtime status lives at `data/runtime/bot-status.json` (override with
`NEMOIR_RUNTIME_DIR`). It stores only safe operational data — state, mode,
model name, an instance ID/PID, timestamps, and a safe error class — and never
a Discord token, DeepSeek key, prompt, response, or raw exception text.

## Offline demonstration

The demonstration uses only the synthetic fixture and fake provider:

```powershell
.\.venv\Scripts\python.exe -m nemoir demo-offline --database data\demo.sqlite3
```

It prints a compact review and never connects to Discord or DeepSeek.

## Export one bundle

```powershell
.\.venv\Scripts\python.exe -m nemoir export --database data\demo.sqlite3 --bundle-id BUNDLE_ID --output data\bundle-export.json
```

Exports omit raw provider responses and never delete source evidence.

## External operation reconciliation

Routing, habitat creation, and publishing reserve a local operation record
before any Discord side effect. An ambiguous failure is never repeated
automatically; it stays in `PENDING`, `EXTERNAL_SUCCEEDED`, or
`NEEDS_RECONCILIATION` until an operator acts:

```powershell
.\.venv\Scripts\python.exe -m nemoir ops --database data\demo.sqlite3 --list
.\.venv\Scripts\python.exe -m nemoir ops --database data\demo.sqlite3 --reconcile OPERATION_ID --message-id OBSERVED_MESSAGE_ID
.\.venv\Scripts\python.exe -m nemoir ops --database data\demo.sqlite3 --reconcile OPERATION_ID --channel-id OBSERVED_CHANNEL_ID --message-id OBSERVED_MESSAGE_ID
.\.venv\Scripts\python.exe -m nemoir ops --database data\demo.sqlite3 --reconcile OPERATION_ID --abandon
```

- A **route** operation is completed with `--message-id` (the confirmed post).
- A **publish/habitat** operation needs both the observed channel ID and the
  observed message ID; either may already be retained on the operation, and the
  other is supplied by the operator. Completion registers the habitat and route
  and is refused unless both IDs exist — an ambiguous side effect is never
  inferred to have failed.
- `--abandon` is the explicit safe path for an operation where the operator
  confirmed **no external side effect occurred** (for example a channel
  creation that never landed). It discards the reservation so the tendril can
  be retried, and is refused once a channel ID is retained (abandoning then
  could orphan a real channel).

`/publish-bundle` replays a completed, reconciled tendril as already published
without creating or posting again.

## Publishing a bundle into channels

Once a bundle reaches `REVIEW_READY` (option selection and analysis both
happen before any channel is created), an authorised user can split its
unclaimed tendrils into separate Discord text channels under the configured
anemone category. Claimed material is never published as a channel: the
recipient's claim is not a tendril, so it remains the conversational subject.

The command shape is:

```text
/publish-bundle bundle_id:<id> confirm:<true|false>
```

- `confirm` omitted or `false` is a **read-only preview** (it also requires
  `REVIEW_READY`). It classifies each tendril by the exact plan a confirmed run
  would follow — will publish, already published, will be skipped, or requires
  reconciliation — and never presents a terminal or already-routed tendril as a
  channel that will definitely be created. Proposed channel names are base
  names (duplicates within the bundle are resolved deterministically) and may
  receive a collision suffix when execution inspects the real category. The
  preview creates no channel, message, route, habitat, or external-operation
  record.
- `confirm:true` actually creates the channels. It requires every one of:
  - bundle state `REVIEW_READY`;
  - requester is the submitter, the recipient, or an administrator;
  - the requester holds Discord `Manage Channels`;
  - the configured guild and anemone category both resolve;
  - the explicit channel-write gate `NEMOIR_ALLOW_CHANNEL_WRITE=true`.

Every eligible, unrouted tendril is published to its own channel. Tendrils
that are already published replay as already published; terminal, snoozed, and
promoted tendrils are skipped; ambiguous or unresolved items are reported as
reconciliation-required; and deterministic failures are reported as failed. A
failure for one tendril never erases or duplicates a sibling's successful
channel.

Channel naming prefers a validated `suggested_habitat_slug` and otherwise
derives a slug from the tendril title, applies the existing Discord
channel-name sanitisation, and resolves collisions deterministically by
appending `-2`, `-3`, … — it never silently adopts an unrelated pre-existing
channel. The final channel ID and canonical slug are persisted.

### Safety and partial-failure recovery

Publishing reuses the existing habitat/routing external-operation machinery.
A durable per-tendril operation is reserved before channel creation with a
stable idempotency key derived from the bundle and tendril (not the latest
interaction ID), the returned channel ID is persisted immediately after
creation, and the message ID is persisted immediately after posting. A
duplicate Discord delivery, a repeated command, a restart after partial
success, or a retry of a partly completed bundle can therefore never create a
duplicate channel or duplicate post.

If a Discord result is ambiguous (for example the channel was created but the
post result was lost), that item stops in `NEEDS_RECONCILIATION` and is never
repeated automatically. Inspect and complete it with the observed IDs:

```text
.\.venv\Scripts\python.exe -m nemoir ops --database data\nemoir.sqlite3 --list
.\.venv\Scripts\python.exe -m nemoir ops --database data\nemoir.sqlite3 --reconcile OPERATION_ID --channel-id OBSERVED_CHANNEL_ID --message-id OBSERVED_MESSAGE_ID
```

If the operator confirms no external side effect occurred, abandon the
reservation so the tendril can be retried instead of silently repeating:

```text
.\.venv\Scripts\python.exe -m nemoir ops --database data\nemoir.sqlite3 --reconcile OPERATION_ID --abandon
```

Routed and published posts carry the complete evidence (exact quotations and
source links) as one tracked Discord message, with a `tendril.txt` attachment
when the text exceeds the 2000-unit Discord limit (measured in UTF-16 code
units, so astral emoji cannot overflow). Every page of the preview, the publish
report, and the routed tendril content is sent with mention suppression:
`@everyone`, `@here`, `<@user>`, and `<@&role>` stay visible as literal text but
never notify anyone.

## Autonomous capture, sorting and publishing (default product workflow)

In Autonomous Test or Autonomous Live mode, a user captures their **own**
messages without naming a recipient, seals the capture, and can walk away:

```text
/tend
<post your messages in the intake channel>
/seal
```

- `/tend` without a `recipient` opens a recipient-free autonomous capture in
  Autonomous modes only (ordinary modes refuse it). The invoking user owns the
  capture, only that user’s messages enter it, and concurrent users may each
  hold their own capture in the same intake channel.
- `/seal` acknowledges **immediately** (“sealed and queued”) and persists one
  durable background job per bundle. It never waits for sorting or channel
  creation. Duplicate `/tend` and `/seal` deliveries are idempotent: they can
  never create a second capture, job, model call, channel, or post.
- A bounded background worker (one running attempt per bundle, default
  concurrency 1) sorts the sealed source through the **autonomous sorting
  contract** — a provider boundary separate from the claim/selected-candidate
  model. Every output topic carries a stable provider/client ID, display
  order, concise title, one-sentence summary, tendril type and actionability,
  exact source-backed evidence (message IDs, quotations, offsets, unit IDs),
  why it remains meaningful/open, a suggested habitat/channel slug, and a
  confidence. Validation fails closed unless **every sealed source unit is
  assigned to exactly one primary topic**, nothing becomes unaccounted
  CONTEXT, quotations/offsets/unit IDs match source truth, topic IDs and
  display order are unique and deterministic, and no evidence is invented,
  silently dropped, or duplicated across primary topics. Invalid output
  creates no tendrils and no channels.
- Validated topics are persisted as **ordinary Nemoir tendrils**, so the
  existing lifecycle, routing, publishing, exports, and evidence views keep
  working. Autonomous bundles do **not** require a claim row.
- After a valid sort is persisted, the bundle moves to `REVIEW_READY` and the
  worker automatically publishes every generated tendril through the existing
  `PublishingService` — the channel-write gate, UTF-16-safe formatting, mention
  suppression, collision handling, attachments, routes, habitats, and
  reconciliation machinery are all reused. No `/claim` or `/publish-bundle`
  interaction is required. Publishing never duplicates channels or posts, and
  ambiguous side effects are never repeated automatically.
- When complete, Nemoir notifies the sealing user in the nursery channel (or
  the intake channel as fallback) with the bundle ID and created channel links.
  **Only the deliberate author mention may ping**; model-derived titles and
  text never trigger mentions. A user who walked away never has to keep an
  interaction open.
- If sorting fails, Nemoir notifies the author safely with retry
  instructions and creates no trusted tendrils. The owner or an administrator
  retries deliberately with a fresh idempotency key — no automatic repair
  call is ever made and the sealed source is never resealed or mutated:

```text
/autonomous-retry bundle_id:<bundle-id>
```

### Durability and the paid-request crash boundary

Each job moves through explicit durable phases: `QUEUED` (safe to start),
`REQUEST_STARTED` (a model request is about to start), `RESULT_PERSISTED`
(the validated result is persisted), `PUBLISHING`, `COMPLETED`, `FAILED`
(provider/validation failure), `REQUEST_AMBIGUOUS` (crash after a request may
have been sent), and `PUBLISH_RECONCILIATION_REQUIRED` (partial publishing).

- Queued work resumes after restart.
- A crash after a model request may have been sent **never triggers an
  automatic second paid request**: the job becomes `REQUEST_AMBIGUOUS` and
  waits for `/autonomous-retry`.
- Validated, persisted topics safely resume publishing after restart through
  the existing per-tendril publish operations.
- Provider/validation failures retain safe receipts and raw output according
  to existing policy but create no trusted tendrils.
- Partial publishing stops in `PUBLISH_RECONCILIATION_REQUIRED`; reconcile (or
  abandon) each unresolved operation with `nemoir ops` and the job resumes
  automatically once nothing ambiguous remains.
- Background exceptions are contained and reported; they never kill the bot.

### Modes, gates, and Control Panel

Two explicit modes run the autonomous workflow:

- **Start Autonomous Test** — deterministic synthetic sort, real Discord
  connection, real shared channel writes. It never reads or calls DeepSeek.
  The confirmation warns that real shared channels will be created.
- **Start Autonomous Live** — real DeepSeek sorting and real channel writes.
  The confirmation warns about paid model calls and real Discord channels.
  Both gates (`NEMOIR_ALLOW_LIVE_DEEPSEEK=true` and
  `NEMOIR_ALLOW_CHANNEL_WRITE=true`) are set **only in the child process**.

Runtime status renders `ONLINE — AUTONOMOUS TEST` or `ONLINE — AUTONOMOUS LIVE`.
Ordinary `Start Synthetic`, `Start Channel Test`, and `Start Live` keep their
existing safety boundaries and cannot trigger autonomous work. Server-side
enforcement never relies on hidden GUI buttons: autonomous sorting and
publishing require the corresponding mode and gates.

CLI equivalents (both refuse without `--confirm-live`):

```powershell
# Autonomous Test: synthetic provider, real channels, never DeepSeek
$env:NEMOIR_ALLOW_CHANNEL_WRITE = 'true'
.\.venv\Scripts\python.exe -m nemoir discord-autonomous-test --confirm-live

# Autonomous Live: paid DeepSeek sorts + real channels (both gates required)
$env:NEMOIR_ALLOW_LIVE_DEEPSEEK = 'true'
$env:NEMOIR_ALLOW_CHANNEL_WRITE = 'true'
.\.venv\Scripts\python.exe -m nemoir discord-autonomous-live --confirm-live
```

`autonomous-test` and `autonomous-live` are accepted CLI aliases.

### Multiple intake channels

`NEMOIR_INTAKE_CHANNEL_ID` remains the required primary intake. Optional
additional intake channels are configured as a comma-separated list:

```text
NEMOIR_ADDITIONAL_INTAKE_CHANNEL_IDS=<additional-intake-channel-id>
```

Every configured intake accepts `/tend`, captured messages, `/seal`, `/cancel`,
and `/prompt`. Captures remain isolated by both user and channel, so the same
person may hold separate captures in separate intakes without either capture
absorbing the other's messages. Restart the bot after changing the setting.

### Shared visibility

Nemoir is a shared two-person workspace. Channels created by either user’s
capture are visible to both users through the inherited anemone category
permissions: every channel is created without private per-channel overwrites,
so **both users must hold `View Channel` permission on the configured anemone
category**. Either user can independently run `/tend` and seal their own
capture; one user’s active capture never absorbs the other user’s messages.
Private per-user captures are **explicitly out of scope for this milestone**
and are documented as a future option; no per-user channel permission
management exists yet.

Semantic routing into an already-existing related habitat is **not yet
implemented**: if the derived channel name collides, the system still creates
a deterministic collision-suffixed new channel (`-2`, `-3`, …) instead of
reusing the existing one.

## Recovery

If a local database becomes disposable during development, move it out of `data\` and rerun the demo. Do not delete a pilot database unless its loss is intentional and separately backed up.

If package installation fails, capture the exact PowerShell output. Do not install global packages or change another Python installation to work around it.

### Legacy manual claim workflow (two-stage claim selection)

This recipient-led workflow is the optional legacy/manual mode, still fully
supported and unchanged: `/tend recipient:<member>` opens a manual capture and
the recipient keeps first right of selection.

`/seal` segments the source and then asks the provider for 2-5 numbered claim
candidates, each with a concise inferred title, a one-sentence summary, and exact
source quotations with jump links. The candidates are validated against source
truth and persisted before the recipient is asked to choose:

```text
/claim options:"1"           # select by the number shown in the generated menu
/claim options:"2,3"         # combine several related candidates into one claim
/claim options:"1, 3, 4"     # whitespace around numbers is trimmed
/claim topic:"..."           # supply a custom subject instead (escape hatch)
/claim-options               # show the options again (recipient or admin only)
/claim-options-retry bundle_id:<id>   # regenerate options after a discovery failure
```

The option numbers are the display numbers of the menu `/seal` generated, not a
fixed mapping: inspect the menu and select by the numbers actually listed.
Exactly one of `options` or `topic` is required. `options` is a comma-separated
list of one or more option numbers; empty elements, non-integers, zero, negative,
duplicate, and unavailable numbers are rejected. Numbers may be supplied in any
order but are stored in canonical persisted display order. When more than one
bundle awaits the recipient, `bundle_id` is required. Titles and summaries are
Nemoir interpretations; quotations are exact source text.

The selected candidates are persisted on the resulting claim (ids plus complete
immutable snapshots), never reduced to titles. When several candidates are
selected, their combined exact evidence becomes the authoritative claim boundary:
the second-stage analysis must preserve exactly that union (deduplicating
identical or overlapping quotations without losing source attribution) and may
normalize the combined label and rationale but may not drop, replace, or invent
selected evidence. The pre-analysis topic deterministically joins the selected
titles in display order; the second stage may still produce a more natural final
label. `/claim-options-retry` is restricted to the capture owner or an
administrator, operates only from `CLAIM_OPTIONS_FAILED`, and never reseals or
modifies the captured source.

Every exact evidence fragment surfaced during discovery remains accounted for
after analysis: it must be absorbed by the claim, preserved in a tendril, or
force review. It is never silently demoted to `CONTEXT`; a candidate may be
split or merged into tendrils, but its evidence cannot disappear.

### Manual analysis retry

Nemoir never makes an automatic repair call after a provider or validation
failure. The designated recipient or an administrator can deliberately retry a
bundle in `ANALYSIS_FAILED` or `NEEDS_REVIEW` from Discord:

```text
/analysis-retry bundle_id:<bundle-id>
```

Each Discord interaction supplies a new analysis idempotency key. A successful
or currently running bundle cannot be retried through this command. Duplicate
interactions with the same id never start a second provider call or post a
second review.

### Ordinary single-turn questions (`/prompt`)

`/prompt` is a separate, stateless surface for asking Nemoir an ordinary
question — it is independent of capture, claim discovery, analysis, tendrils,
habitats, and routing:

```text
/prompt question:"How do aeroplanes work?"
```

Behaviour and boundaries:

- It is restricted to the configured development guild and intake channel.
- `question` must be non-empty and at most `NEMOIR_PROMPT_MAX_INPUT_CHARS`
  characters (default `2000`). Oversized or empty input is rejected before any
  provider call.
- It is single-turn and stateless: no conversation memory, no source capture,
  no claim or tendril creation, no tools, and no channel actions. The model
  text is display-only and never executes suggested actions.
- The interaction is deferred immediately, then the answer is posted publicly
  in the channel. The first page is labelled as a Nemoir response (DeepSeek in
  live mode, the deterministic synthetic provider in pilot mode).
- Long answers — long paragraphs, long unbroken lines, Markdown, and fenced
  code blocks — are paginated with the deterministic pagination helper, so
  every page stays at or below Discord's 2000-character limit (measured in
  UTF-16 code units) and no text is silently dropped.
- Every model-generated response page is sent with
  `discord.AllowedMentions.none()`, so `@everyone`, `@here`, `<@user>`, and
  `<@&role>` remain visible as literal text but never notify anyone. Legitimate
  recipient mentions in the capture/claim workflow are unaffected.
- A failed request shows a short, non-leaking failure message; credentials,
  internal prompts, receipts, and raw exception details are never echoed.
- Duplicate Discord deliveries are idempotent (one model call and one set of
  pages), and at most one prompt may be running per user in the guild.
- Maximum output tokens are configurable with `NEMOIR_PROMPT_MAX_OUTPUT_TOKENS`
  (default `1024`). There is no automatic retry that could duplicate model
  cost. The live DeepSeek call requires the same explicit live-operation gate
  as analysis (`NEMOIR_ALLOW_LIVE_DEEPSEEK=true`), so using this command costs
  model tokens per call.

### Explicit promotion

Actionability is an AI inference, not authority. An authorised bundle
participant or administrator can explicitly promote an `OPEN` or `RESURFACED`
tendril:

```text
/promote-actionable tendril_id:<tendril-id>
```

Promotion moves the lifecycle state to `PROMOTED_ACTIONABLE`, appends one
lifecycle event, and never rewrites the stored evidence, source links, or the
originally inferred actionability value. Repeated interactions with the same
id are idempotent.

### Discord message length handling

Every Discord message Nemoir emits stays below the 2000-character platform
limit, measured in UTF-16 code units (`discord_text_units`): BMP characters
count as one unit, astral characters (for example most emoji) count as two.
Evidence-bearing output is paginated deterministically (`— page i/n —`
footers), never truncated: a single over-long line is hard-split at code-point
boundaries (never inside a character), and the widest footer (including
two-digit page indexes) is reserved inside the unit limit. List views
(`/tendrils`) are bounded pages that report when more items remain instead of
silently dropping them. Routed posts and habitat posts are a single tracked
message: when the evidence text is too long for one message, the bounded
summary carries the exact full evidence as an attached `tendril.txt` file, so
routing and reconciliation always refer to exactly one message id.

## Live integration gates

### Synthetic Discord pilot

The smallest Discord-only pilot uses the deterministic fake provider. It connects
to the configured development guild but does not read DeepSeek configuration or
make model requests:

```powershell
.\.venv\Scripts\python.exe -m nemoir discord-pilot --confirm-live
```

The pilot accepts only the exact three ordered messages stored in
`tests\fixtures\synthetic_bundle.json`. Any other content fails closed. The
`/route`, `/habitat-create`, and `/publish-bundle` commands are not registered
in pilot mode, so the bounded flow is `/tend`, synthetic message capture,
`/seal` (which generates and shows the claim-option menu), `/claim
options:"<n>"` selecting by the menu's actual displayed number
(comma-separate to combine candidates, e.g. two adjacent options as `"2,3"`,
or `/claim topic:"..."` for a custom topic), and review delivery to the
configured nursery channel. Candidate titles and summaries are Nemoir
interpretations; evidence quotations are exact source text. Ordinary synthetic
mode can never create channels.

### Synthetic channel-writing test

A separate, clearly labelled mode exercises `/publish-bundle` end to end with
the deterministic synthetic provider and a real Discord connection that may
create real channels in the configured development category. It never calls
DeepSeek:

```powershell
$env:NEMOIR_ALLOW_CHANNEL_WRITE = 'true'
.\.venv\Scripts\python.exe -m nemoir discord-channel-test --confirm-live
```

Both `--confirm-live` and `NEMOIR_ALLOW_CHANNEL_WRITE=true` are required; the
channel-write gate is enabled only for this child process and is also
re-checked by the server-side `/publish-bundle` command, so hiding a GUI button
is never sufficient protection. The Control Panel's `Start Channel Test` button
launches exactly this command after a confirmation dialog. Live mode requires
the same explicit `NEMOIR_ALLOW_CHANNEL_WRITE=true` gate in addition to
`/publish-bundle confirm:true`.

Manual Channel Test steps (human-observed; real channels, no DeepSeek):

1. Click `Start Channel Test` in the panel (or run
   `$env:NEMOIR_ALLOW_CHANNEL_WRITE='true'; .\.venv\Scripts\python.exe -m nemoir discord-channel-test --confirm-live`).
   Confirm the dialog explains it creates real channels, and the badge reaches
   `ONLINE — CHANNEL TEST` (mode `channel-test`), never `ONLINE — SYNTHETIC`.
2. In the intake channel run `/tend`, post the three fixture messages, `/seal`,
   and `/claim options:"1"`; wait for the review to reach `REVIEW_READY`.
3. Run `/publish-bundle bundle_id:<id>` and confirm the read-only preview lists
   each tendril's channel name, type, and evidence count with no channels yet.
4. Run `/publish-bundle bundle_id:<id> confirm:true` as a `Manage Channels`
   user; confirm one new channel per unclaimed tendril under the anemone
   category, each with its tendril's exact evidence and source links, and no
   channel for the claimed material.
5. Run the same `confirm:true` again; confirm every tendril reports
   `Already published` with no new channels or duplicate posts.
6. Confirm a non-`Manage Channels` user and a non-participant are both refused,
   and that ordinary `Start Synthetic` cannot run `/publish-bundle` at all.

This command still makes a real Discord connection. Run it only during an
explicitly approved, human-observed live verification step. Every Discord bot
startup — CLI or panel — records its runtime state atomically under
`data/runtime` and holds an OS-released single-instance lock, so a second bot
process fails safely and a crashed bot never blocks a later start.

### Synthetic autonomous pilot (Autonomous Test)

A separate, clearly labelled mode drives the default autonomous product
workflow end to end with the deterministic synthetic sort and a real Discord
connection that creates real shared channels. It never calls DeepSeek:

```powershell
$env:NEMOIR_ALLOW_CHANNEL_WRITE = 'true'
.\.venv\Scripts\python.exe -m nemoir discord-autonomous-test --confirm-live
```

Manual Autonomous Test steps (human-observed; real channels, no DeepSeek):

1. Click `Start Autonomous Test` in the panel (or run the command above).
   Confirm the dialog warns that real shared channels will be created, and the
   badge reaches `ONLINE — AUTONOMOUS TEST` (mode `autonomous-test`), never
   `ONLINE — SYNTHETIC`.
2. In the intake channel run `/tend` **without a recipient** (the command
   refuses to open a recipient-free capture in ordinary modes), then post your
   own messages.
3. Run `/seal`. Confirm the “sealed and queued” acknowledgement appears
   immediately and no provider work happens inside the interaction.
4. Walk away. In the background the synthetic sort validates full-source
   coverage, publishes every topic into its own channel under the anemone
   category, and posts a completion notice in the nursery channel that
   mentions only you, with the bundle ID and channel links. Verify `/tend` and
   `/seal` again with a second user: each user’s capture stays independent.
5. Run `/seal` with the same interaction a second time (or `/tend` twice) and
   confirm nothing is duplicated — one capture, one job, one set of channels.
6. Confirm the channels inherit the anemone category permissions (no private
   per-channel overwrites) and are visible to both workspace users through
   `View Channel` on the category.
7. Stop the bot. No live DeepSeek call is made at any point in this flow.

For Autonomous Live (real DeepSeek sorting), set **both**
`NEMOIR_ALLOW_LIVE_DEEPSEEK=true` and `NEMOIR_ALLOW_CHANNEL_WRITE=true` (or
click `Start Autonomous Live` in the panel after its paid-call confirmation),
and expect one paid model request per sealed capture.

The DeepSeek adapter refuses live calls unless all of the following are true:

1. the optional dependency is installed;
2. `DEEPSEEK_API_KEY` is configured outside source control;
3. `NEMOIR_ALLOW_LIVE_DEEPSEEK=true` is deliberately set;
4. application code constructs the adapter with the live gate enabled.

Ordinary tests and the offline demo make no external model requests.

Run the one-call contract check only during a human-observed verification step:

```powershell
$env:NEMOIR_ALLOW_LIVE_DEEPSEEK = 'true'
.\.venv\Scripts\python.exe scripts\check_deepseek.py --fixture tests\fixtures\synthetic_bundle.json --confirm-live
```

It makes one analysis request, performs no Discord writes, and prints the token/latency receipt plus validation codes without echoing fixture text.

The synthetic contract check passed against `deepseek-v4-flash` on 2026-08-28:
7/7 units covered, no validation errors or warnings, and no review required.

Discord startup is also an explicit human-observed step:

```powershell
.\scripts\start_discord_bot.ps1 -ConfirmLive
```

The platform-neutral domain does not import Discord types.
