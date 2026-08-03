"""FastAPI web layer: upload a deck + options, poll job status, download results.

Each job gets its own uploads/work/out directory under ``data_root`` so
concurrent (queued) jobs from different users never collide. The Gemini API
key, if supplied, is passed straight through to the pipeline in-memory for
that one job and is never written to a job record or echoed back in any
response.
"""

import json
import os
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from ppt2course.jobs import JobManager, JobStatus
from ppt2course.script_gen import DEFAULT_GEMINI_MODEL, ScriptMode
from ppt2course.video import (
    DEFAULT_BGM_VOLUME,
    DEFAULT_FONT_SIZE,
    DEFAULT_FPS,
    DEFAULT_LOGO_MARGIN,
    DEFAULT_LOGO_WIDTH,
    DEFAULT_RESOLUTION,
    DEFAULT_TRANSITION,
    DEFAULT_TRANSITION_DURATION_MS,
)

DEFAULT_DATA_ROOT = os.environ.get("PPT2COURSE_DATA_ROOT", "data/jobs")
ALLOWED_DOWNLOAD_TYPES = {"mp4", "srt", "docx"}


def create_app(job_manager: JobManager | None = None, data_root: str = DEFAULT_DATA_ROOT) -> FastAPI:
    app = FastAPI(title="PPT2Course AI")
    app.state.job_manager = job_manager if job_manager is not None else JobManager()
    app.state.data_root = data_root

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/api/jobs")
    async def create_job(
        pptx: UploadFile = File(...),
        images: list[UploadFile] = File(...),
        script_mode: str = Form(...),
        voice: str = Form(...),
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

    @app.get("/api/jobs/{job_id}/download/{filetype}")
    def download_job_file(job_id: str, filetype: str):
        manager: JobManager = app.state.job_manager
        job = manager.get(job_id)
        if job is None or job.status is not JobStatus.DONE:
            raise HTTPException(status_code=404, detail="file not available")
        if filetype not in ALLOWED_DOWNLOAD_TYPES or filetype not in job.result:
            raise HTTPException(status_code=404, detail="file not available")
        return FileResponse(job.result[filetype])

    return app


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
