import json

import pytest

from knowledge_distiller.primary import PrimaryRecovery
from knowledge_distiller.v1.knowledge_model import (
    AnthropicKnowledgeModel,
    KnowledgeModelError,
    parse_knowledge, source_segments,
)
from knowledge_distiller.v1.llm import LLMRequestError
from knowledge_distiller.v1.reviewer import build_reviewer


class Client:
    def __init__(self, result: str = "", error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = []

    def complete(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def knowledge_payload() -> str:
    return json.dumps(
        {
            "qualified": True,
            "rejection_reason": None,
            "title": "注意力需要边界",
            "subtitle": "说明边界如何保护有限注意力。",
            "summary": "主动设定边界可以减少注意力损耗。",
            "core_points": [
                {
                    "id": "p1",
                    "statement": "边界保护注意力。",
                    "argument": "持续切换会带来额外损耗。",
                    "evidence_ids": ["e1"],
                }
            ],
            "other_points": [],
            "evidence": [
                {"id": "e1", "occurrence": 1, "text": "持续切换"}
            ],
        },
        ensure_ascii=False,
    )


def test_knowledge_model_builds_exact_evidence_from_text_occurrence() -> None:
    snapshot = "持续切换很累；作者再次说持续切换会带来额外损耗。"
    client = Client(knowledge_payload())

    result = parse_knowledge(snapshot, knowledge_payload())

    assert result.title == "注意力需要边界"
    assert result.subtitle == "说明边界如何保护有限注意力。"
    assert result.evidence[0].text == "持续切换"
    assert result.evidence[0].start == snapshot.rindex("持续切换")


def test_knowledge_model_preserves_llm_failure_code() -> None:
    model = AnthropicKnowledgeModel(
        Client(error=LLMRequestError("llm_config_unavailable"))
    )

    with pytest.raises(KnowledgeModelError) as failure:
        model.derive("完整来源事实。")

    assert failure.value.args == ("llm_config_unavailable",)


def test_unqualified_source_never_becomes_knowledge() -> None:
    client = Client(
        json.dumps(
            {"qualified": False, "rejection_reason": "只有宣传，没有实质主张"},
            ensure_ascii=False,
        )
    )

    with pytest.raises(KnowledgeModelError) as failure:
        AnthropicKnowledgeModel(client).derive("点击链接立即购买。")

    assert failure.value.args == ("knowledge_not_qualified",)
    assert failure.value.rejection_reason == '只有宣传，没有实质主张'


def test_reviewer_reuses_shared_client_and_preserves_source() -> None:
    source = "持续切换会带来额外损耗。"
    client = Client(
        json.dumps(
            {"candidate_text": source, "issues": []}, ensure_ascii=False
        )
    )

    result = build_reviewer(client).review(PrimaryRecovery(source, "zh", ()))

    assert result.failure is None
    assert result.candidate.text == source
    assert client.calls[0]["max_tokens"] == 4096
    assert json.dumps(source, ensure_ascii=False) in client.calls[0]["user"]


def test_model_accepts_other_points_from_clear_text_and_receives_unknowns() -> None:
    snapshot = "[听辨不清]。持续切换会带来额外损耗。"
    payload = json.loads(knowledge_payload())
    payload["other_points"] = payload.pop("core_points")
    payload["core_points"] = []
    payload["evidence"] = [{"id":"e1", "start_segment":"s2", "end_segment":"s2"}]
    client = Client(json.dumps(payload, ensure_ascii=False))
    unknowns = ({"by": "human", "action": "unknown", "text": "[听辨不清]"},)

    result = AnthropicKnowledgeModel(client).derive(snapshot, unknowns)

    assert not result.core_points
    assert len(result.other_points) == 1
    assert result.evidence[0].text == "持续切换会带来额外损耗。"
    assert json.loads(client.calls[0]["user"]) == {
        "source_segments": [{"id": k, "text": snapshot[a:b]} for k, (a,b) in source_segments(snapshot).items()],
        "uncertainties": list(unknowns),
    }


def test_model_rejects_evidence_that_crosses_unknown_source() -> None:
    snapshot = "持续切换[听辨不清]会带来额外损耗。"
    payload = json.loads(knowledge_payload())
    payload["evidence"] = [{"id":"e1", "start_segment":"s1", "end_segment":"s1"}]
    model = AnthropicKnowledgeModel(Client(json.dumps(payload, ensure_ascii=False)))

    with pytest.raises(KnowledgeModelError) as failure:
        model.derive(snapshot)

    assert failure.value.args == ("knowledge_structure_invalid",)


def range_payload(first="s1", last="s1"):
    result = json.loads(knowledge_payload())
    result["evidence"] = [{"id":"e1", "start_segment":first, "end_segment":last}]
    return json.dumps(result, ensure_ascii=False)


def test_selected_range_restores_ocr_whitespace_and_duplicate_identity():
    snapshot = "同一句。\n\n  同一句。\n理由跨越\n两行。"
    client = Client(range_payload("s2", "s4"))
    result = AnthropicKnowledgeModel(client).derive(snapshot)
    evidence = result.evidence[0]
    assert evidence.start == snapshot.rindex("同一句")
    assert evidence.text == "同一句。\n理由跨越\n两行。"
    assert evidence.text == snapshot[evidence.start:evidence.end]
    assert client.calls[0]["max_tokens"] == 8192
    assert "source_fact" not in json.loads(client.calls[0]["user"])


@pytest.mark.parametrize("first,last", [("s9","s9"), ("s2","s1"), ([],"s1")])
def test_bad_source_range_is_rejected(first, last):
    with pytest.raises(KnowledgeModelError, match="knowledge_structure_invalid"):
        AnthropicKnowledgeModel(Client(range_payload(first,last))).derive("前句。后句。")


def test_new_calls_reject_model_supplied_quote_instead_of_fuzzy_matching():
    with pytest.raises(KnowledgeModelError, match="knowledge_structure_invalid"):
        AnthropicKnowledgeModel(Client(knowledge_payload())).derive("持续切换持续切换")


@pytest.mark.parametrize("snapshot", ["\n代码：\n    if x:\n        y()\n\n后续。", "一。\n\n二！三？", "abc\r\n  def\n"])
def test_input_segments_preserve_all_source_whitespace(snapshot):
    segments = source_segments(snapshot)
    assert ''.join(snapshot[start:end] for start,end in segments.values()) == snapshot
