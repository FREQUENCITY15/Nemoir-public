# Contributing to Nemoir

Nemoir is an experimental reference implementation. Contributions should make
the tendril model easier to understand, test, or port without pretending that
the current Discord/private-alpha behaviour is production-ready.

## Local setup

Nemoir requires Python 3.10 or newer. On Windows PowerShell, from the repository
root:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

Install `.[all]` only when working on the optional Discord or DeepSeek
adapters. The deterministic suite and offline demo must not require live
Discord, network access, or API credits.

`.env.example` is a list of configuration names and safe defaults. Nemoir does
not automatically load a `.env` file; set values in the launching process or
use a local, untracked environment loader. Never put real values in the
example file.

## Architectural boundary

Keep the reusable Nemoir model separate from reference adapters:

- Core behaviour: Claim, semantic exclusion, Tendril recovery, provenance,
  validation, lifecycle, review, and resurfacing.
- Adapters and infrastructure: Discord commands/messages/channels, model
  providers, SQLite, the Windows GUI, and launch scripts.

Core domain objects must not depend on Discord types or a provider-specific
response shape. A new adapter should translate at the boundary rather than
moving platform assumptions into the domain.

## Evidence and tests

- Preserve exact source evidence, coverage, claim exclusion, and append-only
  lifecycle behaviour when changing the semantic workflow.
- Add deterministic offline tests for behaviour changes and failure paths.
- Do not loosen assertions to make a rewritten fixture pass.
- Use synthetic names, identifiers, URLs, and conversation text. Do not derive
  public fixtures from private conversations unless informed consent and the
  intended public wording are documented outside the fixture.
- Never enable live Discord/channel writes or paid model calls in automated
  tests.

## Proposing a change

Keep proposals small and state whether they affect the core concept, an
adapter/provider, persistence, or documentation. Explain the behaviour being
preserved, the new evidence or test, privacy implications, and any unresolved
semantic judgement. Pull requests should include the deterministic checks that
were actually run; live checks require separate human approval and should not
be presented as ordinary CI evidence.

Do not include credentials, database files, logs, provider raw output, Discord
exports, real message links, or private conversation excerpts in an issue or
pull request. Follow [SECURITY.md](SECURITY.md) for sensitive reports.

By contributing, you agree that your contribution is licensed under the
repository's [Apache License 2.0](LICENSE).
