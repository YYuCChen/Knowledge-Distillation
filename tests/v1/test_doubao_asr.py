import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from knowledge_distiller.primary import PrimaryFailure, StandardAudio
from knowledge_distiller.secondary import SEED_QUERY_URL, SEED_SUBMIT_URL
from knowledge_distiller.v1.doubao_asr import DoubaoRecognizer


class TosError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


class Tos:
    def __init__(self):
        self.uploads = self.deletes = 0
        self.exists = False
        self.delete_error = False
        self.put_error = False
        self.calls = []

    def head_object(self, bucket, key):
        if not self.exists:
            raise TosError(404)
        return SimpleNamespace(version_id="v1")

    def put_object(self, bucket, key, **kwargs):
        self.uploads += 1
        self.exists = True
        assert kwargs["content"].read() == b"owned-audio"
        if self.put_error:
            self.put_error = False
            raise TimeoutError
        return SimpleNamespace(version_id="v1")

    def pre_signed_url(self, method, bucket, key, **kwargs):
        assert kwargs["query"] == {"versionId": "v1"}
        return SimpleNamespace(signed_url="https://private-url?credential=never-persist")

    def delete_object(self, bucket, key, **kwargs):
        self.calls.append((bucket, key, kwargs))
        if self.delete_error:
            raise TimeoutError
        self.deletes += 1
        self.exists = False


def response(code="20000000", body=None, status=200):
    return SimpleNamespace(status_code=status, headers={"X-Api-Status-Code": code},
                           json=lambda: body if body is not None else {})


RESULT = {"result": {"text": "真实音频文本", "utterances": [
    {"text": "真实音频文本", "start_time": 500, "end_time": 2000}]}}


class Http:
    def __init__(self, queries=None, submit=None):
        self.calls = []
        self.queries = queries or [response(body=RESULT)]
        self.submit = submit or response(body={"task_id": "remote-task"})

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.submit if url == SEED_SUBMIT_URL else self.queries.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "tos", SimpleNamespace(
        HttpMethodType=SimpleNamespace(Http_Method_Get="GET")))
    path = tmp_path / "normalized.wav"
    path.write_bytes(b"owned-audio")
    audio = StandardAudio(path, 3, 16000, 1, 2)
    tos, http, unavailable = Tos(), Http(), []
    recognizer = DoubaoRecognizer(lambda: "API_SECRET", lambda: "AK_SECRET", lambda: "SK_SECRET",
        "cn-beijing", "owned-bucket", lambda: unavailable.append(True),
        http=http, tos_factory=lambda *args: tos, poll_interval_seconds=0)
    return recognizer, audio, tos, http, unavailable


def state(audio):
    return json.loads(next(audio.path.parent.glob("seed-asr-*.json")).read_text())


def test_success_preserves_timing_cleans_exact_version_and_caches(setup):
    recognizer, audio, tos, http, unavailable = setup
    result = recognizer.recognize(audio)
    assert result.recovery.text == "真实音频文本"
    assert result.recovery.chunks[0].start_seconds == .5
    assert result.recovery.chunks[0].end_seconds == 2
    assert tos.uploads == tos.deletes == 1
    assert tos.calls[0][2] == {"version_id": "v1", "skip_trash": True}
    submit = http.calls[0][1]
    assert submit["json"]["request"]["enable_auto_lang"] is True
    assert submit["headers"]["X-Api-Key"] == "API_SECRET"
    assert http.calls[1][1]["headers"]["X-Api-Request-Id"] == "remote-task"
    saved = json.dumps(state(audio))
    assert all(secret not in saved for secret in ["API_SECRET", "AK_SECRET", "SK_SECRET", "private-url"])
    assert recognizer.recognize(audio) == result
    assert len(http.calls) == 2
    assert unavailable == []


def test_pending_then_success(setup):
    recognizer, audio, tos, http, _ = setup
    http.queries = [response("20000002"), response("20000001"), response(body=RESULT)]
    assert recognizer.recognize(audio).recovery
    assert len(http.calls) == 4


def test_network_failure_resumes_existing_task_without_resubmit(setup):
    recognizer, audio, tos, http, unavailable = setup
    http.queries = [TimeoutError(), response(body=RESULT)]
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_FAILED
    assert state(audio)["phase"] == "submitted"
    assert tos.deletes == 0
    restarted = DoubaoRecognizer(recognizer.api_key, recognizer.access_key, recognizer.secret_key,
        recognizer.region, recognizer.bucket, recognizer.mark_unavailable,
        http=http, tos_factory=lambda *args: tos, poll_interval_seconds=0)
    assert restarted.recognize(audio).recovery
    assert sum(url == SEED_SUBMIT_URL for url, _ in http.calls) == 1
    assert tos.uploads == 1 and unavailable == []


