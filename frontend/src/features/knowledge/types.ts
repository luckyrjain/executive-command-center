export type EntityKind = 'person' | 'organization' | 'project' | 'topic' | 'decision' | 'document'
export type EntityStatus = 'active' | 'archived' | 'redirected'

export type KnowledgeEntity = {
  id: string
  kind: EntityKind
  canonical_name: string
  summary: string | null
  status: EntityStatus
  confidence: number
  version: number
  created_at: string
  updated_at: string
}

export type EntityList = { items: KnowledgeEntity[]; next_cursor?: string | null }

export type EntityAlias = {
  id: string
  entity_id: string
  alias_type: string
  normalized_value: string
  source_id: string
  confidence: number
  created_at: string
}

export type EntityAliasList = { items: EntityAlias[] }

export type Claim = {
  id: string
  subject_id: string
  predicate: string
  value: Record<string, unknown>
  source_id: string
  confidence: number
  valid_from: string | null
  valid_to: string | null
  superseded_by: string | null
  created_at: string
}

export type ClaimList = { items: Claim[] }

export type RelationshipType =
  | 'MEMBER_OF' | 'PARTICIPATES_IN' | 'OWNS' | 'ASSIGNED_TO' | 'MAKES' | 'MADE_TO'
  | 'RELATES_TO' | 'ADVANCES' | 'THREATENS' | 'BLOCKS' | 'DEPENDS_ON' | 'PRODUCES'
  | 'SUPPORTS' | 'SUPERSEDES' | 'ABOUT' | 'MENTIONS' | 'DERIVED_FROM' | 'SCHEDULED_FOR'
  | 'PROPOSES_ACTION_ON' | 'HIGHLIGHTS' | 'WORKS_ON'

export const RELATIONSHIP_TYPES: RelationshipType[] = [
  'MEMBER_OF', 'PARTICIPATES_IN', 'OWNS', 'ASSIGNED_TO', 'MAKES', 'MADE_TO',
  'RELATES_TO', 'ADVANCES', 'THREATENS', 'BLOCKS', 'DEPENDS_ON', 'PRODUCES',
  'SUPPORTS', 'SUPERSEDES', 'ABOUT', 'MENTIONS', 'DERIVED_FROM', 'SCHEDULED_FOR',
  'PROPOSES_ACTION_ON', 'HIGHLIGHTS', 'WORKS_ON',
]

export type Relationship = {
  id: string
  from_entity_id: string
  to_entity_id: string
  relationship_type: RelationshipType
  confidence: number
  evidence_id: string
  valid_from: string | null
  valid_to: string | null
  status: 'active' | 'disputed' | 'invalidated'
}

export type RelationshipList = { items: Relationship[] }

export type TimelineEntry = {
  id: string
  entity_id: string
  effective_at: string
  recorded_at: string
  event_type: string
  source_id: string | null
  summary: string
}

export type TimelineList = { items: TimelineEntry[]; next_cursor?: string | null }

export type CandidateStatus = 'open' | 'confirmed' | 'rejected' | 'expired'

export type ResolutionCandidate = {
  id: string
  left_entity_id: string
  right_entity_id: string
  score: number
  factors: Record<string, number>
  resolver_version: string
  status: CandidateStatus
  created_at: string
  resolved_at: string | null
  resolved_by: string | null
  reason: string | null
  deferred_until: string | null
}

export type ResolutionCandidateList = { items: ResolutionCandidate[]; next_cursor?: string | null }

export type ResolutionCandidateResult = { deterministic: boolean; candidate: ResolutionCandidate | null }

export type EntityOperation = {
  id: string
  operation_type: 'merge' | 'reverse'
  status: 'active' | 'reversed'
  source_entity_id: string | null
  target_entity_id: string | null
  actor_id: string
  reason: string
  reverses_operation_id: string | null
  created_at: string
}

export type RetrievalResult = {
  entity_type: string
  entity_id: string
  title: string
  snippet: string
  score: number
  matching_mode: string
  factors: Record<string, number>
  evidence_state: string
  source_version: number
  stale: boolean
}

export type RetrievalResponse = {
  items: RetrievalResult[]
  next_cursor?: string | null
  mode: string
  degraded: boolean
  degraded_reason: string | null
}

export type EvidenceStatus = 'available' | 'missing'

export type EvidenceItem = {
  id: string
  status: EvidenceStatus
  source_type: string | null
  label: string | null
  captured_at: string | null
}

export type EvidenceListResponse = { items: EvidenceItem[] }
