export type PersonalView = 'domains' | 'records' | 'insights' | 'grants' | 'export'

export type DomainKey = 'habits' | 'learning' | 'travel' | 'relationships' | 'health' | 'finance'

export type Classification = 'standard' | 'sensitive' | 'high_stakes'

// Mirrors `backend/ecc/domains/personal/domains.py`'s own
// `_CLASSIFICATION_BY_DOMAIN` -- the six domains and their classification
// are a closed enum fixed since Task 1's migration, never returned by any
// list endpoint on their own (only enabled domains have a `personal_
// domains` row at all), so the frontend keeps its own copy the same way
// `ConnectorHealthPanel.tsx`'s `PROVIDERS` list keeps its own copy of a
// backend-fixed enum.
export const DOMAIN_KEYS: readonly DomainKey[] = ['habits', 'learning', 'travel', 'relationships', 'health', 'finance']

export const CLASSIFICATION_BY_DOMAIN: Record<DomainKey, Classification> = {
  habits: 'standard',
  learning: 'standard',
  travel: 'standard',
  relationships: 'sensitive',
  health: 'high_stakes',
  finance: 'high_stakes',
}

export const DOMAIN_LABELS: Record<DomainKey, string> = {
  habits: 'Habits',
  learning: 'Learning',
  travel: 'Travel',
  relationships: 'Relationships',
  health: 'Health',
  finance: 'Finance',
}

export type Domain = {
  id: string
  domain_key: string
  classification: Classification
  enabled: boolean
  enabled_at: string | null
  version: number
  created_at: string
  updated_at: string
}

export type DomainListResponse = { domains: Domain[] }

export type PersonalRecord = {
  id: string
  domain_key: string
  record_type: string
  payload: Record<string, unknown>
  domain_source_id: string | null
  effective_at: string
  retention_acknowledged_at: string | null
  version: number
  created_at: string
  updated_at: string
}

export type RecordListResponse = { records: PersonalRecord[] }

export type InsightKind = 'observation' | 'trend' | 'correlation' | 'reminder' | 'planning_suggestion'
export type Confidence = 'low' | 'medium' | 'high'

export type Insight = {
  id: string
  domain_key: string
  kind: InsightKind
  title: string
  evidence: Record<string, unknown>
  source_period_start: string
  source_period_end: string
  missing_data: string | null
  confidence: Confidence
  limitations: string
  // Non-empty specifically when a source is `high_stakes` -- the
  // "sensitive insight" boundary this surface must show near the insight
  // itself (`UX-STATES.md`: "Health and finance surfaces show boundaries
  // near the insight").
  professional_referral_note: string | null
  computed_at: string
  dismissed_at: string | null
}

export type InsightListResponse = { insights: Insight[] }

export type InsightGenerateResponse = {
  available: boolean
  insight: Insight | null
  error_code: string | null
}

export type Grant = {
  id: string
  source_domain_key: string
  purpose: string
  granted_categories: string[]
  granted_at: string
  expires_at: string | null
  revoked_at: string | null
  created_at: string
}

export type GrantListResponse = { grants: Grant[] }

export type DomainExport = {
  domain_key: string
  exported_at: string
  goals: Array<Record<string, unknown>>
  routines: Array<Record<string, unknown>>
  check_ins: Array<Record<string, unknown>>
  domain_records: Array<Record<string, unknown>>
}

export type DomainDeletion = {
  id: string
  domain_key: string
  status: string
  requested_at: string
  completed_at: string | null
}
