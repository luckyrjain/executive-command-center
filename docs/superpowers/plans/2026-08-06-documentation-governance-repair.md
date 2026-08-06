# Documentation Governance Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish one enforceable documentation control plane, reconcile current repository claims, and add the missing Phase 10, operational-readiness, recovery, evidence, and KPI contracts without changing runtime behavior.

**Architecture:** `docs/phases/status.json` is the canonical current-state registry. A dependency-free Python renderer owns bounded generated blocks in the README, roadmap, and phase index; a separate dependency-free validator checks registry structure, generated drift, governed metadata, phase contracts, local links/anchors, durable evidence references, and `.env.example` coverage. Human-authored contracts and runbooks remain Markdown and explicitly distinguish shipped behavior from planned or unsupported behavior.

**Tech Stack:** Python 3.14 standard library, `unittest`/pytest test discovery, JSON, Markdown, Make, GitHub Actions.

## Global Constraints

- Do not change backend, frontend, database, API, authentication, recovery, encryption-key rotation, product telemetry, connector, or Gmail runtime behavior.
- Do not edit or stage `docs/phases/phase-006/TEST-PLAN.md` or `graphify-out/graph.html`; both contained unrelated user-owned modifications before implementation began.
- Use Python's standard library only for `scripts/docs_status.py` and `scripts/check_docs.py`.
- Treat `docs/phases/status.json` as the only source of current phase state; generated Markdown is a projection, never a second source.
- Metadata `status` values are bounded lifecycle states; task history belongs in document bodies.
- Do not close dogfood, human review, production-readiness, validation, or promotion gates without durable evidence.
- Unsupported production operations must say `Unsupported — production blocker` and must not recommend direct-SQL workarounds.
- Every executable behavior slice follows red-green-refactor and is committed independently.

---

## File map

| Area | Files | Responsibility |
|---|---|---|
| Canonical status | `docs/phases/status.json` | Machine-readable current state for Phases 0-10 |
| Status rendering | `scripts/docs_status.py` | Parse/validate the registry and render/check bounded Markdown blocks |
| Structural validation | `scripts/check_docs.py` | Run all repository documentation checks and emit actionable failures |
| Tests | `tests/test_docs_status.py`, `tests/test_check_docs.py` | Exercise behavior against temporary repositories and hand-derived fixtures |
| Generated projections | `README.md`, `docs/ROADMAP.md`, `docs/phases/README.md` | Current phase table and open-gate summary only inside generated markers |
| Governance | `docs/RFC-000.md`, `docs/RFC-001.md`, `docs/RFC-002.md`, `docs/RFC-003.md`, `docs/RFC-004.md`, `docs/00-document-control.md`, templates | Current lifecycle, inheritance, metadata, changelog, and freeze rules |
| Status reconciliation | Phase specs and `docs/phases/phase-*/IMPLEMENTATION-STATUS.md`, `docs/phases/PHASE-REVIEW.md` | Correct bounded statuses and keep detailed delivery summaries in bodies |
| Phase 10 contracts | `docs/phases/PHASE-010-gmail-connector.md`, six files under `docs/phases/phase-010/` | Normative contracts separating Tasks 1-2 current behavior from Tasks 3-8 planned behavior |
| Operations/evidence/product | `docs/evidence/README.md`, `docs/operations/PRODUCTION-READINESS.md`, four recovery runbooks, `docs/product/KPI-CONTRACT.md` | Durable evidence rules, blockers, containment/recovery, and measurable KPI definitions |
| Setup and enforcement | `.env.example`, `docs/SETUP.md`, `docs/CONTRIBUTING.md`, `docs/adr/README.md`, `Makefile`, `.github/workflows/ci.yml`, `.github/pull_request_template.md` | Accurate configuration, navigation, local check, and CI gate |

### Task 1: Canonical registry and deterministic renderer

