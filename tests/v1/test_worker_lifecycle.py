from __future__ import annotations

import threading

import pytest

from knowledge_distiller.v1.worker_lifecycle import (
    AdmissionError,
    WorkAdmissionGate,
    WorkerCoordinator,
)
from knowledge_distiller.v1.wiki_worker import WikiWorker


class Worker:
    def __init__(self, *, reserve=True, stop=True):
        self.reserve_result = reserve
        self.stop_result = stop
        self.reserved = False
        self.releases = 0
        self.starts = 0

    def start(self):
        self.starts += 1
        return True

    def stop(self, _timeout=0):
        return self.stop_result

    def reserve_for_update(self):
        self.reserved = self.reserve_result
        return self.reserve_result

    def release_update(self):
        self.reserved = False
        self.releases += 1

    def update_ready(self):
        return True


class Ingress:
    def __init__(self, *, stop=True, running=True):
        self.stop_result = stop
        self.running = running
        self.starts = 0
        self.recoveries = 0

    def start(self):
        self.starts += 1
        self.running = True

    def stop(self):
        if self.stop_result:
            self.running = False
        return self.stop_result

    def is_running(self):
        return self.running

    def recover_after_failed_stop(self):
        self.recoveries += 1


def test_reserved_gate_allows_only_the_still_active_reserving_request():
    gate = WorkAdmissionGate()
    outer = gate.enter()
    assert gate.reserve() is True
    nested = gate.enter()
    nested.close()
    outer.close()
    with pytest.raises(AdmissionError, match="update_reserved"):
        gate.enter()
    # Reusing the same worker thread identity does not grant a reservation bypass.
    assert gate.reserve() is False
    gate.release_reservation()
    gate.enter().close()


def test_update_reservation_rolls_back_first_worker_when_second_refuses():
    gate = WorkAdmissionGate()
    primary, wiki = Worker(), Worker(reserve=False)
    ingress = Ingress()
    coordinator = WorkerCoordinator(primary, wiki, gate)
    coordinator.attach_ingress(ingress)

    assert coordinator.reserve_for_update() is False
    assert primary.releases == 1
    assert primary.reserved is False
    assert gate.reserved is False
    assert ingress.starts == 1


def test_ingress_stop_timeout_schedules_recovery_after_reservation_rollback():
    gate = WorkAdmissionGate()
    ingress = Ingress(stop=False)
    coordinator = WorkerCoordinator(Worker(), Worker(), gate)
    coordinator.attach_ingress(ingress)

    assert coordinator.reserve_for_update() is False
    assert gate.reserved is False
    assert ingress.recoveries == 1
    assert ingress.starts == 0


def test_update_reservation_does_not_start_an_ingress_that_was_stopped():
    gate = WorkAdmissionGate()
    ingress = Ingress(running=False)
    coordinator = WorkerCoordinator(Worker(), Worker(), gate)
    coordinator.attach_ingress(ingress)

    assert coordinator.reserve_for_update() is True
    coordinator.release_update()
    assert ingress.starts == 0
    assert ingress.running is False


def test_concurrent_admission_prevents_update_until_request_leaves():
    gate = WorkAdmissionGate()
    coordinator = WorkerCoordinator(Worker(), Worker(), gate)
    entered = threading.Event()
    release = threading.Event()

    def request():
        with gate.enter():
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=request)
    thread.start()
    assert entered.wait(2)
    assert coordinator.reserve_for_update() is False
    release.set()
    thread.join(2)
    assert coordinator.reserve_for_update() is True
    coordinator.release_update()


def test_wiki_stop_timeout_retains_live_thread_and_refuses_second_start(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class Runner:
        def reset_cancellation(self):
            pass

        def cancel(self):
            pass

    worker = WikiWorker(object(), tmp_path, Runner(), idle_seconds=0.01)

    def slow_run():
        entered.set()
        release.wait(5)
        return None

    worker.run_one = slow_run
    assert worker.start() is True
    assert entered.wait(2)
    live_thread = worker._thread
    assert worker.stop(timeout=0.01) is False
    assert worker._thread is live_thread and live_thread.is_alive()
    assert worker.start() is False
    release.set()
    assert worker.stop(timeout=2) is True
    assert worker._thread is None
