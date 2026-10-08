"""Private bounded exec bytes only; no subprocess, model, proof or acceptance.

Integration must mark an actual successful Popen, tee each read BEFORE decoding,
report real EOF/stdin progress, terminate on error, and finish in its finally.
An attempt/reservation is never evidence that a process was spawned.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import time

STDOUT_LIMIT = 8 * 1024 * 1024
STDERR_LIMIT = 256 * 1024
INPUT_LIMIT = 32 * 1024 * 1024
ARGV_LIMIT = 64 * 1024
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
ERRORS = frozenset({"recording_failed", "recording_unsafe_path", "recording_path_changed",
    "recording_invalid_input", "recording_invalid_state", "recording_invalid_metadata",
    "recording_callback_failed", "recording_output_limit", "recording_deadline",
    "recording_stopped", "recording_busy", "recording_spawn_failed", "recording_abandoned",
    "recording_incomplete", "recording_external_failure", "runner_output_limit",
    "runner_timeout", "interrupted", "agent_failed", "typed_output_invalid",
    "context_compaction_observed", "context_limit_observed"})


class RecordingError(RuntimeError):
    def __init__(self, code="recording_failed"):
        super().__init__(code if type(code) is str and code in ERRORS else "recording_failed")


def _require(ok, code):
    if not ok:
        raise RecordingError(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _diagnostic(value):
    """Finite private host observations, never exception text or caller material."""
    if value is None:
        return None
    _require(type(value) is dict and set(value) == {
        "original_transport_error_code", "cleanup_observation_v1"}, "recording_invalid_metadata")
    code, observed = value["original_transport_error_code"], value["cleanup_observation_v1"]
    _require(code is None or (type(code) is str and code in ERRORS), "recording_invalid_metadata")
    choices = {
        "phase": {"pump", "cancel", "finally", "not_observed"},
        "term": {"sent", "absent", "denied", "not_observed"},
        "kill": {"sent", "absent", "denied", "not_observed"},
        "probe": {"present", "absent", "denied", "not_observed"},
        "lock": {"acquired", "deadline", "not_observed"},
        "wait": {"completed", "timeout", "not_observed"},
        "failure_code": {None, "context_mismatch", "lock_deadline", "signal_denied",
                         "wait_timeout", "cleanup_exception", "prior_failure"},
    }
    _require(type(observed) is dict and set(observed) == set(choices) | {
        "leader_returncode_before", "first_result"}, "recording_invalid_metadata")
    for key, allowed in choices.items():
        item = observed[key]
        _require((item is None and key == "failure_code") or
                 (type(item) is str and item in allowed), "recording_invalid_metadata")
    leader = observed["leader_returncode_before"]
    _require(leader is None or (type(leader) is int and -(2 ** 31) <= leader < 2 ** 31),
             "recording_invalid_metadata")
    result = observed["first_result"]
    _require(result is None or type(result) is bool, "recording_invalid_metadata")
    return {"original_transport_error_code": code, "cleanup_observation_v1": dict(observed)}


def _seconds(value, maximum):
    _require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum,
             "recording_invalid_input")
    return float(value)


def _identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _owned_directory(fd):
    info = os.fstat(fd)
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o700, "recording_unsafe_path")


def _open_directory(path):
    """Traverse from '/' with no symlink component; caller supplies owned leaf."""
    path = Path(path)
    _require(path.is_absolute() and ".." not in path.parts, "recording_unsafe_path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            parent, fd = fd, child
            os.close(parent)
        _owned_directory(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _file(fd):
    info = os.fstat(fd)
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o600, "recording_unsafe_path")


def _create(directory_fd, name):
    # Names are exclusively module literals; never supplied by a model/caller.
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    try:
        os.fchmod(fd, 0o600)
        _file(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_all(fd, data, progress=None):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        _require(written > 0, "recording_failed")
        if progress is not None:
            progress(written)
        view = view[written:]


def _save(directory_fd, name, data):
    fd = _create(directory_fd, name)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


class ExecRecordingV1:
    CONTRACT = "g3-exec-recording-v1"

    def __init__(self, private_root, *, workspace_root, runtime_root,
                 total_timeout_seconds=1500, per_exec_timeout_seconds=180,
                 before_spawn=None, after_finish=None):
        self.root = Path(private_root)
        self.workspace = Path(workspace_root)
        self.runtime = Path(runtime_root)
        self._fd = None
        self._active = None
        self._stopped = False
        self.attempts = self.reserved = self.actual_spawned = 0
        self._per_call = _seconds(per_exec_timeout_seconds, 180)
        self._deadline = time.monotonic() + _seconds(total_timeout_seconds, 1500)
        _require(before_spawn is None or callable(before_spawn), "recording_invalid_input")
        _require(after_finish is None or callable(after_finish), "recording_invalid_input")
        self.before_spawn, self.after_finish = before_spawn, after_finish
        self._input_identities = {}
        try:
            # Check the two explicit input paths without loading any file bodies.
            for other in (self.workspace, self.runtime):
                fd = _open_directory(other)
                try:
                    self._input_identities[other] = _identity(fd)
                finally:
                    os.close(fd)
                _require(not (self.root == other or self.root.is_relative_to(other)
                         or other.is_relative_to(self.root)), "recording_unsafe_path")
            self._fd = _open_directory(self.root)
            self._identity = _identity(self._fd)
        except (OSError, RecordingError):
            self.close()
            raise RecordingError("recording_unsafe_path") from None

    @property
    def counts(self):
        return {"attempts": self.attempts, "reserved": self.reserved,
                "actual_spawned": self.actual_spawned}

    def _anchor(self):
        _require(self._fd is not None, "recording_stopped")
        _owned_directory(self._fd)
        current = _open_directory(self.root)
        try:
            _require(_identity(current) == self._identity, "recording_path_changed")
        finally:
            os.close(current)
        for path, identity in self._input_identities.items():
            current = _open_directory(path)
            try:
                _require(_identity(current) == identity, "recording_path_changed")
            finally:
                os.close(current)

    def _close_root(self):
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                os.close(fd)
            except OSError:
                raise RecordingError("recording_failed") from None

    def begin(self, *, argv, stdin_bytes, schema_bytes, timeout_seconds):
        _require(not self._stopped and self._fd is not None, "recording_stopped")
        _require(self._active is None, "recording_busy")
        self.attempts += 1
        call = None
        try:
            _require(self.attempts <= 8, "recording_stopped")
            requested = _seconds(timeout_seconds, 900)
            _require(type(argv) is tuple and 0 < len(argv) <= 4096
                     and all(type(a) is str and "\x00" not in a for a in argv), "recording_invalid_input")
            encoded_argv = _json(argv)
            _require(len(encoded_argv) <= ARGV_LIMIT and type(stdin_bytes) is bytes
                     and type(schema_bytes) is bytes and len(stdin_bytes) + len(schema_bytes) <= INPUT_LIMIT,
                     "recording_invalid_input")
            self._anchor()
            remaining = self._deadline - time.monotonic()
            _require(remaining > 0, "recording_deadline")
            timeout = min(requested, self._per_call, remaining)
            call = _ExecCall(self, self.attempts, stdin_bytes, schema_bytes, encoded_argv, timeout)
            self._active = call
            if self.before_spawn is not None:
                try:
                    self.before_spawn(call_id=call.call_id, argv=argv, stdin_bytes=stdin_bytes,
                                      schema_bytes=schema_bytes, timeout_seconds=timeout)
                except BaseException:
                    raise RecordingError("recording_callback_failed") from None
            _require(time.monotonic() < call.deadline, "recording_deadline")
            call.reserved = True
            self.reserved += 1
            return call
        except BaseException as error:
            self._stopped = True
            if call is not None:
                try:
                    call.finish(returncode=None, error_code=str(error), usage={})
                except RecordingError:
                    pass  # Original failure is retained; never return a usable reservation.
            self.close()
            raise RecordingError(str(error) if isinstance(error, RecordingError) else "recording_failed") from None

    def close(self):
        try:
            if self._active is not None:
                try:
                    self._active.finish(returncode=None, error_code="recording_abandoned", usage={})
                except RecordingError as error:
                    if str(error) != "recording_abandoned":
                        raise
        finally:
            self._stopped = True
            self._close_root()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class _ExecCall:
    def __init__(self, session, call_id, stdin, schema, argv, timeout):
        self.session, self.call_id = session, call_id
        self.name = f"exec-{call_id:04d}"
        self.path = session.root / self.name
        self.timeout_seconds = timeout
        self.deadline = min(session._deadline, time.monotonic() + timeout)
        self.reserved = self.actual_spawned = self.finished = False
        self.pid = None
        self.stdin_size, self.stdin_written = len(stdin), 0
        self._usage = {}
        self.eof = {"out": False, "err": False}
        self.observed = {"out": 0, "err": 0}
        self.retained = {"out": 0, "err": 0}
        self.overflow = False
        self.fault = None
        self._dir = None
        self._streams = {}
        self._stream_ids = {}
        try:
            os.mkdir(self.name, mode=0o700, dir_fd=session._fd)
            self._dir = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=session._fd)
            _owned_directory(self._dir)
            self._identity = _identity(self._dir)
            _save(self._dir, "argv.json", argv)
            _save(self._dir, "stdin.utf8", stdin)
            _save(self._dir, "schema.json", schema)
            self._streams["out"] = _create(self._dir, "stdout.jsonl.raw")
            self._streams["err"] = _create(self._dir, "stderr.raw")
            self._stream_ids = {tag: _identity(fd) for tag, fd in self._streams.items()}
            os.fsync(self._dir)
            os.fsync(session._fd)
        except BaseException:
            self._release()
            raise

    def _release(self):
        failed = False
        for fd in self._streams.values():
            try:
                os.close(fd)
            except OSError:
                failed = True
        self._streams.clear()
        if self._dir is not None:
            try:
                os.close(self._dir)
            except OSError:
                failed = True
            self._dir = None
        if failed:
            self.fault = "recording_failed"
            self.session._stopped = True
        return failed

    @property
    def remaining_seconds(self):
        return max(0.0, min(self.deadline, self.session._deadline) - time.monotonic())

    def _failed(self, code):
        self.fault = code
        self.session._stopped = True
        self._release()  # All call FDs close; finish can securely re-anchor only this inode.
        self.session._close_root()
        raise RecordingError(code)

    def mark_spawned(self, pid):
        try:
            _require(self.reserved and not self.actual_spawned and not self.finished and self.fault is None
                     and type(pid) is int and pid > 0, "recording_invalid_state")
        except RecordingError as error:
            self._failed(str(error))
        # Called immediately AFTER successful Popen; never infer it from begin().
        self.actual_spawned, self.pid = True, pid
        self.session.actual_spawned += 1

    @property
    def usage(self):
        return dict(self._usage)

    def progress(self, *, stdin_written=None, stdout_eof=False, stderr_eof=False, usage=None):
        try:
            _require(self.actual_spawned and not self.finished and self.fault is None,
                     "recording_invalid_state")
            _require(type(stdout_eof) is bool and type(stderr_eof) is bool, "recording_invalid_metadata")
            if usage is not None:
                _require(type(usage) is dict, "recording_invalid_metadata")
                counters = {}
                for key in USAGE_KEYS:
                    if key in usage:
                        _require(type(usage[key]) is int and usage[key] >= 0,
                                 "recording_invalid_metadata")
                        counters[key] = usage[key]
                self._usage = counters
            if stdin_written is not None:
                _require(type(stdin_written) is int and self.stdin_written <= stdin_written <= self.stdin_size,
                         "recording_invalid_metadata")
                self.stdin_written = stdin_written
            self.eof["out"] |= stdout_eof
            self.eof["err"] |= stderr_eof
        except RecordingError as error:
            self._failed(str(error))

    def write(self, tag, data):
        try:
            _require(self.actual_spawned and not self.finished and self.fault is None
                     and tag in ("out", "err") and type(data) is bytes and not self.eof[tag],
                     "recording_invalid_state")
            self.observed[tag] += len(data)
            limit = STDOUT_LIMIT if tag == "out" else STDERR_LIMIT
            prefix = data[:max(0, limit - self.retained[tag])]
            fd = self._streams[tag]
            _file(fd)
            def retained(size):
                self.retained[tag] += size
            _write_all(fd, prefix, retained)
            if self.observed[tag] > limit:
                self.overflow = True
                os.fsync(fd)
                self._failed("recording_output_limit")
        except (OSError, RecordingError) as error:
            if self.fault is not None:
                raise RecordingError(self.fault) from None
            self._failed(str(error) if isinstance(error, RecordingError) else "recording_failed")

    def finish(self, *, returncode, usage, error_code=None, cancelled=False, timed_out=False,
               diagnostic=None):
        _require(not self.finished, "recording_invalid_state")
        code = self.fault or (error_code if type(error_code) is str and error_code in ERRORS
                             else "recording_external_failure" if error_code is not None else None)
        terminal_fd = None
        parent_fd = None
        try:
            private_diagnostic = _diagnostic(diagnostic)
            _require(returncode is None or type(returncode) is int, "recording_invalid_metadata")
            _require(type(cancelled) is bool and type(timed_out) is bool, "recording_invalid_metadata")
            _require(type(usage) is dict, "recording_invalid_metadata")
            counters = {}
            for key in USAGE_KEYS:
                if key in usage:
                    _require(type(usage[key]) is int and usage[key] >= 0, "recording_invalid_metadata")
                    counters[key] = usage[key]
            for fd in self._streams.values():
                _file(fd)
                os.fsync(fd)
            _require(not self._release(), "recording_failed")
            # A previous stream/callback failure may already have closed all
            # FDs. Reopen only the exact original evidence root to seal failure.
            parent_fd = _open_directory(self.session.root)
            _require(_identity(parent_fd) == self.session._identity, "recording_path_changed")
            terminal_fd = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=parent_fd)
            _owned_directory(terminal_fd)
            _require(_identity(terminal_fd) == self._identity, "recording_path_changed")
            # A fault closes stream FDs immediately. Re-anchor and fsync only
            # the original fixed-name stream inodes before publishing terminal.
            for tag, name in (("out", "stdout.jsonl.raw"), ("err", "stderr.raw")):
                fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=terminal_fd)
                try:
                    _file(fd)
                    _require(_identity(fd) == self._stream_ids[tag]
                             and os.fstat(fd).st_size == self.retained[tag], "recording_path_changed")
                    os.fsync(fd)
                finally:
                    os.close(fd)
            if code is None:
                code = ("interrupted" if cancelled else "runner_timeout" if timed_out else
                        "recording_spawn_failed" if not self.actual_spawned else
                        "recording_deadline" if time.monotonic() >= self.deadline else
                        "recording_incomplete" if not all(self.eof.values()) or self.stdin_written != self.stdin_size else
                        "agent_failed" if returncode != 0 else None)
            if self.session.after_finish is not None:
                try:
                    self.session.after_finish(call_id=self.call_id, returncode=returncode,
                        usage=dict(counters), error_code=code, stdin_written=self.stdin_written,
                        stdin_size=self.stdin_size)
                except BaseException:
                    code = "recording_callback_failed"
            result = {"contract": self.session.CONTRACT, "call_id": self.call_id,
                "reserved": self.reserved, "actual_spawned": self.actual_spawned, "pid": self.pid,
                "returncode": returncode, "error_code": code, "usage": counters,
                "stdin_size": self.stdin_size, "stdin_written": self.stdin_written,
                "complete_stdout_eof": self.eof["out"], "complete_stderr_eof": self.eof["err"],
                "not_observed_tail": not all(self.eof.values()),
                "truncated_due_to_overflow": self.overflow,
                "observed_bytes": dict(self.observed), "retained_bytes": dict(self.retained),
                "timeout_seconds": self.timeout_seconds, "cancelled": cancelled, "timed_out": timed_out}
            if private_diagnostic is not None:
                result["diagnostic"] = private_diagnostic
            _save(terminal_fd, "terminal.json", _json(result))
            os.fsync(terminal_fd)
            os.fsync(parent_fd)
            if code is not None:
                self.session._stopped = True
                raise RecordingError(code)
            return result
        except (OSError, RecordingError) as error:
            self.session._stopped = True
            raise RecordingError(str(error) if isinstance(error, RecordingError) else "recording_failed") from None
        finally:
            self.finished = True
            try:
                _require(not self._release(), "recording_failed")
            finally:
                try:
                    if terminal_fd is not None:
                        os.close(terminal_fd)
                finally:
                    try:
                        if parent_fd is not None:
                            os.close(parent_fd)
                    finally:
                        self.session._active = None
                        if self.session._stopped:
                            self.session._close_root()
