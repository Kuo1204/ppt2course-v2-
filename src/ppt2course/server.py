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
from dataclasses import asdict
from pathlib import Path
# Aliased: fastapi.Request (imported below, used for the ASGI request in the
# Basic Auth middleware) would otherwise silently shadow this one.
from urllib.request import Request as UrllibRequest
from urllib.request import urlopen

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from ppt2course.avatar import (
    AVATAR_MODES,
    DEFAULT_AVATAR_MODE,
)
from ppt2course.jobs import JobManager, JobStatus
from ppt2course.media_search import DEFAULT_CANDIDATE_LIMIT, search_pexels
from ppt2course.pptx_preview import DEFAULT_THUMBNAIL_WIDTH, PptxPreviewError, render_pptx_thumbnails
from ppt2course.script_extract import ScriptExtractionError, extract_text_from_file
from ppt2course.script_gen import (
    DEFAULT_GEMINI_MODEL,
    ScriptGenerationError,
    ScriptMode,
    generate_script,
)
from ppt2course.timeline_models import VisualAssetType
from ppt2course.tts import DEFAULT_RATE, DEFAULT_VOLUME, TtsError, synthesize, synthesize_preview
from ppt2course.upload import PptParseError, parse_ppt
from ppt2course.video import (
    AVATAR_POSITIONS,
    AVATAR_SIZES,
    DEFAULT_AVATAR_MARGIN,
    DEFAULT_AVATAR_POSITION,
    DEFAULT_AVATAR_SIZE,
    DEFAULT_BGM_VOLUME,
    DEFAULT_FONT_SIZE,
    DEFAULT_FPS,
    DEFAULT_LOGO_MARGIN,
    DEFAULT_LOGO_OPACITY,
    DEFAULT_LOGO_POSITION,
    DEFAULT_LOGO_WIDTH,
    DEFAULT_RESOLUTION,
    DEFAULT_SUBTITLE_FONT_COLOR,
    DEFAULT_SUBTITLE_MARGIN_V,
    DEFAULT_SUBTITLE_OUTLINE_COLOR,
    DEFAULT_TRANSITION,
    DEFAULT_TRANSITION_DURATION_MS,
    LOGO_POSITIONS,
)
from ppt2course.visual_analyzer import (
    DEFAULT_BROLL_DURATION_MS,
    DEFAULT_VISUAL_MODEL,
    analyze_visual_needs,
    suggest_broll_window_ms,
)

