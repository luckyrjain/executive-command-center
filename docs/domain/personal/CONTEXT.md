---
id: CONTEXT-PERSONAL
title: Personal Context
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Personal

This package holds two genuinely separate sub-areas sharing only an outer package boundary — see the open
question below before treating this as one coherent context.

## Language — Personal Data Vault

**PersonalDomain**:
A per-owner enablement flag for one of six fixed categories (habits, learning, travel, relationships, health,
finance), each with a server-derived classification of standard, sensitive, or high_stakes.
_Avoid_: calling this just "domain" in a context where it could be confused with a bounded context — the code
itself uses the bare word, but this glossary needs the fuller name to stay unambiguous.

**DomainConsent**:
The grant/revoke history for a PersonalDomain. Re-enabling after a disable always writes a fresh consent
record rather than resurrecting the old one.

**DomainRecord**:
The generic vault's storage unit — a typed payload (contact, interaction, vital reading, symptom log, account,
or transaction) with narrative fields encrypted and structured fields left plaintext.

**CrossDomainGrant**:
A purpose-scoped read permission letting AI insight generation read across a different PersonalDomain than the
one being analyzed.

**PersonalInsight**:
A deterministic or AI-generated observation (a trend or a correlation) surfaced from a person's own data,
evidence-grounded and redacted in list views.

## Language — Gmail Connector

**EmailThread** / **EmailMessage**:
Structured, dedicated records for synced Gmail data — not stored as DomainRecords. Plaintext envelope fields
(sender, recipients, subject, direction); body and snippet individually encrypted, null until fetched. Gated
by the email PersonalDomain's consent, not by the connector-authorization mechanism in
[Engineering](../engineering/CONTEXT.md).

**GmailAdapter**:
A ConnectorAdapter implementation (see [Engineering](../engineering/CONTEXT.md)) handling Gmail's OAuth
exchange, sync, and entity resolution into the [Knowledge](../knowledge/CONTEXT.md) graph. Built on
Engineering's connector framework — it sits beside the Personal Data Vault, not inside it.

## Open question

Should Personal Data Vault and Gmail Connector be split into two real contexts? They share only a package
folder today — different data models, different authorization mechanisms (consent vs. connector auth),
different lifecycles. Nothing in this glossary depends on the split happening, but discussing "the Personal
context" without specifying which half invites confusion.
