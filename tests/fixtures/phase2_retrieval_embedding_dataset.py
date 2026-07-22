"""Versioned labelled dataset for hybrid-retrieval semantic-recall
evaluation, run against the real sentence-transformers model.

DATASET_VERSION bumps whenever a query, document, or relevance judgement is
added, removed, or relabeled -- RETRIEVAL-CONTRACT.md's "Evaluation" section
requires a versioned benchmark with relevance judgements so a ranking-formula
change can be compared against a known baseline (before/after benchmark
results), matching the same discipline phase2_resolution_dataset.py already
established for entity resolution.

Every document below is deliberately worded so a query that means the same
thing shares few or no literal words with it -- this dataset exists
specifically to measure semantic recall lexical-only search cannot achieve
(see test_knowledge_retrieval_benchmark_postgres.py), not to re-test lexical
matching, which test_knowledge_retrieval_postgres.py already covers.
"""

from dataclasses import dataclass

DATASET_VERSION = "1.0.0"


@dataclass(frozen=True)
class LabelledDocument:
    kind: str
    canonical_name: str
    summary: str


@dataclass(frozen=True)
class LabelledQuery:
    description: str
    query: str
    # Index into DOCUMENTS (build_dataset()[1]) this query should retrieve.
    relevant_document_index: int


def build_dataset() -> tuple[tuple[LabelledDocument, ...], tuple[LabelledQuery, ...]]:
    documents = (
        LabelledDocument(
            "person", "Priya Natarajan", "Sets company direction and reports to the board"
        ),
        LabelledDocument("person", "Robert Chen", "Writes and reviews backend service code daily"),
        LabelledDocument(
            "project",
            "Meridian Platform Migration",
            "Moving customer workloads to the new data center",
        ),
        LabelledDocument(
            "decision", "Freeze non-critical spend for Q3", "Board-approved cost control measure"
        ),
        LabelledDocument(
            "topic",
            "Vendor concentration risk",
            "Over-reliance on a single cloud infrastructure supplier",
        ),
        LabelledDocument(
            "person", "Grace Hopper", "Enjoys long-distance cycling and trail running on weekends"
        ),
        LabelledDocument(
            "project", "Lighthouse Mobile App", "Rebuilding the customer-facing mobile experience"
        ),
        LabelledDocument(
            "decision",
            "Approve the annual security audit budget",
            "Funds an external penetration test",
        ),
    )
    queries = (
        LabelledQuery(
            "chief executive, worded around leadership not the title", "who runs the company", 0
        ),
        LabelledQuery(
            "software engineer, worded around the activity not the title",
            "person who writes code",
            1,
        ),
        LabelledQuery(
            "data center migration, worded around the goal",
            "moving infrastructure to a new facility",
            2,
        ),
        LabelledQuery(
            "spending freeze, worded around the outcome", "cutting costs this quarter", 3
        ),
        LabelledQuery(
            "single-vendor risk, worded around the concern",
            "depending on one cloud provider too much",
            4,
        ),
        LabelledQuery(
            "mobile app rebuild, worded around the deliverable",
            "new version of the customer app",
            6,
        ),
        LabelledQuery(
            "security audit funding, worded around the purpose", "budget for an external pentest", 7
        ),
    )
    return documents, queries
