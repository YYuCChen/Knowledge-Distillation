import json
from dataclasses import asdict

import httpx
import pytest

from knowledge_distiller.faithful_review import (
    AnthropicCompatibleReviewRuntime,
    FaithfulReviewAdapter,
    ReviewFailure,
    ReviewRuntimeFailure,
    ReviewRuntimeResult,
    ReviewRuntimeUnavailable,
)
from knowledge_distiller.primary import PrimaryChunk, PrimaryRecovery


PRIMARY_TEXT = (
    "药品集中采购先由医疗机构报告需求量，然后由企业按规则报价。"
    "作者接着说明，中选不等于所有医院立刻只使用一种药，还要看约定采购量和临床需要。"
)


class ReviewBinding:
    def __init__(self, payload=None, *, stop_reason="end_turn", error=None):
        self.payload = payload
        self.stop_reason = stop_reason
        self.error = error
        self.calls = []

    def complete(self, primary_text):
        self.calls.append(primary_text)
        if self.error is not None:
            raise self.error
        return ReviewRuntimeResult(
            json.dumps(self.payload, ensure_ascii=False)
            if not isinstance(self.payload, str)
            else self.payload,
            self.stop_reason,
        )


def recovery(text=PRIMARY_TEXT, *, completed_normally=True, truncated=False):
    return PrimaryRecovery(
        text=text,
        language="Chinese",
        chunks=(PrimaryChunk(text, 0.0, 20.0, "Chinese"),),
        completed_normally=completed_normally,
        truncated=truncated,
    )


def candidate_payload(text=PRIMARY_TEXT, concerns=None):
    return {
        "candidate_text": text,
        "issues": concerns or [],
    }


def test_complete_faithful_review_is_translated_to_project_result():
    binding = ReviewBinding(candidate_payload())

    result = FaithfulReviewAdapter(binding).review(recovery())

    assert result.failure is None
    assert asdict(result.candidate) == {
        "text": PRIMARY_TEXT,
        "concerns": (),
        "repairs": (),
    }
    assert binding.calls == [PRIMARY_TEXT]
    assert "ReviewRuntimeResult" not in repr(result.candidate)


def test_explicit_concern_is_preserved_with_candidate_span():
    text = "作者说约定采购量是三年，随后解释临床仍有选择空间。"
    start = text.index("三年")
    binding = ReviewBinding(
        candidate_payload(
            text,
            [
                {
                    "issue_text": "三年",
                    "occurrence": 0,
                    "reason": "这里也可能是时间点，单凭全文无法安全确定。",
                    "meaning_may_change": True,
                    "candidate_readings": ["三年", "三点"],
                }
            ],
        )
    )

    result = FaithfulReviewAdapter(binding).review(recovery(text))

    concern = result.candidate.concerns[0]
    assert text[concern.start_offset : concern.end_offset] == concern.text
    assert concern.meaning_may_change is True
    assert concern.candidate_readings == ("三年", "三点")


def test_repeated_issue_text_uses_explicit_occurrence_to_generate_span():
    issue_text = "三年"
    text = "作者先说三年，随后再次说三年，但第二处可能是三点。"
    second_start = text.rindex(issue_text)
    binding = ReviewBinding(
        candidate_payload(
            text,
            [
                {
                    "issue_text": issue_text,
                    "occurrence": 1,
                    "reason": "第二处听法会改变时间含义。",
                    "meaning_may_change": True,
                    "candidate_readings": ["三年", "三点"],
                }
            ],
        )
    )

    result = FaithfulReviewAdapter(binding).review(recovery(text))

    concern = result.candidate.concerns[0]
    assert concern.start_offset == second_start
    assert concern.end_offset == second_start + len(issue_text)
    assert text[concern.start_offset : concern.end_offset] == issue_text


def test_concerns_are_sorted_after_project_generates_spans():
    text = "第一处可能有误，第二处也可能有误。"
    binding = ReviewBinding(
        candidate_payload(
            text,
            [
                {
                    "issue_text": "第二处",
                    "occurrence": 0,
                    "reason": "第二处需要回听。",
                    "meaning_may_change": False,
                    "candidate_readings": [],
                },
                {
                    "issue_text": "第一处",
                    "occurrence": 0,
                    "reason": "第一处需要回听。",
                    "meaning_may_change": False,
                    "candidate_readings": [],
                },
            ],
        )
    )

    result = FaithfulReviewAdapter(binding).review(recovery(text))

    assert [concern.text for concern in result.candidate.concerns] == [
        "第一处",
        "第二处",
    ]


