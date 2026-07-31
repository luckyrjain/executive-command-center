// Shared response/domain shapes for the Phase 6 Engineering Workspace
// surface -- mirrors `docs/phases/phase-006/API-SCHEMAS.md` and the real
// backend response models in `backend/ecc/domains/engineering/*.py`
// field-for-field (read directly, not guessed), matching this codebase's
// own convention (`frontend/src/features/automation/types.ts`).

export type EngineeringView =
  | 'overview'
  | 'delivery'
  | 'reliability'
  | 'repositories'
  | 'work-items'
  | 'incidents'
  | 'decisions'
  | 'connector-health'
  | 'coverage'

export type ConnectorProvider = 'github' | 'gitlab' | 'jira' | 'datadog' | 'sandbox'

export type ConnectorStatus =
  | 'pending'
  | 'active'
  | 'permission_lost'
  | 'rate_limited'
  | 'disconnected'
  | 'error'

export type ConnectorAccount = {
  id: string
  provider: ConnectorProvider
  external_account_id: string
  display_name: string
  granted_scopes: string[]
  status: ConnectorStatus
  status_detail: string | null
  last_synced_at: string | null
  last_error: string | null
  disconnected_at: string | null
  version: number
  created_at: string
  updated_at: string
}

export type ConnectorAccountListResponse = { connectors: ConnectorAccount[] }

export type SyncRunType = 'backfill' | 'incremental' | 'webhook'
export type SyncRunStatus = 'running' | 'succeeded' | 'failed' | 'partial'

export type SyncRun = {
  id: string
  connector_account_id: string
  run_type: SyncRunType
  status: SyncRunStatus
  items_processed: number
  error_summary: string | null
  started_at: string
  completed_at: string | null
}

export type SyncRunListResponse = { sync_runs: SyncRun[] }

// --- Delivery/reliability metrics (`GET /engineering/metrics`) ---------

export type MetricKey =
  | 'delivery_frequency'
  | 'lead_time_for_changes'
  | 'change_failure_rate'
  | 'time_to_restore'
  | 'work_ageing'
  | 'blocked_work'
  | 'review_latency'

export type CoverageStatus = 'complete' | 'partial' | 'insufficient_coverage'

export type MetricSnapshot = {
  id: string
  metric_key: MetricKey
  window_label: string
  population: number
  numerator: number | null
  denominator: number | null
  value: number | null
  details: Record<string, unknown> | null
  coverage_status: CoverageStatus
  coverage_percentage: number
  coverage_gap_description: string | null
  computed_at: string
}

export type MetricsListResponse = { metrics: MetricSnapshot[] }

// --- Incidents/decisions (Task 6) --------------------------------------

export type IncidentSeverity = 'low' | 'medium' | 'high' | 'critical'
export type IncidentStatus = 'open' | 'resolved'

export type Incident = {
  id: string
  title: string
  description: string | null
  severity: IncidentSeverity
  status: IncidentStatus
  detected_at: string
  resolved_at: string | null
  change_ids: string[]
  version: number
  created_at: string
  updated_at: string
}

export type IncidentListResponse = { incidents: Incident[] }

export type DecisionStatus = 'proposed' | 'decided' | 'superseded'

export type Decision = {
  id: string
  title: string
  description: string | null
  rationale: string | null
  status: DecisionStatus
  decided_at: string | null
  change_ids: string[]
  version: number
  created_at: string
  updated_at: string
}

export type DecisionListResponse = { decisions: Decision[] }

// --- Repositories/work items (Task 8's own additive query endpoints,
// see `connector_accounts.py`'s own comment above `RepositoryResponse`) --

export type PermissionState = 'active' | 'permission_lost' | 'deleted'
export type FreshnessState = 'fresh' | 'stale' | 'disconnected'

export type Repository = {
  id: string
  connector_account_id: string
  provider: ConnectorProvider
  external_id: string
  name: string
  source_url: string
  default_branch: string | null
  permission_state: PermissionState
  freshness_state: FreshnessState
  provider_updated_at: string | null
  observed_at: string
  created_at: string
  updated_at: string
  // Team linkage (migration `0050_phase6_team_linkage.py`) -- `team_entity_id`
  // is the human-confirmed link (set only via `POST .../team` below, never by
  // a sync); `suggested_team_name` is a sync-refreshed hint a human reads
  // before confirming, never itself a link. `team_assignment_version`/
  // `team_assignment_updated_by` are that endpoint's own optimistic-
  // concurrency/audit fields, scoped to this one field (not the whole row --
  // see the migration's own docstring for why). See that migration's own
  // docstring for the "hybrid: auto-suggest, human confirms" design.
  team_entity_id: string | null
  suggested_team_name: string | null
  team_assignment_version: number
  team_assignment_updated_by: string | null
}

export type RepositoryListResponse = { repositories: Repository[] }

export type WorkItem = {
  id: string
  connector_account_id: string
  provider: ConnectorProvider
  external_id: string
  title: string
  source_url: string
  item_type: string | null
  status: string | null
  // Raw, never-resolved provider identifiers -- there is no resolution-
  // candidate mechanism for engineering identities (unlike Phase 2's
  // `resolution_candidates`), so these are shown as a disclosure, not
  // resolved to a person.
  reporter_external_id: string | null
  assignee_external_id: string | null
  permission_state: PermissionState
  freshness_state: FreshnessState
  provider_created_at: string | null
  observed_at: string
  created_at: string
  updated_at: string
  // See `Repository`'s identical fields above.
  team_entity_id: string | null
  suggested_team_name: string | null
  team_assignment_version: number
  team_assignment_updated_by: string | null
}

export type WorkItemListResponse = { work_items: WorkItem[] }

// --- Team assignment (`POST /engineering/repositories|work-items/{id}/team`) --

export type TeamAssignmentRequest = { expected_version: number; team_entity_id: string | null }
