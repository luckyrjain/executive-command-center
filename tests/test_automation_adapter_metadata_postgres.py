"""Adapter scope metadata (scope-enforcement design, Decisions 1-2): the
registry refuses an adapter that cannot be named in a policy scope, and the
six production adapters declare the accepted classification.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from automation_scope_support import FakeAdapter, FinancialFakeAdapter

from ecc.domains.ai_runtime.runtime import DataClass
from ecc.domains.automation.adapter_contract import (
    ACTION_TYPES,
    DATA_CLASSES,
    AdapterActionTypeInvalid,
    AdapterDataClassInvalid,
    AdapterRegistry,
    AdapterValueUndeclared,
)
from ecc.domains.automation.adapters import registry as production_registry


def test_data_classes_match_phase_4_vocabulary_in_order() -> None:
    assert DATA_CLASSES == get_args(DataClass)


@pytest.mark.parametrize("action_type", ["", "local.create_note", "comment.delete"])
def test_registry_rejects_unknown_action_type(action_type: str) -> None:
    with pytest.raises(AdapterActionTypeInvalid):
        AdapterRegistry().register(FakeAdapter("test.bad", action_type=action_type))


@pytest.mark.parametrize("data_class", ["", "secret", "Internal"])
def test_registry_rejects_unknown_data_class(data_class: str) -> None:
    with pytest.raises(AdapterDataClassInvalid):
        AdapterRegistry().register(FakeAdapter("test.bad", data_class=data_class))


def test_registry_rejects_a_set_valued_data_class() -> None:
    adapter: Any = FakeAdapter("test.bad")
    adapter.data_class = frozenset({"internal"})
    with pytest.raises(AdapterDataClassInvalid):
        AdapterRegistry().register(adapter)


def test_registry_rejects_missing_scope_members() -> None:
    adapter: Any = FakeAdapter("test.bad")
    del adapter.action_type
    with pytest.raises(TypeError):
        AdapterRegistry().register(adapter)


def test_registry_rejects_financial_adapter_without_dispatch_value() -> None:
    with pytest.raises(AdapterValueUndeclared):
        AdapterRegistry().register(FakeAdapter("test.money", categories=frozenset({"financial"})))
    AdapterRegistry().register(FinancialFakeAdapter("test.money"))


def test_production_adapters_declare_the_accepted_classification() -> None:
    declared = {
        adapter_id: (adapter.action_type, adapter.data_class)
        for adapter_id in production_registry.adapter_ids()
        if (adapter := production_registry.get(adapter_id)) is not None
    }
    assert declared == {
        "local.create_note": ("note.create", "sensitive"),
        "local.send_test_notification": ("notification.send", "sensitive"),
        "fake.external_action": ("fake.external", "internal"),
        "github.add_issue_comment": ("comment.create", "sensitive"),
        "gitlab.add_note": ("comment.create", "sensitive"),
        "jira.add_comment": ("comment.create", "sensitive"),
    }
    assert {action_type for action_type, _ in declared.values()} == ACTION_TYPES
