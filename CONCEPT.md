# The Nemoir concept

Nemoir is a model for preserving worthwhile conversational branches that were
raised but not meaningfully taken up. The model is independent of Discord,
DeepSeek, Python, SQLite, or any particular user interface.

The original Nemoir concept is attributed to **Thom Finlayson**.

## Defining operation

A recipient chooses the part of a compound conversation they intend to answer
first. That claimed semantic region is excluded from branch recovery.
Meaningful unaddressed material remaining outside the claim can then be
preserved as resumable Tendrils with provenance and human review.

```text
Conversation Bundle
-> Claim
-> semantic exclusion of claimed material
-> unclaimed remainder
-> Tendril recovery
-> review
-> lifecycle and later return
```

The claim is not merely another detected topic. It is a human choice that
changes what the recovery operation is allowed to return.

## Core terms

### Conversation Bundle

One or more deliberately submitted source messages considered together. A
bundle preserves source order, speaker and platform references, and immutable
source evidence where the platform permits it.

### Claim

The recipient's declaration of the semantic region they intend to take up in
the live conversation. A claim can be expressed in the recipient's own words
or selected from reviewable, source-backed options.

### Claimed material

The exact source-backed region matched to the Claim. The boundary must remain
inspectable: a label alone is not enough. Evidence that belongs to the Claim
must not reappear as an independent Tendril.

### Semantic exclusion or semantic subtraction

The operation that removes the meaning covered by the Claim from branch
recovery while retaining the original Bundle unchanged. This is semantic,
because the claimed idea can span or overlap message boundaries; it is not
destructive text deletion.

### Unclaimed remainder

The source-backed material outside the Claim. Not every word in the remainder
must become a Tendril: connective context and substantive repetition may be
accounted for without becoming resumable branches. Meaningful material must
not silently disappear.

### Tendril

A meaningful unfinished edge in the unclaimed remainder that could support a
later continuation. Examples include an open question, claim, disagreement,
research lead, decision, task candidate, or project seed. A Tendril combines a
concise interpretation with exact provenance; the interpretation never
replaces the evidence.

### Provenance and source evidence

The record showing why a Claim or Tendril exists: source message identity,
exact quotations or spans, source order, and a durable source reference where
available. Provenance lets a person inspect the original context and challenge
an incorrect boundary, split, merge, or interpretation.

### Review

The human checkpoint between model output and trusted conversational
structure. Review keeps uncertain semantic boundaries visible and prevents an
inference about importance, actionability, or routing from becoming authority.

### Tendril lifecycle

The states and events through which a Tendril remains open, becomes dormant or
snoozed, is routed or resurfaced, is explicitly promoted, is merged, is
resolved, or is deliberately released. Lifecycle history should be retained so
later systems can explain what changed without rewriting provenance.

### Resurfacing or return

The deliberate reintroduction of a still-relevant Tendril into human
attention. Return may be requested manually or proposed by a future policy,
but it must preserve provenance and avoid treating every old branch as an
interruption.

## What makes Nemoir distinct

Nemoir is not generic summarisation: compression can describe a branch without
preserving it as a resumable object with evidence and lifecycle.

Nemoir is not merely topic classification: the defining boundary is relative
to a human Claim, not just a set of detected subjects.

Nemoir is not automatically task extraction: questions, disagreements,
research leads, reflections, and project seeds may be valuable without being
commitments. Actionability is an interpretation until a human promotes it.

Human choice remains primary. Semantic boundaries are uncertain, so Claims,
exclusions, Tendrils, and routing suggestions must remain reviewable.

## Invariants

- The original Conversation Bundle remains intact.
- Claimed evidence is not recovered again as an independent Tendril.
- Every Tendril is supported by source evidence.
- Meaningful source material is claimed, preserved, or explicitly accounted
  for; it is not silently lost.
- Model interpretations do not authorise actions or overwrite human choices.
- Lifecycle changes append history instead of erasing provenance.
- Uncertainty is surfaced for review rather than hidden by confident prose.

## Relationship to the reference implementation

The repository's Discord integration is a proof surface for the model. Its
provider adapters, SQLite schema, channel routing, control panel, and command
set are replaceable implementation choices.

The current Discord implementation also offers a recipient-free autonomous
sorting mode. In that extension there is no Claim, so every source unit is
assigned to a recovered topic before automatic publishing. That behaviour is
implemented and experimental, but it is not the defining claim-relative
operation described above. Future ports should keep the core model and such
extensions distinguishable.