def test_ambiguous_submit_only_queries_persisted_request_id(setup):
    recognizer, audio, tos, http, _ = setup
    http.submit = TimeoutError()
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_FAILED
    submitted_id = state(audio)["task_id"]
    assert recognizer.recognize(audio).recovery
    assert http.calls[-1][1]["headers"]["X-Api-Request-Id"] == submitted_id
    assert sum(url == SEED_SUBMIT_URL for url, _ in http.calls) == 1


def test_lost_upload_response_recovers_head_without_reupload(setup):
    recognizer, audio, tos, http, _ = setup
    tos.put_error = True
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_FAILED
    assert recognizer.recognize(audio).recovery
    assert tos.uploads == 1


def test_cleanup_failure_is_durable_and_retries_without_asr(setup):
    recognizer, audio, tos, http, _ = setup
    tos.delete_error = True
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_FAILED
    assert state(audio)["cleanup_pending"] is True
    assert "result" in state(audio)
    tos.delete_error = False
    assert recognizer.recognize(audio).recovery
    assert len(http.calls) == 2 and state(audio)["cleaned"]


def test_auth_failure_marks_unavailable_and_cleans(setup):
    recognizer, audio, tos, http, unavailable = setup
    http.submit = response("45000001", status=403)
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_UNAVAILABLE
    assert unavailable == [True] and tos.deletes == 1


def test_missing_secret_makes_no_network_request(setup):
    recognizer, audio, tos, http, unavailable = setup
    recognizer.api_key = lambda: ""
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_UNAVAILABLE
    assert http.calls == [] and tos.uploads == 0


def test_invalid_timestamp_never_becomes_success(setup):
    recognizer, audio, tos, http, _ = setup
    http.queries = [response(body={"result": {"text": "x", "utterances": [
        {"text": "x", "start_time": 0, "end_time": 4000}]}})]
    assert recognizer.recognize(audio).failure == PrimaryFailure.INCOMPLETE
    assert tos.deletes == 1


def test_timeout_is_resumable_and_does_not_mark_configuration_unavailable(setup):
    recognizer, audio, tos, http, unavailable = setup
    recognizer.timeout_seconds = 0
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_FAILED
    recognizer.timeout_seconds = 600
    assert recognizer.recognize(audio).recovery
    assert unavailable == [] and tos.uploads == 1


def test_submit_server_error_is_ambiguous_and_does_not_resubmit(setup):
    recognizer, audio, tos, http, unavailable = setup
    http.submit = response(status=503)
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_FAILED
    assert state(audio)["phase"] == "submitted"
    assert recognizer.recognize(audio).recovery
    assert sum(url == SEED_SUBMIT_URL for url, _ in http.calls) == 1
    assert unavailable == []


def test_provider_terminal_failure_allows_explicit_retry(setup):
    recognizer, audio, tos, http, unavailable = setup
    http.queries = [response("45000151"), response(body=RESULT)]
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_FAILED
    assert state(audio)["phase"] == "failed" and state(audio)["cleaned"]
    assert recognizer.recognize(audio).recovery
    assert tos.uploads == 2 and unavailable == []


def test_changed_destination_preserves_old_task_and_requires_restoring_config(setup):
    recognizer, audio, tos, http, unavailable = setup
    http.queries = [TimeoutError(), response(body=RESULT)]
    assert recognizer.recognize(audio).failure
    recognizer.bucket = "other-bucket"
    before = len(http.calls)
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_FAILED
    assert len(http.calls) == before and tos.uploads == 1
    assert state(audio)["last_error"] == "asr_configuration_changed"
    recognizer.bucket = "owned-bucket"
    assert recognizer.recognize(audio).recovery
    assert unavailable == []


def test_http_failure_with_pending_header_is_not_polled(setup):
    recognizer, audio, tos, http, unavailable = setup
    http.queries = [response("20000001", status=503)]
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_FAILED
    assert len(http.calls) == 2
    assert state(audio)["phase"] == "submitted" and unavailable == []