@pytest.mark.parametrize(
    "error,expected",
    [
        (ReviewRuntimeUnavailable(), ReviewFailure.RUNTIME_UNAVAILABLE),
        (ReviewRuntimeFailure(), ReviewFailure.RUNTIME_FAILED),
    ],
)
def test_runtime_failures_are_translated(error, expected):
    result = FaithfulReviewAdapter(ReviewBinding(error=error)).review(recovery())

    assert result.failure is expected
    assert result.candidate is None


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        {},
        {"candidate_text": "", "issues": []},
        {"candidate_text": PRIMARY_TEXT},
        {"candidate_text": PRIMARY_TEXT, "issues": {}},
        {
            "candidate_text": PRIMARY_TEXT,
            "issues": [
                {
                    "issue_text": "不存在的疑点",
                    "occurrence": 0,
                    "reason": "无法确认",
                    "meaning_may_change": True,
                    "candidate_readings": [],
                }
            ],
        },
        {
            "candidate_text": PRIMARY_TEXT,
            "issues": [
                {
                    "issue_text": " ",
                    "occurrence": 0,
                    "reason": "无法确认",
                    "meaning_may_change": False,
                    "candidate_readings": [],
                }
            ],
        },
        {
            "candidate_text": PRIMARY_TEXT,
            "issues": [
                {
                    "issue_text": "药品",
                    "occurrence": -1,
                    "reason": "无法确认",
                    "meaning_may_change": False,
                    "candidate_readings": [],
                }
            ],
        },
        {
            "candidate_text": PRIMARY_TEXT,
            "issues": [
                {
                    "issue_text": "药品",
                    "occurrence": True,
                    "reason": "无法确认",
                    "meaning_may_change": False,
                    "candidate_readings": [],
                }
            ],
        },
        {
            "candidate_text": PRIMARY_TEXT,
            "issues": [
                {
                    "issue_text": "药品",
                    "occurrence": 1,
                    "reason": "无法确认",
                    "meaning_may_change": False,
                    "candidate_readings": [],
                }
            ],
        },
        {
            "candidate_text": PRIMARY_TEXT,
            "issues": [
                {
                    "start": 0,
                    "end": 0,
                    "text": "",
                    "reason": "占位结构不应被接受",
                    "meaning_may_change": False,
                    "candidate_readings": [],
                }
            ],
        },
    ],
)
def test_empty_or_structurally_invalid_output_is_rejected(payload):
    result = FaithfulReviewAdapter(ReviewBinding(payload)).review(recovery())

    assert result.failure is ReviewFailure.INVALID_OUTPUT


def test_overlapping_generated_concerns_are_rejected():
    text = "药品集中采购需要报告需求量。"
    binding = ReviewBinding(
        candidate_payload(
            text,
            [
                {
                    "issue_text": "集中采购",
                    "occurrence": 0,
                    "reason": "完整词组需要回听。",
                    "meaning_may_change": True,
                    "candidate_readings": [],
                },
                {
                    "issue_text": "采购",
                    "occurrence": 0,
                    "reason": "子词组也被重复标为疑点。",
                    "meaning_may_change": True,
                    "candidate_readings": [],
                },
            ],
        )
    )

    result = FaithfulReviewAdapter(binding).review(recovery(text))

    assert result.failure is ReviewFailure.INVALID_OUTPUT


def test_non_normal_model_ending_is_rejected():
    result = FaithfulReviewAdapter(
        ReviewBinding(candidate_payload(), stop_reason="max_tokens")
    ).review(recovery())

    assert result.failure is ReviewFailure.INCOMPLETE


@pytest.mark.parametrize(
    "candidate_text",
    [
        "作者介绍了药品集采。",
        "这是一篇完全重新撰写、没有保留原句结构但长度被人为填充的文章。" * 3,
    ],
)
def test_summary_or_major_rewrite_is_rejected(candidate_text):
    result = FaithfulReviewAdapter(
        ReviewBinding(candidate_payload(candidate_text))
    ).review(recovery())

    assert result.failure is ReviewFailure.INSUFFICIENT_COVERAGE


def test_incomplete_primary_cannot_enter_review():
    binding = ReviewBinding(candidate_payload())

    result = FaithfulReviewAdapter(binding).review(recovery(truncated=True))

    assert result.failure is ReviewFailure.INPUT_INVALID
    assert binding.calls == []


def test_runtime_explicitly_disables_extra_provider_thinking(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(candidate_payload(), ensure_ascii=False),
                    }
                ],
                "stop_reason": "end_turn",
            }

    def fake_post(url, *, headers, json, timeout):
        captured.update(json)
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)

    result = AnthropicCompatibleReviewRuntime(
        "https://provider.example",
        "review-model",
        "private-test-key",
    ).complete(PRIMARY_TEXT)

    assert captured["max_tokens"] == 4096
    assert captured["thinking"] == {"type": "disabled"}
    assert '"issues":[]' in captured["system"]
    assert '"issue_text"' in captured["system"]
    assert '"occurrence"' in captured["system"]
    assert '"start"' not in captured["system"]
    assert result.stop_reason == "end_turn"