**Files:**
- Create: `docs/phases/status.json`
- Create: `scripts/docs_status.py`
- Create: `tests/test_docs_status.py`
- Modify: `README.md`
- Modify: `docs/ROADMAP.md`
- Modify: `docs/phases/README.md`

**Interfaces:**
- Produces: `load_registry(path: Path) -> dict[str, object]`
- Produces: `validate_registry(data: object, repo_root: Path) -> list[str]`
- Produces: `render_status_block(data: dict[str, object], target: str) -> str`
- Produces CLI: `python scripts/docs_status.py {render,check} [--root PATH]`
- Generated markers: `<!-- BEGIN GENERATED PHASE STATUS -->` and `<!-- END GENERATED PHASE STATUS -->`

- [ ] **Step 1: Write failing registry and renderer tests**

Create temporary repositories in `tests/test_docs_status.py`. Use literal expectations for these behaviors:

```python
def test_validate_registry_rejects_duplicate_phase_number(tmp_path: Path) -> None:
    data = registry_with_two_entries(number=1)
    assert validate_registry(data, tmp_path) == ["duplicate phase number: 1"]


def test_render_status_block_separates_engineering_validation_and_promotion() -> None:
    block = render_status_block(minimal_registry(), "README.md")
    assert "| 10 | Gmail Connector | In progress | Not started | Not promoted |" in block
    assert "Tasks 3-8" in block


def test_check_returns_nonzero_when_generated_block_is_stale(tmp_path: Path) -> None:
    completed = run_cli(tmp_path, "check")
    assert completed.returncode == 1
    assert "README.md: generated phase status is stale" in completed.stderr
```

- [ ] **Step 2: Run the focused tests and confirm the intended red signal**

Run: `uv run pytest -q tests/test_docs_status.py`

Expected: collection fails because `scripts.docs_status` does not exist.

- [ ] **Step 3: Implement the minimal parser, validation, renderer, and CLI**

Implement closed state sets, schema version `1`, unique phase IDs/numbers, contiguous Phase 0-10 coverage, referenced-file existence, marker replacement, deterministic table ordering, `render`, and drift-only `check`. Do not parse Markdown outside the owned marker block.

- [ ] **Step 4: Add the canonical Phase 0-10 registry**

Record separate specification, engineering, validation, and promotion states. Record Phase 10 as `in_progress`, `Tasks 1-2 of 8 complete`, with Tasks 3-8 and real-account/backup/change-review gates open. Record all other open dogfood, validation, independent-review, and promotion gates without upgrading them from the repository evidence.

- [ ] **Step 5: Generate the three status projections and verify green**

Run:

```bash
python scripts/docs_status.py render
uv run pytest -q tests/test_docs_status.py
python scripts/docs_status.py check
```

Expected: all tests pass and the check exits `0`.

- [ ] **Step 6: Commit the independent renderer slice**

```bash
git add docs/phases/status.json scripts/docs_status.py tests/test_docs_status.py README.md docs/ROADMAP.md docs/phases/README.md
git commit -m "docs: add canonical phase status registry"
```

### Task 2: Structural documentation validator

**Files:**
- Create: `scripts/check_docs.py`
- Create: `tests/test_check_docs.py`

**Interfaces:**
- Consumes: `load_registry`, `validate_registry`, and generated-block checking from `scripts.docs_status`
- Produces: `parse_frontmatter(path: Path) -> dict[str, object]`
- Produces: `github_anchor(text: str) -> str`
- Produces: `validate_repository(root: Path) -> list[str]`
- Produces CLI: `python scripts/check_docs.py [--root PATH]`

- [ ] **Step 1: Write failing behavior tests using temporary repositories**

Each test names one break and asserts an exact actionable diagnostic:

