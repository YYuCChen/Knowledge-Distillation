"""Synthetic real files/FDs only; no CLI/model/Popen or proof qualification.

mark_spawned(os.getpid()) simulates the integration lifecycle using this test
process's PID. It does not prove a child exec/model was actually launched.
"""
import json
import os
import pytest

from knowledge_distiller.v1.wiki_exec_recording import (
    ExecRecordingV1, RecordingError, STDOUT_LIMIT, STDERR_LIMIT,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="anchored POSIX FD contract")
PROMPT = "完整合成自述\r\né 😀\n".encode()
SCHEMA = b'{"type":"object","properties":{}}'
ARGV = ("synthetic-exec-not-run", "--json", "-c", "LOCAL-ONLY-SYNTHETIC-CAPABILITY", "-")


@pytest.fixture
def roots(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    result = {}
    for name in ("evidence", "workspace", "runtime"):
        p = root / name
        p.mkdir(mode=0o700)
        p.chmod(0o700)
        result[name] = p
    return result


def recorder(roots, **kwargs):
    return ExecRecordingV1(roots["evidence"], workspace_root=roots["workspace"],
                           runtime_root=roots["runtime"], **kwargs)


def begin(session, **changes):
    values = dict(argv=ARGV, stdin_bytes=PROMPT, schema_bytes=SCHEMA, timeout_seconds=900)
    values.update(changes)
    return session.begin(**values)


def terminal(call):
    return json.loads((call.path / "terminal.json").read_bytes())


def completed(call):
    call.mark_spawned(os.getpid())
    call.progress(stdin_written=len(PROMPT), stdout_eof=True, stderr_eof=True)
    return call.finish(returncode=0, usage={"input_tokens": 9, "output_tokens": 2})


def test_exact_raw_bytes_private_inputs_and_distinct_spawn_counts(roots):
    with recorder(roots) as session:
        call = begin(session)
        assert session.counts == {"attempts": 1, "reserved": 1, "actual_spawned": 0}
        assert (call.path / "stdin.utf8").read_bytes() == PROMPT
        assert (call.path / "schema.json").read_bytes() == SCHEMA
        assert json.loads((call.path / "argv.json").read_bytes()) == list(ARGV)
        call.mark_spawned(os.getpid())
        data = b'{"type":"item.completed"}\n\xff\x00\r\nunterminated'
        call.write("out", data[:7])
        call.write("out", data[7:])  # Bad UTF8 is retained; recorder never decodes it.
        call.write("err", b"synthetic stderr\xff")
        call.progress(stdin_written=len(PROMPT), stdout_eof=True, stderr_eof=True)
        result = call.finish(returncode=0, usage={"input_tokens": 11, "output_tokens": 3,
                                                "credential": "NEVER-KEEP"})
        assert (call.path / "stdout.jsonl.raw").read_bytes() == data
        assert result["usage"] == {"input_tokens": 11, "output_tokens": 3}
        assert "NEVER-KEEP" not in (call.path / "terminal.json").read_text()
        assert result["actual_spawned"] and result["complete_stdout_eof"]
        assert result["retained_bytes"]["out"] == len(data)
        assert session.counts == {"attempts": 1, "reserved": 1, "actual_spawned": 1}
        assert call._streams == {} and call._dir is None
        assert all((p.stat().st_mode & 0o777) == 0o600 for p in call.path.iterdir())
    assert session._fd is None


@pytest.mark.parametrize("tag,cap,filename", [("out", STDOUT_LIMIT, "stdout.jsonl.raw"),
                                            ("err", STDERR_LIMIT, "stderr.raw")])
def test_cap_crossing_retains_every_prefix_byte_and_observed_count(roots, tag, cap, filename):
    with recorder(roots) as session:
        call = begin(session)
        call.mark_spawned(os.getpid())
        block = b"a" * 65536
        for _ in range((cap - 1) // len(block)):
            call.write(tag, block)
        call.write(tag, b"b" * (cap - 1 - call.retained[tag]))
        expected = (call.path / filename).read_bytes() + b"\xff"
        with pytest.raises(RecordingError, match="^recording_output_limit$"):
            call.write(tag, b"\xffUNRETAINED")
        assert call._streams == {} and call._dir is None
        assert session._fd is None
        assert (call.path / filename).read_bytes() == expected
        with pytest.raises(RecordingError, match="^recording_output_limit$"):
            call.finish(returncode=-15, usage={}, error_code="runner_output_limit")
        result = terminal(call)
        assert result["retained_bytes"][tag] == cap
        assert result["observed_bytes"][tag] == cap + len(b"UNRETAINED")
        assert result["truncated_due_to_overflow"] and result["not_observed_tail"]
        assert result["returncode"] == -15
        with pytest.raises(RecordingError, match="^recording_stopped$"):
            begin(session)


def test_spawn_failure_is_reserved_not_spawned_and_never_retried(roots):
    with recorder(roots) as session:
        call = begin(session)
        with pytest.raises(RecordingError, match="^recording_spawn_failed$"):
            call.finish(returncode=None, usage={}, error_code="recording_spawn_failed")
        result = terminal(call)
        assert result["reserved"] and not result["actual_spawned"]
        assert result["returncode"] is None and result["stdin_written"] == 0
        assert session.counts == {"attempts": 1, "reserved": 1, "actual_spawned": 0}
        with pytest.raises(RecordingError):
            begin(session)


@pytest.mark.parametrize("flags,reason", [({}, "recording_incomplete"),
    ({"cancelled": True}, "interrupted"), ({"timed_out": True}, "runner_timeout")])
def test_partial_stdin_and_non_eof_tail_are_never_complete(roots, flags, reason):
    with recorder(roots) as session:
        call = begin(session)
        call.mark_spawned(os.getpid())
        call.write("out", b"valid prefix\xff")
        call.progress(stdin_written=2, stderr_eof=True)
        with pytest.raises(RecordingError, match=f"^{reason}$"):
            call.finish(returncode=0, usage={}, **flags)
        result = terminal(call)
        assert result["stdin_written"] == 2 and result["stdin_size"] == len(PROMPT)
        assert result["not_observed_tail"] and not result["complete_stdout_eof"]
        assert result["retained_bytes"]["out"] == len(b"valid prefix\xff")


@pytest.mark.parametrize("stage", ["before", "after"])
def test_callback_failure_is_fixed_private_no_success_or_next_attempt(roots, stage):
    calls = []
    def refuse(**values):
        calls.append(values)
        raise RuntimeError("SECRET-MODEL-MATERIAL")
    session = recorder(roots, **{f"{stage}_spawn" if stage == "before" else "after_finish": refuse})
    try:
        if stage == "before":
            with pytest.raises(RecordingError, match="^recording_callback_failed$"):
                begin(session)
            assert session.counts == {"attempts": 1, "reserved": 0, "actual_spawned": 0}
            path = roots["evidence"] / "exec-0001"
        else:
            call = begin(session)
            with pytest.raises(RecordingError, match="^recording_callback_failed$"):
                completed(call)
            path = call.path
        assert json.loads((path / "terminal.json").read_bytes())["error_code"] == "recording_callback_failed"
        assert "SECRET-MODEL-MATERIAL" not in (path / "terminal.json").read_text()
        assert len(calls) == 1
        with pytest.raises(RecordingError):
            begin(session)
    finally:
        session.close()


@pytest.mark.parametrize("unsafe", ["symlink", "permissions", "overlap"])
def test_roots_reject_symlinks_permissions_and_overlap_without_repair(roots, unsafe):
    evidence = roots["evidence"]
    if unsafe == "symlink":
        link = evidence.parent / "linked-evidence"
        link.symlink_to(evidence, target_is_directory=True)
        roots["evidence"] = link
    elif unsafe == "permissions":
        evidence.chmod(0o755)
    else:
        roots["workspace"] = evidence
    with pytest.raises(RecordingError, match="^recording_unsafe_path$"):
        recorder(roots)
    assert list(evidence.iterdir()) == []
    if unsafe == "permissions":
        assert evidence.stat().st_mode & 0o777 == 0o755


def test_preexisting_artifacts_are_not_overwritten_or_adopted(roots):
    original = roots["evidence"] / "exec-0001"
    original.mkdir(mode=0o700)
    (original / "keep").write_bytes(b"synthetic preimage")
    session = recorder(roots)
    with pytest.raises(RecordingError, match="^recording_failed$"):
        begin(session)
    assert (original / "keep").read_bytes() == b"synthetic preimage"
    assert session._fd is None


@pytest.mark.parametrize("swap", ["hardlink", "replacement"])
def test_stream_identity_or_link_drift_rejects_terminal_success(roots, swap):
    with recorder(roots) as session:
        call = begin(session)
        call.mark_spawned(os.getpid())
        call.write("out", b"original")
        path = call.path / "stdout.jsonl.raw"
        if swap == "hardlink":
            os.link(path, call.path / "second-link")
        else:
            path.rename(call.path / "old-stream")
            path.write_bytes(b"original")
            path.chmod(0o600)
        call.progress(stdin_written=len(PROMPT), stdout_eof=True, stderr_eof=True)
        with pytest.raises(RecordingError):
            call.finish(returncode=0, usage={})
        assert call._streams == {} and call._dir is None
        assert not (call.path / "terminal.json").exists()


def test_monotonic_session_deadline_and_per_call_clamp_do_not_reset(roots):
    with recorder(roots, total_timeout_seconds=120, per_exec_timeout_seconds=30) as session:
        deadline = session._deadline
        first = begin(session)
        assert 0 < first.remaining_seconds <= first.timeout_seconds == 30
        completed(first)
        second = begin(session)
        assert session._deadline == deadline and second.deadline <= deadline
        completed(second)
        session._deadline = 0  # deterministic elapsed monotonic boundary, no sleep/network
        with pytest.raises(RecordingError, match="^recording_deadline$"):
            begin(session)
        assert session.actual_spawned == 2


def test_invalid_known_usage_cannot_leak_or_claim_completion(roots):
    with recorder(roots) as session:
        call = begin(session)
        call.mark_spawned(os.getpid())
        call.progress(stdin_written=len(PROMPT), stdout_eof=True, stderr_eof=True)
        with pytest.raises(RecordingError, match="^recording_invalid_metadata$"):
            call.finish(returncode=0, usage={"input_tokens": True})
        assert call._streams == {} and call._dir is None
        assert not (call.path / "terminal.json").exists()


def test_real_broken_fd_cleanup_closes_other_streams_and_fails_closed(roots):
    with recorder(roots) as session:
        call = begin(session)
        call.mark_spawned(os.getpid())
        os.close(call._streams["out"])  # actual FD failure, no Popen/os.read monkeypatch
        with pytest.raises(RecordingError, match="^recording_failed$"):
            call.write("out", b"not written")
        assert call._streams == {} and call._dir is None
        with pytest.raises(RecordingError):
            call.finish(returncode=-15, usage={})
        result = terminal(call)
        assert result["actual_spawned"] is True and result["returncode"] == -15
        assert result["error_code"] == "recording_failed"
        assert result["observed_bytes"]["out"] == len(b"not written")
        assert result["retained_bytes"]["out"] == 0 and session._fd is None


def test_exact_cap_is_complete_without_false_overflow(roots):
    with recorder(roots) as session:
        call = begin(session)
        call.mark_spawned(os.getpid())
        block = bytes(range(256)) * 256
        for _ in range(STDOUT_LIMIT // len(block)):
            call.write("out", block)
        call.progress(stdin_written=len(PROMPT), stdout_eof=True, stderr_eof=True)
        result = call.finish(returncode=0, usage={})
        assert (call.path / "stdout.jsonl.raw").read_bytes() == block * (STDOUT_LIMIT // len(block))
        assert result["observed_bytes"]["out"] == result["retained_bytes"]["out"] == STDOUT_LIMIT
        assert not result["truncated_due_to_overflow"] and not result["not_observed_tail"]


@pytest.mark.parametrize("which", ["evidence", "workspace", "runtime"])
def test_bound_directory_replacement_rejects_original_fd_and_closes(roots, which):
    session = recorder(roots)
    original = roots[which]
    retained = original.with_name(f"retained-{which}")
    original.rename(retained)
    original.mkdir(mode=0o700)
    with pytest.raises(RecordingError, match="^recording_path_changed$"):
        begin(session)
    assert session._fd is None
    assert list(original.iterdir()) == list(retained.iterdir()) == []


def test_existing_terminal_is_not_overwritten_and_unknown_error_is_fixed(roots):
    with recorder(roots) as session:
        call = begin(session)
        sentinel = call.path / "terminal.json"
        sentinel.write_bytes(b"retained synthetic sentinel")
        sentinel.chmod(0o600)
        with pytest.raises(RecordingError, match="^recording_failed$"):
            call.finish(returncode=None, usage={}, error_code="SECRET-MATERIAL")
        assert sentinel.read_bytes() == b"retained synthetic sentinel"
        assert call._streams == {} and call._dir is None and session._fd is None

    # A separate synthetic root proves the public reason never contains input.
    second = roots["evidence"].with_name("second-evidence")
    second.mkdir(mode=0o700)
    roots["evidence"] = second
    with recorder(roots) as session:
        call = begin(session)
        with pytest.raises(RecordingError, match="^recording_external_failure$"):
            call.finish(returncode=None, usage={}, error_code="SECRET-MATERIAL")
        assert terminal(call)["error_code"] == "recording_external_failure"
        assert b"SECRET-MATERIAL" not in (call.path / "terminal.json").read_bytes()
