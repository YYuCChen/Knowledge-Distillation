"""K04: a ten-second preview cannot claim to contain a longer target."""
import wave
from knowledge_distiller.primary import StandardAudio,PrimaryRecovery,PrimaryChunk
from knowledge_distiller.faithful_review import ReviewConcern
from knowledge_distiller.v1.confirmation import locate_concern_audio


def test_long_target_is_unavailable_instead_of_silently_cropped(tmp_path):
    path=tmp_path/'source.wav'
    with wave.open(str(path),'wb') as stream:
        stream.setparams((1,2,16000,0,'NONE','not compressed'));stream.writeframes(bytes(20*16000*2))
    text='The complete target phrase occupies the entire twenty second source interval.'
    recovery=PrimaryRecovery(text,'en',(PrimaryChunk(text,0,20,'en'),),timeline_status='available')
    concern=ReviewConcern(0,len(text),text,'unclear',True)
    assert locate_concern_audio(StandardAudio(path,20),recovery,text,concern) is None


def test_preview_contains_target_at_source_edges():
    from knowledge_distiller.v1.confirmation import _preview_window
    for start, end, duration in ((0, 3, 25), (22, 25, 25), (7, 17, 25), (0, 6, 6)):
        left, right = _preview_window(start, end, duration)
        assert 0 <= left <= start < end <= right <= duration
        assert right - left == min(10, duration)


def test_preview_rejects_even_slightly_oversized_target():
    from knowledge_distiller.v1.confirmation import _preview_window
    assert _preview_window(3, 13.001, 25) is None