```python
def test_rejects_missing_governed_metadata(tmp_path: Path) -> None:
    write(tmp_path / "docs/adr/ADR-0001-example.md", "# Example\n")
    assert validate_repository(tmp_path) == [
        "docs/adr/ADR-0001-example.md: missing front matter fields: id, owner, status, title, version"
    ]


def test_rejects_broken_relative_heading_anchor(tmp_path: Path) -> None:
    write(tmp_path / "docs/a.md", "[target](b.md#missing)\n")
    write(tmp_path / "docs/b.md", "# Present\n")
    assert "docs/a.md: broken anchor b.md#missing" in validate_repository(tmp_path)


def test_rejects_missing_environment_alias(tmp_path: Path) -> None:
    write(tmp_path / "backend/ecc/config.py", 'Field(default=False, validation_alias="ECC_FEATURE")')
    write(tmp_path / ".env.example", "ECC_ENV=development\n")
    assert "ECC_FEATURE is missing from .env.example" in validate_repository(tmp_path)
```

Also cover invalid lifecycle status, a missing approved-phase contract, duplicate GitHub-style headings, fenced-code link exclusion, external/mailto links, dead `.superpowers/sdd/` evidence, and a legacy authoritative filename.

- [ ] **Step 2: Run the validator tests and observe red**

Run: `uv run pytest -q tests/test_check_docs.py`

Expected: collection fails because `scripts.check_docs` does not exist.

- [ ] **Step 3: Implement focused validation functions**

Implement frontmatter parsing for the repository's YAML subset without PyYAML, governed-file classification, lifecycle validation by document class, Markdown link and GitHub-anchor resolution outside fenced blocks, contract existence, phase/index coverage, durable-evidence reference rejection, environment-alias comparison, and legacy-authority rejection. Return all diagnostics sorted by path and message; never stop after the first failure.

- [ ] **Step 4: Verify focused green and run the validator against the real repository**

Run:

```bash
uv run pytest -q tests/test_check_docs.py tests/test_docs_status.py
python scripts/check_docs.py
```

Expected: unit tests pass; the real-repository invocation exits non-zero and prints the existing documentation debt that Tasks 3-5 will correct. Save the diagnostic list in the implementation notes; do not weaken rules merely to make it shorter.

- [ ] **Step 5: Commit the validator slice**

```bash
git add scripts/check_docs.py tests/test_check_docs.py
git commit -m "docs: add structural documentation validator"
```

### Task 3: Reconcile governance, lifecycle metadata, and durable evidence

**Files:**
- Modify: `docs/RFC-000.md`
- Modify: `docs/RFC-001.md`
- Modify: `docs/RFC-002.md`
- Modify: `docs/RFC-003.md`
- Modify: `docs/RFC-004.md`
- Modify: `docs/00-document-control.md`
- Modify: `docs/phases/PHASE-002-knowledge-platform.md`
- Modify: `docs/phases/PHASE-REVIEW.md`
- Modify: `docs/phases/phase-001/IMPLEMENTATION-STATUS.md`
- Modify: `docs/phases/phase-002/IMPLEMENTATION-STATUS.md`
- Modify: `docs/phases/phase-003/IMPLEMENTATION-STATUS.md`
- Modify: `docs/phases/phase-004/IMPLEMENTATION-STATUS.md`
- Modify: `docs/phases/phase-005/IMPLEMENTATION-STATUS.md`
- Modify: `docs/phases/phase-006/IMPLEMENTATION-STATUS.md`
- Modify: `docs/phases/phase-007/IMPLEMENTATION-STATUS.md`
- Modify: `docs/phases/phase-008/IMPLEMENTATION-STATUS.md`
- Modify: `docs/phases/phase-010/IMPLEMENTATION-STATUS.md`
- Modify: governed templates under `docs/templates/`
- Create: `docs/evidence/README.md`

**Interfaces:**
- Consumes: lifecycle rules enforced by `validate_repository`
- Produces: governed metadata and evidence references that pass the validator without changing historical delivery facts

- [ ] **Step 1: Capture the current validator failures for this slice**

