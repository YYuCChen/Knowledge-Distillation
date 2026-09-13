import json
import wave
from types import SimpleNamespace

import pytest

from knowledge_distiller.primary import PrimaryRecovery, PrimaryChunk, PrimaryRecognition, StandardAudio
from knowledge_distiller.v1.primary_cache import recognize_cached
from knowledge_distiller.v1.subtitle_baseline import select_subtitle


@pytest.mark.parametrize('chunks', [(),(PrimaryChunk('whole text',5,2),),
    (PrimaryChunk('whole text',float('nan'),5),),
    (PrimaryChunk('whole',0,7),PrimaryChunk('text',5,8))])
def test_complete_text_bad_timing_survives_first_and_cache(tmp_path,chunks):
    audio=StandardAudio(tmp_path/'audio.wav',10);audio.path.write_bytes(b'audio identity')
    calls=[]
    class Recognizer:
        def recognize(self,audio):
            calls.append(1);return PrimaryRecognition.succeeded(PrimaryRecovery('whole text','en',chunks))
    first=recognize_cached(Recognizer(),audio,tmp_path)
    restarted=recognize_cached(Recognizer(),audio,tmp_path)
    assert first==restarted and first.failure is None and calls==[1]
    assert first.recovery.text=='whole text'
    assert first.recovery.timeline_status=='needs_recovery' and not first.recovery.chunks


def test_truncated_text_is_not_fixed_by_valid_timing(tmp_path):
    audio=StandardAudio(tmp_path/'audio.wav',10);audio.path.write_bytes(b'audio identity')
    recognizer=SimpleNamespace(recognize=lambda _:PrimaryRecognition.succeeded(
        PrimaryRecovery('missing end','en',(PrimaryChunk('missing end',0,10),),truncated=True)))
    assert recognize_cached(recognizer,audio,tmp_path).failure=='incomplete'
    assert not (tmp_path/'primary-recovery.json').exists()


def test_long_caption_gap_needs_actual_audio_evidence(tmp_path):
    vtt='WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nfirst\n\n00:00:12.000 --> 00:00:14.000\nlast\n'
    captured=SimpleNamespace(source_key='fixture',duration_seconds=14,metadata={
        'original_language':'en','captions':[{'source_key':'fixture','language':'en',
        'translated':False,'kind':'manual','text':vtt}]})
    def audio(name,gap):
        p=tmp_path/name
        with wave.open(str(p),'wb') as w:
            w.setnchannels(1);w.setsampwidth(2);w.setframerate(100)
            w.writeframes(b'\x01\x01'*200+gap+b'\x01\x01'*200)
        return StandardAudio(p,14,100)
    quiet=audio('quiet.wav',b'\0\0'*1000)
    sound=audio('uncovered-sound.wav',b'\x01\x01'*1000)
    accepted,lineage=select_subtitle(captured,quiet)
    assert accepted.text=='first\nlast'
    assert lineage['subtitle_baseline']['gap_verification']=='zero_pcm'
    rejected,lineage=select_subtitle(captured,sound)
    assert rejected is None
    assert lineage['caption_selection'][-1]['code']=='caption_gap_needs_audio_recognition'
    assert select_subtitle(captured)[0] is None
