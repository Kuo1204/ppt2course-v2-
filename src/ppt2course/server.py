"""FastAPI web layer: upload a deck + options, poll job status, download results.

Each job gets its own uploads/work/out directory under ``data_root`` so
concurrent (queued) jobs from different users never collide. The Gemini API
key, if supplied, is passed straight through to the pipeline in-memory for
that one job and is never written to a job record or echoed back in any
response.
"""

import base64
import html as html_escape
import json
import os
import re
import secrets
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from ppt2course.jobs import JobManager, JobStatus
from ppt2course.script_extract import ScriptExtractionError, extract_text_from_file
from ppt2course.script_gen import (
    DEFAULT_GEMINI_MODEL,
    ScriptGenerationError,
    ScriptMode,
    generate_script,
)
from ppt2course.tts import DEFAULT_RATE, DEFAULT_VOLUME, TtsError, synthesize_preview
from ppt2course.upload import PptParseError, parse_ppt
from ppt2course.video import (
    DEFAULT_BGM_VOLUME,
    DEFAULT_FONT_SIZE,
    DEFAULT_FPS,
    DEFAULT_LOGO_MARGIN,
    DEFAULT_LOGO_OPACITY,
    DEFAULT_LOGO_WIDTH,
    DEFAULT_RESOLUTION,
    DEFAULT_TRANSITION,
    DEFAULT_TRANSITION_DURATION_MS,
)

DEFAULT_DATA_ROOT = os.environ.get("PPT2COURSE_DATA_ROOT", "data/jobs")
# src/ppt2course/server.py -> parents[2] is the project root, where `frontend/` lives.
DEFAULT_FRONTEND_DIST = os.environ.get(
    "PPT2COURSE_FRONTEND_DIST", str(Path(__file__).resolve().parents[2] / "frontend" / "dist")
)
ALLOWED_DOWNLOAD_TYPES = {"mp4", "srt", "docx"}

# The mobile-download QR code links to /share/{job_id}, which itself embeds
# links to /download and /view. A phone's camera app / QR scanner typically
# opens the link in a lightweight in-app browser that doesn't render a Basic
# Auth login prompt — the request just silently fails there. The job id is an
# unguessable UUID, so exempting only these routes (not job creation, not job
# status, not anything else) keeps "scan the QR, land on a page, choose
# preview or download" frictionless without weakening the gate on the parts
# that actually cost compute.
_DOWNLOAD_TYPES_ALTERNATION = "|".join(re.escape(t) for t in ALLOWED_DOWNLOAD_TYPES)
_AUTH_EXEMPT_ROUTE_PATTERN = re.compile(
    r"^/api/jobs/[^/]+/(?:download|view)/(?:" + _DOWNLOAD_TYPES_ALTERNATION + r")$"
    r"|^/share/[^/]+$"
)


def _is_auth_exempt_route(path: str) -> bool:
    return bool(_AUTH_EXEMPT_ROUTE_PATTERN.match(path))

# Unset by default (no auth) so local dev/tests are unaffected; set both env
# vars to gate the whole app (API + the mounted frontend) behind a shared
# HTTP Basic Auth credential before exposing it beyond the local network.
DEFAULT_BASIC_AUTH_USER = os.environ.get("PPT2COURSE_BASIC_AUTH_USER")
DEFAULT_BASIC_AUTH_PASSWORD = os.environ.get("PPT2COURSE_BASIC_AUTH_PASSWORD")

# Sample text per supported voice, used for the "preview this voice" button.
# Kept as an explicit allow-list (rather than accepting any edge-tts voice
# string) so the preview endpoint can't be used to synthesize arbitrary text
# through an unvetted voice.
VOICE_PREVIEW_SAMPLES = {
    "zh-TW-HsiaoChenNeural": "您好，這是語音預覽範例。",
    "zh-TW-YunJheNeural": "您好，這是語音預覽範例。",
    "zh-CN-XiaoxiaoNeural": "您好，这是语音预览示例。",
    "zh-CN-YunxiNeural": "您好，这是语音预览示例。",
    "en-US-AriaNeural": "Hello, this is a voice preview.",
}