DEFAULT_DATA_ROOT = os.environ.get("PPT2COURSE_DATA_ROOT", "data/jobs")
# src/ppt2course/server.py -> parents[2] is the project root, where `frontend/` lives.
DEFAULT_FRONTEND_DIST = os.environ.get(
    "PPT2COURSE_FRONTEND_DIST", str(Path(__file__).resolve().parents[2] / "frontend" / "dist")
)
ALLOWED_DOWNLOAD_TYPES = {"mp4", "srt", "docx"}
_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

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
        subtitle_margin_v: int = Form(DEFAULT_SUBTITLE_MARGIN_V),
        subtitle_font_color: str = Form(DEFAULT_SUBTITLE_FONT_COLOR),
        subtitle_outline_color: str = Form(DEFAULT_SUBTITLE_OUTLINE_COLOR),
        logo_width: int = Form(DEFAULT_LOGO_WIDTH),
        logo_margin: int = Form(DEFAULT_LOGO_MARGIN),
        logo_opacity: float = Form(DEFAULT_LOGO_OPACITY),
        logo_position: str = Form(DEFAULT_LOGO_POSITION),
        bgm_volume: float = Form(DEFAULT_BGM_VOLUME),
        custom_dict: str | None = Form(None),
        logo: UploadFile | None = File(None),
        bgm: UploadFile | None = File(None),
        intro: UploadFile | None = File(None),
        outro: UploadFile | None = File(None),
        broll_selections: str | None = Form(None),
        avatar_mode: str = Form(DEFAULT_AVATAR_MODE),
        avatar_position: str = Form(DEFAULT_AVATAR_POSITION),
        avatar_size: str = Form(DEFAULT_AVATAR_SIZE),
        avatar_margin: int = Form(DEFAULT_AVATAR_MARGIN),
        avatar_custom_slides: str | None = Form(None),
        reading_pause_ms: int = Form(0),
        closing_pause_ms: int = Form(0),
        target_duration_ms: int | None = Form(None),
        avoid_voice_overlap: bool = Form(False),
    ):
        try:
            mode = ScriptMode[script_mode]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"invalid script_mode: {script_mode}")

        if logo_position not in LOGO_POSITIONS:
            raise HTTPException(
                status_code=400, detail=f"invalid logo_position: {logo_position}"
            )

        if avatar_mode not in AVATAR_MODES:
            raise HTTPException(status_code=400, detail=f"invalid avatar_mode: {avatar_mode}")
        if avatar_position not in AVATAR_POSITIONS:
            raise HTTPException(
                status_code=400, detail=f"invalid avatar_position: {avatar_position}"
            )
        if avatar_size not in AVATAR_SIZES:
            raise HTTPException(status_code=400, detail=f"invalid avatar_size: {avatar_size}")

        if not _HEX_COLOR_RE.match(subtitle_font_color):
            raise HTTPException(
                status_code=400, detail=f"invalid subtitle_font_color: {subtitle_font_color}"
            )
        if not _HEX_COLOR_RE.match(subtitle_outline_color):
            raise HTTPException(
                status_code=400, detail=f"invalid subtitle_outline_color: {subtitle_outline_color}"
            )

        if reading_pause_ms < 0:
            raise HTTPException(status_code=400, detail="reading_pause_ms must be >= 0")
        if closing_pause_ms < 0:
            raise HTTPException(status_code=400, detail="closing_pause_ms must be >= 0")
        if target_duration_ms is not None and target_duration_ms <= 0:
            raise HTTPException(status_code=400, detail="target_duration_ms must be > 0")

        parsed_texts = json.loads(texts) if texts else None
        try:
            parsed_avatar_custom_slides = (
                json.loads(avatar_custom_slides) if avatar_custom_slides else []
            )
            if not isinstance(parsed_avatar_custom_slides, list) or not all(
                isinstance(n, int) for n in parsed_avatar_custom_slides
            ):
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=400, detail="avatar_custom_slides must be a JSON array of integers"
            ) from None

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

        custom_dict_path = None
        if custom_dict and custom_dict.strip():
            custom_dict_path = os.path.join(uploads_dir, "custom_dict.txt")
            with open(custom_dict_path, "w", encoding="utf-8") as f:
                f.write(custom_dict)

        resolved_broll_selections = _download_broll_selections(broll_selections, uploads_dir)

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
            subtitle_margin_v=subtitle_margin_v,
            subtitle_font_color=subtitle_font_color,
            subtitle_outline_color=subtitle_outline_color,
            logo_path=logo_path,
            logo_width=logo_width,
            logo_margin=logo_margin,
            logo_opacity=logo_opacity,
            logo_position=logo_position,
            bgm_path=bgm_path,
            bgm_volume=bgm_volume,
            intro_path=intro_path,
            outro_path=outro_path,
            custom_dict_path=custom_dict_path,
            broll_selections=resolved_broll_selections,
            avatar_mode=avatar_mode,
            avatar_position=avatar_position,
            avatar_size=avatar_size,
            avatar_margin=avatar_margin,
            avatar_custom_slides=parsed_avatar_custom_slides,
            reading_pause_ms=reading_pause_ms,
            closing_pause_ms=closing_pause_ms,
            target_duration_ms=target_duration_ms,
            avoid_voice_overlap=avoid_voice_overlap,
        )
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        manager: JobManager = app.state.job_manager
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")

        if job.status is JobStatus.DONE:
            generation_seconds = None
            if job.started_at is not None and job.completed_at is not None:
                generation_seconds = round(job.completed_at - job.started_at, 1)
            return {
                "status": "done",
                "downloads": {
                    filetype: f"/api/jobs/{job_id}/download/{filetype}"
                    for filetype in job.result
                    if filetype in ALLOWED_DOWNLOAD_TYPES
                },
                # 影片容量/時長/講稿字數/生成時間長 — shown on the results
                # screen once a job finishes. The first three come straight
                # from run_pipeline's result (see pipeline.py); generation
                # time is Job.started_at -> completed_at, i.e. actual
                # processing time, not time spent waiting in the queue.
                "details": {
                    "video_size_bytes": job.result.get("video_size_bytes"),
                    "video_duration_ms": job.result.get("video_duration_ms"),
                    "script_char_count": job.result.get("script_char_count"),
                    "generation_seconds": generation_seconds,
                    # Only present when target_duration_ms was requested;
                    # False means the target was shorter than the deck's
                    # real narration-only length already produces — see
                    # pipeline.py's _reading_pauses_for_target_duration.
                    "target_duration_reachable": job.result.get("target_duration_reachable"),
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

    @app.post("/api/pptx-preview")
    async def pptx_preview(pptx: UploadFile = File(...)):
        # Mirrors /api/generate-script's ephemeral-tempfile pattern: the
        # upload is written to disk only long enough for LibreOffice to read
        # it, then deleted — nothing from this preview-only call persists.
        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as tmp:
            tmp.write(await pptx.read())
            pptx_path = tmp.name
        try:
            try:
                thumbnails = render_pptx_thumbnails(pptx_path, thumbnail_width=DEFAULT_THUMBNAIL_WIDTH)
            except PptxPreviewError as exc:
                raise HTTPException(status_code=502, detail=f"failed to render PPTX preview: {exc}") from exc
        finally:
            os.unlink(pptx_path)

        data_urls = [
            "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
            for png_bytes in thumbnails
        ]
        return {"thumbnails": data_urls}

    @app.post("/api/pptx-notes")
    async def pptx_notes(pptx: UploadFile = File(...)):
        # NOTES mode uses each slide's speaker-notes text verbatim as the
        # narration script — this lets the user see that text before ever
        # submitting a job, instead of only finding out what got narrated
        # after the video is already rendered. Mirrors /api/pptx-preview's
        # ephemeral-tempfile pattern: nothing from this preview-only call
        # persists.
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

        return {"notes": [slide.notes for slide in slides]}

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

    @app.post("/api/analyze-visuals")
    async def analyze_visuals(
        pptx: UploadFile = File(...),
        texts: str = Form(...),
        voice: str = Form(...),
        voice_rate: str = Form(DEFAULT_RATE),
        voice_volume: str = Form(DEFAULT_VOLUME),
        gemini_api_key: str | None = Form(None),
        gemini_model: str = Form(DEFAULT_VISUAL_MODEL),
    ):
        # Preview-only, like /api/generate-script: runs the (optional) Gemini
        # call synchronously and returns advice for the user to accept or
        # reject. Never touches compose_video — this endpoint cannot change
        # any job's video/subtitle timing, only suggest slides that might
        # benefit from an added visual (and where, see
        # _suggest_broll_timing below).
        scripts = json.loads(texts)

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

        if len(scripts) != len(slides):
            raise HTTPException(
                status_code=400,
                detail=f"texts length ({len(scripts)}) does not match slide count ({len(slides)})",
            )

        result = analyze_visual_needs(slides, scripts, api_key=gemini_api_key, model=gemini_model)

        # Keyed by slide_number rather than zipped by position — safer than
        # assuming result.recommendations comes back in the same order as
        # slides/scripts (heuristic mode does; AI mode only guarantees a
        # slide_number-sorted order, which happens to match today but
        # shouldn't be relied on here).
        scripts_by_slide_number = {slide.index: script for slide, script in zip(slides, scripts)}
        recommendations_payload = []
        for rec in result.recommendations:
            # synthesize() (called inside _suggest_broll_timing) runs its own
            # asyncio.run() internally, which cannot nest inside the event
            # loop this route is already running on — offload to a thread
            # pool thread, the same way pipeline.py's background worker
            # thread already gives synthesize() a loop-free thread to do
            # that in.
            start_ms, end_ms = await run_in_threadpool(
                _suggest_broll_timing,
                scripts_by_slide_number.get(rec.slide_number, ""),
                rec,
                voice=voice,
                rate=voice_rate,
                volume=voice_volume,
            )
            payload = asdict(rec)
            payload["suggested_start_ms"] = start_ms
            payload["suggested_end_ms"] = end_ms
            recommendations_payload.append(payload)

        return {
            "recommendations": recommendations_payload,
            "warnings": list(result.warnings),
            "used_ai": result.used_ai,
        }

    @app.get("/api/media-search")
    def media_search(
        keyword: str,
        slide_number: int,
        media_type: str = "image",
        pexels_api_key: str | None = None,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ):
        # search_pexels already turns every failure mode (no key, no results,
        # network/HTTP errors) into a warning instead of raising, so the only
        # thing this route itself validates is the media_type enum value.
        try:
            asset_type = VisualAssetType(media_type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid media_type: {media_type}")

        result = search_pexels(
            keyword,
            slide_number,
            media_type=asset_type,
            api_key=pexels_api_key,
            limit=limit,
        )
        return {
            "assets": [asdict(item) for item in result.assets],
            "warning": result.warning,
        }

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


def _suggest_broll_timing(script: str, recommendation, voice: str, rate: str, volume: str) -> tuple[int, int]:
    """Real-timing basis for "AI 自動抓時間點": synthesizes this one slide's
    script (throwaway audio, deleted immediately) to get the same
    WordBoundary-aligned TimedChunk list the real job would produce, then
    hands it to suggest_broll_window_ms() to place the window against
    ``recommendation.script_anchor``'s *actual* narration timing.

    Only recommended slides with a non-empty anchor pay for a TTS call —
    everything else gets the fixed default window without touching the
    network. Never raises: a TTS failure here (still fully optional/
    advisory) just falls back to the same default any other slide gets.
    """
    if not recommendation.recommended or not recommendation.script_anchor:
        return (0, DEFAULT_BROLL_DURATION_MS)

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        audio_tmp_path = tmp.name
    try:
        chunks = synthesize(script, voice, audio_tmp_path, rate=rate, volume=volume)
        return suggest_broll_window_ms(script, recommendation.script_anchor, chunks)
    except TtsError:
        return (0, DEFAULT_BROLL_DURATION_MS)
    finally:
        if os.path.exists(audio_tmp_path):
            os.unlink(audio_tmp_path)


def _download_broll_selections(raw_json: str | None, uploads_dir: str) -> list[dict]:
    """Parse the client's confirmed B-roll picks and materialize each one's
    image into a local file pipeline.py can hand to ffmpeg.

    Never raises: a malformed entry or a failed download just drops that one
    overlay rather than the whole job — B-roll must stay optional, the same
    "never let this add-on break core video generation" guarantee
    media_search.py already gives its own callers for search failures.
    """
    if not raw_json:
        return []
    try:
        raw_selections = json.loads(raw_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(raw_selections, list):
        return []

    resolved: list[dict] = []
    for i, selection in enumerate(raw_selections):
        if not isinstance(selection, dict):
            continue
        try:
            slide_number = int(selection["slide_number"])
            download_url_value = str(selection["download_url"])
            start_ms = int(selection["start_ms"])
            end_ms = int(selection["end_ms"])
        except (KeyError, TypeError, ValueError):
            continue

        ext = os.path.splitext(download_url_value.split("?")[0])[1][:5] or ".jpg"
        image_path = os.path.join(uploads_dir, f"broll_{slide_number}_{i}{ext}")
        try:
            request = UrllibRequest(
                download_url_value, headers={"User-Agent": "PPT2Course-AI/0.1"}
            )
            with urlopen(request, timeout=15.0) as response:
                data = response.read()
            with open(image_path, "wb") as f:
                f.write(data)
        except Exception:
            continue

        resolved.append(
            {
                "slide_number": slide_number,
                "image_path": image_path,
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )
    return resolved


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