Run: `python scripts/check_docs.py 2>&1 | tee /tmp/ecc-docs-before-governance.txt`

Expected: failures identify unbounded implementation statuses, stale governance states, dead evidence pointers, missing/invalid governed metadata, or legacy inheritance claims.

- [ ] **Step 2: Reconcile governance documents**

Set RFC-000 through RFC-004 to `Approved` with semantic version/changelog entries for this governance decision. In RFC-000, define the exact lifecycles from the design, governed-document scope, README/generated exceptions, changelog scope, and promotion-only specification tags. Update `00-document-control.md` to reference only the existing RFC-001 through RFC-005, STD-001, SPEC-000, architecture, domain, phase, security, operations, and runbook hierarchy.

- [ ] **Step 3: Reconcile phase and implementation-status metadata**

Set PHASE-002 to `Approved for Implementation`. Set active Phase 2-8 and Phase 10 implementation reports to bounded `Active` while keeping their detailed delivery text under `## Overall status` or directly below the title. Keep Phase 1 active because exit validation is open. Mark `PHASE-REVIEW.md` `Archived`, add `as_of: 2026-07-23`, and add a visible historical-snapshot banner linked to `status.json`.

- [ ] **Step 4: Replace non-durable evidence claims**

Create `docs/evidence/README.md` with the accepted evidence types from the design. Remove `.superpowers/sdd/` as inspectable evidence from Phase 1 status/release/final-acceptance records; replace each with a committed source/test reference or label it `unverified`. Do not create reconstructed review evidence.

- [ ] **Step 5: Verify this slice without touching unrelated failures**

Run:

```bash
python scripts/check_docs.py 2>&1 | tee /tmp/ecc-docs-after-governance.txt
rg -n '\.superpowers/sdd/' docs README.md
git diff --check
```

Expected: `rg` exits `1`; governance/status/evidence diagnostics are absent, while Phase 10/operations/setup failures may remain until Tasks 4-5.

- [ ] **Step 6: Commit the governance slice**

Stage only the files listed in this task and commit:

```bash
git commit -m "docs: reconcile governance and evidence lifecycle"
```

### Task 4: Complete the Phase 10 contract set

**Files:**
- Modify: `docs/phases/PHASE-010-gmail-connector.md`
- Create: `docs/phases/phase-010/DATA-MODEL.md`
- Create: `docs/phases/phase-010/API-SCHEMAS.md`
- Create: `docs/phases/phase-010/SYNC-CONTRACT.md`
- Create: `docs/phases/phase-010/PRIVACY-CONSENT-CONTRACT.md`
- Create: `docs/phases/phase-010/UX-STATES.md`
- Create: `docs/phases/phase-010/TEST-PLAN.md`

**Interfaces:**
- Consumes: current Tasks 1-2 behavior from `backend/ecc/domains/engineering/gmail_adapter.py`, Gmail OAuth/sync modules, migrations, tests, and `IMPLEMENTATION-STATUS.md`
- Produces: six files named in PHASE-010 `contracts` metadata

- [ ] **Step 1: Establish the missing-contract red signal**

Run: `python scripts/check_docs.py`

Expected diagnostics include each missing Phase 10 contract named after the PHASE-010 metadata is added, or the absence of required `contracts` metadata before it is added.

- [ ] **Step 2: Add contract metadata and current/planned labels**

Add all six filenames to PHASE-010. Every contract starts with governed metadata, `status: Approved for Implementation`, a semantic version, owner, dependency list, and a changelog. Every behavior table uses a `Current (Tasks 1-2)` or `Planned (Tasks 3-8)` label.

- [ ] **Step 3: Write the data/API/sync contracts from current code and approved design**

Document implemented OAuth, 30-day backfill, incremental Gmail history cursor, pagination, deduplication, entity linking, consent re-check, rate-limit/permission-loss behavior, encryption/provenance/retention, and current database indexes. Mark deterministic attention, recommendation creation, AI action detection, on-demand body reading, revocation cascade, and executive UX as planned when they are not present in Tasks 1-2.

