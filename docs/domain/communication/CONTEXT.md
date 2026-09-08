---
id: CONTEXT-COMMUNICATION
title: Communication Context
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Communication

Tracking promises made between people. Despite the name, this context has no relationship to messaging.

## Language

**Commitment**:
A tracked promise or obligation, either made by the owner or made to the owner, naming a counterparty and
optionally linked to supporting evidence. Its lifecycle runs detected, confirmed, active, then fulfilled,
broken, or cancelled.

## Open question

`docs/domain/DOMAIN-MODEL.md` describes this context as also owning Conversation, Message, and EmailThread.
Verified against code: Conversation and Message don't exist anywhere in the codebase; EmailThread is real but
lives in [Personal](../personal/CONTEXT.md)'s Gmail Connector area, gated by that context's consent mechanism,
not this one. Commitment is the only concept this context actually owns. Whether "Communication" is still the
right name for a context that only tracks commitments — or whether Commitment should be renamed/relocated
instead — is an open naming decision, not resolved here.
