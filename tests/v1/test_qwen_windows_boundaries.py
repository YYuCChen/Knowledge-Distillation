"""Windows segmentation frame ownership; synthetic PCM is mechanism evidence only."""
import array
from knowledge_distiller.v1.adapters import qwen_windows_worker as worker


def test_segments_prefer_sustained_quiet_gap_not_a_brief_plosive():
    rate = 16000
    samples = array.array('h', [1000]) * (rate * 45)
    samples[18*rate:18*rate+rate//10] = array.array('h', [0]) * (rate//10)
    samples[19*rate:19*rate+rate//50] = array.array('h', [0]) * (rate//50)
    ranges = worker.source_ranges(samples.tobytes(), rate)
    assert 18 <= ranges[0][1] / rate <= 18.1
    assert ranges[0][1] / rate != 19
    assert ranges[0][0] == 0 and ranges[-1][1] == len(samples)
    assert all(a[1] == b[0] for a,b in zip(ranges,ranges[1:]))
    assert all(0 < end-start <= 20*rate for start,end in ranges)
    assert b''.join(samples[start:end].tobytes() for start,end in ranges) == samples.tobytes()


def test_short_audio_keeps_its_original_frame_interval():
    assert worker.source_ranges(b'\x01\x00' * 127999, 16000) == [(0, 127999)]


def test_continuous_audio_keeps_a_bounded_contiguous_fallback():
    rate = 16000
    assert worker.source_ranges(b'\xe8\x03' * (rate*41), rate) == [
        (0,rate*20),(rate*20,rate*40),(rate*40,rate*41)]


def test_windows_worker_change_invalidates_only_its_cache_identity(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import qwen_component
    for name in ('qwen_windows_worker.py', 'qwen_worker.py'):
        (tmp_path / name).write_text('old worker')
    monkeypatch.setattr(qwen_component, 'ASSETS', tmp_path)
    class Component:
        def __init__(self, windows):
            self.windows = windows
        def identity(self):
            return ('test-runtime', 'test-model')
    windows = qwen_component.ComponentQwenRuntime(Component(True))
    mac = qwen_component.ComponentQwenRuntime(Component(False))
    old_windows, old_mac = windows.cache_identity, mac.cache_identity
    (tmp_path / 'qwen_windows_worker.py').write_text('pause-aware worker')
    assert windows.cache_identity != old_windows
    assert mac.cache_identity == old_mac
