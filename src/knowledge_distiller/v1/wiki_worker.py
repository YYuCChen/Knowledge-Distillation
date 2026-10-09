"""Independent coordinator for durable V3 wiki maintenance tasks."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Callable

from .wiki_lock import VaultWriteLock, WikiLockError
from .wiki_publish import (
    PublishExpectation,
    PublishState,
    WikiPublishError,
    publish_wiki,
    recover_wiki,
)
from .wiki_runner import CodexWikiRunner, RunnerResult, WikiRunnerError
from .wiki_session_broker import WikiSessionBroker
from .wiki_staging import (
    StagingSnapshot,
    ValidatedBatch,
    WikiStagingError,
    accept_validated_batch,
    cleanup_staging,
    load_staging_snapshot,
    prepare_staging,
    validate_staging,
    verify_formal_inputs,
    verify_recovered_publish,
    verify_staging_protected,
)
from .wiki_kit import WikiKitError, verify_source_kit
from .wiki_kit_runtime import WikiKitRuntimeError
from .wiki_tasks import WikiTask, WikiTaskError, WikiTaskStore, scan_workflow_protocol


@dataclass(frozen=True)
class WikiWorkResult:
    task_id: str
    error_code: str | None


class WikiWorker:
    """Run wiki tasks without occupying the source-distillation worker."""

    def __init__(self, store: WikiTaskStore, runtime_root: Path | str,
                 runner: CodexWikiRunner, *, idle_seconds: float = 0.5,
                 publish: Callable = publish_wiki, recover: Callable = recover_wiki,
                 source_store=None):
        self.store = store
        self.runtime_root = Path(runtime_root)
        self.runner = runner
        self.idle_seconds = idle_seconds
        self.publish = publish
        self.recover = recover
        self.source_store = source_store
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._activity = threading.Lock()
        self._update_reserved = False
        self._observation_guard = threading.Lock()
        self._observation_requests: dict[str, bool] = {}
        self._observation_last: dict[str, float] = {}
        self.observation_interval = 30.0

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        reset = getattr(self.runner, "reset_cancellation", None)
        if reset is not None:
            reset()
        self._stopping.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name="wiki-worker", daemon=True)
        self._thread.start()
        return True

    def wake(self) -> None:
        self._wake.set()

    def request_observation(self, vault: Path | str, *, force: bool = False) -> bool:
        value = os.fspath(vault)
        with self._observation_guard:
            last = self._observation_last.get(value, 0.0)
            if (not force and value not in self._observation_requests
                    and time.monotonic() - last < self.observation_interval):
                return False
            self._observation_requests[value] = (
                force or self._observation_requests.get(value, False))
        self.wake()
        return True

    def stop(self, timeout: float = 10) -> bool:
        self._stopping.set()
        self._wake.set()
        self.runner.cancel()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                return False
            self._thread = None
        return True

    def pending_work(self) -> bool:
        from .database import connect
        with connect(self.store.database_path) as connection:
            return connection.execute(
                """SELECT 1 FROM wiki_tasks
                   WHERE state IN ('queued','preparing','running','validating','publishing')
                      OR (state='failed' AND recovery_state IN ('required','running','failed'))
                   LIMIT 1"""
            ).fetchone() is not None

    def reserve_for_update(self) -> bool:
        if not self._activity.acquire(blocking=False):
            return False
        try:
            if self.pending_work():
                return False
            self._update_reserved = True
            return True
        finally:
            self._activity.release()

    def update_ready(self) -> bool:
        return not self._activity.locked() and not self.pending_work()

    def release_update(self) -> None:
        with self._activity:
            self._update_reserved = False
        self.wake()

    def _loop(self) -> None:
        while not self._stopping.is_set():
            result = None
            with self._activity:
                if not self._update_reserved and not self._stopping.is_set():
                    result = self.run_one()
                    if result is not None:
                        try:
                            completed = self.store.get(result.task_id)
                            self.request_observation(completed.vault_path, force=True)
                        except WikiTaskError:
                            pass
                    else:
                        self._refresh_observation()
            if result is None:
                self._wake.wait(self.idle_seconds)
                self._wake.clear()

    def _refresh_observation(self) -> bool:
        with self._observation_guard:
            if not self._observation_requests:
                return False
            vault, _force = self._observation_requests.popitem()
        error_code = None
        scan = None
        try:
            with VaultWriteLock.acquire(vault):
                scan = scan_workflow_protocol(vault, runtime=self.store.runtime)
        except BlockingIOError:
            error_code = "vault_busy"
        except WikiTaskError as error:
            code = str(error)
            error_code = code if code in {
                "protocol_error", "kit_missing", "kit_drift", "kit_incompatible",
                "raw_path_invalid", "raw_symlink", "raw_changed",
            } else "protocol_error"
        except (OSError, WikiLockError):
            error_code = "raw_path_invalid"
        try:
            self.store.record_observation(vault, scan=scan, error_code=error_code)
        except WikiTaskError:
            pass
        with self._observation_guard:
            self._observation_last[vault] = time.monotonic()
        return True

    def run_one(self) -> WikiWorkResult | None:
        skipped: set[str] = set()
        while True:
            task = self.store.claim_next_runnable(exclude_task_ids=frozenset(skipped))
            if task is None:
                return None
            try:
                lock = VaultWriteLock.acquire(task.vault_path)
                break
            except BlockingIOError:
                # Try other Vaults before sleeping; never spin on a busy head task.
                skipped.add(task.task_id)
            except (OSError, WikiLockError):
                self._safe_fail(task.task_id, None, "validation_failed",
                                recovery_phase="none")
                skipped.add(task.task_id)
        try:
            with lock:
                return self._run_locked(self.store.get(task.task_id), lock)
        except Exception:
            # Keep the worker alive without persisting arbitrary exception text.
            # A journal-bearing batch must retain its recovery gate.
            try:
                current = self.store.get(task.task_id)
                phase = ("publishing" if any(batch.state == "publishing"
                                             for batch in current.batches) else "none")
            except Exception:
                phase = "none"
            self._safe_fail(task.task_id, None, "internal_error", recovery_phase=phase)
            return WikiWorkResult(task.task_id, "internal_error")

    def _run_locked(self, task: WikiTask, lock: VaultWriteLock) -> WikiWorkResult:
        if task.outcome_contract == 'r08-wiki-outcomes-v1':
            return self._run_typed_locked(task, lock)
        recovered = self._recover_interrupted_publish(task, lock)
        if isinstance(recovered, WikiWorkResult):
            return recovered
        task = recovered
        if task.completed_batch_count == task.batch_count:
            try:
                cleanup_staging(load_staging_snapshot(
                    self.runtime_root / "wiki-tasks" / task.task_id))
            except WikiStagingError:
                pass
            return self._finalize(task)

        if task.state == "queued":
            task = self.store.set_task_state(task.task_id, "preparing")
        try:
            snapshot = prepare_staging(
                task.vault_path, self.runtime_root, task.task_id, task.raw,
                python_executable=self.store.python_executable,
                source_kit_root=self.store.kit_root, lock=lock,
                kit_runtime=self.store.runtime)
        except WikiStagingError as error:
            code = self._staging_code(str(error))
            self._safe_fail(task.task_id, None, code, recovery_phase="none")
            return WikiWorkResult(task.task_id, code)
        if task.state == "preparing":
            task = self.store.set_task_state(task.task_id, "running")

        for batch in task.batches:
            if batch.state == "succeeded":
                continue
            if batch.state != "queued":
                task = self._reset_unpublished_batch(task, batch.batch_no)
                if task.state == "queued":
                    task = self.store.set_task_state(task.task_id, "preparing")
                    task = self.store.set_task_state(task.task_id, "running")
            raw_paths = tuple(item.relative_path for item in task.raw
                              if item.batch_no == batch.batch_no)
            self.store.set_batch_state(task.task_id, batch.batch_no, "preparing")
            self.store.set_batch_state(task.task_id, batch.batch_no, "running")
            try:
                result = self.runner.run(
                    snapshot.workspace, self.runtime_root, model=task.model, effort=task.effort,
                    batch_no=batch.batch_no, raw_paths=raw_paths)
            except WikiRunnerError as error:
                code = self._runner_code(str(error))
                return self._failed(task.task_id, batch.batch_no, code, snapshot)
            if not result.succeeded:
                return self._failed(task.task_id, batch.batch_no,
                                    self._runner_code(result.error_code or "agent_failed"), snapshot)
            if not self._trusted_kb(snapshot, task):
                return self._failed(task.task_id, batch.batch_no, "validation_failed", snapshot)
            self.store.set_batch_state(task.task_id, batch.batch_no, "validating")
            try:
                validated = validate_staging(
                    snapshot, batch.batch_no, raw_paths,
                    python_executable=self.store.python_executable,
                    source_kit_root=self.store.kit_root,
                    kit_runtime=self.store.runtime)
                formal_check = verify_formal_inputs(snapshot, task.vault_path, lock=lock)
            except WikiStagingError as error:
                return self._failed(task.task_id, batch.batch_no,
                                    self._staging_code(str(error)), snapshot)

            health_performed = False
            if validated.health_due:
                if formal_check.late_raw_count == 0:
                    before_health = validated
                    try:
                        health_result = self.runner.run_health(
                            snapshot.workspace, self.runtime_root,
                            model=task.model, effort=task.effort)
                    except WikiRunnerError as error:
                        return self._failed(task.task_id, batch.batch_no,
                                            self._runner_code(str(error)), snapshot)
                    if not health_result.succeeded or not self._trusted_kb(snapshot, task):
                        code = self._runner_code(health_result.error_code or "validation_failed")
                        return self._failed(task.task_id, batch.batch_no, code, snapshot)
                    try:
                        validated = validate_staging(
                            snapshot, batch.batch_no, raw_paths,
                            python_executable=self.store.python_executable,
                            source_kit_root=self.store.kit_root,
                            kit_runtime=self.store.runtime)
                    except WikiStagingError as error:
                        return self._failed(task.task_id, batch.batch_no,
                                            self._staging_code(str(error)), snapshot)
                    if not self._health_completed(snapshot, before_health, validated):
                        return self._failed(task.task_id, batch.batch_no,
                                            "validation_failed", snapshot)
                    health_performed = True

            try:
                formal_check = verify_formal_inputs(snapshot, task.vault_path, lock=lock)
            except WikiStagingError as error:
                return self._failed(task.task_id, batch.batch_no,
                                    self._staging_code(str(error)), snapshot)
            if health_performed and formal_check.late_raw_count:
                return self._failed(task.task_id, batch.batch_no, "validation_failed", snapshot)
            result = self._publish_batch(task, snapshot, validated, lock,
                                         health_performed=health_performed)
            if isinstance(result, WikiWorkResult):
                return result
            snapshot, task = result

        cleanup_staging(snapshot)
        return self._finalize(self.store.get(task.task_id))

    def _typed_source(self, lock):
        from .wiki_source_proof import trusted_source_callback
        if self.source_store is None or Path(self.source_store.path).resolve() != self.store.database_path.resolve():
            raise WikiTaskError('validation_failed')
        return trusted_source_callback(self.source_store, lock)

    def _typed_call(self, task, snapshot, batch_no, phase, lock, source, invoke):
        """Persist phase reservation before spawn; recorder limits grant no retries."""
        import uuid
        from . import wiki_typed as t
        from .wiki_exec_recording import ExecRecordingV1
        from .wiki_staging import preserve_batch_baseline, _checkpoint_write
        root = preserve_batch_baseline(snapshot, self.runtime_root, task=task,
                                       batch_no=batch_no, lock=lock)
        from .wiki_staging import (_checkpoint_read, _checkpoint_tree,
                                   _verify_generation_terminal, REGENERABLE_GRAPH)
        from dataclasses import asdict
        generation = 'generation' in phase
        schema = None if phase == 'health' else (t.PROPOSAL_SCHEMA if generation else t.CHECK_SCHEMA)
        phase_record = root / (phase + '-result.json')
        if phase_record.exists():
            saved = _checkpoint_read(phase_record)
            record = saved['record']
            current_tree = _checkpoint_tree(snapshot)
            old = {row['relative_path']: row for row in saved['tree']}
            current = {row['relative_path']: row for row in current_tree}
            changed = {path for path in set(old) | set(current)
                       if old.get(path) != current.get(path)}
            if not changed <= REGENERABLE_GRAPH or saved['plan_sha256'] != task.plan_sha256:
                raise WikiStagingError('checkpoint_binding_changed')
            recorded = Path(record['recording_call'])
            _verify_generation_terminal(_checkpoint_read(recorded / 'terminal.json'), recorded, record,
                                        schema_definition=schema)
            final = Path(record['argv'][record['argv'].index('-o') + 1])
            content = t.read_final(final.parent, final.name)
            if t.digest(content) != saved['final_sha256']:
                raise WikiStagingError('checkpoint_binding_changed')
            return t.TypedRunnerResult(None, tuple(saved['usage']), content, saved['final_sha256'],
                                       t.InputBinding(**record['input_binding']))
        attempt_path = None
        if not generation:
            for attempt in range(1, 4):
                candidate = root / f'{phase}-reservation-{attempt}.json'
                if not candidate.exists():
                    attempt_path = candidate
                    break
            if attempt_path is None:
                raise WikiRunnerError('agent_failed')
        evidence = self.runtime_root.parent / ('wiki-exec-' + uuid.uuid4().hex)
        evidence.mkdir(mode=0o700)
        permit = []
        records = []
        prompt_context = []
        def host_context():
            from .wiki_staging import _phase_started_at
            from .wiki_support import PHASE_ACTIVITIES
            canonical_phase = ('generation' if generation else 'repair-check'
                               if re.fullmatch(r'repair-[12]-check', phase) else phase)
            scan = scan_workflow_protocol(snapshot.workspace, runtime=self.store.runtime)
            context = dict(activity_date=_phase_started_at()[:10],
                           issue_counts=dict(scan.issue_counts), candidate_count=scan.candidate_count,
                           activity=PHASE_ACTIVITIES[canonical_phase], batch_no=batch_no,
                           raw_ids=list(self._batch_raw_ids(task, batch_no)))
            prompt_context[:] = [context]
            return context
        def before_spawn(**actual):
            from .wiki_staging import _phase_started_at
            started_at = None if generation else _phase_started_at()
            if not generation and prompt_context and prompt_context[0]['activity_date'] != started_at[:10]:
                raise WikiStagingError('checkpoint_binding_changed')
            if generation:
                binding = t.admit_input(actual['stdin_bytes'], t.PROPOSAL_SCHEMA, None,
                    input_policy=t.APPLICATION_UTF8_POLICY, schema_bytes=actual['schema_bytes'])
                value = self.store.reserve_generation(task.task_id, snapshot, self.runtime_root,
                    expected_plan_sha256=task.plan_sha256, batch_no=batch_no, lock=lock,
                    source_proof=source, argv=actual['argv'], stdin_bytes=actual['stdin_bytes'],
                    schema_bytes=actual['schema_bytes'], input_binding=binding,
                    recording_call=evidence / f"exec-{actual['call_id']:04d}", timeout_seconds=actual['timeout_seconds'],
                    allow_recovery=True)
                permit.append(value)
                if prompt_context and prompt_context[0]['activity_date'] != value.reservation['started_at'][:10]:
                    raise WikiStagingError('checkpoint_binding_changed')
                value.claim_spawn()
            else:
                admission = t.admit_input(actual['stdin_bytes'], schema, None,
                    input_policy=t.APPLICATION_UTF8_POLICY, schema_bytes=actual['schema_bytes'], schema_absent=schema is None)
                record = {
                    'task_id': task.task_id, 'plan_sha256': task.plan_sha256,
                    'batch_no': batch_no, 'phase': phase,
                    'recording_call': str(evidence / f"exec-{actual['call_id']:04d}"),
                    'argv': list(actual['argv']), 'stdin_sha256': t.digest(actual['stdin_bytes']),
                    'schema_sha256': t.digest(actual['schema_bytes']), 'input_binding': asdict(admission),
                    'timeout_seconds': actual['timeout_seconds']}
                record['started_at'] = started_at
                _checkpoint_write(attempt_path, t.encoded(record))
                records.append(record)
        previous = self.runner.recording
        recording = ExecRecordingV1(evidence, workspace_root=snapshot.workspace,
            runtime_root=self.runtime_root, before_spawn=before_spawn, max_attempts=1)
        recording.host_context = host_context
        self.runner.recording = recording
        try:
            result = invoke()
            if not result.succeeded:
                raise WikiRunnerError(result.error_code or 'agent_failed')
            if generation:
                if len(permit) != 1:
                    raise WikiTaskError('validation_failed')
                self.store.save_generation_result(permit[0], result, lock=lock, source_proof=source)
            elif len(records) == 1 and (schema is None or not any(
                    r['status'] == 'unknown' or r['source_check']['status'] != 'complete'
                    or any(d['status'] == 'unknown' for d in r['dimensions'])
                    for r in t.strict_json(result.final_bytes)['reviews'])):
                _checkpoint_write(phase_record, t.encoded({'record': records[0], 'plan_sha256': task.plan_sha256,
                    'tree': _checkpoint_tree(snapshot), 'final_sha256': result.final_sha256,
                    'usage': result.usage}))
            return result
        finally:
            self.runner.recording = previous
            recording.close()

    def _proposal_feedback(self, task, snapshot, batch_no, source):
        """Recover one narrowly diagnosed failure from its immutable recording."""
        from . import wiki_typed as t
        from .wiki_staging import (_checkpoint_read, _checkpoint_write,
                                   _verify_generation_terminal)
        root = snapshot.task_root.parent.parent / 'execution' / f'batch-{batch_no}'
        number = next((n for n in (3, 2, 1) if
                       (root / ('reservation.json' if n == 1 else f'reservation-{n}.json')).exists()), None)
        if number is None:
            return None
        record = _checkpoint_read(root / ('reservation.json' if number == 1 else f'reservation-{number}.json'))
        call = Path(record['recording_call'])
        if not (call / 'terminal.json').exists():
            return None
        terminal = _checkpoint_read(call / 'terminal.json')
        # Existing recorder seals unregistered TypedError codes as external
        # failure. Never infer reparability from that code alone: require a
        # complete clean transport and reproduce this exact parser diagnosis.
        if terminal.get('error_code') not in {'typed_protocol_invalid', 'recording_external_failure'}:
            return None
        cleanup = terminal.get('diagnostic', {}).get('cleanup_observation_v1', {})
        if cleanup.get('first_result') is not True or cleanup.get('failure_code') is not None:
            return None
        try:
            _verify_generation_terminal(terminal, call, record, expected_error=terminal['error_code'])
        except WikiStagingError as error:
            if str(error) == 'generation_interrupted':
                return None
            raise
        verify_staging_protected(snapshot)
        binding, rows, _payload = t.freeze_input(task, snapshot, batch_no, source, runtime_root=self.runtime_root)
        if (record['binding'] != binding or record['task_id'] != task.task_id
                or record['plan_sha256'] != task.plan_sha256 or record['batch_no'] != batch_no
                or record['first_attempt'] != snapshot.task_root.name):
            raise WikiStagingError('checkpoint_binding_changed')
        final = snapshot.control / record['final_relative_path']
        if str(final) != record['argv'][record['argv'].index('-o') + 1]:
            raise WikiStagingError('checkpoint_binding_changed')
        content = t.read_final(final.parent, final.name)
        try:
            t.parse_proposal(content, binding, rows)
        except t.ProposalDocumentsMissing as error:
            issues = error.issues
        except t.TypedError:
            return None
        else:
            return None
        candidate = t.strict_json(content)
        # Missing documents never excuses a different document/hash violation.
        documents = {d['path']: d['sha256'] for o in candidate['outcomes'] for d in o['documents']}
        for outcome in candidate['outcomes']:
            if any(documents[d['path']] != d['sha256'] for d in outcome['documents']):
                raise WikiStagingError('checkpoint_binding_changed')
        if documents:
            t.checked_documents(snapshot, documents)
        saved = dict(candidate=content.decode('utf-8'), candidate_sha256=t.digest(content), issues=issues,
                     reservation_sha256=t.digest(t.encoded(record)), terminal_sha256=t.digest(t.encoded(terminal)))
        path = root / f'generation-feedback-{number}.json'
        if path.exists():
            if _checkpoint_read(path) != saved:
                raise WikiStagingError('checkpoint_binding_changed')
        else:
            _checkpoint_write(path, t.encoded(saved))
        for previous in range(1, number):
            old_path = root / f'generation-feedback-{previous}.json'
            if old_path.exists():
                old = _checkpoint_read(old_path)
                if t.digest(old['candidate'].encode('utf-8')) != old['candidate_sha256']:
                    raise WikiStagingError('checkpoint_binding_changed')
                if t.strict_json(old['candidate'].encode('utf-8')) == candidate and old['issues'] == issues:
                    raise WikiRunnerError('agent_failed')
        if number == 3:
            raise WikiRunnerError('agent_failed')
        return dict(candidate=saved['candidate'], issues=issues)

    def _typed_generation(self, task, snapshot, batch_no, lock, source, args):
        feedback = self._proposal_feedback(task, snapshot, batch_no, source)
        root = snapshot.task_root.parent.parent / 'execution' / f'batch-{batch_no}'
        def reservations():
            return tuple(name for name in ('reservation.json', 'reservation-2.json', 'reservation-3.json')
                         if (root / name).exists())
        while True:
            before = reservations()
            try:
                return self._typed_call(task, snapshot, batch_no, 'generation', lock, source,
                    lambda: self.runner.run_outcomes(snapshot, self.runtime_root,
                                                     generation_feedback=feedback, **args)).final_bytes
            except WikiRunnerError:
                if reservations() == before:
                    raise
                feedback = self._proposal_feedback(task, snapshot, batch_no, source)
                if feedback is None:
                    raise

    def _typed_validate(self, snapshot, task, batch_no, *, regenerate=True):
        if regenerate:
            if not self._trusted_kb(snapshot, task):
                raise WikiStagingError('validation_failed')
        else:
            # A completed stage is an immutable candidate. Running kb here
            # rewrites dated system pages before cached-check/CAS verification,
            # making an otherwise unchanged next-day recovery fail its hashes.
            verify_staging_protected(snapshot)
            try:
                manifest = verify_source_kit(self.store.kit_root)
            except WikiKitError as error:
                raise WikiStagingError('validation_failed') from error
            if manifest.manifest_sha256 != task.kit_manifest_sha256:
                raise WikiStagingError('validation_failed')
        return validate_staging(snapshot, batch_no,
            [r.relative_path for r in task.raw if r.batch_no == batch_no],
            python_executable=self.store.python_executable,
            source_kit_root=self.store.kit_root, kit_runtime=self.store.runtime)

    def _typed_candidate(self, task, snapshot, batch_no, validated, proposal, checked, source,
                         *, parent_registry=None, reservation=None, reuse_generated=False, check_phase=None):
        from . import wiki_support as support
        from . import wiki_typed as t
        from .wiki_outcomes import WikiOutcomes, Outcome, CONTRACT
        from .wiki_staging import _stable_bytes
        baseline = snapshot.task_root.parent.parent / 'execution' / f'batch-{batch_no}' / 'before'
        changes = tuple(support.DocumentChange(c.relative_path,
            None if c.before_sha256 is None else _stable_bytes(baseline / c.relative_path, changed_code='validation_failed'),
            c.before_sha256, _stable_bytes(snapshot.workspace / c.relative_path, changed_code='validation_failed'),
            c.after_sha256) for c in validated.changes
            if c.relative_path.startswith('wiki/') and c.relative_path.endswith('.md'))
        contents = {r.raw_id: _stable_bytes(snapshot.workspace / r.relative_path, changed_code='raw_changed') for r in task.raw}
        pages = tuple(support.FrozenPage(f.relative_path,
            _stable_bytes(baseline / f.relative_path, changed_code='validation_failed'), f.sha256)
            for f in snapshot.files if f.relative_path.startswith('wiki/') and f.relative_path.endswith('.md')
            and f.relative_path not in {c.path for c in changes})
        read_facts = lambda: self._program_facts(task, snapshot, batch_no)
        cached = (support.WikiSupportGate.cached_generated(
            snapshot.task_root.parent.parent / 'execution', f'batch-{batch_no}-final', snapshot.workspace,
            check_phase=check_phase)
            if reuse_generated else None)
        registry = support.build_registry(snapshot.workspace, changes,
            tuple(support.FrozenRaw(r.relative_path, r.raw_id, contents[r.raw_id], r.content_sha256) for r in task.raw),
            pages=pages, generated=(cached[0] if cached is not None else self._managed_sections(snapshot, task)),
            parent_registry=parent_registry,
            program_facts=read_facts(), program_facts_readback=read_facts,
            claim_mapping=() if parent_registry is None else tuple(support.ClaimMapping(
                c.block.claim_id, c.block.path, c.block.position) for c in parent_registry.claims))
        if cached is not None:
            _, original, binding_hash = cached
            if (registry.binding_hash != binding_hash or registry.candidate_hash != original['candidate_hash']
                    or registry.payload()['documents'] != original['documents']):
                raise support.WikiSupportError('binding_mismatch')
        client = self.runner.support_client(snapshot, self.runtime_root, task=task,
            registry=registry, model=task.model, effort=task.effort, source_proof=source,
            input_policy=t.APPLICATION_UTF8_POLICY)
        gate = support.WikiSupportGate(snapshot.task_root.parent.parent / 'execution',
            f'batch-{batch_no}-final', registry, client.model_config_hash)
        if reservation is None:
            # Resume the Gate's own persisted reservation; no new repair budget
            # or manufactured success. Its normal field/document checks apply.
            with gate._lock():
                state = gate._state()
                pending = state['repair']
            if pending is not None and not pending['consumed'] and registry.candidate_hash != pending['parent_hash']:
                from dataclasses import replace
                registry = replace(registry, parent_hash=pending['parent_hash'],
                    claim_mapping=tuple(support.ClaimMapping(c.block.claim_id, c.block.path, c.block.position)
                                        for c in registry.claims))
                client = self.runner.support_client(snapshot, self.runtime_root, task=task,
                    registry=registry, model=task.model, effort=task.effort, source_proof=source,
                    input_policy=t.APPLICATION_UTF8_POLICY)
                gate = support.WikiSupportGate(snapshot.task_root.parent.parent / 'execution',
                    f'batch-{batch_no}-final', registry, client.model_config_hash)
                reservation = support.RepairReservation(pending['token'], pending['number'], pending['parent_hash'], {})
        result = gate.review(client, reservation=reservation)
        if result.status not in {'supported_candidate_not_published', 'no_changed_claims'} or result.diagnostics:
            self._support_failure = (registry, gate, result)
            raise support.WikiSupportError('source_support_failed')
        binding, rows, _ = t.freeze_input(task, snapshot, batch_no, source, runtime_root=self.runtime_root)
        parsed = t.parse_proposal(proposal, binding, rows)
        outcomes = tuple(Outcome(o['raw_id'], o['content_sha256'], CONTRACT, task.boundary_sha256,
            o['status'], o['reason_code'], o['reason'], tuple((d['path'], d['sha256']) for d in o['documents']))
            for o in parsed['outcomes'])
        candidate = WikiOutcomes(snapshot.control / 'outcomes.sqlite3')
        candidate.initialize()
        receipt = candidate.validate(task, batch_no,
            {r.raw_id: contents[r.raw_id] for r in task.raw if r.batch_no == batch_no}, outcomes,
            checker=None, support_gate=gate, support_client=client, full_contents=contents,
            check_result=checked, proposal=proposal)
        return candidate, receipt

    @staticmethod
    def _batch_raw_ids(task, batch_no):
        batches = [b for b in task.batches if b.batch_no == batch_no]
        rows = [r for r in task.raw if r.batch_no == batch_no]
        if (type(batch_no) is not int or batch_no < 1 or len(batches) != 1 or not rows
                or type(batches[0].item_count) is not int or batches[0].item_count != len(rows)
                or any(type(r.raw_id) is not str for r in rows)
                or len({r.raw_id for r in rows}) != len(rows)):
            raise WikiStagingError('checkpoint_binding_changed')
        return tuple(r.raw_id for r in rows)

    def _program_facts(self, task, snapshot, batch_no):
        """Read-only program state plus verified successful local checkpoints."""
        from . import wiki_typed as t
        from .wiki_staging import _checkpoint_read, _verify_generation_terminal
        from .wiki_support import PHASE_ACTIVITIES
        verify_staging_protected(snapshot)
        raw_ids = self._batch_raw_ids(task, batch_no)
        run = self.store.runtime.run('managed', snapshot.workspace, ('describe-state',))
        if run.returncode:
            raise WikiStagingError('validation_failed')
        facts = json.loads(run.stdout)
        root = snapshot.task_root.parent.parent / 'execution' / f'batch-{batch_no}'
        phases = []
        for path in sorted(root.glob('*.json')):
            match = re.fullmatch(r'result(?:-([23]))?\.json', path.name)
            if match:
                attempt = int(match[1] or 1)
                saved = _checkpoint_read(path)
                reservation = root / ('reservation.json' if attempt == 1 else f'reservation-{attempt}.json')
                record = _checkpoint_read(reservation)
                phase, schema, final_sha = 'generation', t.PROPOSAL_SCHEMA, saved['proposal_sha256']
            else:
                match = re.fullmatch(r'(check|final-check|health|repair-([12])-check)-result\.json', path.name)
                if match is None:
                    continue
                saved = _checkpoint_read(path)
                record = saved['record']
                phase = 'repair-check' if match[2] else match[1]
                attempt = int(match[2] or 1)
                schema = None if phase == 'health' else t.CHECK_SCHEMA
                final_sha = saved['final_sha256']
                if saved['plan_sha256'] != task.plan_sha256:
                    raise WikiStagingError('checkpoint_binding_changed')
            if (record['task_id'] != task.task_id or record['plan_sha256'] != task.plan_sha256
                    or type(record.get('batch_no')) is not int or record['batch_no'] != batch_no):
                raise WikiStagingError('checkpoint_binding_changed')
            call = Path(record['recording_call'])
            terminal = _checkpoint_read(call / 'terminal.json')
            _verify_generation_terminal(terminal, call, record, schema_definition=schema)
            final = Path(record['argv'][record['argv'].index('-o') + 1])
            if t.digest(t.read_final(final.parent, final.name)) != final_sha:
                raise WikiStagingError('checkpoint_binding_changed')
            item = dict(phase=phase, attempt=attempt, final_sha256=final_sha,
                        activity=PHASE_ACTIVITIES[phase], batch_no=batch_no, raw_ids=list(raw_ids))
            if 'started_at' in record:
                item['started_at'] = record['started_at']
            phases.append(item)
        facts['completed_phases'] = phases
        # protocol-scan has calendar-dependent reminders. Freeze its actual
        # observation for this complete tree and phase history, not today's
        # reinterpretation of a previously checked candidate.
        from .wiki_staging import _checkpoint_tree, _checkpoint_write
        binding = dict(plan_sha256=task.plan_sha256, tree=_checkpoint_tree(snapshot), phases=phases)
        observed = root / ('program-scan-' + t.digest(t.encoded(binding)) + '.json')
        if observed.exists():
            record = _checkpoint_read(observed)
            if set(record) != {'binding', 'issue_counts', 'candidate_count', 'observed_at'} or record['binding'] != binding:
                raise WikiStagingError('checkpoint_binding_changed')
        else:
            from .wiki_staging import _phase_started_at
            observed_at = _phase_started_at()
            scan = scan_workflow_protocol(snapshot.workspace, runtime=self.store.runtime)
            record = dict(binding=binding, issue_counts=dict(scan.issue_counts), candidate_count=scan.candidate_count,
                          observed_at=observed_at)
            if _checkpoint_tree(snapshot) != binding['tree']:
                raise WikiStagingError('checkpoint_binding_changed')
            _checkpoint_write(observed, t.encoded(record))
        facts['issue_counts'] = record['issue_counts']
        facts['candidate_count'] = record['candidate_count']
        facts['scan_observed_at'] = record['observed_at']
        return t.encoded(facts)

    def _managed_sections(self, snapshot, task):
        """Render existing kb-managed regions; certify only exact current bytes."""
        from .wiki_support import GeneratedSection
        from .wiki_typed import digest
        verify_staging_protected(snapshot)
        if verify_source_kit(self.store.kit_root).manifest_sha256 != task.kit_manifest_sha256:
            raise WikiStagingError('validation_failed')
        run = self.store.runtime.run('managed', snapshot.workspace, ('describe-generated',))
        if run.returncode:
            raise WikiStagingError('validation_failed')
        rows = json.loads(run.stdout)
        # Check entries have a fixed operational grammar, no free prose field.
        log = (snapshot.workspace / 'wiki/log.md').read_text(encoding='utf-8')
        for match in re.finditer(r'(?m)^## (\[\d{4}-\d{2}-\d{2}\] check \| 程序检查)\n'
                r'- 修改页面 \d+ 个；错误 \d+ 条，提醒 \d+ 条\n(?:\n|$)', log):
            rows.append(['wiki/log.md', match[1], match[0]])
        proofs = []
        for path, heading, text in rows:
            content = (snapshot.workspace / path).read_bytes()
            # Section line endings from pg.render include the next separator;
            # take the actual full section only when the renderer equals it.
            if heading != '@system':
                actual = next((m for m in re.finditer(r'(?m)^## '+re.escape(heading)+r'\n[\s\S]*?(?=^#{1,2} |\Z)', content.decode())
                               if m[0].rstrip('\n') == text.rstrip('\n')), None)
                if actual is None:
                    continue
                text = actual[0]
            if heading == '@system' and content != text.encode():
                continue
            proofs.append(GeneratedSection(path, digest(content), heading, text.encode()))
        return tuple(dict.fromkeys(proofs))

    def _accept_typed(self, task, batch_no, snapshot, journal, lock, candidate, receipt, *, recovered=False):
        from .wiki_source_proof import trusted_source_callback
        from .wiki_outcomes import full_frozen_context
        from .ingestion import encoded, read_regular
        def verify(fresh, db):
            accepted = candidate._verified_publication(receipt, task_store=self.store,
                snapshot=snapshot, journal=journal, lock=lock, require_completed_batch=False, task=fresh)
            if (accepted.get('context_raw') is None or accepted.get('documents') is None
                    or accepted.get('check_sha256') is None or accepted.get('support') is None):
                raise WikiTaskError('readback_failed')
            context = full_frozen_context(fresh, {r.raw_id: read_regular(snapshot.workspace, r.relative_path) for r in fresh.raw})
            source = trusted_source_callback(self.source_store, lock, connection=db)
            if source(task=fresh, snapshot=snapshot, context=context) != accepted['source_proof_sha256']:
                raise WikiTaskError('readback_failed')
            accepted['plan_sha256'] = fresh.plan_sha256
            return receipt, encoded(accepted)
        return self.store.accept_published_batch(task.task_id, batch_no, verify=verify, recovered=recovered)

    def _run_typed_locked(self, task, lock):
        from . import wiki_typed as t
        from .wiki_staging import (bind_task_execution, load_bound_staging, _checkpoint_write,
                                   _checkpoint_read)
        from .wiki_outcomes import WikiOutcomes, OutcomeError
        from .wiki_source_proof import SourceProofError
        from .wiki_support import WikiSupportError
        snapshot = None
        batch_no = None
        self._support_failure = None
        try:
            source = self._typed_source(lock)
            container = self.runtime_root / 'wiki-tasks' / task.task_id
            if (container / 'execution' / 'binding.json').exists():
                snapshot = load_bound_staging(container, self.runtime_root, task=task, lock=lock)
            else:
                snapshot = prepare_staging(task.vault_path, self.runtime_root, task.task_id, task.raw,
                    python_executable=self.store.python_executable, source_kit_root=self.store.kit_root,
                    lock=lock, kit_runtime=self.store.runtime)
                bind_task_execution(snapshot, self.runtime_root, task=task, lock=lock)
            if task.state == 'queued':
                task = self.store.set_task_state(task.task_id, 'preparing')
            if task.state == 'preparing':
                task = self.store.set_task_state(task.task_id, 'running')
            for batch in task.batches:
                batch_no = batch.batch_no
                if batch.state == 'succeeded':
                    if any(r.relative_path in snapshot.pending_before for r in task.raw if r.batch_no == batch_no):
                        validated = self._typed_validate(snapshot, task, batch_no, regenerate=False)
                        snapshot = accept_validated_batch(snapshot, validated)
                    continue
                root = container / 'execution' / f'batch-{batch_no}'
                journal = snapshot.control / f'publish-{batch_no}'
                if batch.state == 'publishing':
                    result = self.recover(task.vault_path, journal, lock=lock)
                    if result.state != PublishState.COMMITTED or not self._committed_publish_proven(
                            task, batch_no, snapshot, journal, result.paths, lock):
                        raise WikiPublishError('publish_conflict')
                    receipt = _checkpoint_read(root / 'validated.json')['receipt_id']
                    task = self._accept_typed(task, batch_no, snapshot, journal, lock,
                        WikiOutcomes(snapshot.control / 'outcomes.sqlite3'), receipt)
                    validated = self._typed_validate(snapshot, task, batch_no, regenerate=False)
                    snapshot = accept_validated_batch(snapshot, validated)
                    continue
                if batch.state != 'queued':
                    task = self._reset_unpublished_batch(task, batch_no)
                    task = self.store.set_task_state(task.task_id, 'preparing')
                    task = self.store.set_task_state(task.task_id, 'running')
                self.store.set_batch_state(task.task_id, batch_no, 'preparing')
                task = self.store.set_batch_state(task.task_id, batch_no, 'running')
                args = dict(task=task, batch_no=batch_no, model=task.model, effort=task.effort,
                            source_proof=source, input_policy=t.APPLICATION_UTF8_POLICY)
                final_phase = next((root / f'final-stage-{n}.json' for n in (2, 1, 0)
                                    if (root / f'final-stage-{n}.json').exists()), None)
                if final_phase is not None:
                    saved = _checkpoint_read(final_phase)
                    proposal = saved['proposal'].encode('utf-8')
                    validated = self._typed_validate(snapshot, task, batch_no, regenerate=False)
                    changes = {c.relative_path: c.after_sha256 for c in validated.changes
                               if c.relative_path.startswith('wiki/') and c.relative_path.endswith('.md')}
                    checked = self._typed_call(task, snapshot, batch_no, saved['check_phase'], lock, source,
                        lambda: self.runner.check_json(snapshot, self.runtime_root, proposal=proposal, changes=changes, **args))
                    binding, rows, payload = t.freeze_input(task, snapshot, batch_no, source, runtime_root=self.runtime_root)
                    t.parse_proposal(proposal, binding, rows)
                    t.parse_check(checked.final_bytes, binding, rows, proposal_sha256=t.digest(proposal),
                        changes_sha256=t.digest(t.encoded(t.checked_documents(snapshot, changes))),
                        source_proof_sha256=payload['source_proof_sha256'],
                        full_context=tuple((r, (snapshot.workspace / r.relative_path).read_bytes()) for r in task.raw))
                    health = saved['health']
                else:
                    if (root / 'reservation.json').exists():
                        try:
                            snapshot, proposal, _ = self.store.load_generation_checkpoint(task.task_id, self.runtime_root,
                                batch_no=batch_no, lock=lock, expected_plan_sha256=task.plan_sha256, source_proof=source,
                                allow_regenerated_graph=True)
                        except WikiStagingError as error:
                            if str(error) != 'generation_interrupted':
                                raise
                            proposal = self._typed_generation(task, snapshot, batch_no, lock, source, args)
                    else:
                        proposal = self._typed_generation(task, snapshot, batch_no, lock, source, args)
                    validated = self._typed_validate(snapshot, task, batch_no)
                    changes = {c.relative_path: c.after_sha256 for c in validated.changes
                               if c.relative_path.startswith('wiki/') and c.relative_path.endswith('.md')}
                    checked = self._typed_call(task, snapshot, batch_no, 'check', lock, source,
                        lambda: self.runner.check_json(snapshot, self.runtime_root, proposal=proposal, changes=changes, **args))
                    stage = t.freeze_outcome_stage(snapshot, self.runtime_root, task=task, batch_no=batch_no,
                        proposal=proposal, check_result=checked, validated=validated, source_proof=source)
                    health = False
                    if validated.health_due and verify_formal_inputs(snapshot, task.vault_path, lock=lock).late_raw_count == 0:
                        before = validated
                        self._typed_call(task, snapshot, batch_no, 'health', lock, source,
                            lambda: self.runner.run_health_bounded(snapshot, self.runtime_root, lock=lock, **args))
                        validated = self._typed_validate(snapshot, task, batch_no)
                        if not self._health_completed(snapshot, before, validated):
                            raise WikiStagingError('validation_failed')
                        health = True
                    refreshed = t.refresh_outcome_documents(snapshot, self.runtime_root, task=task, batch_no=batch_no,
                        stage=stage, validated=validated, source_proof=source)
                    proposal = refreshed.proposal
                    if refreshed.reusable_check is None:
                        changes = {c.relative_path: c.after_sha256 for c in validated.changes
                                   if c.relative_path.startswith('wiki/') and c.relative_path.endswith('.md')}
                        checked = self._typed_call(task, snapshot, batch_no, 'final-check', lock, source,
                            lambda: self.runner.check_json(snapshot, self.runtime_root, proposal=proposal, changes=changes, **args))
                    _checkpoint_write(root / 'final-stage-0.json', t.encoded({
                        'proposal': proposal.decode('utf-8'), 'health': health,
                        'check_phase': 'final-check' if refreshed.reusable_check is None else 'check'}))
                # Gate invokes the real runner support client inside a recorded call.
                previous = self.runner.recording
                import uuid
                from .wiki_exec_recording import ExecRecordingV1
                evidence = self.runtime_root.parent / ('wiki-exec-' + uuid.uuid4().hex)
                evidence.mkdir(mode=0o700)
                recorder = ExecRecordingV1(evidence, workspace_root=snapshot.workspace, runtime_root=self.runtime_root)
                self.runner.recording = recorder
                try:
                    parent, reservation = None, None
                    while True:
                        try:
                            candidate, receipt = self._typed_candidate(task, snapshot, batch_no, validated,
                                proposal, checked, source, parent_registry=parent, reservation=reservation,
                                reuse_generated=final_phase is not None and parent is None and reservation is None,
                                check_phase=saved['check_phase'] if final_phase is not None else None)
                            break
                        except WikiSupportError:
                            if self._support_failure is None:
                                raise
                            parent, gate, failure = self._support_failure
                            self._support_failure = None
                            if failure.status not in {'source_support_failed', 'source_boundary_failed'}:
                                raise
                            with gate._lock():
                                state = gate._state()
                                abandoned = state['repair']
                                if abandoned is not None and not abandoned['consumed']:
                                    # Unknown completion consumes its reserved
                                    # extra attempt; retain evidence and used.
                                    from .wiki_staging import _checkpoint_bytes
                                    path = root / f"repair-interrupted-{abandoned['number']}.json"
                                    if not path.exists():
                                        _checkpoint_write(path, _checkpoint_bytes(abandoned))
                                    abandoned['consumed'] = True
                                    gate._save(state)
                            reservation = gate.reserve_repair()
                            if not reservation.token:
                                raise WikiSupportError('source_support_failed')
                            proposal = self._typed_call(task, snapshot, batch_no,
                                f'repair-{reservation.number}-generation', lock, source,
                                lambda: self.runner.run_outcomes(snapshot, self.runtime_root,
                                    repair_feedback=reservation.feedback, **args)).final_bytes
                            validated = self._typed_validate(snapshot, task, batch_no)
                            changes = {c.relative_path: c.after_sha256 for c in validated.changes
                                       if c.relative_path.startswith('wiki/') and c.relative_path.endswith('.md')}
                            checked = self._typed_call(task, snapshot, batch_no,
                                f'repair-{reservation.number}-check', lock, source,
                                lambda: self.runner.check_json(snapshot, self.runtime_root,
                                    proposal=proposal, changes=changes, **args))
                            _checkpoint_write(root / f'final-stage-{reservation.number}.json', t.encoded({
                                'proposal': proposal.decode('utf-8'), 'health': health,
                                'check_phase': f'repair-{reservation.number}-check'}))
                finally:
                    self.runner.recording = previous
                    recorder.close()
                try:
                    _checkpoint_write(root / 'validated.json', t.encoded({'receipt_id': receipt}))
                except FileExistsError:
                    if _checkpoint_read(root / 'validated.json') != {'receipt_id': receipt}:
                        raise WikiStagingError('checkpoint_binding_changed') from None
                self.store.set_batch_state(task.task_id, batch_no, 'validating')
                formal = verify_formal_inputs(snapshot, task.vault_path, lock=lock)
                if health and formal.late_raw_count:
                    raise WikiStagingError('validation_failed')
                self.store.set_batch_state(task.task_id, batch_no, 'publishing')
                published = self.publish(task.vault_path, snapshot.workspace, journal,
                    {c.relative_path: PublishExpectation(c.before_sha256, c.after_sha256) for c in validated.changes}, lock=lock)
                if published.state != PublishState.COMMITTED:
                    raise WikiPublishError('publish_interrupted')
                if not self._committed_publish_proven(task, batch_no, snapshot, journal, published.paths, lock):
                    raise WikiPublishError('readback_failed')
                task = self._accept_typed(task, batch_no, snapshot, journal, lock, candidate, receipt)
                snapshot = accept_validated_batch(snapshot, validated)
            return self._finalize(self.store.get(task.task_id))
        except (WikiTaskError, WikiStagingError, WikiRunnerError, WikiPublishError,
                t.TypedError, OutcomeError, SourceProofError, WikiSupportError, OSError) as error:
            phase = 'publishing' if snapshot is not None and batch_no is not None and (
                snapshot.control / f'publish-{batch_no}').exists() else 'none'
            code = self._publish_code(str(error)) if phase == 'publishing' else (
                self._runner_code(str(error)) if isinstance(error, WikiRunnerError) else 'validation_failed')
            self._safe_fail(task.task_id, batch_no, code, recovery_phase=phase)
            return WikiWorkResult(task.task_id, code)

    def _trusted_kb(self, snapshot: StagingSnapshot, task: WikiTask) -> bool:
        try:
            verify_staging_protected(snapshot)
            manifest = verify_source_kit(self.store.kit_root)
            if manifest.manifest_sha256 != task.kit_manifest_sha256:
                return False
            with WikiSessionBroker(snapshot.workspace, self.runtime_root) as session:
                result = self.store.runtime.run(
                    "kb", snapshot.workspace,
                    session_environment=session.environment())
                return result.returncode == 0
        except (OSError, UnicodeError, WikiStagingError, WikiKitError,
                WikiKitRuntimeError):
            return False

    @staticmethod
    def _health_completed(snapshot: StagingSnapshot, before: ValidatedBatch,
                          after: ValidatedBatch) -> bool:
        baseline = {item.relative_path: item.sha256 for item in snapshot.files}
        before_changes = {item.relative_path: item.after_sha256 for item in before.changes}
        after_changes = {item.relative_path: item.after_sha256 for item in after.changes}
        before_report = before_changes.get("wiki/体检报告.md",
                                           baseline.get("wiki/体检报告.md"))
        after_report = after_changes.get("wiki/体检报告.md",
                                         baseline.get("wiki/体检报告.md"))
        return (
            before.health_due
            and after.health_eligible
            and not after.health_due
            and after.health_due_reason == "current"
            and after.lint_count == before.lint_count + 1
            and before_report is not None
            and after_report is not None
            and before_report != after_report
        )

    def _publish_batch(self, task: WikiTask, snapshot: StagingSnapshot,
                       validated: ValidatedBatch, lock: VaultWriteLock, *,
                       health_performed: bool,
                       ) -> tuple[StagingSnapshot, WikiTask] | WikiWorkResult:
        if not validated.changes:
            return self._failed(task.task_id, validated.batch_no,
                                "validation_failed", snapshot)
        expected = {
            item.relative_path: PublishExpectation(item.before_sha256, item.after_sha256)
            for item in validated.changes
        }
        journal = snapshot.control / f"publish-{validated.batch_no}"
        self.store.set_batch_state(task.task_id, validated.batch_no, "publishing")
        try:
            published = self.publish(
                task.vault_path, snapshot.workspace, journal, expected, lock=lock)
            if published.state != PublishState.COMMITTED:
                raise WikiPublishError("publish_interrupted")
            accepted = accept_validated_batch(snapshot, validated)
            check = verify_formal_inputs(accepted, task.vault_path, lock=lock)
            protocol = scan_workflow_protocol(task.vault_path, runtime=self.store.runtime)
            frozen = {item.relative_path for item in task.raw}
            formal_frozen_pending = {item.relative_path for item in protocol.pending} & frozen
            expected_frozen_pending = set(validated.pending_after) & frozen
            if formal_frozen_pending != expected_frozen_pending:
                raise WikiPublishError("readback_failed")
            if health_performed and check.late_raw_count:
                raise WikiPublishError("readback_failed")
            task = self.store.mark_batch_readback_succeeded(task.task_id, validated.batch_no)
            return accepted, task
        except (WikiPublishError, WikiStagingError, WikiTaskError) as error:
            code = self._publish_code(str(error))
            self._safe_fail(task.task_id, validated.batch_no, code,
                            recovery_phase="publishing")
            return WikiWorkResult(task.task_id, code)

    def _recover_interrupted_publish(self, task: WikiTask,
                                     lock: VaultWriteLock) -> WikiTask | WikiWorkResult:
        publishing = [batch for batch in task.batches if batch.state == "publishing"]
        if not publishing:
            return task
        task_root = self.runtime_root / "wiki-tasks" / task.task_id
        try:
            snapshot = load_staging_snapshot(task_root)
        except WikiStagingError:
            return self._recovery_failed(task, publishing[0].batch_no, "recovery_failed")
        for batch in publishing:
            journal = snapshot.control / f"publish-{batch.batch_no}"
            if not journal.is_dir():
                return self._recovery_failed(task, batch.batch_no, "recovery_failed")
            try:
                result = self.recover(task.vault_path, journal, lock=lock)
            except WikiPublishError:
                return self._recovery_failed(task, batch.batch_no, "recovery_failed")
            if result.state == PublishState.RECOVERY_FAILED:
                return self._recovery_failed(task, batch.batch_no, "recovery_failed")
            if result.state == PublishState.RECOVERED:
                self.store.fail_batch(task.task_id, batch.batch_no, "publish_interrupted")
                self.store.fail_task(task.task_id, "publish_interrupted",
                                     recovery_phase="publishing")
                self.store.set_recovery_state(task.task_id, "running")
                self.store.set_recovery_state(task.task_id, "succeeded")
                return WikiWorkResult(task.task_id, "publish_interrupted")
            if not self._committed_publish_proven(task, batch.batch_no, snapshot,
                                                  journal, result.paths, lock):
                return self._recovery_failed(task, batch.batch_no, "publish_conflict")
            task = self.store.mark_batch_readback_succeeded(task.task_id, batch.batch_no)
        return task

    def recover_task(self, task_id: str) -> WikiWorkResult:
        """Explicitly resolve a failed publish journal; never retry the model."""
        if not self._activity.acquire(blocking=False):
            return WikiWorkResult(task_id, "vault_busy")
        try:
            return self._recover_task(task_id)
        finally:
            self._activity.release()

    def _recover_task(self, task_id: str) -> WikiWorkResult:
        try:
            task = self.store.get(task_id)
            if (task.state != "failed" or task.recovery_phase != "publishing"
                    or task.recovery_state not in {"required", "failed"}):
                return WikiWorkResult(task_id, "recovery_failed")
            lock = VaultWriteLock.acquire(task.vault_path)
        except BlockingIOError:
            return WikiWorkResult(task_id, "vault_busy")
        except (WikiTaskError, OSError):
            return WikiWorkResult(task_id, "recovery_failed")
        with lock:
            self.store.set_recovery_state(task_id, "running")
            task = self.store.get(task_id)
            failed = [batch for batch in task.batches if batch.state == "failed"]
            if len(failed) != 1:
                self.store.set_recovery_state(task_id, "failed")
                return WikiWorkResult(task_id, "recovery_failed")
            batch = failed[0]
            task_root = self.runtime_root / "wiki-tasks" / task_id
            try:
                snapshot = load_staging_snapshot(task_root)
                journal = snapshot.control / f"publish-{batch.batch_no}"
                result = self.recover(task.vault_path, journal, lock=lock)
                if result.state == PublishState.RECOVERY_FAILED:
                    raise WikiPublishError("recovery_failed")
                if result.state == PublishState.COMMITTED:
                    if not self._committed_publish_proven(
                        task, batch.batch_no, snapshot, journal, result.paths, lock
                    ):
                        raise WikiPublishError("publish_conflict")
                    if task.outcome_contract == 'r08-wiki-outcomes-v1':
                        from .wiki_outcomes import WikiOutcomes
                        from .wiki_staging import _checkpoint_read
                        self._typed_source(lock)
                        receipt = _checkpoint_read(task_root / 'execution' / f'batch-{batch.batch_no}' / 'validated.json')['receipt_id']
                        self._accept_typed(task, batch.batch_no, snapshot, journal, lock,
                            WikiOutcomes(snapshot.control / 'outcomes.sqlite3'), receipt, recovered=True)
                    else:
                        self.store.mark_recovered_batch_readback_succeeded(task_id, batch.batch_no)
                self.store.set_recovery_state(task_id, "succeeded")
                return WikiWorkResult(task_id, "publish_interrupted")
            except (WikiPublishError, WikiStagingError, WikiTaskError, ValueError, OSError):
                self.store.set_recovery_state(task_id, "failed")
                return WikiWorkResult(task_id, "recovery_failed")

    def _recovery_failed(self, task: WikiTask, batch_no: int,
                         code: str) -> WikiWorkResult:
        self._safe_fail(task.task_id, batch_no, code, recovery_phase="publishing")
        try:
            current = self.store.get(task.task_id)
            if current.recovery_state == "required":
                self.store.set_recovery_state(task.task_id, "running")
                self.store.set_recovery_state(task.task_id, "failed")
        except WikiTaskError:
            pass
        return WikiWorkResult(task.task_id, code)

    def _committed_publish_proven(self, task: WikiTask, batch_no: int,
                                  snapshot: StagingSnapshot, journal: Path,
                                  paths: tuple[str, ...], lock: VaultWriteLock) -> bool:
        try:
            after = self._journal_after(journal, paths)
            verify_recovered_publish(snapshot, task.vault_path, after, lock=lock)
            manifest = verify_source_kit(self.store.kit_root)
            if manifest.manifest_sha256 != task.kit_manifest_sha256:
                return False
            protocol = scan_workflow_protocol(task.vault_path, runtime=self.store.runtime)
            frozen = {item.relative_path for item in task.raw}
            expected = {
                item.relative_path for item in task.raw
                if item.batch_no != batch_no
                and next(batch.state for batch in task.batches
                         if batch.batch_no == item.batch_no) != "succeeded"
            }
            return ({item.relative_path for item in protocol.pending} & frozen) == expected
        except (OSError, UnicodeError, ValueError, WikiKitError,
                WikiStagingError, WikiTaskError):
            return False

    @staticmethod
    def _journal_after(journal: Path, paths: tuple[str, ...]) -> dict[str, str]:
        target = journal / "journal.json"
        info = target.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("journal")
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("state") != "committed":
            raise ValueError("journal")
        items = data.get("items")
        if not isinstance(items, list):
            raise ValueError("journal")
        after: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("journal")
            relative, digest = item.get("path"), item.get("after")
            if (not isinstance(relative, str) or relative in after
                    or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                raise ValueError("journal")
            after[relative] = digest
        if set(after) != set(paths):
            raise ValueError("journal")
        return after

    def _reset_unpublished_batch(self, task: WikiTask, batch_no: int) -> WikiTask:
        self.store.fail_batch(task.task_id, batch_no, "interrupted")
        self.store.fail_task(task.task_id, "interrupted", recovery_phase="none")
        return self.store.retry_failed(task.task_id)

    def _failed(self, task_id: str, batch_no: int, code: str,
                snapshot: StagingSnapshot) -> WikiWorkResult:
        self._safe_fail(task_id, batch_no, code, recovery_phase="none")
        try:
            cleanup_staging(snapshot)
        except WikiStagingError:
            pass
        return WikiWorkResult(task_id, code)

    def _safe_fail(self, task_id: str, batch_no: int | None, code: str, *,
                   recovery_phase: str) -> None:
        try:
            if batch_no is not None:
                current = self.store.get(task_id)
                row = next((batch for batch in current.batches if batch.batch_no == batch_no), None)
                if row is not None and row.state != "failed":
                    self.store.fail_batch(task_id, batch_no, code)
            current = self.store.get(task_id)
            if current.state != "failed":
                self.store.fail_task(task_id, code, recovery_phase=recovery_phase)
        except WikiTaskError:
            pass

    def _finalize(self, task: WikiTask) -> WikiWorkResult:
        try:
            if task.state == "queued":
                task = self.store.set_task_state(task.task_id, "preparing")
            if task.state == "preparing":
                task = self.store.set_task_state(task.task_id, "running")
            if task.state == "running":
                task = self.store.set_task_state(task.task_id, "validating")
            if task.state == "validating":
                task = self.store.set_task_state(task.task_id, "publishing")
            if task.state == "publishing":
                task = self.store.set_task_state(task.task_id, "succeeded")
            if task.state != "succeeded":
                raise WikiTaskError("invalid_transition")
            return WikiWorkResult(task.task_id, None)
        except WikiTaskError:
            self._safe_fail(task.task_id, None, "readback_failed", recovery_phase="readback")
            return WikiWorkResult(task.task_id, "readback_failed")

    @staticmethod
    def _runner_code(code: str) -> str:
        return code if code in {
            "config_required", "runner_unavailable", "runner_timeout", "model_unavailable",
            "network_error", "agent_failed", "vault_busy", "interrupted",
        } else "agent_failed"

    @staticmethod
    def _staging_code(code: str) -> str:
        if code == "raw_changed":
            return code
        if code == "publish_conflict":
            return code
        return "validation_failed"

    @staticmethod
    def _publish_code(code: str) -> str:
        if code in {"target_digest_changed", "publish_path_changed", "publish_conflict"}:
            return "publish_conflict"
        if code == "readback_failed":
            return code
        return "publish_interrupted"
