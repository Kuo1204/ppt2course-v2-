import base64
import io
from urllib.parse import quote

from fastapi.testclient import TestClient

from ppt2course.jobs import JobManager
from ppt2course.pipeline import PipelineError
from ppt2course.server import create_app
from ppt2course.tts import TtsError


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


def test_create_job_forwards_voice_rate_and_volume(tmp_path):
    calls = []

    def fake_pipeline(**kwargs):
        calls.append(kwargs)
        return {}

    client, manager = _make_client(fake_pipeline, tmp_path)

    client.post(
        "/api/jobs",
        data=_upload_form(rate="+20%", volume="-10%", font_size="60"),
        files=_upload_files(),
    )
    manager.process_next()

    assert calls[0]["voice_rate"] == "+20%"
    assert calls[0]["voice_volume"] == "-10%"
    assert calls[0]["font_size"] == 60


def test_create_job_forwards_logo_opacity(tmp_path):
    calls = []

    def fake_pipeline(**kwargs):
        calls.append(kwargs)
        return {}

    client, manager = _make_client(fake_pipeline, tmp_path)

    client.post(
        "/api/jobs",
        data=_upload_form(logo_opacity="0.35"),
        files={**_upload_files(), "logo": ("logo.png", io.BytesIO(b"fake-png"), "image/png")},
    )
    manager.process_next()

    assert calls[0]["logo_opacity"] == 0.35
    assert calls[0]["logo_path"] is not None


def test_create_job_defaults_logo_opacity_to_fully_opaque(tmp_path):
    calls = []

    def fake_pipeline(**kwargs):
        calls.append(kwargs)
        return {}

    client, manager = _make_client(fake_pipeline, tmp_path)

    client.post("/api/jobs", data=_upload_form(), files=_upload_files())
    manager.process_next()

    assert calls[0]["logo_opacity"] == 1.0


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


def test_download_file_uses_the_course_name_not_a_generic_browser_filename(tmp_path):
    # FileResponse without an explicit filename= has no Content-Disposition
    # filename, so the browser falls back to a generic name like "mp4 (1).mp4"
    # derived from the URL — not the user's chosen course name.
    out_file = tmp_path / "試作影片.mp4"
    out_file.write_bytes(b"video-bytes")

    def fake_pipeline(**kwargs):
        return {"mp4": str(out_file)}

    client, manager = _make_client(fake_pipeline, tmp_path)
    job_id = client.post("/api/jobs", data=_upload_form(), files=_upload_files()).json()["job_id"]
    manager.process_next()

    response = client.get(f"/api/jobs/{job_id}/download/mp4")
    assert response.status_code == 200
    content_disposition = response.headers["content-disposition"]
    # non-ASCII filenames are RFC 5987 percent-encoded in the header
    assert quote("試作影片.mp4") in content_disposition


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


def test_extract_script_text_from_txt_upload(tmp_path):
    manager = JobManager(pipeline_fn=lambda **kwargs: {}, auto_start=False)
    app = create_app(job_manager=manager, data_root=str(tmp_path / "data"), frontend_dist=None)
    client = TestClient(app)

    response = client.post(
        "/api/extract-script-text",
        files={"file": ("script.txt", io.BytesIO("第1頁\n大家好".encode("utf-8")), "text/plain")},
    )
    assert response.status_code == 200
    assert response.json() == {"text": "第1頁\n大家好"}


def test_extract_script_text_rejects_unsupported_extension(tmp_path):
    manager = JobManager(pipeline_fn=lambda **kwargs: {}, auto_start=False)
    app = create_app(job_manager=manager, data_root=str(tmp_path / "data"), frontend_dist=None)
    client = TestClient(app)

    response = client.post(
        "/api/extract-script-text",
        files={"file": ("script.pdf", io.BytesIO(b"whatever"), "application/pdf")},
    )
    assert response.status_code == 400