- [ ] **Step 4: Write privacy/UX/test contracts**

Document scopes, allowlist, token encryption, body-access/AI boundaries, export/deletion/revocation, audit redaction, CASA/public rollout, every required disconnected/error/degraded/stale/deletion state, current automated tests, and the required real-account, backup/restore, privacy, adversarial, performance, accessibility, and recovery evidence.

- [ ] **Step 5: Validate contracts and links**

Run:

```bash
python scripts/check_docs.py
git diff --check -- docs/phases/PHASE-010-gmail-connector.md docs/phases/phase-010
```

Expected: no Phase 10 contract, metadata, or link diagnostic remains.

- [ ] **Step 6: Commit the Phase 10 contract slice**

```bash
git add docs/phases/PHASE-010-gmail-connector.md docs/phases/phase-010/DATA-MODEL.md docs/phases/phase-010/API-SCHEMAS.md docs/phases/phase-010/SYNC-CONTRACT.md docs/phases/phase-010/PRIVACY-CONSENT-CONTRACT.md docs/phases/phase-010/UX-STATES.md docs/phases/phase-010/TEST-PLAN.md
git commit -m "docs: add Phase 10 contracts"
```

### Task 5: Production readiness, recovery, KPI, setup, and navigation

**Files:**
- Create: `docs/operations/PRODUCTION-READINESS.md`
- Create: `docs/runbooks/PHASE-6-CONNECTOR-RECOVERY.md`
- Create: `docs/runbooks/PHASE-7-PERSONAL-DATA-RECOVERY.md`
- Create: `docs/runbooks/PHASE-8-IDENTITY-RECOVERY.md`
- Create: `docs/runbooks/PHASE-10-GMAIL-RECOVERY.md`
- Create: `docs/product/KPI-CONTRACT.md`
- Modify: `.env.example`
- Modify: `docs/SETUP.md`
- Modify: `docs/CONTRIBUTING.md`
- Modify: `docs/adr/README.md`
- Modify: `README.md`
- Modify: `docs/ROADMAP.md`

**Interfaces:**
- Consumes: current configuration aliases from `backend/ecc/config.py`, metrics from RFC-001, registry states, and current runtime behavior
- Produces: one blocker register, four containment/recovery runbooks, and one KPI contract

- [ ] **Step 1: Capture environment and navigation red signals**

Run:

```bash
python scripts/check_docs.py
comm -23 <(rg -o 'validation_alias="ECC_[A-Z0-9_]+"' backend/ecc/config.py | cut -d'"' -f2 | sort -u) <(sed -n 's/^\(ECC_[A-Z0-9_]*\)=.*/\1/p' .env.example | sort -u)
```

Expected: the missing embeddings, meeting-prep AI, personal-insight AI, and Gmail OAuth aliases are listed.

- [ ] **Step 2: Add the canonical production blocker register**

Create entries for owner provisioning; password reset/recovery and MFA/step-up; connector-token/personal-data key rotation; connector/Gmail revocation and sync recovery; personal-data export/deletion/backup/restore; automated post-deploy smoke checks; Phase 1/3/5 human validation; and Phase 6-10 promotion/review decisions. Each entry includes `status`, `affected capability`, `current safe behavior`, `required evidence`, `owner: Lucky Jain`, and `blocking scope`.

- [ ] **Step 3: Add four truthful recovery runbooks**

Document detection, immediate containment, safe current commands/endpoints, verification, escalation, and evidence capture. For unavailable key rotation, first-owner provisioning, password recovery, or MFA, use the exact unsupported-production-blocker label and link to the blocker register; do not invent operational procedures.

- [ ] **Step 4: Define the KPI measurement contract**

