from knowledge_distiller.faithful_review import (
    FaithfulReviewCandidate,
    ReviewConcern,
)
from knowledge_distiller.primary import PrimaryChunk, PrimaryRecovery
from knowledge_distiller.legacy.snapshot_acceptance import (
    SnapshotAcceptanceKind,
    accept_snapshot_candidate,
    make_snapshot_candidate_readable,
)


TEXT = (
    "作者先介绍药品集中采购的基本流程，然后解释医疗机构如何上报采购量。"
    "随后作者说明中选企业的供应责任、医院的采购要求，以及奖励资金的使用范围。"
)


def recovery(text=TEXT, *, completed_normally=True, truncated=False):
    return PrimaryRecovery(
        text=text,
        language="Chinese",
        chunks=(PrimaryChunk(text, 0.0, 20.0, "Chinese"),),
        completed_normally=completed_normally,
        truncated=truncated,
    )


def recovery_with_chunk_boundaries(boundaries):
    return PrimaryRecovery(
        text=TEXT,
        language="Chinese",
        chunks=tuple(
            PrimaryChunk(TEXT, start, end, "Chinese")
            for start, end in boundaries
        ),
    )


def concern(text, phrase, *, meaning_may_change):
    start = text.index(phrase)
    return ReviewConcern(
        start,
        start + len(phrase),
        phrase,
        "这个局部字面仅凭当前材料无法安全唯一确定。",
        meaning_may_change,
        (phrase,),
    )


def long_candidate_text(sentence_count=45):
    return "".join(
        f"第{index}部分说明品牌、市场与销售之间各自承担的工作边界和衔接关系。"
        for index in range(sentence_count)
    )


def restore_inserted_breaks(original, readable):
    restored: list[str] = []
    original_index = 0
    readable_index = 0
    inserted_positions: list[int] = []
    while original_index < len(original):
        if readable[readable_index] == original[original_index]:
            restored.append(readable[readable_index])
            original_index += 1
            readable_index += 1
            continue
        assert readable[readable_index : readable_index + 2] == "\n\n"
        inserted_positions.append(readable_index)
        readable_index += 2
    assert readable_index == len(readable)
    return "".join(restored), tuple(inserted_positions)


def test_candidate_without_concerns_passes():
    result = accept_snapshot_candidate(
        recovery(),
        FaithfulReviewCandidate(TEXT, ()),
    )

    assert result.kind is SnapshotAcceptanceKind.PASSED
    assert result.snapshot.text == TEXT
    assert result.snapshot.uncertainties == ()


def test_long_single_paragraph_is_split_without_changing_original_characters():
    text = long_candidate_text()

    readable = make_snapshot_candidate_readable(FaithfulReviewCandidate(text, ()))

    assert readable is not None
    assert readable.text.count("\n\n") >= 2
    restored, inserted_positions = restore_inserted_breaks(text, readable.text)
    assert restored == text
    assert inserted_positions
    assert all(readable.text[position - 1] in "。！？!?" for position in inserted_positions)


def test_existing_paragraphs_and_short_text_are_unchanged():
    short = FaithfulReviewCandidate(TEXT, ())
    paragraphs = FaithfulReviewCandidate(
        long_candidate_text(10) + "\n\n" + long_candidate_text(10),
        (),
    )

    assert make_snapshot_candidate_readable(short) is short
    assert make_snapshot_candidate_readable(paragraphs) is paragraphs


def test_concerns_are_not_split_and_offsets_shift_after_each_insertion():
    text = long_candidate_text(48)
    before_phrase = "第2部分"
    across_boundary_phrase = "衔接关系。第10部分"
    after_phrase = "第46部分"
    concerns = tuple(
        concern(text, phrase, meaning_may_change=False)
        for phrase in (before_phrase, across_boundary_phrase, after_phrase)
    )

    readable = make_snapshot_candidate_readable(
        FaithfulReviewCandidate(text, concerns)
    )

    assert readable is not None
    assert readable.text.count("\n\n") >= 3
    for original, shifted in zip(concerns, readable.concerns, strict=True):
        assert readable.text[shifted.start_offset : shifted.end_offset] == original.text
        assert shifted.end_offset - shifted.start_offset == len(original.text)
    assert readable.concerns[0].start_offset == concerns[0].start_offset
    assert readable.concerns[1].text == across_boundary_phrase
    assert "\n\n" not in readable.concerns[1].text
    assert readable.concerns[2].start_offset > concerns[2].start_offset + 2


