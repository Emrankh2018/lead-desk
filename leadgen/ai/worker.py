"""Single background worker for AI jobs (one model call at a time; the local model is CPU-bound)."""
import logging
import queue
import threading
import time
from collections.abc import Callable

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from leadgen.ai import tasks
from leadgen.ai.provider import Provider, ProviderError
from leadgen.models import AiInsight, OutreachDraft
from leadgen.services import audit

log = logging.getLogger("leadgen.ai")
STATUS_TTL_SECONDS = 30


class AiWorker:
    def __init__(self, sessionmaker: Callable[[], Session], provider_factory: Callable[[], Provider]):
        self._sessionmaker = sessionmaker
        self._provider_factory = provider_factory
        self._queue: queue.Queue[tuple[str, int, str]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._status: tuple[float, bool, str] | None = None
        self.current: str | None = None

    def status(self) -> tuple[bool, str]:
        """Provider availability, cached briefly so page loads stay fast."""
        now = time.monotonic()
        if not self._status or now - self._status[0] > STATUS_TTL_SECONDS:
            ok, msg = self._provider_factory().status()
            self._status = (now, ok, msg)
        return self._status[1], self._status[2]

    def pending(self) -> int:
        return self._queue.qsize() + (1 if self.current else 0)

    def submit(self, kind: str, row_id: int, actor: str) -> None:
        with self._start_lock:
            if not self._thread:
                self._thread = threading.Thread(target=self._loop, daemon=True, name="ai-worker")
                self._thread.start()
        self._queue.put((kind, row_id, actor))

    def drain(self, timeout: float = 60) -> None:
        """Block until queued jobs finish (tests and CLI)."""
        deadline = time.monotonic() + timeout
        while (self._queue.unfinished_tasks or self.current) and time.monotonic() < deadline:
            time.sleep(0.05)

    def _loop(self) -> None:
        while True:  # lives for the process; exiting when idle could strand a job submitted at that moment
            kind, row_id, actor = self._queue.get()
            self.current = f"{kind} #{row_id}"
            try:
                self.run_one(kind, row_id, actor)
            finally:
                self.current = None
                self._queue.task_done()

    def run_one(self, kind: str, row_id: int, actor: str) -> None:
        model_cls = AiInsight if kind == "insight" else OutreachDraft
        with self._sessionmaker() as s:
            row = s.get(model_cls, row_id)
            if row is None or row.status != "pending":
                return
            try:
                provider = self._provider_factory()
                (tasks.run_insight if kind == "insight" else tasks.run_draft)(s, row, provider)
            except (ProviderError, ValidationError) as exc:
                s.rollback()
                row = s.get(model_cls, row_id)
                row.status = "failed"
                row.error = str(exc) if isinstance(exc, ProviderError) else "model output did not match the expected format"
            except Exception:
                log.exception("AI job %s %s crashed", kind, row_id)
                s.rollback()
                row = s.get(model_cls, row_id)
                row.status, row.error = "failed", "unexpected error (see server log)"
            audit.record(s, actor, f"ai.{kind}", "business", row.business_id, row_id=row_id, status=row.status,
                         model=row.model, latency_ms=row.latency_ms)
            s.commit()


def fail_interrupted(session: Session) -> None:
    """Jobs left pending by a restart will never run; mark them so the user can retry."""
    for model_cls in (AiInsight, OutreachDraft):
        session.execute(update(model_cls).where(model_cls.status == "pending")
                        .values(status="failed", error="interrupted because the app restarted; try again"))
