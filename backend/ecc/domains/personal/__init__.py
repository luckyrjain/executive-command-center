"""Phase 7 Personal Intelligence domain package (Task 1: domain/consent/
vault framework and the `habits` reference domain).

`docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`
/ `docs/phases/phase-007/DATA-MODEL.md`. This package owns `personal_
domains`/`domain_consents`/`domain_sources`/`domain_records`/`goals`/
`routines`/`check_ins`/`deletion_jobs`/`personal_insights` (`domains.py`
for the generic domain/consent/vault framework, `habits.py` for the one
reference domain's routines/check-ins/deterministic insights,
`export_deletion.py` for export and whole-domain deletion) -- the tables
migration `0054_phase7_personal_domains.py` creates.

`learning`/`travel`/`relationships`/`health`/`finance`, `cross_domain_
grants`, and every AI-generated insight kind do not exist in this package
yet -- each is a later task per `docs/superpowers/plans/2026-07-31-
phase-7-personal-intelligence.md`. `ecc.domains.personal.crypto` (field-
level encryption for `sensitive`/`high_stakes` narrative content) is
deferred to the task that first needs it (`relationships`) -- `habits` is
`standard`-classified and has no encrypted field to exercise; see
`domains.py`'s own module docstring for why building it now with no real
caller would be premature.
"""