For every RFC-001 product metric, define decision, formula/duration, population/window, source and collection state, privacy class, target or `baseline_required`, owner, and review cadence. Use `not_collectable` rather than zero where no trustworthy telemetry exists; retain existing approved manual dogfood thresholds.

- [ ] **Step 5: Reconcile environment, setup, contribution, and navigation docs**

Add all runtime aliases to `.env.example` with safe empty/false defaults. Update SETUP capabilities and limitations through Phase 10 Task 2, ADR index entries 0011-0013, README/roadmap links, and contribution guidance. Keep the generated blocks untouched except by `docs_status.py render`.

- [ ] **Step 6: Validate the documentation set**

Run:

```bash
python scripts/docs_status.py render
python scripts/docs_status.py check
python scripts/check_docs.py
git diff --check
```

Expected: all four commands exit `0`.

- [ ] **Step 7: Commit the operational documentation slice**

Stage only Task 5 files and commit:

```bash
git commit -m "docs: add production readiness and KPI contracts"
```

### Task 6: Local/CI enforcement and complete regression proof

**Files:**
- Modify: `Makefile`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/pull_request_template.md`
- Modify: `docs/CONTRIBUTING.md`
- Modify: `docs/SETUP.md`
- Modify: any clean documentation file identified by the validator as containing a broken local link/anchor or invalid governed metadata

**Interfaces:**
- Consumes: `python scripts/docs_status.py check` and `python scripts/check_docs.py`
- Produces: `make docs-check` and a standard-library-only CI step in the backend job before dependency installation

- [ ] **Step 1: Establish the Make/CI red signal**

Run: `make docs-check`

Expected: Make exits non-zero with `No rule to make target 'docs-check'`.

- [ ] **Step 2: Add the local and CI enforcement hooks**

Add `docs-check` to `.PHONY` and implement:

```make
docs-check:
	python scripts/docs_status.py check
	python scripts/check_docs.py
```

In the backend CI job, immediately after checkout and before setup-uv/dependency installation, run `python scripts/docs_status.py check` and `python scripts/check_docs.py`. Add the same command to the pull-request checklist and setup/contribution guidance.

- [ ] **Step 3: Run the full documentation test and enforcement suite**

Run:

```bash
uv run pytest -q tests/test_docs_status.py tests/test_check_docs.py
uv run ruff check scripts/docs_status.py scripts/check_docs.py tests/test_docs_status.py tests/test_check_docs.py
uv run ruff format --check scripts/docs_status.py scripts/check_docs.py tests/test_docs_status.py tests/test_check_docs.py
uv run mypy scripts/docs_status.py scripts/check_docs.py
make docs-check
```

Expected: every command exits `0` with no failures or drift.

- [ ] **Step 4: Prove generated files are deterministic**

Run:

```bash
python scripts/docs_status.py render
git diff --exit-code -- README.md docs/ROADMAP.md docs/phases/README.md
```

Expected: the render creates no diff.

- [ ] **Step 5: Review scope and protected paths before the final commit**

Run:

```bash
git status --short
git diff --check
git diff --name-only 845586e..HEAD
```

Confirm neither protected dirty file was included in any task commit, and every changed path maps to this plan.

- [ ] **Step 6: Commit enforcement changes**

```bash
git add Makefile .github/workflows/ci.yml .github/pull_request_template.md docs/CONTRIBUTING.md docs/SETUP.md
git commit -m "ci: enforce documentation governance"
```

- [ ] **Step 7: Run fresh final verification from committed HEAD**

Run the full commands from Step 3 again, followed by:

```bash
git status --short
git log --oneline --decorate -7
```

Expected: all checks pass; status shows only the two protected unrelated modifications.

## Review and delivery checkpoint

After all six tasks, load `keystone:change-review` and review the complete diff from design commit `845586e` to HEAD for correctness, scope, governance consistency, and false operational claims. Remediate blocking findings with the same red/green/proof discipline. Do not push, open a pull request, or merge without a separate shipping authorization.
