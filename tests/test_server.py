import io

from fastapi.testclient import TestClient

from ppt2course.jobs import JobManager
from ppt2course.pipeline import PipelineError
from ppt2course.server import create_app


def _make_client(pipeline_fn, tmp_path):
    manager = JobManager(pipeline_fn=pipeline_fn, auto_start=False)
    app = create_app(job_manager=manager, data_root=str(tmp_path / "data"), frontend_dist=None)
    return TestClient(app), manager


def _upload_files():
    return {
        "pptx": ("deck.pptx", io.BytesIO(b"fake-pptx-bytes"), "application/octet-stream"),
        "images": ("slide1.png", io.BytesIO(b"fake-png-bytes"), "image/png"),
    }


def _upload_form(**overrides):
    form = {"script_mode": "NOTES", "voice": "zh-TW-HsiaoChenNeural"}
    form.update(overrides)
    return form


def test_create_job_saves_uploads_and_returns_job_id(tmp_path):
    calls = []

    def fake_pipeline(**kwargs):
        calls.append(kwargs)
        return {"mp4": "課程.mp4", "srt": "課程.srt", "docx": "課程.docx"}

    client, manager = _make_client(fake_pipeline, tmp_path)

    response = client.post("/api/jobs", data=_upload_form(), files=_upload_files())

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert job_id

    manager.process_next()

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["pptx_path"].endswith("deck.pptx")
    import os
    assert os.path.exists(kwargs["pptx_path"])
    assert len(kwargs["image_paths"]) == 1
    assert os.path.exists(kwargs["image_paths"][0])


def test_get_job_status_queued_before_processing(tmp_path):
    client, manager = _make_client(lambda **kwargs: {}, tmp_path)

    job_id = client.post("/api/jobs", data=_upload_form(), files=_upload_files()).json()["job_id"]

    response = client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}


def test_get_job_status_done_includes_download_links(tmp_path):
    def fake_pipeline(**kwargs):
        return {"mp4": "課程.mp4", "srt": "課程.srt", "docx": "課程.docx"}

    client, manager = _make_client(fake_pipeline, tmp_path)
    job_id = client.post("/api/jobs", data=_upload_form(), files=_upload_files()).json()["job_id"]
    manager.process_next()

    response = client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert body["downloads"] == {
        "mp4": f"/api/jobs/{job_id}/download/mp4",
        "srt": f"/api/jobs/{job_id}/download/srt",
        "docx": f"/api/jobs/{job_id}/download/docx",
    }


def test_get_job_status_error_includes_message(tmp_path):
    def failing_pipeline(**kwargs):
        raise PipelineError("TTS synthesis failed for slide 1: network error")

    client, manager = _make_client(failing_pipeline, tmp_path)
    job_id = client.post("/api/jobs", data=_upload_form(), files=_upload_files()).json()["job_id"]
    manager.process_next()

    response = client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json() == {
        "status": "error",
        "error": "TTS synthesis failed for slide 1: network error",
    }


def test_get_unknown_job_returns_404(tmp_path):
    client, _ = _make_client(lambda **kwargs: {}, tmp_path)
    response = client.get("/api/jobs/does-not-exist")
    assert response.status_code == 404


def test_invalid_script_mode_returns_400(tmp_path):
    client, _ = _make_client(lambda **kwargs: {}, tmp_path)
    response = client.post(
        "/api/jobs", data=_upload_form(script_mode="NOT_A_MODE"), files=_upload_files()
    )
    assert response.status_code == 400


def test_download_file_after_done(tmp_path):
    out_file = tmp_path / "課程.mp4"
    out_file.write_bytes(b"video-bytes")

    def fake_pipeline(**kwargs):
        return {"mp4": str(out_file)}

    client, manager = _make_client(fake_pipeline, tmp_path)
    job_id = client.post("/api/jobs", data=_upload_form(), files=_upload_files()).json()["job_id"]
    manager.process_next()

    response = client.get(f"/api/jobs/{job_id}/download/mp4")
    assert response.status_code == 200
    assert response.content == b"video-bytes"


def test_download_before_done_returns_404(tmp_path):
    client, manager = _make_client(lambda **kwargs: {"mp4": "x"}, tmp_path)
    job_id = client.post("/api/jobs", data=_upload_form(), files=_upload_files()).json()["job_id"]

    response = client.get(f"/api/jobs/{job_id}/download/mp4")
    assert response.status_code == 404


def test_download_unknown_filetype_returns_404(tmp_path):
    def fake_pipeline(**kwargs):
        return {"mp4": "x", "srt": "y", "docx": "z"}

    client, manager = _make_client(fake_pipeline, tmp_path)
    job_id = client.post("/api/jobs", data=_upload_form(), files=_upload_files()).json()["job_id"]
    manager.process_next()

    response = client.get(f"/api/jobs/{job_id}/download/exe")
    assert response.status_code == 404


def test_serves_built_frontend_index_at_root(tmp_path):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>PPT2COURSE_FRONTEND</html>", encoding="utf-8")

    manager = JobManager(pipeline_fn=lambda **kwargs: {}, auto_start=False)
    app = create_app(
        job_manager=manager, data_root=str(tmp_path / "data"), frontend_dist=str(dist_dir)
    )
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    assert "PPT2COURSE_FRONTEND" in response.text


def test_serves_built_frontend_static_assets(tmp_path):
    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('hi')", encoding="utf-8")

    manager = JobManager(pipeline_fn=lambda **kwargs: {}, auto_start=False)
    app = create_app(
        job_manager=manager, data_root=str(tmp_path / "data"), frontend_dist=str(dist_dir)
    )
    client = TestClient(app)

    response = client.get("/assets/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_api_routes_still_work_when_frontend_is_mounted(tmp_path):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    manager = JobManager(pipeline_fn=lambda **kwargs: {}, auto_start=False)
    app = create_app(
        job_manager=manager, data_root=str(tmp_path / "data"), frontend_dist=str(dist_dir)
    )
    client = TestClient(app)

    response = client.get("/api/jobs/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"detail": "job not found"}


def test_missing_frontend_dist_does_not_crash_app_creation(tmp_path):
    manager = JobManager(pipeline_fn=lambda **kwargs: {}, auto_start=False)
    app = create_app(
        job_manager=manager,
        data_root=str(tmp_path / "data"),
        frontend_dist=str(tmp_path / "no-such-dir"),
    )
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 404


def test_gemini_api_key_reaches_pipeline_but_never_echoed_in_status(tmp_path):
    calls = []

    def fake_pipeline(**kwargs):
        calls.append(kwargs)
        return {}

    client, manager = _make_client(fake_pipeline, tmp_path)
    job_id = client.post(
        "/api/jobs",
        data=_upload_form(script_mode="AUTO", gemini_api_key="super-secret-key"),
        files=_upload_files(),
    ).json()["job_id"]
    manager.process_next()

    assert calls[0]["gemini_api_key"] == "super-secret-key"

    response = client.get(f"/api/jobs/{job_id}")
    assert "super-secret-key" not in response.text
