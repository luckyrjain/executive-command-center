"""`TARGET_RESOURCE_TYPES` maps every recommendation `target_type` to the
authz resource type its target is authorized as. It is spelled out rather
than derived by pluralizing, so a new target type added to
`RecommendationCreate.target_type` without a mapping (or with one authz
does not know) fails here instead of raising `KeyError` or
`UnknownResourceTypeError` on a live request.
"""

from __future__ import annotations

from typing import get_args

from ecc.domains.governance.recommendation_models import RecommendationCreate
from ecc.domains.governance.recommendation_targets import TARGET_RESOURCE_TYPES
from ecc.platform import authz


def test_every_target_type_maps_to_a_known_authz_resource_type() -> None:
    target_types = set(get_args(RecommendationCreate.model_fields["target_type"].annotation))

    assert set(TARGET_RESOURCE_TYPES) == target_types
    for resource_type in TARGET_RESOURCE_TYPES.values():
        authz.require_known_resource_type(resource_type)
