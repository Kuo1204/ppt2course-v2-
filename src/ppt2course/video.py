"""STEP 5: Video composition — xfade transitions, subtitle burn-in, optional
Logo overlay / BGM mixing / intro-outro concatenation.

This module is the single place that computes the post-transition cumulative
timeline, so the burned-in subtitles and the xfade/acrossfade-shortened video
always agree on where each slide actually starts — per-slide TimedChunk lists
(STEP3's raw, un-offset output) go in, offsets are computed here, and
STEP4's generate_cues/cues_to_srt are called with those offsets baked in.
"""

import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from ppt2course.audio_duration import get_audio_duration_ms
from ppt2course.subtitle import MIN_GAP_MS, SubtitleCue, TimedChunk, cues_to_srt, generate_cues

DEFAULT_TRANSITION = "fade"
DEFAULT_TRANSITION_DURATION_MS = 500
DEFAULT_RESOLUTION = (1920, 1080)
DEFAULT_FPS = 30
DEFAULT_FONT_SIZE = 48
DEFAULT_LOGO_WIDTH = 160
DEFAULT_LOGO_MARGIN = 24
DEFAULT_BGM_VOLUME = 0.2

# ASS BackColour alpha: 00=opaque .. FF=transparent. 0x33 ≈ 20% transparent
# i.e. ~80% opaque black box, per the confirmed subtitle styling default.
SUBTITLE_BACK_ALPHA = "33"


class VideoComposeError(Exception):
    pass


@dataclass(frozen=True)
class SlideVideoInput:
    image_path: str
    audio_path: str
    chunks: list[TimedChunk]


def _compute_slide_offsets(durations_ms: list[int], transition_ms: int) -> list[int]:
    if not durations_ms:
        return []
    offsets = [0]
    for i in range(1, len(durations_ms)):
        offsets.append(offsets[i - 1] + durations_ms[i - 1] - transition_ms)
    return offsets


def _total_duration_ms(durations_ms: list[int], transition_ms: int) -> int:
    if not durations_ms:
        return 0
    offsets = _compute_slide_offsets(durations_ms, transition_ms)
    return offsets[-1] + durations_ms[-1]


def _delay_cues(cues: list[SubtitleCue], delay_ms: int) -> list[SubtitleCue]:
    return [
        SubtitleCue(index=c.index, start_ms=c.start_ms + delay_ms, end_ms=c.end_ms + delay_ms, text=c.text)
        for c in cues
    ]


def _build_cues(
    slides: list[SlideVideoInput], durations_ms: list[int], transition_ms: int
) -> list[SubtitleCue]:
    offsets = _compute_slide_offsets(durations_ms, transition_ms)
    per_slide_cues = [
        generate_cues(slide.chunks, start_offset_ms=offset)
        for slide, offset in zip(slides, offsets)
    ]

    # Consecutive slides' narration overlaps during the crossfade transition
    # (acrossfade), which can make a slide's cues start before the previous
    # slide's last cue naturally ends. Rather than truncating the earlier
    # cue early (STEP4's normal gap-trim, which assumes non-overlapping
    # input), the later slide's cues are delayed as a block until they clear
    # the previous slide's last cue by MIN_GAP_MS — so subtitles never
    # overlap, and a slide's own last cue is never cut short.
    reconciled: list[list[SubtitleCue]] = []
    prev_last_end_ms: int | None = None
    for cues in per_slide_cues:
        if cues and prev_last_end_ms is not None:
            min_start = prev_last_end_ms + MIN_GAP_MS
            if cues[0].start_ms < min_start:
                cues = _delay_cues(cues, min_start - cues[0].start_ms)
        reconciled.append(cues)
        if cues:
            prev_last_end_ms = cues[-1].end_ms

    flat = [cue for cues in reconciled for cue in cues]
    return [
        SubtitleCue(index=i + 1, start_ms=c.start_ms, end_ms=c.end_ms, text=c.text)
        for i, c in enumerate(flat)
    ]


def _escape_ffmpeg_filter_path(path: str) -> str:
    escaped = path.replace("\\", "\\\\").replace(":", "\\:")
    return escaped.replace("'", "\\'")