def create_app(
    job_manager: JobManager | None = None,
    data_root: str = DEFAULT_DATA_ROOT,
    frontend_dist: str | None = DEFAULT_FRONTEND_DIST,
    voice_preview_fn=synthesize_preview,
    basic_auth_user: str | None = DEFAULT_BASIC_AUTH_USER,
    basic_auth_password: str | None = DEFAULT_BASIC_AUTH_PASSWORD,
) -> FastAPI:
    app = FastAPI(title="PPT2Course AI")
    app.state.job_manager = job_manager if job_manager is not None else JobManager()
    app.state.data_root = data_root
    app.state.voice_preview_cache = {}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if basic_auth_user and basic_auth_password:

        @app.middleware("http")
        async def require_basic_auth(request: Request, call_next):
            if _is_auth_exempt_route(request.url.path) or _has_valid_basic_auth(
                request.headers.get("authorization", ""), basic_auth_user, basic_auth_password
            ):
                return await call_next(request)
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="PPT2Course AI"'},
            )

    @app.post("/api/jobs")
    async def create_job(
        pptx: UploadFile = File(...),
        images: list[UploadFile] = File(...),
        script_mode: str = Form(...),
        voice: str = Form(...),
        rate: str = Form(DEFAULT_RATE),
        volume: str = Form(DEFAULT_VOLUME),
        base_name: str = Form("課程"),
        texts: str | None = Form(None),
        gemini_api_key: str | None = Form(None),
        gemini_model: str = Form(DEFAULT_GEMINI_MODEL),
        transition: str = Form(DEFAULT_TRANSITION),
        transition_duration_ms: int = Form(DEFAULT_TRANSITION_DURATION_MS),
        resolution_width: int = Form(DEFAULT_RESOLUTION[0]),
        resolution_height: int = Form(DEFAULT_RESOLUTION[1]),
        fps: int = Form(DEFAULT_FPS),
        font_size: int = Form(DEFAULT_FONT_SIZE),
        logo_width: int = Form(DEFAULT_LOGO_WIDTH),
        logo_margin: int = Form(DEFAULT_LOGO_MARGIN),
        logo_opacity: float = Form(DEFAULT_LOGO_OPACITY),
        bgm_volume: float = Form(DEFAULT_BGM_VOLUME),
        logo: UploadFile | None = File(None),
        bgm: UploadFile | None = File(None),
        intro: UploadFile | None = File(None),
        outro: UploadFile | None = File(None),
    ):
        try:
            mode = ScriptMode[script_mode]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"invalid script_mode: {script_mode}")

        parsed_texts = json.loads(texts) if texts else None

        job_id = uuid.uuid4().hex
        job_root = os.path.join(data_root, job_id)
        uploads_dir = os.path.join(job_root, "uploads")
        work_dir = os.path.join(job_root, "work")
        out_dir = os.path.join(job_root, "out")
        os.makedirs(uploads_dir, exist_ok=True)

        pptx_path = os.path.join(uploads_dir, "deck.pptx")
        await _save_upload(pptx, pptx_path)

        image_paths = []
        for i, image in enumerate(images, start=1):
            ext = os.path.splitext(image.filename or "")[1] or ".png"
            image_path = os.path.join(uploads_dir, f"slide_{i:03d}{ext}")
            await _save_upload(image, image_path)
            image_paths.append(image_path)

        logo_path = await _save_optional_upload(logo, uploads_dir, "logo")
        bgm_path = await _save_optional_upload(bgm, uploads_dir, "bgm")
        intro_path = await _save_optional_upload(intro, uploads_dir, "intro")
        outro_path = await _save_optional_upload(outro, uploads_dir, "outro")

        manager: JobManager = app.state.job_manager
        manager.submit(
            job_id=job_id,
            pptx_path=pptx_path,
            image_paths=image_paths,
            work_dir=work_dir,
            out_dir=out_dir,
            base_name=base_name,
            script_mode=mode,
            voice=voice,
            voice_rate=rate,
            voice_volume=volume,
            texts=parsed_texts,
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            transition=transition,
            transition_duration_ms=transition_duration_ms,
            resolution=(resolution_width, resolution_height),
            fps=fps,
            font_size=font_size,
            logo_path=logo_path,
            logo_width=logo_width,
            logo_margin=logo_margin,
            logo_opacity=logo_opacity,
            bgm_path=bgm_path,
            bgm_volume=bgm_volume,
            intro_path=intro_path,
            outro_path=outro_path,
        )
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        manager: JobManager = app.state.job_manager
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")

        if job.status is JobStatus.DONE:
            return {
                "status": "done",
                "downloads": {
                    filetype: f"/api/jobs/{job_id}/download/{filetype}"
                    for filetype in job.result
                    if filetype in ALLOWED_DOWNLOAD_TYPES
                },
            }
        if job.status is JobStatus.ERROR:
            return {"status": "error", "error": job.error}
        return {"status": job.status.value}

    def _resolve_job_file_path(job_id: str, filetype: str) -> str:
        manager: JobManager = app.state.job_manager
        job = manager.get(job_id)
        if job is None or job.status is not JobStatus.DONE:
            raise HTTPException(status_code=404, detail="file not available")
        if filetype not in ALLOWED_DOWNLOAD_TYPES or filetype not in job.result:
            raise HTTPException(status_code=404, detail="file not available")
        return job.result[filetype]

    @app.get("/api/jobs/{job_id}/download/{filetype}")
    def download_job_file(job_id: str, filetype: str):
        path = _resolve_job_file_path(job_id, filetype)
        return FileResponse(path, filename=os.path.basename(path))

    @app.get("/api/jobs/{job_id}/view/{filetype}")
    def view_job_file(job_id: str, filetype: str):
        # Same file as /download, but inline instead of attachment disposition
        # so a <video> tag can stream/scrub it instead of the browser
        # immediately saving it to disk. FileResponse handles Range requests
        # for either disposition, which is what lets video seeking work.
        path = _resolve_job_file_path(job_id, filetype)
        return FileResponse(path, content_disposition_type="inline")

    @app.get("/share/{job_id}", response_class=HTMLResponse)
    def share_job_page(job_id: str):
        manager: JobManager = app.state.job_manager
        job = manager.get(job_id)
        if job is None:
            return HTMLResponse(_render_share_page_not_found(), status_code=404)
        if job.status is JobStatus.DONE:
            return HTMLResponse(_render_share_page_done(job_id, job.result))
        if job.status is JobStatus.ERROR:
            return HTMLResponse(_render_share_page_error(job.error))
        return HTMLResponse(_render_share_page_processing())

    @app.post("/api/extract-script-text")
    async def extract_script_text(file: UploadFile = File(...)):
        content = await file.read()
        try:
            text = extract_text_from_file(file.filename or "", content)
        except ScriptExtractionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"text": text}

    @app.post("/api/generate-script")
    async def generate_script_preview(
        pptx: UploadFile = File(...),
        script_mode: str = Form(...),
        gemini_api_key: str = Form(...),
        gemini_model: str = Form(DEFAULT_GEMINI_MODEL),
        texts: str | None = Form(None),
    ):
        # Runs only the Gemini call, synchronously — not the whole pipeline —
        # so the user can see what the AI actually wrote and choose to
        # continue (or fix it) before ever submitting a job. The key is used
        # for this one call and never written anywhere or echoed back.
        try:
            mode = ScriptMode[script_mode]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"invalid script_mode: {script_mode}")
        if mode not in (ScriptMode.AUTO, ScriptMode.POLISH):
            raise HTTPException(
                status_code=400,
                detail=f"script preview is only available for AUTO/POLISH, got {script_mode}",
            )

        parsed_texts = json.loads(texts) if texts else None

        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as tmp:
            tmp.write(await pptx.read())
            pptx_path = tmp.name
        try:
            try:
                slides = parse_ppt(pptx_path)
            except PptParseError as exc:
                raise HTTPException(status_code=400, detail=f"failed to parse PPT: {exc}") from exc
        finally:
            os.unlink(pptx_path)

        try:
            generated_texts = generate_script(
                mode, slides, texts=parsed_texts, api_key=gemini_api_key, model=gemini_model
            )
        except ScriptGenerationError as exc:
            raise HTTPException(status_code=502, detail=f"script generation failed: {exc}") from exc

        return {"texts": generated_texts}

    @app.get("/api/voice-preview/{voice}")
    def voice_preview(voice: str, rate: str = DEFAULT_RATE, volume: str = DEFAULT_VOLUME):
        sample_text = VOICE_PREVIEW_SAMPLES.get(voice)
        if sample_text is None:
            raise HTTPException(status_code=400, detail=f"unsupported voice: {voice}")

        cache: dict[tuple[str, str, str], bytes] = app.state.voice_preview_cache
        cache_key = (voice, rate, volume)
        if cache_key not in cache:
            try:
                cache[cache_key] = voice_preview_fn(sample_text, voice, rate=rate, volume=volume)
            except TtsError as exc:
                raise HTTPException(status_code=502, detail=f"voice preview failed: {exc}") from exc

        return Response(content=cache[cache_key], media_type="audio/mpeg")

    # Mounted last so the /api/* routes above always match first. Serves the
    # built React SPA (npm run build in frontend/) for a single-deployment setup.
    if frontend_dist and os.path.isdir(frontend_dist):
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app


