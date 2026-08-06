"""Pipeline orchestration — wires STEP1 through STEP6 into one call.

.pptx + slide images in, {base_name}.mp4 / .srt / .docx out. Any failure
along the way aborts immediately with a clear PipelineError rather than
falling back to a partial result — consistent with every module's own
"raise, don't fail silently" behavior.
"""

import os
import subprocess

from ppt2course.audio_duration import get_audio_duration_ms
from ppt2course.export import ExportError, export_outputs
from ppt2course.script_cleaner import clean_script
from ppt2course.script_gen import (
    DEFAULT_GEMINI_MODEL,
    ScriptGenerationError,
    ScriptMode,
    generate_script,
)
from ppt2course.subtitle import TimedChunk, split_into_sentences
from ppt2course.tts import DEFAULT_RATE, DEFAULT_VOLUME, TtsError, synthesize
from ppt2course.upload import PptParseError, parse_ppt
from ppt2course.video import (
    DEFAULT_BGM_VOLUME,
    DEFAULT_FONT_SIZE,
    DEFAULT_FPS,
    DEFAULT_LOGO_MARGIN,
    DEFAULT_LOGO_OPACITY,
    DEFAULT_LOGO_POSITION,
    DEFAULT_LOGO_WIDTH,
    DEFAULT_RESOLUTION,
    DEFAULT_SUBTITLE_MARGIN_V,
    DEFAULT_TRANSITION,
    DEFAULT_TRANSITION_DURATION_MS,
    SlideVideoInput,
    VideoComposeError,
    compose_video,
)

DEFAULT_SILENT_DURATION_MS = 2000

# A deliberate, controllable breath between sentences. edge-tts synthesizes
# a whole slide's script as one continuous utterance — its neural voices do
# pause somewhat at 。！？ on their own, but not enough on their own to read
# as a person taking a breath between sentences, especially across several
# short sentences in a row. edge-tts also has no real way to ask for more:
# it XML-escapes whatever text it's given before wrapping it in its own
# internal SSML envelope, so a literal "<break/>" tag in the script would
# just get spoken as literal text, not interpreted as a pause. Instead,
# _synthesize_with_sentence_pauses synthesizes each sentence as its own
# clip and physically splices in a silent gap of this length between them.
DEFAULT_SENTENCE_PAUSE_MS = 300


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


def _concatenate_audio_segments(segment_paths: list[str], out_path: str) -> None:
    list_path = f"{out_path}.concat_list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in segment_paths:
            # ffmpeg's concat demuxer treats this like a small shell dialect:
            # a literal single quote inside a 'path' has to be closed,
            # escaped, and reopened.
            escaped = os.path.abspath(p).replace("'", r"'\''")
            f.write(f"file '{escaped}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        out_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        os.remove(list_path)
    if result.returncode != 0:
        raise PipelineError(f"failed to splice narration pauses together: {result.stderr}")


def _trim_segment_to_content(
    seg_path: str, chunks: list[TimedChunk]
) -> tuple[str, list[TimedChunk]]:
    """edge-tts pads each synthesized clip with its own leading/trailing
    silence — independent of, and typically much larger than, the
    deliberate pause _synthesize_with_sentence_pauses is trying to insert
    (measured as long as ~900ms trailing silence on an isolated sentence).
    Left in, splicing sentence clips together stacks that padding on top
    of the requested pause, producing gaps far longer than asked for.
    Trimming each clip down to exactly its WordBoundary-measured
    [start, end] span — the same timing data subtitle.py already treats as
    the single source of truth — keeps the gap between sentences equal to
    what sentence_pause_ms actually asked for."""
    if not chunks:
        return seg_path, chunks

    lead_ms = chunks[0].start_ms
    content_end_ms = chunks[-1].end_ms
    if lead_ms <= 0 and content_end_ms <= 0:
        return seg_path, chunks

    trimmed_path = f"{seg_path}.trimmed.mp3"
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(lead_ms / 1000),
        "-i", seg_path,
        "-t", str(max(content_end_ms - lead_ms, 1) / 1000),
        "-c", "copy",
        trimmed_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise PipelineError(f"failed to trim narration segment padding: {result.stderr}")

    shifted_chunks = [
        TimedChunk(c.text, c.start_ms - lead_ms, c.end_ms - lead_ms) for c in chunks
    ]
    return trimmed_path, shifted_chunks


def _synthesize_with_sentence_pauses(
    text: str,
    voice: str,
    out_path: str,
    rate: str,
    volume: str,
    sentence_pause_ms: int,
) -> list[TimedChunk]:
    sentences = split_into_sentences(text)
    if sentence_pause_ms <= 0 or len(sentences) <= 1:
        return synthesize(text, voice, out_path, rate=rate, volume=volume)

    segment_paths: list[str] = []
    cleanup_paths: list[str] = []
    all_chunks: list[TimedChunk] = []
    cumulative_ms = 0
    try:
        for i, sentence in enumerate(sentences):
            raw_seg_path = f"{out_path}.part{i}.mp3"
            seg_chunks = synthesize(sentence, voice, raw_seg_path, rate=rate, volume=volume)
            cleanup_paths.append(raw_seg_path)

            seg_path, seg_chunks = _trim_segment_to_content(raw_seg_path, seg_chunks)
            if seg_path != raw_seg_path:
                cleanup_paths.append(seg_path)

            all_chunks.extend(
                TimedChunk(c.text, c.start_ms + cumulative_ms, c.end_ms + cumulative_ms)
                for c in seg_chunks
            )
            segment_paths.append(seg_path)
            cumulative_ms += get_audio_duration_ms(seg_path)

            if i < len(sentences) - 1:
                pause_path = f"{out_path}.pause{i}.mp3"
                _generate_silent_audio(pause_path, sentence_pause_ms)
                cleanup_paths.append(pause_path)
                segment_paths.append(pause_path)
                cumulative_ms += sentence_pause_ms

        _concatenate_audio_segments(segment_paths, out_path)
    finally:
        for p in cleanup_paths:
            if os.path.exists(p):
                os.remove(p)

    return all_chunks


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
    subtitle_margin_v: int = DEFAULT_SUBTITLE_MARGIN_V,
    logo_path: str | None = None,
    logo_width: int = DEFAULT_LOGO_WIDTH,
    logo_margin: int = DEFAULT_LOGO_MARGIN,
    logo_opacity: float = DEFAULT_LOGO_OPACITY,
    logo_position: str = DEFAULT_LOGO_POSITION,
    bgm_path: str | None = None,
    bgm_volume: float = DEFAULT_BGM_VOLUME,
    intro_path: str | None = None,
    outro_path: str | None = None,
    custom_dict_path: str | None = None,
    sentence_pause_ms: int = DEFAULT_SENTENCE_PAUSE_MS,
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
                chunks = _synthesize_with_sentence_pauses(
                    script_text, voice, audio_path, voice_rate, voice_volume, sentence_pause_ms
                )
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
            subtitle_margin_v=subtitle_margin_v,
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
        )
    except VideoComposeError as exc:
        raise PipelineError(f"video composition failed: {exc}") from exc

    try:
        return export_outputs(video_path, srt_path, cleaned_scripts, out_dir, base_name)
    except ExportError as exc:
        raise PipelineError(f"export failed: {exc}") from exc
