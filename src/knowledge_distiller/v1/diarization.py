"""Independent R12 candidate. No ASR, persistence, UI, or import-time inference.

Times describe supplied spans, never inferred character timestamps. Native
execution requires separate authorization and an externally isolated process.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from copy import deepcopy
from pathlib import Path
import sys
from typing import Callable

PYANNOTE_VERSION = '4.0.7'
PYANNOTE_COMMIT = 'b749285c5cdd4636b2edc7f766f1352c8dde9369'
MODEL_REVISION = '3533c8cf8e369892e6b79ff1bf80f7b0286a54ee'
WHEEL_SHA256 = '852ea15c4d85bc34773e618267603ffca6a521669a74d33742692cc67fc700d6'
CORE_ASSET_PATHS = ('config.yaml', 'segmentation/pytorch_model.bin',
                    'embedding/pytorch_model.bin', 'plda/plda.npz', 'plda/xvec_transform.npz')


class DiarizationContractError(ValueError):
    def __init__(self, code: str, input_ref: str = ''):
        self.code, self.input_ref = code, input_ref
        super().__init__(f'{code}:{input_ref}')


@dataclass(frozen=True)
class Provenance:
    kind: str
    source_ref: str
    revision: str | None = None


@dataclass(frozen=True)
class TimedTextSpan:
    char_start: int
    char_end: int
    start_seconds: float
    end_seconds: float
    granularity: str
    provenance: Provenance


@dataclass(frozen=True)
class SpeakerInterval:
    start_seconds: float
    end_seconds: float
    speaker_key: str
    provenance: Provenance


@dataclass(frozen=True)
class ExistingLabel:
    char_start: int
    char_end: int
    label: str
    provenance: Provenance


@dataclass(frozen=True)
class LocalAlignmentNeed:
    char_start: int
    char_end: int
    time_evidence: tuple[TimedTextSpan, ...]
    reasons: tuple[str, ...]
    resolution_kind: str = 'acoustic_alignment'


@dataclass(frozen=True)
class AlignedPartition:
    char_start: int
    char_end: int
    status: str
    speaker_id: str | None
    candidate_speaker_ids: tuple[str, ...]
    time_evidence: tuple[TimedTextSpan, ...]
    speaker_evidence: tuple[SpeakerInterval, ...]
    provenance: tuple[Provenance, ...]
    reasons: tuple[str, ...]
    preserved_labels: tuple[ExistingLabel, ...]


@dataclass(frozen=True)
class AlignmentResult:
    recording_id: str
    text: str
    transcript_provenance: Provenance
    partitions: tuple[AlignedPartition, ...]
    speaker_map: tuple[tuple[str, str], ...]
    needs_local_alignment: tuple[LocalAlignmentNeed, ...]
    diagnostics: tuple[str, ...]


def _nonempty(value, ref):
    if not isinstance(value, str) or not value.strip():
        raise DiarizationContractError('invalid_string', ref)


def _provenance(value, kinds, ref):
    if not isinstance(value, Provenance) or value.kind not in kinds:
        raise DiarizationContractError('invalid_provenance', ref)
    _nonempty(value.source_ref, ref)
    if value.revision is not None:
        _nonempty(value.revision, ref)


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _time(start, end, duration, ref):
    if not (_number(start) and _number(end) and 0 <= start < end <= duration):
        raise DiarizationContractError('invalid_time', ref)


def _chars(start, end, text, ref):
    if not (type(start) is int and type(end) is int and 0 <= start < end <= len(text)):
        raise DiarizationContractError('invalid_char_range', ref)


def _anonymous(index):
    result = ''
    index += 1
    while index:
        index, digit = divmod(index - 1, 26)
        result = chr(65 + digit) + result
    return result


def _classify(span, intervals, ids):
    relevant = tuple(i for i in intervals
                     if i.start_seconds < span.end_seconds and i.end_seconds > span.start_seconds)
    cuts = sorted({span.start_seconds, span.end_seconds, *(
        t for i in relevant for t in (i.start_seconds, i.end_seconds)
        if span.start_seconds < t < span.end_seconds)})
    active_sets = [frozenset(i.speaker_key for i in relevant
                            if i.start_seconds <= start and i.end_seconds >= end)
                   for start, end in zip(cuts, cuts[1:])]
    candidates = tuple(ids[k] for k in ids if any(k in active for active in active_sets))
    if active_sets and len(active_sets[0]) == 1 and all(a == active_sets[0] for a in active_sets):
        key = next(iter(active_sets[0]))
        return 'speaker', ids[key], candidates, relevant, ()
    if active_sets and all(len(a) > 1 for a in active_sets):
        return 'overlap', None, candidates, relevant, ('simultaneous_speakers',)
    reasons = []
    if any(not a for a in active_sets):
        reasons.append('speaker_coverage_gap')
    if any(len(a) > 1 for a in active_sets):
        reasons.append('mixed_overlap')
    if len(candidates) > 1:
        reasons.append('speaker_boundary_without_char_time')
    return 'unknown', None, candidates, relevant, tuple(reasons or ['no_speaker_evidence'])


def align_transcript(*, recording_id: str, text: str, transcript_provenance: Provenance,
                     duration_seconds: float, timed_spans: tuple[TimedTextSpan, ...],
                     speaker_intervals: tuple[SpeakerInterval, ...],
                     existing_labels: tuple[ExistingLabel, ...] = ()) -> AlignmentResult:
    """Partition exact source characters using only explicitly supplied evidence.

    Labels retain source identity separately from the acoustic status/anonymous
    IDs. A label boundary inside a timed span does not create a new timestamp.
    """
    _nonempty(recording_id, 'recording_id')
    if not isinstance(text, str):
        raise DiarizationContractError('invalid_text')
    _provenance(transcript_provenance, {'transcript'}, 'transcript')
    if not _number(duration_seconds) or duration_seconds <= 0:
        raise DiarizationContractError('invalid_duration')
    spans = tuple(timed_spans)
    intervals = tuple(speaker_intervals)
    labels = tuple(existing_labels)
    previous_char = 0
    previous = None
    timing_kinds = {'word': 'word_timing', 'boundary': 'boundary_timing', 'chunk': 'chunk_timing'}
    for n, span in enumerate(spans):
        ref = f'timed_spans[{n}]'
        if not isinstance(span, TimedTextSpan):
            raise DiarizationContractError('invalid_span', ref)
        _chars(span.char_start, span.char_end, text, ref)
        _time(span.start_seconds, span.end_seconds, duration_seconds, ref)
        if span.granularity not in timing_kinds:
            raise DiarizationContractError('invalid_granularity', ref)
        _provenance(span.provenance, {timing_kinds[span.granularity]}, ref)
        if span.char_start < previous_char:
            raise DiarizationContractError('overlapping_or_unordered_chars', ref)
        if previous and (span.start_seconds < previous.start_seconds or
                         span.end_seconds < previous.end_seconds):
            raise DiarizationContractError('time_order_conflict', ref)
        if previous and span.start_seconds < previous.end_seconds and (
                span.granularity != 'chunk' or previous.granularity != 'chunk'):
            raise DiarizationContractError('fine_time_coverage_conflict', ref)
        previous_char, previous = span.char_end, span
    first = {}
    for n, interval in enumerate(intervals):
        ref = f'speaker_intervals[{n}]'
        if not isinstance(interval, SpeakerInterval):
            raise DiarizationContractError('invalid_speaker_interval', ref)
        _time(interval.start_seconds, interval.end_seconds, duration_seconds, ref)
        _nonempty(interval.speaker_key, ref)
        _provenance(interval.provenance, {'diarization'}, ref)
        first[interval.speaker_key] = min(first.get(interval.speaker_key, math.inf), interval.start_seconds)
    ids = {key: _anonymous(n) for n, key in enumerate(sorted(first, key=lambda k: (first[k], k)))}
    for n, label in enumerate(labels):
        ref = f'existing_labels[{n}]'
        if not isinstance(label, ExistingLabel):
            raise DiarizationContractError('invalid_label', ref)
        _chars(label.char_start, label.char_end, text, ref)
        _nonempty(label.label, ref)
        _provenance(label.provenance, {'source_subtitle', 'user_confirmed'}, ref)
    classifications = [_classify(span, intervals, ids) for span in spans]
    # Overlapping coarse windows for disjoint characters are not precise timing.
    conflicting_chunks = {n for n, span in enumerate(spans) if any(
        n != m and span.start_seconds < other.end_seconds and other.start_seconds < span.end_seconds
        for m, other in enumerate(spans))}
    cuts = sorted({0, len(text), *(c for s in spans for c in (s.char_start, s.char_end)),
                   *(c for label in labels for c in (label.char_start, label.char_end))})
    partitions, needs, diagnostics = [], [], []
    for start, end in zip(cuts, cuts[1:]):
        if start == end:
            continue
        index = next((n for n, s in enumerate(spans) if s.char_start <= start and s.char_end >= end), None)
        if index is None:
            status, speaker, candidates, evidence, reasons = 'unknown', None, (), (), ('missing_char_timing',)
            time_evidence = ()
        else:
            status, speaker, candidates, evidence, reasons = classifications[index]
            time_evidence = (spans[index],)
            if index in conflicting_chunks:
                status, speaker = 'unknown', None
                reasons = (*reasons, 'coarse_time_coverage_conflict')
        preserved = tuple(label for label in labels if label.char_start <= start and label.char_end >= end)
        if len({label.label for label in preserved}) > 1:
            diagnostics.append(f'label_conflict:{start}:{end}')
            status, speaker = 'unknown', None
            reasons = (*reasons, 'label_conflict')
        if preserved:
            # Different identity namespaces are never compared as equal/unequal.
            diagnostics.append(f'source_label_preserved:{start}:{end}')
        prov = tuple(dict.fromkeys((transcript_provenance,
            *(s.provenance for s in time_evidence), *(i.provenance for i in evidence),
            *(label.provenance for label in preserved))))
        partitions.append(AlignedPartition(start, end, status, speaker, candidates,
                                            time_evidence, evidence, prov, reasons, preserved))
        if status == 'unknown':
            resolution = 'source_label_review' if 'label_conflict' in reasons else 'acoustic_alignment'
            needs.append(LocalAlignmentNeed(start, end, time_evidence, reasons, resolution))
    return AlignmentResult(recording_id, text, transcript_provenance, tuple(partitions),
                           tuple(ids.items()), tuple(needs), tuple(diagnostics))


@dataclass(frozen=True)
class LocalAsset:
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class LocalPyannoteConfig:
    model_directory: Path
    assets: tuple[LocalAsset, ...]
    license_acceptance_ref: str | None = None
    model_revision: str = MODEL_REVISION
    runtime_version: str = PYANNOTE_VERSION


@dataclass(frozen=True)
class PreparedLocalCandidate:
    state: str
    missing: tuple[str, ...]
    problems: tuple[str, ...]
    model_revision: str
    verified_assets: tuple[LocalAsset, ...]
    deployment_verified: bool = False
    core_manifest_complete: bool = False


@dataclass(frozen=True)
class ExecutionPermit:
    """References to externally reviewed evidence, not self-certifying flags."""
    authorization_ref: str
    network_isolation_ref: str
    config_review_ref: str
    runtime_python: Path
    asset_immutability_ref: str | None = None


@dataclass(frozen=True)
class DiarizationEvidence:
    recording_id: str
    intervals: tuple[SpeakerInterval, ...]
    state: str = 'prepared_local_candidate'
    deployment_verified: bool = False


def _plain_local_path(path: Path) -> bool:
    return path.is_absolute() and '..' not in path.parts and not any(
        parent.is_symlink() for parent in (path, *path.parents))


class PyannoteLocalAdapter:
    """Preparation is read-only. Inference is explicitly gated and lazy.

    An injected loader supports fake tests. Native loading belongs solely in an
    authorized isolated component process; this class does not create isolation.
    """
    def __init__(self, config: LocalPyannoteConfig, *, loader: Callable | None = None):
        self.config, self.loader = config, loader

    def prepare(self) -> PreparedLocalCandidate:
        config = self.config
        missing, problems, verified = [], [], []
        root = config.model_directory
        if not isinstance(root, Path) or not _plain_local_path(root):
            raise DiarizationContractError('expected_absolute_local_model_directory')
        if not root.is_dir():
            missing.append('model_directory')
        if not isinstance(config.license_acceptance_ref, str) or not config.license_acceptance_ref.strip():
            problems.append('license_acceptance_evidence_missing')
        if config.model_revision != MODEL_REVISION:
            problems.append('model_revision_mismatch')
        if config.runtime_version != PYANNOTE_VERSION:
            problems.append('runtime_version_mismatch')
        seen = set()
        for asset in config.assets:
            relative = Path(asset.relative_path)
            if (relative.is_absolute() or '..' in relative.parts or not relative.parts or
                    asset.relative_path in seen or ':' in asset.relative_path or
                    relative.as_posix() != asset.relative_path):
                raise DiarizationContractError('invalid_asset_path', asset.relative_path)
            seen.add(asset.relative_path)
            if len(asset.sha256) != 64 or any(c not in '0123456789abcdef' for c in asset.sha256):
                raise DiarizationContractError('invalid_asset_digest', asset.relative_path)
            path = root / relative
            if not _plain_local_path(path):
                problems.append(f'linked_asset:{asset.relative_path}')
            elif not path.is_file():
                missing.append(asset.relative_path)
            else:
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                if digest != asset.sha256:
                    problems.append(f'asset_hash_mismatch:{asset.relative_path}')
                else:
                    verified.append(asset)
        for required in CORE_ASSET_PATHS:
            if required not in seen:
                missing.append(f'manifest:{required}')
        return PreparedLocalCandidate('prepared_local_candidate', tuple(missing), tuple(problems),
                                      config.model_revision, tuple(verified),
                                      core_manifest_complete=not missing and not problems)

    def diarize(self, *, recording_id: str, audio_path: Path, duration_seconds: float,
                permit: ExecutionPermit | None = None) -> DiarizationEvidence:
        _nonempty(recording_id, 'recording_id')
        if not _number(duration_seconds) or duration_seconds <= 0:
            raise DiarizationContractError('invalid_duration')
        if permit is None:
            raise DiarizationContractError('execution_not_authorized')
        for key in ('authorization_ref', 'network_isolation_ref', 'config_review_ref'):
            _nonempty(getattr(permit, key), key)
        prepared = self.prepare()
        if prepared.missing or prepared.problems:
            raise DiarizationContractError('local_candidate_not_ready',
                                           ','.join((*prepared.missing, *prepared.problems)))
        if not isinstance(audio_path, Path) or not _plain_local_path(audio_path) or not audio_path.is_file():
            raise DiarizationContractError('expected_local_audio_file')
        loader = self.loader or _native_loader
        pipeline = loader(self.config, permit)
        output = pipeline(str(audio_path))
        # Exclusive output intentionally never used: it removes overlaps.
        annotation = output.speaker_diarization
        provenance = Provenance('diarization', recording_id, self.config.model_revision)
        intervals = tuple(SpeakerInterval(turn.start, turn.end, speaker, provenance)
                          for turn, _, speaker in annotation.itertracks(yield_label=True))
        for n, interval in enumerate(intervals):
            _time(interval.start_seconds, interval.end_seconds, duration_seconds, str(n))
            _nonempty(interval.speaker_key, str(n))
        return DiarizationEvidence(recording_id, intervals)


def _native_loader(config: LocalPyannoteConfig, permit: ExecutionPermit):
    """4.0.7 primary API; never called during import or preparation.

    These guards do not implement a network sandbox. config_review_ref must
    cover the hashed config and *all* referenced local submodels, not just YAML.
    """
    if not isinstance(permit.runtime_python, Path) or permit.runtime_python != Path(sys.executable):
        raise DiarizationContractError('isolated_runtime_python_mismatch')
    for key in ('authorization_ref', 'network_isolation_ref', 'config_review_ref'):
        _nonempty(getattr(permit, key), key)
    _nonempty(permit.asset_immutability_ref, 'asset_immutability_ref')
    expected = {'PYANNOTE_METRICS_ENABLED': 'false', 'HF_HUB_OFFLINE': '1',
                'HF_HUB_DISABLE_TELEMETRY': '1'}
    if any(os.environ.get(k) != v for k, v in expected.items()):
        raise DiarizationContractError('isolated_process_environment_required')
    if any(k == 'pyannote.audio' or k.startswith('pyannote.audio.') for k in sys.modules):
        raise DiarizationContractError('pyannote_already_imported')
    if os.environ.get('PYANNOTE_SKIP_DEPENDENCY_CHECK', '').lower() in {'1', 'true', 'yes'}:
        raise DiarizationContractError('dependency_check_bypass_refused')
    # Revalidate license/version declarations, root/links, all manifest hashes,
    # and all five core entries here, even if prepare succeeded earlier.
    _require_prepared(config)
    from importlib.metadata import version
    if version('pyannote-audio') != PYANNOTE_VERSION:
        raise DiarizationContractError('installed_runtime_version_mismatch')
    import yaml
    config_bytes = _verified_config_bytes(config)
    local_config = _constrain_pipeline_config(yaml.safe_load(config_bytes), config)
    from pyannote.audio import Pipeline
    # Dictionary is an official 4.0.7 entry point. Explicit absolute child
    # checkpoints avoid its dictionary-mode Path.cwd() resolution entirely.
    _require_prepared(config)
    pipeline = Pipeline.from_pretrained(local_config, token=False)
    # Detection after loading cannot undo unpickling changed bytes. The external
    # immutable snapshot/read-only ownership boundary is still mandatory.
    _require_prepared(config)
    if pipeline is None:
        raise DiarizationContractError('local_pipeline_load_failed')
    return pipeline


def _require_prepared(config):
    prepared = PyannoteLocalAdapter(config).prepare()
    if not prepared.core_manifest_complete:
        raise DiarizationContractError('local_candidate_not_ready',
                                       ','.join((*prepared.missing, *prepared.problems)))


def _verified_config_bytes(config):
    root = config.model_directory
    path = root / 'config.yaml'
    if not _plain_local_path(root) or not _plain_local_path(path) or not root.is_dir():
        raise DiarizationContractError('local_config_path_changed')
    expected = next((a.sha256 for a in config.assets if a.relative_path == 'config.yaml'), None)
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise DiarizationContractError('local_config_hash_changed')
    # Parse these exact verified bytes, never reopen YAML after verification.
    return content


def _constrain_pipeline_config(value, config: LocalPyannoteConfig):
    """Fail closed on unreviewed config shapes; never supply remote defaults.

    This restricted shape is based on primary loader code, not a claim that
    the inaccessible gated config has been tested against it.
    """
    if not isinstance(value, dict) or set(value) - {'pipeline', 'params', 'dependencies', 'version'}:
        raise DiarizationContractError('unsupported_local_config')
    constrained = deepcopy(value)
    # Upstream uses version to overwrite dependencies. Neither field loads a
    # model, but do not allow arbitrary metadata paths or untyped strings.
    if 'version' in constrained:
        if constrained['version'] != PYANNOTE_VERSION or 'dependencies' in constrained:
            raise DiarizationContractError('unsupported_config_version')
    if 'dependencies' in constrained:
        if constrained['dependencies'] != {'pyannote.audio': PYANNOTE_VERSION}:
            raise DiarizationContractError('unsupported_config_dependencies')
    if 'params' in constrained:
        hyperparams = constrained['params']
        allowed = {'segmentation': {'min_duration_off', 'threshold'},
                   'clustering': {'threshold', 'Fa', 'Fb'}}
        if not isinstance(hyperparams, dict) or set(hyperparams) - allowed.keys():
            raise DiarizationContractError('unsupported_instantiation_params')
        for group, entries in hyperparams.items():
            if not isinstance(entries, dict) or set(entries) - allowed[group]:
                raise DiarizationContractError('unsupported_instantiation_params', group)
            for key, number in entries.items():
                if not _number(number):
                    raise DiarizationContractError('invalid_instantiation_number', f'{group}.{key}')
    pipeline = constrained.get('pipeline')
    if (not isinstance(pipeline, dict) or set(pipeline) != {'name', 'params'} or
            pipeline['name'] != 'pyannote.audio.pipelines.SpeakerDiarization'):
        raise DiarizationContractError('unsupported_pipeline_class')
    params = pipeline['params']
    scalar_keys = {'legacy', 'segmentation_step', 'embedding_exclude_overlap',
                   'clustering', 'embedding_batch_size', 'segmentation_batch_size'}
    model_keys = {'segmentation', 'embedding', 'plda'}
    if not isinstance(params, dict) or set(params) - scalar_keys - model_keys or not model_keys <= set(params):
        raise DiarizationContractError('explicit_local_submodels_required')
    if params.get('legacy', False) is not False:
        raise DiarizationContractError('ordinary_diarize_output_required')
    if params.get('clustering', 'VBxClustering') != 'VBxClustering':
        raise DiarizationContractError('unsupported_clustering')
    if 'embedding_exclude_overlap' in params and type(params['embedding_exclude_overlap']) is not bool:
        raise DiarizationContractError('unsupported_pipeline_parameter', 'embedding_exclude_overlap')
    for key in ('embedding_batch_size', 'segmentation_batch_size'):
        if key in params and (type(params[key]) is not int or params[key] <= 0):
            raise DiarizationContractError('invalid_pipeline_parameter', key)
    if 'segmentation_step' in params and (
            not _number(params['segmentation_step']) or params['segmentation_step'] <= 0):
        raise DiarizationContractError('invalid_pipeline_parameter', 'segmentation_step')
    required = {'segmentation': ('segmentation/pytorch_model.bin',),
                'embedding': ('embedding/pytorch_model.bin',),
                'plda': ('plda/plda.npz', 'plda/xvec_transform.npz')}
    manifest = {asset.relative_path for asset in config.assets}
    for key, paths in required.items():
        if params[key] != f'$model/{key}':
            raise DiarizationContractError('unsupported_submodel_reference', key)
        if not set(paths) <= manifest:
            raise DiarizationContractError('submodel_manifest_incomplete', key)
        params[key] = {'checkpoint': str(config.model_directory), 'subfolder': key, 'token': False}
    return constrained
