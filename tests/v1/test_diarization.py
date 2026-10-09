"""Synthetic R12 contract tests. No model imports, services, or real audio.

Execution is delegated separately; this task authors but does not run pytest.
All filesystem cases use pytest's explicitly disposable tmp_path.
"""
from dataclasses import replace
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from knowledge_distiller.v1.diarization import (
    DiarizationContractError, ExistingLabel, ExecutionPermit, LocalAsset,
    LocalPyannoteConfig, MODEL_REVISION, Provenance, PyannoteLocalAdapter,
    SpeakerInterval, TimedTextSpan, align_transcript,
    _constrain_pipeline_config,
    _native_loader, _verified_config_bytes,
)


SOURCE = Provenance('transcript', 'synthetic:source')
ACOUSTIC = Provenance('diarization', 'synthetic:whole-recording', MODEL_REVISION)


def span(a, b, start, end, granularity='word'):
    return TimedTextSpan(a, b, start, end, granularity,
                         Provenance(f'{granularity}_timing', f'synthetic:time:{a}:{b}'))


def speaker(start, end, key):
    return SpeakerInterval(start, end, key, ACOUSTIC)


def align(text, spans=(), intervals=(), labels=(), **kwargs):
    return align_transcript(recording_id='synthetic:recording', text=text,
                            transcript_provenance=SOURCE, duration_seconds=20,
                            timed_spans=spans, speaker_intervals=intervals,
                            existing_labels=labels, **kwargs)


def exact_partition(result):
    assert ''.join(result.text[p.char_start:p.char_end] for p in result.partitions) == result.text
    assert [p.char_start for p in result.partitions] == [0, *[p.char_end for p in result.partitions[:-1]]]
    assert result.partitions[-1].char_end == len(result.text)
    assert all(p.char_end > p.char_start and SOURCE in p.provenance for p in result.partitions)


def test_three_speakers_short_interruption_and_non_alternating_return():
    text = '甲乙丙丁'
    spans = (span(0, 1, 0, 2), span(1, 2, 2, 2.1), span(2, 3, 2.1, 4), span(3, 4, 4, 5))
    intervals = (speaker(4, 5, 'z'), speaker(2, 2.1, 'y'), speaker(2.1, 4, 'x'), speaker(0, 2, 'x'))
    result = align(text, spans, intervals)
    assert [p.speaker_id for p in result.partitions] == ['A', 'B', 'A', 'C']
    assert dict(result.speaker_map) == {'x': 'A', 'y': 'B', 'z': 'C'}
    assert all(p.time_evidence[0].granularity == 'word' for p in result.partitions)
    exact_partition(result)


def test_coarse_cross_person_chunk_never_uses_longest_duration():
    original = '问题？ 是。\n嘉宾的回答 '
    coarse = span(0, len(original), 0, 10, 'chunk')
    result = align(original, (coarse,), (speaker(0, 9.9, 'host'), speaker(9.9, 10, 'guest')))
    p = result.partitions[0]
    assert p.status == 'unknown' and p.speaker_id is None
    assert p.candidate_speaker_ids == ('A', 'B')
    assert p.time_evidence == (coarse,)
    assert result.needs_local_alignment[0].time_evidence == (coarse,)
    exact_partition(result)


@pytest.mark.parametrize('granularity', ['word', 'boundary', 'chunk'])
def test_entire_span_unique_continuous_speaker_preserves_actual_granularity(granularity):
    timing = span(0, 3, 0, 3, granularity)
    result = align('甲乙丙', (timing,), (speaker(0, 1, 'x'), speaker(1, 3, 'x')))
    assert result.partitions[0].speaker_id == 'A'
    assert result.partitions[0].time_evidence == (timing,)
    assert result.needs_local_alignment == ()


def test_full_overlap_retains_both_speakers_and_original_intervals():
    intervals = (speaker(0, 4, 'x'), speaker(0, 4, 'y'))
    result = align('同时', (span(0, 2, 1, 3),), intervals)
    p = result.partitions[0]
    assert p.status == 'overlap' and p.speaker_id is None
    assert p.candidate_speaker_ids == ('A', 'B') and p.speaker_evidence == intervals


def test_brief_overlap_inside_span_cannot_be_discarded():
    result = align('插话', (span(0, 2, 0, 5),), (speaker(0, 5, 'x'), speaker(2, 2.1, 'y')))
    assert result.partitions[0].status == 'unknown'
    assert 'mixed_overlap' in result.partitions[0].reasons
    assert result.needs_local_alignment


