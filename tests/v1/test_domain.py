from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_distiller.v1.domain import (
    CapturedMaterial,
    Evidence,
    Knowledge,
    Point,
    knowledge_from_dict,
    knowledge_to_dict,
    validate_knowledge,
)


def example_knowledge() -> Knowledge:
    return Knowledge(
        title="注意力需要边界",
        subtitle="说明边界如何保护有限注意力。",
        summary="作者认为主动设定边界能减少注意力损耗。",
        core_points=(
            Point("p1", "边界保护注意力。", "持续切换会带来额外损耗。", ("e1",)),
        ),
        other_points=(),
        evidence=(Evidence("e1", 0, 7, "持续切换会带来"),),
    )


def test_captured_material_requires_a_verified_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="verified media"):
        CapturedMaterial(
            "douyin",
            "123",
            "https://v.douyin.com/a/",
            "https://www.douyin.com/video/123",
            {},
            tmp_path / "missing.mp4",
            1.0,
        )


def test_knowledge_round_trip_keeps_exact_evidence() -> None:
    snapshot = "持续切换会带来额外损耗。"
    knowledge = example_knowledge()

    validate_knowledge(snapshot, knowledge)

    assert knowledge_from_dict(snapshot, knowledge_to_dict(knowledge)) == knowledge


def test_knowledge_rejects_evidence_that_does_not_match_source() -> None:
    with pytest.raises(ValueError, match="does not match"):
        validate_knowledge("另一段来源。", example_knowledge())


def test_knowledge_keeps_subtitle_distinct_from_expanded_summary() -> None:
    knowledge = example_knowledge()
    invalid = Knowledge(
        knowledge.title,
        knowledge.summary,
        knowledge.summary,
        knowledge.core_points,
        knowledge.other_points,
        knowledge.evidence,
    )

    with pytest.raises(ValueError, match="structure is incomplete"):
        validate_knowledge("持续切换会带来额外损耗。", invalid)


def test_knowledge_allows_only_other_points_and_round_trips() -> None:
    snapshot = "持续切换会带来额外损耗。"
    original = example_knowledge()
    knowledge = replace(original, core_points=(), other_points=original.core_points)

    assert knowledge_from_dict(snapshot, knowledge_to_dict(knowledge)) == knowledge


def test_knowledge_still_requires_at_least_one_point() -> None:
    knowledge = replace(example_knowledge(), core_points=(), other_points=())

    with pytest.raises(ValueError, match="structure is incomplete"):
        validate_knowledge("持续切换会带来额外损耗。", knowledge)


@pytest.mark.parametrize("quote", ["[听辨不清]", "要求[听辨不清]之后", "听辨", "不清]之后"])
def test_knowledge_cannot_use_unknown_text_as_evidence(quote: str) -> None:
    snapshot = "要求[听辨不清]之后再处理。持续切换会带来额外损耗。"
    start = snapshot.index(quote)
    knowledge = replace(
        example_knowledge(),
        evidence=(Evidence("e1", start, start + len(quote), quote),),
    )

    with pytest.raises(ValueError, match="unrecognized source"):
        validate_knowledge(snapshot, knowledge)


def test_knowledge_can_use_clear_evidence_when_other_source_text_is_unknown() -> None:
    snapshot = "持续切换会带来额外损耗。[听辨不清]"

    validate_knowledge(snapshot, example_knowledge())
