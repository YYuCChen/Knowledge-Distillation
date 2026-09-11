from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .pipeline import Distiller
from .store import Store


logger = logging.getLogger(__name__)


class SingleWorker:
    """One process-local worker with SQLite as the durable FIFO authority."""

    def __init__(
        self,
        store: Store,
        distiller: Distiller | Callable[[], Distiller],
        *,
        idle_seconds: float = 0.25,
        organization=None,
        maintenance=None,
    ):
        self.store = store
        self.distiller = distiller
        self.idle_seconds = idle_seconds
        self.organization = organization
        self.maintenance = maintenance
        self._next_maintenance = 0.0
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._activity = threading.Lock()
        self._update_reserved = False

    def reserve_for_update(self) -> bool:
        """Close the claim race before permitting the desktop to quit for update."""
        if not self._activity.acquire(blocking=False):
            return False
        try:
            if self.pending_work():
                return False
            self._update_reserved = True
            return True
        finally:
            self._activity.release()

    def pending_work(self):
        from .database import connect
        with connect(self.store.path) as db:
            return bool(db.execute("SELECT 1 FROM distill_items WHERE state IN ('queued','working') LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM organization_events WHERE status='running' LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM collection_operations WHERE state IN ('queued','working') LIMIT 1").fetchone())

    def update_ready(self):
        return not self._activity.locked() and not self.pending_work()

    def release_update(self):
        with self._activity:
            self._update_reserved = False
        self.wake()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.store.requeue_interrupted()
        self._stopping.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="knowledge-distiller-worker",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def run_organization(self) -> bool:
        if self.organization is None:
            return False
        from .database import connect
        from knowledge_distiller.organization_service import fail_event
        from knowledge_distiller.organization_models import OrganizationFailureCode
        with connect(self.store.path) as db:
            row = db.execute("SELECT event_id FROM organization_events WHERE status='running' ORDER BY event_id LIMIT 1").fetchone()
        if row is None:
            return False
        try:
            service = self.organization() if callable(self.organization) else self.organization
            service.drive(row['event_id'])
        except Exception as error:
            fail_event(self.store.path, row['event_id'], OrganizationFailureCode.GROWTH_PLANNING_FAILED)
            logger.error("Organization %s failed (%s)", row['event_id'], type(error).__name__)
        return True

    def run_one(self) -> int | None:
        work = self.store.claim_next_work()
        if work is None:
            return None
        kind, item_id = work
        if kind == 'collection':
            from .collections import Collections
            from .database import connect
            try:
                service = self.distiller() if callable(self.distiller) else self.distiller
                Collections(self.store).run(item_id, service)
            except Exception as error:
                with connect(self.store.path) as db:
                    db.execute("UPDATE collection_operations SET state='failed',error_code='collection_processing_failed' WHERE operation_id=?", (item_id,))
                logger.error("Collection %s failed (%s)",item_id,type(error).__name__)
            return item_id
        try:
            service = self.distiller() if callable(self.distiller) else self.distiller
            service.run(item_id)
        except Exception as error:
            # Isolate one failed item, not a failed database or queue claim.
            # Do not log provider exception text, which may contain private data.
            row = self.store.item_bundle(item_id)
            if row is not None and row["state"] == "working":
                self.store.mark_failed(
                    item_id, row["phase"], "processing_unexpected_failure"
                )
            logger.error(
                "Item %s processing failed (%s)", item_id, type(error).__name__
            )
        return item_id

    def _loop(self) -> None:
        while not self._stopping.is_set():
            with self._activity:
                if not self._update_reserved:
                    if self.maintenance is not None and time.monotonic() >= self._next_maintenance:
                        self.maintenance()
                        self._next_maintenance = time.monotonic() + 60
                    if self.run_organization():
                        continue
                    if self.run_one() is not None:
                        continue
            self._wake.wait(self.idle_seconds)
            self._wake.clear()
