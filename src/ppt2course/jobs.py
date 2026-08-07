"""In-process job queue for running the pipeline behind a web request.

A single background worker thread processes jobs one at a time (video
composition is CPU-heavy; running several in parallel on one box just makes
all of them slower). A separate cleanup pass deletes finished jobs' work/out
directories once they are older than the retention window, so a public,
unattended deployment doesn't accumulate other users' files forever.
"""

import queue
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from ppt2course.pipeline import PipelineError, run_pipeline

DEFAULT_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_CLEANUP_INTERVAL_SECONDS = 10 * 60


class JobStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class Job:
    id: str
    status: JobStatus
    created_at: float
    work_dir: str
    out_dir: str
    started_at: float | None = None
    completed_at: float | None = None
    result: dict | None = None
    error: str | None = None
    kwargs: dict = field(default_factory=dict, repr=False)


class JobManager:
    def __init__(
        self,
        pipeline_fn=run_pipeline,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        cleanup_interval_seconds: int = DEFAULT_CLEANUP_INTERVAL_SECONDS,
        auto_start: bool = True,
    ):
        self._pipeline_fn = pipeline_fn
        self._retention_seconds = retention_seconds
        self._cleanup_interval_seconds = cleanup_interval_seconds
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()

        if auto_start:
            threading.Thread(target=self._worker_loop, daemon=True).start()
            threading.Thread(target=self._cleanup_loop, daemon=True).start()

    def submit(self, job_id: str | None = None, **pipeline_kwargs) -> str:
        job_id = job_id if job_id is not None else uuid.uuid4().hex
        job = Job(
            id=job_id,
            status=JobStatus.QUEUED,
            created_at=time.time(),
            work_dir=pipeline_kwargs.get("work_dir", ""),
            out_dir=pipeline_kwargs.get("out_dir", ""),
            kwargs=pipeline_kwargs,
        )
        with self._lock:
            self._jobs[job_id] = job
        self._queue.put(job_id)
        return job_id

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def process_next(self, block: bool = True, timeout: float | None = None) -> bool:
        try:
            job_id = self._queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return False
        self._process_one(job_id)
        return True

    def _process_one(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = JobStatus.RUNNING
            # Marks when processing actually began, not when it was
            # submitted — "生成時間長" (completed_at - started_at) should
            # reflect real generation time, not time spent waiting behind
            # other jobs in the single-worker queue.
            job.started_at = time.time()
            kwargs = job.kwargs

        try:
            result = self._pipeline_fn(**kwargs)
        except PipelineError as exc:
            with self._lock:
                job.status = JobStatus.ERROR
                job.error = str(exc)
                job.completed_at = time.time()
            return

        with self._lock:
            job.status = JobStatus.DONE
            job.result = result
            job.completed_at = time.time()

    def cleanup_expired(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            expired_ids = [
                job_id
                for job_id, job in self._jobs.items()
                if job.status in (JobStatus.DONE, JobStatus.ERROR)
                and job.completed_at is not None
                and now - job.completed_at >= self._retention_seconds
            ]
            expired = [self._jobs.pop(job_id) for job_id in expired_ids]

        for job in expired:
            shutil.rmtree(job.work_dir, ignore_errors=True)
            shutil.rmtree(job.out_dir, ignore_errors=True)

    def _worker_loop(self) -> None:
        while True:
            self.process_next()

    def _cleanup_loop(self) -> None:
        while True:
            time.sleep(self._cleanup_interval_seconds)
            self.cleanup_expired()
