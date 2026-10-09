from knowledge_distiller.v1.confirmation_display import local_choices
from knowledge_distiller.v1.reviewer import ReviewBinding


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
