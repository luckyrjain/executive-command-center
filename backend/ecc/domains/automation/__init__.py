"""Phase 5 Automation domain package (Task 1: data layer only).

`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` /
`docs/phases/phase-005/DATA-MODEL.md` v0.2.0. This package owns
`workflow_definitions`/`workflow_versions` (`workflows.py`),
`automation_policies` (`policy.py`) and `triggers` (`triggers.py`) --
the three tables migration `0038_phase5_workflow_schema.py` creates.

No worker, scheduler, runtime orchestration loop or action-adapter registry
exists in this package yet (design doc's `worker.py`/`scheduler.py`/
`runtime.py`/`adapters.py`/`compensation.py` are later tasks, matching this
task's own explicit scope boundary: "the data layer only").
"""
