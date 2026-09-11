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
