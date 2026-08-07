import os
import time

import pytest

from ppt2course.jobs import Job, JobManager, JobStatus
from ppt2course.pipeline import PipelineError


def _make_manager(pipeline_fn, retention_seconds=86400):
    return JobManager(pipeline_fn=pipeline_fn, retention_seconds=retention_seconds, auto_start=False)


def test_submit_returns_job_id_and_job_starts_queued():
    manager = _make_manager(pipeline_fn=lambda **kwargs: {"mp4": "a"})
    job_id = manager.submit(work_dir="w", out_dir="o")

    job = manager.get(job_id)
    assert isinstance(job_id, str) and job_id
    assert job.status is JobStatus.QUEUED


def test_submit_accepts_caller_supplied_job_id():
    manager = _make_manager(pipeline_fn=lambda **kwargs: {})
    job_id = manager.submit(job_id="fixed-id", work_dir="w", out_dir="o")

    assert job_id == "fixed-id"
    assert manager.get("fixed-id") is not None


def test_get_unknown_job_id_returns_none():
    manager = _make_manager(pipeline_fn=lambda **kwargs: {})
    assert manager.get("does-not-exist") is None


def test_process_next_runs_pipeline_and_marks_done():
    calls = []

    def fake_pipeline(**kwargs):
        calls.append(kwargs)
        return {"mp4": "課程.mp4", "srt": "課程.srt", "docx": "課程.docx"}

    manager = _make_manager(pipeline_fn=fake_pipeline)
    job_id = manager.submit(work_dir="w", out_dir="o", base_name="課程")

    processed = manager.process_next()

    job = manager.get(job_id)
    assert processed is True
    assert job.status is JobStatus.DONE
    assert job.result == {"mp4": "課程.mp4", "srt": "課程.srt", "docx": "課程.docx"}
    assert calls == [{"work_dir": "w", "out_dir": "o", "base_name": "課程"}]


def test_process_next_records_started_at_before_running_pipeline():
    # "生成時間長" on the results screen is completed_at - started_at, not
    # created_at (submission time) - started_at, so it reflects actual
    # processing time rather than time spent waiting in the queue.
    seen_started_at = []

    def fake_pipeline(**kwargs):
        seen_started_at.append(manager.get(job_id).started_at)
        return {"mp4": "a"}

    manager = _make_manager(pipeline_fn=fake_pipeline)
    job_id = manager.submit(work_dir="w", out_dir="o")

    before = time.time()
    manager.process_next()
    after = time.time()

    job = manager.get(job_id)
    assert job.started_at is not None
    assert before <= job.started_at <= after
    assert job.started_at <= job.completed_at
    # started_at must already be set by the time the pipeline itself runs,
    # not backfilled afterwards
    assert seen_started_at == [job.started_at]


def test_process_next_marks_error_on_pipeline_error():
    def failing_pipeline(**kwargs):
        raise PipelineError("script generation failed: slide 2")

    manager = _make_manager(pipeline_fn=failing_pipeline)
    job_id = manager.submit(work_dir="w", out_dir="o")

    manager.process_next()

    job = manager.get(job_id)
    assert job.status is JobStatus.ERROR
    assert job.error == "script generation failed: slide 2"
    assert job.result is None


def test_process_next_returns_false_when_queue_empty():
    manager = _make_manager(pipeline_fn=lambda **kwargs: {})
    assert manager.process_next(block=False) is False


def test_jobs_process_in_fifo_order():
    order = []

    def fake_pipeline(**kwargs):
        order.append(kwargs["out_dir"])
        return {}

    manager = _make_manager(pipeline_fn=fake_pipeline)
    manager.submit(work_dir="w1", out_dir="first")
    manager.submit(work_dir="w2", out_dir="second")

    manager.process_next()
    manager.process_next()

    assert order == ["first", "second"]


def test_cleanup_expired_deletes_old_completed_job_dirs(tmp_path):
    work_dir = tmp_path / "work"
    out_dir = tmp_path / "out"
    work_dir.mkdir()
    out_dir.mkdir()
    (out_dir / "課程.mp4").write_bytes(b"fake")

    manager = _make_manager(pipeline_fn=lambda **kwargs: {"mp4": str(out_dir / "課程.mp4")}, retention_seconds=3600)
    job_id = manager.submit(work_dir=str(work_dir), out_dir=str(out_dir))
    manager.process_next()

    future = time.time() + 7200
    manager.cleanup_expired(now=future)

    assert manager.get(job_id) is None
    assert not work_dir.exists()
    assert not out_dir.exists()


def test_cleanup_expired_keeps_jobs_within_retention_window(tmp_path):
    work_dir = tmp_path / "work"
    out_dir = tmp_path / "out"
    work_dir.mkdir()
    out_dir.mkdir()

    manager = _make_manager(pipeline_fn=lambda **kwargs: {}, retention_seconds=3600)
    job_id = manager.submit(work_dir=str(work_dir), out_dir=str(out_dir))
    manager.process_next()

    soon = time.time() + 60
    manager.cleanup_expired(now=soon)

    assert manager.get(job_id) is not None
    assert work_dir.exists()
    assert out_dir.exists()


def test_cleanup_expired_never_deletes_queued_or_running_jobs(tmp_path):
    work_dir = tmp_path / "work"
    out_dir = tmp_path / "out"
    work_dir.mkdir()
    out_dir.mkdir()

    manager = _make_manager(pipeline_fn=lambda **kwargs: {}, retention_seconds=3600)
    job_id = manager.submit(work_dir=str(work_dir), out_dir=str(out_dir))

    far_future = time.time() + 999999
    manager.cleanup_expired(now=far_future)

    job = manager.get(job_id)
    assert job is not None
    assert job.status is JobStatus.QUEUED
    assert work_dir.exists()
