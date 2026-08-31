# Security and privacy

Nemoir is an experimental reference implementation, not a production security
boundary or a demonstrated multi-tenant service. Run it only in a Discord
guild and channels whose participants understand the experiment and the data
flow.

## Report a vulnerability privately

Do not put a credential, private message, database, provider response, or real
Discord link/identifier in a public issue.

Use GitHub's private vulnerability reporting or a Security Advisory if the
repository has that feature enabled. Otherwise contact the maintainer through
a private channel you already trust. If neither is available, open a public
issue containing no sensitive details and ask for a private reporting route.

Include only the minimum non-sensitive reproduction information until a safe
channel is established.

## Actual data behaviour

The current implementation:

- captures only messages deliberately posted by the capture owner while that
  owner has an open capture in an allow-listed intake channel;
- stores submitted message content, author/display identifiers, Discord source
  URLs, Claims, exact evidence, lifecycle records, questions and responses in
  a local SQLite database;
- can retain raw model-provider output and provider receipts for successful or
  failed semantic operations so invalid output remains reviewable;
- sends deliberately submitted source text or prompt questions to the
  configured external provider in live modes; and
- creates/posts to real Discord channels only through explicit modes and
  channel-write gates, although the autonomous live/test modes are designed to
  publish after a sealed capture.

The repository ignores `.env`, SQLite databases and sidecars, `data/`, runtime
state, logs, and common build/test artifacts. Ignore rules are a safeguard, not
a substitute for inspecting the exact tracked-file set before publication.
Do not share a zip of a live working directory without separately excluding and
reviewing ignored files.

## Credentials

- Never commit or paste `DISCORD_BOT_TOKEN`, `DEEPSEEK_API_KEY`, Discord scope
  identifiers, or bearer tokens into source, fixtures, issues, screenshots, or
  logs.
- Keep credentials in the launching environment or another untracked local
  secret mechanism. `.env.example` must contain names and safe defaults only.
- Use a least-privilege Discord bot and a dedicated development guild/category.
- Keep live provider and channel-write gates disabled by default.
- If a credential may have been exposed, revoke or rotate it at the provider
  first. Then preserve only redacted evidence for investigation. Removing it
  from the current file is not sufficient if it entered Git history.

## Conversation data

Assume every live SQLite database contains private conversational material,
even when exports omit raw provider responses. Protect backups and sidecar
files (`-wal` and `-shm`) with the same care as the main database.

Do not use real private conversations as public fixtures. Synthetic replacement
must preserve the relevant schema, semantic distinctions, source spans, and
assertions; if that cannot be established confidently, keep the material out
of the public repository and report the blocker.

Users must not assume local-only processing simply because Nemoir runs on a
local machine. The Discord connection is external, and live model adapters send
selected content to their configured provider. A genuinely local provider
would require its own implementation and configuration.

## Logs and errors

The logging layer redacts common bearer-token and API-key forms, and runtime
status is designed to store operational state rather than message content.
Redaction is best-effort and does not make arbitrary logs safe to publish.
Avoid logging request bodies, raw exceptions from remote services, source
message text, or provider output.