def test_long_candidate_is_reaccepted_after_paragraphing_without_changing_gates():
    text = long_candidate_text()

    result = accept_snapshot_candidate(
        recovery(text),
        FaithfulReviewCandidate(text, ()),
    )

    assert result.kind is SnapshotAcceptanceKind.PASSED
    assert result.snapshot is not None
    assert result.snapshot.text.count("\n\n") >= 2
    restored, _ = restore_inserted_breaks(text, result.snapshot.text)
    assert restored == text


def test_long_text_without_safe_sentence_boundaries_keeps_original_paragraph():
    text = "这是一段没有任何可用句末边界的连续来源内容" * 40

    result = accept_snapshot_candidate(
        recovery(text),
        FaithfulReviewCandidate(text, ()),
    )

    assert result.kind is SnapshotAcceptanceKind.PASSED
    assert result.snapshot is not None
    assert result.snapshot.text == text


def test_few_noncritical_local_uncertainties_pass_and_are_preserved():
    uncertainty = concern(TEXT, "作者", meaning_may_change=False)

    result = accept_snapshot_candidate(
        recovery(),
        FaithfulReviewCandidate(TEXT, (uncertainty,)),
    )

    assert result.kind is SnapshotAcceptanceKind.PASSED_WITH_UNCERTAINTY
    assert result.snapshot.uncertainties == (uncertainty,)


def test_meaning_changing_concern_requires_local_resolution():
    result = accept_snapshot_candidate(
        recovery(),
        FaithfulReviewCandidate(
            TEXT,
            (concern(TEXT, "采购量", meaning_may_change=True),),
        ),
    )

    assert result.kind is SnapshotAcceptanceKind.NEEDS_LOCAL_RESOLUTION
    assert result.snapshot is None


def test_major_content_loss_is_rejected():
    result = accept_snapshot_candidate(
        recovery(),
        FaithfulReviewCandidate("作者介绍了药品集采。", ()),
    )

    assert result.kind is SnapshotAcceptanceKind.REJECTED


def test_incomplete_primary_is_rejected():
    result = accept_snapshot_candidate(
        recovery(truncated=True),
        FaithfulReviewCandidate(TEXT, ()),
    )

    assert result.kind is SnapshotAcceptanceKind.REJECTED


def test_equal_adjacent_chunk_boundaries_are_accepted():
    result = accept_snapshot_candidate(
        recovery_with_chunk_boundaries(((0.0, 10.0), (10.0, 20.0))),
        FaithfulReviewCandidate(TEXT, ()),
    )

    assert result.kind is SnapshotAcceptanceKind.PASSED


def test_float_noise_at_adjacent_chunk_boundary_is_accepted():
    result = accept_snapshot_candidate(
        recovery_with_chunk_boundaries(
            (
                (0.0, 181.4399375),
                (181.43993749999998, 194.28275),
            )
        ),
        FaithfulReviewCandidate(TEXT, ()),
    )

    assert result.kind is SnapshotAcceptanceKind.PASSED


def test_chunk_overlap_does_not_reject_complete_text_without_local_resolution():
    result = accept_snapshot_candidate(
        recovery_with_chunk_boundaries(((0.0, 10.0), (9.999, 20.0))),
        FaithfulReviewCandidate(TEXT, ()),
    )

    assert result.kind is SnapshotAcceptanceKind.PASSED


def test_out_of_order_chunks_do_not_reject_complete_text_without_local_resolution():
    result = accept_snapshot_candidate(
        recovery_with_chunk_boundaries(((0.0, 10.0), (5.0, 6.0))),
        FaithfulReviewCandidate(TEXT, ()),
    )

    assert result.kind is SnapshotAcceptanceKind.PASSED


def test_invalid_or_nonlocal_uncertainty_structure_is_rejected():
    phrases = ("作者", "药品", "流程", "医院")
    concerns = tuple(
        concern(TEXT, phrase, meaning_may_change=False) for phrase in phrases
    )
    concerns = tuple(sorted(concerns, key=lambda item: item.start_offset))

    result = accept_snapshot_candidate(
        recovery(),
        FaithfulReviewCandidate(TEXT, concerns),
    )

    assert result.kind is SnapshotAcceptanceKind.REJECTED


def test_many_meaning_changing_concerns_are_rejected_before_local_resolution():
    phrases = ("作者", "药品", "流程", "医院")
    concerns = tuple(
        concern(TEXT, phrase, meaning_may_change=True) for phrase in phrases
    )
    concerns = tuple(sorted(concerns, key=lambda item: item.start_offset))

    result = accept_snapshot_candidate(
        recovery(),
        FaithfulReviewCandidate(TEXT, concerns),
    )

    assert result.kind is SnapshotAcceptanceKind.REJECTED
