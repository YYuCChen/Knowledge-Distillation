"""U05 operation grouping must preserve the real confirmation contracts.

These SSR checks complement browser layout/focus/media acceptance; they do not
claim to verify CSS geometry or background preparation. Display fields are
synthetic; legacy audio-recovery flags do not establish readiness.
"""
from types import SimpleNamespace
from pathlib import Path
from copy import deepcopy
import struct
import wave

from flask import render_template
from bs4 import BeautifulSoup
import pytest

from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import _home_context, create_app
from knowledge_distiller.v1.domain import CapturedMaterial
from knowledge_distiller.v1.confirmation_preparation import build_evidence, digest, ready


def _synthetic_wav(path):
    """Two seconds of fixture-only PCM; never speech or model evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b''.join(struct.pack('<h', (n % 257 - 128) * 80)
                                   for n in range(32000)))


def _prepared_waiting(store, item, pending):
    """Actual storage/byte proof through a controlled synthetic producer.

    Used again after fixture mutations; never mock ready or reuse stale proof.
    """
    pending = deepcopy(pending)
    # Every call explicitly constructs a new unprepared fixture request. Keeping
    # its prior manifest exercises real invalidation/failed→explicit retry even
    # when candidate semantics happen to be unchanged in the identity loop.
    for concern in pending['concerns']:
        concern['audio_recovery_required'] = True
    store.mark_waiting(item, pending)
    page = store.discover_pending_presentations()
    if item not in page['enqueued']:
        # A derived-field edit invalidates an otherwise same-bound old manifest.
        # Exercise its actual failed state and explicit public retry, not SQL or
        # deletion of the old marker to evade the gate.
        assert store.item_bundle(item)['state'] == 'failed'
        store.retry_pending_presentation(item)
    assert store.claim_next_work() == ('presentation', item)
    ownership = store.presentation_ownership(item)
    context = store.presentation_context(item)
    complete = deepcopy(context['pending'])
    root = context['item_runtime_root']
    complete['audio_alignment'] = 'asr_chunk_v2'
    ranges = {}
    for ordinal, concern in enumerate(complete['concerns']):
        filename = f'concern-{ordinal + 1}.wav'
        target = root / 'confirmation' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        # The range is the complete two-second synthetic source, so preview
        # PCM is exactly source[0:32000], not merely another valid WAV.
        target.write_bytes((root / 'audio' / 'standard.wav').read_bytes())
        concern.update(audio_file=filename, audio_recovery_required=False)
        concern.setdefault('sentence_span', {'start': 0, 'end': len(complete['snapshot'])})
        concern['candidate_translations'] = {
            value: concern.get('candidate_translations', {}).get(value, f'整句中文解释假例：{value}')
            for value in concern['candidates']}
        concern['candidate_basis'] = {
            value: concern.get('candidate_basis', {}).get(value, f'独立依据假例：{value}')
            for value in concern['candidates']}
        ranges[concern['concern_uid']] = [0.0, 2.0]
    final = store.presentation_context(item, complete)
    evidence = build_evidence(final['pending'], root,
        source_descriptor=final['source_descriptor'],
        model={'reviewer_type': 'controlled-ui-fixture', 'model': 'synthetic-only',
               'config_sha256': digest('ui-fixture-config'),
               'recognizer_sha256': digest('ui-fixture-no-recognizer')},
        ranges=ranges)
    assert {m['identity']['concern_uid'] for m in evidence['members']} == set(ranges)
    assert store.finish_pending_presentation(item, ownership, {
        'ownership': ownership, 'status': 'prepared', 'code': None,
        'pending': final['pending'], 'evidence': evidence}) == 'waiting_user'
    current = store.presentation_context(item)
    assert ready(current['pending'], root, source_descriptor=current['source_descriptor'])


def application(tmp_path, *, choices=None, explanations=None, recovery=True, english=True):
    tmp_path = tmp_path.resolve()
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    store = Store(tmp_path / 'isolated.sqlite3', runtime_root=runtime)
    app = create_app(store, object(), settings_service=SimpleNamespace(chrome=object()))
    item = store.create_item('http://127.0.0.1/synthetic-source')
    source_audio = tmp_path / 'synthetic-source.wav'
    _synthetic_wav(source_audio)
    store.attach_material(item, CapturedMaterial('feishu_voice', 'ui-synthetic-source',
        'http://127.0.0.1/synthetic-source', 'http://127.0.0.1/synthetic-source',
        {'source_version': 'ui-fixture-v1', 'synthetic': True}, source_audio, 2.0))
    root = runtime / 'items' / str(item)
    (root / 'audio').mkdir(parents=True)
    (root / 'audio' / 'standard.wav').write_bytes(source_audio.read_bytes())
    snapshot = ('We heard a sample phrase. This is synthetic practice. Keep the source unchanged.'
                if english else '这是一段合成测试文字。请核对局部词语，并保留来源原文。')
    text = 'sample phrase' if english else '局部词语'
    start = snapshot.index(text)
    values = list(choices) if choices is not None else [text, 'simple phrase' if english else '局部词句']
    display_fields = ({'sentence_span': {'start': 0, 'end': snapshot.index('.') + 1},
                       'candidate_translations': {v: f'整句中文解释假例：{v}' for v in values},
                       'candidate_basis': {v: f'独立依据假例：{v}' for v in values}} if english else {})
    _prepared_waiting(store, item, {'snapshot': snapshot,
        'audio_timeline': {'text': snapshot, 'duration_seconds': 2.0,
            'timeline_status': 'available', 'chunks': [
                {'text': snapshot, 'start_seconds': 0.0, 'end_seconds': 2.0}]},
        'concerns': [{
        'audio_name': 'synthetic.wav', 'start': start, 'end': start + len(text),
        'text': text, 'reason': '合成听写疑点', 'candidates': values,
        **display_fields,
        'candidate_explanations': explanations or {},
        'audio_recovery_required': recovery,
    }]})
    return app, store, item


def rendered(app, store, item, *, error=None):
    with app.test_request_context('/'):
        values = _home_context(store, None, confirmation_error=error)
        view = values['todo'][0]['confirmation']
        if error:
            return BeautifulSoup(render_template('home.html', **values), 'html.parser'), item, view
    response = app.test_client().get('/')
    assert response.status_code == 200
    return BeautifulSoup(response.text, 'html.parser'), item, view


def page(tmp_path, **kwargs):
    return rendered(*application(tmp_path, **kwargs))


@pytest.mark.parametrize('choices,explanations', [
    (('sample phrase',), {}),
    (('sample phrase', 'simple phrase'), {'sample phrase': '保留原有表达'}),
    (('sample phrase', 'simple phrase'),
     {'sample phrase': '保留原有表达', 'simple phrase': '简单的表达；另一个候选'}),
])
def test_recovery_operations_preserve_forms_media_and_candidate_order(
        tmp_path, choices, explanations):
    soup, item, pending = page(tmp_path, choices=choices, explanations=explanations)
    concern = pending['concerns'][0]
    key = f"{item}-{concern['concern_uid']}"
    area = soup.select_one('.confirmation-actions')
    operations = area.select_one('.confirmation-operations')
    assert area['id'] == f'actions-{key}' and area.has_attr('hidden')
    assert operations.parent is area
    assert operations['id'] == f'operations-{key}'
    assert operations['class'] == ['confirmation-operations']
    assert not soup.select('[data-suggest-candidates], [data-recover-audio]')
    assert '结合上下文补充候选' not in soup.get_text()
    assert '重试局部原音恢复' not in soup.get_text()
    assert not soup.select('form form')

    candidate_panel = area.select_one('.candidate-actions')
    if choices:
        assert candidate_panel.parent is area
        assert [node for node in area.children if getattr(node, 'name', None)] == [candidate_panel, area.select_one('.concern-reason'), operations, area.select_one('.confirmation-empty-error')]
        assert [b['value'] for b in candidate_panel.select('button')] == list(choices)
        assert [b.get_text() for b in candidate_panel.select('button')] == [
            '保留原文' if value == concern['text'] else '采用' for value in choices]
        assert [p.get_text() for p in candidate_panel.select('.candidate-text')] == [
            'We heard a ' + value + '.' for value in choices]
        assert [p.get_text() for p in candidate_panel.select('.candidate-explanation')] == [
            concern['candidate_translations'][choice] for choice in choices]
        assert [p.get_text() for p in candidate_panel.select('.candidate-basis p')] == [
            concern['candidate_basis'][choice] for choice in choices]
        assert all(d.select_one('summary').get_text() == '判断依据' and not d.has_attr('open')
                   for d in candidate_panel.select('details'))
        assert area.select_one('.concern-reason').get_text() == concern['reason']
    else:
        assert candidate_panel is None

    header = soup.select_one('.english-confirmation .source-fragment')
    assert header.get_text() == 'We heard a sample phrase.'
    assert not header.has_attr('data-context-before')
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
    empty_error = area.select_one('.confirmation-empty-error')
    assert empty_error['id'] == f'empty-error-{key}' and empty_error.has_attr('hidden')
    assert manual.select_one('input[name=value]')['data-empty-error'] == empty_error['id']
    assert manual.select_one('input[name=value]')['data-default-placeholder'] == '自定义（可选）'

    for form in area.select('form'):
        assert form['method'] == 'post'
        fields = {i['name']: i['value'] for i in form.select('input[type=hidden]')}
        assert fields['token'] == pending['token']
        if form.has_attr('data-rerecognize'):
            assert form['action'] == f'/items/{item}/rerecognize'
            assert fields == {'token': pending['token']}
        else:
            assert fields['concern_id'] == 'synthetic.wav'
            assert fields['concern_revision'] == concern['revision']
            assert form['action'] == f'/items/{item}/confirm'
            if form is manual:
                assert fields['action'] == 'manual'
            elif 'candidate-row' in form.get('class', []):
                assert fields['action'] == 'candidate'
    assert operations.select_one('.confirmation-buttons button[name=action]')['value'] == 'unable'
    assert operations.select_one('.source-actions a')['href'] == 'http://127.0.0.1/synthetic-source'
    assert not area.select('form[action$="/suggestions"], form[action$="/recover-confirmation-audio"]')


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
    assert not area.select('[data-recover-audio], [data-suggest-candidates]')
    assert '结合上下文补充候选' not in soup.get_text()
    assert '重试局部原音恢复' not in soup.get_text()
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
        _prepared_waiting(store, item, pending)
        current, _, view = rendered(app, store, item)
        operations = current.select_one('.confirmation-operations')
        audio = operations.select_one('audio')
        manual = operations.select_one('.manual-confirmation')
        assert operations['id'] == initial_operations['id']
        assert operations['class'] == ['confirmation-operations']
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


def test_english_operation_layout_contract_is_scoped_and_reclaims_removed_space():
    """Static row/column contract only; browser must verify actual geometry."""
    css = (Path(__file__).resolve().parents[2] /
           'src/knowledge_distiller/v1/static/home-todo.css').read_text()
    tail = css.split('/* Design master confirmation states;', 1)[1]
    assert 'has-audio-recovery' not in tail and 'without-suggestions' not in tail
    assert 'suggest-candidates' not in css and '[data-recover-audio]' not in css
    assert 'grid-template-columns: 130px minmax(0, 1fr) 122px;' in tail
    assert 'grid-template-columns: minmax(0, 1fr) 56px;' in tail
    assert 'height: 32px;' in tail and 'min-width: 0;' in tail
    wide, narrow = tail.split('@media (max-width: 720px)', 1)
    manual = wide.split('.confirmation-operations .manual-entry {', 1)[1].split('}', 1)[0]
    assert 'grid-column: 2;' in manual and 'grid-row: 2;' in manual
    assert '.confirmation-operations .source-actions { grid-column: 1; grid-row: 2; }' in narrow
    assert '.confirmation-operations .manual-entry { grid-column: 1; grid-row: 3; padding-right: 0; }' in narrow
    assert '.confirmation-operations .confirmation-buttons { grid-column: 1; grid-row: 4; }' in narrow
    for line in tail.splitlines():
        if '.confirmation-operations' in line:
            assert line.strip().startswith('.english-confirmation ')


def test_chinese_inline_candidates_submit_original_values_and_keep_expansion_identity(tmp_path):
    soup, item, pending = page(tmp_path, english=False)
    panel = soup.select_one('.compact-candidates')
    assert [b.get_text() for b in panel.select('button')] == ['局部词语', '局部词句']
    assert [b['value'] for b in panel.select('button')] == pending['concerns'][0]['candidates']
    for form in panel.select('form'):
        assert form['action'] == f'/items/{item}/confirm' and form['method'] == 'post'
        assert form.select_one('[name=action]')['value'] == 'candidate'
        assert form.select_one('[name=token]')['value'] == pending['token']
    assert soup.select_one('.confirmation-actions').has_attr('hidden')
    assert soup.select_one('audio').has_attr('controls')
    assert not soup.select('.candidate-basis, .candidate-description, .concern-reason')


@pytest.mark.parametrize('value,message,is_empty', [
    ('', '请输入正确文字', True),
    ('  ', '请输入正确文字', True),
    ('用户保留的草稿', '来源确认已更新，请查看当前疑点；输入已保留。', False),
    ('', 'source confirmation no longer matches snapshot', False),
    ('非空草稿', '请输入正确文字', False),
])
def test_manual_empty_error_is_exact_and_preserves_input(tmp_path, value, message, is_empty):
    app, store, item = application(tmp_path, english=False)
    error = {'id': item, 'concern_id': 'synthetic.wav', 'message': message, 'value': value}
    soup, _, _ = rendered(app, store, item, error=error)
    input_node = soup.select_one('.manual-confirmation input[name=value]')
    assert input_node['value'] == value and input_node['aria-invalid'] == 'true'
    assert input_node.has_attr('data-empty-confirmation') is is_empty
    assert not soup.select_one('.confirmation-actions').has_attr('hidden')
    paragraph = soup.select_one('.confirmation-empty-error')
    assert input_node['data-empty-error'] == paragraph['id']
    assert input_node['data-default-placeholder'] == '自定义输入…'
    if is_empty:
        assert input_node['placeholder'] == '请输入确认文字'
        assert paragraph.get_text() == '请输入确认文字后再提交；也可以选择候选或回听原音。'
        assert input_node['aria-describedby'] == paragraph['id']
        assert not paragraph.has_attr('hidden')
    else:
        assert input_node['placeholder'] == message and paragraph.has_attr('hidden')
        assert not input_node.has_attr('aria-describedby')


def test_independent_basis_identity_and_html_are_safe_in_the_actual_template(tmp_path):
    app, store, item = application(tmp_path)
    pending = store.confirmation_view(item)
    concern = pending['concerns'][0]
    concern['candidate_basis']['simple phrase'] = '<script>synthetic</script> & independent'
    concern['candidate_translations']['simple phrase'] = '整句中文 <img src=x onerror=synthetic>'
    _prepared_waiting(store, item, pending)
    soup, _, view = rendered(app, store, item)
    details = soup.select('.candidate-basis')
    assert len(details) == 2 and len({d['id'] for d in details}) == 2
    assert all(d['data-persist-details'] == d['id'] for d in details)
    assert details[1].select_one('p').get_text() == concern['candidate_basis']['simple phrase']
    assert not soup.select('.candidate-description script, .candidate-description img')
    first = {b['value']: (f['id'], f.select_one('details')['id']) for f in soup.select('.candidate-row')
             for b in f.select('button[name=value]')}
    pending = store.confirmation_view(item)
    pending['concerns'][0]['candidates'].reverse()
    _prepared_waiting(store, item, pending)
    reordered, _, _ = rendered(app, store, item)
    assert {f.select_one('button[name=value]')['value']: (f['id'], f.select_one('details')['id'])
            for f in reordered.select('.candidate-row')} == first