def _scale_pad_filter(index: int, width: int, height: int, fps: int) -> str:
    return (
        f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{index}]"
    )


def _audio_label_filter(input_index: int, slide_index: int) -> str:
    return f"[{input_index}:a]anull[a{slide_index}]"


def _video_xfade_chain(
    n: int, transition: str, transition_duration_ms: int, offsets_ms: list[int]
) -> tuple[str, str]:
    if n == 1:
        return "", "v0"

    transition_sec = transition_duration_ms / 1000
    parts = []
    prev_label = "v0"
    for i in range(1, n):
        out_label = "vout" if i == n - 1 else f"vx{i}"
        offset_sec = offsets_ms[i] / 1000
        parts.append(
            f"[{prev_label}][v{i}]xfade=transition={transition}:"
            f"duration={transition_sec}:offset={offset_sec}[{out_label}]"
        )
        prev_label = out_label
    return ";".join(parts), prev_label


def _audio_acrossfade_chain(n: int, transition_duration_ms: int) -> tuple[str, str]:
    if n == 1:
        return "", "a0"

    transition_sec = transition_duration_ms / 1000
    parts = []
    prev_label = "a0"
    for i in range(1, n):
        out_label = "aout" if i == n - 1 else f"ax{i}"
        parts.append(
            f"[{prev_label}][a{i}]acrossfade=d={transition_sec}:c1=tri:c2=tri[{out_label}]"
        )
        prev_label = out_label
    return ";".join(parts), prev_label


def _subtitle_filter(srt_path: str, video_label: str, font_size: int) -> tuple[str, str]:
    escaped_path = _escape_ffmpeg_filter_path(srt_path)
    style = (
        f"FontSize={font_size},PrimaryColour=&H00FFFFFF,"
        f"BackColour=&H{SUBTITLE_BACK_ALPHA}000000,BorderStyle=3,Outline=0,Shadow=0"
    )
    filt = f"[{video_label}]subtitles='{escaped_path}':force_style='{style}'[vsub]"
    return filt, "vsub"


def _build_ffmpeg_command(
    slides: list[SlideVideoInput],
    durations_ms: list[int],
    offsets_ms: list[int],
    srt_path: str,
    out_video_path: str,
    transition: str,
    transition_duration_ms: int,
    resolution: tuple[int, int],
    fps: int,
    font_size: int,
) -> list[str]:
    width, height = resolution
    n = len(slides)

    cmd = ["ffmpeg", "-y"]
    for slide, duration_ms in zip(slides, durations_ms):
        cmd += ["-loop", "1", "-t", str(duration_ms / 1000), "-i", slide.image_path]
    for slide in slides:
        cmd += ["-i", slide.audio_path]

    scale_filters = [_scale_pad_filter(i, width, height, fps) for i in range(n)]
    audio_label_filters = [_audio_label_filter(n + i, i) for i in range(n)]
    xfade_filter, video_label = _video_xfade_chain(n, transition, transition_duration_ms, offsets_ms)
    audio_filter, audio_label = _audio_acrossfade_chain(n, transition_duration_ms)
    sub_filter, final_video_label = _subtitle_filter(srt_path, video_label, font_size)

    filter_parts = scale_filters + audio_label_filters
    if xfade_filter:
        filter_parts.append(xfade_filter)
    filter_parts.append(sub_filter)
    if audio_filter:
        filter_parts.append(audio_filter)

    filter_complex = ";".join(filter_parts)

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        f"[{final_video_label}]",
        "-map",
        f"[{audio_label}]",
        "-r",
        str(fps),
        out_video_path,
    ]
    return cmd