def test_small_uncovered_gap_blocks_single_speaker_assignment():
    result = align('缺口', (span(0, 2, 0, 3),), (speaker(0, 1, 'x'), speaker(1.00001, 3, 'x')))
    assert result.partitions[0].status == 'unknown'
    assert 'speaker_coverage_gap' in result.partitions[0].reasons


def test_partial_word_times_keep_punctuation_whitespace_and_unicode_unknown():
    text = '甲，\n 乙🙂'
    result = align(text, (span(0, 1, 0, 1), span(4, 5, 2, 3)), (speaker(0, 4, 'x'),))
    assert [p.status for p in result.partitions] == ['speaker', 'unknown', 'speaker', 'unknown']
    assert result.text == text
    assert result.partitions[1].time_evidence == ()
    exact_partition(result)


def test_existing_label_splits_char_partition_without_fabricating_seconds():
    label = ExistingLabel(1, 2, '鲁豫', Provenance('source_subtitle', 'synthetic:subtitle:1'))
    timing = span(0, 3, 0, 3, 'chunk')
    result = align('甲乙丙', (timing,), (speaker(0, 3, 'SPEAKER_99'),), (label,))
    assert [p.speaker_id for p in result.partitions] == ['A', 'A', 'A']
    assert result.partitions[1].preserved_labels == (label,)
    assert all(p.time_evidence == (timing,) for p in result.partitions)
    assert result.partitions[1].time_evidence[0].char_start == 0
    assert label.label == '鲁豫' and dict(result.speaker_map) == {'SPEAKER_99': 'A'}
    exact_partition(result)


def test_conflicting_existing_labels_preserved_with_no_arbitrary_winner():
    labels = (ExistingLabel(0, 2, 'A', Provenance('source_subtitle', 'synthetic:subtitle')),
              ExistingLabel(0, 2, '嘉宾', Provenance('user_confirmed', 'synthetic:user')))
    result = align('未知', labels=labels)
    assert result.partitions[0].preserved_labels == labels
    assert result.partitions[0].status == 'unknown'
    assert 'label_conflict:0:2' in result.diagnostics
    assert result.partitions[0].speaker_id is None
    assert 'label_conflict' in result.partitions[0].reasons
    assert result.needs_local_alignment[0].resolution_kind == 'source_label_review'


def test_conflicting_source_labels_override_unique_acoustic_assignment():
    timing = span(0, 4, 0, 4, 'chunk')
    acoustic = (speaker(0, 4, 'anonymous-acoustic-key'),)
    labels = (ExistingLabel(1, 3, '原字幕甲', Provenance('source_subtitle', 'synthetic:subtitle')),
              ExistingLabel(2, 4, '用户乙', Provenance('user_confirmed', 'synthetic:user')))
    result = align('原字全文', (timing,), acoustic, labels)
    assert [p.status for p in result.partitions] == ['speaker', 'speaker', 'unknown', 'speaker']
    conflict = result.partitions[2]
    assert (conflict.char_start, conflict.char_end) == (2, 3)
    assert conflict.speaker_id is None and conflict.candidate_speaker_ids == ('A',)
    assert conflict.speaker_evidence == acoustic and conflict.time_evidence == (timing,)
    assert conflict.preserved_labels == labels
    assert all(label.provenance in conflict.provenance for label in labels)
    assert conflict.reasons == ('label_conflict',)
    assert len(result.needs_local_alignment) == 1
    assert result.needs_local_alignment[0].resolution_kind == 'source_label_review'
    assert labels[0].label == '原字幕甲' and labels[1].label == '用户乙'
    exact_partition(result)


def test_empty_recording_text_and_missing_timing_are_lossless():
    assert align('').partitions == ()
    result = align('是\n否 ')
    assert result.partitions[0].status == 'unknown'
    exact_partition(result)


def test_anonymous_identity_is_whole_recording_and_input_order_independent():
    intervals = (speaker(5, 6, 'x'), speaker(0, 1, 'y'), speaker(2, 3, 'x'))
    spans = (span(0, 1, 0, 1), span(1, 2, 2, 3), span(2, 3, 5, 6))
    first = align('甲乙丙', spans, intervals)
    second = align('甲乙丙', spans, intervals[::-1])
    assert first.speaker_map == second.speaker_map == (('y', 'A'), ('x', 'B'))
    assert [p.speaker_id for p in first.partitions] == ['A', 'B', 'B']


@pytest.mark.parametrize('bad', [
    span(-1, 1, 0, 1), span(0, 4, 0, 1), span(True, 1, 0, 1),
    span(0, 1, -1, 1), span(0, 1, 0, 21), span(0, 1, 1, 1),
    span(0, 1, float('nan'), 1), span(0, 1, 0, float('inf')),
    replace(span(0, 1, 0, 1), provenance=Provenance('semantic_guess', 'synthetic:guess')),
])
def test_invalid_ranges_or_untrusted_times_fail_without_clipping(bad):
    original = '原字'
    with pytest.raises(DiarizationContractError):
        align(original, (bad,))
    assert original == '原字'