def test_voice_preview_returns_audio_and_caches_by_voice(tmp_path):
    calls = []

    def fake_preview(text, voice, rate=None, volume=None):
        calls.append((text, voice, rate, volume))
        return b"fake-mp3-bytes"

    manager = JobManager(pipeline_fn=lambda **kwargs: {}, auto_start=False)
    app = create_app(
        job_manager=manager,
        data_root=str(tmp_path / "data"),
        frontend_dist=None,
        voice_preview_fn=fake_preview,
    )
    client = TestClient(app)

    first = client.get("/api/voice-preview/zh-TW-HsiaoChenNeural")
    assert first.status_code == 200
    assert first.content == b"fake-mp3-bytes"
    assert first.headers["content-type"] == "audio/mpeg"

    second = client.get("/api/voice-preview/zh-TW-HsiaoChenNeural")
    assert second.content == b"fake-mp3-bytes"

    assert len(calls) == 1  # cached on the second request, no repeat synthesis


def test_voice_preview_rejects_unknown_voice(tmp_path):
    manager = JobManager(pipeline_fn=lambda **kwargs: {}, auto_start=False)
    app = create_app(
        job_manager=manager,
        data_root=str(tmp_path / "data"),
        frontend_dist=None,
        voice_preview_fn=lambda text, voice, rate=None, volume=None: b"",
    )
    client = TestClient(app)

    response = client.get("/api/voice-preview/not-a-real-voice")
    assert response.status_code == 400


def test_voice_preview_wraps_tts_error(tmp_path):
    def failing_preview(text, voice, rate=None, volume=None):
        raise TtsError("edge-tts streaming failed: network error")

    manager = JobManager(pipeline_fn=lambda **kwargs: {}, auto_start=False)
    app = create_app(
        job_manager=manager,
        data_root=str(tmp_path / "data"),
        frontend_dist=None,
        voice_preview_fn=failing_preview,
    )
    client = TestClient(app)

    response = client.get("/api/voice-preview/zh-TW-HsiaoChenNeural")
    assert response.status_code == 502


def test_voice_preview_forwards_rate_and_volume_and_caches_separately(tmp_path):
    calls = []

    def fake_preview(text, voice, rate=None, volume=None):
        calls.append((voice, rate, volume))
        return f"{rate}|{volume}".encode()

    manager = JobManager(pipeline_fn=lambda **kwargs: {}, auto_start=False)
    app = create_app(
        job_manager=manager,
        data_root=str(tmp_path / "data"),
        frontend_dist=None,
        voice_preview_fn=fake_preview,
    )
    client = TestClient(app)

    default = client.get("/api/voice-preview/zh-TW-HsiaoChenNeural")
    fast = client.get("/api/voice-preview/zh-TW-HsiaoChenNeural?rate=%2B20%25&volume=-10%25")
    fast_again = client.get("/api/voice-preview/zh-TW-HsiaoChenNeural?rate=%2B20%25&volume=-10%25")

    assert default.content == b"+0%|+0%"
    assert fast.content == b"+20%|-10%"
    assert fast_again.content == b"+20%|-10%"
    assert len(calls) == 2  # default and +20%/-10% each synthesized once, second +20% call cached


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


# ---------- basic auth gate (opt-in, for exposing the app publicly) ----------