@pytest.mark.parametrize("utterances", [
    [{"text": "x", "start_time": True, "end_time": 2000}],
    [{"text": "x", "start_time": 1000, "end_time": 2000},
     {"text": "x", "start_time": 500, "end_time": 900}],
])
def test_boolean_or_backward_timing_is_rejected(setup, utterances):
    recognizer, audio, tos, http, _ = setup
    http.queries = [response(body={"result": {"text": "x", "utterances": utterances}})]
    assert recognizer.recognize(audio).failure == PrimaryFailure.INCOMPLETE


def test_legacy_submit_without_task_id_uses_client_request_id(setup):
    recognizer, audio, tos, http, _ = setup
    http.submit = response(body={})
    assert recognizer.recognize(audio).recovery
    assert http.calls[0][1]["headers"]["X-Api-Request-Id"] == http.calls[1][1]["headers"]["X-Api-Request-Id"]


def test_submit_success_header_without_json_body_is_accepted(setup):
    recognizer, audio, tos, http, _ = setup
    def no_body():
        raise ValueError("empty JSON body")
    http.submit = response()
    http.submit.json = no_body
    assert recognizer.recognize(audio).recovery


@pytest.mark.parametrize('ec,reason', [('0002-00000020','doubao_tos_invalid_key'),('0003-00000012','doubao_tos_access_denied')])
def test_tos_failure_reports_safe_reason_before_asr_submit(setup, ec, reason):
    recognizer, audio, tos, http, _ = setup
    reasons = []
    recognizer.report_unavailable = reasons.append
    def denied(*args):
        error = TosError(403)
        error.header = {'x-tos-ec':ec}
        raise error
    tos.head_object = denied
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_UNAVAILABLE
    assert reasons == [reason]
    assert not http.calls and tos.uploads == 0


def test_doubao_api_auth_failure_is_distinct_from_tos(setup):
    recognizer, audio, tos, http, _ = setup
    reasons=[]
    recognizer.report_unavailable = reasons.append
    http.submit = response(status=403)
    assert recognizer.recognize(audio).failure == PrimaryFailure.RUNTIME_UNAVAILABLE
    assert reasons == ['doubao_api_unavailable']


def test_cleaned_cache_with_changed_request_is_recognized_again(setup,monkeypatch):
    from knowledge_distiller.v1 import doubao_asr as module
    recognizer,audio,tos,http,_=setup
    assert recognizer.recognize(audio).recovery
    monkeypatch.setitem(module.SEED_REQUEST_OPTIONS,'enable_auto_lang',False)
    http.queries=[response(body=RESULT)]
    assert recognizer.recognize(audio).recovery
    assert tos.uploads==2 and len(http.calls)==4
    assert state(audio)['request_identity']==recognizer.cache_identity
    assert http.calls[2][1]['json']['request']['enable_auto_lang'] is False


def test_changed_request_never_discards_pending_cleanup(setup,monkeypatch):
    from knowledge_distiller.v1 import doubao_asr as module
    recognizer,audio,tos,http,_=setup
    tos.delete_error=True
    assert recognizer.recognize(audio).failure
    pending=state(audio)
    monkeypatch.setitem(module.SEED_REQUEST_OPTIONS,'enable_auto_lang',False)
    calls=len(http.calls)
    assert recognizer.recognize(audio).failure
    assert state(audio)['object_key']==pending['object_key']
    assert state(audio)['cleanup_pending'] is True
    assert state(audio)['last_error']=='asr_configuration_changed'
    assert len(http.calls)==calls and tos.uploads==1
    monkeypatch.setitem(module.SEED_REQUEST_OPTIONS,'enable_auto_lang',True)
    tos.delete_error=False
    assert recognizer.recognize(audio).recovery
    assert tos.deletes==1 and tos.uploads==1


@pytest.mark.parametrize('cleaned',[False,True])
def test_legacy_state_without_identity_keeps_pending_task_but_rechecks_cleaned_result(setup,cleaned):
    recognizer,audio,tos,http,_=setup
    if not cleaned:tos.delete_error=True
    recognizer.recognize(audio)
    path=next(audio.path.parent.glob('seed-asr-*.json'))
    saved=json.loads(path.read_text());saved.pop('request_identity')
    path.write_text(json.dumps(saved))
    tos.delete_error=False
    if cleaned:http.queries=[response(body=RESULT)]
    assert recognizer.recognize(audio).recovery
    assert tos.uploads == (2 if cleaned else 1)
    assert state(audio)['request_identity']==recognizer.cache_identity