@pytest.mark.parametrize('granularity', ['word', 'boundary'])
def test_disjoint_characters_with_conflicting_fine_time_coverage_rejected(granularity):
    with pytest.raises(DiarizationContractError, match='fine_time_coverage_conflict'):
        align('甲乙', (span(0, 1, 0, 2, granularity), span(1, 2, 1, 3, granularity)))


def test_coarse_overlapping_windows_remain_unknown_even_with_one_speaker():
    result = align('甲乙', (span(0, 1, 0, 2, 'chunk'), span(1, 2, 1, 3, 'chunk')),
                   (speaker(0, 3, 'x'),))
    assert all(p.status == 'unknown' and 'coarse_time_coverage_conflict' in p.reasons
               for p in result.partitions)


@pytest.mark.parametrize('spans,code', [
    ((span(0, 2, 0, 1), span(1, 3, 1, 2)), 'overlapping_or_unordered_chars'),
    ((span(0, 1, 2, 3), span(1, 2, 0, 1)), 'time_order_conflict'),
])
def test_order_and_char_conflicts_fail(spans, code):
    with pytest.raises(DiarizationContractError, match=code):
        align('甲乙丙', spans)


@pytest.fixture
def local_candidate(tmp_path):
    root = tmp_path.resolve() / 'synthetic-r12-component'
    root.mkdir()
    assets = []
    # These are fake bytes, never a usable model or audio payload.
    for name, content in [('config.yaml', b'fake reviewed config'),
                          ('segmentation/pytorch_model.bin', b'fake segmentation'),
                          ('embedding/pytorch_model.bin', b'fake embedding'),
                          ('plda/plda.npz', b'fake plda'),
                          ('plda/xvec_transform.npz', b'fake transform')]:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
        assets.append(LocalAsset(name, hashlib.sha256(content).hexdigest()))
    config = LocalPyannoteConfig(root, tuple(assets), 'synthetic:license-evidence')
    permit = ExecutionPermit('synthetic:authorization', 'synthetic:network-isolation',
                             'synthetic:config-review', Path('/synthetic/component/python'))
    audio = root / 'synthetic-audio-placeholder'
    audio.write_bytes(b'not real audio; fake pipeline ignores content')
    return config, permit, audio


def test_prepare_does_not_invoke_loader_and_does_not_claim_deployment(local_candidate):
    config, _, _ = local_candidate
    def forbidden(*args):
        pytest.fail('prepare must not load models')
    result = PyannoteLocalAdapter(config, loader=forbidden).prepare()
    assert result.state == 'prepared_local_candidate'
    assert not result.deployment_verified and not result.missing and not result.problems
    assert result.core_manifest_complete


def test_no_permit_refuses_before_any_model_access(local_candidate):
    config, _, audio = local_candidate
    def forbidden(*args):
        pytest.fail('unauthorized inference must not load models')
    with pytest.raises(DiarizationContractError, match='execution_not_authorized'):
        PyannoteLocalAdapter(config, loader=forbidden).diarize(
            recording_id='synthetic', audio_path=audio, duration_seconds=3)


def test_fake_adapter_uses_ordinary_unrounded_overlap_output(local_candidate):
    config, permit, audio = local_candidate
    calls = []
    class Annotation:
        def itertracks(self, *, yield_label):
            assert yield_label is True
            return iter([(SimpleNamespace(start=0.00123, end=2.9), None, 'x'),
                         (SimpleNamespace(start=1.1, end=2.2), None, 'y')])
    class Output:
        speaker_diarization = Annotation()
        @property
        def exclusive_speaker_diarization(self):
            pytest.fail('exclusive output loses overlap')
    def loader(actual_config, actual_permit):
        assert actual_config == config and actual_permit == permit
        calls.append('load')
        def pipeline(path):
            assert path == str(audio)
            calls.append('infer')
            return Output()
        return pipeline
    result = PyannoteLocalAdapter(config, loader=loader).diarize(
        recording_id='synthetic', audio_path=audio, duration_seconds=3, permit=permit)
    assert calls == ['load', 'infer']
    assert len(result.intervals) == 2 and result.intervals[0].start_seconds == 0.00123
    assert not result.deployment_verified


