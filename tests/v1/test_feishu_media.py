from types import SimpleNamespace
from unittest.mock import Mock
import wave

from knowledge_distiller.v1.feishu_media import FeishuMedia


def test_resource_cache_is_owned_by_app_and_survives_restart(tmp_path):
    api=SimpleNamespace(app_id='new-bot',upload_image=Mock(return_value='img-new'))
    assert FeishuMedia(api,tmp_path).image(b'image')=='img-new'
    assert FeishuMedia(api,tmp_path).image(b'image')=='img-new'
    api.upload_image.assert_called_once()
    other=SimpleNamespace(app_id='other-bot',upload_image=Mock(return_value='img-other'))
    assert FeishuMedia(other,tmp_path).image(b'image')=='img-other'
    other.upload_image.assert_called_once()


def test_audio_transcodes_only_excerpt_once_and_keeps_source(tmp_path,monkeypatch):
    source=tmp_path/'excerpt.wav'
    with wave.open(str(source),'wb') as output:
        output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000)
        output.writeframes(b'\0\0'*16000)
    before=source.read_bytes()
    def convert(command,**kwargs):
        from pathlib import Path
        Path(command[-1]).write_bytes(b'encoded opus')
        return SimpleNamespace(returncode=0)
    convert=Mock(side_effect=convert)
    monkeypatch.setattr('knowledge_distiller.v1.feishu_media.subprocess.run',convert)
    api=SimpleNamespace(app_id='new-bot',upload_audio=Mock(return_value='file-own'))
    media=FeishuMedia(api,tmp_path/'cache')
    assert media.audio(source)==media.audio(source)=='file-own'
    convert.assert_called_once()
    api.upload_audio.assert_called_once_with(b'encoded opus',duration_ms=1000)
    assert source.read_bytes()==before
    assert not list(media.root.glob('opus-*'))
