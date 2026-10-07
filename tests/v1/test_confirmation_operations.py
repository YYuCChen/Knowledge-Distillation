"""U05 operation grouping must preserve the real confirmation contracts.

These SSR checks complement browser layout/focus/media acceptance; they do not
claim to verify the CSS's visual output.
"""
from types import SimpleNamespace

from bs4 import BeautifulSoup
import pytest

from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import _home_context, create_app


def application(tmp_path, *, choices=(), explanations=None, recovery=True, english=True):
    store = Store(tmp_path / 'isolated.sqlite3')
    app = create_app(store, object(), settings_service=SimpleNamespace(chrome=object()))
    item = store.create_item('http://127.0.0.1/synthetic-source')
    snapshot = ('We heard a sample phrase. This is synthetic practice. Keep the source unchanged.'
                if english else '这是一段合成测试文字。请核对局部词语，并保留来源原文。')
    text = 'sample phrase' if english else '局部词语'
    start = snapshot.index(text)
    store.mark_waiting(item, {'snapshot': snapshot, 'concerns': [{
        'audio_name': 'synthetic.wav', 'start': start, 'end': start + len(text),
        'text': text, 'reason': '合成听写疑点', 'candidates': list(choices),
        'candidate_explanations': explanations or {},
        'audio_recovery_required': recovery,
    }]})
    return app, store, item


def rendered(app, store, item):
    with app.test_request_context('/'):
        view = _home_context(store, None)['todo'][0]['confirmation']
    response = app.test_client().get('/')
    assert response.status_code == 200
    return BeautifulSoup(response.text, 'html.parser'), item, view


def page(tmp_path, **kwargs):
    return rendered(*application(tmp_path, **kwargs))


@pytest.mark.parametrize('choices,explanations,needs_suggestions', [
    ((), {}, True),
    (('sample phrase', 'simple phrase'), {'sample phrase': '保留原有表达'}, True),
    (('sample phrase', 'simple phrase'),
     {'sample phrase': '保留原有表达', 'simple phrase': '简单的表达；另一个候选'}, False),
])
def test_recovery_operations_preserve_forms_media_and_candidate_order(
        tmp_path, choices, explanations, needs_suggestions):
    soup, item, pending = page(tmp_path, choices=choices, explanations=explanations)
    concern = pending['concerns'][0]
    key = f"{item}-{concern['concern_uid']}"
    area = soup.select_one('.confirmation-actions')
    operations = area.select_one('.confirmation-operations')
    assert area['id'] == f'actions-{key}' and area.has_attr('hidden')
    assert operations.parent is area
    assert operations['id'] == f'operations-{key}'
    assert 'has-audio-recovery' in operations['class']
    assert ('without-suggestions' in operations['class']) is not needs_suggestions
    assert bool(operations.select('form[data-suggest-candidates]')) is needs_suggestions
    assert not soup.select('form form')

    candidate_panel = area.select_one('.candidate-actions')
    if choices:
        assert candidate_panel.parent is area
        assert [node for node in area.children if getattr(node, 'name', None)] == [candidate_panel, operations]
        assert [b['value'] for b in candidate_panel.select('button')] == list(choices)
        assert [b.get_text() for b in candidate_panel.select('button')] == list(choices)
        assert [p.get_text() for p in candidate_panel.select('.candidate-explanation')] == [
            explanations[choice] for choice in choices if explanations.get(choice)]
    else:
        assert candidate_panel is None

    audio = operations.select_one('audio')
    assert audio['data-audio-identity'] == key
    assert audio['data-audio-revision'] == concern['audio_revision']
    assert audio['src'] == f'/items/{item}/confirmation-audio?concern_id=synthetic.wav'
    assert audio.has_attr('controls') and audio['preload'] == 'metadata'
    assert not audio.has_attr('autoplay')
    toggle = soup.select_one('[data-card-toggle]')
    assert toggle['aria-controls'] == area['id'] and toggle['aria-expanded'] == 'false'
    manual = operations.select_one('.manual-confirmation')
    assert manual['id'] == f'manual-{key}'
    assert operations.select_one('.confirmation-buttons > button')['form'] == manual['id']
    assert manual.select_one('input[name=value]')['value'] == ''

    for form in area.select('form'):
        assert form['method'] == 'post'
        fields = {i['name']: i['value'] for i in form.select('input[type=hidden]')}
        assert fields['token'] == pending['token']
        if form.has_attr('data-recover-audio'):
            assert form['action'] == f'/items/{item}/recover-confirmation-audio'
            assert fields == {'token': pending['token'], 'concern_id': 'synthetic.wav'}
        elif form.has_attr('data-rerecognize'):
            assert form['action'] == f'/items/{item}/rerecognize'
            assert fields == {'token': pending['token']}
        else:
            assert fields['concern_id'] == 'synthetic.wav'
            assert fields['concern_revision'] == concern['revision']
            assert form['action'] == (f'/items/{item}/suggestions' if form.has_attr('data-suggest-candidates')
                                      else f'/items/{item}/confirm')
            if form is manual:
                assert fields['action'] == 'manual'
            elif 'candidate-row' in form.get('class', []):
                assert fields['action'] == 'candidate'
    assert operations.select_one('.confirmation-buttons button[name=action]')['value'] == 'unable'


