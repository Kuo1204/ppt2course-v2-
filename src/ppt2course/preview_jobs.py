"""Generic in-memory async task tracker for the "preview" endpoints.

Unlike ``jobs.JobManager`` (a single-worker queue purpose-built for the
CPU-heavy video pipeline, where only one job should ever run at a time),
these tasks are lightweight, mostly-I/O-bound calls (Gemini requests) that
are fine to run concurrently -- so each task just gets its own background
thread, no queue.

Why this exists at all: this app is deployed behind a reverse proxy
(Pomerium) with its own upstream response timeout. A synchronous
request/response for something like AI script generation can run past that
timeout even though the backend is still working correctly -- the proxy
gives up and returns its own 504 before the real answer is ever sent. Moving
the slow call behind submit()/get() lets the browser poll for the result
instead of holding one HTTP request open for the whole duration, so no
single request needs to outlive the proxy's timeout.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_RETENTION_SECONDS = 60 * 60  # 1 hour: plenty for a browser tab to
# poll and pick up its own result; these aren't meant to be durable like a
# rendered video's job record.


@dataclass
class PreviewTask:
    id: str
    status: str  # "running" | "done" | "error"
    created_at: float
    result: Any = None
    error: str | None = None
    completed_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class PreviewTaskManager:
    """Runs each submitted call on its own thread and lets callers poll for
    the result by id. No queueing/concurrency limit -- these are cheap
    compared to video rendering, and each is one user waiting on one
    request, so serializing them would just add needless latency."""

    def __init__(self, retention_seconds: int = DEFAULT_RETENTION_SECONDS):
        self._retention_seconds = retention_seconds
        self._tasks: dict[str, PreviewTask] = {}
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., Any], *args, **kwargs) -> str:
        task_id = uuid.uuid4().hex
        task = PreviewTask(id=task_id, status="running", created_at=time.time())
        with self._lock:
            self._tasks[task_id] = task
        threading.Thread(
            target=self._run, args=(task, fn, args, kwargs), daemon=True
        ).start()
        return task_id

    def _run(self, task: PreviewTask, fn: Callable[..., Any], args, kwargs) -> None:
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see
            # jobs.py's _process_one for the same reasoning: one task's
            # unexpected exception must only ever fail that task, never take
            # down anything shared.
            with task._lock:
                task.status = "error"
                task.error = str(exc)
                task.completed_at = time.time()
            return
        with task._lock:
            task.status = "done"
            task.result = result
            task.completed_at = time.time()

    def get(self, task_id: str) -> PreviewTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
        return task

    def cleanup_expired(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            expired_ids = [
                task_id
                for task_id, task in self._tasks.items()
                if task.completed_at is not None
                and now - task.completed_at >= self._retention_seconds
            ]
            for task_id in expired_ids:
                del self._tasks[task_id]
