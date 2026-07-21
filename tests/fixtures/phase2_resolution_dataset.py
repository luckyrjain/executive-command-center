"""Versioned labelled dataset for score_candidate benchmark evaluation.

DATASET_VERSION bumps whenever an entry is added, removed, or relabeled --
ENTITY-RESOLUTION-CONTRACT.md's "Quality metrics" section requires
precision/recall/false-merge-rate/unresolved-rate to be measured "on a
versioned labelled dataset" so a scoring change can be compared against a
known baseline rather than a silently-drifting fixture.

Each entry is a (left, right, is_match, description) tuple. `is_match`
records the ground-truth human judgement -- not what the scorer currently
outputs -- so this file must never be edited to make a failing test pass;
only a genuine labelling correction justifies a change here.
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

from ecc.domains.knowledge.resolution import CandidateEntity

DATASET_VERSION = "1.1.0"


@dataclass(frozen=True)
class LabelledPair:
    description: str
    left: CandidateEntity
    right: CandidateEntity
    is_match: bool


def _entity(
    kind: str,
    canonical_name: str,
    aliases: frozenset[str] = frozenset(),
    neighbor_ids: frozenset[UUID] = frozenset(),
) -> CandidateEntity:
    return CandidateEntity(
        id=uuid4(),
        kind=kind,
        canonical_name=canonical_name,
        aliases=aliases,
        neighbor_ids=neighbor_ids,
    )


def _shared_neighbors(count: int) -> frozenset[UUID]:
    return frozenset(uuid4() for _ in range(count))


def build_dataset() -> tuple[LabelledPair, ...]:
    shared_engine_team = _shared_neighbors(3)
    shared_ops_team = _shared_neighbors(3)

    return (
        # -- True matches: name similarity plus real corroborating signal --
        LabelledPair(
            "identical name, fully overlapping aliases",
            _entity("person", "Ada Lovelace", aliases=frozenset({"ada.l@example.test"})),
            _entity("person", "Ada Lovelace", aliases=frozenset({"ada.l@example.test"})),
            is_match=True,
        ),
        LabelledPair(
            "minor typo in name, overlapping alias and neighborhood",
            _entity(
                "person",
                "Ada Lovelace",
                aliases=frozenset({"countess-lovelace"}),
                neighbor_ids=shared_engine_team,
            ),
            _entity(
                "person",
                "Ada Lovelase",
                aliases=frozenset({"countess-lovelace"}),
                neighbor_ids=shared_engine_team,
            ),
            is_match=True,
        ),
        LabelledPair(
            "identical name, fully shared project neighborhood",
            _entity("person", "Grace Hopper", neighbor_ids=shared_engine_team),
            _entity("person", "Grace Hopper", neighbor_ids=shared_engine_team),
            is_match=True,
        ),
        LabelledPair(
            "abbreviated name, overlapping alias and neighborhood",
            _entity(
                "person",
                "Robert Chen",
                aliases=frozenset({"rchen@example.test"}),
                neighbor_ids=shared_ops_team,
            ),
            _entity(
                "person",
                "Rob Chen",
                aliases=frozenset({"rchen@example.test"}),
                neighbor_ids=shared_ops_team,
            ),
            is_match=True,
        ),
        LabelledPair(
            "organization name near-miss, shared alias and stakeholder overlap",
            _entity(
                "organization",
                "Analytical Engines Ltd",
                aliases=frozenset({"ael-uk"}),
                neighbor_ids=shared_ops_team,
            ),
            _entity(
                "organization",
                "Analytical Engines Ltd.",
                aliases=frozenset({"ael-uk"}),
                neighbor_ids=shared_ops_team,
            ),
            is_match=True,
        ),
        LabelledPair(
            "identical name, partial alias and full neighborhood overlap",
            _entity(
                "person",
                "Priya Natarajan",
                aliases=frozenset({"priya.n@example.test", "priyan"}),
                neighbor_ids=shared_engine_team,
            ),
            _entity(
                "person",
                "Priya Natarajan",
                aliases=frozenset({"priyan"}),
                neighbor_ids=shared_engine_team,
            ),
            is_match=True,
        ),
        # -- Hard negatives: strong name similarity, no real corroboration --
        LabelledPair(
            "common name collision, no shared aliases or neighbors",
            _entity("person", "John Smith", neighbor_ids=_shared_neighbors(2)),
            _entity("person", "John Smith", neighbor_ids=_shared_neighbors(2)),
            is_match=False,
        ),
        LabelledPair(
            "identical name, disjoint alias sets",
            _entity("person", "Maria Garcia", aliases=frozenset({"mgarcia.sales@example.test"})),
            _entity("person", "Maria Garcia", aliases=frozenset({"mgarcia.eng@example.test"})),
            is_match=False,
        ),
        LabelledPair(
            "near-identical name, disjoint neighborhoods",
            _entity("person", "David Lee", neighbor_ids=_shared_neighbors(2)),
            _entity("person", "David Leigh", neighbor_ids=_shared_neighbors(2)),
            is_match=False,
        ),
        LabelledPair(
            "same name string, incompatible entity kind",
            _entity("person", "Meridian"),
            _entity("project", "Meridian"),
            is_match=False,
        ),
        LabelledPair(
            "unrelated people, dissimilar names",
            _entity("person", "Ada Lovelace"),
            _entity("person", "Grace Hopper"),
            is_match=False,
        ),
        LabelledPair(
            "shared surname only, no other overlap",
            _entity("person", "Alan Turing"),
            _entity("person", "Miles Turing"),
            is_match=False,
        ),
    )