def _run_ffmpeg(cmd: list[str], step_description: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise VideoComposeError(
            f"ffmpeg failed during {step_description} (exit {result.returncode}): {result.stderr}"
        )


def _add_logo_overlay(
    video_path: str, logo_path: str, out_path: str, logo_width: int, margin: int
) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", logo_path,
        "-filter_complex",
        f"[1:v]scale={logo_width}:-1[logo];[0:v][logo]overlay=W-w-{margin}:{margin}",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    _run_ffmpeg(cmd, "logo overlay")


def _mix_background_music(
    video_path: str, bgm_path: str, out_path: str, bgm_volume: float
) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1", "-i", bgm_path,
        "-filter_complex",
        f"[1:a]volume={bgm_volume}[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        out_path,
    ]
    _run_ffmpeg(cmd, "background music mixing")


def _concatenate_with_intro_outro(
    main_video_path: str,
    out_path: str,
    intro_path: str | None,
    outro_path: str | None,
    resolution: tuple[int, int],
    fps: int,
) -> None:
    parts = [p for p in (intro_path, main_video_path, outro_path) if p]

    if len(parts) == 1:
        shutil.copy(main_video_path, out_path)
        return

    width, height = resolution
    cmd = ["ffmpeg", "-y"]
    for p in parts:
        cmd += ["-i", p]

    filter_segments = []
    for idx in range(len(parts)):
        filter_segments.append(
            f"[{idx}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={fps},setsar=1,format=yuv420p[v{idx}];"
            f"[{idx}:a]aformat=sample_rates=44100:channel_layouts=stereo[a{idx}]"
        )
    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(len(parts)))
    filter_complex = (
        ";".join(filter_segments)
        + f";{concat_inputs}concat=n={len(parts)}:v=1:a=1[outv][outa]"
    )

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p",
        out_path,
    ]
    _run_ffmpeg(cmd, "intro/outro concatenation")


def compose_video(
    slides: list[SlideVideoInput],
    out_video_path: str,
    out_srt_path: str,
    transition: str = DEFAULT_TRANSITION,
    transition_duration_ms: int = DEFAULT_TRANSITION_DURATION_MS,
    resolution: tuple[int, int] = DEFAULT_RESOLUTION,
    fps: int = DEFAULT_FPS,
    font_size: int = DEFAULT_FONT_SIZE,
    logo_path: str | None = None,
    logo_width: int = DEFAULT_LOGO_WIDTH,
    logo_margin: int = DEFAULT_LOGO_MARGIN,
    bgm_path: str | None = None,
    bgm_volume: float = DEFAULT_BGM_VOLUME,
    intro_path: str | None = None,
    outro_path: str | None = None,
) -> None:
    if not slides:
        raise VideoComposeError("slides must not be empty")

    if shutil.which("ffmpeg") is None:
        raise VideoComposeError("ffmpeg executable not found")

    durations_ms = [get_audio_duration_ms(s.audio_path) for s in slides]
    offsets_ms = _compute_slide_offsets(durations_ms, transition_duration_ms)

    cues = _build_cues(slides, durations_ms, transition_duration_ms)
    with open(out_srt_path, "w", encoding="utf-8", newline="") as f:
        f.write(cues_to_srt(cues))

    needs_post_processing = bool(logo_path or bgm_path or intro_path or outro_path)
    temp_dir = tempfile.mkdtemp(prefix="ppt2course_video_") if needs_post_processing else None

    try:
        core_output = f"{temp_dir}/01_core.mp4" if needs_post_processing else out_video_path

        cmd = _build_ffmpeg_command(
            slides,
            durations_ms,
            offsets_ms,
            out_srt_path,
            core_output,
            transition,
            transition_duration_ms,
            resolution,
            fps,
            font_size,
        )
        _run_ffmpeg(cmd, "slide/transition/subtitle composition")

        current = core_output

        if logo_path:
            next_path = f"{temp_dir}/02_logo.mp4"
            _add_logo_overlay(current, logo_path, next_path, logo_width, logo_margin)
            current = next_path

        if bgm_path:
            next_path = f"{temp_dir}/03_bgm.mp4"
            _mix_background_music(current, bgm_path, next_path, bgm_volume)
            current = next_path

        if intro_path or outro_path:
            _concatenate_with_intro_outro(
                current, out_video_path, intro_path, outro_path, resolution, fps
            )
        elif current != out_video_path:
            shutil.copy(current, out_video_path)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