def test_missing_hash_license_and_runtime_are_explicit(local_candidate):
    config, _, _ = local_candidate
    (config.model_directory / 'segmentation/pytorch_model.bin').unlink()
    (config.model_directory / 'config.yaml').write_bytes(b'changed fake config')
    result = PyannoteLocalAdapter(replace(config, license_acceptance_ref=None,
                                          runtime_version='other')).prepare()
    assert result.missing == ('segmentation/pytorch_model.bin',)
    assert 'asset_hash_mismatch:config.yaml' in result.problems
    assert 'license_acceptance_evidence_missing' in result.problems
    assert 'runtime_version_mismatch' in result.problems


@pytest.mark.parametrize('path', ['https://example.com/model', '/absolute', '../escape'])
def test_remote_or_escaping_asset_rejected(local_candidate, path):
    config, _, _ = local_candidate
    config = replace(config, assets=(LocalAsset(path, 'a' * 64),))
    with pytest.raises(DiarizationContractError, match='invalid_asset_path'):
        PyannoteLocalAdapter(config).prepare()


def test_model_directory_symlink_refused(local_candidate, tmp_path):
    config, _, _ = local_candidate
    alias = tmp_path.resolve() / 'model-alias'
    alias.symlink_to(config.model_directory, target_is_directory=True)
    with pytest.raises(DiarizationContractError, match='expected_absolute_local_model_directory'):
        PyannoteLocalAdapter(replace(config, model_directory=alias)).prepare()


def test_native_loader_requires_distinct_runtime_before_import(local_candidate):
    config, permit, audio = local_candidate
    with pytest.raises(DiarizationContractError, match='isolated_runtime_python_mismatch'):
        PyannoteLocalAdapter(config).diarize(recording_id='synthetic', audio_path=audio,
                                             duration_seconds=3, permit=permit)


def test_primary_local_config_rewrite_has_no_cwd_or_remote_defaults(local_candidate):
    config, _, _ = local_candidate
    required = ('segmentation/pytorch_model.bin', 'embedding/pytorch_model.bin',
                'plda/plda.npz', 'plda/xvec_transform.npz')
    config = replace(config, assets=tuple(LocalAsset(p, 'a' * 64) for p in required))
    value = {'pipeline': {'name': 'pyannote.audio.pipelines.SpeakerDiarization',
                         'params': {k: f'$model/{k}' for k in ('segmentation', 'embedding', 'plda')}}}
    result = _constrain_pipeline_config(value, config)
    for key in ('segmentation', 'embedding', 'plda'):
        assert result['pipeline']['params'][key] == {
            'checkpoint': str(config.model_directory), 'subfolder': key, 'token': False}
        assert value['pipeline']['params'][key] == f'$model/{key}'


@pytest.mark.parametrize('params', [
    {}, {'segmentation': 'pyannote/remote', 'embedding': '$model/embedding', 'plda': '$model/plda'},
    {'segmentation': '$model/segmentation@remote', 'embedding': '$model/embedding', 'plda': '$model/plda'},
])
def test_missing_or_remote_submodel_config_fails_before_pipeline_import(local_candidate, params):
    config, _, _ = local_candidate
    value = {'pipeline': {'name': 'pyannote.audio.pipelines.SpeakerDiarization', 'params': params}}
    with pytest.raises(DiarizationContractError):
        _constrain_pipeline_config(value, config)


def test_invalid_speaker_time_is_not_clipped_or_ignored():
    with pytest.raises(DiarizationContractError, match='invalid_time'):
        align('原字', (span(0, 2, 0, 2),), (speaker(0, 20.001, 'x'),))


def test_missing_isolation_evidence_refuses_fake_loader(local_candidate):
    config, permit, audio = local_candidate
    def forbidden(*args):
        pytest.fail('missing isolation evidence must block even injected loader')
    with pytest.raises(DiarizationContractError, match='invalid_string:network_isolation_ref'):
        PyannoteLocalAdapter(config, loader=forbidden).diarize(
            recording_id='synthetic', audio_path=audio, duration_seconds=3,
            permit=replace(permit, network_isolation_ref=''))


def test_unsupported_config_class_and_missing_native_manifest_refused(local_candidate):
    config, _, _ = local_candidate
    params = {key: f'$model/{key}' for key in ('segmentation', 'embedding', 'plda')}
    with pytest.raises(DiarizationContractError, match='unsupported_pipeline_class'):
        _constrain_pipeline_config({'pipeline': {'name': 'arbitrary.remote.Class', 'params': params}}, config)
    with pytest.raises(DiarizationContractError, match='submodel_manifest_incomplete'):
        _constrain_pipeline_config({'pipeline': {
            'name': 'pyannote.audio.pipelines.SpeakerDiarization', 'params': params}},
            replace(config, assets=config.assets[:2]))