def _has_valid_basic_auth(header: str, expected_user: str, expected_password: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.removeprefix("Basic ")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, _, password = decoded.partition(":")
    return secrets.compare_digest(user, expected_user) and secrets.compare_digest(
        password, expected_password
    )


_SHARE_PAGE_LABELS = {"mp4": "課程影片 (.mp4)", "srt": "字幕檔 (.srt)", "docx": "逐字稿 (.docx)"}

# Self-contained (no build step, no external assets) so it loads fast on a
# phone straight off a QR scan and still renders if the SPA bundle can't.
# Matches the app's own light/dark tokens for visual consistency.
_SHARE_PAGE_STYLE = """
  :root { --bg:#15130e; --surface:#1f1b14; --border:#3d3626; --text:#ede7d8;
    --text-dim:#9c9385; --accent:#e8a33d; --state-error:#e8573f; }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f5f1e6; --surface:#ffffff; --border:#ddd5c0; --text:#211d14;
      --text-dim:#6b6355; --accent:#b8791f; --state-error:#c13a24; }
  }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    padding:24px; background:var(--bg); color:var(--text);
    font-family:-apple-system,"Segoe UI",Arial,sans-serif; }
  .card { width:100%; max-width:420px; background:var(--surface); border:1px solid var(--border);
    border-radius:8px; padding:24px; text-align:center; }
  h1 { font-size:17px; margin:0 0 16px; }
  video { width:100%; border-radius:6px; background:#000; margin-bottom:18px; }
  .btn { display:block; width:100%; padding:14px; margin-bottom:10px; border-radius:4px;
    font-weight:700; text-decoration:none; box-sizing:border-box; }
  .btn-primary { background:var(--accent); color:var(--bg); }
  .btn-secondary { background:transparent; color:var(--text-dim); border:1px solid var(--border); }
  p { color:var(--text-dim); font-size:14px; line-height:1.6; }
  .error { color:var(--state-error); }
"""


def _share_page_shell(body: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>PPT2Course AI</title>"
        f"<style>{_SHARE_PAGE_STYLE}</style></head>"
        f"<body><div class=\"card\">{body}</div></body></html>"
    )


def _render_share_page_done(job_id: str, result: dict) -> str:
    video_html = (
        f'<video controls playsinline preload="metadata" '
        f'src="/api/jobs/{job_id}/view/mp4"></video>'
        if "mp4" in result
        else ""
    )
    buttons = []
    if "mp4" in result:
        buttons.append(
            f'<a class="btn btn-primary" href="/api/jobs/{job_id}/download/mp4" download>下載影片</a>'
        )
    for filetype in ("srt", "docx"):
        if filetype in result:
            buttons.append(
                f'<a class="btn btn-secondary" href="/api/jobs/{job_id}/download/{filetype}" download>'
                f"下載{_SHARE_PAGE_LABELS[filetype]}</a>"
            )
    return _share_page_shell(
        f"<h1>課程影片已完成</h1>{video_html}{''.join(buttons)}"
    )


def _render_share_page_processing() -> str:
    body = (
        "<h1>影片製作中</h1>"
        "<p>還在處理中，請稍後再試，這個畫面會自動重新整理。</p>"
    )
    # No-JS-required auto-retry: plain <meta refresh> works even in the
    # bare-bones in-app browser a QR scanner opens.
    return (
        "<!doctype html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        '<meta http-equiv="refresh" content="5">'
        "<title>PPT2Course AI</title>"
        f"<style>{_SHARE_PAGE_STYLE}</style></head>"
        f"<body><div class=\"card\">{body}</div></body></html>"
    )


def _render_share_page_error(error: str | None) -> str:
    safe_error = html_escape.escape(error or "未知錯誤")
    return _share_page_shell(
        f'<h1>影片製作失敗</h1><p class="error">{safe_error}</p>'
    )


def _render_share_page_not_found() -> str:
    return _share_page_shell(
        "<h1>找不到這支影片</h1>"
        "<p>連結可能已經過期(產出的檔案只保留 24 小時)，或網址不正確，請確認後再試一次。</p>"
    )


async def _save_upload(upload: UploadFile, dest_path: str) -> None:
    with open(dest_path, "wb") as f:
        f.write(await upload.read())


async def _save_optional_upload(upload: UploadFile | None, uploads_dir: str, base_name: str) -> str | None:
    if upload is None or not upload.filename:
        return None
    ext = os.path.splitext(upload.filename)[1] or ""
    path = os.path.join(uploads_dir, f"{base_name}{ext}")
    await _save_upload(upload, path)
    return path


app = create_app()
