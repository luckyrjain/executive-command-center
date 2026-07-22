from fixtures.phase2_resolution_dataset import DATASET_VERSION, build_dataset

from ecc.domains.knowledge.resolution import DEFAULT_THRESHOLDS, score_candidate

# Benchmark thresholds from ENTITY-RESOLUTION-CONTRACT.md's "Quality
# metrics": false merges are the highest-severity failure and block
# release, so zero tolerance is enforced on this dataset; precision and
# recall get looser (but still high) floors since fuzzy candidates only
# ever *propose* review, they never auto-confirm.
MIN_PRECISION = 0.9
MIN_RECALL = 0.7
MAX_FALSE_MERGE_RATE = 0.0
MAX_UNRESOLVED_RATE = 0.35
# Reviewer override rate: the fraction of the dataset where a human's
# ground-truth decision would have to go against what the score alone
# suggests (a false merge a reviewer must reject, or a real match the score
# left below the auto-suggest threshold that a reviewer must confirm
# anyway) -- ENTITY-RESOLUTION-CONTRACT.md's fifth named quality metric,
# alongside precision/recall/false-merge-rate/unresolved-rate. It composes
# the other two failure-mode rates into one number answering "how often
# does a reviewer have to overrule the algorithm," so it's bounded by their
# sum -- with MAX_FALSE_MERGE_RATE floored at 0.0, it equals
# MAX_UNRESOLVED_RATE today, but the two ceilings are independent and would
# no longer coincide if a future dataset ever tolerated nonzero false merges.
MAX_REVIEWER_OVERRIDE_RATE = 0.35


def test_dataset_version_is_pinned() -> None:
    assert DATASET_VERSION == "1.1.0"


def test_scorer_meets_quality_thresholds_on_labelled_dataset() -> None:
    dataset = build_dataset()
    assert len(dataset) >= 10

    true_positives = 0
    false_positives = 0
    false_negatives = 0
    true_negatives = 0

    for pair in dataset:
        result = score_candidate(pair.left, pair.right)
        predicted_match = result.score >= DEFAULT_THRESHOLDS.high_confidence
        if predicted_match and pair.is_match:
            true_positives += 1
        elif predicted_match and not pair.is_match:
            false_positives += 1
        elif not predicted_match and pair.is_match:
            false_negatives += 1
        else:
            true_negatives += 1

    total_negatives = false_positives + true_negatives
    total_positives = true_positives + false_negatives

    precision = (
        true_positives / (true_positives + false_positives)
        if (true_positives + false_positives)
        else 1.0
    )
    recall = true_positives / total_positives if total_positives else 1.0
    false_merge_rate = false_positives / total_negatives if total_negatives else 0.0
    unresolved_rate = false_negatives / total_positives if total_positives else 0.0
    reviewer_override_rate = (false_positives + false_negatives) / len(dataset)

    assert false_merge_rate <= MAX_FALSE_MERGE_RATE, (
        f"false merges detected: {false_positives} negative pairs scored as high-confidence "
        f"matches (false_merge_rate={false_merge_rate})"
    )
    assert precision >= MIN_PRECISION, f"precision={precision} below floor {MIN_PRECISION}"
    assert recall >= MIN_RECALL, f"recall={recall} below floor {MIN_RECALL}"
    assert unresolved_rate <= MAX_UNRESOLVED_RATE, (
        f"unresolved_rate={unresolved_rate} above ceiling {MAX_UNRESOLVED_RATE}"
    )
    assert reviewer_override_rate <= MAX_REVIEWER_OVERRIDE_RATE, (
        f"reviewer_override_rate={reviewer_override_rate} above ceiling "
        f"{MAX_REVIEWER_OVERRIDE_RATE} ({false_positives} false merges + {false_negatives} "
        f"missed matches out of {len(dataset)} pairs)"
    )
