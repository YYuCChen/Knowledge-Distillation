"""One admission and lifecycle boundary for application-owned workers."""
from __future__ import annotations

import threading
from typing import Protocol


class AdmissionError(RuntimeError):
    """Fixed-code rejection for a new state-changing operation."""


class AdmissionLease:
    def __init__(self, gate: "WorkAdmissionGate", owner: int):
        self._gate = gate
        self._owner = owner
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gate._leave(self._owner)

    def __enter__(self) -> "AdmissionLease":
        return self

    def __exit__(self, _kind, _value, _traceback) -> None:
        self.close()


class WorkAdmissionGate:
    """Reject new writes atomically while an update owns the process."""

    def __init__(self):
        self._guard = threading.Lock()
        self._active: dict[int, int] = {}
        self._reserved_by: int | None = None

    def enter(self) -> AdmissionLease:
        owner = threading.get_ident()
        with self._guard:
            if self._reserved_by is not None:
                if self._reserved_by != owner or self._active.get(owner, 0) == 0:
                    raise AdmissionError("update_reserved")
            self._active[owner] = self._active.get(owner, 0) + 1
        return AdmissionLease(self, owner)

    def try_enter(self) -> AdmissionLease | None:
        try:
            return self.enter()
        except AdmissionError:
            return None

    def _leave(self, owner: int) -> None:
        with self._guard:
            depth = self._active.get(owner, 0)
            if depth <= 1:
                self._active.pop(owner, None)
            else:
                self._active[owner] = depth - 1

    def reserve(self) -> bool:
        owner = threading.get_ident()
        with self._guard:
            if self._reserved_by is not None:
                return False
            other_active = sum(
                depth for thread_id, depth in self._active.items()
                if thread_id != owner
            )
            if other_active:
                return False
            self._reserved_by = owner
            return True

    def release_reservation(self) -> None:
        with self._guard:
            if self._reserved_by is None:
                return
            self._reserved_by = None

    @property
    def reserved(self) -> bool:
        with self._guard:
            return self._reserved_by is not None

    @property
    def idle(self) -> bool:
        with self._guard:
            return not self._active


class _Worker(Protocol):
    def start(self): ...
    def stop(self, timeout: float = ...) -> bool: ...
    def reserve_for_update(self) -> bool: ...
    def release_update(self) -> None: ...
    def update_ready(self) -> bool: ...


class WorkerCoordinator:
    """Reserve, stop and restart the distillation and wiki workers together."""

    def __init__(self, primary: _Worker, wiki: _Worker, gate: WorkAdmissionGate):
        self.primary = primary
        self.wiki = wiki
        self.gate = gate
        self._ingress = None
        self._update_reserved = False
        self._resume_ingress = False
        self._guard = threading.Lock()

    def attach_ingress(self, ingress) -> None:
        self._ingress = ingress

    def start(self) -> bool:
        with self._guard:
            if self._update_reserved or self.gate.reserved:
                return False
            primary_started = self.primary.start()
            wiki_started = self.wiki.start()
            if self._ingress is not None:
                self._ingress.start()
            return primary_started is not False or wiki_started is not False

    def reserve_for_update(self) -> bool:
        with self._guard:
            if self._update_reserved:
                return True
            if not self.gate.reserve():
                return False
            ingress_was_running = self._ingress_running()
            ingress_stopped = not ingress_was_running
            primary_reserved = False
            wiki_reserved = False
            try:
                if ingress_was_running:
                    ingress_stopped = self._ingress.stop()
                    if ingress_stopped is False:
                        return False
                primary_reserved = self.primary.reserve_for_update()
                if not primary_reserved:
                    return False
                wiki_reserved = self.wiki.reserve_for_update()
                if not wiki_reserved:
                    return False
                self._update_reserved = True
                self._resume_ingress = ingress_was_running
                return True
            finally:
                if not (primary_reserved and wiki_reserved):
                    if wiki_reserved:
                        self.wiki.release_update()
                    if primary_reserved:
                        self.primary.release_update()
                    self.gate.release_reservation()
                    if ingress_was_running and ingress_stopped and self._ingress is not None:
                        self._ingress.start()
                    elif ingress_was_running and self._ingress is not None:
                        recover = getattr(self._ingress, "recover_after_failed_stop", None)
                        if recover is not None:
                            recover()

    def release_update(self) -> None:
        with self._guard:
            if not self._update_reserved:
                return
            self.wiki.release_update()
            self.primary.release_update()
            self._update_reserved = False
            self.gate.release_reservation()
            resume_ingress = self._resume_ingress
            self._resume_ingress = False
            if resume_ingress and self._ingress is not None:
                self._ingress.start()

    def _ingress_running(self) -> bool:
        if self._ingress is None:
            return False
        checker = getattr(self._ingress, "is_running", None)
        if callable(checker):
            return bool(checker())
        if hasattr(self._ingress, "runtime"):
            return self._ingress.runtime is not None
        # Existing ingress test doubles predate an explicit running state.
        return True

    def update_ready(self) -> bool:
        return (not self._update_reserved and self.gate.idle
                and self.primary.update_ready() and self.wiki.update_ready())

    def stop(self, *, primary_timeout: float = 2, wiki_timeout: float = 10) -> bool:
        with self._guard:
            if not self.gate.reserved and not self.gate.reserve():
                return False
            ingress_stopped = True
            if self._ingress is not None:
                ingress_stopped = self._ingress.stop()
            primary_stopped = self.primary.stop(primary_timeout)
            wiki_stopped = self.wiki.stop(wiki_timeout)
            return bool(ingress_stopped and primary_stopped and wiki_stopped)