def _basic_auth_header(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_no_auth_required_when_not_configured(tmp_path):
    manager = JobManager(pipeline_fn=lambda **kw: None, auto_start=False)
    app = create_app(job_manager=manager, data_root=str(tmp_path / "data"), frontend_dist=None)
    client = TestClient(app)

    response = client.get("/api/jobs/does-not-exist")

    assert response.status_code == 404  # reached the route rather than being blocked by auth


def test_rejects_request_without_credentials_when_auth_configured(tmp_path):
    manager = JobManager(pipeline_fn=lambda **kw: None, auto_start=False)
    app = create_app(
        job_manager=manager,
        data_root=str(tmp_path / "data"),
        frontend_dist=None,
        basic_auth_user="admin",
        basic_auth_password="s3cret",
    )
    client = TestClient(app)

    response = client.get("/api/jobs/does-not-exist")

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_rejects_wrong_credentials(tmp_path):
    manager = JobManager(pipeline_fn=lambda **kw: None, auto_start=False)
    app = create_app(
        job_manager=manager,
        data_root=str(tmp_path / "data"),
        frontend_dist=None,
        basic_auth_user="admin",
        basic_auth_password="s3cret",
    )
    client = TestClient(app)

    response = client.get(
        "/api/jobs/does-not-exist", headers=_basic_auth_header("admin", "wrong-password")
    )

    assert response.status_code == 401


def test_accepts_correct_credentials(tmp_path):
    manager = JobManager(pipeline_fn=lambda **kw: None, auto_start=False)
    app = create_app(
        job_manager=manager,
        data_root=str(tmp_path / "data"),
        frontend_dist=None,
        basic_auth_user="admin",
        basic_auth_password="s3cret",
    )
    client = TestClient(app)

    response = client.get(
        "/api/jobs/does-not-exist", headers=_basic_auth_header("admin", "s3cret")
    )

    assert response.status_code == 404  # past the auth gate, reached the route


def test_auth_gate_also_protects_the_mounted_frontend(tmp_path):
    manager = JobManager(pipeline_fn=lambda **kw: None, auto_start=False)
    frontend_dir = tmp_path / "dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html>app</html>", encoding="utf-8")
    app = create_app(
        job_manager=manager,
        data_root=str(tmp_path / "data"),
        frontend_dist=str(frontend_dir),
        basic_auth_user="admin",
        basic_auth_password="s3cret",
    )
    client = TestClient(app)

    unauthenticated = client.get("/")
    authenticated = client.get("/", headers=_basic_auth_header("admin", "s3cret"))

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200


def test_download_route_is_exempt_from_basic_auth(tmp_path):
    # The mobile-download QR code points straight at this URL. A phone's
    # camera app / QR scanner typically opens it in a lightweight in-app
    # browser that doesn't render a Basic Auth login prompt for a direct
    # file download — the request just silently fails, which is what
    # actually reached us as "can scan the QR but can't download or view".
    # The job id itself is an unguessable UUID, so exempting only this one
    # route (not job creation or the rest of the app) keeps the QR
    # share-a-finished-video flow frictionless without weakening the gate
    # on the parts that cost real compute (uploading, generating).
    out_file = tmp_path / "課程.mp4"
    out_file.write_bytes(b"video-bytes")

    def fake_pipeline(**kwargs):
        return {"mp4": str(out_file)}

    manager = JobManager(pipeline_fn=fake_pipeline, auto_start=False)
    app = create_app(
        job_manager=manager,
        data_root=str(tmp_path / "data"),
        frontend_dist=None,
        basic_auth_user="admin",
        basic_auth_password="s3cret",
    )
    client = TestClient(app)
    job_id = client.post(
        "/api/jobs",
        data=_upload_form(),
        files=_upload_files(),
        headers=_basic_auth_header("admin", "s3cret"),
    ).json()["job_id"]
    manager.process_next()

    response = client.get(f"/api/jobs/{job_id}/download/mp4")  # no credentials

    assert response.status_code == 200
    assert response.content == b"video-bytes"


def test_job_status_and_job_creation_still_require_auth_when_configured(tmp_path):
    manager = JobManager(pipeline_fn=lambda **kw: None, auto_start=False)
    app = create_app(
        job_manager=manager,
        data_root=str(tmp_path / "data"),
        frontend_dist=None,
        basic_auth_user="admin",
        basic_auth_password="s3cret",
    )
    client = TestClient(app)

    assert client.get("/api/jobs/does-not-exist").status_code == 401
    assert client.post("/api/jobs", data=_upload_form(), files=_upload_files()).status_code == 401
