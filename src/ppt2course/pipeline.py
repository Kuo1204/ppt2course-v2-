"""Pipeline orchestration — wires STEP1 through STEP6 into one call.

.pptx + slide images in, {base_name}.mp4 / .srt / .docx out. Any failure
along the way aborts immediately with a clear PipelineError rather than
falling back to a partial result — consistent with every module's own
"raise, don't fail silently" behavior.
"""

import os
import subprocess

from ppt2course.export import ExportError, export_outputs
from ppt2course.script_cleaner import clean_script
from ppt2course.script_gen import (
    DEFAULT_GEMINI_MODEL,
    ScriptGenerationError,
    ScriptMode,
    generate_script,
)
from ppt2course.tts import DEFAULT_RATE, DEFAULT_VOLUME, TtsError, synthesize
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
    SlideVideoInput,
    VideoComposeError,
    compose_video,
)

DEFAULT_SILENT_DURATION_MS = 2000


class PipelineError(Exception):
    pass


def _generate_silent_audio(out_path: str, duration_ms: int) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
        "-t", str(duration_ms / 1000),
        "-q:a", "9",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise PipelineError(f"failed to generate silent placeholder audio: {result.stderr}")


def run_pipeline(
    pptx_path: str,
    image_paths: list[str],
    work_dir: str,
    out_dir: str,
    base_name: str,
    script_mode: ScriptMode,
    voice: str,
    voice_rate: str = DEFAULT_RATE,
    voice_volume: str = DEFAULT_VOLUME,
    texts: list[str] | None = None,
    gemini_api_key: str | None = None,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    transition: str = DEFAULT_TRANSITION,
    transition_duration_ms: int = DEFAULT_TRANSITION_DURATION_MS,
    resolution: tuple[int, int] = DEFAULT_RESOLUTION,
    fps: int = DEFAULT_FPS,
    font_size: int = DEFAULT_FONT_SIZE,
    logo_path: str | None = None,
    logo_width: int = DEFAULT_LOGO_WIDTH,
    logo_margin: int = DEFAULT_LOGO_MARGIN,
    logo_opacity: float = DEFAULT_LOGO_OPACITY,
    bgm_path: str | None = None,
    bgm_volume: float = DEFAULT_BGM_VOLUME,
    intro_path: str | None = None,
    outro_path: str | None = None,
) -> dict[str, str]:
    try:
        slides = parse_ppt(pptx_path)
    except PptParseError as exc:
        raise PipelineError(f"failed to parse PPT: {exc}") from exc

    if len(image_paths) != len(slides):
        raise PipelineError(
            f"image count ({len(image_paths)}) does not match slide count ({len(slides)})"
        )

    try:
        scripts = generate_script(
            script_mode, slides, texts=texts, api_key=gemini_api_key, model=gemini_model
        )
    except ScriptGenerationError as exc:
        raise PipelineError(f"script generation failed: {exc}") from exc

    cleaned_scripts = [clean_script(s) for s in scripts]

    os.makedirs(work_dir, exist_ok=True)

    slide_inputs = []
    for slide, image_path, script_text in zip(slides, image_paths, cleaned_scripts):
        audio_path = os.path.join(work_dir, f"slide_{slide.index:03d}.mp3")

        if script_text.strip():
            try:
                chunks = synthesize(script_text, voice, audio_path, rate=voice_rate, volume=voice_volume)
            except TtsError as exc:
                raise PipelineError(
                    f"TTS synthesis failed for slide {slide.index}: {exc}"
                ) from exc
        else:
            _generate_silent_audio(audio_path, DEFAULT_SILENT_DURATION_MS)
            chunks = []

        slide_inputs.append(
            SlideVideoInput(image_path=image_path, audio_path=audio_path, chunks=chunks)
        )

    video_path = os.path.join(work_dir, "course.mp4")
    srt_path = os.path.join(work_dir, "course.srt")

    try:
        compose_video(
            slide_inputs,
            video_path,
            srt_path,
            transition=transition,
            transition_duration_ms=transition_duration_ms,
            resolution=resolution,
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
    except VideoComposeError as exc:
        raise PipelineError(f"video composition failed: {exc}") from exc

    try:
        return export_outputs(video_path, srt_path, cleaned_scripts, out_dir, base_name)
    except ExportError as exc:
        raise PipelineError(f"export failed: {exc}") from exc