def model_config(**extras):
    return {'pipeline': {'name': 'pyannote.audio.pipelines.SpeakerDiarization',
                         'params': {k: f'$model/{k}' for k in ('segmentation', 'embedding', 'plda')}},
            **extras}


@pytest.mark.parametrize('extra,code', [
    ({'params': '/outside/params.yml'}, 'unsupported_instantiation_params'),
    ({'params': {'segmentation': {'min_duration_off': '/outside/file'}}}, 'invalid_instantiation_number'),
    ({'params': {'clustering': {'threshold': 'https://example.com/model'}}}, 'invalid_instantiation_number'),
    ({'params': {'checkpoint': '/outside/model'}}, 'unsupported_instantiation_params'),
    ({'params': {'clustering': {'threshold': float('nan')}}}, 'invalid_instantiation_number'),
    ({'params': {'clustering': {'threshold': True}}}, 'invalid_instantiation_number'),
    ({'dependencies': {'/outside/package': '4.0.7'}}, 'unsupported_config_dependencies'),
    ({'dependencies': {'pyannote.audio': 'https://example.com/version'}}, 'unsupported_config_dependencies'),
    ({'version': '/outside/version'}, 'unsupported_config_version'),
    ({'version': '4.0.7', 'dependencies': {'pyannote.audio': '4.0.7'}}, 'unsupported_config_version'),
    ({'preprocessors': {'audio': '/outside/audio.wav'}}, 'unsupported_local_config'),
    ({'freeze': '/outside/freeze'}, 'unsupported_local_config'),
    ({'device': 'remote'}, 'unsupported_local_config'),
])
def test_top_level_config_path_remote_or_untyped_values_rejected(local_candidate, extra, code):
    config, _, _ = local_candidate
    with pytest.raises(DiarizationContractError, match=code):
        _constrain_pipeline_config(model_config(**extra), config)


def test_top_level_instantiation_only_keeps_known_finite_numeric_values(local_candidate):
    config, _, _ = local_candidate
    params = {'segmentation': {'min_duration_off': 0.0},
              'clustering': {'threshold': .6, 'Fa': .07, 'Fb': .8}}
    value = model_config(params=params, dependencies={'pyannote.audio': '4.0.7'})
    result = _constrain_pipeline_config(value, config)
    assert result['params'] == params
    assert result['dependencies'] == {'pyannote.audio': '4.0.7'}


def test_two_fake_assets_do_not_report_complete_core_manifest(local_candidate):
    config, _, _ = local_candidate
    result = PyannoteLocalAdapter(replace(config, assets=config.assets[:2])).prepare()
    assert result.missing == ('manifest:embedding/pytorch_model.bin',
                              'manifest:plda/plda.npz', 'manifest:plda/xvec_transform.npz')
    assert not result.core_manifest_complete and not result.deployment_verified


@pytest.mark.parametrize('changed', ['license', 'weights', 'directory'])
def test_native_entry_rechecks_candidate_before_import(local_candidate, monkeypatch, changed):
    config, permit, _ = local_candidate
    assert PyannoteLocalAdapter(config).prepare().core_manifest_complete
    permit = replace(permit, runtime_python=Path(sys.executable),
                     asset_immutability_ref='synthetic:read-only-snapshot')
    monkeypatch.setenv('PYANNOTE_METRICS_ENABLED', 'false')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('HF_HUB_DISABLE_TELEMETRY', '1')
    monkeypatch.delenv('PYANNOTE_SKIP_DEPENDENCY_CHECK', raising=False)
    if changed == 'license':
        config = replace(config, license_acceptance_ref=None)
    elif changed == 'weights':
        (config.model_directory / 'embedding/pytorch_model.bin').write_bytes(b'changed fake bytes')
    else:
        (config.model_directory / 'plda/plda.npz').unlink()
    with pytest.raises(DiarizationContractError, match='local_candidate_not_ready'):
        _native_loader(config, permit)


def test_config_bytes_rechecked_not_reopened_after_hash(local_candidate):
    config, _, _ = local_candidate
    assert _verified_config_bytes(config) == b'fake reviewed config'
    (config.model_directory / 'config.yaml').write_bytes(b'changed YAML')
    with pytest.raises(DiarizationContractError, match='local_config_hash_changed'):
        _verified_config_bytes(config)


def test_native_entry_requires_immutable_asset_evidence(local_candidate):
    config, permit, _ = local_candidate
    permit = replace(permit, runtime_python=Path(sys.executable))
    with pytest.raises(DiarizationContractError, match='invalid_string:asset_immutability_ref'):
        _native_loader(config, permit)
