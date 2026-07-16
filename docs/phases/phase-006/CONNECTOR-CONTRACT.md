---
id: PHASE-006-CONNECTOR
title: Engineering Connector Contract
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Engineering Connector Contract

Connectors implement authorize, validate, backfill, incremental sync, webhook ingestion where available, permission refresh and disconnect. Tokens use least privilege and encrypted secret storage. Sync persists cursor only after durable projection, deduplicates webhook/poll overlap and handles rate limits with bounded backoff.

Provider deletion, access loss and rename are distinct states. Disconnect revokes credentials when possible and stops future sync; locally retained records follow configured retention. Connector payloads are untrusted and cannot issue runtime instructions.