@pytest.mark.parametrize('english,recovery', [(True, False), (False, True), (False, False)])
def test_other_confirmation_layouts_keep_original_direct_operations(tmp_path, english, recovery):
    soup, _, _ = page(tmp_path, recovery=recovery, english=english)
    area = soup.select_one('.confirmation-actions')
    operations = area.select_one('.confirmation-operations')
    if english:
        assert operations.parent is area
        assert operations['id'].startswith('operations-')
        assert 'has-audio-recovery' not in operations['class']
    else:
        assert operations is None
    parent = operations if english else area
    assert area.select_one('audio').parent is parent
    assert area.select_one('.source-actions').parent is parent
    assert area.select_one('.manual-entry').parent is parent
    assert area.select_one('.confirmation-buttons').parent is parent
    assert bool(area.select('[data-recover-audio]')) is recovery
    assert bool(area.select('.suggest-candidates')) is english
    assert bool(soup.select('.english-confirmation')) is english


def test_same_english_operation_identity_survives_candidates_and_recovery_end(tmp_path):
    app, store, item = application(tmp_path)
    first, _, initial = rendered(app, store, item)
    initial_operations = first.select_one('.confirmation-operations')
    initial_audio = first.select_one('audio')
    initial_manual = first.select_one('.manual-confirmation')
    for recovery in (True, False, True):
        pending = store.confirmation_view(item)
        concern = pending['concerns'][0]
        concern['candidates'] = ['sample phrase', 'simple phrase']
        concern['candidate_explanations'] = {
            'sample phrase': '保留原有表达', 'simple phrase': '简单的表达；另一个候选'}
        concern['audio_recovery_required'] = recovery
        store.mark_waiting(item, pending)
        current, _, view = rendered(app, store, item)
        operations = current.select_one('.confirmation-operations')
        audio = operations.select_one('audio')
        manual = operations.select_one('.manual-confirmation')
        assert operations['id'] == initial_operations['id']
        assert ('has-audio-recovery' in operations['class']) is recovery
        assert current.select_one('.candidate-actions').parent is operations.parent
        assert not operations.select('.suggest-candidates')
        assert audio['data-audio-identity'] == initial_audio['data-audio-identity']
        assert audio['src'] == initial_audio['src']
        assert audio['data-audio-revision'] == view['concerns'][0]['audio_revision']
        assert manual['id'] == initial_manual['id']
        assert operations.select_one('.confirmation-buttons > button')['form'] == manual['id']
        assert manual['action'] == initial_manual['action']
        # Real writes issue fresh tokens; every retained action must render the
        # current token/revision, rather than retaining a stale submission.
        assert view['token'] != initial['token']
        assert {node['value'] for node in operations.select('input[name=token]')} == {view['token']}
        assert {node['value'] for node in operations.select('input[name=concern_revision]')} == {
            view['concerns'][0]['revision']}
        assert not current.select('form form')
