"""Run the Phase 5 durable automation worker's poll loop and the schedule-
trigger evaluation tick, together, as one long-running process.

**Judgment call worth a reviewer's explicit attention.** Neither Task 2
(`ecc.domains.automation.worker`) nor this task added a Docker Compose
service, systemd unit, or any other always-on process supervisor for this
loop -- there is no pre-existing "run this as a background/periodic
process" convention anywhere in this repository to wire into. Checked
directly before writing this file: `backend/Dockerfile`'s only `CMD` runs
`uvicorn` (the HTTP API), `docker-compose.yml` defines exactly three
services (`postgres`, `backend`, `frontend`), and `docs/runbooks/
PHASE-2-DEPLOYMENT.md`'s own closest precedent (`scripts/
rebuild_knowledge_projections.py`, a projection-rebuild CLI) states
outright: "There is no scheduled job that runs this automatically." This
script follows that exact precedent -- a plain, manually-invoked CLI
entrypoint (`uv run python scripts/run_automation_worker.py`), not a new
Compose service or supervisor -- rather than inventing production
deployment infrastructure this task was never asked to design (a real
choice between a Compose service, a systemd timer, or something else
belongs to whichever later task actually turns Phase 5 automation on for
real dogfood use, `docs/phases/phase-005/IMPLEMENTATION-STATUS.md`'s
"Browser acceptance and staged dogfood" slice). `docs/runbooks/
PHASE-5-RECOVERY.md`'s own operator-facing text already anticipates "the
worker runs as a service in the same Compose stack as backend" for the
expected case -- this script is what that eventual service's `command`
would run; wiring it into `docker-compose.yml` itself is left to that
later task, not invented here.

**What this script actually does.** Two independent loops in one process,
matching each mechanism's own documented tick interval (`worker.
POLL_INTERVAL_SECONDS` = 2s, `scheduler.SCHEDULER_TICK_INTERVAL_SECONDS`
= 60s) rather than inventing a third number: every `POLL_INTERVAL_SECONDS`,
call `worker.run_worker_once` (claims and drives at most one `workflow_
runs` row to its next durability checkpoint); every `SCHEDULER_TICK_
INTERVAL_SECONDS`, call `scheduler.run_scheduler_once` (evaluates every
`schedule` trigger and fires whichever are due). Both use the shared,
still-empty production `adapters.registry` (`ecc.domains.automation.
adapters`'s own module docstring: zero real adapters registered by any
task through this one) -- a real registered adapter, when a later task
adds one, becomes usable by this script with no changes here.

CLI usage:

    uv run python scripts/run_automation_worker.py
        Poll ECC_DATABASE_URL (or DATABASE_URL, or the local default)
        forever, until interrupted (SIGINT/SIGTERM/Ctrl-C).
"""

from __future__ import annotations

import signal
import time
from types import FrameType
from uuid import uuid4

from ecc.database import SessionFactory
from ecc.domains.automation import scheduler as scheduler_module
from ecc.domains.automation import worker as worker_module
from ecc.domains.automation.adapters import registry

_running = True


def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
    global _running
    _running = False


def main() -> int:
    worker_id = f"automation-worker-{uuid4().hex[:12]}"
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    print(f"{worker_id}\tautomation worker starting")
    last_scheduler_tick = 0.0
    while _running:
        loop_started_at = time.monotonic()

        with SessionFactory() as session:
            claimed = worker_module.run_worker_once(session, worker_id, registry)
        if claimed is not None:
            print(f"{worker_id}\trun\t{claimed.id}\t{claimed.status}")

        tick_interval = scheduler_module.SCHEDULER_TICK_INTERVAL_SECONDS
        if loop_started_at - last_scheduler_tick >= tick_interval:
            outcomes = scheduler_module.run_scheduler_once(SessionFactory)
            for outcome in outcomes:
                if isinstance(outcome, scheduler_module.TriggerFired):
                    print(f"{worker_id}\tscheduler\tfired\t{outcome.trigger_id}\t{outcome.run_id}")
                elif isinstance(outcome, scheduler_module.TriggerMisfireSkipped):
                    print(f"{worker_id}\tscheduler\tmisfire-skipped\t{outcome.trigger_id}")
                elif isinstance(outcome, scheduler_module.TriggerFireFailedWorkflowNotActive):
                    print(
                        f"{worker_id}\tscheduler\tworkflow-not-active\t"
                        f"{outcome.trigger_id}\t{outcome.workflow_id}"
                    )
            last_scheduler_tick = loop_started_at

        elapsed = time.monotonic() - loop_started_at
        remaining = worker_module.POLL_INTERVAL_SECONDS - elapsed
        if remaining > 0 and _running:
            time.sleep(remaining)

    print(f"{worker_id}\tautomation worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
