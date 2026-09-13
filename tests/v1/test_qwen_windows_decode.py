"""Regression for actual Windows output shortened by processor post-processing."""
import pytest

from knowledge_distiller.v1.adapters.qwen_windows_worker import decode_source, has_repetition_run


class CapturedProcessor:
    def __init__(self, raw, parsed):
        self.raw, self.parsed = raw, parsed

    def decode(self, generated, *, return_format, **kwargs):
        if return_format == 'raw':
            assert kwargs == {'skip_special_tokens': True}
            return [self.raw]
        assert return_format == 'parsed'
        return [{'transcription': self.parsed}]


def test_real_repeated_output_keeps_all_model_words_and_requires_recovery():
    # Native CPU inference on 30 spoken words ended in EOS after 24 words.
    # The installed Transformers parser then returned only "Apple. Apple.".
    model_words = ' '.join(['Apple.'] * 24)
    decoded = decode_source(CapturedProcessor(
        'language English<asr_text>' + model_words, 'Apple. Apple.'), None)
    assert decoded == {'text': model_words, 'language': 'English',
                       'postprocessing_changed_text': True}


@pytest.mark.parametrize('raw, text, language', [
    ('language English<asr_text>Apple. Apple.', 'Apple. Apple.', 'English'),
    ('language Chinese<asr_text>不，不，不。\n保留这行。', '不，不，不。\n保留这行。', 'Chinese'),
    ('language None<asr_text>', '', None),
    ('assistant\nlanguage English<asr_text>Hello.', 'Hello.', 'English'),
    ('A plain response.', 'A plain response.', None),
    ('The literal text <asr_text> stays.', 'The literal text <asr_text> stays.', None),
])
def test_protocol_removal_preserves_content_and_does_not_reject_ordinary_output(raw, text, language):
    assert decode_source(CapturedProcessor(raw, text), None) == {
        'text': text, 'language': language, 'postprocessing_changed_text': False}


def test_interior_assistant_line_is_source_content_even_if_parser_drops_it():
    words = 'Keep this assistant\nquoted word.'
    result = decode_source(CapturedProcessor(
        'language English<asr_text>' + words, 'quoted word.'), None)
    assert result['text'] == words
    assert result['postprocessing_changed_text'] is True


@pytest.mark.parametrize('text', [
    'Apple. ' * 13, 'Banana! ' * 5, 'Red blue green yellow. ' * 5,
    '前文。' + '不要重复删除。' * 5 + '后文。', '不不不不不',
])
def test_dense_repetition_requests_smaller_audio_pieces_without_vocabulary_rules(text):
    assert has_repetition_run(text)


@pytest.mark.parametrize('text', [
    'Apple. Apple. Apple. Apple.',
    'One two three four five six seven eight nine ten.',
    'The first apple is red. The next apple is green. Those bananas are ripe.',
    '中文内容需要保留，不能删除。', '',
])
def test_short_repeats_and_varied_speech_do_not_trigger_repetition_recovery(text):
    assert not has_repetition_run(text)
