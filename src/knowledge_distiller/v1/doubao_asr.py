"""Seed ASR 2.0 primary recognition, with task-local durable recovery."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Callable
import uuid

from knowledge_distiller.primary import (
    PrimaryChunk, PrimaryFailure, PrimaryRecognition, PrimaryRecovery, StandardAudio,
)
from knowledge_distiller.secondary import SEED_QUERY_URL, SEED_SUBMIT_URL, seed_headers


SEED_REQUEST_OPTIONS = {"model_name": "bigmodel", "enable_auto_lang": True,
                        "enable_itn": False, "enable_punc": False,
                        "enable_ddc": False, "show_utterances": True}


class _Unavailable(Exception):
    pass


class _Failed(Exception):
    pass


class DoubaoRecognizer:
    def __init__(self, api_key: Callable[[], str], access_key: Callable[[], str],
                 secret_key: Callable[[], str], region: str, bucket: str,
                 mark_unavailable: Callable[[], None] = lambda: None, *,
                 runtime_root: Path | None = None, http=None, tos_factory=None,
                 report_unavailable: Callable[[str], None] | None = None,
                 poll_interval_seconds: float = 2, timeout_seconds: float = 600):
        self.api_key, self.access_key, self.secret_key = api_key, access_key, secret_key
        self.region, self.bucket = region, bucket
        self.mark_unavailable = mark_unavailable
        self.report_unavailable = report_unavailable
        self.runtime_root = runtime_root
        self.http, self.tos_factory = http, tos_factory
        self.poll_interval_seconds, self.timeout_seconds = poll_interval_seconds, timeout_seconds

    @property
    def cache_identity(self):
        from knowledge_distiller import secondary
        return {'provider': 'doubao', 'resource_id': secondary.SEED_RESOURCE_ID,
                'submit_url': SEED_SUBMIT_URL, 'query_url': SEED_QUERY_URL,
                'request': dict(SEED_REQUEST_OPTIONS)}

    def recognize(self, audio: StandardAudio) -> PrimaryRecognition:
        state = None
        client = None
        try:
            with audio.path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            root = self.runtime_root or audio.path.parent
            # Include the audio identity: replacing an input must not reuse its old result.
            path = root / ("seed-asr-" + digest + ".json")
            if path.exists():
                state = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or state.get("sha256") != digest:
                    raise _Failed
            else:
                state = {"sha256": digest, "region": self.region, "bucket": self.bucket,
                         "object_key": "knowledge-distiller/asr/" + uuid.uuid4().hex + ".wav",
                         "task_id": str(uuid.uuid4()), "phase": "uploading", "cleaned": False,
                         "request_identity": self.cache_identity}
                self._save(path, state)

            # Only a completed record has discharged its remote cleanup obligation.
            if state.get("cleaned") and state.get("request_identity") != self.cache_identity:
                path.unlink()
                return self.recognize(audio)
            if not state.get("cleaned"):
                identity = state.get("request_identity", {
                    'provider': 'doubao', 'resource_id': 'volc.seedasr.auc',
                    'submit_url': 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit',
                    'query_url': 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/query',
                    'request': {'model_name': 'bigmodel', 'enable_auto_lang': True,
                                'enable_itn': False, 'enable_punc': False,
                                'enable_ddc': False, 'show_utterances': True}})
                if identity != self.cache_identity:
                    raise _Failed("asr_configuration_changed")
                if "request_identity" not in state:
                    # Prior releases used this exact fixed request; retain its pending task.
                    state["request_identity"] = identity
                    self._save(path, state)
            # A matching completed result requires neither credentials nor network.
            if "result" in state and state.get("cleaned"):
                return self._translate(state["result"], audio)
            if state["region"] != self.region or state["bucket"] != self.bucket:
                # Never use a newly chosen destination to abandon an old owned object/task.
                # Restoring the prior TOS configuration makes this record resumable.
                raise _Failed("asr_configuration_changed")
            try:
                api, ak, sk = self.api_key(), self.access_key(), self.secret_key()
            except Exception as error:
                # Keychain retrieval failures are configuration failures, not successful empty keys.
                raise _Unavailable from error
            if not all((api, ak, sk, self.region, self.bucket)):
                raise _Unavailable
            if self.http is None:
                import httpx
                self.http = httpx
            if self.tos_factory is None:
                import tos
                self.tos_factory = tos.TosClientV2
            client = self.tos_factory(ak, sk, "https://tos-" + state["region"] + ".volces.com",
                                      state["region"])
            if "result" in state:
                self._cleanup(client, state, path)
                return self._translate(state["result"], audio)
            if state["phase"] == "failed":
                self._cleanup(client, state, path)
                # This is an explicit new recognize/retry after a definite terminal failure.
                path.unlink()
                return self.recognize(audio)
            if state["phase"] == "uploading":
                # Head first also recovers a put whose response was lost or process interrupted.
                try:
                    uploaded = client.head_object(state["bucket"], state["object_key"])
                except Exception as error:
                    if getattr(error, "status_code", None) != 404:
                        raise
                    with audio.path.open("rb") as source:
                        uploaded = client.put_object(state["bucket"], state["object_key"],
                            content=source, content_length=audio.path.stat().st_size,
                            content_type="audio/wav")
                state["version_id"] = getattr(uploaded, "version_id", None)
                state["phase"] = "uploaded"
                self._save(path, state)
            if state["phase"] == "uploaded":
                # SDK uses HttpMethodType, not a bare HTTP verb string.
                from tos import HttpMethodType
                signed = client.pre_signed_url(HttpMethodType.Http_Method_Get,
                    state["bucket"], state["object_key"], expires=86400,
                    query={"versionId": state["version_id"]} if state.get("version_id") else {}).signed_url
                state["phase"] = "submitted"
                self._save(path, state)  # Persist ID before a possibly ambiguous submit.
                response = self.http.post(SEED_SUBMIT_URL,
                    headers=seed_headers(api, state["task_id"], submit=True),
                    json={"user": {"uid": "knowledge-distiller"},
                          "audio": {"url": signed, "format": "wav", "codec": "raw",
                                    "rate": 16000, "bits": 16, "channel": 1},
                          "request": dict(SEED_REQUEST_OPTIONS)}, timeout=30)
                try:
                    self._status(response)
                except (_Unavailable, _Failed):
                    if response.status_code < 500:
                        state["phase"] = "failed"
                        self._save(path, state)
                    raise
                try:
                    body = response.json()
                except ValueError:
                    # Legacy v3 submit acknowledges in headers with no JSON result.
                    body = {}
                if isinstance(body, dict) and body.get("task_id"):
                    state["task_id"] = str(body["task_id"])
                    self._save(path, state)
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                response = self.http.post(SEED_QUERY_URL,
                    headers=seed_headers(api, state["task_id"], submit=False), json={}, timeout=30)
                if response.status_code >= 400:
                    self._status(response)
                code = response.headers.get("X-Api-Status-Code")
                if code in {"20000001", "20000002"}:
                    time.sleep(self.poll_interval_seconds)
                    continue
                try:
                    self._status(response)
                except (_Unavailable, _Failed):
                    # Network/5xx responses remain resumable; provider terminal status is final.
                    if response.status_code < 500 and code:
                        state["phase"] = "failed"
                        self._save(path, state)
                    raise
                state["result"] = response.json()
                state["phase"] = "done"
                self._save(path, state)
                self._cleanup(client, state, path)
                return self._translate(state["result"], audio)
            raise _Failed
        except (ImportError, _Unavailable) as error:
            self._mark_unavailable("doubao_runtime_unavailable" if isinstance(error, ImportError)
                                   else str(error) or "doubao_credentials_unavailable")
            return PrimaryRecognition.failed(PrimaryFailure.RUNTIME_UNAVAILABLE)
        except Exception as error:
            if state is not None:
                state["last_error"] = str(error) if isinstance(error, _Failed) and str(error) else type(error).__name__
                self._save(path, state)
            if getattr(error, "status_code", None) in (401, 403):
                headers = getattr(error, "header", {}) or {}
                invalid_key = (getattr(error, "code", "") == "InvalidAccessKeyId"
                               or headers.get("x-tos-ec") == "0002-00000020")
                self._mark_unavailable("doubao_tos_invalid_key" if invalid_key else "doubao_tos_access_denied")
                return PrimaryRecognition.failed(PrimaryFailure.RUNTIME_UNAVAILABLE)
            return PrimaryRecognition.failed(PrimaryFailure.RUNTIME_FAILED)
        finally:
            if client is not None and state is not None and state.get("phase") == "failed":
                try:
                    self._cleanup(client, state, path)
                except Exception:
                    # _cleanup persisted the exact object for the next user retry.
                    pass

    def _mark_unavailable(self, reason):
        if self.report_unavailable is not None:
            self.report_unavailable(reason)
        else:
            self.mark_unavailable()

    @staticmethod
    def _status(response):
        if response.status_code in (401, 403):
            raise _Unavailable("doubao_api_unavailable")
        if response.status_code >= 400 or response.headers.get("X-Api-Status-Code") != "20000000":
            raise _Failed

    @staticmethod
    def _save(path, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)

    def _cleanup(self, client, state, path):
        if state.get("cleaned"):
            return
        try:
            client.delete_object(state["bucket"], state["object_key"],
                                 version_id=state.get("version_id"), skip_trash=True)
        except Exception:
            state["cleanup_pending"] = True
            self._save(path, state)
            raise
        state["cleaned"] = True
        state.pop("cleanup_pending", None)
        self._save(path, state)

    @staticmethod
    def _translate(body, audio):
        try:
            result = body["result"]
            text, utterances = result["text"], result["utterances"]
            if not isinstance(text, str) or not text.strip():
                return PrimaryRecognition.failed(PrimaryFailure.EMPTY_OUTPUT)
            if not isinstance(utterances, list) or not utterances:
                raise ValueError
            chunks = []
            for entry in utterances:
                if isinstance(entry["start_time"], bool) or isinstance(entry["end_time"], bool):
                    raise ValueError
                start, end = float(entry["start_time"]) / 1000, float(entry["end_time"]) / 1000
                phrase = entry["text"]
                if (not all(map(math.isfinite, (start, end))) or start < 0 or end <= start
                        or end > audio.duration_seconds + .25 or not isinstance(phrase, str)
                        or not phrase.strip() or (chunks and start < chunks[-1].start_seconds)):
                    raise ValueError
                chunks.append(PrimaryChunk(phrase, start, end))
            return PrimaryRecognition.succeeded(PrimaryRecovery(text, None, tuple(chunks)))
        except (KeyError, TypeError, ValueError):
            return PrimaryRecognition.failed(PrimaryFailure.INCOMPLETE)
