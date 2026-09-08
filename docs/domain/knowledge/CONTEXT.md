# Knowledge

The entity graph: people, organizations, and everything known and evidenced about them.

## Language

**Entity**:
A node in the knowledge graph — a person, organization, project, topic, decision, document, or team.
_Avoid_: KnowledgeItem (used in `docs/domain/DOMAIN-MODEL.md`, but no such class or table exists in code —
Entity is the real, single generic node type), node

**EntityAlias**:
An alternate name or identifier that resolves to an Entity.

**Claim**:
A time-bounded, sourced assertion (a predicate and a value) about an Entity.

**Relationship**:
A typed, directed, evidenced connection between two Entities.
_Avoid_: edge

**Evidence**:
A locator and provenance record backing a Claim or a Relationship. Never hard-deleted — only redacted when its
source is deleted.

**Note**:
A free-text record owned by a user, optionally linked to a Meeting, typed as general, meeting, decision, or
journal.

**ResolutionCandidate**:
A proposed match between two Entities suspected of being duplicates, awaiting human confirmation or deferral.

**EntityOperation**:
A reversible merge or split action performed on Entities.

## Open questions

- **"Decision" is overloaded three ways with no shared meaning**: an Entity kind (a generic knowledge-graph
  node), a Note kind (what a MeetingPack's own "decisions" section actually returns — a list of Notes), and a
  wholly separate `engineering_decisions` record owned by [Engineering](../engineering/CONTEXT.md) (Phase 6,
  human-authored, unrelated to the entity graph). These are three unconnected concepts sharing one English
  word — worth naming distinctly if confusion comes up in discussion.
- Person and Organization are conceptually owned by
  [Identity & Access](../identity-access/CONTEXT.md), but all persistence and business rules for them actually
  live here, in Knowledge's entity graph. Not resolved which context should be considered authoritative for
  discussion purposes.
