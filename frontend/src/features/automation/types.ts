// Shared response/domain shapes for the Phase 5 automation surface --
// mirrors `docs/phases/phase-005/API-SCHEMAS.md` and the real backend
// response models in `backend/ecc/domains/automation/*.py` field-for-field
// (read directly, not guessed). Kept in one file per this codebase's own
// convention (`frontend/src/features/schedule/scheduleTypes.ts`).

export type WorkflowStatus = 'draft' | 'active' | 'retired'

export type GraphStep = {
  step_id: string
  step_type: 'action' | 'approval_gate' | 'condition' | 'compensation'
  action_ref?: string | null
  input_mapping?: Record<string, unknown>
  on_success?: string | null
  on_failure?: string | null
  compensate_ref?: string | null
}

export type WorkflowGraph = { steps: GraphStep[] }

export type WorkflowVersion = {
  id: string
  workflow_id: string
  version: number
  graph: WorkflowGraph
  trigger_refs: string[]
  policy_ref: string | null
  definition_hash: string
  status: WorkflowStatus
  created_at: string
  updated_at: string
}

export type WorkflowSummary = {
  workflow_id: string
  latest_version: number
  latest_status: WorkflowStatus
  active_version: number | null
  // Task 7 addition -- the row UUID `GET /workflows/{version_id}` actually
  // requires (never a bare version number). See workflows.py's own
  // `WorkflowSummary` docstring comment for the full "why this was
  // missing" reasoning.
  latest_version_id: string
  active_version_id: string | null
}

export type WorkflowListResponse = { workflows: WorkflowSummary[] }

// --- Simulation (POST /workflows/{id}/simulate) -----------------------

export type DispatchGate =
  | 'clear'
  | 'requires_approval'
  | 'policy_blocked'
  | 'adapter_not_registered'
  | 'not_applicable'

export type PolicyBlockReason = 'no_policy' | 'policy_revoked' | 'policy_expired'

export type SimulateStepResult = {
  step_index: number
  step_id: string
  step_type: string
  action_ref?: string | null
  compensate_ref?: string | null
  preview?: Record<string, unknown> | null
  reversible?: boolean | null
  high_impact_categories: string[]
  dispatch_gate: DispatchGate
  policy_block_reason?: PolicyBlockReason | null
  error?: string | null
}

export type SimulateResponse = {
  workflow_id: string
  version: number
  steps: SimulateStepResult[]
}

// --- Policies -----------------------------------------------------------

export type ApprovalMode = 'preview_only' | 'per_run' | 'bounded_recurring'
export type PolicyLifecycleStatus = 'active' | 'expired' | 'revoked'

export type Policy = {
  id: string
  workflow_id: string
  action_types: string[]
  data_classes: string[]
  value_limit: string
  count_limit: number
  rate_limit: Record<string, unknown>
  schedule: string | null
  approval_mode: ApprovalMode
  expires_at: string
  revoked_at: string | null
  status: PolicyLifecycleStatus
  version: number
  created_at: string
  updated_at: string
}

export type PolicyListResponse = { policies: Policy[] }

// --- Approvals ------------------------------------------------------------

export type ApprovalDecision = 'approved' | 'rejected'
export type ApprovalLifecycleStatus = 'pending' | 'approved' | 'rejected' | 'expired'

export type Approval = {
  id: string
  run_id: string
  step_index: number
  action_digest: string
  high_impact_categories: string[]
  status: ApprovalLifecycleStatus
  requested_at: string
  expires_at: string
  decided_at: string | null
  decision: ApprovalDecision | null
  decided_by: string | null
  created_at: string
  updated_at: string
}

export type ApprovalListResponse = { approvals: Approval[] }

// --- Runs -------------------------------------------------------------

export type RunStatus =
  | 'queued'
  | 'leased'
  | 'running'
  | 'waiting_approval'
  | 'paused'
  | 'needs_review'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'compensating'
  | 'compensated'
  | 'compensation_failed'
  | 'expired'
  | 'rate_limited'

export type RunStepStatus =
  | 'pending'
  | 'dispatched'
  | 'succeeded'
  | 'failed'
  | 'unknown'
  | 'skipped'
  | 'retrying'

export type RunStep = {
  step_index: number
  step_type: string
  status: RunStepStatus
  action_digest: string | null
  // Task 7 addition -- `workflow_run_steps.attempt_count` (Task 6) is what
  // makes the "retrying" state (`status === 'retrying'`) legible: which
  // attempt, out of the backend's fixed MAX_RETRY_ATTEMPTS = 3 (2s/4s/8s
  // backoff, `worker.py`), this step is on.
  attempt_count: number
  // Found during independent review: without this, an approver had no way
  // to see *which* adapter/action a step actually invokes (only the
  // generic `step_type` and raw input) -- resolved server-side from the
  // run's own pinned workflow-version graph. `null` for a step with no
  // adapter at all (`approval_gate`/`condition`).
  action_ref: string | null
  input: Record<string, unknown>
  output: Record<string, unknown> | null
  started_at: string | null
  finished_at: string | null
  error_class: string | null
}

export type CompensationStep = {
  compensates_step_index: number
  status: string
  action_digest: string | null
  started_at: string | null
  finished_at: string | null
  error_class: string | null
}

export type Run = {
  id: string
  workflow_id: string
  workflow_version: number
  policy_id: string | null
  trigger_ref: string | null
  status: RunStatus
  current_step_index: number
  queued_at: string
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

export type RunDetail = Run & { steps: RunStep[]; compensation_steps: CompensationStep[] }
export type RunListResponse = { runs: Run[] }

// --- Kill switches -----------------------------------------------------

export type KillSwitch = {
  workflow_id: string | null
  active: boolean
  reason: string | null
  activated_by: string | null
  activated_at: string | null
  deactivated_by: string | null
  deactivated_at: string | null
}

export type KillSwitchStatus = {
  workflow_id: string
  killed: boolean
  active_global: KillSwitch | null
  active_workflow: KillSwitch | null
  history: KillSwitch[]
}

// --- Triggers (GET /automations/triggers, Task 7's own additive endpoint) -

export type TriggerType = 'manual' | 'event' | 'schedule'

export type Trigger = {
  id: string
  workflow_id: string
  trigger_type: TriggerType
  event_type_filter: string | null
  schedule_expression: string | null
  timezone: string | null
  skip_missed: boolean
  last_fired_at: string | null
  created_at: string
  updated_at: string
}

export type TriggerListResponse = { triggers: Trigger[] }
