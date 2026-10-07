"""Focused gates for the frozen wiki checks in candidate verification."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_candidate_under_test", ROOT / "scripts/verify_candidate.py"
)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def protocol_payload() -> dict[str, object]:
    return {
        "protocol_version": 2,
        "issue_counts": {"错误": 0, "提醒": 0, "信息": 1},
        "pending": [{
            "relative_path": "raw/外部/2026/10/R-20261001-0001.md",
            "raw_id": "R-20261001-0001",
            "identity": "第三方",
            "collected_at": "2026-10-01T10:00:00+08:00",
            "addendum_target": "",
            "adjacent_raw_ids": [],
            "byte_count": 120,
            "content_sha256": "a" * 64,
        }],
        "candidate_count": 0,
        "health": {
            "eligible": False,
            "due": False,
            "due_reason": "not_eligible",
            "last_lint_date": "",
            "lint_count": 0,
        },
    }


def display_payload() -> dict[str, object]:
    return {
        "state": "planned",
        "plan_id": "b" * 32,
        "page_count": 1,
        "raw_sha256": "c" * 64,
    }


def test_protocol_and_display_results_are_fully_validated_but_summarized() -> None:
    assert verify._validate_protocol_scan(protocol_payload()) == {
        "protocol_version": 2,
        "structure_valid": True,
    }
    assert verify._validate_display_plan(display_payload()) == {
        "state": "planned",
        "page_count": 1,
        "structure_valid": True,
    }


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(protocol_version=3),
    lambda value: value["issue_counts"].update(错误=1),
    lambda value: value["pending"][0].update(content_sha256="not-a-digest"),
    lambda value: value["health"].update(due=True),
])
def test_bad_protocol_results_fail_with_one_fixed_code(mutate) -> None:
    payload = protocol_payload()
    mutate(payload)
    with pytest.raises(verify.CandidateVerificationError) as caught:
        verify._validate_protocol_scan(payload)
    assert str(caught.value) == "wiki_protocol_result_invalid"


@pytest.mark.parametrize("change", [
    {"state": "failed"},
    {"plan_id": "private material"},
    {"page_count": 0},
    {"raw_sha256": "private material"},
])
def test_bad_display_results_fail_without_echoing_values(change) -> None:
    payload = display_payload()
    payload.update(change)
    with pytest.raises(verify.CandidateVerificationError) as caught:
        verify._validate_display_plan(payload)
    assert str(caught.value) == "wiki_display_result_invalid"
    assert "private material" not in str(caught.value)


def test_frozen_helpers_use_only_explicit_synthetic_vault_and_safe_summary(
    tmp_path, monkeypatch,
) -> None:
    commands: list[list[str]] = []
    outputs = [protocol_payload(), display_payload()]

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["cwd"] == tmp_path
        assert kwargs["env"]["HOME"] == str(tmp_path / "wiki-helper-home")
        return SimpleNamespace(returncode=0, stdout=json.dumps(outputs.pop(0)), stderr="")

    monkeypatch.setattr(verify.subprocess, "run", run)
    vault, summary = verify._verify_frozen_wiki_helpers(
        Path("/candidate/KnowledgeDistiller"), tmp_path, {"PATH": "/usr/bin"}
    )

    assert vault == tmp_path / "synthetic-vault"
    assert commands == [
        ["/candidate/KnowledgeDistiller", "--wiki-kit", "kb", "--vault-root",
         str(vault), "--", "protocol-scan"],
        ["/candidate/KnowledgeDistiller", "--wiki-kit", "display", "--vault-root",
         str(vault), "--", "plan", "--journal-root",
         str(tmp_path / "wiki-display-journal")],
    ]
    assert summary == {
        "synthetic_vault": True,
        "protocol_scan": {"protocol_version": 2, "structure_valid": True},
        "display_plan": {"state": "planned", "page_count": 1, "structure_valid": True},
        "app_database_started": False,
    }
    assert not (tmp_path / "data/knowledge.sqlite3").exists()


def test_mac_candidate_home_and_codex_auth_are_isolated(tmp_path) -> None:
    environment = {
        "HOME": "/Users/private",
        "CODEX_HOME": "/Users/private/.codex",
        "CODEX_API_KEY": "private",
        "CODEX_SESSION_TOKEN": "private",
        "OPENAI_API_KEY": "private",
        "OPENAI_API_TOKEN": "private",
        "LANG": "C.UTF-8",
    }
    verify._isolate_mac_home(environment, tmp_path)
    assert environment == {
        "HOME": str(tmp_path / "candidate-home"),
        "LANG": "C.UTF-8",
    }
    assert (tmp_path / "candidate-home").stat().st_mode & 0o777 == 0o700


def test_malformed_helper_result_is_a_fixed_gate_and_never_echoes_output(
    tmp_path, monkeypatch,
) -> None:
    secret = "SYNTHETIC_SECRET_MUST_NOT_ESCAPE"

    def run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"body": secret}), stderr=secret)

    monkeypatch.setattr(verify.subprocess, "run", run)
    with pytest.raises(verify.CandidateVerificationError) as caught:
        verify._verify_frozen_wiki_helpers(
            Path("/candidate/KnowledgeDistiller"), tmp_path, {"PATH": "/usr/bin"}
        )
    assert str(caught.value) == "wiki_protocol_result_invalid"
    assert secret not in str(caught.value)
    assert verify._fixed_error(RuntimeError(secret)) == "candidate_verification_failed"


@pytest.mark.parametrize("relative", [
    "knowledge.sqlite3",
    "unexpected/nested/.desktop-instance.json",
])
def test_helper_gate_rejects_app_start_anywhere_in_output(
    tmp_path, monkeypatch, relative,
) -> None:
    outputs = [protocol_payload(), display_payload()]

    def run(*_args, **_kwargs):
        forbidden = tmp_path / relative
        forbidden.parent.mkdir(parents=True, exist_ok=True)
        forbidden.write_bytes(b"unexpected app startup")
        return SimpleNamespace(returncode=0, stdout=json.dumps(outputs.pop(0)), stderr="")

    monkeypatch.setattr(verify.subprocess, "run", run)
    with pytest.raises(verify.CandidateVerificationError) as caught:
        verify._verify_frozen_wiki_helpers(
            Path("/candidate/KnowledgeDistiller"), tmp_path, {"PATH": "/usr/bin"}
        )
    assert str(caught.value) == "wiki_helper_started_app"


def test_home_gate_requires_submit_form_inside_real_v3_status_section() -> None:
    valid = b'''<section class="section organization-section" data-sync-key="organization">
      <form id="wiki-submit" method="post" action="/organization"><button>go</button></form>
    </section>'''
    assert verify._verify_wiki_workflow_entry(valid) is True

    for invalid in (
        valid.replace(b'action="/organization"', b'action="/other"'),
        valid.replace(b'data-sync-key="organization"', b'data-sync-key="other"'),
        b'<form id="wiki-submit" method="post" action="/organization"></form>',
    ):
        with pytest.raises(verify.CandidateVerificationError) as caught:
            verify._verify_wiki_workflow_entry(invalid)
        assert str(caught.value) == "wiki_workflow_entry_missing"


class CandidateProcess:
    def __init__(self, waits):
        self.pid = 1234
        self.waits = iter(waits)
        self.returncode = None
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1

    def wait(self, timeout):
        result = next(self.waits)
        if isinstance(result, BaseException):
            raise result
        self.returncode = result
        return result


def test_candidate_exit_timeout_is_fixed_and_cleanup_preserves_original_error(tmp_path) -> None:
    state = tmp_path / ".desktop-instance.json"
    state.write_text(json.dumps({"pid": 1234, "port": 57740}), encoding="utf-8")
    process = CandidateProcess([
        subprocess.TimeoutExpired("candidate", 35),
        subprocess.TimeoutExpired("candidate", 15),
        -9,
    ])

    with pytest.raises(verify.CandidateVerificationError) as caught:
        verify._wait_for_candidate_exit(process, state)
    original = caught.value
    assert str(original) == "candidate_exit_timeout"
    assert verify._cleanup_candidate(process, state)
    assert process.terminated == 2
    assert process.killed == 1
    assert not state.exists()
    assert str(original) == "candidate_exit_timeout"


def test_candidate_state_must_belong_to_launched_process(tmp_path) -> None:
    state = tmp_path / ".desktop-instance.json"
    state.write_text(json.dumps({"pid": 9999, "port": 57740}), encoding="utf-8")
    process = CandidateProcess([])

    with pytest.raises(verify.CandidateVerificationError) as caught:
        verify._wait_for_candidate_ready(process, state, timeout=0)
    assert str(caught.value) == "candidate_state_invalid"


def test_windows_candidate_accepts_positive_child_pid(tmp_path) -> None:
    state = tmp_path / ".desktop-instance.json"
    state.write_text(json.dumps({"pid": 9999, "port": 57740}), encoding="utf-8")
    process = CandidateProcess([])

    assert verify._wait_for_candidate_ready(
        process, state, timeout=0, require_process_pid=False
    ) == {"pid": 9999, "port": 57740}


def test_cleanup_failure_is_reported_without_replacing_stage_error() -> None:
    report = {}
    original = verify.CandidateVerificationError("candidate_page_failed")

    with pytest.raises(verify.CandidateVerificationError) as caught:
        verify._finish_candidate_cleanup(original, False, report)
    assert caught.value is original
    assert report == {"cleanup_error": "candidate_cleanup_failed"}
