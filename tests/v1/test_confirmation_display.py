from knowledge_distiller.v1.confirmation_display import local_choices
from knowledge_distiller.v1.reviewer import ReviewBinding
from knowledge_distiller.v1.confirmation_display import english_candidate_display


def english_fixture():
    snapshot = 'Earlier sentence. We repeat retrieval, then retrieval. Later sentence.'
    left = snapshot.index('We repeat')
    start = snapshot.index('retrieval', snapshot.index('then'))
    right = snapshot.index('. Later') + 1
    concern = {'concern_uid': 'synthetic-uid', 'start': start, 'end': start + 9,
               'text': 'retrieval', 'candidates': ['retrieval', 'retention'],
               'sentence_span': {'start': left, 'end': right},
               'candidate_translations': {'retrieval': '回忆提取，再次提取。', 'retention': '回忆提取，之后保持。'},
               'candidate_basis': {'retrieval': '后文讨论主动回忆；仍需原音。', 'retention': '可能讨论记忆保持；证据有限。'},
               'candidate_explanations': {'retrieval': '旧混合解释不可替代新合同'},
               'reason': '仅为共同疑点原因'}
    return snapshot, concern


def test_english_sentence_replaces_only_the_verified_occurrence_without_mutation():
    from copy import deepcopy
    snapshot, concern = english_fixture()
    original = deepcopy(concern)
    view = english_candidate_display(snapshot, concern)
    assert view['sentence'] == 'We repeat retrieval, then retrieval.'
    assert [c['value'] for c in view['candidates']] == ['retrieval', 'retention']
    assert [c['text'] for c in view['candidates']] == [
        'We repeat retrieval, then retrieval.', 'We repeat retrieval, then retention.']
    assert view['candidates'][1]['meaning'] == concern['candidate_translations']['retention']
    assert view['candidates'][1]['basis'] == concern['candidate_basis']['retention']
    assert concern == original


def test_english_display_identity_tracks_semantics_not_order_or_submission_token():
    from copy import deepcopy
    snapshot, concern = english_fixture()
    first = {c['value']: c['key'] for c in english_candidate_display(snapshot, concern)['candidates']}
    reordered = deepcopy(concern)
    reordered.update(token='fresh', revision='fresh', candidates=list(reversed(concern['candidates'])))
    assert {c['value']: c['key'] for c in english_candidate_display(snapshot, reordered)['candidates']} == first
    for field in ('candidate_basis', 'candidate_translations'):
        changed = deepcopy(concern)
        changed[field]['retention'] += ' 新内容。'
        keys = {c['value']: c['key'] for c in english_candidate_display(snapshot, changed)['candidates']}
        assert keys['retention'] != first['retention'] and keys['retrieval'] == first['retrieval']
    moved = deepcopy(concern)
    moved['concern_uid'] = 'other-uid'
    assert english_candidate_display(snapshot, moved)['candidates'][0]['key'] != first['retrieval']
    assert all(len(key) == 64 and set(key) <= set('0123456789abcdef') for key in first.values())


def test_english_display_preserves_candidate_spaces_unicode_and_html_as_plain_text():
    snapshot = '“A e\u0301 phrase” stays. Other text.'
    start = snapshot.index('e\u0301')
    original = 'e\u0301'
    alternative = ' <script>👩🏽\u200d💻</script> '
    concern = {'concern_uid': 'unicode', 'start': start, 'end': start + len(original),
               'text': original, 'candidates': [original, alternative],
               'sentence_span': {'start': 0, 'end': snapshot.index(' Other')},
               'candidate_translations': {original: '原文', alternative: '替换'},
               'candidate_basis': {original: '依据一', alternative: '依据二'}}
    result = english_candidate_display(snapshot, concern)['candidates'][1]
    assert result['value'] == alternative
    assert result['text'] == '“A  <script>👩🏽\u200d💻</script>  phrase” stays.'


def test_english_display_rejects_invalid_spans_and_source_mismatch():
    from copy import deepcopy
    import pytest
    snapshot, concern = english_fixture()
    mutations = [
        {'sentence_span': None}, {'sentence_span': {'start': 0}},
        {'sentence_span': {'start': 0, 'end': len(snapshot), 'guessed': True}},
        {'sentence_span': {'start': -1, 'end': len(snapshot)}},
        {'sentence_span': {'start': 0, 'end': len(snapshot) + 1}},
        {'sentence_span': {'start': concern['start'] + 1, 'end': len(snapshot)}},
        {'sentence_span': {'start': 0, 'end': concern['end'] - 1}},
        {'sentence_span': {'start': False, 'end': len(snapshot)}},
        {'start': True}, {'end': concern['start']}, {'text': 'stale'},
    ]
    for mutation in mutations:
        changed = deepcopy(concern)
        changed.update(mutation)
        with pytest.raises(ValueError):
            english_candidate_display(snapshot, changed)
    for start, end in [(0, 1), (1, 2)]:
        changed = deepcopy(concern)
        changed.update(start=start, end=end, text='e\u0301'[start:end],
                       sentence_span={'start': 0, 'end': 2}, candidates=['e\u0301'[start:end]])
        changed['candidate_translations'] = {changed['text']: '解释'}
        changed['candidate_basis'] = {changed['text']: '依据'}
        with pytest.raises(ValueError, match='cluster'):
            english_candidate_display('e\u0301', changed)


