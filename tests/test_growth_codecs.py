from __future__ import annotations

import copy
import json

import pytest

from knowledge_distiller.organization_models import (
    OrganizationCodecError,
    decode_insight_payload,
    decode_relation_payload,
    encode_growth_plan,
    encode_insight_payload,
    encode_relation_payload,
    parse_growth_plan,
    parse_insight_payload,
    parse_relation_payload,
)
from tests.fixtures.growth import (
    empty_growth_plan_payload,
    insight_payload,
    premise,
    relation_payload,
    source_participant,
)


def _full_plan() -> dict[str, object]:
    payload = empty_growth_plan_payload()
    payload["new_relations"] = [
        {
            "new_relation_key": "r-new",
            "target_kind": "create_identity",
            "payload": relation_payload(),
            "participants": [
                source_participant("a", 1, position=0),
                source_participant("b", 2, position=1),
            ],
            "used_relations": [
                {
                    "ref_kind": "boundary_current",
                    "relation_version_id": 70,
                    "role_text": "Existing connection",
                }
            ],
        }
    ]
    payload["candidate_versions"] = [
        {
            "new_insight_key": "i-new",
            "target_kind": "create_identity",
            "payload": insight_payload(),
            "participants": [
                source_participant("a", 1, position=0),
                source_participant("b", 2, position=1),
            ],
            "used_relations": [
                {
                    "ref_kind": "planned_stable",
                    "new_relation_key": "r-new",
                    "role_text": "Explains the connection",
                }
            ],
        }
    ]
    return payload


def test_versioned_persisted_payload_codecs_round_trip_canonically():
    relation = parse_relation_payload(relation_payload())
    insight = parse_insight_payload(insight_payload())

    assert decode_relation_payload(encode_relation_payload(relation)) == relation
    assert decode_insight_payload(encode_insight_payload(insight)) == insight
    assert json.loads(encode_relation_payload(relation))["codec"] == "relation-v1"
    assert json.loads(encode_insight_payload(insight))["codec"] == "insight-v1"


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda value: value.update(extra="x"), "unknown or missing"),
        (lambda value: value.__setitem__("codec", "relation-v2"), "unsupported"),
        (lambda value: value.__setitem__("relation_statement", "  "), "cannot be empty"),
        (
            lambda value: value["required_premises"][0].__setitem__("extra", True),
            "unknown or missing",
        ),
        (
            lambda value: value["required_premises"][0].__setitem__("supported_by", []),
            "cannot be empty",
        ),
    ],
)
def test_relation_codec_rejects_unknown_version_empty_and_invalid_premise(mutator, match):
    value = relation_payload()
    mutator(value)
    with pytest.raises(OrganizationCodecError, match=match):
        parse_relation_payload(value)


def test_relation_payload_cannot_hide_an_independent_candidate_claim():
    """E2E-16: an independent C cannot be smuggled into relation-v1."""
    value = relation_payload()
    value["independent_claim"] = "A separately reusable conclusion"

    with pytest.raises(OrganizationCodecError, match="unknown or missing"):
        parse_relation_payload(value)


def test_insight_codec_rejects_unknown_enum_and_reason_count():
    value = insight_payload()
    value["claim_kind"] = "fact"
    with pytest.raises(OrganizationCodecError, match="invalid claim kind"):
        parse_insight_payload(value)

    value = insight_payload()
    value["connection_reasons"] = ["one", "two", "three", "four"]
    with pytest.raises(OrganizationCodecError, match="1 to 3"):
        parse_insight_payload(value)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(unknown=True),
        lambda value: value["new_input_reviews"][0].__setitem__(
            "knowledge_result_id", True
        ),
        lambda value: value["new_relations"][0]["participants"][1].__setitem__(
            "position", 0
        ),
        lambda value: value["new_relations"].append(
            copy.deepcopy(value["new_relations"][0])
        ),
        lambda value: value["candidate_versions"][0]["used_relations"][0].__setitem__(
            "ref_kind", "unknown"
        ),
        lambda value: value["new_relations"][0]["used_relations"][0].update(
            new_relation_key="not-allowed"
        ),
    ],
)
def test_growth_plan_codec_rejects_unknown_bool_position_duplicate_key_and_ref_union(mutator):
    value = _full_plan()
    mutator(value)
    with pytest.raises(OrganizationCodecError):
        parse_growth_plan(value)


def test_growth_plan_codec_round_trip_preserves_exact_union_variants():
    value = _full_plan()
    value["candidate_versions"][0]["used_relations"] = [
        {
            "ref_kind": "boundary_current",
            "relation_version_id": 70,
            "role_text": "old",
        },
        {
            "ref_kind": "requalified_current",
            "relation_version_id": 80,
            "role_text": "restored",
        },
        {
            "ref_kind": "planned_stable",
            "new_relation_key": "r-new",
            "role_text": "new",
        },
    ]
    plan = parse_growth_plan(value)

    assert parse_growth_plan(encode_growth_plan(plan)) == plan


@pytest.mark.parametrize("fact_kind", ["basis_invalid", "refuted"])
def test_growth_plan_codec_accepts_exact_accepted_disqualification(fact_kind):
    value = _full_plan()
    value["accepted_disqualifications"] = [
        {
            "insight_version_id": 50,
            "fact_kind": fact_kind,
            "reason_text": "Exact accepted version was formally reviewed",
        }
    ]

    plan = parse_growth_plan(value)

    assert plan.accepted_disqualifications[0].fact_kind.value == fact_kind
    assert parse_growth_plan(encode_growth_plan(plan)) == plan


@pytest.mark.parametrize(
    "mutator",
    [
        lambda item: item.__setitem__("fact_kind", "unknown"),
        lambda item: item.__setitem__("insight_version_id", True),
        lambda item: item.__setitem__("reason_text", "  "),
        lambda item: item.__setitem__("unknown", "field"),
    ],
)
def test_accepted_disqualification_codec_rejects_invalid_exact_shape(mutator):
    value = _full_plan()
    action = {
        "insight_version_id": 50,
        "fact_kind": "basis_invalid",
        "reason_text": "Necessary basis no longer qualifies",
    }
    mutator(action)
    value["accepted_disqualifications"] = [action]

    with pytest.raises(OrganizationCodecError):
        parse_growth_plan(value)


def test_accepted_disqualification_codec_accepts_distinct_kinds_for_one_target():
    value = _full_plan()
    value["accepted_disqualifications"] = [
        {
            "insight_version_id": 50,
            "fact_kind": "basis_invalid",
            "reason_text": "First action",
        },
        {
            "insight_version_id": 50,
            "fact_kind": "refuted",
            "reason_text": "Second action",
        },
    ]

    plan = parse_growth_plan(value)

    assert [
        action.fact_kind.value for action in plan.accepted_disqualifications
    ] == ["basis_invalid", "refuted"]


def test_accepted_disqualification_codec_rejects_duplicate_target_and_kind():
    value = _full_plan()
    action = {
        "insight_version_id": 50,
        "fact_kind": "basis_invalid",
        "reason_text": "Repeated action",
    }
    value["accepted_disqualifications"] = [action, dict(action)]

    with pytest.raises(
        OrganizationCodecError,
        match="accepted disqualification target and kind",
    ):
        parse_growth_plan(value)


def test_relation_plan_cannot_depend_on_same_event_planned_relation():
    value = _full_plan()
    value["new_relations"][0]["used_relations"] = [
        {
            "ref_kind": "planned_stable",
            "new_relation_key": "r-new",
            "role_text": "cycle",
        }
    ]

    with pytest.raises(OrganizationCodecError, match="cannot use planned"):
        parse_growth_plan(value)
