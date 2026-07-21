from datetime import UTC, datetime
from uuid import uuid4

from ecc.domains.knowledge.resolution import RESOLVER_VERSION, CandidateEntity, score_candidate


def _entity(**overrides: object) -> CandidateEntity:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "kind": "person",
        "canonical_name": "Ada Lovelace",
        "aliases": frozenset(),
        "neighbor_ids": frozenset(),
        "active_from": None,
        "active_to": None,
    }
    defaults.update(overrides)
    return CandidateEntity(**defaults)  # type: ignore[arg-type]


def test_identical_names_score_maximum_name_similarity() -> None:
    left = _entity(canonical_name="Ada Lovelace")
    right = _entity(canonical_name="Ada Lovelace")
    result = score_candidate(left, right)
    assert result.factors.name_similarity == 1.0


def test_dissimilar_names_score_low_name_similarity() -> None:
    left = _entity(canonical_name="Ada Lovelace")
    right = _entity(canonical_name="Grace Hopper")
    result = score_candidate(left, right)
    assert result.factors.name_similarity < 0.3


def test_near_miss_typo_scores_high_but_not_maximum_name_similarity() -> None:
    left = _entity(canonical_name="Ada Lovelace")
    right = _entity(canonical_name="Ada Lovelase")
    result = score_candidate(left, right)
    assert 0.5 < result.factors.name_similarity < 1.0


def test_full_alias_overlap_scores_maximum() -> None:
    left = _entity(aliases=frozenset({"ada.lovelace@example.test", "countess of lovelace"}))
    right = _entity(aliases=frozenset({"ADA.LOVELACE@example.test", "Countess of Lovelace"}))
    result = score_candidate(left, right)
    assert result.factors.alias_overlap == 1.0


def test_no_alias_overlap_scores_zero() -> None:
    left = _entity(aliases=frozenset({"ada.lovelace@example.test"}))
    right = _entity(aliases=frozenset({"grace.hopper@example.test"}))
    result = score_candidate(left, right)
    assert result.factors.alias_overlap == 0.0


def test_partial_alias_overlap_scores_between_zero_and_one() -> None:
    left = _entity(aliases=frozenset({"a", "b", "c"}))
    right = _entity(aliases=frozenset({"b", "c", "d"}))
    result = score_candidate(left, right)
    assert 0.0 < result.factors.alias_overlap < 1.0


def test_shared_neighbors_increase_neighbor_overlap() -> None:
    shared = uuid4()
    left = _entity(neighbor_ids=frozenset({shared, uuid4()}))
    right = _entity(neighbor_ids=frozenset({shared, uuid4()}))
    result = score_candidate(left, right)
    assert result.factors.neighbor_overlap > 0.0


def test_no_shared_neighbors_scores_zero_neighbor_overlap() -> None:
    left = _entity(neighbor_ids=frozenset({uuid4()}))
    right = _entity(neighbor_ids=frozenset({uuid4()}))
    result = score_candidate(left, right)
    assert result.factors.neighbor_overlap == 0.0


def test_overlapping_active_intervals_are_temporally_compatible() -> None:
    left = _entity(
        active_from=datetime(2026, 1, 1, tzinfo=UTC), active_to=datetime(2026, 6, 1, tzinfo=UTC)
    )
    right = _entity(
        active_from=datetime(2026, 3, 1, tzinfo=UTC), active_to=datetime(2026, 9, 1, tzinfo=UTC)
    )
    result = score_candidate(left, right)
    assert result.factors.temporal_compatibility == 1.0


def test_disjoint_active_intervals_are_temporally_incompatible() -> None:
    left = _entity(
        active_from=datetime(2026, 1, 1, tzinfo=UTC), active_to=datetime(2026, 2, 1, tzinfo=UTC)
    )
    right = _entity(
        active_from=datetime(2026, 6, 1, tzinfo=UTC), active_to=datetime(2026, 7, 1, tzinfo=UTC)
    )
    result = score_candidate(left, right)
    assert result.factors.temporal_compatibility == 0.0


def test_open_ended_intervals_default_to_compatible() -> None:
    left = _entity()
    right = _entity()
    result = score_candidate(left, right)
    assert result.factors.temporal_compatibility == 1.0


def test_mismatched_kind_forces_zero_score_regardless_of_other_factors() -> None:
    left = _entity(kind="person", canonical_name="Ada Lovelace")
    right = _entity(kind="project", canonical_name="Ada Lovelace")
    result = score_candidate(left, right)
    assert result.score == 0.0


def test_score_result_reports_resolver_version() -> None:
    result = score_candidate(_entity(), _entity())
    assert result.resolver_version == RESOLVER_VERSION


def test_score_is_bounded_between_zero_and_one() -> None:
    left = _entity(
        canonical_name="Ada Lovelace",
        aliases=frozenset({"ada"}),
        neighbor_ids=frozenset({uuid4()}),
    )
    right = _entity(
        canonical_name="Ada Lovelace",
        aliases=frozenset({"ada"}),
        neighbor_ids=left.neighbor_ids,
    )
    result = score_candidate(left, right)
    assert 0.0 <= result.score <= 1.0