def test_english_display_requires_original_and_exact_complete_independent_maps():
    from copy import deepcopy
    import pytest
    snapshot, concern = english_fixture()
    invalid = [
        {'concern_uid': ''}, {'candidates': []}, {'candidates': ['retention']},
        {'candidates': ['retrieval', 'retrieval']}, {'candidates': ['retrieval', 1]},
        {'candidate_translations': {'retrieval': '只有一项'}},
        {'candidate_basis': None}, {'candidate_basis': {'retrieval': '一项'}},
        {'candidate_basis': {**concern['candidate_basis'], 'unknown': '多余项'}},
        {'candidate_translations': {**concern['candidate_translations'], 'retention': ' '}},
        {'candidate_basis': {**concern['candidate_basis'], 'retention': 1}},
    ]
    for mutation in invalid:
        changed = deepcopy(concern)
        changed.update(mutation)
        with pytest.raises(ValueError):
            english_candidate_display(snapshot, changed)
    # Neither mixed legacy explanations nor the concern-level reason fills a gap.
    missing = deepcopy(concern)
    del missing['candidate_basis']
    with pytest.raises(ValueError, match='mapping'):
        english_candidate_display(snapshot, missing)


def test_short_original_chinese_word_is_not_split():
    view = local_choices({'start': 5, 'text': '持续', 'candidates': ['持续', '继续']})
    assert view['text'] == '持续'
    assert view['choices'] == [('持续', '持续'), ('继续', '继续')]


def test_language_specific_review_instructions_do_not_leak():
    class Client:
        def complete(self, **kwargs):
            self.kwargs = kwargs
            return '{}'
    client = Client()
    binding = ReviewBinding(client)
    binding.complete('中文讨论亚马逊和Tim的品牌关系。')
    assert '英文较弱' not in client.kwargs['system']
    assert 'candidate_explanations' not in client.kwargs['system']
    binding.complete('These people return to civilization pretty fast and afraid.')
    assert '英文较弱' in client.kwargs['system']
    assert 'candidate_explanations' in client.kwargs['system']


def window(before, marked, after):
    from knowledge_distiller.v1.confirmation_display import context_window
    return context_window(before + marked + after,
                          {'start': len(before), 'end': len(before) + len(marked),
                           'text': marked, 'concern_uid': 'stable'},
                          full_context_ref='/items/fixture')


def test_shared_context_chinese_budget_and_borrowing():
    view = window('前' * 80, '疑点', '后' * 80)
    assert view['before'] == '前' * 48
    assert view['after'] == '后' * 48
    assert view['marked'] == '疑点'
    assert view['span'] == [80, 82]
    assert view['omitted_before'] and view['omitted_after']
    assert view['full_context_ref'] == '/items/fixture'
    view = window('前' * 3, '疑点', '后' * 100)
    assert len(view['after']) == 93
    assert not view['omitted_before']


def test_english_context_complete_words_apostrophes_and_borrowing():
    view = window(' '.join(["don't"] * 30) + ' ', 'target', ' ' + ' '.join(['after'] * 30))
    assert view['before'].split() == ["don't"] * 24
    assert view['after'].split() == ['after'] * 24
    view = window('one two ', 'target', ' ' + ' '.join(['after'] * 60))
    assert len(view['after'].split()) == 46
    assert view['unit'] == 'word'


def test_context_clusters_emoji_flags_combining_and_local_language():
    from knowledge_distiller.v1.confirmation_display import _clusters
    for cluster in ('e\u0301', '👩🏽\u200d💻', '🇨🇳', 'क्\u200dष'):
        assert len(_clusters(cluster)) == 1
        view = window(cluster * 70, '中文', cluster * 70)
        assert view['before'] == cluster * 48
        assert view['after'] == cluster * 48
    view = window(('foreign ' * 1000) + '这是当前中文上下文', '疑点', '后续内容')
    assert view['unit'] == 'display_cluster'


def test_context_rejects_stale_or_cluster_splitting_span():
    import pytest
    from knowledge_distiller.v1.confirmation_display import context_window
    with pytest.raises(ValueError, match='match'):
        context_window('abc', {'start': 0, 'end': 1, 'text': 'wrong'})
    with pytest.raises(ValueError, match='cluster'):
        context_window('e\u0301', {'start': 0, 'end': 1, 'text': 'e'})


def test_context_does_not_modify_candidate_callback_values():
    import copy
    from knowledge_distiller.v1.confirmation_display import context_window
    concern = {'start': 0, 'end': 2, 'text': '原文', 'candidates': [' 新词 ', '原词']}
    original = copy.deepcopy(concern)
    assert context_window('原文', concern)['marked'] == '原文'
    assert concern == original
